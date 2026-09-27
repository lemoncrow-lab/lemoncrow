"""The public ``lc mcp`` entrypoint is only a compatibility alias for the thin client."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli


def test_lc_mcp_delegates_to_thin_client(monkeypatch) -> None:
    calls: list[bool] = []

    def fake_serve() -> int:
        calls.append(True)
        return 0

    monkeypatch.setattr("lemoncrow_client.mcpserver.serve", fake_serve)
    result = CliRunner().invoke(cli, ["mcp"])

    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_lc_mcp_root_maps_to_thin_client_home(monkeypatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_serve() -> int:
        import os

        seen.append(os.environ.get("LEMONCROW_HOME", ""))
        return 0

    monkeypatch.setattr("lemoncrow_client.mcpserver.serve", fake_serve)
    root = tmp_path / "state"
    result = CliRunner().invoke(cli, ["mcp", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert seen == [str(root)]


def test_legacy_local_mcp_runtime_is_absent() -> None:
    repo = Path(__file__).resolve().parents[2]
    adapters = repo / "src" / "lemoncrow" / "gateway" / "adapters"
    assert not (adapters / "mcp_bridge.py").exists()
    assert not (adapters / "mcp_daemon.py").exists()

    runtime_sources = [repo / "src" / "lemoncrow" / "gateway" / "cli" / "commands" / "mcp.py"]
    assert not (repo / "src" / "lemoncrow" / "infra" / "runtime" / "servicectl_lifecycle.py").exists()
    assert not (repo / "src" / "lemoncrow" / "infra" / "runtime" / "stack_lifecycle.py").exists()
    text = "\n".join(path.read_text(encoding="utf-8") for path in runtime_sources)
    assert "LEMONCROW_MCP_SINGLETON" not in text
    assert "mcp_daemon" not in text
    assert "mcp_bridge" not in text
