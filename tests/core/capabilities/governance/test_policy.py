from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from atelier.core.capabilities.governance import (
    GovernancePolicy,
    RedactionRule,
    load_policy,
    record_within_retention,
    redact_record,
    save_policy,
)


def test_policy_redacts_and_filters_by_retention(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    policy = GovernancePolicy(
        retention_days={"memory": 1},
        redaction_rules=[RedactionRule(pattern="secret", replacement="[MASKED]")],
    )
    save_policy(root, policy)
    loaded = load_policy(root)
    old_record = {"at": (datetime.now(UTC) - timedelta(days=3)).isoformat(), "content": "secret token"}
    new_record = {"at": datetime.now(UTC).isoformat(), "content": "secret token"}

    assert loaded.retention_days["memory"] == 1
    assert record_within_retention(old_record, record_type="memory", policy=loaded) is False
    assert redact_record(new_record, loaded)["content"] == "[MASKED] token"
