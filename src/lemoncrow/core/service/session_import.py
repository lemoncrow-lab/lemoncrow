"""Internal session-import worker for periodic maintenance."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_IMPORT_PROGRESS_LOGGER = "lemoncrow.gateway.hosts.session_parsers"
_IMPORT_PROGRESS_HANDLER_FLAG = "_lemoncrow_import_progress_handler"


def _ensure_import_progress_logging() -> None:
    """Send parser diagnostics to stderr without accumulating handlers."""
    progress_logger = logging.getLogger(_IMPORT_PROGRESS_LOGGER)
    for handler in progress_logger.handlers:
        if getattr(handler, _IMPORT_PROGRESS_HANDLER_FLAG, False):
            if isinstance(handler, logging.StreamHandler):
                if getattr(handler.stream, "closed", False):
                    progress_logger.removeHandler(handler)
                    break
                handler.setStream(sys.stderr)
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _IMPORT_PROGRESS_HANDLER_FLAG, True)
    progress_logger.addHandler(handler)
    progress_logger.propagate = False
    if progress_logger.level == logging.NOTSET or progress_logger.level > logging.INFO:
        progress_logger.setLevel(logging.INFO)


def import_agent_sessions(root: Path, *, store: Any | None = None) -> dict[str, int]:
    """Import supported hosts independently, then refresh imported usage."""
    from lemoncrow.gateway.hosts.session_parsers.registry import iter_importer_classes
    from lemoncrow.infra.storage.factory import create_store

    store = store if store is not None else create_store(root)
    store.init()
    counts: dict[str, int] = {}
    imported_ids: list[str] = []
    for name, importer_cls in iter_importer_classes():
        try:
            importer = importer_cls(store)
            # Release the SQLite writer lock between hosts (GH #43).
            with store.history.batch_mode():
                ids = importer.import_all()
            counts[name] = len(ids)
            imported_ids.extend(ids)
        except Exception:
            logger.exception("session importer failed for host %s", name)
    try:
        from lemoncrow.core.service.sync import sync_usage

        sync_usage(root, session_ids=imported_ids)
    except Exception:
        logger.exception("usage sync failed after session import")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    _ensure_import_progress_logging()
    print(json.dumps(import_agent_sessions(args.root)))


if __name__ == "__main__":
    main()
