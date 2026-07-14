from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill


@pytest.fixture(autouse=True)
def _isolated_spill_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    return tmp_path


def test_spill_bytes_writes_verbatim_and_returns_record(tmp_path: Path) -> None:
    data = b"%PDF-1.4 fake binary content \x00\x01\x02"

    record = tool_output_spill.spill_bytes(data, tool_name="web_fetch", kind="original", suffix=".pdf")

    assert record is not None
    assert record.original_bytes == len(data)
    assert record.path.suffix == ".pdf"
    assert record.path.read_bytes() == data
    assert record.path.parent == tmp_path


def test_spill_bytes_filename_carries_tool_and_kind(tmp_path: Path) -> None:
    record = tool_output_spill.spill_bytes(b"data", tool_name="web_fetch", kind="original", suffix=".pdf")

    assert record is not None
    assert record.path.name.startswith("original-web_fetch-")


def test_retention_sweep_evicts_old_pdf_spills_by_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_MAX_FILES", "1")
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_TTL_SECONDS", "0")

    first = tool_output_spill.spill_bytes(b"one", tool_name="web_fetch", suffix=".pdf")
    second = tool_output_spill.spill_bytes(b"two", tool_name="web_fetch", suffix=".pdf")

    assert first is not None
    assert second is not None
    assert not first.path.exists()  # oldest evicted once the count exceeds the cap
    assert second.path.exists()
