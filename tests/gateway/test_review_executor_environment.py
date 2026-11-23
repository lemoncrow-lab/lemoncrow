"""The executor environment probe a carry depends on (Trust T3 item 1 fixes).

The probe resolves what a recipe actually runs through the sandbox's PATH and
mounts, records each executable's resolved path, version output and content
digest with the host image and kernel release, and caches nothing by process:
every cached probe is keyed by the binary's on-disk identity, so an upgrade under
a running server is probed again. Any unknown component makes the whole
environment unknown (``""``), so it never carries.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import hermetic
from lemoncrow.pro.capabilities.review.runner_host import command_environment, command_identity, command_view
from lemoncrow.pro.environment import runners as runtimes
from lemoncrow.pro.environment.execution import EXECUTOR_FINGERPRINT_VERSION


def _tool(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory on the sandbox's host-runtime PATH, mounted read-only like ``/usr/local``."""

    directory = tmp_path / "tools"
    directory.mkdir()
    monkeypatch.setattr(runtimes, "_COMMAND_OPTIONAL_MOUNTS", (*runtimes._COMMAND_OPTIONAL_MOUNTS, str(directory)))
    monkeypatch.setattr(runtimes, "_COMMAND_PATH", f"{directory}:{runtimes._COMMAND_PATH}")
    return directory


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every version probe the fingerprint actually runs."""

    ran: list[list[str]] = []
    real = hermetic._PROBE_POPEN

    def counting(argv: list[str], **kwargs: Any) -> Any:
        ran.append(list(argv))
        return real(argv, **kwargs)

    monkeypatch.setattr(hermetic, "_PROBE_POPEN", counting)
    return ran


def test_the_fingerprint_records_what_the_recipe_runs(tools: Path) -> None:
    tool = _tool(tools / "pyrun", 'echo "pyrun 1.0"\n')
    fingerprint = command_identity(["pyrun", "run_tests.py"])
    assert fingerprint.version == EXECUTOR_FINGERPRINT_VERSION == 2
    first, interpreter, *_rest = fingerprint.executables
    assert (first.name, first.path, first.version) == ("pyrun", str(tool), "pyrun 1.0")
    assert first.sha256.startswith("sha256:") and len(first.sha256) == 71
    # Its ``#!`` interpreter ran too, resolved inside the sandbox view.
    assert interpreter.name == "/bin/sh" and interpreter.path and interpreter.sha256
    assert fingerprint.kernel == os.uname().release
    assert fingerprint.executor_known and command_environment(["pyrun"]).startswith("envx:sha256:")
    assert command_environment(["pyrun"]) == fingerprint.executor_digest


def test_an_upgraded_executable_is_probed_again_under_one_running_process(tools: Path, probes: list[list[str]]) -> None:
    """No per-process cache: the probe is keyed by the binary's on-disk identity."""

    tool = _tool(tools / "pyrun", 'echo "pyrun 1.0"\n')
    before = command_environment(["pyrun"])
    assert before and [str(tool), "--version"] in probes
    # Unchanged: served from the cache, nothing runs again.
    probes.clear()
    assert command_environment(["pyrun"]) == before and [str(tool), "--version"] not in probes
    # Rewritten in place (same inode, new bytes and version): probed again.
    _tool(tool, 'echo "pyrun 2.0"\n')
    upgraded = command_environment(["pyrun"])
    assert [str(tool), "--version"] in probes
    assert upgraded and upgraded != before
    assert command_identity(["pyrun"]).executables[0].version == "pyrun 2.0"
    # Replaced by rename (a package upgrade): probed again.
    probes.clear()
    staged = _tool(tools / ".pyrun.new", 'echo "pyrun 3.0"\n')
    os.replace(staged, tool)
    assert command_environment(["pyrun"]) not in {before, upgraded}
    assert [str(tool), "--version"] in probes
    # Other bytes under the same version output: the content digest alone moves the environment.
    same_version = command_environment(["pyrun"])
    _tool(tool, 'echo "pyrun 3.0"  # rebuilt\n')
    assert command_identity(["pyrun"]).executables[0].version == "pyrun 3.0"
    assert command_environment(["pyrun"]) != same_version


def test_the_image_part_is_read_again_when_its_inputs_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.delenv("LEMONCROW_SANDBOX_IMAGE_DIGEST", raising=False)
    monkeypatch.setenv("PATH", f"{host}:{os.environ.get('PATH', '')}")
    bwrap = _tool(host / "bwrap", 'echo "bubblewrap 0.1.0"\n')
    first = hermetic.executor_image_digest()
    assert first.startswith("host:sha256:") and first == hermetic.runtime_image_digest()
    _tool(bwrap, 'echo "bubblewrap 0.2.0"\n')
    second = hermetic.executor_image_digest()
    assert second.startswith("host:sha256:") and second != first
    # A bubblewrap whose version cannot be read leaves the image partial, so it never carries.
    _tool(bwrap, "exit 0\n")
    assert hermetic.executor_image_digest().startswith("host-partial:sha256:")
    assert command_environment(["sh"]) == ""
    # A declared worker image is the image.
    monkeypatch.setenv("LEMONCROW_SANDBOX_IMAGE_DIGEST", "sha256:worker")
    assert hermetic.executor_image_digest() == "sha256:worker"


def test_a_new_kernel_is_another_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    before = command_environment(["sh"])
    assert before
    real = os.uname()
    moved = os.uname_result((real.sysname, real.nodename, real.release + "-next", real.version, real.machine))
    monkeypatch.setattr(hermetic.os, "uname", lambda: moved)
    assert command_identity(["sh"]).kernel == real.release + "-next"
    assert command_environment(["sh"]) not in {"", before}
    unread = os.uname_result((real.sysname, real.nodename, "", real.version, real.machine))
    monkeypatch.setattr(hermetic.os, "uname", lambda: unread)
    assert command_environment(["sh"]) == ""


@pytest.mark.skipif(sys.platform != "linux", reason="the bubblewrap view maps the workspace to /workspace")
def test_an_unknown_component_makes_the_whole_environment_unknown(tools: Path, tmp_path: Path) -> None:
    """Unknown never compares equal: an unknown environment has no executor digest at all."""

    # A name the sandbox cannot resolve.
    assert command_environment(["lemoncrow-no-such-tool"]) == ""
    assert command_identity(["lemoncrow-no-such-tool"]).executables[0].path == ""
    # A tool that prints no version.
    _tool(tools / "silent", "exit 0\n")
    assert command_identity(["silent"]).executables[0].version == "unknown"
    assert command_environment(["silent"]) == command_environment(["silent"]) == ""
    # A tool whose version output runs away is cut off, not read to the end.
    _tool(tools / "chatty", "while :; do echo y; done\n")
    assert command_identity(["chatty"]).executables[0].version == "unavailable"
    assert command_environment(["chatty"]) == ""
    # A tool that rejects the flag still answers deterministically.
    _tool(tools / "strict", 'echo "strict: bad option" >&2; exit 2\n')
    assert command_identity(["strict"]).executables[0].version == "exit 2: strict: bad option"
    assert command_environment(["strict"])
    # Reviewed content (the workspace) is never run or read by the probe, so it is unknown.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _tool(workspace / "run.sh", 'echo "run 1.0"\n')
    view = command_view(workspace=workspace)
    [script, *_wrappers] = command_identity(["./run.sh"], view=view).executables
    assert (script.path, script.version, script.sha256) == ("/workspace/run.sh", "", "")
    assert command_identity(["./run.sh"], view=view).executor_digest == ""
    # Before dispatch there is no workspace in the view: the name does not resolve at all.
    assert command_identity(["./run.sh"]).executables[0].path == ""


def test_a_probe_failure_is_unavailable_not_a_crash(tools: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tool(tools / "pyrun", 'echo "pyrun 1.0"\n')

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("exec format error")

    monkeypatch.setattr(hermetic, "_PROBE_POPEN", refuse)
    _tool(tools / "pyrun", 'echo "pyrun 1.1"\n')  # a new identity, so the cache cannot answer
    assert command_identity(["pyrun"]).executables[0].version == "unavailable"
    assert command_environment(["pyrun"]) == ""
