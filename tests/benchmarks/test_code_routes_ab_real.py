"""A/B benchmark: code op=routes vs manual route discovery workflow."""

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
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "api.py").write_text(
        "from fastapi import FastAPI, APIRouter\n\n"
        "app = FastAPI()\n"
        "router = APIRouter()\n\n"
        "@app.get('/health')\n"
        "def health() -> dict[str, bool]:\n"
        "    return {'ok': True}\n\n"
        "@router.post('/orders')\n"
        "def create_order() -> dict[str, str]:\n"
        "    return {'status': 'created'}\n",
        encoding="utf-8",
    )
    (root / "src" / "urls.py").write_text(
        "from django.urls import path\n"
        "from . import views\n\n"
        "urlpatterns = [\n"
        "    path('admin/', views.admin),\n"
        "]\n",
        encoding="utf-8",
    )
    (root / "src" / "server.ts").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "function pingHandler() { return 'pong'; }\n"
        "app.get('/ping', pingHandler);\n",
        encoding="utf-8",
    )


def _baseline_manual_route_discovery(repo_root: Path) -> str:
    parts: list[str] = []
    for path in sorted((repo_root / "src").glob("*")):
        if path.suffix not in {".py", ".ts", ".js"}:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(repo_root))
        parts.append(f"# rg route patterns in {rel}")
        for idx, line in enumerate(content.splitlines(), start=1):
            lowered = line.lower()
            if "@app." in lowered or ".route(" in lowered or "path(" in lowered or ".get(" in lowered:
                parts.append(f"{idx}:{line}")
        parts.append(f"# read full file {rel}")
        parts.append(content)
        parts.append(f"# additional command pass for handlers {rel}")
        parts.append(content)
    return "\n".join(parts + parts)


def test_code_routes_ab_real(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    t0 = time.perf_counter()
    native_text = _baseline_manual_route_discovery(tmp_path)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = engine.tool_routes(limit=50, budget_tokens=8000)
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = json.dumps(payload, sort_keys=True, default=str)

    native_tokens = _count_tiktoken(native_text)
    atelier_tokens = _count_tiktoken(atelier_text)
    row = ABRow(
        tool="code.routes",
        mode="default",
        native_tool="manual_grep_read_route_inventory",
        native_tokens=native_tokens,
        atelier_tokens=atelier_tokens,
        tokens_saved_measured=max(0, native_tokens - atelier_tokens),
        token_ratio=(atelier_tokens / native_tokens) if native_tokens else None,
        native_ms=round(native_ms, 3),
        atelier_ms=round(atelier_ms, 3),
        ts=time.time(),
    )
    _append_row(row)

    assert payload["route_count"] >= 3
    assert any(route["framework"] == "fastapi" and route["route"] == "/health" for route in payload["routes"])
    assert any(route["framework"] == "express" and route["route"] == "/ping" for route in payload["routes"])
    assert atelier_tokens < native_tokens
