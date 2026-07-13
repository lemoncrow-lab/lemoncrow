"""Retrieval provider MRR benchmark -- every provider, LemonCrow included, over
the same stdio/CLI surface. No provider gets in-process shortcuts.

Providers: lemoncrow / ctags / ast-grep / serena / code-index-mcp / jcodemunch /
cg / rg / cmm / fff. Same gold set and output JSON format as the retired
fitness_explore_mrr.py; history + delta reporting live here now.

Run via:
    uv run python benchmarks/codebench/eval_external_provider_mrr.py --provider lemoncrow
    uv run python benchmarks/codebench/eval_external_provider_mrr.py --provider ctags
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, cast

# ---------------------------------------------------------------------------
# Minimal JSON-RPC line client (used by JCodeMunchProvider)
# ---------------------------------------------------------------------------


class _JsonRpcLineClient:
    def __init__(self, command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        # Bounded tail of the child's stderr, kept drained by a background
        # thread (started in start()). Draining is mandatory, not cosmetic:
        # the OS stderr pipe buffer is only ~64KB, and the child -- plus any
        # subprocess that inherits its stderr (ast-grep/zoekt/`lc code
        # index` warm) and Python's root-logger lastResort handler -- BLOCKS
        # on write() once it fills. Because only stdout is read on the hot
        # path, an undrained stderr pipe wedges the server mid-write: it stops
        # answering, every subsequent query hits the read timeout, and the run
        # appears to "get stuck" (fast -> ~0.2 q/s) until a restart hands it a
        # fresh empty pipe -- which then refills and stalls again.
        self._stderr_tail: deque[str] = deque(maxlen=400)
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.command,
            cwd=str(self.cwd) if self.cwd else None,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Continuously drain stderr so the child never blocks on a full pipe.
        self._stderr_tail.clear()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True)
        self._stderr_thread.start()
        self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "lemoncrow-bench", "version": "1"},
                "capabilities": {},
            },
        )
        self.notify("notifications/initialized", {})

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        """Pump the child's stderr into a bounded ring buffer.

        Runs on a daemon thread for the life of the process. Reading line by
        line keeps the OS pipe empty so the child never blocks writing to it;
        the ring buffer retains just the recent tail for timeout diagnostics.
        """
        stream = proc.stderr
        if stream is None:
            return
        with contextlib.suppress(Exception):
            for line in stream:
                self._stderr_tail.append(line)

    def _read_message(self, *, timeout: float) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        proc = self.proc
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            with contextlib.suppress(Exception):
                proc.kill()

        timer = threading.Timer(timeout, _kill_on_timeout)
        timer.start()
        try:
            line = proc.stdout.readline()
        finally:
            timer.cancel()
        if timed_out.is_set() or not line:
            # Recent stderr comes from the drain thread's ring buffer -- reading
            # proc.stderr directly here would race that thread for the pipe.
            stderr = ""
            with contextlib.suppress(Exception):
                stderr = "".join(list(self._stderr_tail))
            raise TimeoutError(f"timed out waiting for JSON-RPC response: {stderr[-400:]}")
        return cast(dict[str, Any], json.loads(line))

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False) + "\n"
        )
        self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any], *, timeout: float = 60) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdin is not None
        request_id = self._next_id
        self._next_id += 1
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, ensure_ascii=False)
            + "\n"
        )
        self.proc.stdin.flush()
        while True:
            message = self._read_message(timeout=timeout)
            if message.get("id") != request_id:
                continue
            return message

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=6)
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=6)
        # Close the pipes explicitly here, inside a caught scope -- otherwise
        # they're finalized later by the GC when the last reference to `proc`
        # drops (e.g. `self._client = None` in a caller), and a flush against
        # an already-dead process prints an unsuppressable "Exception ignored
        # in ..." BrokenPipeError instead of being handled.
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
        # The daemon drain thread exits on stderr EOF (the close above); join
        # briefly so a restarted client doesn't leak a thread per recovery.
        thread, self._stderr_thread = self._stderr_thread, None
        if thread is not None:
            thread.join(timeout=1.0)


sys.path.insert(0, "src")
sys.path.insert(0, ".")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser(description="External provider MRR benchmark")
_parser.add_argument(
    "--provider",
    required=True,
    choices=[
        "lemoncrow",
        "ctags",
        "ast-grep",
        "serena",
        "code-index-mcp",
        "jcodemunch",
        "cg",
        "rg",
        "cmm",
        "fff",
        "ccc",
    ],
)
_parser.add_argument("--full", action="store_true")
_parser.add_argument("--sample", type=int, default=None)
_parser.add_argument("--repo", default=os.environ.get("FITNESS_REPO", ""))
_parser.add_argument(
    "--workers",
    type=int,
    default=int(os.environ.get("EVAL_WORKERS", "1")),
    help="Parallel repo workers (1 = sequential). Each worker spawns its own provider instance "
    "so repos with independent start/stop (lemoncrow, rg, cmm, fff, ctags, ast-grep, jcodemunch) "
    "benefit. CgProvider/SerenaProvider use a shared MCP server class-level singleton and will "
    "race connection state with >1 worker — use --workers 1 for those.",
)
_args, _ = _parser.parse_known_args()

PROVIDER = _args.provider
# Channel label: the CLI runs LemonCrow channel variants (lexical / lexical+zoekt /
# lexical+zoekt+semantic) as env toggles on the same provider; the label keeps
# their history and tags distinguishable.
_LABEL = os.environ.get("EVAL_CHANNEL_LABEL", PROVIDER)
_TAG = f"[ext:{_LABEL}]"  # per-channel tag so parallel runs don't interleave identically
FULL = _args.full
SAMPLE = _args.sample
REPO_FILTER = _args.repo

# ---------------------------------------------------------------------------
# Gold loading
# ---------------------------------------------------------------------------
FITNESS_PAIRS = os.environ.get(
    "FITNESS_PAIRS",
    "benchmarks/codebench/data/bench_pairs_def_gold.json",
)
_gold_paths = [Path(p.strip()) for p in FITNESS_PAIRS.split(",") if p.strip()]

_golds: list[tuple[str, list, dict]] = []  # [(kind, pairs, true_map)]
_all_repos: dict[str, dict] = {}  # prefix -> {ws, db, ...}

for _gp in _gold_paths:
    _raw = json.loads(_gp.read_text())
    _kind = _raw.get("gold_kind", "definition")
    _golds.append((_kind, _raw["pairs"], _raw["true_map"]))
    for _prefix, _meta in _raw.get("repos", {}).items():
        if _prefix not in _all_repos:
            _all_repos[_prefix] = _meta

# Build (query, prefix) -> {kind: tid} lookup for scoring
_q_to_tids: dict[tuple[str, str], dict[str, str]] = {}
for _kind, _pairs, _tm in _golds:
    for _q, _tid, _prefix in _pairs:
        _key = (_q, _prefix)
        if _key not in _q_to_tids:
            _q_to_tids[_key] = {}
        _q_to_tids[_key][_kind] = _tid

# Union of unique (query, prefix) pairs, interleaved by gold kind so sampled
# runs cover every supplied gold file instead of exhausting the first file.
_union: list[tuple[str, str]] = []  # [(query, prefix)]
_seen: set[tuple[str, str]] = set()
_by_gold: list[list[tuple[str, str]]] = []
for _, _pairs, _ in _golds:
    _items: list[tuple[str, str]] = []
    for _q, _tid, _prefix in _pairs:
        key = (_q, _prefix)
        if key not in _seen:
            _seen.add(key)
            _items.append(key)
    if _items:
        _by_gold.append(_items)
for _idx in range(max((len(items) for items in _by_gold), default=0)):
    for _items in _by_gold:
        if _idx < len(_items):
            _union.append(_items[_idx])

if REPO_FILTER:
    _union = [(q, p) for q, p in _union if REPO_FILTER in p]
    _all_repos = {k: v for k, v in _all_repos.items() if REPO_FILTER in k}

# Sample
_total_available_queries = len(_union)
if not FULL:
    target = SAMPLE if SAMPLE else 500
    _by_repo: dict[str, list] = defaultdict(list)
    for item in _union:
        _by_repo[item[1]].append(item)
    per_repo = max(1, target // max(len(_by_repo), 1))
    _union = [x for items in _by_repo.values() for x in items[:per_repo]]
    print(
        f"{_TAG} sampled {len(_union)} queries (target={target}, available={_total_available_queries})",
        file=sys.stderr,
    )

# Group by repo
_queries_by_repo: dict[str, list[str]] = defaultdict(list)
_seen_qr: set[tuple[str, str]] = set()
for q, prefix in _union:
    if (q, prefix) not in _seen_qr:
        _seen_qr.add((q, prefix))
        _queries_by_repo[prefix].append(q)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int((p / 100.0) * (len(s) - 1)))]


def _lat_summary(lats: list[float]) -> dict:
    if not lats:
        return {"mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0, "over_100ms": 0}
    return {
        "mean": round(sum(lats) / len(lats), 1),
        "p50": round(_pct(lats, 50), 1),
        "p95": round(_pct(lats, 95), 1),
        "max": round(max(lats), 1),
        "over_100ms": sum(1 for x in lats if x > 100),
    }


def _rel(path_str: str, ws: Path) -> str:
    """Normalize a path to be relative to ws (or strip leading ./ for already-relative paths)."""
    p = path_str.replace("\\", "/")
    ws_str = str(ws).replace("\\", "/").rstrip("/") + "/"
    if p.startswith(ws_str):
        return p[len(ws_str) :]
    try:
        return str(Path(path_str).relative_to(ws)).replace("\\", "/")
    except ValueError:
        # Already relative: normalize away leading ./ (.\)
        return str(Path(p)).replace("\\", "/")


_PY_KEYWORDS = frozenset(
    {
        "def",
        "class",
        "import",
        "from",
        "return",
        "if",
        "else",
        "elif",
        "for",
        "while",
        "with",
        "as",
        "try",
        "except",
        "finally",
        "raise",
        "yield",
        "async",
        "await",
        "lambda",
        "pass",
        "break",
        "continue",
    }
)


def _sym(query: str) -> str:
    """Extract the best single symbol token from a raw query string."""
    tokens = [t for t in re.split(r"[\s|,()\[\]]+", query.strip()) if t]
    # Skip leading Python keywords (e.g. "def foo" -> "foo")
    for tok in tokens:
        if tok not in _PY_KEYWORDS:
            return tok
    return tokens[0] if tokens else query


def _extract_paths_text(text: str, ws: Path) -> list[str]:
    """Extract file paths from free-form text, normalized relative to ws."""
    ws_str = str(ws).rstrip("/") + "/"
    seen: set[str] = set()
    result: list[str] = []
    for m in re.finditer(re.escape(ws_str) + r"[^\s'\">,;]+", text):
        rel = m.group()[len(ws_str) :]
        if rel not in seen:
            seen.add(rel)
            result.append(rel)
    # Also catch bare relative paths (tab-sep output like readtags)
    for m in re.finditer(r"(?<![/\w])[\w][\w/.-]+\.[a-zA-Z]{1,5}(?![/\w])", text):
        p = m.group()
        if p not in seen and not p.startswith("/"):
            seen.add(p)
            result.append(p)
    return result


def _rank(ranked_files: list[str], gold_files: list[str]) -> int | None:
    """Return 1-based rank of first gold file, or None."""
    norm_gold = {g.replace("\\", "/") for g in gold_files if g}
    for i, f in enumerate(ranked_files, 1):
        if f.replace("\\", "/") in norm_gold:
            return i
    return None


# Language name map: extension -> (ast-grep lang, generic lang)
_EXT_LANG: list[tuple[str, str]] = [
    ("*.c", "c"),
    ("*.h", "c"),
    ("*.py", "python"),
    ("*.ts", "typescript"),
    ("*.tsx", "tsx"),
    ("*.js", "javascript"),
    ("*.jsx", "jsx"),
    ("*.rs", "rust"),
    ("*.go", "go"),
    ("*.java", "java"),
    ("*.cpp", "cpp"),
    ("*.cc", "cpp"),
    ("*.cxx", "cpp"),
    ("*.rb", "ruby"),
]

# Serena doesn't support 'c' — map generic lang names to Serena-supported ones.
_SERENA_LANG_MAP: dict[str, str] = {
    "c": "cpp",  # closest supported; serena supports cpp / cpp_ccls
    "tsx": "typescript",
    "jsx": "javascript",
}


def _ctags_exclude_args(ws: Path) -> list[str]:
    """Build ctags --exclude flags from .gitignore + standard ignores."""
    args = [
        "--exclude=.git",
        "--exclude=.venv",
        "--exclude=__pycache__",
        "--exclude=node_modules",
    ]
    gitignore = ws / ".gitignore"
    if gitignore.exists():
        args.append(f"--exclude=@{gitignore}")
    return args


def _dominant_lang(ws: Path) -> str:
    """Fast heuristic: first extension with a match at the top two levels."""
    for pat, lang in _EXT_LANG:
        # next() short-circuits — stops at first hit even in huge trees
        if next(ws.glob(pat), None) is not None:
            return lang
        if next(ws.glob(f"*/{pat}"), None) is not None:
            return lang
    return "python"  # safe fallback


# ---------------------------------------------------------------------------
# Provider base
# ---------------------------------------------------------------------------


class Provider:
    """Base class: override start/stop/search_symbol/search_text."""

    name: str = ""
    # None means the provider claims the generic search surface can attempt every
    # loaded gold kind. Specialized providers override this so unsupported golds
    # do not pollute the supported-only aggregate.
    supported_gold_kinds: frozenset[str] | None = None

    def start(self, ws: Path) -> None:
        pass

    def stop(self) -> None:
        pass

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        """Return ranked file paths relative to ws for a symbol-definition lookup."""
        return []

    def search_text(self, query: str, ws: Path) -> list[str]:
        """Return ranked file paths relative to ws for a text-content search."""
        return []


# ---------------------------------------------------------------------------
# ctags
# ---------------------------------------------------------------------------


class CtagsProvider(Provider):
    name = "ctags"
    supported_gold_kinds = frozenset({"definition"})

    def __init__(self) -> None:
        self._tags_db: Path | None = None
        self._ws: Path | None = None
        self._ctags: Path | None = None
        self._readtags: Path | None = None

    def start(self, ws: Path) -> None:
        from benchmarks.mcp_tools.bench_external_indexers import ensure_universal_ctags

        self._ctags, self._readtags = ensure_universal_ctags()
        self._ws = ws
        fd, tmp = tempfile.mkstemp(suffix=".tags")
        os.close(fd)
        self._tags_db = Path(tmp)
        # Use git ls-files to get only tracked files — respects all nested
        # .gitignore files recursively, avoids indexing .venv / build dirs.
        ls = subprocess.run(
            ["git", "ls-files"],
            cwd=ws,
            capture_output=True,
            timeout=30,
        )
        if ls.returncode == 0 and ls.stdout.strip():
            fd2, flist = tempfile.mkstemp(suffix=".lst")
            os.close(fd2)
            Path(flist).write_bytes(ls.stdout)
            cmd = [
                str(self._ctags),
                "--fields=+nKsS",
                "-f",
                str(self._tags_db),
                "-L",
                flist,
            ]
        else:
            # Not a git repo — fall back to recursive with exclusions
            cmd = [
                str(self._ctags),
                "-R",
                "--fields=+nKsS",
                "-f",
                str(self._tags_db),
                *_ctags_exclude_args(ws),
                ".",
            ]
            flist = None
        try:
            proc = subprocess.run(cmd, cwd=ws, capture_output=True, timeout=600)
        finally:
            if flist:
                Path(flist).unlink(missing_ok=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode()[:800])

    def stop(self) -> None:
        if self._tags_db and self._tags_db.exists():
            self._tags_db.unlink(missing_ok=True)

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        if not self._readtags or not self._tags_db:
            return []
        sym = _sym(query)
        proc = subprocess.run(
            [str(self._readtags), "-t", str(self._tags_db), "-e", sym],
            capture_output=True,
            text=True,
            timeout=30,
        )
        seen: set[str] = set()
        paths: list[str] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                p = parts[1].replace("\\", "/")
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
        return paths

    def search_text(self, query: str, ws: Path) -> list[str]:
        # ctags has no text/content search
        return []


# ---------------------------------------------------------------------------
# ast-grep
# ---------------------------------------------------------------------------


class AstGrepProvider(Provider):
    name = "ast-grep"

    # Resolved once at class level to avoid per-call subprocess overhead.
    _AST_GREP_BIN: str = ""

    @classmethod
    def _resolve_bin(cls) -> str:
        if cls._AST_GREP_BIN:
            return cls._AST_GREP_BIN
        # Prefer the project-local binary; fall back to npx.
        local = Path(".lemoncrow/bin/ast-grep")
        for candidate in local.rglob("ast-grep"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                cls._AST_GREP_BIN = str(candidate)
                return cls._AST_GREP_BIN
        # npx fallback (slower but always correct)
        cls._AST_GREP_BIN = "__npx__"
        return cls._AST_GREP_BIN

    def _run(self, pattern: str, ws: Path) -> list[str]:
        lang = _dominant_lang(ws)
        bin_path = self._resolve_bin()
        if bin_path == "__npx__":
            cmd = [
                "npx",
                "--yes",
                "-p",
                "@ast-grep/cli",
                "sg",
                "run",
                "--pattern",
                pattern,
                "--lang",
                lang,
                "--json",
                str(ws),
            ]
        else:
            cmd = [bin_path, "run", "--pattern", pattern, "--lang", lang, "--json", str(ws)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode > 1 or (proc.returncode == 1 and not (proc.stdout or "").strip().startswith("[")):
            return []
        seen: set[str] = set()
        result: list[str] = []
        try:
            items = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return []
        for item in items:
            p = _rel(str(item.get("file", "")), ws)
            if p and p not in seen:
                seen.add(p)
                result.append(p)
        return result

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        return self._run(_sym(query), ws)

    def search_text(self, query: str, ws: Path) -> list[str]:
        return self._run(query, ws)


# ---------------------------------------------------------------------------
# serena
# ---------------------------------------------------------------------------


# Untimed warm-up ceiling for SerenaProvider.start() -- see the comment at its
# call sites. Generously above the worst observed real-world LSP index time
# (linux: ~480s) without being unbounded; a genuinely hung server still fails
# fast enough not to stall the whole benchmark run indefinitely.
_WARMUP_TIMEOUT_S = 900


class _SerenaMCPClient:
    """Non-destructive MCP stdio client for Serena.

    Unlike ``_JsonRpcLineClient`` this client **never kills** the subprocess on
    timeout — it simply raises ``TimeoutError`` so the caller can decide whether
    to retry or skip.  This is essential because Serena's first ``find_symbol``
    call on a cold project can take 60-120s to start the LSP server.
    """

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None) -> None:
        self.command = command
        self.env = env
        self.proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self.env,
        )
        # MCP handshake
        self._send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "lemoncrow-bench", "version": "1"},
                    "capabilities": {},
                },
            }
        )
        self._recv(timeout=30)  # initialize response
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, msg: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self, *, timeout: float) -> dict:
        """Read one JSON-RPC line from stdout with a wall-clock timeout.

        Does **not** kill the subprocess if the timeout fires — only raises.
        """
        assert self.proc is not None and self.proc.stdout is not None
        # Use select/poll on Unix to avoid killing the process
        import select

        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError(f"no response from serena MCP server after {timeout:.0f}s")
        line = self.proc.stdout.readline()
        if not line:
            raise BrokenPipeError("serena MCP server closed stdout")
        msg = json.loads(line)
        assert isinstance(msg, dict)
        return msg

    def call(self, method: str, params: dict, *, timeout: float = 300) -> dict:
        """Send a JSON-RPC request and wait for the matching response."""
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"serena MCP call '{method}' timed out after {timeout:.0f}s")
                try:
                    msg = self._recv(timeout=remaining)
                except TimeoutError:
                    raise
                if isinstance(msg, dict) and msg.get("id") == req_id:
                    return msg
                # Skip other messages (notifications, responses to other requests)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=6)
        # See _JsonRpcLineClient.stop(): close pipes here (caught) instead of
        # leaving them for GC finalization to raise an unsuppressable
        # BrokenPipeError against the already-dead process.
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()


class SerenaProvider(Provider):
    """Persistent MCP-server-based provider for Serena.

    Class-level state shares a single ``serena start-mcp-server --transport stdio``
    process across all repos in a benchmark run, avoiding the per-repo
    ``serena init`` + ``project create`` + server start/stop overhead.

    Projects that already have a ``.serena/`` directory are reused as-is; only
    missing projects are created on the fly.
    """

    name = "serena"

    # -- Class-level persistent state (shared across all repos) ---------------
    _mcp: _SerenaMCPClient | None = None
    _serena_home: Path | None = None
    _setup_done: bool = False

    # -- Per-instance state ---------------------------------------------------
    def __init__(self) -> None:
        self._lang: str = "python"

    # -- Global setup (once per script invocation) ----------------------------

    @classmethod
    def _global_init(cls) -> None:
        if cls._setup_done:
            return
        cls._serena_home = Path(tempfile.mkdtemp(prefix="serena-bench-"))
        env = {**os.environ, "HOME": str(cls._serena_home)}
        proc = subprocess.run(
            ["serena", "init", "-b", "LSP"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"serena global init failed: {(proc.stderr or proc.stdout)[:800]}")
        cls._setup_done = True

    @classmethod
    def _ensure_mcp(cls) -> _SerenaMCPClient:
        if cls._mcp is not None:
            return cls._mcp
        cls._global_init()
        env = {**os.environ, "HOME": str(cls._serena_home)}
        mcp = _SerenaMCPClient(
            ["serena", "start-mcp-server", "--transport", "stdio"],
            env=env,
        )
        mcp.start()
        cls._mcp = mcp
        import atexit

        atexit.register(cls._global_cleanup)
        return mcp

    @classmethod
    def _global_cleanup(cls) -> None:
        if cls._mcp is not None:
            with contextlib.suppress(Exception):
                cls._mcp.stop()
            cls._mcp = None
        if cls._serena_home is not None and cls._serena_home.exists():
            import shutil

            shutil.rmtree(cls._serena_home, ignore_errors=True)
            cls._serena_home = None

    # -- Per-repo lifecycle ---------------------------------------------------

    def start(self, ws: Path) -> None:
        """Activate the serena project for *ws* via the shared MCP server.

        Creates the project first if no ``.serena/`` directory exists yet.
        """
        self._lang = _SERENA_LANG_MAP.get(_dominant_lang(ws), _dominant_lang(ws))
        mcp = self._ensure_mcp()

        # Lazily create the project when the workspace has no .serena/ dir.
        if not (ws / ".serena" / "project.yml").exists():
            env = {**os.environ, "HOME": str(self._serena_home)}
            subprocess.run(
                [
                    "serena",
                    "project",
                    "create",
                    str(ws),
                    "--name",
                    f"bench-{ws.name}",
                    "--language",
                    self._lang,
                ],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )

        # Activate the project through the MCP server so subsequent tool
        # calls (find_symbol / search_for_pattern) target this repo.
        result = mcp.call(
            "tools/call",
            {"name": "activate_project", "arguments": {"project": str(ws)}},
            timeout=180,
        )
        if result.get("result", {}).get("isError"):
            err_text = result.get("result", {}).get("content", [{}])[0].get("text", repr(result))
            raise RuntimeError(f"serena activate_project failed: {err_text}")

        # Untimed warm-up: activate_project returns before the language server
        # has indexed the project, so without this the FIRST TIMED query pays
        # the whole LSP index (240s+ on large repos). One symbol lookup + one
        # pattern search block until the server is actually ready.
        #
        # _WARMUP_TIMEOUT_S is deliberately much larger than _call_tool's normal
        # 300s default: this call is untimed (off the measured critical path),
        # so there is no reason to cap it tightly. Confirmed via a real run this
        # mattered -- linux's LSP index took ~480s, longer than the old 300s
        # warm-up timeout. When that fires, _call_tool's own try/except (plus
        # this contextlib.suppress) swallows the TimeoutError and start()
        # returns as if ready, but _SerenaMCPClient never kills the subprocess
        # on timeout (by design, see its docstring) -- so the LSP server keeps
        # indexing in the background regardless. The FIRST REAL, TIMED query
        # then has to wait for that abandoned warm-up request's response to
        # finally arrive (its id no longer matches anything _recv is waiting
        # on, so it's silently skipped) before its own response can be read off
        # the same stdout stream -- so the entire remaining index time lands on
        # that one timed query instead of on this untimed warm-up. This is
        # exactly the shape of linux's serena p100 outlier (480011ms).
        with contextlib.suppress(Exception):
            self._call_tool(
                "find_symbol",
                {"name_path_pattern": "main", "substring_matching": True, "max_matches": 1, "include_body": False},
                timeout=_WARMUP_TIMEOUT_S,
            )
        with contextlib.suppress(Exception):
            self._call_tool(
                "search_for_pattern",
                {"substring_pattern": "def ", "restrict_search_to_code_files": True, "max_answer_chars": 2000},
                timeout=_WARMUP_TIMEOUT_S,
            )

    def stop(self) -> None:
        """No per-repo teardown — the shared MCP server stays alive."""
        pass

    # -- Tool calls via MCP ---------------------------------------------------

    def _call_tool(self, name: str, args: dict[str, object], *, timeout: float = 300) -> str:
        mcp = self._ensure_mcp()
        try:
            result = mcp.call("tools/call", {"name": name, "arguments": args}, timeout=timeout)
        except Exception:
            return ""
        content = result.get("result", {}).get("content", [])
        if result.get("result", {}).get("isError"):
            return ""
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(texts)

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        resp = self._call_tool(
            "find_symbol",
            {
                "name_path_pattern": _sym(query),
                "substring_matching": True,
                "max_matches": 20,
                "include_body": False,
            },
        )
        return _extract_paths_text(resp, ws)

    def search_text(self, query: str, ws: Path) -> list[str]:
        resp = self._call_tool(
            "search_for_pattern",
            {"substring_pattern": query, "restrict_search_to_code_files": True},
        )
        return _extract_paths_text(resp, ws)


# ---------------------------------------------------------------------------
# code-index-mcp
# ---------------------------------------------------------------------------


class CodeIndexProvider(Provider):
    name = "code-index-mcp"

    def __init__(self) -> None:
        self._runner: Any = None
        self._ws: Path | None = None

    def start(self, ws: Path) -> None:
        from benchmarks.mcp_tools.bench_external_indexers import (
            CodeIndexRunner,
            bench_tools_root,
            ensure_code_index_checkout,
            ensure_code_index_runtime,
        )

        self._ws = ws
        # Single shared checkout under ~/.lemoncrow/_bench_tools/ so we don't clone
        # a fresh copy for every gold repo workspace, and so python_bin is always
        # an absolute Path (no cwd-relative confusion in run_cmd).
        code_index_repo = ensure_code_index_checkout(bench_tools_root() / "code-index-mcp")
        # Pre-warm the venv before creating the runner so any uv sync failure
        # surfaces during start() rather than inside the subprocess -- and so
        # python_bin is always an absolute Path (no cwd-relative confusion).
        python_bin = ensure_code_index_runtime(code_index_repo)
        tmp_ws = Path(tempfile.mkdtemp(prefix="cidx-ws-"))
        self._runner = CodeIndexRunner(
            repo_root=ws,
            workspace_root=tmp_ws,
            code_index_repo=code_index_repo,
        )
        self._runner.start(python_bin=python_bin)

    def stop(self) -> None:
        if self._runner is not None:
            with contextlib.suppress(Exception):
                self._runner.stop()
        self._runner = None

    def _paths_from_result(self, result: dict, ws: Path) -> list[str]:
        seen: set[str] = set()
        paths: list[str] = []
        for item in result.get("results", []) or []:
            for key in ("file", "path", "file_path"):
                raw = item.get(key)
                if raw:
                    p = _rel(str(raw), ws)
                    if p not in seen:
                        seen.add(p)
                        paths.append(p)
                    break
        if not paths:
            paths = _extract_paths_text(json.dumps(result), ws)
        return paths

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        if not self._runner:
            return []
        try:
            result = self._runner.query(_sym(query), file_pattern="*")
        except Exception:
            return []
        return self._paths_from_result(result, ws)

    def search_text(self, query: str, ws: Path) -> list[str]:
        if not self._runner:
            return []
        try:
            result = self._runner.query(query, file_pattern="*")
        except Exception:
            return []
        return self._paths_from_result(result, ws)


# ---------------------------------------------------------------------------
# jcodemunch
# ---------------------------------------------------------------------------


class JCodeMunchProvider(Provider):
    name = "jcodemunch"

    def __init__(self) -> None:
        self._client: Any = None
        self._repo_id: str | None = None
        self._ws: Path | None = None

    def _tool_call(self, name: str, args: dict) -> dict:
        assert self._client
        import json as _json

        response = self._client.call("tools/call", {"name": name, "arguments": args}, timeout=120)
        result = response.get("result", {})
        if result.get("isError"):
            raise RuntimeError(_json.dumps(result))
        return result

    def _content_text_json(self, result: dict) -> dict:
        content = result.get("content", [])
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "{}")
            return json.loads(text)
        return {}

    def start(self, ws: Path) -> None:
        from benchmarks.mcp_tools.bench_external_indexers import run_cmd

        self._ws = ws
        # Index the repo
        idx = run_cmd(
            ["jcodemunch-mcp", "index", str(ws), "--no-ai-summaries"],
            timeout=1800,
        )
        if idx.returncode != 0:
            raise RuntimeError(idx.stderr[:800] or idx.stdout[:800])
        self._client = _JsonRpcLineClient(["jcodemunch-mcp", "serve"])
        self._client.start()
        repo_result = self._tool_call("resolve_repo", {"path": str(ws)})
        payload = self._content_text_json(repo_result)
        self._repo_id = str(payload["repo"])

    def stop(self) -> None:
        if self._client:
            self._client.stop()
            self._client = None

    def _paths_from_result(self, result: dict, ws: Path) -> list[str]:
        text = json.dumps(result)
        return _extract_paths_text(text, ws)

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        if not self._client or not self._repo_id:
            return []
        try:
            result = self._tool_call(
                "search_symbols",
                {
                    "repo": self._repo_id,
                    "query": _sym(query),
                    "language": _dominant_lang(self._ws) if self._ws else "python",
                    "max_results": 20,
                    "detail_level": "compact",
                    "fuzzy": False,
                },
            )
        except Exception:
            return []
        return self._paths_from_result(result, ws)

    def search_text(self, query: str, ws: Path) -> list[str]:
        if not self._client or not self._repo_id:
            return []
        try:
            result = self._tool_call(
                "search_text",
                {"repo": self._repo_id, "query": query, "max_results": 20, "context_lines": 0},
            )
        except Exception:
            return []
        return self._paths_from_result(result, ws)


# ---------------------------------------------------------------------------
# rg — bare ripgrep, no ranking (baseline for text search)
# ---------------------------------------------------------------------------


class RgProvider(Provider):
    name = "rg"

    def start(self, ws: Path) -> None:
        pass  # stateless

    def stop(self) -> None:
        pass

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        return self._rg(query, ws)

    def search_text(self, query: str, ws: Path) -> list[str]:
        return self._rg(query, ws)

    def _rg(self, query: str, ws: Path) -> list[str]:
        try:
            proc = subprocess.run(
                [
                    "rg",
                    "--files-with-matches",
                    "-l",
                    "--no-heading",
                    "--iglob",
                    "!.git",
                    "--iglob",
                    "!.venv",
                    "--iglob",
                    "!node_modules",
                    "--iglob",
                    "!__pycache__",
                    # Use rg's default regex mode — our queries are grep patterns.
                    # Falls back gracefully: rg exits 1 (no match) on bad patterns.
                    query,
                    str(ws),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return []
        if proc.returncode > 1:
            # returncode 1 = no matches (normal); >1 = error
            return []
        seen: set[str] = set()
        result: list[str] = []
        for line in proc.stdout.splitlines():
            p = _rel(line.strip(), ws)
            if p and p not in seen:
                seen.add(p)
                result.append(p)
        return result


class CgProvider(Provider):
    """Persistent ``codegraph serve --mcp`` shared across all repos.

    The one-shot ``codegraph query`` CLI pays ~110ms of node startup + db
    open per call vs ~2ms for the same search over MCP, and
    ``codegraph_search`` takes ``projectPath`` per call, so a single server
    covers every gold repo (same pattern as SerenaProvider).
    """

    name = "cg"
    supported_gold_kinds = frozenset({"definition"})

    _mcp: _JsonRpcLineClient | None = None

    # One `path:line` line per search result in the markdown response.
    _RESULT_LINE = re.compile(r"^(\S+\.[A-Za-z0-9]{1,5}):\d+$", re.MULTILINE)

    @classmethod
    def _ensure_mcp(cls) -> _JsonRpcLineClient:
        if cls._mcp is not None:
            return cls._mcp
        client = _JsonRpcLineClient(["codegraph", "serve", "--mcp"])
        client.start()
        cls._mcp = client
        import atexit

        atexit.register(cls._teardown)
        return client

    @classmethod
    def _teardown(cls) -> None:
        if cls._mcp is not None:
            with contextlib.suppress(Exception):
                cls._mcp.stop()
            cls._mcp = None

    def start(self, ws: Path) -> None:
        cg_db = ws / ".codegraph" / "codegraph.db"
        if not cg_db.exists():
            print(f"{_TAG} cg init {ws.name} ...", file=sys.stderr, flush=True)
            t1 = time.perf_counter()
            r = subprocess.run(
                ["codegraph", "init", "-i", str(ws)],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if r.returncode != 0:
                raise RuntimeError(f"codegraph init failed: {r.stderr[:400]}")
            print(f"{_TAG} cg init done in {time.perf_counter() - t1:.1f}s", file=sys.stderr)
        self._ws = ws
        # Warm-up query: the server lazily opens/syncs a project on first
        # touch (seconds on a cold repo) — pay that here, not in query stats.
        self.search_symbol(ws.name, ws)

    def stop(self) -> None:
        pass  # shared MCP server stays alive; torn down atexit

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        cls = type(self)
        try:
            response = cls._ensure_mcp().call(
                "tools/call",
                {
                    "name": "codegraph_search",
                    "arguments": {"query": _sym(query), "limit": 20, "projectPath": str(ws)},
                },
                timeout=120,
            )
        except Exception:
            cls._teardown()  # dead/hung server: restart lazily on the next call
            return []
        result = response.get("result", {})
        if result.get("isError"):
            return []
        text = "\n".join(
            c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"
        )
        seen: set[str] = set()
        files: list[str] = []
        for m in self._RESULT_LINE.finditer(text):
            p = _rel(m.group(1), ws)
            if p not in seen:
                seen.add(p)
                files.append(p)
        return files

    def search_text(self, query: str, ws: Path) -> list[str]:
        return []  # codegraph has no content/text search


# ---------------------------------------------------------------------------
# cmm (codebase-memory-mcp)
# ---------------------------------------------------------------------------

_CMM_VERSION = "v0.8.1"
_CMM_ASSET = "codebase-memory-mcp-linux-amd64.tar.gz"
_CMM_HOME = Path(os.environ.get("CMM_HOME", "/tmp/cmm-bench")).resolve()


class CmmProvider(Provider):
    """DeusData's codebase-memory-mcp: a single static Go binary driven in
    one-shot `cli <tool> '<json>'` mode -- no persistent MCP server, so
    start()/stop() manage the binary + per-repo index rather than a long-lived
    process (the same on-disk graph.db is read fresh on every call)."""

    name = "cmm"

    def __init__(self) -> None:
        self._bin: Path | None = None
        self._env: dict[str, str] = {}
        self._project: str | None = None

    @staticmethod
    def _ensure_binary() -> Path:
        env_bin = os.environ.get("CMM_BIN")
        if env_bin and Path(env_bin).is_file():
            return Path(env_bin)
        bindir = _CMM_HOME / "bin"
        binpath = bindir / "codebase-memory-mcp"
        if binpath.is_file():
            return binpath
        bindir.mkdir(parents=True, exist_ok=True)
        tgz = bindir / _CMM_ASSET
        url = f"https://github.com/DeusData/codebase-memory-mcp/releases/download/{_CMM_VERSION}/{_CMM_ASSET}"
        print(f"{_TAG} downloading {url}", file=sys.stderr, flush=True)
        urllib.request.urlretrieve(url, tgz)  # nosec - pinned release asset
        with tarfile.open(tgz) as tf:
            tf.extract("codebase-memory-mcp", path=bindir)
        binpath.chmod(0o755)
        return binpath

    def _cli(self, tool: str, args: dict, timeout: int = 120) -> dict:
        assert self._bin is not None
        proc = subprocess.run(
            [str(self._bin), "cli", tool, json.dumps(args)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._env,
        )
        out = proc.stdout.strip()
        if not out:
            return {}
        try:
            return cast(dict, json.loads(out))
        except json.JSONDecodeError:
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return cast(dict, json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return {}

    def _paths(self, result: dict, key: str, ws: Path, limit: int = 10) -> list[str]:
        files: list[str] = []
        seen: set[str] = set()
        for it in result.get("results", []) or []:
            raw = str(it.get(key) or it.get("file_path") or it.get("file") or "")
            f = _rel(raw, ws) if raw else ""
            if f and f not in seen:
                seen.add(f)
                files.append(f)
            if len(files) >= limit:
                break
        return files

    def start(self, ws: Path) -> None:
        self._bin = self._ensure_binary()
        self._env = dict(os.environ)
        home = _CMM_HOME / "home"
        home.mkdir(parents=True, exist_ok=True)
        self._env["HOME"] = str(home)
        idx = self._cli("index_repository", {"repo_path": str(ws), "mode": "full"}, timeout=3600)
        project = idx.get("project")
        if not project or (idx.get("status") != "indexed" and not idx.get("nodes")):
            raise RuntimeError(f"cmm index failed: {json.dumps(idx)[:400]}")
        self._project = project

    def stop(self) -> None:
        self._project = None  # one-shot CLI -- no persistent process to tear down

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        if not self._project:
            return []
        res = self._cli("search_graph", {"project": self._project, "query": query, "limit": 10})
        return self._paths(res, "file_path", ws)

    def search_text(self, query: str, ws: Path) -> list[str]:
        if not self._project:
            return []
        res = self._cli(
            "search_code",
            {"project": self._project, "pattern": query, "limit": 10, "mode": "compact"},
        )
        return self._paths(res, "file", ws)


# ---------------------------------------------------------------------------
# fff-mcp (crates/fff-mcp in github.com/dmtrKovalenko/fff -- the same engine
# the @ff-labs/pi-fff pi extension wraps, run over its own stdio MCP surface)
# ---------------------------------------------------------------------------

# A bare relative path token: either a dotfile (".gitignore") or a normal
# path ending in a short extension ("src/foo/bar.py"). Deliberately anchored
# (fullmatch via ^...$) so it rejects everything else fff-mcp's text output
# mixes in on the same first-token position: header lines ("8/40 matches",
# "0 results (0 indexed)" -- first token "8/40"/"0", no literal dot),
# "cursor: N" lines (token "cursor:", no dot), the "-> Read <path> [def]"
# hint line (token is the arrow glyph, not a path -- the same path also
# appears on its own line right after, so nothing is lost), and indented
# match/body-context lines ("NNN: code" / "NNN| code", token "NNN:"/"NNN|",
# no dot).
_FFF_PATH_TOKEN = re.compile(r"^(?:\.[\w-]+|[\w][\w./-]*\.[A-Za-z0-9]{1,8})$")
_FFF_INDEXED_RE = re.compile(r"\((\d+) indexed\)")


class FffProvider(Provider):
    """FFF's standalone MCP server (``fff-mcp``), independent of the ``pi``
    agent ``@ff-labs/pi-fff`` plugs into.

    Confirmed against a real v0.9.6 server via a manual tools/list +
    tools/call probe -- the pi-fff README describes a *different* tool
    surface (that is the pi extension's own wrapper, not the raw MCP server):
      - real tool names are ``grep`` / ``find_files`` / ``multi_grep`` (not
        ``ffgrep``/``fffind``/``fff-multi-grep``), args ``query`` +
        ``maxResults`` (not ``pattern``/``limit``).
      - responses are pre-rendered TEXT meant for an LLM to read (a
        "src/foo.py" path line followed by indented "NNN: code" /
        "NNN| code" match lines, optionally preceded by a
        "-> Read <path> [def]" hint and followed by "cursor: N"), not the
        structured ``{items: [...]}`` JSON the Node/Python SDK bindings
        return -- so paths are parsed line-by-line via _FFF_PATH_TOKEN
        instead of read off a JSON field.

    Not a code-symbol tool (no AST/LSP) -- ``grep`` is used for both
    search_symbol and search_text, same as RgProvider, so no
    ``supported_gold_kinds`` restriction: every gold kind is attempted
    rather than excluded.

    A fresh server is spawned per repo (cwd=ws): fff-mcp indexes the directory
    it is launched in (per its docs: "the current git-indexed directory"),
    there is no per-call project-switch argument confirmed at the MCP layer
    (unlike cg/serena, which expose an explicit project path per call and so
    can share one persistent process across repos).

    Install: ``curl -L https://dmtrkovalenko.dev/install-fff-mcp.sh | bash``
    (or ``brew install dmtrKovalenko/fff/fff-mcp``) -- assumed already on
    PATH, same convention as CgProvider/SerenaProvider/JCodeMunchProvider.
    """

    name = "fff"

    def __init__(self) -> None:
        self._client: _JsonRpcLineClient | None = None

    def start(self, ws: Path) -> None:
        client = _JsonRpcLineClient(["fff-mcp"], cwd=ws)
        client.start()
        self._client = client
        # Untimed warm-up: fff-mcp scans + content-indexes the workspace in
        # the background after the MCP handshake returns -- an immediate
        # query on a real repo comes back "0 results (0 indexed)" before the
        # scan finishes (confirmed via a real run against this repo: still
        # "(0 indexed)" at 2s, fully indexed and matching by 4s). A query
        # that can never match a real file keeps every poll response in the
        # "(N indexed)" branch -- that suffix only disappears once real
        # matches start coming back -- so N's growth can be tracked to
        # completion: poll until it stops growing for two consecutive
        # checks, or until _WARMUP_TIMEOUT_S (same 900s ceiling
        # SerenaProvider uses for its own cold-index warm-up) elapses.
        deadline = time.monotonic() + _WARMUP_TIMEOUT_S
        last_count = -1
        stable = 0
        while time.monotonic() < deadline:
            try:
                text = self._raw_call("find_files", {"query": "xnonexistentwarmupprobex", "maxResults": 1})
            except Exception:
                break
            m = _FFF_INDEXED_RE.search(text)
            if m is None:
                break  # no "(N indexed)" suffix left in the response: fully scanned
            count = int(m.group(1))
            stable = stable + 1 if count == last_count else 0
            last_count = count
            if stable >= 2:
                break
            time.sleep(1)

    def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.stop()
        self._client = None

    def _raw_call(self, tool: str, args: dict) -> str:
        assert self._client is not None
        response = self._client.call("tools/call", {"name": tool, "arguments": args}, timeout=30)
        result = response.get("result", {})
        return "\n".join(
            c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"
        )

    def _call(self, tool: str, args: dict, ws: Path) -> list[str]:
        if self._client is None:
            return []
        try:
            text = self._raw_call(tool, args)
        except Exception:
            return []
        seen: set[str] = set()
        paths: list[str] = []
        for line in text.splitlines():
            tokens = line.split(None, 1)
            if not tokens or not _FFF_PATH_TOKEN.match(tokens[0]):
                continue
            p = _rel(tokens[0], ws)
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
        if not paths and text.strip():
            paths = _extract_paths_text(text, ws)
        return paths

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        return self._call("grep", {"query": _sym(query), "maxResults": 20}, ws)

    def search_text(self, query: str, ws: Path) -> list[str]:
        return self._call("grep", {"query": query, "maxResults": 20}, ws)


# ---------------------------------------------------------------------------
# ccc (cocoindex-code) -- local semantic vector search over code chunks
# ---------------------------------------------------------------------------


class CccProvider(Provider):
    """cocoindex-code (``ccc``): chunk-level semantic search backed by a local
    Ollama embedding model (``nomic-embed-text`` by default).

    One-shot CLI per query -- confirmed ``ccc search`` answers in ~100ms once
    the project is indexed (the embedding model stays warm in the Ollama
    daemon, so there is no per-call model-load cost), same shape as
    CmmProvider/RgProvider. ``ccc`` has no per-call project-path argument, so
    each repo gets its own ``.cocoindex_code/`` project dir via ``cwd=ws``
    (``ccc init`` is idempotent -- prints "Project already initialized." and
    exits 0 on repeat runs, so start() can call it unconditionally).

    Requires ``ollama serve`` running locally with ``nomic-embed-text``
    pulled -- ccc's default embedder; its own CLI does not provision this.
    """

    name = "ccc"
    _RESULT_FILE = re.compile(r"^File: (\S+):\d+-\d+", re.MULTILINE)

    def _check_ollama(self) -> None:
        """Fail fast and loud if the Ollama daemon backing ccc's embedder is
        unreachable, instead of silently running the whole suite through
        _search()'s "non-zero rc -> []" fallback. Confirmed root cause of a
        full 6016-query run scoring MRR 0.0000 / hit@1 0.0000 while `ccc init`
        and `ccc index` still succeeded (index already built from a prior run,
        so start() never needed the embedder) -- Ollama was down for the
        query-time embedding calls `ccc search` makes on every invocation.
        """
        base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        url = base if "://" in base else f"http://{base}"
        try:
            urllib.request.urlopen(f"{url}/api/tags", timeout=5)
        except Exception as exc:
            raise RuntimeError(
                f"ccc requires a reachable ollama daemon at {url} for nomic-embed-text "
                f"(query-time embedding) -- got: {exc}"
            ) from exc

    def start(self, ws: Path) -> None:
        self._ws = ws
        self._check_ollama()
        init = subprocess.run(["ccc", "init"], cwd=ws, capture_output=True, text=True, timeout=60)
        if init.returncode != 0:
            raise RuntimeError(f"ccc init failed: {(init.stderr or init.stdout)[:400]}")
        idx = subprocess.run(["ccc", "index"], cwd=ws, capture_output=True, text=True, timeout=1800)
        if idx.returncode != 0:
            raise RuntimeError(f"ccc index failed: {(idx.stderr or idx.stdout)[:800]}")

    def stop(self) -> None:
        pass  # one-shot CLI per query -- no persistent process to tear down

    def _search(self, query: str, ws: Path) -> list[str]:
        try:
            r = subprocess.run(
                ["ccc", "search", query, "--limit", "20"],
                cwd=ws,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return []
        if r.returncode != 0:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for m in self._RESULT_FILE.finditer(r.stdout):
            p = _rel(m.group(1), ws)
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    # Semantic tool -- raw query for both, same convention as CmmProvider/
    # LemonCrowProvider (no _sym() token extraction; embeddings want the full
    # natural-language-ish query, not a bare identifier).
    def search_symbol(self, query: str, ws: Path) -> list[str]:
        return self._search(query, ws)

    def search_text(self, query: str, ws: Path) -> list[str]:
        return self._search(query, ws)


# ---------------------------------------------------------------------------
# lemoncrow — the shipped LemonCrow MCP server, treated as just another provider
# ---------------------------------------------------------------------------


class LemonCrowProvider(Provider):
    """LemonCrow's stock MCP server over stdio, no special treatment.

    Launches ``lc mcp`` per workspace and calls the shipped ``code_search``
    tool with the RAW query (the surface agents actually use -- no ``_sym()``
    shaping, or MRR loses continuity with the retired fitness_explore_mrr
    history). Measures engine + serialization + transport end-to-end.

    DB routing without touching the server: the provisioned index (and its
    sibling intel/fts/vectors DBs) is symlinked into a bench LEMONCROW_ROOT at
    the engine's default ``workspaces/<key>/`` location, so the server
    resolves it exactly as production would. The server's own startup warm
    path (page cache, centrality, ANN matrix, zoekt webserver) covers cold
    costs; one untimed warm-up query in start() absorbs any residual
    first-query wait (zoekt readiness) so timed queries measure steady state.

    ``search_symbol``/``search_text`` share one memoized explore per query:
    explore is LemonCrow's single retrieval surface for both, exactly as the
    fitness benchmark measured it (latency is gold-independent).
    """

    name = "lemoncrow"

    _STORE_ROOT = Path(os.environ.get("LEMONCROW_BENCH_STORE", "/tmp/lemoncrow-bench-store"))

    def __init__(self) -> None:
        self._client: _JsonRpcLineClient | None = None
        self._memo: dict[str, list[str]] = {}

    def _route_db(self, ws: Path) -> None:
        """Symlink the provisioned per-repo DBs into the engine-default layout.

        Re-links on every call instead of ``if not link.exists()``: a stale
        symlink from an earlier run/session (pointing at a since-replaced or
        since-completed snapshot, e.g. a partially-backfilled embedding DB)
        would otherwise persist silently forever -- every future eval reads
        the SAME wrong file, with no error, indefinitely. Symlink creation is
        microseconds; there is no real cost to always refreshing it.
        """
        from lemoncrow.core.foundation.paths import workspace_key  # src/ is on sys.path

        # Always ensure the engine-default workspace dir exists, even when
        # there's no prebuilt snapshot to symlink: without it, a fresh
        # sqlite3.connect() for an on-demand build hard-errors with "unable
        # to open database file" (sqlite can't create the file if its parent
        # dir is missing) before the engine ever gets a chance to build one --
        # confirmed via a real run where a since-deleted prebuilt DB left this
        # dir never created, silently zeroing every query against that
        # workspace instead of falling through to the documented on-demand
        # build below.
        ws_dir = self._STORE_ROOT / "workspaces" / workspace_key(ws.resolve())
        ws_dir.mkdir(parents=True, exist_ok=True)

        meta = next((m for m in _all_repos.values() if Path(m.get("ws", "")) == ws), {})
        db = Path(meta["db"]) if meta.get("db") else None
        if db is None or not db.exists():
            # With LEMONCROW_CODE_AUTOSYNC=0 (the bench default, see start()) the
            # server no longer builds a missing index on demand, so a missing
            # snapshot means every query for this workspace scores ZERO. Make
            # that impossible to miss instead of a silent flat-0.0 gold.
            print(
                f"[route] *** NO PREBUILT SNAPSHOT for {ws.name}: expected {db}. "
                f"Queries against this workspace will score zero. Run "
                f"scripts/rebuild_isolated_bench_secondary_dbs.py to regenerate. ***",
                file=sys.stderr,
                flush=True,
            )
            return
        links = {"code_context.sqlite": db}
        # Sibling DBs live at db.parent/<sibling> -- the same convention the
        # engine itself uses (CodeContextEngine.{intel,fts,vectors}_db_path are
        # always db_path.parent-relative), which is exactly why each repo's
        # snapshot MUST live in its own directory (see
        # scripts/rebuild_isolated_bench_secondary_dbs.py, which points
        # --db-path at a dedicated /tmp/idx_isolated/<repo>/ dir per repo).
        # Previously every frozen snapshot sat flat in /tmp
        # (/tmp/idx_<repo>.db), so db.parent was /tmp for EVERY repo and this
        # same lookup silently resolved to ONE shared /tmp/fts.sqlite (and
        # /tmp/intel.sqlite) for all of them -- confirmed via a real run:
        # /tmp/fts.sqlite's file_line_fts held 8 different repos' lines (2.85M
        # rows total, astropy's own share only 500K), so every repo's
        # line-search channel scanned every OTHER routed repo's text too. A
        # missing sibling is skipped (that channel falls back to empty/
        # on-demand build) rather than ever falling back to a shared file.
        for sibling in ("intel.sqlite", "fts.sqlite", "vectors.sqlite"):
            src = db.parent / sibling
            if src.exists():
                links[sibling] = src
        for link_name, target in links.items():
            link = ws_dir / link_name
            if link.exists() or link.is_symlink():
                if link.is_symlink() and link.resolve() == target.resolve():
                    continue  # already correct, skip the churn
                link.unlink()
            link.symlink_to(target)
        routed_db = ws_dir / "code_context.sqlite"
        self._clear_retrieval_cache(routed_db)
        self._warn_if_stale(ws.name, routed_db)

    def _warn_if_stale(self, repo_name: str, db_path: Path) -> None:
        """Loudly flag a routed DB whose embeddings look incomplete.

        Symptom this catches: a frozen/provisioned snapshot that was captured
        mid-backfill (or never refreshed after one completed) silently scores
        as a ranking regression instead of an obvious data problem -- this
        turns that into an impossible-to-miss stderr line at the START of the
        run, not a multi-hour investigation after the numbers look wrong.
        Only checks when semantic is actually configured for this run
        (LEMONCROW_CODE_EMBEDDER set); otherwise an unembedded DB is expected.
        """
        if not os.environ.get("LEMONCROW_CODE_EMBEDDER"):
            return
        import sqlite3 as _sqlite3

        with contextlib.suppress(_sqlite3.Error):
            conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            try:
                symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
                vectors = conn.execute("SELECT COUNT(*) FROM symbol_vectors").fetchone()[0]
            finally:
                conn.close()
            coverage = (vectors / symbols) if symbols else 1.0
            print(
                f"[route] {repo_name}: {db_path.resolve()} -- symbols={symbols} "
                f"symbol_vectors={vectors} ({coverage:.0%} embedded)",
                file=sys.stderr,
                flush=True,
            )
            if coverage < 0.95:
                print(
                    f"[route] *** STALE/INCOMPLETE EMBEDDINGS for {repo_name}: only {coverage:.0%} of "
                    f"symbols have vectors. Semantic-channel results for this repo are UNRELIABLE. "
                    f"Refresh the provisioned snapshot ({db_path.resolve()}) from a complete backfill "
                    f"before trusting these numbers. ***",
                    file=sys.stderr,
                    flush=True,
                )

    def _clear_retrieval_cache(self, db_path: Path) -> None:
        """Wipe the persisted retrieval_cache table before a channel's run.

        The cache key is (tool_name, args, index_version, repo_id) -- it does NOT
        include zoekt mode / semantic mode / embedder, so every channel that shares
        this repo's DB (symlinked, not copied -- same underlying file across
        lexical / lexical+zoekt / lexical+zoekt+semantic) would otherwise silently
        replay whichever channel ran first for repeat queries, never actually
        exercising its own zoekt/semantic config. Set LEMONCROW_BENCH_REUSE_CACHE=1
        to skip this and reuse a warm cache for fast local iteration (NOT for a
        real cross-channel comparison run).
        """
        if os.environ.get("LEMONCROW_BENCH_REUSE_CACHE", "").strip() == "1":
            return
        if not db_path.exists():
            return
        import sqlite3 as _sqlite3

        with contextlib.suppress(_sqlite3.Error):
            conn = _sqlite3.connect(str(db_path), timeout=30.0)
            try:
                conn.execute("DELETE FROM retrieval_cache")
                conn.commit()
            finally:
                conn.close()

    def _backfill_semantic_vectors(self, ws: Path) -> None:
        """Explicitly build semantic vectors for a frozen bench snapshot.

        When ``--channel lexical+zoekt+semantic`` is used the embedder env is
        set, but the routed snapshot's ``symbol_vectors`` table may be empty
        (captured before embeddings existed, or never refreshed).  The engine's
        ``_ensure_indexed()`` returns early (FTS index present) and autosync is
        off, so no background worker ever populates the missing vectors —
        queries silently score against an empty semantic channel, tanking MRR.

        Running ``lc code index`` triggers the "nothing to extract" fast path
        that still checks ``embedded < total`` and calls
        ``_build_symbol_embeddings`` for the delta.  This is the explicit
        backfill the engine comments recommend for /tmp snapshots.
        """
        import subprocess as _sp

        bench_python = os.environ.get("LEMONCROW_BENCH_PYTHON", "").strip()
        cmd = [
            bench_python or sys.executable,
            "-m",
            "lemoncrow.gateway.cli",
            "code",
            "index",
            "--repo-root",
            str(ws),
        ]
        env = {
            **os.environ,
            "LEMONCROW_ROOT": str(self._STORE_ROOT),
            "LEMONCROW_CODE_AUTOSYNC": "0",
            "LEMONCROW_CODE_FILE_WATCHER": "0",
        }
        # No-op when no embedder is configured — only pays the FTS-check cost.
        try:
            t0 = time.perf_counter()
            result = _sp.run(
                cmd,
                cwd=Path.cwd(),
                env=env,
                capture_output=True,
                timeout=600,
                check=False,
            )
            elapsed = time.perf_counter() - t0
            if result.returncode != 0:
                stderr_tail = (result.stderr or b"")[-300:].decode("utf-8", errors="replace")
                print(
                    f"[route] semantic backfill failed for {ws.name} (rc={result.returncode}, "
                    f"{elapsed:.1f}s): {stderr_tail}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"[route] semantic backfill done for {ws.name} ({elapsed:.1f}s)",
                    file=sys.stderr,
                    flush=True,
                )
        except _sp.TimeoutExpired:
            print(
                f"[route] semantic backfill timed out for {ws.name} (600s)",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            print(
                f"[route] semantic backfill error for {ws.name}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def start(self, ws: Path) -> None:
        self._memo = {}
        self._route_db(ws)
        # When semantic search is enabled, frozen bench snapshots may have the
        # FTS index but no symbol vectors.  Without an explicit backfill the
        # engine's _ensure_indexed() returns early (index "ready"), autosync is
        # off, and every semantic query silently scores zero — confirmed via a
        # real sweep where MRR slipped to 0.5.  Build the missing vectors here
        # (untimed) so timed queries see a fully-populated vector store.
        if os.environ.get("LEMONCROW_CODE_EMBEDDER"):
            self._backfill_semantic_vectors(ws)
        # LEMONCROW_BENCH_PYTHON: absolute path to an alternate interpreter whose
        # site-packages carries the lemoncrow wheel to measure -- e.g. a venv with
        # the mypyc-compiled production build (scripts/build.sh). When set, the
        # working-tree src/ is NOT injected on PYTHONPATH, so the spawned server
        # runs the compiled package instead of interpreted source.
        bench_python = os.environ.get("LEMONCROW_BENCH_PYTHON", "").strip()
        env = {
            **os.environ,
            "LEMONCROW_ROOT": str(self._STORE_ROOT),
            "LEMONCROW_WORKSPACE_ROOT": str(ws),
            # the candidate/working-tree code, not an installed wheel (unless
            # LEMONCROW_BENCH_PYTHON points at a wheel-bearing interpreter above)
            "PYTHONPATH": (
                os.environ.get("PYTHONPATH", "")
                if bench_python
                else "src" + os.pathsep + os.environ.get("PYTHONPATH", "")
            ),
            # let the untimed warm-up absorb the one-time zoekt shard load
            "LEMONCROW_ZOEKT_READY_TIMEOUT_S": os.environ.get("LEMONCROW_ZOEKT_READY_TIMEOUT_S", "30"),
            # `ws` lives under /tmp (a provisioned bench snapshot, not a stray
            # scratch dir) and `_route_db` above already documents "no prebuilt
            # index: the server will build one on demand" when a snapshot's DB
            # has gone missing/stale. The engine's /tmp autoindex guard (added
            # to stop a stray query against a real scratch dir from silently
            # kicking off a large background index build) would otherwise
            # refuse that on-demand build here too, hard-erroring every query
            # for the workspace instead of indexing it -- confirmed via a real
            # run: the "sessions" gold (491 queries, all against this kind of
            # snapshot) scored a flat 0.0 MRR at ~3ms/query, the signature of
            # an instant "unable to open database file" error, not a real
            # search. Opt back in by default; still overridable via env.
            "LEMONCROW_ALLOW_TMP_AUTOINDEX": os.environ.get("LEMONCROW_ALLOW_TMP_AUTOINDEX", "1"),
            # Benchmark snapshots are FROZEN: no autosync drift checks, no file
            # watcher. Without this the engine treats the routed snapshot as a
            # live workspace -- at linux scale the inotify watch limit disables
            # the watcher, the polling fallback stat-walks the whole tree, and
            # a detected "drift" launches a low-priority whole-repo reindex
            # subprocess (observed running 2h+ at 100% of one core) that
            # rewrites the frozen snapshot THROUGH the symlinks while timed
            # queries run against it (linux collapsed to a steady 1.8/s).
            # _route_db warns loudly when a snapshot is missing, since the
            # on-demand build path is disabled along with autosync.
            "LEMONCROW_CODE_AUTOSYNC": os.environ.get("LEMONCROW_CODE_AUTOSYNC", "0"),
            "LEMONCROW_CODE_FILE_WATCHER": os.environ.get("LEMONCROW_CODE_FILE_WATCHER", "0"),
        }
        # Host workspace vars outrank LEMONCROW_WORKSPACE_ROOT in the server's
        # resolution; a bench run inside Claude Code/Cursor would otherwise
        # inherit them and silently search the WRONG repo.
        for host_var in ("CLAUDE_WORKSPACE_ROOT", "CURSOR_WORKSPACE_ROOT", "VSCODE_CWD", "CLAUDE_PROJECT_DIR"):
            env.pop(host_var, None)
        # Host SESSION ids make the bench server adopt the launching agent
        # session's identity: every credited tool call then re-parses that
        # session's (huge) transcript and the 5s-rate-limited statusline
        # refresh recomputes historical savings windows over sessions/** --
        # pure-Python, GIL-holding, measured as moving 3-4s stalls landing on
        # whatever timed query runs concurrently (p100 4.9s on an otherwise
        # 163ms-p95 run). No session id = savings sidecar fails closed =
        # timed queries measure retrieval, not the statusline pipeline.
        for session_var in (
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_SESSION_ID",
            "OPENCODE_SESSION_ID",
            "GITHUB_COPILOT_SESSION_ID",
            "CURSOR_SESSION_ID",
            "CURSOR_TRACE_ID",
            "HERMES_SESSION_ID",
            "ANTIGRAVITY_SESSION_ID",
            "AGY_SESSION_ID",
        ):
            env.pop(session_var, None)
        client = _JsonRpcLineClient(
            [bench_python or sys.executable, "-c", "from lemoncrow.gateway.adapters.mcp_server import main; main()"],
            cwd=Path.cwd(),
            env=env,
        )
        client.start()
        self._client = client
        # Untimed warm-up (same pattern as CgProvider): pays engine init +
        # readiness waits here, not in the timed query stats. Two shapes:
        # a symbol-ish query (zoekt shard load, FTS page cache) and an
        # NL-ish one — the fused explore only reaches the semantic channel
        # for NL queries, so without it the embed model loads on the first
        # TIMED natural-language query (~120s cold).
        #
        # _WARMUP_TIMEOUT_S (900s, same constant SerenaProvider uses) instead
        # of the previous 240s/300s: confirmed via a real run this mattered at
        # linux scale under host contention (other concurrent lemoncrow mcp
        # processes sharing the same GPU) -- when a warm-up call ran past its
        # old budget, _search's own 120s timeout on the FIRST REAL TIMED query
        # fired next, killing the subprocess (_JsonRpcLineClient never blocks
        # indefinitely) and _recover_client's synchronous restart+re-warm got
        # bundled into that one query's measured latency (observed: 184s/312s
        # instead of the true warm per-query cost). Generous headroom here
        # keeps that whole cold/contended cost off the timed critical path,
        # exactly like the untimed warm-up is supposed to.
        with contextlib.suppress(Exception):
            self._search(f"warmup {ws.name}", ws, timeout=_WARMUP_TIMEOUT_S)
        with contextlib.suppress(Exception):
            self._search(
                "how does the configuration loading and startup initialization work",
                ws,
                timeout=_WARMUP_TIMEOUT_S,
            )
        self._memo = {}

    def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.stop()
        self._client = None
        self._memo = {}

    def _paths_from_payload(self, payload: dict, ws: Path) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []

        def _add(raw: object) -> None:
            if raw:
                p = _rel(str(raw), ws)
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)

        # `files` are the ranked top matches; `candidate_files` extend the
        # ranked tail. Same order tool_explore returned them.
        for f in payload.get("files", []) or []:
            if isinstance(f, dict):
                _add(f.get("path") or f.get("file_path"))
        for c in payload.get("candidate_files", []) or []:
            _add(c)
        return out

    def _search(self, query: str, ws: Path, *, timeout: float = 120) -> list[str]:
        if query in self._memo:
            return self._memo[query]
        if self._client is None:
            return []
        response = self._client.call(
            "tools/call",
            {"name": "code_search", "arguments": {"query": query, "max_files": 10}},
            timeout=timeout,
        )
        result = response.get("result", {})
        if result.get("isError"):
            self._memo[query] = []
            return []
        payload: dict = result.get("structuredContent") or {}
        if not payload:
            for chunk in result.get("content", []) or []:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    with contextlib.suppress(json.JSONDecodeError):
                        payload = json.loads(chunk.get("text", ""))
                        break
        if not payload:
            # Compact markdown wire format (code_search renderer): ranked files
            # arrive as `## path` headers, the recall tail as one
            # `candidate_files: a, b, ...` line. Parse ONLY those -- section
            # source, outline pointers, and related_symbols lines carry paths
            # that are NOT part of the ranked surface; regex-scraping them (the
            # old fallback) polluted ranks 2+ and misread the ranking.
            md_files: list[str] = []
            md_cands: list[str] = []
            for chunk in result.get("content", []) or []:
                if not (isinstance(chunk, dict) and chunk.get("type") == "text"):
                    continue
                for line in str(chunk.get("text", "")).splitlines():
                    if line.startswith("## "):
                        md_files.append(line[3:].strip())
                    elif line.startswith("candidate_files: "):
                        md_cands.extend(p.strip() for p in line[len("candidate_files: ") :].split(","))
            if md_files or md_cands:
                payload = {"files": [{"path": p} for p in md_files], "candidate_files": md_cands}
        files = self._paths_from_payload(payload, ws) if payload else []
        if not files:  # last resort: scrape paths from raw text
            files = _extract_paths_text(json.dumps(result), ws)
        self._memo[query] = files
        return files

    def _recover_client(self, ws: Path) -> None:
        """Restart the MCP server after a failed call.

        A single slow query (e.g. a hard natural-language query that exceeds
        _search's 120s timeout) makes _JsonRpcLineClient._read_message kill the
        server subprocess -- but without this, every subsequent query in the
        SAME run would write to that now-dead process's stdin, raise, and be
        silently swallowed by the blanket ``except Exception: return []``
        below, scoring as an instant empty miss. One slow query would then
        silently zero out every query after it for the rest of the benchmark
        instead of just the one that actually timed out. Confirmed via a real
        run: reported latency showed a max of ~120195ms (just over the 120s
        timeout) followed by a burst of queries all completing within the same
        reported elapsed second -- the signature of exactly this cascade, not
        of genuinely fast/working queries.
        """
        with contextlib.suppress(Exception):
            if self._client is not None:
                self._client.stop()
        with contextlib.suppress(Exception):
            self.start(ws)

    def search_symbol(self, query: str, ws: Path) -> list[str]:
        try:
            return self._search(query, ws)
        except Exception:
            self._recover_client(ws)
            return []

    def search_text(self, query: str, ws: Path) -> list[str]:
        try:
            return self._search(query, ws)
        except Exception:
            self._recover_client(ws)
            return []


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[Provider]] = {
    "lemoncrow": LemonCrowProvider,
    "ctags": CtagsProvider,
    "ast-grep": AstGrepProvider,
    "serena": SerenaProvider,
    "code-index-mcp": CodeIndexProvider,
    "jcodemunch": JCodeMunchProvider,
    "cg": CgProvider,
    "rg": RgProvider,
    "cmm": CmmProvider,
    "fff": FffProvider,
    "ccc": CccProvider,
}

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score_gold(kind: str, tm: dict, results: dict[tuple[str, str], list[str]]) -> dict:
    """Compute MRR/hit metrics for one gold kind.

    results: {(query, prefix): ranked_file_list}
    """
    agg = {"rr": 0.0, "h1": 0, "h2": 0, "h3": 0, "n": 0}
    by_repo: dict[str, dict] = {}
    lats_by_repo: dict[str, list[float]] = defaultdict(list)

    for (q, prefix), files in results.items():
        tids = _q_to_tids.get((q, prefix), {})
        tid = tids.get(kind)
        if not tid:
            continue
        trues = [p.replace("\\", "/") for p in tm.get(tid, []) if p]
        if not trues:
            continue
        r = _rank(files, trues)
        br = by_repo.setdefault(prefix, {"rr": 0.0, "h1": 0, "h2": 0, "h3": 0, "n": 0})
        for d in (agg, br):
            d["n"] += 1
            if r:
                d["rr"] += 1.0 / r
                if r == 1:
                    d["h1"] += 1
                if r <= 2:
                    d["h2"] += 1
                if r <= 3:
                    d["h3"] += 1

    return {
        "mrr": round(agg["rr"] / max(agg["n"], 1), 4),
        "hit1": round(agg["h1"] / max(agg["n"], 1), 4),
        "hit2": round(agg["h2"] / max(agg["n"], 1), 4),
        "hit3": round(agg["h3"] / max(agg["n"], 1), 4),
        "n": agg["n"],
        "by_repo": {
            p: {
                "mrr": round(d["rr"] / max(d["n"], 1), 4),
                "hit1": round(d["h1"] / max(d["n"], 1), 4),
                "hit2": round(d["h2"] / max(d["n"], 1), 4),
                "hit3": round(d["h3"] / max(d["n"], 1), 4),
                "n": d["n"],
                "latency_ms": _lat_summary(lats_by_repo.get(p, [])),
            }
            for p, d in sorted(by_repo.items())
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if not _golds:
    print(f"{_TAG} no golds loaded", file=sys.stderr)
    sys.exit(1)

WORKERS = _args.workers
provider_cls = _PROVIDERS[PROVIDER]
total_queries = len(_union)
print(f"{_TAG} repos={len(_queries_by_repo)} queries={total_queries} workers={WORKERS}", file=sys.stderr)


def _process_repo(prefix: str, queries: list[str]) -> dict | None:
    """Process all queries for a single repo. Returns result dict or None if skipped.

    Creates its own provider instance so >1 worker can run repos in parallel.
    """
    repo_meta = _all_repos.get(prefix, {})
    ws = Path(repo_meta.get("ws", ""))
    if not ws.exists():
        print(f"{_TAG} skip {prefix}: ws not found ({ws})", file=sys.stderr)
        return None

    prov = provider_cls()
    try:
        prov.start(ws)
    except Exception as exc:
        print(f"{_TAG} {prefix} start failed: {exc}", file=sys.stderr)
        return None

    t_repo = time.perf_counter()
    print(f"{_TAG} start {prefix} ({len(queries)} queries)", file=sys.stderr, flush=True)

    sym_res: dict[tuple[str, str], list[str]] = {}
    txt_res: dict[tuple[str, str], list[str]] = {}
    lats: list[float] = []

    for q in queries:
        t1 = time.perf_counter()
        try:
            sym_files = prov.search_symbol(q, ws)
        except Exception:
            sym_files = []
        try:
            txt_files = prov.search_text(q, ws)
        except Exception:
            txt_files = []
        elapsed_q = (time.perf_counter() - t1) * 1000.0
        lats.append(elapsed_q)
        sym_res[(q, prefix)] = sym_files
        txt_res[(q, prefix)] = txt_files

    # LEMONCROW_BENCH_LAT_DUMP=<dir>: per-query latency JSON per repo, for tail
    # diagnosis (which queries are slow, not just the percentiles).
    _dump_dir = os.environ.get("LEMONCROW_BENCH_LAT_DUMP", "").strip()
    if _dump_dir:
        _dd = Path(_dump_dir)
        _dd.mkdir(parents=True, exist_ok=True)
        (_dd / f"{prefix}.json").write_text(
            json.dumps(
                [{"q": q, "ms": round(ms, 1)} for q, ms in zip(queries, lats, strict=True)],
                indent=0,
            )
        )

    try:
        prov.stop()
    except Exception as exc:
        print(f"{_TAG} {prefix} stop error: {exc}", file=sys.stderr)

    t_elapsed = time.perf_counter() - t_repo
    avg_lat = sum(lats) / len(lats) if lats else 0
    print(
        f"{_TAG} done  {prefix} ({len(queries)} queries, {t_elapsed:.0f}s, {avg_lat:.0f}ms avg)",
        file=sys.stderr,
        flush=True,
    )
    return {
        "prefix": prefix,
        "sym_res": sym_res,
        "txt_res": txt_res,
        "lats": lats,
    }


_repo_results: list[dict] = []

if WORKERS <= 1:
    for prefix, queries in sorted(_queries_by_repo.items()):
        r = _process_repo(prefix, queries)
        if r is not None:
            _repo_results.append(r)
else:
    _SINGLETON_PROVIDERS = frozenset({"cg", "serena", "code-index-mcp", "jcodemunch"})
    if PROVIDER in _SINGLETON_PROVIDERS:
        print(
            f"{_TAG} WARNING: {PROVIDER} uses a shared MCP server — parallel workers "
            f"race on the JSON-RPC connection. Use --workers 1 for this provider.",
            file=sys.stderr,
        )
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _repos = sorted(_queries_by_repo.items())
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(_repos))) as pool:
        _futures = {pool.submit(_process_repo, p, qs): p for p, qs in _repos}
        for _fut in as_completed(_futures):
            r = _fut.result()
            if r is not None:
                _repo_results.append(r)

# {(query, prefix): ranked files} for scoring
_sym_results: dict[tuple[str, str], list[str]] = {}
_txt_results: dict[tuple[str, str], list[str]] = {}
all_latencies: list[float] = []
lats_by_repo: dict[str, list[float]] = defaultdict(list)
for _rr in _repo_results:
    _sym_results.update(_rr["sym_res"])
    _txt_results.update(_rr["txt_res"])
    lats_by_repo[_rr["prefix"]].extend(_rr["lats"])
    all_latencies.extend(_rr["lats"])


# Score each gold kind.
# definition -> sym_results; content -> txt_results
# swebench -> merge both (queries are a mix of symbol-lookup and text-search;
#             take sym hits first as they are more precise, then text hits).
def _merged(key: tuple[str, str]) -> list[str]:
    sym = _sym_results.get(key, [])
    txt = _txt_results.get(key, [])
    seen: set[str] = set(sym)
    return sym + [f for f in txt if f not in seen]


_gold_scores: dict[str, dict] = {}
for _kind, _, _tm in _golds:
    if _kind == "definition":
        scored = _sym_results
    elif _kind in {"content", "regex"}:
        scored = _txt_results
    else:  # semantic/swebench or any future mixed kind
        scored = {k: _merged(k) for k in set(_sym_results) | set(_txt_results)}
    _gold_scores[_kind] = _score_gold(_kind, _tm, scored)

# Attach per-repo latencies to by_repo entries
for _gk, gdata in _gold_scores.items():
    for prefix, rd in gdata.get("by_repo", {}).items():
        rd["latency_ms"] = _lat_summary(lats_by_repo.get(prefix, []))


def _weighted_gold_summary(golds: dict[str, dict]) -> dict[str, float | int]:
    total_n = sum(int(gd.get("n") or 0) for gd in golds.values())
    if total_n <= 0:
        return {"mrr": 0.0, "hit1": 0.0, "hit3": 0.0, "n": 0}
    return {
        "mrr": sum(float(gd.get("mrr") or 0.0) * int(gd.get("n") or 0) for gd in golds.values()) / total_n,
        "hit1": sum(float(gd.get("hit1") or 0.0) * int(gd.get("n") or 0) for gd in golds.values()) / total_n,
        "hit3": sum(float(gd.get("hit3") or 0.0) * int(gd.get("n") or 0) for gd in golds.values()) / total_n,
        "n": total_n,
    }


# Primary metrics (first gold kind) stay at top level for compatibility; overall
# is the n-weighted aggregate across every loaded gold kind.
# All tools are now evaluated on ALL gold kinds (unsupported types score 0).
_primary = _gold_scores[_golds[0][0]]
_overall = _weighted_gold_summary(_gold_scores)
_base_mode = "full" if FULL else (f"sample={SAMPLE}" if SAMPLE else "default")
_mode = f"{_base_mode}[{_LABEL}]"
if REPO_FILTER:
    _mode += f" repo={REPO_FILTER}"
out = {
    **_primary,
    "overall": _overall,
    "supported_overall": _overall,  # backward compat: all tools now eval on all golds
    "supported_gold_kinds": list(_gold_scores.keys()),  # backward compat
    "latency_ms": _lat_summary(all_latencies),
    "golds": _gold_scores,
    "provider": PROVIDER,
    "mode": _mode,
}

print(json.dumps(out, ensure_ascii=False))

# ── History: persist this run so trends and deltas survive across runs ────────
_HISTORY = Path("benchmarks/codebench/results/mrr_history.jsonl")
_HISTORY.parent.mkdir(parents=True, exist_ok=True)
try:
    _sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    _dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    _sha_label = _sha + ("+" if _dirty else "")
except Exception:
    _sha_label = "unknown"

from datetime import UTC  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402

_record = {
    "ts": _datetime.now(UTC).isoformat(timespec="seconds"),
    "sha": _sha_label,
    "mode": _mode,
    "mrr": out["mrr"],
    "hit1": out["hit1"],
    "hit3": out["hit3"],
    "n": out["n"],
    "overall_mrr": _overall["mrr"],
    "overall_hit1": _overall["hit1"],
    "overall_hit3": _overall["hit3"],
    "overall_n": _overall["n"],
    "supported_overall_mrr": _overall["mrr"],  # now same as overall (all tools eval all golds)
    "supported_overall_hit1": _overall["hit1"],
    "supported_overall_hit3": _overall["hit3"],
    "supported_overall_n": _overall["n"],
    "supported_gold_kinds": list(_gold_scores.keys()),  # all golds now
    "latency_ms": out["latency_ms"],
    "by_repo": out.get("by_repo", {}),
    "golds": out["golds"],
}
with _HISTORY.open("a") as _fh:
    _fh.write(json.dumps(_record) + "\n")

try:
    _runs = [json.loads(line) for line in _HISTORY.read_text().splitlines() if line.strip()]
except Exception:
    _runs = [_record]
# Only compare against a previous run of the same mode — cross-mode comparisons
# (different sample sizes / channels) skew the MRR baseline.
_prev = next((r for r in reversed(_runs[:-1]) if r.get("mode") == _mode), None)

# Summary — match fitness_explore_mrr.py format with per-repo breakdown
print("\n" + "─" * 60, file=sys.stderr)
print(f"  {_record['ts'][:16]}  {_sha_label}  [{_mode}]  provider={PROVIDER}", file=sys.stderr)
for gk, gd in _gold_scores.items():
    print(
        f"  gold={gk:<18} MRR {gd['mrr']:.4f}  hit@1 {gd['hit1']:.4f}  hit@3 {gd['hit3']:.4f}  n={gd['n']}",
        file=sys.stderr,
    )
_gold_kinds_label = ",".join(_gold_scores.keys()) if _gold_scores else "none"
print(
    f"  [{_gold_kinds_label}] MRR {_overall['mrr']:.4f}  "
    f"hit@1 {_overall['hit1']:.4f}  hit@3 {_overall['hit3']:.4f}  n={_overall['n']}",
    file=sys.stderr,
)
print(
    f"  all-gold             MRR {_overall['mrr']:.4f}  hit@1 {_overall['hit1']:.4f}  "
    f"hit@3 {_overall['hit3']:.4f}  n={_overall['n']}",
    file=sys.stderr,
)
lat = out["latency_ms"]
print(f"  lat  mean={lat['mean']:.0f}ms  p95={lat['p95']:.0f}ms  max={lat['max']:.0f}ms", file=sys.stderr)
# Per-repo rows sorted by primary-gold MRR ascending (worst first)
_primary_gk = _golds[0][0]
_by_repo_sorted = sorted(
    _gold_scores[_primary_gk].get("by_repo", {}).items(),
    key=lambda kv: kv[1].get("mrr", 0),
)
for _rprefix, _rd in _by_repo_sorted:
    _rmrr = _rd.get("mrr", 0)
    _rn = _rd.get("n", 0)
    _rlat = _rd.get("latency_ms") or {}
    _rp95 = _rlat.get("p95", 0)
    _rp100 = _rlat.get("max", 0)
    _icon = "✓" if _rmrr >= 0.9 else ("~" if _rmrr >= 0.5 else "✗")
    _short = _rprefix.split("__")[-1] if "__" in _rprefix else _rprefix
    # Build def/con MRR string
    _mrr_parts = []
    for _gk in ("definition", "content"):
        _gk_repo = (_gold_scores.get(_gk) or {}).get("by_repo", {}).get(_rprefix)
        if _gk_repo and isinstance(_gk_repo, dict):
            _mrr_parts.append(f"{_gk_repo['mrr']:.3f}")
    _mrr_str = "/".join(_mrr_parts) if len(_mrr_parts) > 1 else f"{_rmrr:.3f}"
    print(
        f"  {_icon}  {_short:<22} n={_rn:<4} MRR={_mrr_str}  p95={_rp95:.0f}ms  p100={_rp100:.0f}ms",
        file=sys.stderr,
    )


# ── Delta vs previous same-mode run ──────────────────────────────────────────
def _record_overall_mrr(record: dict) -> float:
    if "overall_mrr" in record:
        return float(record["overall_mrr"])
    golds = record.get("golds")
    if isinstance(golds, dict) and golds:
        return float(_weighted_gold_summary(golds)["mrr"])
    return float(record.get("mrr") or 0.0)


if _prev:
    print("", file=sys.stderr)
    _pmrr = _record_overall_mrr(_prev)
    _cmrr = float(_overall["mrr"])
    _dmrr = _cmrr - _pmrr
    _sign = "+" if _dmrr >= 0 else ""
    print(
        f"  vs {_prev['ts'][:16]} [{_prev['mode']}]  overall MRR {_pmrr:.4f} → {_cmrr:.4f}  ({_sign}{_dmrr:.4f})",
        file=sys.stderr,
    )
    _pall = _record_overall_mrr(_prev)
    _call = float(_overall["mrr"])
    _dall = _call - _pall
    _sign_all = "+" if _dall >= 0 else ""
    print(f"     all-gold MRR {_pall:.4f} → {_call:.4f}  ({_sign_all}{_dall:.4f})", file=sys.stderr)
    # per-repo deltas — only show movers
    _by_now = out.get("by_repo", {}) or {}
    _by_prev = _prev.get("by_repo", {}) or {}
    _movers = []
    for _rname in set(_by_now) | set(_by_prev):
        _cm = (_by_now.get(_rname) or {}).get("mrr", 0)
        _pm = (_by_prev.get(_rname) or {}).get("mrr", 0)
        if _cm != _pm:
            _movers.append((_rname.split("__")[-1], _pm, _cm, _cm - _pm))
    _movers.sort(key=lambda x: x[3])
    for _rn2, _pm, _cm, _dd in _movers:
        _sign2 = "+" if _dd >= 0 else ""
        print(f"    {_rn2:<22}  {_pm:.3f} → {_cm:.3f}  ({_sign2}{_dd:.3f})", file=sys.stderr)
print("─" * 60 + "\n", file=sys.stderr)
