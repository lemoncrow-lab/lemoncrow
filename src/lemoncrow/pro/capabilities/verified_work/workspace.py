"""Isolated candidate workspace, structured patch application, and verification."""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.verified_work.models import RepairProposal, VerifiedWorkOpportunity

_MAX_UNTRACKED_FILES = 128
_MAX_UNTRACKED_BYTES = 16 * 1024 * 1024
_DEPENDENCY_DIRS = ("node_modules", ".venv", "venv")


@dataclass(frozen=True, slots=True)
class VerifierExecution:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    network_isolated: bool
    resource_limited: bool


def _run(
    args: list[str], *, cwd: Path | None = None, input_bytes: bytes | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _copy_path(source: Path, target: Path) -> int:
    if source.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        target.symlink_to(os.readlink(source))
        return 0
    if source.is_file():
        size = source.stat().st_size
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return size
    if not source.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    return 0


def _git_root(workspace: Path) -> Path:
    proc = _run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        raise RuntimeError("workspace is not a git worktree")
    return Path(proc.stdout.decode("utf-8", errors="replace").strip()).resolve()


class CandidateWorkspace(AbstractContextManager["CandidateWorkspace"]):
    """Disposable git worktree overlaid with the live checkout's dirty files."""

    def __init__(self, opportunity: VerifiedWorkOpportunity) -> None:
        self.opportunity = opportunity
        self.live_root = _git_root(Path(opportunity.workspace))
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None
        self.workspace: Path | None = None
        self.cwd: Path | None = None

    def __enter__(self) -> CandidateWorkspace:
        live_workspace = Path(self.opportunity.workspace).resolve()
        live_cwd = Path(self.opportunity.cwd).resolve()
        try:
            rel_workspace = live_workspace.relative_to(self.live_root)
            rel_cwd = live_cwd.relative_to(self.live_root)
        except ValueError as exc:
            raise RuntimeError("verifier cwd escapes git workspace") from exc

        self._tmp = tempfile.TemporaryDirectory(prefix="lemoncrow-verified-work-")
        candidate = Path(self._tmp.name) / "candidate"
        proc = _run(
            ["git", "-C", str(self.live_root), "worktree", "add", "--detach", "--quiet", str(candidate), "HEAD"],
            timeout=60,
        )
        if proc.returncode != 0:
            self._tmp.cleanup()
            self._tmp = None
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:1000] or "git worktree add failed")
        self.root = candidate
        try:
            self._overlay_dirty_files()
            self._link_dependency_dirs()
            self.workspace = (candidate / rel_workspace).resolve()
            self.cwd = (candidate / rel_cwd).resolve()
            if not self.cwd.is_dir():
                raise RuntimeError("candidate verifier cwd is missing")
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _overlay_dirty_files(self) -> None:
        assert self.root is not None
        changed = _run(["git", "-C", str(self.live_root), "diff", "--no-renames", "--name-only", "-z", "HEAD"])
        if changed.returncode != 0:
            raise RuntimeError("cannot enumerate dirty tracked files")
        for raw in changed.stdout.split(b"\0"):
            if not raw:
                continue
            rel = Path(raw.decode("utf-8", errors="surrogateescape"))
            source = self.live_root / rel
            target = self.root / rel
            # Gitlinks/directories are intentionally not mirrored; ordinary
            # verifier repairs should not depend on mutating submodule state.
            if source.is_dir() and not source.is_symlink():
                continue
            _copy_path(source, target)

        untracked = _run(["git", "-C", str(self.live_root), "ls-files", "--others", "--exclude-standard", "-z"])
        if untracked.returncode != 0:
            raise RuntimeError("cannot enumerate untracked files")
        count = 0
        total = 0
        for raw in untracked.stdout.split(b"\0"):
            if not raw:
                continue
            count += 1
            if count > _MAX_UNTRACKED_FILES:
                raise RuntimeError("too many untracked files for isolated shadow repair")
            rel = Path(raw.decode("utf-8", errors="surrogateescape"))
            source = self.live_root / rel
            if source.is_dir() and not source.is_symlink():
                continue
            size = source.stat().st_size if source.is_file() else 0
            total += size
            if total > _MAX_UNTRACKED_BYTES:
                raise RuntimeError("untracked files exceed isolated shadow repair budget")
            _copy_path(source, self.root / rel)

    def _link_dependency_dirs(self) -> None:
        assert self.root is not None
        bases = [self.live_root]
        try:
            bases.extend(path for path in self.live_root.iterdir() if path.is_dir() and not path.name.startswith("."))
        except OSError:
            pass
        for base in bases:
            try:
                rel_base = base.relative_to(self.live_root)
            except ValueError:
                continue
            for name in _DEPENDENCY_DIRS:
                source = base / name
                target = self.root / rel_base / name
                if not source.is_dir() or target.exists() or target.is_symlink():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source, target_is_directory=True)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.root is not None:
            _run(["git", "-C", str(self.live_root), "worktree", "remove", "--force", str(self.root)], timeout=30)
        if self._tmp is not None:
            self._tmp.cleanup()
        self.root = None
        self.workspace = None
        self.cwd = None
        self._tmp = None


def apply_proposal(
    root: Path, proposal: RepairProposal, *, allowed_paths: tuple[str, ...], max_diff_lines: int
) -> tuple[tuple[str, ...], int]:
    allowed = set(allowed_paths)
    before: dict[str, str] = {}
    for edit in proposal.edits:
        if edit.path not in allowed:
            raise ValueError(f"edit path is not allowed: {edit.path}")
        path = (root / edit.path).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("edit path escapes candidate workspace") from exc
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read edit target: {edit.path}") from exc
        before.setdefault(edit.path, text)
        if text.count(edit.old) != 1:
            raise ValueError(f"edit old text must match exactly once: {edit.path}")
        path.write_text(text.replace(edit.old, edit.new, 1), encoding="utf-8")

    changed = tuple(dict.fromkeys(edit.path for edit in proposal.edits))
    diff_lines = 0
    for rel in changed:
        after = (root / rel).read_text(encoding="utf-8")
        delta = list(
            difflib.unified_diff(
                before[rel].splitlines(),
                after.splitlines(),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
        )
        diff_lines += sum(1 for line in delta if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    if diff_lines > max_diff_lines:
        raise ValueError(f"candidate diff exceeds budget: {diff_lines}>{max_diff_lines}")
    return changed, diff_lines


def verifier_environment(candidate_root: Path) -> dict[str, str]:
    home = candidate_root / ".lemoncrow-shadow-home"
    home.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "CI": "1",
        "NO_COLOR": "1",
        "PIP_NO_INDEX": "1",
        "UV_OFFLINE": "1",
        "npm_config_offline": "true",
        "YARN_ENABLE_NETWORK": "0",
        "CARGO_NET_OFFLINE": "true",
        "GOPROXY": "off",
        "GONOSUMDB": "*",
    }
    for key in ("LANG", "LC_ALL", "TZ"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def run_exact_verifier(
    command: str,
    *,
    cwd: Path,
    root: Path,
    timeout_s: int,
    memory_mb: int = 2048,
) -> VerifierExecution:
    """Run the exact verifier with stripped credentials and strongest local sandbox available."""
    from lemoncrow.pro.environment.sandbox_limits import limit_prefix_for

    started = time.perf_counter()
    env = verifier_environment(root)
    shell_command: list[str] = ["/bin/bash", "-c", command]
    network_isolated = False

    # On Linux reuse Review's hermetic bubblewrap runtime: only runtime
    # binaries/libraries, the candidate tree, and dependency targets are visible.
    # Never ro-bind the host root -- that would make ~/.ssh/cloud credentials
    # readable by absolute path even with HOME redirected.
    bwrap = shutil.which("bwrap") if os.name == "posix" else None
    if bwrap and Path("/proc").exists():
        from lemoncrow.pro.capabilities.review.hermetic import offline_bwrap_prefix

        def _mkdir_args(path: Path, *, stop: Path | None = None) -> list[str]:
            resolved = path.resolve()
            parents: list[Path] = []
            current = resolved
            stop_resolved = stop.resolve() if stop is not None else None
            while current != current.parent and current != stop_resolved:
                parents.append(current)
                current = current.parent
            args: list[str] = []
            for item in reversed(parents):
                args.extend(("--dir", str(item)))
            return args

        prefix = offline_bwrap_prefix(unshare_all=True)
        # /tmp and /home already exist inside Review's base namespace; create
        # only the nested destination path before binding the candidate root.
        parent_stop = (
            Path("/tmp")
            if str(root).startswith("/tmp/")
            else Path("/home") if str(root).startswith("/home/") else Path("/")
        )
        prefix.extend(_mkdir_args(root, stop=parent_stop))
        prefix.extend(("--bind", str(root), str(root)))

        # Candidate worktrees may contain absolute symlinks to dependency trees
        # from the live checkout. Expose only those concrete directories, read-
        # only, rather than the live repository or developer home.
        for dep_name in _DEPENDENCY_DIRS:
            for link in root.rglob(dep_name):
                if not link.is_symlink():
                    continue
                try:
                    target = link.resolve(strict=True)
                except OSError:
                    continue
                if not target.is_dir():
                    continue
                dep_stop = (
                    Path("/home")
                    if str(target).startswith("/home/")
                    else Path("/tmp") if str(target).startswith("/tmp/") else Path("/")
                )
                prefix.extend(_mkdir_args(target, stop=dep_stop))
                prefix.extend(("--ro-bind", str(target), str(target)))

        prefix.extend(("--chdir", str(cwd), "--", "/bin/bash", "-c", command))
        shell_command = prefix
        network_isolated = True

    limit_prefix = limit_prefix_for(memory_mb=memory_mb, timeout=float(timeout_s), tasks=128)
    argv = [*limit_prefix, *shell_command]
    resource_limited = bool(limit_prefix)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s + 3,
            check=False,
        )
        return VerifierExecution(
            exit_code=proc.returncode,
            stdout=proc.stdout[-12_000:],
            stderr=proc.stderr[-12_000:],
            duration_ms=int((time.perf_counter() - started) * 1000),
            network_isolated=network_isolated,
            resource_limited=resource_limited,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return VerifierExecution(
            exit_code=124,
            stdout=stdout[-12_000:],
            stderr=(stderr + "\nverifier timed out")[-12_000:],
            duration_ms=int((time.perf_counter() - started) * 1000),
            network_isolated=network_isolated,
            resource_limited=resource_limited,
        )


__all__ = ["CandidateWorkspace", "VerifierExecution", "apply_proposal", "run_exact_verifier", "verifier_environment"]
