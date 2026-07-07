from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from atelier.core.capabilities.governance import (
    GovernancePolicy,
    RedactionRule,
    default_policy,
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


def test_default_policy_redacts_api_key_and_token() -> None:
    policy = default_policy()

    # Generic api-key assignment must be redacted (regression: double-escaped \\s).
    api_key_record = {"content": "api_key: hunter2supersecretvalue"}
    sk_record = {"content": "bearer sk-ABC123def456token"}

    redacted_api = redact_record(api_key_record, policy)["content"]
    redacted_sk = redact_record(sk_record, policy)["content"]

    assert "hunter2supersecretvalue" not in redacted_api
    assert "[REDACTED]" in redacted_api
    assert "sk-ABC123def456token" not in redacted_sk
    assert "[REDACTED]" in redacted_sk
