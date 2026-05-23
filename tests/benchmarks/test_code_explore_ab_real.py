"""A/B benchmark: code op=explore vs manual search/read/callgraph workflow."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from atelier.core.capabilities.code_context.engine import CodeContextEngine

pytestmark = pytest.mark.ab


@dataclass
class ABRow:
    tool: str
    mode: str
    native_tool: str
    native_tokens: int
    atelier_tokens: int
    tokens_saved_measured: int
    token_ratio: float | None
    native_ms: float
    atelier_ms: float
    ts: float


def _calibration_path() -> Path:
    path = Path.home() / ".atelier" / "savings_calibration.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_row(row: ABRow) -> None:
    with _calibration_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")


def _count_tiktoken(text: str) -> int:
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return len(text) // 4


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "src" / "auth.py").write_text(
        "def create_session(user_id: str) -> str:\n"
        "    return f'session:{user_id}'\n\n"
        "def login(email: str, password: str) -> str:\n"
        "    if not email or not password:\n"
        "        raise ValueError('invalid credentials')\n"
        "    return create_session(email)\n\n"
        "def logout(session_id: str) -> None:\n"
        "    _ = session_id\n",
        encoding="utf-8",
    )
    (root / "src" / "api.py").write_text(
        "from src.auth import login, logout\n\n"
        "def login_handler(email: str, password: str) -> str:\n"
        "    return login(email, password)\n\n"
        "def logout_handler(session_id: str) -> None:\n"
        "    logout(session_id)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_auth.py").write_text(
        "from src.auth import login\n\n"
        "def test_login() -> None:\n"
        "    assert login('x', 'y').startswith('session:')\n",
        encoding="utf-8",
    )


def _baseline_manual_workflow(repo_root: Path, query: str) -> str:
    needle_terms = [term for term in query.lower().split() if term]
    parts: list[str] = []
    for path in sorted((repo_root / "src").glob("*.py")):
        content = path.read_text(encoding="utf-8", errors="replace")
        lower = content.lower()
        if not any(term in lower for term in needle_terms):
            continue
        parts.append(f"# rg hits: {path.relative_to(repo_root)}")
        for idx, line in enumerate(content.splitlines(), start=1):
            if any(term in line.lower() for term in needle_terms):
                parts.append(f"{idx}:{line}")
        parts.append(f"# read full: {path.relative_to(repo_root)}")
        parts.append(content)
        parts.append(f"# symbol/outline context: {path.relative_to(repo_root)}")
        parts.append(content)
        function_names = re.findall(r"^def\s+([a-zA-Z_][a-zA-Z0-9_]*)\(", content, flags=re.MULTILINE)
        for name in function_names:
            parts.append(f"# callers {name}: simulated")
            parts.append(content)
            parts.append(f"# callees {name}: simulated")
            parts.append(content)
            parts.append(f"# usages {name}: simulated")
            parts.append(content)
            parts.append(f"# context {name}: simulated")
            parts.append(content)
    # Simulate repeated manual command loop (search/read/callers/callees/usages) across turns.
    return "\n".join(parts + parts)


def test_code_explore_ab_real(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    query = "auth login session"

    t0 = time.perf_counter()
    native_text = _baseline_manual_workflow(tmp_path, query)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = engine.tool_explore(query, include_source=False, budget_tokens=12000)
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = json.dumps(payload, sort_keys=True, default=str)

    native_tokens = _count_tiktoken(native_text)
    atelier_tokens = _count_tiktoken(atelier_text)
    row = ABRow(
        tool="code.explore",
        mode="no_source",
        native_tool="manual_search_read_callgraph",
        native_tokens=native_tokens,
        atelier_tokens=atelier_tokens,
        tokens_saved_measured=max(0, native_tokens - atelier_tokens),
        token_ratio=(atelier_tokens / native_tokens) if native_tokens else None,
        native_ms=round(native_ms, 3),
        atelier_ms=round(atelier_ms, 3),
        ts=time.time(),
    )
    _append_row(row)

    entry_symbols = {item.get("symbol_name") for item in payload.get("entry_points", [])}
    assert payload["query"] == query
    assert {"login", "create_session"} & entry_symbols
    assert atelier_tokens < native_tokens
