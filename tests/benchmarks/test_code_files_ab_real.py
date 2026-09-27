"""A/B benchmark: code op=files vs native recursive file listing."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

pytestmark = [pytest.mark.ab, pytest.mark.slow]


@dataclass
class ABRow:
    tool: str
    mode: str
    native_tool: str
    native_tokens: int
    lemoncrow_tokens: int
    tokens_saved_measured: int
    token_ratio: float | None
    native_ms: float
    lemoncrow_ms: float
    ts: float


def _calibration_path() -> Path:
    path = Path.home() / ".lemoncrow" / "savings_calibration.jsonl"
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
    except (ImportError, ValueError):
        return len(text) // 4


def _write_fixture_repo(root: Path) -> None:
    (root / "src" / "lemoncrow").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)

    (root / "src" / "lemoncrow" / "auth.py").write_text(
        "class AuthService:\n    def login(self, email: str) -> bool:\n        return bool(email)\n",
        encoding="utf-8",
    )
    (root / "src" / "lemoncrow" / "routes.py").write_text(
        "def route_login() -> str:\n    return '/login'\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_auth.py").write_text(
        "from src.lemoncrow.auth import AuthService\n\ndef test_login() -> None:\n    assert AuthService().login('x')\n",
        encoding="utf-8",
    )
    (root / "docs" / "readme.md").write_text("# Fixture\n", encoding="utf-8")
    for idx in range(1, 10):
        (root / "src" / "lemoncrow" / f"module_{idx}.py").write_text(
            f"def run() -> str:\n    return 'module_{idx}'\n",
            encoding="utf-8",
        )
        (root / "tests" / f"test_module_{idx}.py").write_text(
            f"from src.lemoncrow.module_{idx} import run\n\n"
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
    engine.index_repo()

    t0 = time.perf_counter()
    native_text = _baseline_recursive_listing(tmp_path)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = engine.tool_files(format="tree", budget_tokens=4000)
    lemoncrow_ms = (time.perf_counter() - t1) * 1000.0
    lemoncrow_text = json.dumps(payload, sort_keys=True, default=str)

    native_tokens = _count_tiktoken(native_text)
    lemoncrow_tokens = _count_tiktoken(lemoncrow_text)
    row = ABRow(
        tool="code.files",
        mode="tree",
        native_tool="python_rglob_file_inventory",
        native_tokens=native_tokens,
        lemoncrow_tokens=lemoncrow_tokens,
        tokens_saved_measured=max(0, native_tokens - lemoncrow_tokens),
        token_ratio=(lemoncrow_tokens / native_tokens) if native_tokens else None,
        native_ms=round(native_ms, 3),
        lemoncrow_ms=round(lemoncrow_ms, 3),
        ts=time.time(),
    )
    _append_row(row)

    assert payload["format"] == "tree"
    assert payload["file_count"] >= 21
    assert "files" in payload
    assert lemoncrow_tokens < native_tokens
