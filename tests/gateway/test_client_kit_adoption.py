"""The main package runs the shared client kit instead of its own copies.

``lemoncrow_client.kit`` holds the one implementation of each local tool's
logic. These checks fail if a moved implementation reappears in the main
package, which is how the two copies drifted apart before.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "lemoncrow"
TOOL_SUPERVISION = SRC / "pro" / "capabilities" / "tool_supervision"


def test_moved_modules_are_gone() -> None:
    for moved in (
        TOOL_SUPERVISION / "path_safety.py",
        TOOL_SUPERVISION / "bash_output_compression.py",
        TOOL_SUPERVISION / "bash_output_profiles.py",
        TOOL_SUPERVISION / "output_delta.py",
        SRC / "core" / "foundation" / "redaction.py",
    ):
        assert not moved.exists(), moved


def test_the_bash_output_pipeline_lives_in_kit() -> None:
    bash_exec = (TOOL_SUPERVISION / "bash_exec.py").read_text(encoding="utf-8")
    assert "from lemoncrow_client.kit.bash_output import" in bash_exec
    for moved in (
        "_ANSI_ESCAPE = ",
        "def _dedupe_repeated_lines",
        "def _extract_test_output",
        "def _extract_anomaly_windows",
        "def _suppress_success_summary",
        "def _inject_stable_flags",
    ):
        assert moved not in bash_exec, moved
    spill = (TOOL_SUPERVISION / "tool_output_spill.py").read_text(encoding="utf-8")
    assert "def spill_notice" not in spill


def test_the_edit_engine_lives_in_kit() -> None:
    wrapper = (TOOL_SUPERVISION / "rich_edit.py").read_text(encoding="utf-8")
    assert "from lemoncrow_client.kit.edit import" in wrapper
    for moved in ("def _replace_in_scope", "def _parse_target", "def _atomic_write", "def _build_retry_hint"):
        assert moved not in wrapper, moved


def test_the_edit_handler_helpers_live_in_kit() -> None:
    server = (SRC / "gateway" / "adapters" / "mcp_server.py").read_text(encoding="utf-8")
    for moved in (
        "def _classify_test_weakening",
        "def _looks_like_test_path",
        "def _snapshot_paths",
        "def _restore_snapshots",
        "_OLD_ALIASES = (",
        "_NEW_ALIASES = (",
    ):
        assert moved not in server, moved
    handler = (SRC / "gateway" / "adapters" / "mcp" / "tools_edit.py").read_text(encoding="utf-8")
    assert "_NEW_ALIASES = (" not in handler
    assert "lemoncrow_client.kit" in handler
