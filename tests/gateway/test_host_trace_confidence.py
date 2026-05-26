"""Tests for WP-30: host trace parity and confidence levels.

Verifies:
- trace-confidence.md exists and documents all confidence levels
- Every supported host has an explicit trace confidence entry
- `host`, `trace_confidence`, `capture_sources`, and `missing_surfaces` fields
  appear in the docs
- The Trace model accepts the new fields
- full_live cannot be claimed without hooks in capture_sources (proof-gate guard)
- Codex and Copilot AGENT_README files document trace confidence
"""

from __future__ import annotations

from pathlib import Path

DOCS_ROOT = Path(__file__).parent.parent.parent / "docs"
TRACE_CONFIDENCE_DOC = DOCS_ROOT / "engineering" / "trace-confidence.md"
HOST_MATRIX = DOCS_ROOT / "hosts" / "host-capability-matrix.md"
CODEX_README = Path(__file__).parent.parent.parent / "integrations" / "codex" / "AGENT_README.md"
COPILOT_README = Path(__file__).parent.parent.parent / "integrations" / "copilot" / "AGENT_README.md"

SUPPORTED_HOSTS = [
    "Claude Code",
    "Codex CLI",
    "Copilot",
    "opencode",
    "Antigravity",
]

CONFIDENCE_LEVELS = [
    "full_live",
    "mcp_live",
    "wrapper_live",
    "imported",
    "manual",
]

REQUIRED_METADATA_FIELDS = [
    "host",
    "trace_confidence",
    "capture_sources",
    "missing_surfaces",
]


# ---------------------------------------------------------------------------
# Doc existence
# ---------------------------------------------------------------------------


def test_trace_confidence_doc_exists() -> None:
    assert TRACE_CONFIDENCE_DOC.exists(), f"missing {TRACE_CONFIDENCE_DOC}"


def test_host_matrix_exists() -> None:
    assert HOST_MATRIX.exists(), f"missing {HOST_MATRIX}"


# ---------------------------------------------------------------------------
# Confidence levels documented
# ---------------------------------------------------------------------------


def test_all_confidence_levels_documented() -> None:
    text = TRACE_CONFIDENCE_DOC.read_text(encoding="utf-8")
    for level in CONFIDENCE_LEVELS:
        assert level in text, f"trace-confidence.md missing confidence level: {level}"


def test_host_matrix_has_confidence_levels() -> None:
    text = HOST_MATRIX.read_text(encoding="utf-8")
    for level in CONFIDENCE_LEVELS:
        assert level in text, f"host-capability-matrix.md missing confidence level: {level}"


# ---------------------------------------------------------------------------
# Per-host confidence mapping
# ---------------------------------------------------------------------------


def test_trace_confidence_doc_covers_all_hosts() -> None:
    text = TRACE_CONFIDENCE_DOC.read_text(encoding="utf-8")
    for host in SUPPORTED_HOSTS:
        assert host in text, f"trace-confidence.md missing host entry: {host}"


# ---------------------------------------------------------------------------
# Metadata fields in docs
# ---------------------------------------------------------------------------


def test_required_metadata_fields_in_trace_confidence_doc() -> None:
    text = TRACE_CONFIDENCE_DOC.read_text(encoding="utf-8")
    for field in REQUIRED_METADATA_FIELDS:
        assert field in text, f"trace-confidence.md missing required metadata field: {field}"


def test_required_metadata_fields_in_host_matrix() -> None:
    text = HOST_MATRIX.read_text(encoding="utf-8")
    for field in ("trace_confidence", "capture_sources", "missing_surfaces"):
        assert field in text, f"host-capability-matrix.md missing required field: {field}"


# ---------------------------------------------------------------------------
# Codex + Copilot AGENT_README coverage
# ---------------------------------------------------------------------------


def test_codex_readme_documents_trace_confidence() -> None:
    assert CODEX_README.exists(), f"missing {CODEX_README}"
    text = CODEX_README.read_text(encoding="utf-8")
    assert "trace_confidence" in text, "Codex AGENT_README missing trace_confidence"
    assert "capture_sources" in text, "Codex AGENT_README missing capture_sources"
    assert "missing_surfaces" in text, "Codex AGENT_README missing missing_surfaces"


def test_copilot_readme_documents_trace_confidence() -> None:
    assert COPILOT_README.exists(), f"missing {COPILOT_README}"
    text = COPILOT_README.read_text(encoding="utf-8")
    assert "trace_confidence" in text, "Copilot AGENT_README missing trace_confidence"
    assert "capture_sources" in text, "Copilot AGENT_README missing capture_sources"
    assert "missing_surfaces" in text, "Copilot AGENT_README missing missing_surfaces"


# ---------------------------------------------------------------------------
# Trace model fields
# ---------------------------------------------------------------------------


def test_trace_model_has_confidence_fields() -> None:
    from atelier.core.foundation.models import Trace

    fields = Trace.model_fields
    assert "host" in fields, "Trace model missing 'host' field"
    assert "trace_confidence" in fields, "Trace model missing 'trace_confidence' field"
    assert "capture_sources" in fields, "Trace model missing 'capture_sources' field"
    assert "missing_surfaces" in fields, "Trace model missing 'missing_surfaces' field"


def test_trace_model_accepts_confidence_payload() -> None:
    from atelier.core.foundation.models import Trace

    t = Trace(
        id="test-id-001",
        agent="claude:claude-opus-4-5",
        domain="coding",
        task="test task",
        status="success",
        host="claude",
        trace_confidence="full_live",
        capture_sources=["hooks", "mcp"],
        missing_surfaces=[],
    )
    assert t.host == "claude"
    assert t.trace_confidence == "full_live"
    assert t.capture_sources == ["hooks", "mcp"]
    assert t.missing_surfaces == []


def test_trace_model_accepts_null_confidence() -> None:
    from atelier.core.foundation.models import Trace

    t = Trace(
        id="test-id-002",
        agent="codex",
        domain="coding",
        task="test task",
        status="partial",
    )
    assert t.trace_confidence is None
    assert t.capture_sources == []
    assert t.missing_surfaces == []


# ---------------------------------------------------------------------------
# Proof-gate guard: no false full_live claims
# ---------------------------------------------------------------------------


def test_full_live_requires_hooks_in_capture_sources() -> None:
    """full_live must include hooks/live_hooks/plugin_hooks in capture_sources."""
    from atelier.core.foundation.models import Trace

    # Valid full_live: has hooks in capture_sources
    t_ok = Trace(
        id="test-id-003",
        agent="claude:claude-opus-4-5",
        domain="coding",
        task="test",
        status="success",
        trace_confidence="full_live",
        capture_sources=["hooks", "mcp"],
    )
    assert t_ok.trace_confidence == "full_live"

    # full_live without hooks is still accepted by the model itself (enforcement
    # happens in tool_record_trace); verify the model doesn't reject the value
    t_no_hooks = Trace(
        id="test-id-004",
        agent="copilot",
        domain="coding",
        task="test",
        status="success",
        trace_confidence="full_live",
        capture_sources=["mcp"],
    )
    # Model accepts the value; enforcement downgrade is in mcp_server
    assert t_no_hooks.trace_confidence == "full_live"


def test_mcp_server_downgrades_full_live_without_hooks(tmp_path: Path) -> None:
    """tool_record_trace downgrades full_live → mcp_live if hooks are absent."""
    import os

    os.environ["ATELIER_ROOT"] = str(tmp_path)
    (tmp_path / "blocks").mkdir(parents=True, exist_ok=True)

    # Import the function directly; patch store internals
    import unittest.mock as mock

    with (
        mock.patch("atelier.gateway.adapters.mcp_server._runtime") as mock_rt,
        mock.patch("atelier.gateway.adapters.mcp_server._get_ledger") as mock_led,
        mock.patch("atelier.gateway.adapters.mcp_server._get_realtime_context") as mock_rtc,
    ):
        # Set up minimal mocks
        fake_store = mock.MagicMock()
        fake_rt = mock.MagicMock()
        fake_rt.store = fake_store
        mock_rt.return_value = fake_rt

        fake_ledger = mock.MagicMock()
        fake_ledger.session_id = "run-test-001"
        mock_led.return_value = fake_ledger

        mock_rtc.return_value = mock.MagicMock()

        from atelier.gateway.adapters.mcp_server import tool_record_trace

        # The mcp_tool decorator wraps functions to accept a single dict argument
        tool_record_trace(
            {
                "agent": "copilot",
                "domain": "coding",
                "task": "test full_live downgrade",
                "status": "success",
                "trace_confidence": "full_live",
                "capture_sources": ["mcp"],  # no hooks
                "missing_surfaces": [],
            }
        )

        # Verify the stored trace was downgraded
        assert fake_store.record_trace.called
        stored_trace = fake_store.record_trace.call_args[0][0]
        assert stored_trace.trace_confidence == "mcp_live", "full_live without hooks must be downgraded to mcp_live"
        assert "hooks" in stored_trace.missing_surfaces, "hooks must appear in missing_surfaces after downgrade"


def test_mcp_server_record_trace_accepts_string_tool_names(tmp_path: Path) -> None:
    """MCP callers may send tools_called as simple names."""
    import os

    os.environ["ATELIER_ROOT"] = str(tmp_path)
    (tmp_path / "blocks").mkdir(parents=True, exist_ok=True)

    import unittest.mock as mock

    with (
        mock.patch("atelier.gateway.adapters.mcp_server._runtime") as mock_rt,
        mock.patch("atelier.gateway.adapters.mcp_server._get_ledger") as mock_led,
        mock.patch("atelier.gateway.adapters.mcp_server._get_realtime_context") as mock_rtc,
    ):
        fake_store = mock.MagicMock()
        fake_rt = mock.MagicMock()
        fake_rt.store = fake_store
        mock_rt.return_value = fake_rt

        fake_ledger = mock.MagicMock()
        fake_ledger.session_id = "run-test-002"
        mock_led.return_value = fake_ledger

        mock_rtc.return_value = mock.MagicMock()

        from atelier.gateway.adapters.mcp_server import tool_record_trace

        tool_record_trace(
            {
                "agent": "codex",
                "domain": "coding",
                "task": "record simple tool names",
                "status": "success",
                "tools_called": ["sed", {"tool": "pytest", "count": "2"}],
                "validation_results": [
                    "pytest passed",
                    {"check": "compile", "status": "pass", "output": "ok"},
                ],
            }
        )

        assert fake_store.record_trace.called
        stored_trace = fake_store.record_trace.call_args[0][0]
        assert [tool.name for tool in stored_trace.tools_called] == ["sed", "pytest"]
        assert [tool.args_hash for tool in stored_trace.tools_called] == ["", ""]
        assert [tool.count for tool in stored_trace.tools_called] == [1, 2]
        assert [result.name for result in stored_trace.validation_results] == [
            "pytest passed",
            "compile",
        ]
        assert [result.passed for result in stored_trace.validation_results] == [True, True]
        assert [result.detail for result in stored_trace.validation_results] == ["", "ok"]


def test_mcp_server_record_trace_persists_and_archives_learnings(tmp_path: Path) -> None:
    """MCP trace learnings are stored on Trace and copied to archival memory."""
    import os

    os.environ["ATELIER_ROOT"] = str(tmp_path)
    (tmp_path / "blocks").mkdir(parents=True, exist_ok=True)

    import unittest.mock as mock

    with (
        mock.patch("atelier.gateway.adapters.mcp_server._runtime") as mock_rt,
        mock.patch("atelier.gateway.adapters.mcp_server._get_ledger") as mock_led,
        mock.patch("atelier.gateway.adapters.mcp_server._get_realtime_context") as mock_rtc,
        mock.patch("atelier.gateway.adapters.mcp_server._memory_store") as mock_memory_store,
    ):
        fake_store = mock.MagicMock()
        fake_rt = mock.MagicMock()
        fake_rt.store = fake_store
        mock_rt.return_value = fake_rt

        fake_ledger = mock.MagicMock()
        fake_ledger.session_id = "run-test-learnings"
        mock_led.return_value = fake_ledger

        mock_rtc.return_value = mock.MagicMock()
        fake_memory = mock.MagicMock()
        mock_memory_store.return_value = fake_memory

        from atelier.gateway.adapters.mcp_server import tool_record_trace

        tool_record_trace(
            {
                "agent": "codex",
                "domain": "coding",
                "task": "record session learnings",
                "status": "success",
                "learnings": [
                    "Focused regressions worked.",
                    {
                        "kind": "didnt work",
                        "text": "Relying on output_summary hid the actual lesson.",
                        "target": "reasonblock",
                    },
                ],
            }
        )

        assert fake_store.record_trace.called
        stored_trace = fake_store.record_trace.call_args[0][0]
        assert [learning.text for learning in stored_trace.learnings] == [
            "Focused regressions worked.",
            "Relying on output_summary hid the actual lesson.",
        ]
        assert [learning.kind for learning in stored_trace.learnings] == ["note", "did_not_work"]
        assert stored_trace.learnings[1].promote_to == "reasonblock"
        assert fake_memory.insert_passage.call_count == 2


def test_mcp_server_record_trace_accepts_legacy_run_id(tmp_path: Path) -> None:
    """Legacy callers may still send run_id instead of session_id."""
    import os

    os.environ["ATELIER_ROOT"] = str(tmp_path)
    (tmp_path / "blocks").mkdir(parents=True, exist_ok=True)

    import unittest.mock as mock

    with (
        mock.patch("atelier.gateway.adapters.mcp_server._runtime") as mock_rt,
        mock.patch("atelier.gateway.adapters.mcp_server._get_ledger") as mock_led,
        mock.patch("atelier.gateway.adapters.mcp_server._get_realtime_context") as mock_rtc,
    ):
        fake_store = mock.MagicMock()
        fake_rt = mock.MagicMock()
        fake_rt.store = fake_store
        mock_rt.return_value = fake_rt

        fake_ledger = mock.MagicMock()
        fake_ledger.session_id = "run-test-003"
        mock_led.return_value = fake_ledger

        mock_rtc.return_value = mock.MagicMock()

        from atelier.gateway.adapters.mcp_server import tool_record_trace

        tool_record_trace(
            {
                "agent": "codex",
                "domain": "coding",
                "task": "accept legacy run_id",
                "status": "success",
                "run_id": "legacy-run-001",
            }
        )

        assert fake_store.record_trace.called
        stored_trace = fake_store.record_trace.call_args[0][0]
        assert stored_trace.session_id == "legacy-run-001"


def test_mcp_server_record_trace_normalizes_legacy_strength_confidence(tmp_path: Path) -> None:
    """Legacy callers may send qualitative confidence labels like `high`."""
    import os

    os.environ["ATELIER_ROOT"] = str(tmp_path)
    (tmp_path / "blocks").mkdir(parents=True, exist_ok=True)

    import unittest.mock as mock

    with (
        mock.patch("atelier.gateway.adapters.mcp_server._runtime") as mock_rt,
        mock.patch("atelier.gateway.adapters.mcp_server._get_ledger") as mock_led,
        mock.patch("atelier.gateway.adapters.mcp_server._get_realtime_context") as mock_rtc,
    ):
        fake_store = mock.MagicMock()
        fake_rt = mock.MagicMock()
        fake_rt.store = fake_store
        mock_rt.return_value = fake_rt

        fake_ledger = mock.MagicMock()
        fake_ledger.session_id = "run-test-004"
        mock_led.return_value = fake_ledger

        mock_rtc.return_value = mock.MagicMock()

        from atelier.gateway.adapters.mcp_server import tool_record_trace

        tool_record_trace(
            {
                "agent": "atelier:code",
                "domain": "coding",
                "task": "accept legacy confidence strength",
                "status": "success",
                "trace_confidence": "high",
            }
        )

        assert fake_store.record_trace.called
        stored_trace = fake_store.record_trace.call_args[0][0]
        assert stored_trace.trace_confidence == "manual"
