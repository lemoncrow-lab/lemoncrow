"""KiloCode session importer for Atelier."""

from __future__ import annotations

from pathlib import Path

from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._vscode_cline import find_task_dirs, import_task_dir

_EXTENSION_ID = "kilocode.kilo-code"


def find_kilo_code_sessions(root: Path | None = None) -> list[Path]:
    return find_task_dirs(_EXTENSION_ID, root)


class KiloCodeImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        imported: list[str] = []
        for task_dir in find_kilo_code_sessions(root):
            trace_id = import_task_dir(
                self.store,
                host="kilo-code",
                extension_id=_EXTENSION_ID,
                task_dir=task_dir,
                force=force,
            )
            if trace_id:
                imported.append(trace_id)
        return imported
