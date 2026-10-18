"""THE packaging audit. The test an enterprise security reviewer actually runs.

Every claim the README makes about this package is asserted here, against the
installed code, two ways:

**Structurally.** An AST and import scan over the shipped modules: what the
package imports, what it calls, what it never mentions. This catches a
capability that exists but was not exercised -- a listening socket in a code
path no test happened to take is still a listening socket.

**At runtime.** The client is driven through a full session -- handshake,
authentication, ``views/open``, manifest chunks, blob upload, a server tool
call, an edit, a push, a re-read, a re-index, close -- inside a subprocess with
``sys.addaudithook`` installed, and the recorded transcript of sockets,
subprocesses and file writes is what the assertions read. Not "the code looks
like it does not bind a port" but "the process bound no port".

The scenario runs in a subprocess on purpose: an audit hook cannot be removed
and sees the whole interpreter, so pytest and the stub server sharing the
process would contaminate the transcript.

Run it alone:

    .venv/bin/python -m pytest tests/test_packaging_audit.py -v
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from _audit_source import PACKAGE_ROOT, code_only, iter_modules
from _stub import TOKEN, StubServer

PROJECT_ROOT = PACKAGE_ROOT.parents[1]
PROBE = Path(__file__).with_name("_audit_probe.py")

#: The one program the client runs on the session path, and why: read-only
#: ``git`` queries for repository identity plus working-tree dirtiness. Repository
#: identity has to come from somewhere, and the alternative -- parsing ``.git``
#: by hand -- would be less correct, not more auditable.
SESSION_PATH_PROGRAMS = {"git"}
_SESSION_GIT_STATUS = (
    "git",
    "status",
    "--porcelain=v2",
    "--branch",
    "-z",
    "--untracked-files=all",
    "--ignore-submodules=dirty",
    "--",
    ".",
    ":(exclude).claude/worktrees",
    ":(exclude).lc-worktrees",
    ":(exclude).lemoncrow",
)


# --------------------------------------------------------------------------- #
# 1. The dependency set                                                       #
# --------------------------------------------------------------------------- #


def _pyproject() -> Mapping[str, Any]:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_the_declared_dependency_set_is_empty() -> None:
    """The audit surface is one package with no transitive tree."""
    assert _pyproject()["project"]["dependencies"] == []


def test_the_installed_distribution_requires_nothing_at_runtime() -> None:
    from importlib import metadata

    try:
        requires = metadata.requires("lemoncrow-client") or []
    except metadata.PackageNotFoundError:  # pragma: no cover - source checkout
        pytest.skip("lemoncrow-client is not installed in this interpreter")
    runtime = [entry for entry in requires if "extra ==" not in entry]
    assert runtime == [], f"the installed wheel requires {runtime}"


def test_no_module_imports_anything_outside_the_standard_library() -> None:
    """Statically, across every shipped module."""
    own = {"lemoncrow_client", ""}
    for path in iter_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # a relative import is within this package
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                # The opt-in Review Companion verifies control-plane Ed25519 leases.
                # Its cryptography imports are lazy (inside validate_lease), guarded
                # by ImportError, and absent from normal zero-dependency lcr usage.
                optional_review_verifier = path.name == "review_worker_client.py" and name == "cryptography"
                assert (
                    name in sys.stdlib_module_names or name in own or optional_review_verifier
                ), f"{path.name} imports third-party module {name!r}"


def test_optional_review_worker_crypto_is_lazy_and_fail_closed() -> None:
    path = PACKAGE_ROOT / "review_worker_client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Review-only crypto must not become an import-time dependency. If this
    # exception ever spreads to normal CLI modules, the static audit refuses it.
    for item in tree.body:
        assert not (
            isinstance(item, ast.ImportFrom) and (item.module or "").startswith("cryptography")
        ), "Ed25519 verification must not import a dependency at module import time"
    validator = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "validate_lease")
    guarded = [
        item
        for item in ast.walk(validator)
        if isinstance(item, ast.Try)
        and any(isinstance(handler.type, ast.Name) and handler.type.id == "ImportError" for handler in item.handlers)
    ]
    assert len(guarded) == 1
    assert any(
        isinstance(item, ast.ImportFrom) and (item.module or "").startswith("cryptography")
        for item in ast.walk(guarded[0])
    )


def test_importing_the_package_pulls_in_nothing_third_party() -> None:
    """And dynamically: whatever an import actually drags in."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, json;"
                "import lemoncrow_client.cli, lemoncrow_client.mcpserver;"
                "import lemoncrow_client.dispatcher, lemoncrow_client.localtools;"
                "from lemoncrow_client.localtools import executor_for;"
                "[executor_for(name) for name in ('bash','edit','grep','sql','index')];"
                "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
            ),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout.strip().splitlines()[-1])
    foreign = [
        name
        for name in loaded
        if name not in sys.stdlib_module_names and name != "lemoncrow_client" and not name.startswith("_")
    ]
    assert foreign == [], f"importing the client loaded {foreign}"


# --------------------------------------------------------------------------- #
# 2. No network socket, no port, no daemon -- structurally                    #
# --------------------------------------------------------------------------- #


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module)
    return names


#: The one module allowed a socket and ``threading.Thread``: background sync's owner-only
#: Unix datagram nudge. Everything it may do is pinned by the tests below.
AUTOSYNC = PACKAGE_ROOT / "autosync.py"

#: The opt-in remote MCP gateway (``runtime mcp serve``): the one subpackage
#: allowed a loopback listener, its tunnel and its service units. Nothing on the
#: stdio session path imports it; ``test_remote_mcp_is_an_explicit_opt_in``
#: pins how it is reached and what it may bind.
REMOTE = PACKAGE_ROOT / "remote"
REMOTE_GATEWAY = REMOTE / "gateway.py"
REMOTE_TUNNEL = REMOTE / "tunnel.py"
LITE_MCP = PACKAGE_ROOT / "lite" / "mcp.py"
#: The opt-in persistent mode's user units and the cloudflared they supervise.
REMOTE_SUPERVISION = frozenset({REMOTE / "cli.py", REMOTE / "service.py", REMOTE_TUNNEL})
SUPERVISION_NEEDLES = frozenset(
    {"systemd", "LaunchAgents", "launchctl", "loginctl", "enable-linger", "Restart=always", "cloudflared"}
)


def _in_remote(path: Path) -> bool:
    return path.is_relative_to(REMOTE)


@pytest.fixture
def state_dir() -> Iterator[Path]:
    """Short on purpose: background sync binds a Unix socket under the state
    directory, and those paths are capped near 104 bytes. pytest's ``tmp_path``
    alone can exceed that, which would silently keep the audit from seeing it."""
    target = Path(tempfile.mkdtemp(prefix="lca", dir="/tmp"))
    yield target
    shutil.rmtree(target, ignore_errors=True)


def test_the_package_never_touches_a_socket_api() -> None:
    """Socket APIs stay isolated; only transport may dial outbound WebSockets."""
    forbidden = {
        "socket",
        "socketserver",
        "http.server",
        "asyncio",
        "selectors",
        "select",
        "multiprocessing",
        "wsgiref",
        "xmlrpc.server",
    }
    for path in iter_modules():
        overlap = _imported_names(path) & forbidden
        if path == AUTOSYNC:
            overlap -= {"socket"}  # a Unix datagram nudge only: see the next test
        if path == PACKAGE_ROOT / "webtransport.py":
            overlap -= {"socket"}  # Public-page DNS validation and pinned outbound connections only.
        if path == PACKAGE_ROOT / "transport.py":
            overlap -= {"select", "socket"}  # The sole egress module has a scoped outbound WSS dial.
        if path == REMOTE_GATEWAY:
            overlap -= {"http.server"}  # The opt-in loopback remote MCP listener only.
        assert not overlap, f"{path.name} imports {sorted(overlap)}"


def test_websocket_dial_is_outbound_only_and_limited_to_the_existing_network_module() -> None:
    """Audit the *exact* dial rather than broadly exempting socket calls."""
    transport = code_only(PACKAGE_ROOT / "transport.py")
    assert transport.count("socket.create_connection(") == 1
    assert "socket.create_connection((host, port), timeout=timeout_s)" in transport
    for forbidden in (
        "socket.socket(",
        "socket.socketpair(",
        "socket.fromfd(",
        "socket.create_server(",
        ".bind(",
        ".listen(",
        ".accept(",
        ".connect_ex(",
    ):
        assert forbidden not in transport, f"Review thin-client transport cannot {forbidden}"
    # No other module may open outbound WebSockets or acquire their bearer.
    for path in iter_modules():
        if path.name in {"transport.py", "review_companion_cli.py"}:
            continue
        assert "websocket_hints(" not in code_only(path), f"{path.name} has an unsanctioned WebSocket call"


def test_remote_mcp_is_an_explicit_opt_in() -> None:
    """Only ``cli.py`` reaches ``remote/``, lazily, and the listener is loopback-only."""
    for path in iter_modules():
        if _in_remote(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                reaches = (node.level and (module == "remote" or module.startswith("remote."))) or (
                    module.startswith("lemoncrow_client.remote")
                )
            elif isinstance(node, ast.Import):
                reaches = any(alias.name.startswith("lemoncrow_client.remote") for alias in node.names)
            else:
                continue
            if not reaches:
                continue
            assert path.name == "cli.py", f"{path.name} imports the remote MCP gateway"
            assert node not in tree.body, "cli.py must import the remote gateway lazily, inside its command"
    for path in REMOTE.rglob("*.py"):
        body = code_only(path)
        assert "0.0.0.0" not in body and '"::"' not in body, f"{path.name} binds a non-loopback address"
    gateway = code_only(REMOTE_GATEWAY)
    assert 'host: str = "127.0.0.1"' in gateway, "the gateway must default to loopback"


def test_the_one_socket_is_an_owner_only_unix_datagram_nudge() -> None:
    """Never a network socket, never a listening stream, never a connection."""
    body = code_only(AUTOSYNC)
    for required in ("socket.AF_UNIX", "socket.SOCK_DGRAM", "0o600", "0o700"):
        assert required in body, f"autosync.py no longer uses {required!r}"
    for forbidden in (
        "AF_INET",
        "SOCK_STREAM",
        ".listen(",
        ".accept(",
        ".connect(",
        "getaddrinfo",
        "create_connection",
        "create_server",
    ):
        assert forbidden not in body, f"autosync.py uses {forbidden!r}"


def test_only_explicit_transport_modules_reach_the_network() -> None:
    """Product RPC, public pages and explicit pinned Pi installs own their transports."""
    network = {"urllib.request", "urllib.error", "http.client", "ftplib", "smtplib", "telnetlib"}
    reaching = {path.name for path in iter_modules() if _imported_names(path) & network}
    assert reaching == {"transport.py", "webtransport.py", "pi_host.py"}


@pytest.mark.parametrize("module", ["transport.py", "webtransport.py"])
def test_the_transport_module_never_writes_a_file(module: str) -> None:
    """Nothing that arrives over the network can become a file on disk.

    That is the structural form of "downloads no binary or model": the only
    module that can receive bytes from the network has no way to persist them.
    """
    source = (PACKAGE_ROOT / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            assert name != "open", "transport.py calls open()"
            assert not name.startswith("os.open"), "transport.py calls os.open()"
            assert "write_bytes" not in name and "write_text" not in name
    assert "urlretrieve" not in source


def test_the_package_installs_no_unit_and_writes_no_host_configuration() -> None:
    """The daemon disposition, as a scan of executable code."""
    forbidden = (
        "systemd",
        "LaunchAgents",
        "launchctl",
        "loginctl",
        "enable-linger",
        "Restart=always",
        "crontab",
        "cloudflared",
        ".claude/settings.json",
        "ANTHROPIC_BASE_URL",
        "posthog",
        "/etc/init.d",
    )
    for path in iter_modules():
        body = code_only(path)
        for needle in forbidden:
            if path in REMOTE_SUPERVISION and needle in SUPERVISION_NEEDLES:
                continue  # ``runtime mcp serve --persistent`` only; pinned below.
            if path.name in {"review_service.py", "review_launchd.py"} and needle in {
                "systemd",
                "LaunchAgents",
                "launchctl",
            }:
                continue  # Only explicit `lcr service` opt-in; tested for safe user-unit paths.
            assert needle not in body, f"{path.name} mentions {needle!r}"


def test_opt_in_review_service_never_claims_jobs_or_writes_privileged_units() -> None:
    linux = code_only(PACKAGE_ROOT / "review_service.py")
    mac = code_only(PACKAGE_ROOT / "review_launchd.py")
    for body in (linux, mac):
        assert "sudo " not in body and "/etc/systemd" not in body
        assert "LaunchDaemons" not in body and "--system" not in body
        assert "claim(" not in body and "api_key" not in body
    assert '"systemd" / "user"' in linux
    assert '"Library" / "LaunchAgents"' in mac
    assert "daemon=True" in linux and "monitor.join(" in linux
    assert "return manage_launchd(" in linux
    assert '"enable", "--now"' in linux
    assert "read_only" not in linux  # Capture remains Git-backed, not an executor.


def test_remote_supervision_writes_only_user_units() -> None:
    """The persistent remote MCP units live in the user's own unit directories."""
    body = code_only(REMOTE / "service.py")
    assert '".config" / "systemd" / "user"' in body and '"Library" / "LaunchAgents"' in body
    for forbidden in ("/etc/", "/Library/LaunchDaemons", "sudo ", "--system"):
        assert forbidden not in body.replace("sudo loginctl enable-linger", ""), forbidden
    assert '"-m", "lemoncrow_client"' in body, "units must run this client, not a server package"


def test_the_package_ships_no_auxiliary_session_or_mcp_process_implementation() -> None:
    """The forbidden behavior is absent from the wheel, not merely unrouted."""
    assert not (PACKAGE_ROOT / "hook.py").exists()
    assert not (PACKAGE_ROOT / "localtools" / "proxy.py").exists()
    assert not (PACKAGE_ROOT / "localtools" / "delegate.py").exists()


def test_the_package_has_no_background_or_persistence_primitives() -> None:
    """No atexit, fork, signal handler or detached session anywhere; threads are
    started only in ``autosync.py``, which joins them on close, and by worker
    pools their owners shut down (the runtime audit checks that none survives
    the session)."""
    forbidden_calls = (
        "atexit.register",
        "threading.Thread",
        "threading.Timer",
        "os.fork",
        "os.forkpty",
        "os.setsid",
        "signal.signal",
        "signal.alarm",
        "daemon=True",
        "start_new_session",
    )
    for path in iter_modules():
        body = code_only(path)
        for needle in forbidden_calls:
            if path == AUTOSYNC and needle in ("threading.Thread", "daemon=True"):
                continue
            if path == REMOTE_GATEWAY and needle in ("threading.Thread", "daemon=True"):
                continue  # The opt-in listener's accept loop and per-request handler threads.
            if path == REMOTE_TUNNEL and needle in ("threading.Thread", "daemon=True"):
                continue  # Drains a quick tunnel's stderr for the life of that child process.
            if path == LITE_MCP and needle in ("threading.Thread", "daemon=True"):
                continue  # One index build per stdio session, off the initialize path; stopped and joined on close.
            if path.name == "review_service.py" and needle in {"threading.Thread", "daemon=True"}:
                assert "stop.set()" in body and "monitor.join(" in body
                continue  # Opt-in foreground Review observer; no OS process or listener spawned.
            assert needle not in body, f"{path.name} uses {needle!r}"
    assert ".join(" in code_only(AUTOSYNC), "autosync.py must join the threads it starts"
    assert "_prepare_stop.set()" in code_only(LITE_MCP), "lite/mcp.py must stop its index build on close"
    assert "thread.join(" in code_only(LITE_MCP), "lite/mcp.py must join its index build on close"
    for path in iter_modules():
        body = code_only(path)
        if "ThreadPoolExecutor(" in body:
            assert ".shutdown(" in body, f"{path.name} must shut down the worker pools it starts"


def test_the_package_has_no_self_update_path() -> None:
    forbidden = ("pip", "ensurepip", "venv", "setuptools", "importlib.reload", "git pull", "install.sh")
    for path in iter_modules():
        body = code_only(path)
        if path.name == "cli.py":
            # ``cli.py`` reads ``importlib.metadata`` to *report* the dependency
            # set. Reading metadata is not installing anything.
            body = body.replace("importlib", "")
            # The one sanctioned install path: ``_run_release_installer`` runs the
            # published ``install.sh`` for `lc update` and the one-time fetch of a
            # missing product (LEMONCROW_NO_LAZY_INSTALL=1 turns the fetch off).
            # ``_INSTALL_URL`` is only printed as a hint.
            if path.parent == PACKAGE_ROOT:
                assert _install_sh_owners(path) == {"_run_release_installer", "_INSTALL_URL"}
                body = body.replace("install.sh", "")
                # Bash pipefail propagates a failed download; it does not invoke pip.
                body = body.replace('"pipefail"', "")
        if path.name == "edit.py" and path.parent.name == "kit":
            # ``kit/edit.py`` names ``.venv`` only as a directory an edit must
            # never write into (``PROTECTED_PARTS``). It neither creates nor runs
            # a virtual environment.
            body = body.replace('".venv"', "")
        if path.name == "bash_output.py" and path.parent.name == "kit":
            # ``kit/bash_output.py`` RECOGNIZES commands the agent already ran --
            # ``uv pip install`` to collapse its successful output, ``pipenv`` as
            # a pytest launcher -- in order to trim their output. It never runs
            # an installer itself.
            body = body.replace(r'r"|(?:uv|pip3?|pipx|poetry)\s+(?:pip\s+)?(?:install|sync|add|update)"', "")
            body = body.replace('"pipenv", ', "")
        if path.name == "command_policy.py" and path.parent.name == "kit":
            # ``kit/command_policy.py`` RECOGNIZES ``curl ... | pip`` and
            # ``&& pip`` installs so it never rewrites them into a plain fetch,
            # and calls its ``od | tail`` rewrite a "pipeline". It never runs
            # an installer itself.
            body = body.replace("pipeline", "").replace("pip[0-9]*|", "").replace("|unzip|pip|", "|unzip|")
        if path.name == "review_agent_snapshot.py" and path.parent == PACKAGE_ROOT:
            # Review source filtering denies local virtualenv source trees;
            # it never installs, creates or updates an environment.
            body = body.replace('".venv"', "")
        if path.name == "search.py" and path.parent.name == "kit":
            # ``kit/search.py`` names ``.venv``/``venv`` only as directories a
            # search skips (``SKIP_DIRS``). It neither creates nor runs one.
            body = body.replace('".venv"', "").replace('"venv"', "")
        if path.name == "security_scan.py" and path.parent.name == "kit":
            # ``kit/security_scan.py`` names ``.venv``/``venv`` only as directories
            # its taint walk skips (``_SKIP_DIR_NAMES``). It neither creates nor
            # runs one.
            body = body.replace('".venv"', "").replace('"venv"', "")
        if path.name == "shell.py" and path.parent.name == "localtools":
            # ``"pipeline_seek"`` is the name of the ``od | tail`` rewrite target.
            body = body.replace('"pipeline_seek"', "")
        for needle in forbidden:
            assert needle not in body, f"{path.name} mentions {needle!r}"


def _install_sh_owners(path: Path) -> set[str]:
    """Top-level function or assignment names whose code mentions ``install.sh``."""
    owners: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                node.body = node.body[1:] or [ast.Pass()]  # a docstring is not code
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            name = ast.unparse(target)
        else:
            name = type(node).__name__
        if "install.sh" in ast.unparse(node):
            owners.add(name)
    return owners


def test_the_installer_runs_only_on_request_or_for_a_missing_product() -> None:
    """The lazy fetch is opt-out, and nothing else calls the installer runner."""
    source = (PACKAGE_ROOT / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name != "_run_release_installer"
        and any(
            isinstance(call, ast.Call) and ast.unparse(call.func) == "_run_release_installer" for call in ast.walk(node)
        )
    }
    assert callers == {"_lazy_install_product", "_update"}
    lazy = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_lazy_install_product")
    assert "LEMONCROW_NO_LAZY_INSTALL" in ast.unparse(lazy.body[1])


def test_persistent_state_writers_are_confined_to_the_worktree_or_state_directory() -> None:
    """Keep the client file-writer allowlist explicit and closed.

    ``kit/fsio.py`` performs every worktree write of the shared kit (atomic
    edit writes and edit rollback), which the ``edit`` executor calls.
    ``workspace_paths.py`` replaces only the repository-local same-host proof
    using an exclusive create; a symlink can never redirect that write.
    ``cli.py``, ``credentials.py`` and ``session.py`` write bounded client
    state; ``config.py`` writes a repo-identity marker under the worktree's own
    ``.git`` directory, only for an unborn repository and never once it has a
    commit; ``liveview.py`` writes only a rebuildable, owner-only binding under
    ``<state_dir>/live-views`` (no source bytes or credentials); ``review.py``
    freezes an exact user-invoked Review snapshot under that same state directory;
    ``review_device_connect.py`` atomically stores one user-approved, repository-scoped
    Review machine credential and Ed25519 verification-key pin under an owner-only
    Review devices directory; it never writes Authward login or Runtime state;
    ``review_model_attempts.py`` writes one O_EXCL owner-only spend marker for
    an explicitly approved, signed model attempt; no secrets or source bytes;
    ``review_delta.py`` atomically writes only metadata (Git object IDs, SHA-256
    digests and sizes, never file bytes or credentials) to owner-only
    ``<review_state_dir>/delta-cursors`` after a successful hosted capture;
    ``shell.py`` persists the bounded overflow body for a capped local-shell result;
    ``trace.py`` appends the bounded, owner-only per-call runtime trace under
    ``<state_dir>/traces`` (14 days / 256 MB, bulk content never stored);
    ``remote/state.py`` writes the opt-in remote MCP gateway's connector bindings,
    OAuth token hashes, pairing code and tunnel reference, owner-only, under
    ``<state_dir>/mcp``; ``remote/service.py`` writes that gateway's two user
    units, only for ``runtime mcp serve --persistent``. ``pi_host.py`` owns the
    explicitly requested pinned Pi bundle and metadata under ``<state_dir>/hosts``.
    Every other module must remain pure unless its persistence contract is
    reviewed here.
    """
    writers: list[str] = []
    for path in iter_modules():
        body = code_only(path)
        if any(needle in body for needle in ("write_text(", "os.fdopen(", "write_bytes(")):
            writers.append(path.relative_to(PACKAGE_ROOT).as_posix())
    assert sorted(writers) == [
        "cli.py",
        "config.py",
        "credentials.py",
        "kit/fsio.py",
        "liveview.py",
        "localtools/shell.py",
        "pi_host.py",
        "remote/service.py",
        "remote/state.py",
        "review.py",
        "review_delta.py",
        "review_device_connect.py",
        "review_launchd.py",
        "review_model_attempts.py",
        "review_service.py",
        "session.py",
        "trace.py",
        "workspace_paths.py",
    ], sorted(writers)


def test_review_snapshots_are_confined_to_the_review_state_directory() -> None:
    source = (PACKAGE_ROOT / "review.py").read_text(encoding="utf-8")
    assert 'TemporaryDirectory(prefix="capture-", dir=config.review_state_dir)' in source
    assert "destination = target / relative" in source
    assert "source = repo / relative" in source


# --------------------------------------------------------------------------- #
# 3. What the process actually did                                            #
# --------------------------------------------------------------------------- #


def _probe(scenario: str, *, url: str, repo_root: Path, state_dir: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(PROBE), scenario, str(repo_root)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=str(repo_root),
        env={
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "LEMONCROW_URL": url,
            "LEMONCROW_TOKEN": TOKEN,
            "LEMONCROW_HOME": str(state_dir),
            "LEMONCROW_REQUEST_TIMEOUT_S": "5",
            # Audit what ships: background sync on, whatever the test env says.
            "LEMONCROW_AUTOSYNC": "1",
        },
    )
    assert completed.returncode == 0, completed.stderr
    transcript = json.loads(completed.stdout.strip().splitlines()[-1])
    assert not transcript["failure"], transcript["failure"]
    return transcript


def _events(transcript: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    return [entry for entry in transcript["events"] if entry["event"] == name]


def _written_paths(transcript: Mapping[str, Any]) -> list[str]:
    """Every path the process opened for writing, from the ``open`` events."""
    written: list[str] = []
    for entry in transcript["events"]:
        if entry["event"] in {"os.mkdir", "os.rename", "os.remove"}:
            written.extend(str(item) for item in entry["args"] if isinstance(item, str))
            continue
        if entry["event"] != "open":
            continue
        path, mode, flags = (entry["args"] + [None, None, None])[:3]
        if not isinstance(path, str):
            continue
        writing = False
        if isinstance(mode, str) and any(letter in mode for letter in "wax+"):
            writing = True
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writing = True
        if writing:
            written.append(path)
    return written


@pytest.fixture
def transcript(stub: StubServer, worktree: Path, state_dir: Path) -> dict[str, Any]:
    return _probe("session", url=stub.url, repo_root=worktree, state_dir=state_dir)


def test_a_full_mcp_session_completes_in_one_process(transcript: dict[str, Any]) -> None:
    """The scenario has to succeed, or the rest of this file proves nothing."""
    assert transcript["result"]["bootstrapped"] is True, transcript["result"]["reason"]
    assert "LemonCrow:" in transcript["result"]["instructions"]
    assert transcript["result"]["errors"] == [False, False, False, False, False]


def test_the_client_binds_no_network_port(transcript: dict[str, Any], state_dir: Path) -> None:
    """The one bind is background sync's Unix socket, under the state directory."""
    binds = _events(transcript, "socket.bind")
    assert len(binds) == 1, binds
    address = binds[0]["args"][1]
    assert isinstance(address, str) and address.endswith(".sock"), f"bound {address!r}"
    assert Path(address).resolve().is_relative_to((state_dir / "runtime" / "sync").resolve()), f"bound {address}"


def test_every_datagram_goes_to_the_clients_own_sync_socket(transcript: dict[str, Any], state_dir: Path) -> None:
    for entry in _events(transcript, "socket.sendto"):
        target = entry["args"][1]
        assert isinstance(target, str), f"sent to {target!r}"
        assert Path(target).resolve().is_relative_to((state_dir / "runtime" / "sync").resolve()), f"sent to {target}"


def test_every_connection_goes_to_the_configured_endpoint_and_nowhere_else(
    transcript: dict[str, Any], stub: StubServer
) -> None:
    from urllib.parse import urlsplit

    endpoint = urlsplit(stub.url)
    expected = [endpoint.hostname, endpoint.port]

    connects = _events(transcript, "socket.connect")
    assert connects, "the scenario never connected to anything"
    for entry in connects:
        address = entry["args"][1]
        assert list(address)[:2] == expected, f"connected to {address}"

    for entry in _events(transcript, "socket.getaddrinfo"):
        assert entry["args"][0] in {endpoint.hostname, None}, f"resolved {entry['args'][0]}"

    for entry in _events(transcript, "urllib.Request"):
        assert str(entry["args"][0]).startswith(stub.url), f"requested {entry['args'][0]}"

    # ``urllib`` dials through ``http.client``, so these are the same
    # connections seen one layer up -- checked, not forbidden.
    for entry in _events(transcript, "http.client.connect"):
        assert entry["args"][1:3] == expected, f"http.client connected to {entry['args'][1:3]}"

    for absent in ("ftplib.connect", "webbrowser.open", "socket.gethostbyname"):
        assert _events(transcript, absent) == []


def test_the_only_program_the_session_path_runs_is_a_read_only_git_query(
    transcript: dict[str, Any], worktree: Path
) -> None:
    spawns = _events(transcript, "subprocess.Popen")
    programs = {Path(str(entry["args"][0])).name for entry in spawns}
    assert programs <= SESSION_PATH_PROGRAMS, f"the session path ran {sorted(programs)}"
    for entry in spawns:
        argv = entry["args"][1]
        assert isinstance(argv, list)
        command_index = 1
        if len(argv) >= 4 and argv[1] == "-C":
            assert Path(argv[2]).resolve() == worktree.resolve(), f"git escaped the audited worktree: {argv}"
            command_index = 3
        if argv[command_index : command_index + 2] == ["-c", "status.renames=false"]:
            assert argv[command_index + 2 :] == [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ]
            env = entry["args"][3]
            assert isinstance(env, dict) and "GIT_OPTIONAL_LOCKS" in env
            continue
        command = argv[command_index] if len(argv) > command_index else ""
        if command == "status":
            assert tuple([argv[0], *argv[command_index:]]) == _SESSION_GIT_STATUS, f"git status was run as {argv}"
            env = entry["args"][3] if len(entry["args"]) > 3 else None
            assert (
                isinstance(env, dict) and "GIT_OPTIONAL_LOCKS" in env
            ), "git status must disable optional index-refresh writes"
        elif command == "worktree":
            assert tuple([argv[0], *argv[command_index:]]) == (
                "git",
                "worktree",
                "list",
                "--porcelain",
            ), f"git worktree discovery was run as {argv}"
        else:
            if command == "ls-tree":
                assert argv[command_index + 1 : command_index + 3] == ["-rlz", "--full-tree"]
                assert len(argv) == command_index + 4
            elif command == "cat-file":
                assert argv[command_index + 1 :] == ["--batch"]
            else:
                assert command in {"rev-parse", "rev-list"}, f"git was run as {argv}"


def test_the_client_never_forks_or_spawns_outside_subprocess(transcript: dict[str, Any]) -> None:
    for event in ("os.fork", "os.forkpty", "os.posix_spawn"):
        assert _events(transcript, event) == [], f"the client raised {event}"


def test_nothing_is_written_outside_the_worktree_and_the_state_directory(
    transcript: dict[str, Any], worktree: Path, state_dir: Path
) -> None:
    allowed = (str(worktree.resolve()), str(state_dir.resolve()))
    escapes = [
        path
        for path in _written_paths(transcript)
        if os.path.isabs(path)
        and Path(path).resolve() != Path(os.devnull).resolve()
        and not str(Path(path).resolve()).startswith(allowed)
    ]
    assert escapes == [], f"wrote outside the permitted roots: {escapes}"


def test_nothing_is_written_into_a_host_configuration_directory(
    transcript: dict[str, Any],
) -> None:
    forbidden = (
        ".config/systemd",
        "LaunchAgents",
        ".claude/settings.json",
        ".cloudflared",
        "/etc/",
        "site-packages",
    )
    for path in _written_paths(transcript):
        for needle in forbidden:
            assert needle not in path, f"wrote {path}"


def test_no_thread_and_no_child_process_survives_the_session(
    transcript: dict[str, Any],
) -> None:
    assert transcript["threads_after"] == transcript["threads_before"] == 1
    assert transcript["thread_names"] == ["MainThread"]
    assert transcript["surviving_children"] == "none", transcript["surviving_children"]


def test_no_shared_library_or_downloaded_artifact_is_loaded(transcript: dict[str, Any]) -> None:
    assert _events(transcript, "ctypes.dlopen") == []


# --------------------------------------------------------------------------- #
# 4. The scenarios that are *supposed* to do something                        #
# --------------------------------------------------------------------------- #


def test_a_user_invoked_bash_spawns_exactly_one_extra_program_and_reaps_it(
    stub: StubServer, worktree: Path, state_dir: Path
) -> None:
    """The contrast case: user-initiated execution is visible, bounded, gone."""
    transcript = _probe("bash", url=stub.url, repo_root=worktree, state_dir=state_dir)
    assert "audited" in transcript["result"]["text"]
    programs = [Path(str(entry["args"][0])).name for entry in _events(transcript, "subprocess.Popen")]
    shells = [name for name in programs if name not in SESSION_PATH_PROGRAMS]
    assert len(shells) == 1, f"expected one shell, got {programs}"
    assert transcript["surviving_children"] == "none"
    assert transcript["threads_after"] == 1


def test_with_no_server_the_client_opens_no_connection_it_was_not_configured_for(
    worktree: Path, state_dir: Path
) -> None:
    """Failing to reach the endpoint must not become reaching for another one."""
    import socket as socket_module

    probe = socket_module.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    transcript = _probe("offline", url=f"http://127.0.0.1:{port}", repo_root=worktree, state_dir=state_dir)
    assert transcript["result"]["bootstrapped"] is False
    assert "client-side tools still work" in transcript["result"]["line"]
    # Ordinary read and grep stay client-local; code_search still refuses.
    assert transcript["result"]["degraded"] == [False, False, False]
    assert transcript["result"]["errors"] == [False, False, True]

    for entry in _events(transcript, "socket.connect"):
        assert list(entry["args"][1])[:2] == ["127.0.0.1", port]
    assert _events(transcript, "socket.bind") == []
    assert transcript["surviving_children"] == "none"


def test_an_offline_session_writes_nothing_at_all(worktree: Path, state_dir: Path) -> None:
    """No local index, and no cache standing in for one."""
    import socket as socket_module

    probe = socket_module.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    transcript = _probe("offline", url=f"http://127.0.0.1:{port}", repo_root=worktree, state_dir=state_dir)
    runtime = state_dir / "runtime"
    traces = runtime / "traces"
    created = [
        path
        for path in _written_paths(transcript)
        if os.path.isabs(path)
        and Path(path) != Path(os.devnull)
        and Path(path) != runtime
        and not Path(path).is_relative_to(traces)
    ]
    # The only write is the per-call runtime trace (bounded, no content).
    assert created == [], f"an offline session wrote {created}"
    assert [p.name for p in state_dir.iterdir()] in ([], ["runtime"])
