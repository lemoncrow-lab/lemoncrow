from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from lemoncrow.core.foundation.history_store import HistoryStore
from lemoncrow.core.foundation.memory_models import ArchivalPassage, MemoryBlock
from lemoncrow.core.foundation.models import Trace
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore
from lemoncrow.pro.capabilities.archival_recall.symbol_recall import SymbolRecallCapability
from lemoncrow.pro.capabilities.code_context import CodeContextEngine


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "auth.py").write_text(
        "class AuthService:\n"
        "    def verify_session(self, token: str) -> bool:\n"
        "        return token.startswith('session:')\n"
        "\n"
        "class BasicAuth:\n"
        "    def verify(self, header: str) -> bool:\n"
        "        return header.startswith('basic ')\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_auth.py").write_text(
        "from src.auth import AuthService\n\n"
        "def test_verify_session_accepts_session_tokens() -> None:\n"
        "    assert AuthService().verify_session('session:ok') is True\n",
        encoding="utf-8",
    )
    (root / "docs" / "decisions" / "001-session-auth.md").write_text(
        "# Session auth\n\nWe keep AuthService.verify_session as the validation seam for session tokens.\n",
        encoding="utf-8",
    )
    (root / "docs" / "decisions" / "002-basic-auth.md").write_text(
        "# Basic auth\n\nBasicAuth stays on header parsing and should not change session token validation.\n",
        encoding="utf-8",
    )


def _seed_symbol_recall(tmp_path: Path) -> tuple[SymbolRecallCapability, str]:
    repo_root = tmp_path / "repo"
    lemoncrow_root = tmp_path / ".lemoncrow"
    _write_fixture_repo(repo_root)

    engine = CodeContextEngine(repo_root)
    engine.index_repo()
    symbol_id = engine.search_symbols("AuthService.verify_session", limit=1)[0].symbol_id

    memory_store = SqliteMemoryStore(lemoncrow_root)
    memory_store.upsert_block(
        MemoryBlock(
            agent_id="shared",
            label="edits/auth-verify-session",
            value="Tightened AuthService.verify_session after a stale-session incident.",
            metadata={"symbol_id": symbol_id, "origin": "edit"},
        ),
        actor="agent:test",
        reason="seed symbol memory",
    )
    memory_store.insert_passage(
        ArchivalPassage(
            agent_id="shared",
            text="AuthService.verify_session rejected stale session tokens during the 2026 incident review.",
            tags=[f"symbol:{symbol_id}", "incident"],
            source="user",
            source_ref="postmortem/auth.md",
            dedup_hash="recall-symbol-passage",
        )
    )

    trace_store = HistoryStore(lemoncrow_root)
    trace_store.init()
    trace_store.record_trace(
        Trace(
            id=Trace.make_id("auth trace", "gsd-executor"),
            agent="gsd-executor",
            domain="code-intel",
            task="Validate AuthService.verify_session recall",
            status="success",
            files_touched=["src/auth.py"],
            diff_summary="Updated AuthService.verify_session token validation",
            output_summary="Confirmed AuthService.verify_session only accepts session tokens.",
            created_at=datetime.now(UTC),
        )
    )

    capability = SymbolRecallCapability(
        repo_root=repo_root,
        engine=engine,
        memory_store=memory_store,
        trace_store=trace_store,
    )
    return capability, symbol_id


def test_recall_symbol_defaults_to_definition_and_memory_bundle(tmp_path: Path) -> None:
    capability, symbol_id = _seed_symbol_recall(tmp_path)

    payload = capability.recall_symbol(query="AuthService.verify_session", agent_id="shared")

    assert payload["included"] == ["definition", "memory"]
    assert payload["definition"]["symbol_id"] == symbol_id
    assert payload["definition"]["qualified_name"] == "AuthService.verify_session"
    assert payload["definition"]["source"].startswith("def verify_session")
    assert [item["item_type"] for item in payload["memory"]] == ["block", "passage"]
    assert payload["memory"][0]["metadata"]["symbol_id"] == symbol_id
    assert payload["memory"][1]["tags"] == [f"symbol:{symbol_id}", "incident"]
    assert "traces" not in payload
    assert "decisions" not in payload
    assert "tests" not in payload


def test_recall_symbol_keeps_definition_when_heavier_sections_expand_under_budget(tmp_path: Path) -> None:
    capability, _ = _seed_symbol_recall(tmp_path)

    payload = capability.recall_symbol(
        query="AuthService.verify_session",
        agent_id="shared",
        include=["traces", "decisions", "tests"],
        budget_tokens=260,
    )

    assert payload["definition"]["qualified_name"] == "AuthService.verify_session"
    assert payload["total_tokens"] <= 260
    assert "definition" in payload["included"]
    assert "memory" in payload["included"]
    assert payload["truncated_sections"]
    assert any(section in {"traces", "decisions", "tests"} for section in payload["truncated_sections"])


def test_recall_symbol_uses_symbol_boundaries_for_decisions_and_limits_related_tests(tmp_path: Path) -> None:
    capability, _ = _seed_symbol_recall(tmp_path)

    payload = capability.recall_symbol(
        query="AuthService.verify_session",
        agent_id="shared",
        include=["decisions", "tests"],
        budget_tokens=1200,
    )

    assert [item["path"] for item in payload["decisions"]] == ["docs/decisions/001-session-auth.md"]
    assert all(item["file_path"].startswith("tests/") for item in payload["tests"])
    assert [item["qualified_name"] for item in payload["tests"]] == ["test_verify_session_accepts_session_tokens"]
