from pathlib import Path

import pytest
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.index.contracts import sha256_hex
from lemoncrow_server_core.index.sqlite import SqliteIndex


def test_lower_budget_keeps_existing_data_readable_and_allows_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    data = b"x" * (2 * 1024**2)
    digest = sha256_hex(data)
    original = SqliteIndex(path, content_size_cap=4 * 1024**2, compact=True)
    original.backend.content.put("org", digest, data)
    original.close()

    recovered = SqliteIndex(
        path,
        content_size_cap=4 * 1024**2,
        compact=True,
        max_bytes=1024**2,
        max_wal_bytes=128 * 1024,
    )
    try:
        assert recovered.backend.content.get("org", digest) == data
        pages = recovered._db.scalar_int("PRAGMA page_count")
        assert recovered._db._conn.execute("PRAGMA max_page_count").fetchone()[0] == pages
        extra = b"y" * (2 * 1024**2)
        with pytest.raises(ServerError) as refusal:
            recovered.backend.content.put("org", sha256_hex(extra), extra)
        assert refusal.value.code == ErrorCode.STORAGE_LIMIT
        assert recovered._db.scalar_int("PRAGMA page_count") == pages
        recovered.backend.content.mark_unreferenced({}, 1.0)
        assert recovered.backend.content.sweep_marked(2.0).blob_count == 1
        assert recovered.backend.content.get("org", digest) is None
    finally:
        recovered.close()
