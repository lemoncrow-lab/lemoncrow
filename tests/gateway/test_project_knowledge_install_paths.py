from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_mcp_server_defaults_to_service_and_project_knowledge() -> None:
    content = (ROOT / "src" / "atelier" / "gateway" / "adapters" / "mcp_server.py").read_text(encoding="utf-8")

    assert 'os.environ.setdefault("ATELIER_SERVICE_URL", "http://127.0.0.1:8787")' in content
    assert 'os.environ.setdefault("ATELIER_WORKSPACE_ROOT", os.getcwd())' in content
    assert (
        'os.environ.setdefault("ATELIER_KNOWLEDGE_ROOT", os.path.join(os.environ["ATELIER_WORKSPACE_ROOT"], ".knowledge"))'
        in content
    )


def test_codex_installer_defaults_to_service_backed_mcp_and_project_knowledge() -> None:
    content = (ROOT / "scripts" / "install_codex.sh").read_text(encoding="utf-8")

    assert "ATELIER_MCP_MODE" not in content
    assert 'export ATELIER_ROOT="\\${HOME}/.atelier"' not in content
    # Codex installer now relies on atelier-mcp defaults for service URL and knowledge root
    assert 'ATELIER_SERVICE_URL="http://127.0.0.1:8787"' not in content
