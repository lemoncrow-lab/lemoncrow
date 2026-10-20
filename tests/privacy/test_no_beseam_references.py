"""Guard: nothing shipped from this repo may name Beseam.

LemonCrow once posted per-session usage to a Beseam host, left over from the
project's earlier name. The call now goes through the telemetry rollup client;
this keeps any Beseam host, rule id or author string from coming back.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = ("src", "client", "enterprise", "integrations")
SKIPPED_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "dist", "build"}
)
# An operational note about the other services already running on the shared pilot
# VM, which must not be clobbered. It names a neighbour; it is not a LemonCrow reference.
ALLOWED = frozenset({"enterprise/deploy/gcp/vm/README.md"})
NEEDLE = b"beseam"
THIS_FILE = Path(__file__).resolve()


def _files() -> list[Path]:
    found: list[Path] = []
    for name in SCANNED_ROOTS:
        for directory, subdirs, names in os.walk(REPO / name):
            subdirs[:] = [d for d in subdirs if d not in SKIPPED_DIRS]
            found.extend(Path(directory, n) for n in names)
    return found


def test_no_beseam_reference_in_src_client_enterprise_or_integrations() -> None:
    files = _files()
    assert files, "the scan found no files; SCANNED_ROOTS is wrong"
    offenders = []
    for path in files:
        relative = path.relative_to(REPO).as_posix()
        if path.resolve() == THIS_FILE or relative in ALLOWED:
            continue
        try:
            if NEEDLE in path.read_bytes().lower():
                offenders.append(relative)
        except OSError:
            continue
    assert not offenders, f"Beseam is not a LemonCrow concept; remove it from: {sorted(offenders)}"
