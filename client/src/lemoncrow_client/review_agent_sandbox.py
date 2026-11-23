"""Fail-closed, offline process boundary for opt-in Review author-agent tasks.

Not an agent dispatcher. A separately authenticated controller must validate the
signed lease and task, materialize the *exact* leased Snapshot, and approve a
provider/credential route before it may use this boundary. No model credentials
or network are exposed here; results NEVER satisfy verified checks or approval.

This stays in the zero-dependency standalone Review client, not Runtime/MCP.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .review_agent_adapters import AgentInvocationRefused, LocalAgentInvocation

# Limits are imposed *inside* the fresh namespace; the parent owns wall-clock
# cancellation. Never use the caller's ambient HOME, credentials or proxy.
_LAUNCHER = r"""
import json, os, resource, sys
argv = json.loads(sys.argv[1])
limits = json.loads(sys.argv[2])
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_CPU, (limits['cpu'], limits['cpu'] + 1))
resource.setrlimit(resource.RLIMIT_AS, (limits['memory'], limits['memory']))
resource.setrlimit(resource.RLIMIT_NPROC, (limits['pids'], limits['pids']))
resource.setrlimit(resource.RLIMIT_FSIZE, (limits['output'], limits['output']))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
os.execv('/agent', argv)
"""

_MAX_OUTPUT = 64 * 1024
_MAX_INPUT = 16 * 1024
_RUNTIME_MOUNTS = ("/usr", "/bin", "/lib", "/lib64", "/usr/local")


@dataclass(frozen=True, slots=True)
class AgentSandboxLimits:
    wall_seconds: int = 30
    cpu_seconds: int = 20
    memory_mb: int = 512
    process_count: int = 32

    def __post_init__(self) -> None:
        if any(
            type(value) is not int
            for value in (self.wall_seconds, self.cpu_seconds, self.memory_mb, self.process_count)
        ):
            raise ValueError("agent process limits must be integers")
        if not (
            1 <= self.wall_seconds <= 300
            and 1 <= self.cpu_seconds <= 300
            and 64 <= self.memory_mb <= 4096
            and 8 <= self.process_count <= 128
        ):
            raise ValueError("agent process limits exceed the local sandbox policy")


@dataclass(frozen=True, slots=True)
class AgentSandboxOutcome:
    status: Literal["exited", "failed", "timed_out", "cancelled", "not_run"]
    output: str
    exit_code: int | None
    duration_ms: int
    output_truncated: bool
    provenance: Literal["author_agent_unverified"] = "author_agent_unverified"
    model_usage_qualified: bool = False


class AgentSandbox:
    """Explicit offline author-agent subprocess; never called by `lcr watch`."""

    def __init__(self, *, bubblewrap: Path | None = None) -> None:
        self.bubblewrap = str(bubblewrap) if bubblewrap is not None else (shutil.which("bwrap") or "")

    def qualify(self) -> tuple[bool, str]:
        """A real read-only/no-network/secretless process probe, not a binary check."""
        if not self.bubblewrap or not Path(self.bubblewrap).is_file():
            return False, "Bubblewrap is unavailable"
        with tempfile.TemporaryDirectory(prefix="lc-agent-probe-") as root:
            snapshot = Path(root) / "snapshot"
            snapshot.mkdir()
            with (snapshot / "source.txt").open("w", encoding="utf-8") as writer:
                writer.write("immutable")
            repo = Path(root) / "repo"
            repo.mkdir()
            probe = Path(root) / "probe"
            probe_source = (
                "#!/usr/bin/python3\nimport os, pathlib, socket, sys\n"
                "assert pathlib.Path('source.txt').read_text() == 'immutable'\n"
                "try:\n pathlib.Path('source.txt').open('w').write('mutated')\n"
                "except OSError: pass\nelse: sys.exit(12)\n"
                "assert not pathlib.Path('/root/.ssh').exists()\n"
                "assert not pathlib.Path('/run/docker.sock').exists()\n"
                "assert os.getenv('LEMONCROW_PROBE_SECRET') is None\n"
                "try: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('1.1.1.1', 80))\n"
                "except OSError: pass\nelse: sys.exit(13)\n"
                "print('qualified')\n"
            )
            with probe.open("w", encoding="utf-8") as writer:
                writer.write(probe_source)
            probe.chmod(0o700)
            # A dummy repo is never itself mounted; the snapshot is a separate
            # source tree. Do not pass any local provider credentials to probe.
            invocation = LocalAgentInvocation(
                provider="codex",
                executable=str(probe),
                argv=(str(probe),),
                stdin="test",
                cwd=repo,
                environment_overrides={},
                task_id="probe",
                review_id="probe",
                snapshot_digest="a" * 64,
                max_runtime_seconds=3,
            )
            try:
                result = self.run(
                    invocation,
                    snapshot=snapshot,
                    limits=AgentSandboxLimits(wall_seconds=3, cpu_seconds=2, memory_mb=128, process_count=16),
                )
            except (OSError, ValueError, AgentInvocationRefused) as exc:
                return False, f"sandbox qualification could not run: {exc}"
            if result.status != "exited" or result.output.strip() != "qualified":
                return False, "read-only/no-network sandbox probe failed"
            if (snapshot / "source.txt").read_text() != "immutable":
                return False, "source changed during sandbox qualification"
        return True, ""

    def run(
        self,
        invocation: LocalAgentInvocation,
        *,
        snapshot: Path,
        limits: AgentSandboxLimits,
        should_cancel: Callable[[], bool] | None = None,
        cancellation_poll_s: float = 0.25,
    ) -> AgentSandboxOutcome:
        """Execute a pre-admitted invocation against a separate read-only tree.

        Caller MUST independently authorize the signed lease and materialize
        and hash-check the immutable source. This function never performs a
        network claim, forwards credentials or reports a successful AI result.
        """
        if not self.bubblewrap or not Path(self.bubblewrap).is_file():
            raise AgentInvocationRefused("unavailable Bubblewrap sandbox")
        if not 0.1 <= cancellation_poll_s <= 5.0:
            raise ValueError("cancellation polling interval is out of bounds")
        if invocation.result_provenance != "author_agent_unverified":
            raise AgentInvocationRefused("author agent cannot verify a Review")
        if len(invocation.argv) < 1 or invocation.argv[0] != invocation.executable:
            raise AgentInvocationRefused("agent executable differs from locally admitted invocation")
        binary = Path(invocation.executable).resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise AgentInvocationRefused("locally selected agent executable is unavailable")
        source = snapshot.resolve(strict=True)
        repo = invocation.cwd.resolve(strict=True)
        if (
            not source.is_dir()
            or not repo.is_dir()
            or source == repo
            or source in repo.parents
            or repo in source.parents
        ):
            raise AgentInvocationRefused("agent must use a separately materialized immutable revision")
        if binary == repo or repo in binary.parents or binary == source or source in binary.parents:
            raise AgentInvocationRefused("agent executable cannot be supplied by untrusted repository source")
        if not (1 <= len(invocation.stdin.encode("utf-8")) <= _MAX_INPUT):
            raise AgentInvocationRefused("agent prompt exceeds bounded local input")
        if should_cancel is not None and should_cancel():
            return AgentSandboxOutcome("cancelled", "", None, 0, False)
        wall = min(limits.wall_seconds, invocation.max_runtime_seconds)
        command = ["/agent", *("/snapshot" if word == str(repo) else word for word in invocation.argv[1:])]
        resources = {
            "cpu": min(limits.cpu_seconds, wall),
            "memory": limits.memory_mb * 1024 * 1024,
            "pids": limits.process_count,
            "output": _MAX_OUTPUT,
        }
        args = [
            self.bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--unshare-net",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            str(source),
            "/snapshot",
            "--ro-bind",
            str(binary),
            "/agent",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/nonexistent",
        ]
        for mount in _RUNTIME_MOUNTS:
            if Path(mount).exists():
                args.extend(("--ro-bind", mount, mount))
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/nonexistent",
            "TMPDIR": "/tmp",
            "CI": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(invocation.environment_overrides)
        for key, value in env.items():
            args.extend(("--setenv", key, value))
        args.extend(
            (
                "--chdir",
                "/snapshot",
                "--",
                "/usr/bin/python3",
                "-I",
                "-c",
                _LAUNCHER,
                json.dumps(command),
                json.dumps(resources),
            )
        )
        started = time.monotonic()
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={"PATH": "/usr/bin:/bin"},
                process_group=0,
            )
            status: Literal["exited", "failed", "timed_out", "cancelled", "not_run"] = "failed"
            try:
                first = True
                while True:
                    if should_cancel is not None:
                        try:
                            if should_cancel():
                                status = "cancelled"
                                break
                        except Exception:
                            status = "cancelled"  # lost lease/control channel: fail closed
                            break
                    if time.monotonic() - started >= wall:
                        status = "timed_out"
                        break
                    try:
                        proc.communicate(
                            input=invocation.stdin.encode("utf-8") if first else None,
                            timeout=min(cancellation_poll_s, max(0.01, wall - (time.monotonic() - started))),
                        )
                        status = "exited" if proc.returncode == 0 else "failed"
                        break
                    except subprocess.TimeoutExpired:
                        first = False
            finally:
                if proc.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=3)
            output.seek(0, os.SEEK_END)
            size = output.tell()
            output.seek(0)
            raw = output.read(_MAX_OUTPUT)
            return AgentSandboxOutcome(
                status=status,
                output=raw.decode("utf-8", errors="replace"),
                exit_code=proc.returncode if status in {"exited", "failed"} else None,
                duration_ms=int((time.monotonic() - started) * 1000),
                output_truncated=size > _MAX_OUTPUT,
            )


__all__ = ["AgentSandbox", "AgentSandboxLimits", "AgentSandboxOutcome"]
