"""The retrieval cache's code fingerprint covers the shared client kit.

``code.pattern`` payloads come from :mod:`lemoncrow_client.kit.astgrep`, which
lives outside the retrieval source roots; a kit change must still invalidate
them.
"""

from pathlib import Path

import lemoncrow_client.kit.astgrep as kit_astgrep
import pytest

from lemoncrow.pro.capabilities.code_context.cache import _retrieval_source_roots, _sources_digest


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    retrieval = tmp_path / "retrieval"
    kit = tmp_path / "kit"
    retrieval.mkdir()
    kit.mkdir()
    (retrieval / "engine.py").write_text("ENGINE = 1\n")
    (kit / "astgrep.py").write_text("ORDER = 'path'\n")
    return retrieval, kit


def test_a_kit_change_changes_the_fingerprint(tmp_path: Path) -> None:
    retrieval, kit = _tree(tmp_path)
    before = _sources_digest((retrieval,), shared=(kit,))
    (kit / "astgrep.py").write_text("ORDER = 'thread'\n")
    assert _sources_digest((retrieval,), shared=(kit,)) != before


def test_a_retrieval_change_still_changes_the_fingerprint(tmp_path: Path) -> None:
    retrieval, kit = _tree(tmp_path)
    before = _sources_digest((retrieval,), shared=(kit,))
    (retrieval / "engine.py").write_text("ENGINE = 2\n")
    assert _sources_digest((retrieval,), shared=(kit,)) != before


def test_kit_sources_alone_do_not_make_a_fingerprint(tmp_path: Path) -> None:
    # Without readable retrieval sources the caller must fall back to the
    # package version; a kit-only digest would miss retrieval code upgrades.
    retrieval, kit = _tree(tmp_path)
    (retrieval / "engine.py").unlink()
    with pytest.raises(OSError):
        _sources_digest((retrieval,), shared=(kit,))


def test_the_fingerprint_hashes_the_kit_the_engine_imports() -> None:
    _, shared = _retrieval_source_roots()
    assert Path(kit_astgrep.__file__).resolve().parent in shared
