from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.store import ContextStore
from atelier.gateway.cli import app as cli_module
from atelier.gateway.cli import cli
from atelier.infra.internal_llm.ollama_client import OllamaUnavailable


def test_cli_reembed_rewrites_legacy_archival_passage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".atelier"
    store = ContextStore(root)
    store.init()
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO archival_passage (
                id, agent_id, text, embedding, embedding_model, embedding_provenance,
                tags, source, source_ref, dedup_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pas-legacy",
                "atelier:code",
                "legacy checkout retry note",
                json.dumps([0.0] * 32).encode("utf-8"),
                "stub",
                "legacy_stub",
                "[]",
                "user",
                "",
                "legacy-hash",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    monkeypatch.delenv("ATELIER_EMBEDDER", raising=False)
    runner = CliRunner()

    result = runner.invoke(cli, ["--root", str(root), "reembed", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["archival_passage"] == 1
    with store._connect() as conn:
        row = conn.execute(
            "SELECT embedding, embedding_provenance FROM archival_passage WHERE id = ?",
            ("pas-legacy",),
        ).fetchone()
    assert row["embedding_provenance"] != "legacy_stub"
    assert len(json.loads(bytes(row["embedding"]).decode("utf-8"))) != 32

    second = runner.invoke(cli, ["--root", str(root), "reembed", "--json"])

    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["archival_passage"] == 0


def test_cli_consolidate_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".atelier"
    store = ContextStore(root)
    store.init()
    for block_id in ["rb-one", "rb-two"]:
        store.upsert_block(
            ReasonBlock(
                id=block_id,
                title="Checkout retry timeout",
                domain="testing",
                situation="When checkout retries fail with timeout during webhook delivery",
                triggers=["checkout", "retry", "timeout"],
                procedure=["Inspect retry budget", "Verify idempotency key"],
                failure_signals=["timeout", "duplicate delivery"],
            ),
            write_markdown=False,
        )
    monkeypatch.setattr(
        "atelier.core.capabilities.consolidation.worker.chat",
        lambda messages, json_schema=None: (_ for _ in ()).throw(OllamaUnavailable("offline")),
    )

    result = CliRunner().invoke(
        cli,
        ["--root", str(root), "consolidate", "--since", "1d", "--dry-run", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["duplicates"] == 1
    assert payload["written"] == 0


def test_cli_letta_commands_route_to_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module, "_run_compose", lambda args: calls.append(args))
    runner = CliRunner()

    up = runner.invoke(cli, ["letta", "up"])
    down = runner.invoke(cli, ["letta", "down"])
    reset = runner.invoke(cli, ["letta", "reset", "--yes"])

    assert up.exit_code == 0, up.output
    assert down.exit_code == 0, down.output
    assert reset.exit_code == 0, reset.output
    assert calls == [["up", "-d"], ["down"], ["down", "-v"]]


def test_cli_letta_reset_requires_confirmation() -> None:
    result = CliRunner().invoke(cli, ["letta", "reset"])
    assert result.exit_code != 0
    assert "without --yes" in result.output
