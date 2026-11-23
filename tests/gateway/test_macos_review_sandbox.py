from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.review import infra_render, runner_host, tool_server, web_preview
from lemoncrow.pro.capabilities.review.analysis_common import decode_report, driver_command
from lemoncrow.pro.capabilities.review.surfaces import RuntimeConfigEntry
from lemoncrow.pro.environment import host_tools, macos_sandbox
from lemoncrow.pro.environment import runners as runtimes
from lemoncrow.pro.environment.macos_sandbox import sandbox_command
from lemoncrow.pro.environment.sandbox_limits import run_limited


@pytest.mark.parametrize(
    "name,target",
    [
        ("docker", "/Applications/Docker.app/Contents/Resources/bin/docker"),
        ("npm", "/opt/homebrew/bin/npm"),
        ("docker-compose", "/Applications/Docker.app/Contents/Resources/cli-plugins/docker-compose"),
    ],
)
def test_macos_tools_without_shell_path(name: str, target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(host_tools.shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda self: str(self) == target)
    monkeypatch.setattr(os, "access", lambda path, mode: str(path) == target)
    assert host_tools.host_executable(name) == target
    if name == "npm":
        assert web_preview._sandbox_visible_executable(name)


requires_macos = pytest.mark.skipif(sys.platform != "darwin", reason="requires native macOS Seatbelt")


@requires_macos
def test_macos_npm_build_is_confined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not host_tools.host_executable("npm"):
        pytest.skip("npm is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    modules = tmp_path / "checkout" / "node_modules"
    modules.mkdir(parents=True)
    dependency = modules / "borrowed.txt"
    dependency.write_text("original")
    secret = tmp_path / "secret"
    secret.write_text("private")
    monkeypatch.setenv("REVIEW_TEST_SECRET", "must-not-reach-build")
    (workspace / "package.json").write_text(json.dumps({"scripts": {"build": "node build.cjs"}}))
    (workspace / "build.cjs").write_text(
        "const fs=require('fs');"
        "if(process.env.REVIEW_TEST_SECRET)throw Error('environment leaked');"
        f"for(const p of {json.dumps([str(dependency), str(secret)])}){{"
        "try{fs.writeFileSync(p,'changed');throw Error('write allowed')}catch(e){"
        "if(!['EPERM','EACCES'].includes(e.code))throw e}}"
        f"try{{fs.readFileSync({json.dumps(str(secret))});throw Error('read allowed')}}"
        "catch(e){if(!['EPERM','EACCES'].includes(e.code))throw e}"
        "fs.mkdirSync('dist');fs.writeFileSync('dist/index.html','preview');"
    )
    command = web_preview._macos_build_command(
        workspace,
        modules,
        repo_root=modules.parent,
        project_root=".",
        package_manager="npm",
        framework="vite",
    )
    proc = run_limited(command, timeout=30, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (workspace / "dist/index.html").read_text() == "preview"
    assert dependency.read_text() == "original"
    assert secret.read_text() == "private"


@requires_macos
def test_docker_compose_runner_with_cleared_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not host_tools.host_executable("docker-compose"):
        pytest.skip("Docker Compose is not installed")
    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
        runner_host=runner_host,
    )

    def materialize(*args: object, target: Path, **kwargs: object) -> str:
        target.mkdir(parents=True)
        (target / "compose.yaml").write_text("services:\n  web:\n    image: nginx:alpine\n")
        return "identity"

    monkeypatch.setattr(runner_host, "materialize_review_side", materialize)
    runtime = RuntimeConfigEntry("compose", "docker-compose", {"compose": ["compose.yaml"]})
    surface = SimpleNamespace(id="web", provider="service", kind="service", metadata={"service": "web"})
    result = runtimes.DockerComposeRunner().run(context, runtime, surface, side="new")
    assert result.status == "passed", result.output or result.summary


@requires_macos
def test_macos_vite_build_with_read_only_checkout_dependencies(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[2] / "frontend"
    modules = checkout / "node_modules"
    if not (modules / "vite").is_dir():
        pytest.skip("frontend Vite dependencies are not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}, "type": "module"}))
    (workspace / "index.html").write_text('<div id="app"></div><script type="module" src="/main.js"></script>')
    (workspace / "main.js").write_text("document.querySelector('#app').textContent='review';")
    (workspace / "vite.config.js").write_text("export default { build: { minify: false } };")
    command = web_preview._macos_build_command(
        workspace,
        modules,
        repo_root=checkout.parent,
        project_root=".",
        package_manager="npm",
        framework="vite",
    )
    result = run_limited(command, timeout=30, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / "dist/index.html").is_file()


@requires_macos
@pytest.mark.parametrize("bind", ["127.0.0.1", "0.0.0.0"])
def test_macos_http_supervisor_and_network_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (workspace / "app.py").write_text(
        "import http.server,os\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200);self.end_headers();self.wfile.write(b'{\"ok\":true}')\n"
        f"http.server.HTTPServer(({bind!r},int(os.environ['PORT'])),Handler).serve_forever()\n"
    )
    config = {
        "command": [sys.executable, "app.py"],
        "port": port,
        "port_env": "PORT",
        "health": "/health",
        "health_timeout": 5,
        "request_timeout": 2,
        "max_body_bytes": 256000,
        "method": "GET",
        "path": "/hello",
    }
    kwargs = dict(
        workspace=workspace,
        cwd=workspace,
        executables=(sys.executable,),
        read_roots=(Path(sys.base_prefix),),
        loopback_port=port,
    )
    command = sandbox_command(
        [sys.executable, "-c", runtimes._HTTP_SERVICE_SUPERVISOR, json.dumps(config)],
        **kwargs,
    )
    proc = run_limited(command, timeout=15, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["body"] == {"ok": True}
    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
        runner_host=runner_host,
    )

    def materialize(*args: object, target: Path, **kwargs: object) -> str:
        target.mkdir(parents=True)
        (target / "app.py").write_text((workspace / "app.py").read_text())
        return "identity"

    monkeypatch.setattr(runner_host, "materialize_review_side", materialize)
    runtime = RuntimeConfigEntry("api", "http-service", {"command": [sys.executable, "app.py"], "port": port})
    surface = SimpleNamespace(id="hello", provider="bruno", kind="api.request", metadata={"url": "/hello"})
    result = runtimes.HttpServiceRunner().run(context, runtime, surface, side="new")
    assert result.status == "passed", result.output or result.summary
    assert result.data["body"] == {"ok": True}
    probe = (
        "import pathlib,socket\n"
        "try:\n pathlib.Path('app.py').write_text('changed')\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('snapshot write allowed')\n"
        "s=socket.socket()\n"
        "try:\n s.connect(('127.0.0.1',1))\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('other loopback port allowed')\n"
    )
    result = subprocess.run(
        sandbox_command([sys.executable, "-c", probe], **kwargs), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


@requires_macos
@pytest.mark.parametrize("side", ["old", "new"])
def test_macos_command_runner_confines_reviewed_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    secret = tmp_path / "secret"
    secret.write_text("private")
    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        execution=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
        runner_host=runner_host,
    )

    def materialize(*args: object, target: Path, **kwargs: object) -> str:
        target.mkdir(parents=True)
        return "identity"

    monkeypatch.setattr(runner_host, "materialize_review_side", materialize)
    monkeypatch.setenv("REVIEW_TEST_SECRET", "private")
    script = (
        "import pathlib,os,socket\n"
        "assert 'REVIEW_TEST_SECRET' not in os.environ\n"
        f"try:\n pathlib.Path({str(secret)!r}).read_text()\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('host read allowed')\n"
        "s=socket.socket()\n"
        "try:\n s.connect(('127.0.0.1',1))\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('network allowed')\n"
        "pathlib.Path('result.txt').write_text('passed')\n"
        "print('check passed')\n"
    )
    runtime = RuntimeConfigEntry("checks", "command", {"command": ["python3", "-c", script]})
    surface = SimpleNamespace(id="checks", provider="unit-tests")
    result = runtimes.CommandRunner().run(context, runtime, surface, side=side)
    assert result.status == "passed", result.output or result.summary
    assert "check passed" in result.output


@requires_macos
def test_macos_command_runner_collects_the_report_a_check_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recipe's ``{report}`` path is writable under Seatbelt; before, every report recipe failed here."""

    outside = tmp_path / "outside"
    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        execution=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
        runner_host=runner_host,
    )

    def materialize(*args: object, target: Path, **kwargs: object) -> str:
        target.mkdir(parents=True)
        return "identity"

    monkeypatch.setattr(runner_host, "materialize_review_side", materialize)
    script = (
        "import os,pathlib,sys\n"
        "target=pathlib.Path(sys.argv[1])\n"
        "assert str(target.parent)==os.environ['LEMONCROW_REPORT_DIR']\n"
        "target.write_text('<testsuite/>')\n"
        f"try:\n pathlib.Path({str(outside)!r}).write_text('x')\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('write outside the report directory allowed')\n"
    )
    runtime = RuntimeConfigEntry(
        "checks",
        "command",
        {"command": ["python3", "-c", script, "/tmp/lemoncrow-reports/report"], "report": True},
    )
    surface = SimpleNamespace(id="checks", provider="unit-tests")
    result = runtimes.CommandRunner().run(context, runtime, surface, side="new")
    assert result.status == "passed", result.output or result.summary
    assert result.data["report"] == "<testsuite/>"
    assert not outside.exists()
    recipe = result.data["execution"]["recipe"]
    assert recipe["command"][-1] == "/tmp/lemoncrow-reports/report"


@requires_macos
def test_macos_service_startup_includes_failure_log(tmp_path: Path) -> None:
    config = {
        "command": [sys.executable, "-c", "raise RuntimeError('startup detail')"],
        "port": 8765,
        "port_env": "PORT",
        "health": "/health",
        "health_timeout": 3,
    }
    command = sandbox_command(
        [sys.executable, "-c", runtimes._HTTP_SERVICE_SUPERVISOR, json.dumps(config)],
        workspace=tmp_path,
        cwd=tmp_path,
        executables=(sys.executable,),
        loopback_port=8765,
    )
    proc = run_limited(command, timeout=10, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "startup detail" in proc.stderr
    assert "service exited before health check" in proc.stderr


@requires_macos
def test_macos_pixel_capture_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        web_preview._chrome_executable()
    except web_preview.WebPreviewUnavailable:
        pytest.skip("Chrome Headless Shell is not installed")
    output = tmp_path / "workspace" / "dist"
    output.mkdir(parents=True)
    (output / "index.html").write_text("<style>body{background:rgb(12,34,56)}</style><h1>Frozen review</h1>")
    revision = SimpleNamespace(id="revision", review_id="review", base_sha="base", head_sha="head", mode="commit_range")
    descriptor = web_preview.WebPreviewDescriptor(framework="vite", routes=("/",), default_route="/")
    monkeypatch.setattr(web_preview, "infer_web_preview_descriptor", lambda *args, **kwargs: descriptor)
    monkeypatch.setattr(web_preview, "_ensure_built", lambda *args, **kwargs: output)
    monkeypatch.setattr(web_preview, "_build_cache_root", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(web_preview, "_prune_web_preview_cache", lambda *args, **kwargs: None)
    image = web_preview.render_web_preview(
        tmp_path,
        tmp_path,
        revision,
        path="src/main.js",
        route="/",
        side="old",
        width=640,
        height=480,
    )
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert image.stat().st_size > 1000
    assert (
        web_preview.render_web_preview(
            tmp_path,
            tmp_path,
            revision,
            path="src/main.js",
            route="/",
            side="old",
            width=640,
            height=480,
        )
        == image
    )


@requires_macos
def test_macos_concurrent_service_sides_use_distinct_ports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
        runner_host=runner_host,
    )

    def materialize(*args: object, target: Path, side: str, **kwargs: object) -> str:
        target.mkdir(parents=True)
        (target / "app.py").write_text(
            "import http.server,os,json,time\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  time.sleep(.1)\n"
            f"  body=json.dumps({{'side':{side!r},'port':int(os.environ['PORT'])}}).encode()\n"
            "  self.send_response(200);self.end_headers();self.wfile.write(body)\n"
            "http.server.ThreadingHTTPServer(('0.0.0.0',int(os.environ['PORT'])),Handler).serve_forever()\n"
        )
        return "identity"

    monkeypatch.setattr(runner_host, "materialize_review_side", materialize)
    # A live unrelated listener occupies the configured port throughout.
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        configured_port = occupied.getsockname()[1]
        runtime = RuntimeConfigEntry(
            "api",
            "http-service",
            {"command": ["python3", "app.py"], "port": configured_port},
        )
        jobs = [(surface, side) for surface in ("plans", "summary") for side in ("old", "new")]

        def run(job: tuple[str, str]) -> object:
            surface_id, side = job
            surface = SimpleNamespace(
                id=surface_id,
                provider="bruno",
                kind="api.request",
                metadata={"url": "/" + surface_id},
            )
            return runtimes.HttpServiceRunner().run(context, runtime, surface, side=side)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(run, jobs))
        for result, (_, side) in zip(results, jobs, strict=True):
            assert result.status == "passed", result.output or result.summary
            assert result.data["body"]["side"] == side
            assert result.data["body"]["port"] != configured_port
        assert len({result.data["body"]["port"] for result in results}) == 4
    assert not runtimes._MACOS_SERVICE_PORTS


def test_repo_named_executables_never_open_roots_under_the_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "home"
    docker = user_home / ".docker" / "bin" / "docker"
    npm = tmp_path / "opt" / "toolchain" / "bin" / "npm"
    for tool in (docker, npm):
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\n")
    (user_home / ".docker" / "config.json").write_text('{"auths": {}}')
    found = {"sandbox-exec": "/usr/bin/sandbox-exec", "docker": str(docker), "npm": str(npm)}
    monkeypatch.setattr("lemoncrow.pro.environment.macos_sandbox.host_executable", found.get)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def profile(repo_executable: str) -> str:
        argv = sandbox_command(
            [repo_executable], workspace=workspace, cwd=workspace, repo_executables=(repo_executable,)
        )
        return argv[argv.index("-p") + 1]

    assert str(user_home) not in profile("docker")
    assert str(npm.parent.parent) in profile("npm")
    # LemonCrow's own toolchain names keep their roots wherever they live.
    argv = sandbox_command(["docker"], workspace=workspace, cwd=workspace, executables=("docker",))
    assert str(user_home / ".docker") in argv[argv.index("-p") + 1]


@dataclass(frozen=True)
class _Homebrew:
    """A Homebrew-shaped tree: ``bin/python3`` -> Cellar keg ``bin/python3`` -> its Python.framework binary."""

    base: Path
    prefix: Path
    keg: Path
    framework: Path
    workspace: Path

    def argv(self, path_executables: tuple[str, ...] = ("python3",)) -> list[str]:
        return sandbox_command(
            ["python3", "-c", "pass"],
            workspace=self.workspace,
            cwd=self.workspace,
            writable_workspace=True,
            path_executables=path_executables,
        )


@pytest.fixture
def homebrew(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Homebrew:
    base = tmp_path.resolve()
    prefix = base / "brew"
    keg = prefix / "Cellar" / "python@3.14" / "3.14.6"
    framework = keg / "Frameworks" / "Python.framework" / "Versions" / "3.14"
    real = framework / "bin" / "python3.14"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    (keg / "bin").mkdir()
    (keg / "bin" / "python3").symlink_to("../Frameworks/Python.framework/Versions/3.14/bin/python3.14")
    (prefix / "bin").mkdir()
    (prefix / "bin" / "python3").symlink_to("../Cellar/python@3.14/3.14.6/bin/python3")
    (base / "usr" / "bin").mkdir(parents=True)
    workspace = base / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(macos_sandbox, "_HOMEBREW_PREFIXES", (prefix,))
    monkeypatch.setattr(macos_sandbox, "_SANDBOX_PATH", (str(prefix / "bin"), str(base / "usr" / "bin")))
    monkeypatch.setattr(macos_sandbox, "host_executable", {"sandbox-exec": "/usr/bin/sandbox-exec"}.get)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: base / "home"))
    return _Homebrew(base, prefix, keg, framework, workspace)


def _grants(argv: list[str]) -> tuple[set[str], str]:
    """The ``file-read*`` subpath roots, and everything from the first ``file-write*`` grant on."""

    profile = argv[argv.index("-p") + 1]
    assert profile.startswith("(version 1)(deny default)")
    reads, writes = profile.split("(allow file-read* ", 1)[1].split("(allow file-write*", 1)
    return set(re.findall(r'\(subpath "([^"]+)"\)', reads)), "(allow file-write*" + writes


def test_a_path_interpreter_in_a_homebrew_keg_gets_only_a_read_grant_on_its_framework_version(
    homebrew: _Homebrew,
) -> None:
    baseline, baseline_tail = _grants(homebrew.argv(()))
    reads, tail = _grants(homebrew.argv())

    # Both symlinks (prefix/bin -> keg/bin -> Frameworks) resolve to the binary's framework version directory.
    assert reads - baseline == {str(homebrew.framework)}
    assert baseline - reads == set()
    # No broader root: not the prefix, the Cellar, opt, the formula or the keg itself.
    for broader in (
        homebrew.prefix,
        homebrew.prefix / "Cellar",
        homebrew.prefix / "opt",
        homebrew.keg.parent,
        homebrew.keg,
    ):
        assert str(broader) not in reads
    # Everything from the write grant on is unchanged: workspace and /dev/null only, no network.
    assert tail == baseline_tail
    assert tail == f'(allow file-write* (subpath {json.dumps(str(homebrew.workspace))}) (literal "/dev/null"))'
    assert "network" not in tail and str(homebrew.framework) not in tail
    # The deny rules and the default-deny shape are untouched, and the user's home stays closed.
    argv = homebrew.argv()
    profile = argv[argv.index("-p") + 1]
    assert profile.startswith("(version 1)(deny default)(allow process-exec process-fork)")
    assert str(homebrew.base / "home") not in profile
    assert profile.count(str(homebrew.framework)) == 1


@pytest.mark.parametrize(
    "layout",
    [
        "real_file_in_prefix_bin",
        "outside_prefix",
        "symlink_out_of_prefix",
        "shadowed_on_path",
        "keg_under_home",
        "keg_too_shallow",
        "not_executable",
    ],
)
def test_a_path_interpreter_outside_a_homebrew_keg_gets_no_grant(
    homebrew: _Homebrew, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    def executable(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    brew_python = homebrew.prefix / "bin" / "python3"
    if layout == "real_file_in_prefix_bin":  # rooting it would open the whole prefix
        brew_python.unlink()
        executable(brew_python)
    elif layout == "outside_prefix":
        executable(homebrew.base / "other" / "bin" / "python3")
        monkeypatch.setattr(macos_sandbox, "_SANDBOX_PATH", (str(homebrew.base / "other" / "bin"),))
    elif layout == "symlink_out_of_prefix":
        brew_python.unlink()
        brew_python.symlink_to(executable(homebrew.base / "outside" / "lib" / "py" / "bin" / "python3"))
    elif layout == "shadowed_on_path":  # the shell would run this one, so the keg one is never reached
        executable(homebrew.base / "usr" / "bin" / "python3")
        monkeypatch.setattr(
            macos_sandbox, "_SANDBOX_PATH", (str(homebrew.base / "usr" / "bin"), str(homebrew.prefix / "bin"))
        )
    elif layout == "keg_under_home":
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: homebrew.base))
    elif layout == "keg_too_shallow":  # <prefix>/Cellar/<formula> is shared by every version
        brew_python.unlink()
        brew_python.symlink_to(executable(homebrew.prefix / "Cellar" / "python@3.14" / "bin" / "python3"))
    else:
        (homebrew.framework / "bin" / "python3.14").chmod(0o644)

    assert _grants(homebrew.argv()) == _grants(homebrew.argv(()))


def test_an_interpreter_nobody_asked_for_is_not_granted_and_existing_callers_are_unchanged(
    homebrew: _Homebrew,
) -> None:
    assert _grants(homebrew.argv(("node",))) == _grants(homebrew.argv(()))
    argv = homebrew.argv(())
    assert str(homebrew.framework) not in argv[argv.index("-p") + 1]


def test_the_review_bash_tool_asks_the_macos_sandbox_for_its_interpreters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def record(command: list[str], **kwargs: object) -> list[str]:
        seen.update(kwargs)
        return command

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(macos_sandbox, "sandbox_command", record)
    tool_server._confined_shell(tmp_path, "true")
    assert seen["path_executables"] == ("python3", "python")
    assert seen["writable_workspace"] is True and not seen.get("executables") and not seen.get("repo_executables")


# One case per Homebrew-keg python3 the sandbox PATH reaches on this host; none (or off macOS) means no case runs.
_HOMEBREW_PYTHON_ROOTS = (
    sorted(macos_sandbox._path_tool_roots(("python3",), list(macos_sandbox._SANDBOX_PATH), Path.home().resolve()))
    if sys.platform == "darwin"
    else []
)


@requires_macos
@pytest.mark.parametrize("framework", _HOMEBREW_PYTHON_ROOTS, ids=str)
def test_macos_sandbox_runs_the_homebrew_python3_with_read_access_to_its_framework_only(
    tmp_path: Path, framework: Path
) -> None:
    cellar = next(parent for parent in framework.parents if parent.name == "Cellar")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = framework / "lc-sandbox-probe"
    probe = "\n".join(
        [
            "import os",
            "def denied(action):",
            "    try:",
            "        action()",
            "    except OSError:",
            "        return True",
            "    return False",
            f"assert os.listdir({str(framework)!r})",
            f"assert denied(lambda: os.listdir({str(cellar)!r}))",
            f"assert denied(lambda: os.listdir({str(cellar.parent)!r}))",
            f"assert denied(lambda: open({str(scratch)!r}, 'w'))",
            f"assert denied(lambda: os.listdir({str(Path.home())!r}))",
            "print('confined')",
        ]
    )

    def run(code: str, **grants: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        argv = sandbox_command(
            ["python3", "-c", code], workspace=workspace, cwd=workspace, writable_workspace=True, **grants
        )
        return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)

    try:
        assert run("pass").returncode != 0  # without the grant the framework is unreadable: the grant is what runs it
        granted = run(probe, path_executables=("python3",))
    finally:
        scratch.unlink(missing_ok=True)
    assert granted.returncode == 0, granted.stdout + granted.stderr
    assert granted.stdout.strip() == "confined"


@requires_macos
def test_macos_command_runner_gives_an_analysis_driver_somewhere_to_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seatbelt has no private /tmp: the driver stopped before writing any report."""

    context = SimpleNamespace(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        execution=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
        runner_host=runner_host,
    )

    def materialize(*args: object, target: Path, **kwargs: object) -> str:
        target.mkdir(parents=True)
        return "identity"

    monkeypatch.setattr(runner_host, "materialize_review_side", materialize)
    command, why = driver_command(
        infra_render.DRIVER, {"entry": "infra.render", "spec": "kustomize-k8s", "tool": "kustomize", "dir": "absent"}
    )
    assert command, why
    runtime = RuntimeConfigEntry("analysis", "command", {"command": list(command), "report": True})
    surface = SimpleNamespace(id="analysis", provider="unit-tests")
    result = runtimes.CommandRunner().run(context, runtime, surface, side="new")
    report, reason = decode_report(result.data["report"])
    assert report is not None, (reason, result.output, result.summary)
    assert report["exit"] == 0
    assert report["missing"] is True
