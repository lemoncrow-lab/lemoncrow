"""External code-indexer benchmark in an isolated workspace.

Runs a practical cross-tool search benchmark for the indexers discussed in this
session:
  - LemonCrow code tool (local lexical index)
  - LemonCrow Zoekt adapter
  - Serena
  - CodeGraph
  - code-index-mcp
  - cocoindex-code (ccc)
  - jcodemunch-mcp

Usage:
  uv run python benchmarks/mcp_tools/bench_external_indexers.py --install --iterations 3
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import select
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import tiktoken

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

ENC = tiktoken.get_encoding("cl100k_base")
CODE_INDEX_REPO_URL = "https://github.com/johnhuang316/code-index-mcp.git"


def token_count(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return len(ENC.encode(text))


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


@dataclass
class ToolBenchResult:
    tool: str
    ok: bool
    median_ms: float
    p95_ms: float
    median_tokens: int
    runs: int
    query: str = ""
    error: str = ""
    input: str = ""
    sample: str = ""
    output: str = ""


class SerenaRunner:
    def __init__(
        self,
        *,
        project_root: Path,
        home_dir: Path,
        project_name: str = "lemoncrow-bench",
        port: int | None = None,
        language: str = "python",
    ) -> None:
        self.project_root = project_root
        self.home_dir = home_dir
        self.project_name = project_name
        self.port = port if port is not None else _find_free_port()
        self.language = language
        self.proc: subprocess.Popen[str] | None = None

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home_dir)
        return env

    def bootstrap(self) -> None:
        if self.home_dir.exists():
            shutil.rmtree(self.home_dir)
        self.home_dir.mkdir(parents=True, exist_ok=True)
        init = run_cmd(["serena", "init", "-b", "LSP"], cwd=self.project_root, timeout=300, env=self._env())
        if init.returncode != 0:
            raise RuntimeError(init.stderr[:1200] or init.stdout[:1200])
        create = run_cmd(
            [
                "serena",
                "project",
                "create",
                str(self.project_root),
                "--name",
                self.project_name,
                "--language",
                self.language,
            ],
            cwd=self.project_root,
            timeout=600,
            env=self._env(),
        )
        if create.returncode != 0:
            raise RuntimeError(create.stderr[:1200] or create.stdout[:1200])

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["serena", "start-project-server", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(self.project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=self._env(),
        )
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/heartbeat", timeout=2) as res:
                    if res.status == 200:
                        return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("Serena project server failed to start")

    def query(self, tool_name: str, params: dict[str, Any]) -> str:
        payload = json.dumps(
            {
                "project_name": self.project_name,
                "tool_name": tool_name,
                "tool_params_json": json.dumps(params),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/query_project",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as res:
            body = res.read()
            assert isinstance(body, bytes)
            return body.decode("utf-8", errors="replace")

    def stop(self) -> None:
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class CodeIndexRunner:
    """Persistent worker: one subprocess per repo that indexes once, then
    serves search_code queries over a stdin/stdout line protocol.

    A fresh ``python -c`` per query costs ~0.5s of interpreter + import
    overhead vs ~0.08s for the search itself, so one-shot mode made benchmark
    runs ~12x slower and reported latencies that measured the harness, not
    the tool.
    """

    _MARKER = "__CIDX__"

    _WORKER_SCRIPT = """
import json
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
code_index_repo = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(code_index_repo / "src"))

from code_index_mcp.project_settings import ProjectSettings
from code_index_mcp.server import CodeIndexerContext, _BootstrapRequestContext, mcp
from code_index_mcp.services.index_management_service import IndexManagementService
from code_index_mcp.services.project_management_service import ProjectManagementService
from code_index_mcp.services.search_service import SearchService
from mcp.server.fastmcp import Context

lifespan = CodeIndexerContext(base_path="", settings=ProjectSettings("", skip_load=True))
ctx = Context(request_context=_BootstrapRequestContext(lifespan), fastmcp=mcp)
ProjectManagementService(ctx).initialize_project(str(repo_root))
IndexManagementService(ctx).rebuild_deep_index(max_workers=4, timeout=600)
print("__CIDX__" + json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    try:
        result = SearchService(ctx).search_code(
            pattern=req["pattern"],
            regex=False,
            file_pattern=req.get("file_pattern", "*"),
            max_results=50,
            context_lines=0,
            case_sensitive=False,
        )
    except Exception as exc:  # report per-query failures without dying
        result = {"__cidx_error__": str(exc)}
    print("__CIDX__" + json.dumps(result, ensure_ascii=False), flush=True)
"""

    def __init__(self, repo_root: Path, workspace_root: Path, code_index_repo: Path) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        self.code_index_repo = code_index_repo
        self.project_root: Path | None = None
        self.python_bin: Path | None = None
        self.proc: subprocess.Popen[str] | None = None
        self._stderr_file: Any = None

    def start(self, *, python_bin: Path | None = None) -> None:
        tool_workspace = external_workspace_root(self.workspace_root)
        # When the caller already wired ensure_code_index_checkout +
        # ensure_code_index_runtime externally, don't redo the work (and
        # keep python_bin exactly as given -- it's already absolute, and
        # .venv/bin/python is a symlink to the shared uv-managed interpreter,
        # so .resolve() would follow it straight past the venv and lose its
        # site-packages, breaking every dependency uv sync installed there).
        if python_bin is not None:
            self.python_bin = python_bin
        else:
            self.code_index_repo = ensure_code_index_checkout(self.code_index_repo)
            self.python_bin = ensure_code_index_runtime(self.code_index_repo)
        self.project_root = prepare_repo_snapshot(self.repo_root, tool_workspace, "code-index-target")
        # Deliberately not a context manager: the file outlives start() and is
        # closed in stop() alongside the worker it captures stderr for.
        self._stderr_file = tempfile.TemporaryFile(mode="w+", prefix="cidx-worker-")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [
                str(self.python_bin),
                "-c",
                self._WORKER_SCRIPT,
                str(self.project_root),
                str(self.code_index_repo),
            ],
            cwd=str(self.code_index_repo),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            text=True,
            bufsize=1,
        )
        # Ready marker arrives after initialize_project + deep index rebuild.
        self._read_response(timeout=1800)

    def _stderr_tail(self) -> str:
        try:
            self._stderr_file.seek(0)
            tail = self._stderr_file.read()[-1200:]
            assert isinstance(tail, str)
            return tail
        except Exception:
            return ""

    def _read_response(self, timeout: float) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stop()
                raise TimeoutError(f"code-index-mcp worker timed out after {timeout:.0f}s")
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                continue  # loop re-checks the deadline
            line = self.proc.stdout.readline()
            if not line:
                err = self._stderr_tail()
                self.stop()
                raise RuntimeError(f"code-index-mcp worker died: {err}")
            if not line.startswith(self._MARKER):
                continue  # stray library output on stdout
            result = json.loads(line[len(self._MARKER) :])
            assert isinstance(result, dict)
            if "__cidx_error__" in result:
                raise RuntimeError(str(result["__cidx_error__"]))
            return result

    def query(self, pattern: str, file_pattern: str = "*") -> dict[str, Any]:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("code-index-mcp not initialized")
        request = json.dumps({"pattern": pattern, "file_pattern": file_pattern}, ensure_ascii=False)
        self.proc.stdin.write(request + "\n")
        self.proc.stdin.flush()
        return self._read_response(timeout=300)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                with suppress(Exception):
                    proc.wait(timeout=5)
            # Close stdin/stdout here (caught) instead of leaving them for GC
            # finalization, which raises an unsuppressable BrokenPipeError
            # against the already-dead process.
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with suppress(Exception):
                        stream.close()
        if self._stderr_file is not None:
            with suppress(Exception):
                self._stderr_file.close()
            self._stderr_file = None


def ensure_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


SNAPSHOT_IGNORE_NAMES = {
    ".git",
    ".venv",
    ".venv-build",
    ".lemoncrow-benchmarks",
    ".codegraph",
    ".mcp-vector-search",
    "node_modules",
    "reports",
    "benchmarks",
    "build",
    "build_dist",
    "dist",
    ".bench-work",
    ".cocoindex_code",
    ".serena",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}


def _is_ignored_snapshot_name(name: str) -> bool:
    # Exact-match the curated set, and prefix-match any virtualenv (.venv,
    # .venv-build, .venv-*) so build venvs never get copied/hashed into a
    # provider snapshot. Indexing third-party site-packages is slow, pollutes
    # results, and copies gigabytes of artifacts per shard.
    return name in SNAPSHOT_IGNORE_NAMES or name.startswith(".venv")


def _snapshot_relpaths(repo_root: Path) -> list[str] | None:
    """Repo-relative paths to snapshot: tracked + untracked files git would keep
    (honoring .gitignore / .git/info/exclude / global excludes), with the
    harness's own ignore list applied on top.

    Returns ``None`` when ``repo_root`` is not a git work tree (or git is
    unavailable), so callers fall back to a plain recursive walk.
    """
    if shutil.which("git") is None:
        return None
    proc = run_cmd(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-x",
            ".venv*",  # skip untracked build venvs git itself does not ignore
            "-z",
        ],
        timeout=300,
    )
    if proc.returncode != 0:
        return None
    rels = (rel for rel in proc.stdout.split("\0") if rel)
    return [rel for rel in rels if not any(_is_ignored_snapshot_name(part) for part in rel.split("/"))]


def _copy_one(src: Path, dst: Path) -> None:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            if src.exists():
                dst.symlink_to(os.readlink(src))
            return
        if src.is_file():
            shutil.copy2(src, dst)
    except FileNotFoundError:
        # File vanished between listing and copy (volatile output written
        # concurrently); skip it rather than aborting the whole snapshot.
        return


def _copy_repo_tree(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    rels = _snapshot_relpaths(src_dir)
    if rels is None:
        _copy_repo_tree_walk(src_dir, dst_dir)
        return
    for rel in rels:
        _copy_one(src_dir / rel, dst_dir / rel)


def _copy_repo_tree_walk(src_dir: Path, dst_dir: Path) -> None:
    """Fallback copy when ``src_dir`` is not a git work tree: honor only the
    harness ignore list."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for entry in src_dir.iterdir():
        if _is_ignored_snapshot_name(entry.name):
            continue
        target = dst_dir / entry.name
        try:
            if entry.is_symlink():
                if entry.exists():
                    target.symlink_to(os.readlink(entry))
                continue
            if entry.is_dir():
                _copy_repo_tree_walk(entry, target)
                continue
            if entry.is_file():
                shutil.copy2(entry, target)
        except FileNotFoundError:
            continue


def prepare_repo_snapshot(repo_root: Path, workspace_root: Path, name: str) -> Path:
    ensure_workspace(workspace_root)
    snapshot_root = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=workspace_root))
    _copy_repo_tree(repo_root, snapshot_root)
    return snapshot_root


def repo_cache_key(repo_root: Path) -> str:
    digest = hashlib.sha256()
    rels = _snapshot_relpaths(repo_root)
    if rels is not None:
        # Hash exactly what the snapshot will contain so the key tracks the
        # gitignore-aware file set and their contents.
        for rel in sorted(rels):
            path = repo_root / rel
            try:
                if path.is_symlink():
                    digest.update(f"link:{rel}:{os.readlink(path)}".encode())
                    continue
                if not path.is_file():
                    continue
                digest.update(f"file:{rel}".encode())
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            except FileNotFoundError:
                continue
        return digest.hexdigest()[:16]
    # Fallback: recursive walk honoring the harness ignore list.
    stack = [repo_root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda item: item.name):
            if _is_ignored_snapshot_name(entry.name):
                continue
            relative = entry.relative_to(repo_root).as_posix()
            if entry.is_dir():
                digest.update(f"dir:{relative}".encode())
                stack.append(entry)
                continue
            if not entry.is_file():
                continue
            digest.update(f"file:{relative}".encode())
            with entry.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()[:16]


@contextmanager
def cache_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def prepare_cached_repo_snapshot(
    repo_root: Path,
    cache_root: Path,
    *,
    name: str,
    cache_key: str,
) -> Path:
    ensure_workspace(cache_root)
    snapshot_root = cache_root / f"{name}-{cache_key}"
    marker_path = snapshot_root / ".lemoncrow-snapshot-ready.json"
    lock_path = cache_root / f"{name}-{cache_key}.lock"
    with cache_lock(lock_path):
        if marker_path.is_file():
            return snapshot_root
        if snapshot_root.exists():
            shutil.rmtree(snapshot_root)
        tmp_root = cache_root / f".{name}-{cache_key}.tmp-{os.getpid()}"
        if tmp_root.exists():
            shutil.rmtree(tmp_root)
        _copy_repo_tree(repo_root, tmp_root)
        (tmp_root / ".lemoncrow-snapshot-ready.json").write_text(
            json.dumps({"cache_key": cache_key}) + "\n", encoding="utf-8"
        )
        tmp_root.rename(snapshot_root)
    return snapshot_root


def install_external_tools(workspace: Path) -> None:
    ensure_workspace(workspace)
    commands = [
        ["npm", "i", "-g", "@colbymchenry/codegraph"],
        ["uv", "tool", "install", "-p", "3.13", "serena-agent"],
        ["uv", "tool", "install", "--upgrade", "cocoindex-code[full]"],
        [
            "uv",
            "tool",
            "install",
            "--upgrade",
            "https://github.com/jgravelle/jcodemunch-mcp/releases/download/v1.108.22/jcodemunch_mcp-1.108.22-py3-none-any.whl",
        ],
    ]
    # Best-effort: a missing package manager (npm/uv) or a single failed install
    # must not abort the whole matrix. Warn and continue so every provider whose
    # tool *did* install (plus the self-provisioning ones) still runs.
    failures: list[str] = []
    for cmd in commands:
        proc = run_cmd(cmd, cwd=workspace, timeout=1800)
        if proc.returncode != 0:
            label = " ".join(cmd)
            detail = (proc.stderr or proc.stdout or "").strip()[:800]
            failures.append(f"  {label}\n    {detail}")
            print(f"[install] WARNING: failed to install: {label}", file=sys.stderr)
    if failures:
        print(
            "[install] Some external provider tools could not be installed; "
            "those providers will report startup_failed:\n" + "\n".join(failures),
            file=sys.stderr,
        )


def external_workspace_root(workspace_root: Path) -> Path:
    root = workspace_root / "external-indexers"
    ensure_workspace(root)
    return root


def default_benchmark_root(repo_root: Path) -> Path:
    # Scratch/cache root for mcp_tools benchmark runs (repo snapshots, external
    # indexer installs, per-shard artifacts) -- lives inside the repo under
    # benchmarks/mcp_tools/results/, not a sibling directory outside the
    # checkout. Gitignored (see benchmarks/mcp_tools/results/.gitignore); the
    # committed results.csv/summary.csv for `lemon eval mcp` stay at the
    # shared reports/benchmark/mcp/ location every other suite uses.
    return repo_root / "benchmarks" / "mcp_tools" / "results"


def ensure_code_index_checkout(code_index_repo: Path) -> Path:
    src_path = code_index_repo / "src"
    if src_path.exists():
        return code_index_repo
    if code_index_repo.exists():
        shutil.rmtree(code_index_repo)
    ensure_workspace(code_index_repo.parent)
    proc = run_cmd(
        ["git", "clone", "--depth", "1", CODE_INDEX_REPO_URL, str(code_index_repo)],
        timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
    if not src_path.exists():
        raise RuntimeError(f"code-index-mcp src not found at {src_path}")
    return code_index_repo


def ensure_code_index_runtime(code_index_repo: Path) -> Path:
    python_bin = code_index_repo / ".venv" / "bin" / "python"
    if python_bin.exists():
        return python_bin
    proc = run_cmd(["uv", "sync"], cwd=code_index_repo, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
    if not python_bin.exists():
        raise RuntimeError(f"code-index-mcp python not found at {python_bin}")
    return python_bin


def bench_tools_root() -> Path:
    """Shared location for self-provisioned external comparator binaries."""
    root = Path.home() / ".lemoncrow" / "_bench_tools"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_universal_ctags() -> tuple[Path, Path]:
    """Build universal-ctags from source (no root required); return (ctags, readtags).

    Idempotent: a prior build is reused. Requires a C toolchain (gcc/make/autoconf/
    automake/pkg-config) on PATH. JSON output is unavailable without libjansson, so
    the runner consumes the native tags format via readtags instead.
    """
    prefix = bench_tools_root() / "ctags"
    ctags = prefix / "bin" / "ctags"
    readtags = prefix / "bin" / "readtags"
    if ctags.exists() and readtags.exists():
        return ctags, readtags
    src = bench_tools_root() / "ctags-src"
    if src.exists():
        shutil.rmtree(src)
    clone = run_cmd(
        ["git", "clone", "--depth", "1", "https://github.com/universal-ctags/ctags.git", str(src)],
        timeout=600,
    )
    if clone.returncode != 0:
        raise RuntimeError(clone.stderr[:1200] or clone.stdout[:1200])
    steps = [
        ["./autogen.sh"],
        ["./configure", f"--prefix={prefix}"],
        ["make", f"-j{os.cpu_count() or 2}"],
        ["make", "install"],
    ]
    for step in steps:
        proc = run_cmd(step, cwd=src, timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(f"ctags build failed at '{' '.join(step)}': " + (proc.stderr[:800] or proc.stdout[:800]))
    if not (ctags.exists() and readtags.exists()):
        raise RuntimeError("ctags build did not produce ctags/readtags binaries")
    return ctags, readtags


def bench_lemoncrow(repo_root: Path, workspace_root: Path, query: str, iterations: int) -> ToolBenchResult:
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from benchmarks.mcp_tools._env import configure_benchmark_runtime

    tool_workspace = external_workspace_root(workspace_root)
    snapshot_root = prepare_repo_snapshot(repo_root, tool_workspace, "lemoncrow-repo")
    runtime_root = Path(tempfile.mkdtemp(prefix="lemoncrow-root-", dir=tool_workspace))
    configure_benchmark_runtime(runtime_root, workspace_root=snapshot_root)
    from benchmarks.mcp_tools._env import call_code_op

    times: list[float] = []
    toks: list[int] = []
    sample = ""
    sample = ""
    request = {
        "op": "search",
        "repo_root": str(snapshot_root),
        "query": query,
        "mode": "lexical",
        "limit": 20,
        "budget_tokens": 4000,
    }
    for _ in range(iterations):
        t0 = time.perf_counter()
        resp = call_code_op(request)
        elapsed = (time.perf_counter() - t0) * 1000
        payload = json.dumps(resp, ensure_ascii=False)
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="lemoncrow",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        query=query,
        input=json.dumps(request, ensure_ascii=False),
        sample=sample,
        output=payload,
    )


def bench_lemoncrow_zoekt(repo_root: Path, workspace_root: Path, query: str, iterations: int) -> ToolBenchResult:
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from benchmarks.mcp_tools._env import configure_benchmark_runtime

    tool_workspace = external_workspace_root(workspace_root)
    snapshot_root = prepare_repo_snapshot(repo_root, tool_workspace, "lemoncrow-zoekt-repo")
    runtime_root = Path(tempfile.mkdtemp(prefix="lemoncrow-zoekt-root-", dir=tool_workspace))
    configure_benchmark_runtime(runtime_root, workspace_root=snapshot_root)
    from lemoncrow.infra.code_intel.zoekt.adapter import get_zoekt_supervisor, reset_zoekt_supervisors

    reset_zoekt_supervisors()
    request = {
        "query": query,
        "search_path": str(snapshot_root),
        "max_files": 20,
        "max_chars_per_file": 600,
        "include_outline": False,
    }
    max_files = 20
    max_chars_per_file = 600
    include_outline = False
    supervisor = get_zoekt_supervisor(snapshot_root)
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        result = supervisor.search(
            query=query,
            search_path=snapshot_root,
            max_files=max_files,
            max_chars_per_file=max_chars_per_file,
            include_outline=include_outline,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        payload = json.dumps(asdict(result), ensure_ascii=False)
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="lemoncrow-zoekt",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        query=query,
        input=json.dumps(request, ensure_ascii=False),
        sample=sample,
        output=payload,
    )


def bench_serena(repo_root: Path, workspace_root: Path, query: str, iterations: int) -> ToolBenchResult:
    tool_workspace = external_workspace_root(workspace_root)
    snapshot_root = prepare_repo_snapshot(repo_root, tool_workspace, "serena-repo")
    runner = SerenaRunner(
        project_root=snapshot_root,
        home_dir=tool_workspace / "serena-home",
    )
    try:
        runner.bootstrap()
        runner.start()
        times: list[float] = []
        toks: list[int] = []
        sample = ""
        params = {
            "substring_pattern": query,
            "relative_path": "src/lemoncrow",
            "restrict_search_to_code_files": True,
        }
        for _ in range(iterations):
            t0 = time.perf_counter()
            resp = runner.query("search_for_pattern", params)
            elapsed = (time.perf_counter() - t0) * 1000
            times.append(elapsed)
            toks.append(token_count(resp))
            sample = resp[:280]
        return ToolBenchResult(
            tool="serena",
            ok=True,
            median_ms=statistics.median(times),
            p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
            median_tokens=int(statistics.median(toks)),
            runs=iterations,
            query=query,
            input=json.dumps(
                {"tool_name": "search_for_pattern", "params": params},
                ensure_ascii=False,
            ),
            sample=sample,
            output=resp,
        )
    finally:
        runner.stop()


def bench_codegraph(repo_root: Path, query: str, iterations: int) -> ToolBenchResult:
    init = run_cmd(["codegraph", "init", "-i", str(repo_root)], timeout=1800)
    if init.returncode != 0:
        raise RuntimeError(init.stderr[:1200] or init.stdout[:1200])
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        proc = run_cmd(["codegraph", "query", "-p", str(repo_root), "-l", "20", "-j", query], timeout=300)
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        payload = proc.stdout
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="codegraph",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        query=query,
        input=json.dumps(
            {"command": ["codegraph", "query", "-p", str(repo_root), "-l", "20", "-j", query]},
            ensure_ascii=False,
        ),
        sample=sample,
        output=payload,
    )


def bench_code_index(
    repo_root: Path, workspace_root: Path, code_index_repo: Path, query: str, iterations: int
) -> ToolBenchResult:
    runner = CodeIndexRunner(repo_root=repo_root, workspace_root=workspace_root, code_index_repo=code_index_repo)
    runner.start()
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    try:
        for _ in range(iterations):
            t0 = time.perf_counter()
            resp = runner.query(query)
            elapsed = (time.perf_counter() - t0) * 1000
            payload = json.dumps(resp, ensure_ascii=False)
            times.append(elapsed)
            toks.append(token_count(payload))
            sample = payload[:280]
    finally:
        runner.stop()
    return ToolBenchResult(
        tool="code-index-mcp",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        query=query,
        input=json.dumps(
            {
                "pattern": query,
                "regex": False,
                "file_pattern": "*.py",
                "max_results": 50,
                "context_lines": 0,
                "case_sensitive": False,
            },
            ensure_ascii=False,
        ),
        sample=sample,
        output=payload,
    )


def bench_ccc(repo_root: Path, workspace_root: Path, query: str, iterations: int) -> ToolBenchResult:
    tool_workspace = external_workspace_root(workspace_root)
    snapshot_root = prepare_repo_snapshot(repo_root, tool_workspace, "ccc-repo")
    init = run_cmd(["ccc", "init", "--force"], cwd=snapshot_root, timeout=300)
    if init.returncode != 0:
        raise RuntimeError(init.stderr[:1200] or init.stdout[:1200])
    index = run_cmd(["ccc", "index"], cwd=snapshot_root, timeout=1800)
    if index.returncode != 0:
        raise RuntimeError(index.stderr[:1200] or index.stdout[:1200])
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        proc = run_cmd(
            ["ccc", "search", "--path", "src/**/*.py", "--limit", "20", query],
            cwd=snapshot_root,
            timeout=300,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        payload = proc.stdout
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="cocoindex-code",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        query=query,
        input=json.dumps(
            {"command": ["ccc", "search", "--path", "src/**/*.py", "--limit", "20", query]},
            ensure_ascii=False,
        ),
        sample=sample,
        output=payload,
    )


def bench_jcodemunch(repo_root: Path, iterations: int) -> ToolBenchResult:
    idx = run_cmd(["jcodemunch-mcp", "index", str(repo_root), "--no-ai-summaries"], timeout=1800)
    if idx.returncode != 0:
        raise RuntimeError(idx.stderr[:1200] or idx.stdout[:1200])
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        proc = run_cmd(["jcodemunch-mcp", "digest", str(repo_root), "--json"], timeout=300)
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        payload = proc.stdout
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="jcodemunch-mcp",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        query="digest",
        input=json.dumps(
            {"command": ["jcodemunch-mcp", "digest", str(repo_root), "--json"]},
            ensure_ascii=False,
        ),
        sample=sample,
        output=payload,
    )


def run_external_benchmarks(
    *,
    repo_root: Path,
    workspace_root: Path,
    code_index_repo: Path,
    query: str,
    iterations: int,
) -> list[ToolBenchResult]:
    results: list[ToolBenchResult] = []
    benches: list[tuple[str, Callable[[], ToolBenchResult]]] = [
        ("lemoncrow", lambda: bench_lemoncrow(repo_root, workspace_root, query, iterations)),
        (
            "lemoncrow-zoekt",
            lambda: bench_lemoncrow_zoekt(repo_root, workspace_root, query, iterations),
        ),
        ("serena", lambda: bench_serena(repo_root, workspace_root, query, iterations)),
        ("codegraph", lambda: bench_codegraph(repo_root, query, iterations)),
        (
            "code-index-mcp",
            lambda: bench_code_index(repo_root, workspace_root, code_index_repo, query, iterations),
        ),
        ("cocoindex-code", lambda: bench_ccc(repo_root, workspace_root, query, iterations)),
        ("jcodemunch-mcp", lambda: bench_jcodemunch(repo_root, iterations)),
    ]
    for name, fn in benches:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(
                ToolBenchResult(
                    tool=name,
                    ok=False,
                    median_ms=0.0,
                    p95_ms=0.0,
                    median_tokens=0,
                    runs=0,
                    query=query if name != "jcodemunch-mcp" else "digest",
                    error=str(exc),
                )
            )
    return results


def render_table(results: list[ToolBenchResult]) -> str:
    lines = [
        "| Tool | Status | Median ms | P95 ms | Median tokens | Runs |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in results:
        status = "ok" if r.ok else "failed"
        lines.append(f"| {r.tool} | {status} | {r.median_ms:.1f} | {r.p95_ms:.1f} | {r.median_tokens} | {r.runs} |")
    return "\n".join(lines)


def write_csv(results: list[ToolBenchResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "query",
                "tool",
                "status",
                "median_ms",
                "p95_ms",
                "median_tokens",
                "runs",
                "error",
                "input",
                "output",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "query": result.query,
                    "tool": result.tool,
                    "status": "ok" if result.ok else "failed",
                    "median_ms": result.median_ms,
                    "p95_ms": result.p95_ms,
                    "median_tokens": result.median_tokens,
                    "runs": result.runs,
                    "error": result.error,
                    "input": result.input,
                    "output": result.output,
                }
            )


def main() -> None:
    repo_default = Path(__file__).resolve().parents[2]
    workspace_default = default_benchmark_root(repo_default)
    parser = argparse.ArgumentParser(description="External code-indexer benchmark runner")
    parser.add_argument("--repo-root", default=str(repo_default))
    parser.add_argument("--workspace-root", default=str(workspace_default))
    parser.add_argument("--code-index-repo", default="")
    parser.add_argument("--query", default="classify_command")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--csv-out", default="")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    code_index_repo = (
        Path(args.code_index_repo).resolve() if args.code_index_repo else (workspace_root / "code-index-mcp").resolve()
    )
    ensure_workspace(workspace_root)

    if args.install:
        install_external_tools(workspace_root)

    results = run_external_benchmarks(
        repo_root=repo_root,
        workspace_root=workspace_root,
        code_index_repo=code_index_repo,
        query=args.query,
        iterations=args.iterations,
    )

    print(render_table(results))
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8")
    if args.csv_out:
        write_csv(results, Path(args.csv_out))


if __name__ == "__main__":
    main()
