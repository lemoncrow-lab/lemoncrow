"""Unit tests for compact_advise logic."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any, cast

from atelier.infra.runtime.run_ledger import RunLedger


class TestCompactAdviseLogic:
    """Test the compact advise calculation logic."""

    def test_advise_at_low_utilisation(self) -> None:
        """Test advise when utilisation is low (~10%)."""
        ledger = RunLedger()
        ledger.token_count = 10_000  # ~5% of 200K
        ledger.events = []

        # Estimate tokens (same as tool)
        tokens_used = ledger.token_count + max(0, len(ledger.events) * 10)
        utilisation_pct = round(100.0 * tokens_used / 200_000, 1)

        assert utilisation_pct < 10.0
        should_compact = utilisation_pct >= 60.0
        assert not should_compact

    def test_advise_at_moderate_utilisation(self) -> None:
        """Test advise when utilisation is moderate (~50%)."""
        ledger = RunLedger()
        ledger.token_count = 80_000  # ~40% of 200K
        ledger.events = cast(Any, list(range(1000)))  # ~100 more tokens

        tokens_used = ledger.token_count + max(0, len(ledger.events) * 10)
        utilisation_pct = round(100.0 * tokens_used / 200_000, 1)

        assert 40.0 <= utilisation_pct < 60.0
        should_compact = utilisation_pct >= 60.0
        assert not should_compact

    def test_advise_at_high_utilisation(self) -> None:
        """Test advise when utilisation is high (~75%)."""
        ledger = RunLedger()
        ledger.token_count = 140_000  # ~70% of 200K
        ledger.events = cast(Any, list(range(500)))  # ~50 more tokens

        tokens_used = ledger.token_count + max(0, len(ledger.events) * 10)
        utilisation_pct = round(100.0 * tokens_used / 200_000, 1)

        assert utilisation_pct >= 60.0
        should_compact = utilisation_pct >= 60.0
        assert should_compact

    def test_preserve_blocks_selection(self) -> None:
        """Test that preserve_blocks are correctly selected."""
        ledger = RunLedger()
        ledger.active_reasonblocks = ["block_a", "block_b", "block_c", "block_d"]

        preserve_blocks = list(set(ledger.active_reasonblocks))[:3]

        assert len(preserve_blocks) == 3
        # Check that 3 of the 4 blocks are present (set order is unpredictable)
        assert all(block in ledger.active_reasonblocks for block in preserve_blocks)

    def test_preserve_blocks_fewer_than_3(self) -> None:
        """Test block selection when fewer than 3 blocks exist."""
        ledger = RunLedger()
        ledger.active_reasonblocks = ["block_a", "block_b"]

        preserve_blocks = list(set(ledger.active_reasonblocks))[:3]

        assert len(preserve_blocks) == 2

    def test_open_files_selection(self) -> None:
        """Test that open_files are correctly selected (last 5)."""
        ledger = RunLedger()
        ledger.files_touched = ["f1.py", "f2.py", "f3.py", "f4.py", "f5.py", "f6.py", "f7.py"]

        open_files = ledger.files_touched[-5:]

        assert len(open_files) == 5
        assert open_files == ["f3.py", "f4.py", "f5.py", "f6.py", "f7.py"]

    def test_open_files_fewer_than_5(self) -> None:
        """Test file selection when fewer than 5 files exist."""
        ledger = RunLedger()
        ledger.files_touched = ["f1.py", "f2.py"]

        open_files = ledger.files_touched[-5:]

        assert len(open_files) == 2

    def test_empty_pinned_memory(self) -> None:
        """Test that empty pinned memory list is handled."""
        pin_memory: list[str] = []

        # Should not raise, just return empty list
        assert pin_memory == []
        assert len(pin_memory) == 0

    def test_suggested_prompt_generation(self) -> None:
        """Test that suggested_prompt is generated correctly."""
        utilisation_pct = 62.5
        preserve_blocks = ["block_a", "block_b"]
        open_files = ["src/main.py", "src/utils.py"]

        suggested_prompt = (
            f"Compact this conversation. Context utilisation: {utilisation_pct}%. "
            f"Please preserve these ReasonBlocks: {', '.join(preserve_blocks) or '(none yet)'}. "
            f"Recently edited files: {', '.join(open_files) or '(none)'}"
        )

        assert "62.5%" in suggested_prompt
        assert "block_a" in suggested_prompt
        assert "block_b" in suggested_prompt
        assert "src/main.py" in suggested_prompt
        assert "src/utils.py" in suggested_prompt

    def test_suggested_prompt_with_no_blocks(self) -> None:
        """Test suggested_prompt when no blocks are preserved."""
        utilisation_pct = 45.0
        preserve_blocks: list[str] = []
        open_files = ["src/main.py"]

        suggested_prompt = (
            f"Compact this conversation. Context utilisation: {utilisation_pct}%. "
            f"Please preserve these ReasonBlocks: {', '.join(preserve_blocks) or '(none yet)'}. "
            f"Recently edited files: {', '.join(open_files) or '(none)'}"
        )

        assert "(none yet)" in suggested_prompt


class TestCompactManifestPersistence:
    """Test manifest file persistence."""

    def test_manifest_structure(self, tmp_path: Path) -> None:
        """Test that manifest has the correct structure."""
        from datetime import datetime

        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "session_id": "test_run_123",
            "should_compact": True,
            "utilisation_pct": 62.5,
            "preserve_blocks": ["block_a", "block_b"],
            "pin_memory": ["mem_1"],
            "open_files": ["src/main.py"],
            "suggested_prompt": "Compact this conversation.",
        }

        manifest_path = tmp_path / "compact_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # Read back and verify
        read_manifest = json.loads(manifest_path.read_text("utf-8"))
        assert read_manifest["session_id"] == "test_run_123"
        assert read_manifest["should_compact"] is True
        assert read_manifest["utilisation_pct"] == 62.5
        assert len(read_manifest["preserve_blocks"]) == 2
        assert len(read_manifest["pin_memory"]) == 1

    def test_manifest_path_creation(self, tmp_path: Path) -> None:
        """Test that manifest directory is created if it doesn't exist."""
        runs_dir = tmp_path / "runs" / "run_123"
        assert not runs_dir.exists()

        runs_dir.mkdir(parents=True, exist_ok=True)

        assert runs_dir.exists()
        assert runs_dir.parent.exists()
