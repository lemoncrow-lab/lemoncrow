from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_mcp_server_keeps_remote_service_opt_in_and_project_lessons() -> None:
    content = (ROOT / "src" / "lemoncrow" / "gateway" / "adapters" / "mcp_server.py").read_text(encoding="utf-8")

    assert 'os.environ.setdefault("LEMONCROW_SERVICE_URL"' not in content
    assert 'os.environ.setdefault("LEMONCROW_WORKSPACE_ROOT", os.getcwd())' in content
    assert '"LEMONCROW_LESSONS_ROOT"' in content
    assert 'os.path.join(os.environ["LEMONCROW_WORKSPACE_ROOT"], ".lemoncrow/lessons")' in content


def test_codex_installer_relies_on_thin_mcp_and_project_lessons() -> None:
    content = (ROOT / "scripts" / "install_codex.sh").read_text(encoding="utf-8")

    assert "LEMONCROW_MCP_MODE" not in content
    assert 'export LEMONCROW_ROOT="\\${HOME}/.lemoncrow"' not in content
    # Codex installer must not pin the optional legacy service URL; lessons stay project-scoped
    assert 'LEMONCROW_SERVICE_URL="http://127.0.0.1:8787"' not in content
