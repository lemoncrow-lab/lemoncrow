"""A/B benchmark: code op=files vs native recursive file listing."""

from __future__ import annotations

import json
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
    (root / "src" / "atelier").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)

    (root / "src" / "atelier" / "auth.py").write_text(
        "class AuthService:\n" "    def login(self, email: str) -> bool:\n" "        return bool(email)\n",
        encoding="utf-8",
    )
    (root / "src" / "atelier" / "routes.py").write_text(
        "def route_login() -> str:\n" "    return '/login'\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_auth.py").write_text(
        "from src.atelier.auth import AuthService\n\n"
        "def test_login() -> None:\n"
        "    assert AuthService().login('x')\n",
        encoding="utf-8",
    )
    (root / "docs" / "readme.md").write_text("# Fixture\n", encoding="utf-8")
    for idx in range(1, 10):
        (root / "src" / "atelier" / f"module_{idx}.py").write_text(
            "def run() -> str:\n" f"    return 'module_{idx}'\n",
            encoding="utf-8",
        )
        (root / "tests" / f"test_module_{idx}.py").write_text(
            f"from src.atelier.module_{idx} import run\n\n"
            "def test_run() -> None:\n"
            f"    assert run() == 'module_{idx}'\n",
            encoding="utf-8",
        )


def _baseline_recursive_listing(repo_root: Path) -> str:
    items = sorted(path for path in repo_root.rglob("*") if path.is_file())
    records: list[dict[str, object]] = []
    for path in items:
        rel = str(path.relative_to(repo_root))
        stat = path.stat()
        preview = path.read_text(encoding="utf-8", errors="replace")[:160]
        records.append(
            {
                "path": rel,
                "absolute_path": str(path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "preview": preview,
            }
        )
    return json.dumps(records, sort_keys=True)


def test_code_files_ab_real(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    t0 = time.perf_counter()
    native_text = _baseline_recursive_listing(tmp_path)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = engine.tool_files(format="tree", budget_tokens=4000)
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = json.dumps(payload, sort_keys=True, default=str)

    native_tokens = _count_tiktoken(native_text)
    atelier_tokens = _count_tiktoken(atelier_text)
    row = ABRow(
        tool="code.files",
        mode="tree",
        native_tool="python_rglob_file_inventory",
        native_tokens=native_tokens,
        atelier_tokens=atelier_tokens,
        tokens_saved_measured=max(0, native_tokens - atelier_tokens),
        token_ratio=(atelier_tokens / native_tokens) if native_tokens else None,
        native_ms=round(native_ms, 3),
        atelier_ms=round(atelier_ms, 3),
        ts=time.time(),
    )
    _append_row(row)

    assert payload["format"] == "tree"
    assert payload["file_count"] >= 21
    assert "files" in payload
    assert atelier_tokens < native_tokens
