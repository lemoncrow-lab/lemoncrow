from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atelier.core.capabilities.audit_export import export_audit_bundle, verify_audit_bundle
from atelier.core.capabilities.cross_vendor_memory.audit_log import MemoryAuditLog
from atelier.core.capabilities.cross_vendor_memory.models import AuditEvent
from atelier.core.capabilities.lesson_promotion.models import TypedLesson
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore
from atelier.core.capabilities.team import TeamWorkspaceManager


def test_export_bundle_redacts_and_verifies_then_detects_tampering(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    manager = TeamWorkspaceManager(root)
    workspace = manager.init_workspace(name="Acme", admin_email="admin@example.com")
    MemoryAuditLog(root).append(
        AuditEvent(
            vendor="claude",
            event="added",
            fact_id="fact-1",
            source_file="memory.md",
            source_line=1,
            content="api_key=sk-secret123",
        )
    )
    (root / "live_savings_events.jsonl").write_text(
        json.dumps({"kind": "model_recommendation", "at": datetime.now(UTC).isoformat(), "configured": True}) + "\n",
        encoding="utf-8",
    )
    TypedLessonStore(root).upsert_lesson(
        TypedLesson(
            kind="cost-cap",
            scope="team",
            confidence=0.8,
            limit_usd_per_session=10.0,
            on_breach="warn",
            metadata={"team_id": workspace.id},
        )
    )
    runs = root / "sessions" / "run-1"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run.json").write_text(
        json.dumps(
            {
                "session_id": "run-1",
                "status": "done",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                "agent_settings": {"user_id": "admin@example.com"},
                "cost": {"total_cost_usd": 0.75},
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    bundle_dir = tmp_path / "bundle"

    export_audit_bundle(root, out_dir=bundle_dir)
    memory_events = json.loads((bundle_dir / "memory_events.json").read_text(encoding="utf-8"))
    verified = verify_audit_bundle(root, bundle_dir=bundle_dir)

    assert "[REDACTED]" in memory_events[0]["content"]
    assert verified["valid"] is True

    (bundle_dir / "README.txt").write_text("tampered", encoding="utf-8")
    tampered = verify_audit_bundle(root, bundle_dir=bundle_dir)
    assert tampered["valid"] is False
    assert "README.txt" in tampered["tampered_files"]
