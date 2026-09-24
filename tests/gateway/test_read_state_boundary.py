from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import read_state


def test_mcp_reexports_canonical_read_freshness_state() -> None:
    assert mcp_server._RANGE_READ_SIGS is read_state.RANGE_READ_SIGS
    assert mcp_server._range_read_sigs_lock is read_state.range_read_sigs_lock
    assert mcp_server._range_read_sigs is read_state.range_read_sigs
    assert mcp_server._line_digests is read_state.line_digests
    assert mcp_server._relocate_served_range is read_state.relocate_served_range
    assert mcp_server._retarget_range_edit is read_state.retarget_range_edit
    assert mcp_server._record_read_sig is read_state.record_read_sig


def test_read_freshness_ownership_no_longer_lives_in_mcp_server() -> None:
    source = inspect.getsource(mcp_server)
    for definition in (
        "def _range_read_sigs(",
        "def _line_digests(",
        "def _aligned_occurrences(",
        "def _relocate_served_range(",
        "def _retarget_range_edit(",
        "def _record_read_sig(",
    ):
        assert definition not in source


def test_read_state_depends_on_narrow_mcp_ledger_not_server_monolith() -> None:
    source = inspect.getsource(read_state)
    assert "mcp_server" not in source
    assert "adapters.mcp.ledger" in source
