from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from click.testing import CliRunner, Result

from lemoncrow.core.foundation.memory_models import MemoryBlock
from lemoncrow.gateway.cli import cli
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore
from lemoncrow.pro.capabilities.cross_vendor_memory.audit_log import MemoryAuditLog
from lemoncrow.pro.capabilities.cross_vendor_memory.models import AuditEvent


def _invoke(root: Path, *args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args])


def _write_done_session(root: Path, user_id: str, *, cost: float) -> None:
    runs = root / "sessions" / "run-1"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run.json").write_text(
        json.dumps(
            {
                "session_id": "run-1",
                "status": "done",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                "agent_settings": {"user_id": user_id},
                "cost": {"total_cost_usd": cost},
                "events": [],
            }
        ),
        encoding="utf-8",
    )


def test_team_usage_and_memory_share_commands(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init = _invoke(root, "team", "init", "--name", "Acme", "--admin-email", "admin@example.com", "--json")
    assert init.exit_code == 0, init.output
    _write_done_session(root, "admin@example.com", cost=1.25)
    SqliteMemoryStore(root).upsert_block(
        MemoryBlock(agent_id="lemoncrow:code", label="fact", value="remember this"),
        actor="test",
    )

    shared = _invoke(root, "memory", "share", "--agent-id", "lemoncrow:code", "--label", "fact", "--json")
    usage = _invoke(root, "team", "usage", "--since", "30d", "--json")

    assert shared.exit_code == 0, shared.output
    assert json.loads(shared.output)["scope"] == "shared"
    assert usage.exit_code == 0, usage.output
    assert json.loads(usage.output)["total_cost_usd"] == 1.25


def test_governance_apply_and_audit_export_verify_commands(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init = _invoke(root, "team", "init", "--name", "Acme", "--admin-email", "admin@example.com")
    assert init.exit_code == 0, init.output
    policy_path = tmp_path / "governance.yaml"
    policy_path.write_text(
        yaml.safe_dump({"retention_days": {"memory": 30}, "redaction_rules": [{"pattern": "secret"}]}),
        encoding="utf-8",
    )
    applied = _invoke(root, "governance", "apply", "--file", str(policy_path), "--json")
    assert applied.exit_code == 0, applied.output
    MemoryAuditLog(root).append(
        AuditEvent(
            vendor="claude",
            event="added",
            fact_id="fact-1",
            source_file="memory.md",
            source_line=1,
            content="secret value",
        )
    )
    _write_done_session(root, "admin@example.com", cost=0.5)
    bundle_dir = tmp_path / "bundle"

    exported = _invoke(root, "audit", "export", "--since", "30d", "--out", str(bundle_dir), "--json")
    verified = _invoke(root, "audit", "verify", str(bundle_dir), "--json")

    assert exported.exit_code == 0, exported.output
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["valid"] is True
