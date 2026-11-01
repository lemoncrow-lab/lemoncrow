"""Hermetic fetch -> build -> run environment (Phase 9R items 3, 25, 30; Phase 14 items 16, 34, 35).

The registry here is a real HTTP server on loopback, allowlisted through the
``private_network`` escape hatch exactly as a customer's private registry would
be. Tests that execute in bubblewrap skip with an explicit reason when the
host cannot create unprivileged namespaces.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.execution import ExecutorClass, cache_namespace
from lemoncrow.pro.capabilities.review.hermetic import (
    DependencyCache,
    HermeticPolicy,
    HermeticRefused,
    HermeticUnavailable,
    RecordingSinks,
    RegistryFetcher,
    RegistryRule,
    hermetic_context,
    hermetic_policy_from_wire,
    load_hermetic_policy,
    offline_bwrap_prefix,
    plan_npm_install,
    provision_npm_dependencies,
)
from lemoncrow.pro.capabilities.review.previews import PreviewSandbox, PreviewUnavailable
from lemoncrow.pro.capabilities.review.run_evidence import RunArtifactStore, execution_evidence_for_model


def _bwrap_works() -> bool:
    binary = shutil.which("bwrap")
    if not binary:
        return False
    try:
        proc = subprocess.run(
            [binary, "--unshare-all", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


needs_bwrap = pytest.mark.skipif(
    not _bwrap_works(), reason="bubblewrap with unprivileged user/network namespaces is unavailable on this host"
)


class _Server:
    """A loopback HTTP server that records every request it receives."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.redirects: dict[str, str] = {}
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def _any(self) -> None:
                outer.requests.append(f"{self.command} {self.path}")
                if self.path in outer.redirects:
                    self.send_response(302)
                    self.send_header("Location", outer.redirects[self.path])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = outer.files.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_CONNECT = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str, host: str = "localhost") -> str:
        return f"http://{host}:{self.port}{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def registry() -> Iterator[_Server]:
    server = _Server()
    yield server
    server.close()


@pytest.fixture
def outsider() -> Iterator[_Server]:
    """An external listener nothing in the sandbox may reach."""

    server = _Server()
    yield server
    server.close()


def _tarball(
    name: str,
    version: str,
    *,
    files: dict[str, str] | None = None,
    scripts: dict[str, str] | None = None,
    manifest_name: str | None = None,
    bins: dict[str, str] | None = None,
    raw_members: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    manifest: dict[str, Any] = {"name": manifest_name or name, "version": version}
    if scripts:
        manifest["scripts"] = scripts
    if bins:
        manifest["bin"] = bins
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:

        def add(member: str, data: bytes, mode: int = 0o644) -> None:
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))

        add("package/package.json", json.dumps(manifest).encode())
        for path, body in (files or {}).items():
            add(f"package/{path}", body.encode())
        for member, data in raw_members:
            add(member, data)
    return buffer.getvalue()


def _sri(payload: bytes, algorithm: str = "sha512") -> str:
    return f"{algorithm}-" + base64.b64encode(hashlib.new(algorithm, payload).digest()).decode()


def _tar_path(name: str, version: str) -> str:
    return f"/{name}/-/{name.split('/')[-1]}-{version}.tgz"


def _project(
    root: Path,
    server: _Server,
    packages: list[tuple[str, str, bytes]],
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
    root_scripts: dict[str, str] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "name": "app",
        "version": "1.0.0",
        "dependencies": {name: version for name, version, _ in packages},
    }
    if root_scripts:
        manifest["scripts"] = root_scripts
    lock: dict[str, Any] = {
        "name": "app",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {"": {"name": "app", "version": "1.0.0", "dependencies": manifest["dependencies"]}},
    }
    for name, version, payload in packages:
        path = _tar_path(name, version)
        server.files[path] = payload
        entry: dict[str, Any] = {"version": version, "resolved": server.url(path), "integrity": _sri(payload)}
        entry.update((overrides or {}).get(name, {}))
        lock["packages"][f"node_modules/{name}"] = entry
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    return root


def _policy(server: _Server, **execution: Any) -> HermeticPolicy:
    registries = execution.pop(
        "registries", [{"host": "localhost", "scheme": "http", "port": server.port, "private_network": True}]
    )
    return load_hermetic_policy(yaml.safe_dump({"execution": {"registries": registries, **execution}}))


# --------------------------------------------------------------------------- #
# Trusted policy authority
# --------------------------------------------------------------------------- #


def test_policy_defaults_to_public_npm_and_only_reads_the_trusted_execution_block() -> None:
    default = load_hermetic_policy(None)
    assert [(rule.ecosystem, rule.host, rule.path_prefix) for rule in default.registries] == [
        ("npm", "registry.npmjs.org", "/"),
        ("pypi", "pypi.org", "/simple"),
        ("pypi", "files.pythonhosted.org", "/packages/"),
    ]
    assert all(rule.scheme == "https" and not rule.private_network for rule in default.registries)
    # Other trusted-base keys are not execution policy.
    assert load_hermetic_policy("reviewers: [a]\n").registries == default.registries

    with pytest.raises(ValueError, match="unknown fields: install_command"):
        load_hermetic_policy("execution:\n  install_command: npm install\n")
    with pytest.raises(ValueError, match="plain-http registries are allowed only with private_network"):
        load_hermetic_policy("execution:\n  registries:\n    - host: registry.example.com\n      scheme: http\n")
    with pytest.raises(ValueError, match="without wildcards"):
        load_hermetic_policy("execution:\n  registries:\n    - host: '*.example.com'\n")


def test_organization_registry_list_is_a_ceiling_over_the_trusted_base() -> None:
    base = yaml.safe_dump(
        {
            "execution": {
                "registries": [{"host": "registry.npmjs.org"}, {"host": "npm.attacker.example"}],
                "redact": ["cust-[0-9]+"],
            }
        }
    )
    policy = load_hermetic_policy(base, organization={"registries": [{"host": "registry.npmjs.org"}]})

    # The organization list is the whole ceiling: it names no pypi registry, so Python has none.
    assert [rule.host for rule in policy.registries] == ["registry.npmjs.org"]
    assert policy.redaction.patterns == ("cust-[0-9]+",)
    assert policy.source == "organization+trusted_base"
    with pytest.raises(HermeticRefused, match="no pypi registry is allowlisted"):
        policy.require_fetch("pypi")


def test_public_default_applies_per_ecosystem_only_while_the_trusted_base_declares_none() -> None:
    npm_mirror = load_hermetic_policy("execution:\n  registries: [{host: npm.corp.example}]\n")
    assert [(rule.ecosystem, rule.host) for rule in npm_mirror.registries] == [
        ("npm", "npm.corp.example"),
        ("pypi", "pypi.org"),
        ("pypi", "files.pythonhosted.org"),
    ]
    assert npm_mirror.registry_for("https://registry.npmjs.org/a/-/a-1.0.0.tgz") is None
    pypi_mirror = load_hermetic_policy(
        "execution:\n  registries: [{host: pypi.corp.example, ecosystem: pypi, path_prefix: /simple}]\n"
    )
    assert [(rule.ecosystem, rule.host) for rule in pypi_mirror.registries] == [
        ("pypi", "pypi.corp.example"),
        ("npm", "registry.npmjs.org"),
    ]
    assert pypi_mirror.registry_for("https://files.pythonhosted.org/packages/x.whl", "pypi") is None
    # An npm rule never serves a pypi lookup and vice versa.
    assert npm_mirror.registry_for("https://npm.corp.example/simple/x/", "pypi") is None
    with pytest.raises(ValueError, match="ecosystem must be one of"):
        load_hermetic_policy("execution:\n  registries: [{host: x.example, ecosystem: cargo}]\n")


@pytest.mark.parametrize("layer", ["organization", "trusted_base"])
def test_deny_all_allows_no_registry_and_refuses_every_fetch(tmp_path: Path, layer: str) -> None:
    base = "execution:\n  registries: [{host: npm.corp.example}]\n"
    if layer == "organization":
        policy = load_hermetic_policy(base, organization={"deny_all": True})
    else:
        policy = load_hermetic_policy("execution:\n  deny_all: true\n", organization={"registries": []})
    assert policy.deny_all is True and policy.registries == ()
    assert policy.registry_for("https://registry.npmjs.org/a/-/a-1.0.0.tgz") is None
    for ecosystem in ("npm", "pypi"):
        with pytest.raises(HermeticRefused, match="deny_all"):
            policy.require_fetch(ecosystem)
    project = _write_pkg(tmp_path / "web", {"left-pad": "1.0.0"})
    (project / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8")
    with pytest.raises(HermeticRefused, match="deny_all"):
        plan_npm_install(project, policy)
    # The recorded policy keeps deny_all and never re-adds public defaults.
    recorded = hermetic_policy_from_wire(policy.to_wire())
    assert recorded.deny_all is True and recorded.registries == ()
    with pytest.raises(ValueError, match="deny_all must be boolean"):
        load_hermetic_policy("execution:\n  deny_all: yes please\n")


def test_invalid_trusted_policy_yields_a_refusing_context_not_silent_defaults(tmp_path: Path) -> None:
    context = hermetic_context(
        trusted_review_config="execution:\n  registries: [{host: 'bad host'}]\n",
        store_root=tmp_path,
        review_id="rv",
        side="new",
    )
    assert context.policy_error
    assert context.namespace == "head_untrusted"
    assert context.executor_class is ExecutorClass.AUTHOR_MACHINE

    workspace = tmp_path / "ws"
    _write_pkg(workspace / "web", {"left-pad": "1.0.0"})
    _write_pkg(tmp_path / "checkout" / "web", {"left-pad": "0.9.0"})
    sandbox = PreviewSandbox(
        workspace=workspace,
        output_dir=tmp_path / "out",
        trusted_checkout=tmp_path / "checkout",
        deadline=1e12,
        hermetic=context,
    )
    with pytest.raises(PreviewUnavailable, match="trusted-base execution policy is invalid"):
        sandbox.node_modules("web")
    assert sandbox.stages[-1].status == "refused"


def _write_pkg(root: Path, dependencies: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({"name": "app", "dependencies": dependencies}), encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# Fetch-stage plan: registry-only, dependency confusion, lookalikes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("resolved", "message"),
    [
        ("http://evil.localhost:{port}/left-pad/-/left-pad-1.0.0.tgz", "outside the allowlisted registries"),
        ("http://localhost:1/left-pad/-/left-pad-1.0.0.tgz", "outside the allowlisted registries"),
        ("http://localhost:{port}@evil.test/left-pad/-/left-pad-1.0.0.tgz", "outside the allowlisted registries"),
        ("https://localhost:{port}/left-pad/-/left-pad-1.0.0.tgz", "outside the allowlisted registries"),
        ("git+https://github.com/attacker/left-pad.git", "outside the allowlisted registries"),
        ("http://localhost:{port}/1eft-pad/-/1eft-pad-1.0.0.tgz", "tarball for a different package"),
        ("http://localhost:{port}/left-pad/-/left-pad-6.6.6.tgz", "tarball for a different package"),
        ("http://localhost:{port}/left-pad/../evil/-/left-pad-1.0.0.tgz", "outside the allowlisted registries"),
    ],
)
def test_plan_refuses_anything_but_an_exact_allowlisted_registry_tarball(
    tmp_path: Path, registry: _Server, resolved: str, message: str
) -> None:
    payload = _tarball("left-pad", "1.0.0")
    project = _project(
        tmp_path / "p",
        registry,
        [("left-pad", "1.0.0", payload)],
        overrides={"left-pad": {"resolved": resolved.format(port=registry.port)}},
    )
    with pytest.raises(HermeticRefused, match=message):
        plan_npm_install(project, _policy(registry))


def test_registry_failures_never_expose_query_grants_or_path_credentials(registry: _Server) -> None:
    """Reviewer-visible NOT_RUN explanations must not echo arbitrary registry secrets."""
    private = "synthetic-query-private-token-canary"
    path = f"/signed-{private}/package.tgz?access_token={private}"
    policy = HermeticPolicy(registries=(RegistryRule("localhost", "http", registry.port, private_network=True),))
    fetcher = RegistryFetcher(policy, timeout=2)
    url = registry.url(path)
    with pytest.raises(HermeticUnavailable, match="HTTP 404") as status:
        fetcher.fetch(url)
    assert private not in str(status.value)
    assert f"localhost:{registry.port}" in str(status.value)
    registry.files[path] = b"ab"
    with pytest.raises(HermeticRefused, match="tarball exceeds") as size:
        fetcher.fetch(url, max_bytes=1)
    assert private not in str(size.value)
    refused = RegistryFetcher(HermeticPolicy(registries=()))
    with pytest.raises(HermeticRefused, match="not an allowlisted registry") as denied:
        refused.fetch(f"https://unused.invalid/signed-{private}?token={private}")
    assert private not in str(denied.value)


def test_registry_fetch_cancellation_interrupts_blocked_real_http_body() -> None:
    """An accepted worker must not await the full registry timeout after lease loss."""
    sent_headers = threading.Event()
    release = threading.Event()
    revoked = threading.Event()

    class StalledBody(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            self.wfile.flush()
            sent_headers.set()
            release.wait(timeout=12)
            try:
                self.wfile.write(b"y" * 99990)
            except (OSError, ValueError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StalledBody)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        policy = HermeticPolicy(
            registries=(RegistryRule("localhost", "http", server.server_port, private_network=True),)
        )
        fetcher = RegistryFetcher(policy, timeout=15, should_cancel=revoked.is_set)

        def revoke() -> None:
            assert sent_headers.wait(timeout=4)
            revoked.set()

        canceller = threading.Thread(target=revoke)
        canceller.start()
        started = time.monotonic()
        with pytest.raises(HermeticUnavailable, match="cancelled by revoked authority"):
            fetcher.fetch(f"http://localhost:{server.server_port}/package.tgz")
        assert time.monotonic() - started < 4
        canceller.join(timeout=2)
        assert not canceller.is_alive()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_plan_refuses_weak_or_missing_integrity_and_workspace_links(tmp_path: Path, registry: _Server) -> None:
    payload = _tarball("left-pad", "1.0.0")
    cases = [
        ({"integrity": _sri(payload, "sha1")}, "sha1-only is refused"),
        ({"integrity": ""}, "no resolved URL and integrity"),
        ({"link": True}, "workspace link"),
    ]
    for index, (override, message) in enumerate(cases):
        project = _project(
            tmp_path / f"p{index}", registry, [("left-pad", "1.0.0", payload)], overrides={"left-pad": override}
        )
        with pytest.raises(HermeticRefused, match=message):
            plan_npm_install(project, _policy(registry))


def test_scoped_names_are_bound_to_their_pinned_registry(tmp_path: Path, registry: _Server) -> None:
    """Dependency confusion: a pinned private scope can never resolve from the public registry."""

    payload = _tarball("@acme/ui", "1.0.0")
    rules = [
        {"host": "localhost", "scheme": "http", "port": registry.port, "private_network": True, "scopes": ["@acme"]},
        {"host": "127.0.0.1", "scheme": "http", "port": registry.port, "private_network": True},
    ]
    policy = _policy(registry, registries=rules)
    confused = _project(
        tmp_path / "confused",
        registry,
        [("@acme/ui", "1.0.0", payload)],
        overrides={"@acme/ui": {"resolved": registry.url(_tar_path("@acme/ui", "1.0.0"), host="127.0.0.1")}},
    )
    with pytest.raises(HermeticRefused, match="dependency confusion"):
        plan_npm_install(confused, policy)

    public_from_private = _project(
        tmp_path / "public",
        registry,
        [("lodash", "4.17.21", _tarball("lodash", "4.17.21"))],
    )
    with pytest.raises(HermeticRefused, match="not a package this registry"):
        plan_npm_install(public_from_private, policy)

    good = _project(tmp_path / "good", registry, [("@acme/ui", "1.0.0", payload)])
    assert [pkg.name for pkg in plan_npm_install(good, policy).packages] == ["@acme/ui"]


# --------------------------------------------------------------------------- #
# Fetch-stage network: redirects, DNS, proxies, integrity
# --------------------------------------------------------------------------- #


def test_registry_redirect_to_a_non_registry_host_is_refused_before_any_request(
    registry: _Server, outsider: _Server
) -> None:
    path = _tar_path("left-pad", "1.0.0")
    registry.redirects[path] = outsider.url("/exfiltrate")

    with pytest.raises(HermeticRefused, match="not an allowlisted registry"):
        RegistryFetcher(_policy(registry)).fetch(registry.url(path))

    assert registry.requests == [f"GET {path}"]
    assert outsider.requests == []


def test_registry_resolving_to_a_private_address_is_refused_without_private_network(registry: _Server) -> None:
    """DNS rebinding: a public registry name that answers with a loopback/private IP is refused."""

    policy = HermeticPolicy(registries=(RegistryRule("localhost", port=registry.port),))

    with pytest.raises(HermeticRefused, match="non-public address"):
        RegistryFetcher(policy).fetch(f"https://localhost:{registry.port}/left-pad/-/left-pad-1.0.0.tgz")
    assert registry.requests == []


def test_fetch_ignores_ambient_proxy_settings(
    registry: _Server, outsider: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, outsider.url("/"))
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    payload = _tarball("left-pad", "1.0.0")
    registry.files[_tar_path("left-pad", "1.0.0")] = payload

    assert RegistryFetcher(_policy(registry)).fetch(registry.url(_tar_path("left-pad", "1.0.0"))) == payload
    assert outsider.requests == []


def test_tampered_tarball_fails_integrity_and_is_never_cached(tmp_path: Path, registry: _Server) -> None:
    payload = _tarball("left-pad", "1.0.0")
    project = _project(tmp_path / "p", registry, [("left-pad", "1.0.0", payload)])
    registry.files[_tar_path("left-pad", "1.0.0")] = _tarball("left-pad", "1.0.0", files={"index.js": "evil()"})
    cache = DependencyCache.for_store(tmp_path / "store", "head_untrusted")

    with pytest.raises(HermeticRefused, match="does not match its lockfile integrity"):
        provision_npm_dependencies(project, policy=_policy(registry), cache=cache)
    assert not cache.writable_tarballs.exists() or not any(cache.writable_tarballs.iterdir())


# --------------------------------------------------------------------------- #
# Build stage: offline, no lifecycle scripts, confined extraction
# --------------------------------------------------------------------------- #


@needs_bwrap
def test_dependency_change_provisions_hermetically_without_lifecycle_scripts(tmp_path: Path, registry: _Server) -> None:
    marker = tmp_path / "lifecycle-ran"
    hostile = _tarball(
        "hostile",
        "1.0.0",
        files={"index.js": "module.exports = 1", "cli.js": "#!/usr/bin/env node\n"},
        scripts={
            "preinstall": f"touch {marker}",
            "install": f"touch {marker}",
            "postinstall": f"curl http://127.0.0.1:{registry.port}/beacon; touch {marker}",
        },
        bins={"hostile": "cli.js"},
    )
    project = _project(
        tmp_path / "p",
        registry,
        [("hostile", "1.0.0", hostile)],
        overrides={"hostile": {"hasInstallScript": True}},
        root_scripts={"postinstall": f"touch {marker}"},
    )
    cache = DependencyCache.for_store(tmp_path / "store", "head_untrusted")

    tree = provision_npm_dependencies(project, policy=_policy(registry), cache=cache)

    assert not marker.exists()
    assert all("beacon" not in request for request in registry.requests)
    assert (tree.node_modules / "hostile" / "index.js").read_text() == "module.exports = 1"
    assert os.readlink(tree.node_modules / ".bin" / "hostile") == "../hostile/cli.js"
    fetch, build = tree.stages
    assert (fetch.stage, fetch.status, fetch.network) == ("fetch", "passed", "registry_only")
    assert (build.stage, build.status, build.network) == ("build", "passed", "denied")
    assert "hostile@1.0.0: install scripts" in fetch.blocked
    assert "(project): postinstall" in fetch.blocked
    assert {"hostile@1.0.0: preinstall", "hostile@1.0.0: postinstall"} <= set(build.blocked)
    assert tree.node_modules.is_relative_to(tmp_path / "store" / "review" / "hermetic" / "head_untrusted")

    # The second run is a cache hit inside the same namespace and fetches nothing.
    before = list(registry.requests)
    again = provision_npm_dependencies(project, policy=_policy(registry), cache=cache)
    assert again.node_modules == tree.node_modules and registry.requests == before
    assert again.stages[0].cache == "hit"


@needs_bwrap
def test_lookalike_tarball_contents_are_refused(tmp_path: Path, registry: _Server) -> None:
    payload = _tarball("left-pad", "1.0.0", manifest_name="left-pad-but-evil")
    project = _project(tmp_path / "p", registry, [("left-pad", "1.0.0", payload)])

    with pytest.raises(HermeticRefused, match="lookalike package"):
        provision_npm_dependencies(
            project, policy=_policy(registry), cache=DependencyCache.for_store(tmp_path / "s", "head_untrusted")
        )


@needs_bwrap
def test_tarball_path_traversal_is_refused_and_writes_nothing_outside(tmp_path: Path, registry: _Server) -> None:
    payload = _tarball("left-pad", "1.0.0", raw_members=(("package/../../../escape.txt", b"owned"),))
    project = _project(tmp_path / "p", registry, [("left-pad", "1.0.0", payload)])
    store = tmp_path / "s"

    with pytest.raises(HermeticRefused, match="escapes its package"):
        provision_npm_dependencies(
            project, policy=_policy(registry), cache=DependencyCache.for_store(store, "head_untrusted")
        )
    assert not list(tmp_path.rglob("escape.txt"))


@needs_bwrap
def test_offline_stage_cannot_reach_listeners_or_read_host_secrets(
    tmp_path: Path, outsider: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "aws" / "credentials"
    secret_file.parent.mkdir()
    secret_file.write_text("aws_secret_access_key=AKIAEXAMPLESECRET")
    monkeypatch.setenv("LEMONCROW_TEST_CONTROL_PLANE_SECRET", "cp-secret-value")
    probe = f"""
import json, os, socket
out = {{}}
try:
    socket.create_connection(("127.0.0.1", {outsider.port}), 2).close()
    out["loopback"] = "connected"
except OSError as exc:
    out["loopback"] = type(exc).__name__
try:
    socket.create_connection(("1.1.1.1", 443), 2).close()
    out["internet"] = "connected"
except OSError as exc:
    out["internet"] = type(exc).__name__
try:
    socket.getaddrinfo("registry.npmjs.org", 443)
    out["dns"] = "resolved"
except OSError as exc:
    out["dns"] = type(exc).__name__
out["home"] = os.path.exists({str(Path.home())!r})
out["ssh"] = os.path.exists({str(Path.home() / ".ssh")!r})
out["secret_file"] = os.path.exists({str(secret_file)!r})
out["docker"] = os.path.exists("/var/run/docker.sock")
out["env"] = sorted(k for k in os.environ if "SECRET" in k or "TOKEN" in k or k.startswith("AWS"))
print(json.dumps(out))
"""
    proc = subprocess.run(
        [*offline_bwrap_prefix(), "--", "python3", "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["loopback"] != "connected" and out["internet"] != "connected" and out["dns"] != "resolved"
    assert out["home"] is False and out["ssh"] is False and out["secret_file"] is False
    assert out["docker"] is False and out["env"] == []
    assert outsider.requests == []


# --------------------------------------------------------------------------- #
# Cache isolation by trust (item 25, Phase 14 item 35)
# --------------------------------------------------------------------------- #


def _fake_tree(cache: DependencyCache, digest: str) -> Path:
    staging, final = cache.new_tree_dir(digest)
    (staging / "node_modules" / "react").mkdir(parents=True)
    (staging / "node_modules" / "react" / "index.js").write_text("poisoned()")
    (staging / "tree.json").write_text(json.dumps({"plan_digest": digest}))
    staging.replace(final)
    return final / "node_modules"


def test_untrusted_head_cache_writes_are_invisible_to_base_and_trusted_runs(tmp_path: Path) -> None:
    store = tmp_path / "store"
    untrusted = DependencyCache.for_store(store, cache_namespace("new", "untrusted_external"))
    unknown = DependencyCache.for_store(store, cache_namespace("new", "unknown"))
    trusted = DependencyCache.for_store(store, cache_namespace("new", "trusted_internal"))
    base = DependencyCache.for_store(store, cache_namespace("old", "untrusted_external"))
    assert (untrusted.namespace, unknown.namespace, trusted.namespace, base.namespace) == (
        "head_untrusted",
        "head_untrusted",
        "head_trusted",
        "base",
    )

    digest = "a" * 64
    poisoned = _fake_tree(untrusted, digest)
    good_payload = b"legit tarball"
    sha = hashlib.sha512(good_payload).hexdigest()
    # The untrusted run plants an entry under the digest of legitimate content.
    untrusted.store_tarball(sha, b"poisoned tarball")

    assert untrusted.tree(digest) == poisoned
    assert base.tree(digest) is None and trusted.tree(digest) is None
    assert base.tarball(sha) is None and trusted.tarball(sha) is None
    # Even inside its own namespace a mismatching tarball is never served.
    assert untrusted.tarball(sha) is None

    # Base entries flow to trusted heads only, never to untrusted ones.
    base_tree = _fake_tree(base, "b" * 64)
    assert trusted.tree("b" * 64) == base_tree
    assert untrusted.tree("b" * 64) is None
    # Writers only ever write their own namespace.
    assert untrusted.writable_tarballs.is_relative_to(store / "review" / "hermetic" / "head_untrusted")


def test_cache_prune_is_the_closer_for_idle_trees(tmp_path: Path) -> None:
    cache = DependencyCache.for_store(tmp_path, "head_untrusted")
    tree = _fake_tree(cache, "c" * 64).parent
    old = 10 * 365 * 24 * 3600
    os.utime(tree, (os.path.getatime(tree) - old, os.path.getmtime(tree) - old))

    cache.prune(force=True)

    assert not tree.exists()


# --------------------------------------------------------------------------- #
# The real caller: preview sandbox executes a dependency-changing side
# --------------------------------------------------------------------------- #


@needs_bwrap
def test_preview_sandbox_runs_a_dependency_changing_side_hermetically(
    tmp_path: Path, registry: _Server, outsider: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_TEST_CONTROL_PLANE_SECRET", "cp-secret-value")
    workspace = tmp_path / "ws"
    payload = _tarball("left-pad", "1.0.0", files={"index.js": "module.exports = 'padded'"})
    _project(workspace / "web", registry, [("left-pad", "1.0.0", payload)])
    _write_pkg(tmp_path / "checkout" / "web", {"left-pad": "0.0.1"})
    output = tmp_path / "out"
    output.mkdir()
    context = hermetic_context(
        trusted_review_config=yaml.safe_dump(
            {
                "execution": {
                    "registries": [
                        {"host": "localhost", "scheme": "http", "port": registry.port, "private_network": True}
                    ],
                    "redact_literals": ["customer-password-123"],
                }
            }
        ),
        store_root=tmp_path / "store",
        review_id="rv-1",
        side="new",
    )
    sandbox = PreviewSandbox(
        workspace=workspace,
        output_dir=output,
        trusted_checkout=tmp_path / "checkout",
        deadline=time.monotonic() + 120,
        hermetic=context,
    )

    modules = sandbox.node_modules("web")
    script = f"""
import json, os, socket
out = {{"dep": open("node_modules/left-pad/index.js").read()}}
for name, port in (("registry", {registry.port}), ("outsider", {outsider.port})):
    try:
        socket.create_connection(("127.0.0.1", port), 2).close()
        out[name] = "connected"
    except OSError as exc:
        out[name] = type(exc).__name__
out["secret_env"] = [k for k in os.environ if "SECRET" in k]
out["ssh"] = os.path.exists({str(Path.home() / ".ssh")!r})
print(json.dumps(out))
print("leaked customer-password-123")
"""
    result = sandbox.run(["python3", "-c", script], cwd="web", node_modules=modules)

    assert result.exit_code == 0, result.output
    out = json.loads(result.output.splitlines()[0])
    assert out["dep"] == "module.exports = 'padded'"
    assert out["registry"] != "connected" and out["outsider"] != "connected"
    assert out["secret_env"] == [] and out["ssh"] is False
    assert "customer-password-123" not in result.output and "<redacted-configured>" in result.output
    assert outsider.requests == []

    execution = sandbox.execution_result(
        review_id="rv-1", revision_id="rev-1", side="new", identity="tree:abc", scope="web", renderer="storybook"
    )
    assert execution is not None
    assert [(stage.stage, stage.status) for stage in execution.stages] == [
        ("fetch", "passed"),
        ("build", "passed"),
        ("build", "passed"),
    ]
    assert execution.executor_class is ExecutorClass.AUTHOR_MACHINE and execution.self_attested
    assert execution.cache_namespace == "head_untrusted"
    assert execution.fingerprint.lockfile_digest == sandbox.lockfile_digest != ""
    assert execution.environment_fingerprint.startswith("envfp:sha256:")
    assert "image " in execution.verification.statement
    assert {item.name for item in execution.verification.not_verified} >= {
        "payment providers",
        "third-party APIs",
        "production data",
    }
    assert all("customer-password-123" not in stage.output for stage in execution.stages)


# --------------------------------------------------------------------------- #
# Run stage: recording sinks (item 30, Phase 14 item 36)
# --------------------------------------------------------------------------- #

_SIDE_EFFECTS = r"""
import json, os, smtplib, socket, urllib.request
results = {}
mail = smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=5)
mail.sendmail("app@example.com", ["ceo@customer.example"],
              "Subject: reset\r\n\r\nyour password is customer-password-123 and key " + "AKIA" + "ABCDEFGHIJKLMNOP")
mail.quit()
results["smtp"] = "sent"
req = urllib.request.Request("http://hooks.example.com/notify", data=json.dumps({"ssn": "customer-password-123"}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
results["webhook"] = urllib.request.urlopen(req, timeout=5).status
try:
    urllib.request.urlopen("https://api.stripe.com/v1/charges", data=b"amount=100", timeout=5)
    results["tls"] = "delivered"
except Exception as exc:
    results["tls"] = type(exc).__name__
put = urllib.request.Request(os.environ["AWS_ENDPOINT_URL_S3"] + "/exports/report.csv", data=b"name,card\nbob,4111111111111111 customer-password-123", method="PUT")
results["s3"] = urllib.request.urlopen(put, timeout=5).status
form = "Action=SendMessage&QueueUrl=https%3A%2F%2Fsqs.example%2Fq&MessageBody=customer-password-123".encode()
results["sqs"] = urllib.request.urlopen(urllib.request.Request(os.environ["AWS_ENDPOINT_URL_SQS"] + "/", data=form), timeout=5).status
for port in (OUTSIDER_PORT,):
    try:
        socket.create_connection(("127.0.0.1", port), 2).close()
        results["direct"] = "connected"
    except OSError as exc:
        results["direct"] = type(exc).__name__
print(json.dumps(results))
"""


@needs_bwrap
def test_run_stage_sinks_record_side_effects_redacted_and_never_deliver(tmp_path: Path, outsider: _Server) -> None:
    policy = load_hermetic_policy(
        yaml.safe_dump({"execution": {"redact_literals": ["customer-password-123"], "redact": ["4111[0-9]{12}"]}})
    )
    sinks = RecordingSinks.create(tmp_path / "sinks")
    command = [
        *offline_bwrap_prefix(),
        *sinks.bwrap_args(),
        "--",
        *sinks.wrap(["python3", "-c", _SIDE_EFFECTS.replace("OUTSIDER_PORT", str(outsider.port))]),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    assert results["smtp"] == "sent" and results["webhook"] == 202 and results["s3"] == 200 and results["sqs"] == 200
    assert results["tls"] != "delivered" and results["direct"] != "connected"
    assert outsider.requests == []

    artifacts = RunArtifactStore.for_store(tmp_path / "store")
    captures, refs = sinks.collect(
        review_id="rv-1", policy=policy.redaction, content_access="ai_denied", artifacts=artifacts
    )
    sinks.cleanup()

    kinds = {capture.kind for capture in captures}
    assert kinds == {"smtp", "http", "object_store", "queue"}
    by_kind = {capture.kind: capture for capture in captures if not capture.blocked}
    # Recipient addresses are personal data: the built-in redaction masks them too.
    assert by_kind["smtp"].destination.startswith("smtp:") and "customer.example" not in by_kind["smtp"].destination
    assert by_kind["http"].destination == "http://hooks.example.com/notify"
    assert by_kind["object_store"].destination == "s3://exports/report.csv"
    assert by_kind["queue"].destination == "https://sqs.example/q"
    blocked = [capture for capture in captures if capture.blocked]
    assert [capture.destination for capture in blocked] == ["https://api.stripe.com:443"]
    for capture in captures:
        assert capture.delivered is False
        assert "customer-password-123" not in capture.payload_preview
        assert "4111111111111111" not in capture.payload_preview
        # Assembled at runtime so the public-mirror credential scan never sees a key-shaped literal.
        assert "AKIA" + "ABCDEFGHIJKLMNOP" not in capture.payload_preview
    assert by_kind["smtp"].redactions >= 2
    for ref in refs:
        stored = artifacts.read("rv-1", ref.digest) or b""
        assert b"customer-password-123" not in stored and b"4111111111111111" not in stored
        assert ref.retention == "raw" and ref.expires_at is not None


def test_ai_denied_sink_payloads_never_reach_a_disallowed_model_route(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.execution import ExecutionRecipe, ExecutionSubject, SinkCapture, StageOutcome
    from lemoncrow.pro.capabilities.review.hermetic import build_execution_result

    capture = SinkCapture(kind="smtp", destination="smtp:a@b", payload_preview="Dear customer, invoice 42")
    stage = StageOutcome("run", "passed", output="pytest log with customer rows")
    common: dict[str, Any] = {
        "subject": ExecutionSubject(review_id="rv", revision_id="r"),
        "executor_class": ExecutorClass.MANAGED_SANDBOX,
        "policy": HermeticPolicy(),
        "recipe": ExecutionRecipe(command=("pytest",)),
        "stages": (stage,),
        "namespace": "head_untrusted",
        "captures": (capture,),
        "image_digest": "sha256:abc",
    }
    denied = build_execution_result(content_access="ai_denied", **common)
    local_only = build_execution_result(content_access="customer_local_only", **common)

    for result, route in ((denied, "external"), (denied, "customer_local"), (local_only, "external")):
        projected = json.dumps(execution_evidence_for_model(result, route_trust=route))
        assert "invoice 42" not in projected and "customer rows" not in projected
        assert "withheld: AI content access" in projected
    # Captures default to ai_denied: the payload stays withheld even on an allowed route.
    allowed = json.dumps(execution_evidence_for_model(local_only, route_trust="customer_local"))
    assert "customer rows" in allowed and "invoice 42" not in allowed
    assert json.loads(allowed)["verification"] == "managed sandbox · image sha256:abc · network denied"
