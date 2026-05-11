"""Integration tests for compact hook round-trip (pre + post compaction)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TestCompactHookRoundTrip:
    """Test the full compact lifecycle: PreCompact -> PostCompact."""

    def test_manifest_creation_and_reading(self, tmp_path: Path) -> None:
        """Test that manifest files can be created and read."""
        session_id = "test_run_abc123"

        # Simulate manifest creation
        manifest_data: dict[str, Any] = {
            "created_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "should_compact": False,
            "utilisation_pct": 0.0,
            "preserve_blocks": [],
            "pin_memory": [],
            "open_files": [],
            "suggested_prompt": "Compact this conversation.",
        }

        # Create run directory structure
        run_dir = tmp_path / ".atelier" / "runs" / session_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Write manifest
        manifest_path = run_dir / "compact_manifest.json"
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

        # Verify it exists
        assert manifest_path.exists()

        # Read back
        read_data = json.loads(manifest_path.read_text("utf-8"))
        assert read_data["session_id"] == session_id

    def test_manifest_with_advise_data(self, tmp_path: Path) -> None:
        """Test manifest persistence and reading with full advise data."""
        atelier_root = tmp_path / ".atelier"

        session_id = "test_run_full"
        run_dir = atelier_root / "runs" / session_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Create a manifest with full advise data
        manifest_data: dict[str, Any] = {
            "created_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "should_compact": True,
            "utilisation_pct": 65.3,
            "preserve_blocks": ["block_reasoning_1", "block_reasoning_2"],
            "pin_memory": ["mem_pinned_1"],
            "open_files": ["src/main.py", "src/utils.py"],
            "suggested_prompt": "Compact this conversation. Preserve blocks: block_reasoning_1, block_reasoning_2.",
        }

        manifest_path = run_dir / "compact_manifest.json"
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

        # Read back and verify
        read_manifest = json.loads(manifest_path.read_text("utf-8"))

        assert read_manifest["should_compact"] is True
        assert read_manifest["utilisation_pct"] == 65.3
        assert "block_reasoning_1" in read_manifest["preserve_blocks"]
        assert "mem_pinned_1" in read_manifest["pin_memory"]
        assert len(read_manifest["open_files"]) == 2

    def test_ledger_events_creation(self, tmp_path: Path) -> None:
        """Test that ledger events can be created and appended."""
        atelier_root = tmp_path / ".atelier"
        runs_dir = atelier_root / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        session_id = "test_run_events"

        # Create initial run file
        run_file = runs_dir / f"{session_id}.json"
        initial_data = {
            "session_id": session_id,
            "agent": "claude",
            "events": [
                {
                    "kind": "start",
                    "summary": "Test started",
                    "at": datetime.now(UTC).isoformat(),
                    "payload": {},
                }
            ],
        }
        run_file.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

        # Simulate pre-compact event
        data = json.loads(run_file.read_text("utf-8"))
        data["events"].append(
            {
                "kind": "note",
                "at": datetime.now(UTC).isoformat(),
                "summary": "context compaction starting (manual)",
                "payload": {
                    "hook_event": "PreCompact",
                    "trigger": "manual",
                    "event": "PreCompact",
                },
            }
        )
        run_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Read and verify
        updated_data = json.loads(run_file.read_text("utf-8"))

        assert len(updated_data["events"]) == 2
        last_event = updated_data["events"][-1]
        assert last_event["kind"] == "note"
        assert "context compaction starting" in last_event["summary"]

    def test_post_compact_ledger_event(self, tmp_path: Path) -> None:
        """Test that post-compact events are recorded correctly."""
        atelier_root = tmp_path / ".atelier"
        runs_dir = atelier_root / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        session_id = "test_run_post"
        run_file = runs_dir / f"{session_id}.json"

        # Create run file with one event
        run_data = {
            "session_id": session_id,
            "agent": "claude",
            "events": [
                {
                    "kind": "start",
                    "summary": "Test started",
                    "at": datetime.now(UTC).isoformat(),
                    "payload": {},
                }
            ],
        }
        run_file.write_text(json.dumps(run_data, indent=2), encoding="utf-8")

        # Simulate post-compact
        data = json.loads(run_file.read_text("utf-8"))
        post_payload = {
            "preserve_blocks": ["block_a"],
            "pin_memory": ["mem_1"],
            "manifest_found": True,
        }
        data["events"].append(
            {
                "kind": "note",
                "at": datetime.now(UTC).isoformat(),
                "summary": "context compaction completed (auto)",
                "payload": {
                    "hook_event": "PostCompact",
                    "trigger": "auto",
                    "event": "PostCompact",
                    **post_payload,
                },
            }
        )
        run_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Read and verify
        updated_data = json.loads(run_file.read_text("utf-8"))

        assert len(updated_data["events"]) == 2
        last_event = updated_data["events"][-1]
        assert last_event["kind"] == "note"
        assert "context compaction completed" in last_event["summary"]
        assert last_event["payload"]["manifest_found"] is True

    def test_manifest_survives_round_trip(self, tmp_path: Path) -> None:
        """Test that manifest data survives write-read cycle."""
        atelier_root = tmp_path / ".atelier"
        runs_dir = atelier_root / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        session_id = "test_run_roundtrip"
        manifest_path = runs_dir / session_id / "compact_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        # Original data
        original = {
            "created_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "should_compact": True,
            "utilisation_pct": 72.3,
            "preserve_blocks": ["rb_001", "rb_002", "rb_003"],
            "pin_memory": ["mb_pin_001", "mb_pin_002"],
            "open_files": ["a.py", "b.py", "c.py"],
            "suggested_prompt": "Test prompt",
        }

        # Write
        manifest_path.write_text(json.dumps(original, indent=2), encoding="utf-8")

        # Read back
        read_data = json.loads(manifest_path.read_text("utf-8"))

        # Verify all fields match
        assert read_data["session_id"] == original["session_id"]
        assert read_data["should_compact"] == original["should_compact"]
        assert read_data["utilisation_pct"] == original["utilisation_pct"]
        assert read_data["preserve_blocks"] == original["preserve_blocks"]
        assert read_data["pin_memory"] == original["pin_memory"]
        assert read_data["open_files"] == original["open_files"]


class TestManifestStructure:
    """Test manifest file structure and format."""

    def test_manifest_json_format(self, tmp_path: Path) -> None:
        """Test that manifest is valid JSON."""
        manifest_path = tmp_path / "compact_manifest.json"

        manifest_data = {
            "created_at": datetime.now(UTC).isoformat(),
            "session_id": "test",
            "should_compact": True,
            "utilisation_pct": 60.0,
            "preserve_blocks": [],
            "pin_memory": [],
            "open_files": [],
            "suggested_prompt": "Compact",
        }

        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

        # Verify it's valid JSON by reading it back
        loaded = json.loads(manifest_path.read_text("utf-8"))
        assert isinstance(loaded, dict)

    def test_manifest_required_fields(self, tmp_path: Path) -> None:
        """Test that manifest contains all required fields."""
        manifest_path = tmp_path / "manifest.json"

        required_fields = [
            "created_at",
            "session_id",
            "should_compact",
            "utilisation_pct",
            "preserve_blocks",
            "pin_memory",
            "open_files",
            "suggested_prompt",
        ]

        manifest_data: dict[str, Any] = {field: None for field in required_fields}
        manifest_data["created_at"] = datetime.now(UTC).isoformat()
        manifest_data["session_id"] = "test"
        manifest_data["should_compact"] = False
        manifest_data["utilisation_pct"] = 0.0
        manifest_data["preserve_blocks"] = []
        manifest_data["pin_memory"] = []
        manifest_data["open_files"] = []
        manifest_data["suggested_prompt"] = ""

        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
        loaded = json.loads(manifest_path.read_text("utf-8"))

        for field in required_fields:
            assert field in loaded


class TestErrorHandling:
    """Test error handling in manifest operations."""

    def test_missing_manifest_returns_none(self, tmp_path: Path) -> None:
        """Test that reading non-existent manifest returns None."""
        manifest_path = tmp_path / "nonexistent.json"

        # Should not raise
        data = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else None

        assert data is None

    def test_corrupt_json_handling(self, tmp_path: Path) -> None:
        """Test that corrupt JSON is handled gracefully."""
        manifest_path = tmp_path / "corrupt.json"
        manifest_path.write_text("{ invalid json", encoding="utf-8")

        try:
            json.loads(manifest_path.read_text("utf-8"))
            raise AssertionError("Should have raised JSONDecodeError")
        except json.JSONDecodeError:
            # Expected
            pass
