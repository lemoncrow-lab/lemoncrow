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
import subprocess
import sys
import tomllib
from collections.abc import Mapping
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
                assert (
                    name in sys.stdlib_module_names or name in own
                ), f"{path.name} imports third-party module {name!r}"


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
# 2. No socket, no port, no daemon -- structurally                            #
# --------------------------------------------------------------------------- #


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module)
    return names


def test_the_package_never_touches_a_socket_api() -> None:
    """A package that never holds a socket cannot bind one."""
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
        assert not overlap, f"{path.name} imports {sorted(overlap)}"


def test_only_the_transport_module_reaches_the_network() -> None:
    """One module to read for egress, which is the point of the layout."""
    network = {"urllib.request", "urllib.error", "http.client", "ftplib", "smtplib", "telnetlib"}
    reaching = {path.name for path in iter_modules() if _imported_names(path) & network}
    assert reaching == {"transport.py"}


def test_the_transport_module_never_writes_a_file() -> None:
    """Nothing that arrives over the network can become a file on disk.

    That is the structural form of "downloads no binary or model": the only
    module that can receive bytes from the network has no way to persist them.
    """
    source = (PACKAGE_ROOT / "transport.py").read_text(encoding="utf-8")
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
            assert needle not in body, f"{path.name} mentions {needle!r}"


def test_the_package_ships_no_auxiliary_session_or_mcp_process_implementation() -> None:
    """The forbidden behavior is absent from the wheel, not merely unrouted."""
    assert not (PACKAGE_ROOT / "hook.py").exists()
    assert not (PACKAGE_ROOT / "localtools" / "proxy.py").exists()
    assert not (PACKAGE_ROOT / "localtools" / "delegate.py").exists()


def test_the_package_has_no_background_or_persistence_primitives() -> None:
    """No thread, no atexit, no fork, no signal handler, no detached session."""
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
            assert needle not in body, f"{path.name} uses {needle!r}"


def test_the_package_has_no_self_update_path() -> None:
    forbidden = ("pip", "ensurepip", "venv", "setuptools", "importlib.reload", "git pull", "install.sh")
    for path in iter_modules():
        body = code_only(path)
        if path.name == "cli.py":
            # ``cli.py`` reads ``importlib.metadata`` to *report* the dependency
            # set. Reading metadata is not installing anything.
            body = body.replace("importlib", "")
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


def test_persistent_state_writers_are_confined_to_the_worktree_or_state_directory() -> None:
    """Keep the client file-writer allowlist explicit and closed.

    ``kit/fsio.py`` performs every worktree write of the shared kit (atomic
    edit writes and edit rollback), which the ``edit`` executor calls.
    ``cli.py``, ``credentials.py`` and ``session.py`` write bounded client
    state; ``review.py`` freezes an exact user-invoked Review snapshot under
    that same state directory; ``shell.py`` persists the bounded overflow body
    for a capped local-shell result. Every other module
    must remain pure unless its persistence contract is reviewed here.
    """
    writers: list[str] = []
    for path in iter_modules():
        body = code_only(path)
        if any(needle in body for needle in ("write_text(", "os.fdopen(", "write_bytes(")):
            writers.append(path.name)
    assert sorted(writers) == [
        "cli.py",
        "credentials.py",
        "fsio.py",
        "review.py",
        "session.py",
        "shell.py",
    ], sorted(writers)


def test_review_snapshots_are_confined_to_the_client_state_directory() -> None:
    source = (PACKAGE_ROOT / "review.py").read_text(encoding="utf-8")
    assert 'TemporaryDirectory(prefix="review-", dir=config.state_dir)' in source
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


def test_the_client_binds_no_port(transcript: dict[str, Any]) -> None:
    assert _events(transcript, "socket.bind") == []


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
        command = argv[command_index] if len(argv) > command_index else ""
        if command == "status":
            assert tuple([argv[0], *argv[command_index:]]) == _SESSION_GIT_STATUS, f"git status was run as {argv}"
            env = entry["args"][3] if len(entry["args"]) > 3 else None
            assert (
                isinstance(env, dict) and "GIT_OPTIONAL_LOCKS" in env
            ), "git status must disable optional index-refresh writes"
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
    # read degraded to disk; grep is unaffected; code_search refuses.
    assert transcript["result"]["degraded"] == [True, False, False]
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
    created = [path for path in _written_paths(transcript) if os.path.isabs(path) and Path(path) != Path(os.devnull)]
    assert created == [], f"an offline session wrote {created}"
    assert list(state_dir.iterdir()) == []
