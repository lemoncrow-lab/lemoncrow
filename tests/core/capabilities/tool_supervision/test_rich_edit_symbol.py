from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.tool_supervision.rich_edit import apply_rich_edits
from atelier.gateway.adapters.mcp_server import _memory_get_block


def _write_symbol_fixture(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "service.py").write_text(
        "class AuthService:\n"
        "    def verify(self, token: str) -> bool:\n"
        "        return token == 'ok'\n",
        encoding="utf-8",
    )


def test_symbol_edit_requires_disambiguation_when_name_is_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "class AuthService:\n"
        "    def verify(self, token: str) -> bool:\n"
        "        return token == 'ok'\n"
        "\n"
        "def verify(token: str) -> bool:\n"
        "    return token == 'ok'\n",
        encoding="utf-8",
    )

    result = apply_rich_edits(
        [{"kind": "symbol", "name": "verify", "mode": "replace", "new_body": "def verify(token: str) -> bool:\n    return False"}],
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is True
    assert result["failed"][0]["error"] == "disambiguation_required"
    assert len(result["failed"][0]["matches"]) == 2


def test_symbol_edit_stale_symbol_id_fails_without_writing(tmp_path: Path) -> None:
    _write_symbol_fixture(tmp_path)
    engine = CodeContextEngine(tmp_path)
    engine.index_repo()
    symbol_id = engine.search_symbols("AuthService.verify", limit=1)[0].symbol_id
    target = tmp_path / "src" / "service.py"
    target.write_text(
        "class AuthService:\n"
        "    def verify_token(self, token: str) -> bool:\n"
        "        return token == 'ok'\n",
        encoding="utf-8",
    )

    result = apply_rich_edits(
        [{"kind": "symbol", "symbol_id": symbol_id, "mode": "replace", "new_body": "def verify(self, token: str) -> bool:\n    return False"}],
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is True
    assert result["failed"][0]["error"] in {"stale_target", "symbol_not_found"}
    assert "verify_token" in target.read_text(encoding="utf-8")


def test_symbol_edit_reindexes_and_tags_memory_on_success(tmp_path: Path) -> None:
    _write_symbol_fixture(tmp_path)

    result = apply_rich_edits(
        [
            {
                "kind": "symbol",
                "name": "AuthService.verify",
                "mode": "replace",
                "new_body": (
                    "def verify(self, token: str) -> bool:\n"
                    "    return token.startswith('ok')"
                ),
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    updated = (tmp_path / "src" / "service.py").read_text(encoding="utf-8")
    assert "startswith('ok')" in updated

    engine = CodeContextEngine(tmp_path)
    payload = engine.get_symbol(qualified_name="AuthService.verify", file_path="src/service.py")
    assert "startswith('ok')" in payload["source"]

    symbol_id = result["applied"][0]["symbol_id"]
    memory = _memory_get_block(agent_id="shared", label=f"edits/{symbol_id}")
    assert memory is not None
    assert memory["value"]
    assert memory["metadata"]["symbol_id"] == symbol_id
