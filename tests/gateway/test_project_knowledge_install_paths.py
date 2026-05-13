from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_stdio_wrapper_defaults_to_service_and_project_knowledge() -> None:
    content = (ROOT / "scripts" / "atelier_mcp_stdio.sh").read_text(encoding="utf-8")

    assert 'export ATELIER_SERVICE_URL="http://127.0.0.1:8787"' in content
    assert 'export ATELIER_ROOT="${HOME}/.atelier"' not in content
    assert 'export ATELIER_KNOWLEDGE_ROOT="${ATELIER_WORKSPACE_ROOT}/.knowledge"' in content


def test_codex_wrapper_defaults_to_service_backed_mcp_and_project_knowledge() -> None:
    content = (ROOT / "scripts" / "install_codex.sh").read_text(encoding="utf-8")

    assert "ATELIER_MCP_MODE" not in content
    assert 'export ATELIER_ROOT="\\${HOME}/.atelier"' not in content
    assert 'export ATELIER_SERVICE_URL="http://127.0.0.1:8787"' in content
    assert 'export ATELIER_KNOWLEDGE_ROOT="\\${ATELIER_WORKSPACE_ROOT}/.knowledge"' in content
