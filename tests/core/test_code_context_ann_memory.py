"""The ANN resident-matrix and streaming-chunk sizes must follow the host's RAM.

Both used to be flat constants tuned on a 128GB workstation (2,000,000 rows =
~7.5GB anonymous at dim=1536; 50,000 rows/chunk), which OOM-kills the same
indexed repo on a 4-8GB laptop.
"""

from __future__ import annotations

import pytest

from lemoncrow.pro.capabilities.code_context import engine

GB = 1024 * 1024 * 1024
DIM = 1536
# Bytes one cached row actually costs: the float32 vector plus its `ids` entry.
ROW_BYTES = DIM * 4 + engine._ANN_ROW_OVERHEAD_BYTES


@pytest.fixture(autouse=True)
def _clear_ann_env(monkeypatch) -> None:
    for var in (
        "LEMONCROW_ANN_CACHE_LIMIT",
        "LEMONCROW_ANN_CACHE_MAX_MB",
        "LEMONCROW_ANN_CACHE_RAM_FRACTION",
    ):
        monkeypatch.delenv(var, raising=False)


def _budget(monkeypatch, ram_bytes: int) -> None:
    monkeypatch.setattr(engine, "_ram_budget_bytes", lambda: ram_bytes)


def test_big_host_still_gets_the_full_historical_ceiling(monkeypatch) -> None:
    _budget(monkeypatch, 64 * GB)

    assert engine._resolve_ann_cache_limit(DIM) == engine._ANN_CACHE_ROW_CEILING
    assert engine._resolve_ann_chunk_rows(DIM) == engine._ANN_CHUNK_ROWS_CEILING


def test_small_host_caches_only_what_it_can_back(monkeypatch) -> None:
    _budget(monkeypatch, 2 * GB)

    limit = engine._resolve_ann_cache_limit(DIM)

    assert 0 < limit < engine._ANN_CACHE_ROW_CEILING
    # The whole point: the matrix this limit permits fits the ANN share of the
    # budget (half of 2GB here), rather than the 7.5GB the old constant allowed.
    assert limit * ROW_BYTES <= GB


def test_no_spare_ram_disables_the_resident_matrix_entirely(monkeypatch) -> None:
    # _ram_budget_bytes() floors at 0 once the hard reserve exceeds MemAvailable.
    _budget(monkeypatch, 0)

    assert engine._resolve_ann_cache_limit(DIM) == 0
    # Streaming still works -- bounded by the floor, never by nothing at all.
    assert engine._resolve_ann_chunk_rows(DIM) == engine._ANN_CHUNK_ROWS_FLOOR


def test_streaming_chunk_shrinks_with_the_budget(monkeypatch) -> None:
    _budget(monkeypatch, 512 * 1024 * 1024)

    rows = engine._resolve_ann_chunk_rows(DIM)

    assert engine._ANN_CHUNK_ROWS_FLOOR <= rows < engine._ANN_CHUNK_ROWS_CEILING
    # Peak is ~3 copies of a chunk in flight (fetchall rows, buf, bytes copy).
    assert rows * ROW_BYTES * engine._ANN_STREAM_PEAK_FACTOR <= 256 * 1024 * 1024


def test_explicit_row_override_still_wins(monkeypatch) -> None:
    _budget(monkeypatch, 0)
    monkeypatch.setenv("LEMONCROW_ANN_CACHE_LIMIT", "1234")

    assert engine._resolve_ann_cache_limit(DIM) == 1234


def test_explicit_mb_ceiling_drives_both_sizes(monkeypatch) -> None:
    _budget(monkeypatch, 64 * GB)
    monkeypatch.setenv("LEMONCROW_ANN_CACHE_MAX_MB", "64")

    limit = engine._resolve_ann_cache_limit(DIM)

    assert limit * ROW_BYTES <= 64 * 1024 * 1024
    assert engine._resolve_ann_chunk_rows(DIM) < engine._ANN_CHUNK_ROWS_CEILING


@pytest.mark.parametrize("var", ["LEMONCROW_ANN_CACHE_LIMIT", "LEMONCROW_ANN_CACHE_MAX_MB"])
def test_zero_means_auto_not_disabled(monkeypatch, var: str) -> None:
    # Both settings default to 0 in the registry meaning "derive it". A persisted
    # 0 arriving as a literal ceiling would silently kill the cache it sizes.
    _budget(monkeypatch, 64 * GB)
    monkeypatch.setenv(var, "0")

    assert engine._resolve_ann_cache_limit(DIM) == engine._ANN_CACHE_ROW_CEILING


def test_unknown_dim_never_divides_by_zero(monkeypatch) -> None:
    _budget(monkeypatch, 64 * GB)

    assert engine._resolve_ann_cache_limit(0) == 0
    assert engine._resolve_ann_chunk_rows(0) == engine._ANN_CHUNK_ROWS_CEILING


def test_garbage_fraction_falls_back_to_the_default(monkeypatch) -> None:
    _budget(monkeypatch, 8 * GB)
    monkeypatch.setenv("LEMONCROW_ANN_CACHE_RAM_FRACTION", "not-a-number")

    assert engine._ann_ram_budget_bytes() == 4 * GB


# ---------------------------------------------------------------------------
# Teardown eviction has to obey the same ceiling the sizers do.
#
# _reuse_connection() drops the cached matrix at the end of every tool call
# when it outgrows the ceiling. Reading LEMONCROW_ANN_CACHE_MAX_MB literally
# there turned the 0 that means "derive it from RAM" into a 0-byte cap, so a
# user who selected the documented default lost the matrix after every single
# call and re-paid the full cold blob-store load on every semantic query.
# ---------------------------------------------------------------------------


class _Matrix:
    """Stands in for the numpy vector matrix: only ``nbytes`` is consulted."""

    def __init__(self, nbytes: int) -> None:
        self.nbytes = nbytes


def _engine_holding(tmp_path, matrix_bytes: int) -> engine.CodeContextEngine:
    eng = engine.CodeContextEngine(repo_root=tmp_path, db_path=tmp_path / "index.sqlite")
    eng._ann_vectors_cache = (("embedder", DIM, 1), ["sym"], _Matrix(matrix_bytes))
    return eng


def test_zero_mb_ceiling_keeps_the_matrix_resident_across_tool_calls(monkeypatch, tmp_path) -> None:
    _budget(monkeypatch, 64 * GB)
    monkeypatch.setenv("LEMONCROW_ANN_CACHE_MAX_MB", "0")  # the registry default: derive it
    eng = _engine_holding(tmp_path, 200 * 1024 * 1024)

    with eng._reuse_connection():
        pass

    assert eng._ann_vectors_cache is not None


def test_a_matrix_beyond_the_ram_budget_is_still_evicted(monkeypatch, tmp_path) -> None:
    _budget(monkeypatch, 64 * 1024 * 1024)  # ANN share: 32 MB
    eng = _engine_holding(tmp_path, 200 * 1024 * 1024)

    with eng._reuse_connection():
        pass

    assert eng._ann_vectors_cache is None


def test_an_explicit_mb_ceiling_still_evicts_what_it_forbids(monkeypatch, tmp_path) -> None:
    """A positive value is the documented pin and must keep working literally."""

    _budget(monkeypatch, 64 * GB)
    monkeypatch.setenv("LEMONCROW_ANN_CACHE_MAX_MB", "64")
    eng = _engine_holding(tmp_path, 200 * 1024 * 1024)

    with eng._reuse_connection():
        pass

    assert eng._ann_vectors_cache is None
