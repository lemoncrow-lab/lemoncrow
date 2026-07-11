"""Governance policy storage plus retention/redaction helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class RedactionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str
    replacement: str = "[REDACTED]"


class GovernancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    retention_days: dict[str, int] = Field(
        default_factory=lambda: {
            "memory": 90,
            "runs": 30,
            "live_savings": 30,
            "team_audit": 365,
            "lessons": 365,
        }
    )
    redaction_rules: list[RedactionRule] = Field(default_factory=list)


def policy_path(root: Path | str) -> Path:
    return Path(root).expanduser().resolve() / "governance.yaml"


def default_policy() -> GovernancePolicy:
    return GovernancePolicy(
        redaction_rules=[
            RedactionRule(pattern=r"sk-[A-Za-z0-9]+"),
            RedactionRule(pattern=r"(?i)api[_-]?key\s*[:=]\s*\S+"),
        ]
    )


def load_policy(root: Path | str) -> GovernancePolicy:
    path = policy_path(root)
    if not path.exists():
        return default_policy()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return GovernancePolicy.model_validate(data)


def save_policy(root: Path | str, policy: GovernancePolicy) -> GovernancePolicy:
    path = policy_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(policy.model_dump(mode="python"), sort_keys=True),
        encoding="utf-8",
    )
    return policy


def record_within_retention(
    record: dict[str, Any],
    *,
    record_type: str,
    policy: GovernancePolicy,
    now: datetime | None = None,
) -> bool:
    retention_days = policy.retention_days.get(record_type)
    if retention_days is None:
        return True
    timestamp = _record_timestamp(record)
    if timestamp is None:
        return True
    current = now or datetime.now(UTC)
    return timestamp >= current - timedelta(days=retention_days)


def redact_record(record: Any, policy: GovernancePolicy) -> Any:
    import re

    if isinstance(record, str):
        redacted = record
        for rule in policy.redaction_rules:
            redacted = re.sub(rule.pattern, rule.replacement, redacted)
        return redacted
    if isinstance(record, list):
        return [redact_record(item, policy) for item in record]
    if isinstance(record, dict):
        return {key: redact_record(value, policy) for key, value in record.items()}
    return record


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    for key in ("at", "updated_at", "created_at", "captured_at", "last_applied_at"):
        raw = record.get(key)
        if not raw or not isinstance(raw, str):
            continue
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    ts = record.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return datetime.fromtimestamp(float(ts), tz=UTC)
    return None
