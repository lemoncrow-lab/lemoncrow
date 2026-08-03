"""LemonCode session importer for LemonCrow.

LemonCode is LemonCrow's controlled fork of OpenCode. The fork rebranded the
XDG *app name* only -- the sqlite schema, the ``opencode.db`` filename, and the
``.opencode/`` workspace dir are byte-identical to upstream OpenCode. So the
parser is reused wholesale via :class:`OpenCodeImporter`; the only differences
are the data root (``$XDG_DATA_HOME/lemoncode``) and the ``source``/``host``
stamps written onto the RawArtifact and Trace.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lemoncrow.gateway.hosts.session_parsers.opencode import (
    OpenCodeImporter,
    find_opencode_sessions,
)


def lemoncode_data_home() -> Path:
    """Global LemonCode data dir: ``$XDG_DATA_HOME/lemoncode`` else ``~/.local/share/lemoncode``."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "lemoncode"


def default_lemoncode_db_path() -> Path:
    """Session DB path. The fork kept upstream's ``opencode.db`` filename."""
    return lemoncode_data_home() / "opencode.db"


def find_lemoncode_sessions(db_path: Path | None = None) -> list[dict[str, Any]]:
    return find_opencode_sessions(db_path or default_lemoncode_db_path())


class LemonCodeImporter(OpenCodeImporter):
    """OpenCode's parser pointed at LemonCode's data root."""

    source = "lemoncode"

    def _default_db_path(self) -> Path:
        return default_lemoncode_db_path()
