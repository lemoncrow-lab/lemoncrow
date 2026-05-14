from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from atelier.core.foundation.models import ConsolidationCandidate
from atelier.core.foundation.store import ContextStore
from atelier.gateway.adapters.cli import cli


def test_consolidation_inbox_and_decide_are_cli_only(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    store = ContextStore(root)
    store.init()
    candidate = ConsolidationCandidate(
        id="cc-test",
        kind="duplicate_cluster",
        affected_block_ids=["rb-one", "rb-two"],
        proposed_action="merge",
        proposed_body="Merged checkout retry guidance.",
        evidence={"method": "unit"},
    )
    store.upsert_consolidation_candidate(candidate)

    runner = CliRunner()
    inbox = runner.invoke(cli, ["--root", str(root), "consolidation", "inbox", "--limit", "10", "--json"])
    assert inbox.exit_code == 0, inbox.output
    payload = json.loads(inbox.output)
    assert [item["id"] for item in payload["candidates"]] == ["cc-test"]

    decided = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "consolidation",
            "decide",
            "cc-test",
            "approved",
            "--reviewer",
            "tests",
            "--json",
        ],
    )
    assert decided.exit_code == 0, decided.output
    decision = json.loads(decided.output)
    assert decision["decision"] == "approved"
    assert decision["decided_by"] == "tests"
    assert decision["decided_at"] is not None
