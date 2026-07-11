"""Workspace audit bundle export and verification."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.cross_vendor_memory.audit_log import MemoryAuditLog
from lemoncrow.core.capabilities.governance import load_policy, record_within_retention, redact_record
from lemoncrow.core.capabilities.lesson_promotion.store import TypedLessonStore
from lemoncrow.core.capabilities.team import TeamWorkspaceManager, summarize_workspace_usage


def export_audit_bundle(root: Path | str, *, out_dir: Path | str, since: datetime | None = None) -> dict[str, Any]:
    store_root = Path(root).expanduser().resolve()
    bundle_dir = Path(out_dir).expanduser().resolve()
    manager = TeamWorkspaceManager(store_root)
    workspace = manager.load_workspace()
    policy = load_policy(store_root)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    memory_events = [
        redact_record(event.to_public_record(), policy)
        for event in MemoryAuditLog(store_root).read(since=since)
        if record_within_retention(event.to_public_record(), record_type="memory", policy=policy)
    ]
    route_decisions = _read_jsonl(
        store_root / "live_savings_events.jsonl",
        since=since,
        record_type="live_savings",
        policy=policy,
    )
    team_audit = [
        redact_record(event.model_dump(mode="json"), policy)
        for event in manager.list_audit_events(since=since)
        if record_within_retention(event.model_dump(mode="json"), record_type="team_audit", policy=policy)
    ]
    lessons = [
        redact_record(lesson.model_dump(mode="json"), policy)
        for lesson in TypedLessonStore(store_root, create=False).list_lessons()
        if record_within_retention(lesson.model_dump(mode="json"), record_type="lessons", policy=policy)
    ]
    sessions = _read_runs(store_root, since=since, policy=policy)
    rollbacks = [event for event in memory_events if event.get("actor") == "lemoncrow-rollback"]
    usage = summarize_workspace_usage(store_root, manager=manager, since=since)

    _write_json(bundle_dir / "memory_events.json", memory_events)
    _write_json(bundle_dir / "route_decisions.json", route_decisions)
    _write_json(bundle_dir / "typed_lessons.json", lessons)
    _write_json(bundle_dir / "team_audit.json", team_audit)
    _write_json(bundle_dir / "rollbacks.json", rollbacks)
    _write_json(bundle_dir / "session_report.json", {"sessions": sessions, "usage": usage})
    _write_readme(
        bundle_dir / "README.txt",
        workspace_name=workspace.name,
        workspace_id=workspace.id,
        since=since,
        counts={
            "memory_events": len(memory_events),
            "route_decisions": len(route_decisions),
            "typed_lessons": len(lessons),
            "team_audit": len(team_audit),
            "sessions": len(sessions),
            "rollbacks": len(rollbacks),
        },
    )

    secret = manager.get_signing_secret()
    key_id = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]
    manifest = {
        "workspace_id": workspace.id,
        "account_id": workspace.account_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "since": since.astimezone(UTC).isoformat() if since else None,
        "key_id": key_id,
        "files": [_manifest_entry(path, bundle_dir) for path in sorted(bundle_dir.iterdir()) if path.is_file()],
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)
    signature = hmac.new(secret.encode("utf-8"), manifest_bytes, hashlib.sha256).hexdigest()
    (bundle_dir / "manifest.sig").write_text(signature, encoding="utf-8")
    return {
        "bundle_dir": str(bundle_dir),
        "workspace_id": workspace.id,
        "key_id": key_id,
        "signature_path": str(bundle_dir / "manifest.sig"),
    }


def verify_audit_bundle(root: Path | str, *, bundle_dir: Path | str) -> dict[str, Any]:
    store_root = Path(root).expanduser().resolve()
    target = Path(bundle_dir).expanduser().resolve()
    manager = TeamWorkspaceManager(store_root)
    secret = manager.get_signing_secret()
    manifest_path = target / "manifest.json"
    signature_path = target / "manifest.sig"
    if not manifest_path.exists() or not signature_path.exists():
        raise FileNotFoundError("manifest.json and manifest.sig are required")
    manifest_bytes = manifest_path.read_bytes()
    expected = hmac.new(secret.encode("utf-8"), manifest_bytes, hashlib.sha256).hexdigest()
    actual = signature_path.read_text(encoding="utf-8").strip()
    signature_ok = hmac.compare_digest(expected, actual)
    manifest = json.loads(manifest_bytes)
    mismatches: list[str] = []
    for entry in manifest.get("files", []):
        rel_path = str(entry["path"])
        file_path = target / rel_path
        if not file_path.exists():
            mismatches.append(rel_path)
            continue
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if digest != entry.get("sha256"):
            mismatches.append(rel_path)
    return {"valid": signature_ok and not mismatches, "signature_ok": signature_ok, "tampered_files": mismatches}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _write_readme(
    path: Path,
    *,
    workspace_name: str,
    workspace_id: str,
    since: datetime | None,
    counts: dict[str, int],
) -> None:
    lines = [
        f"Workspace: {workspace_name} ({workspace_id})",
        f"Since: {since.astimezone(UTC).isoformat() if since else 'all time'}",
        "",
    ]
    for key, value in counts.items():
        lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _manifest_entry(path: Path, bundle_dir: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(bundle_dir)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def _read_jsonl(
    path: Path,
    *,
    since: datetime | None,
    record_type: str,
    policy: Any,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not record_within_retention(record, record_type=record_type, policy=policy):
            continue
        timestamp = _record_timestamp(record)
        if since is not None and timestamp is not None and timestamp < since.astimezone(UTC):
            continue
        records.append(redact_record(record, policy))
    return records


def _read_runs(root: Path, *, since: datetime | None, policy: Any) -> list[dict[str, Any]]:
    sessions_dir = root / "sessions"
    if not sessions_dir.exists():
        return []
    sessions: list[dict[str, Any]] = []
    for path in sorted(sessions_dir.glob("**/run.json")):
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not record_within_retention(snapshot, record_type="runs", policy=policy):
            continue
        timestamp = _record_timestamp(snapshot)
        if since is not None and timestamp is not None and timestamp < since.astimezone(UTC):
            continue
        sessions.append(redact_record(snapshot, policy))
    return sessions


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    for key in ("at", "updated_at", "created_at", "captured_at"):
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
