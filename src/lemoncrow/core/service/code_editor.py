"""Safe local code-editor detection and launching for the /code workspace."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class CodeEditorSpec:
    editor_id: str
    label: str
    executable: str


_EDITOR_SPECS: Final[tuple[CodeEditorSpec, ...]] = (
    CodeEditorSpec("cursor", "Cursor", "cursor"),
    CodeEditorSpec("vscode", "VS Code", "code"),
)
_EDITOR_ALIASES: Final[dict[str, str]] = {
    "cursor": "cursor",
    "code": "vscode",
    "vscode": "vscode",
    "visual studio code": "vscode",
}
_DISABLED_VALUES: Final[set[str]] = {"0", "false", "no", "none", "off", "disabled"}


def _normalized_editor_preference() -> str | None:
    explicit = os.environ.get("LEMONCROW_CODE_EDITOR", "").strip()
    if explicit.lower() in _DISABLED_VALUES:
        return "disabled"
    candidates = [explicit, os.environ.get("VISUAL", ""), os.environ.get("EDITOR", "")]
    for raw in candidates:
        raw = raw.strip()
        if not raw:
            continue
        try:
            token = shlex.split(raw)[0]
        except ValueError:
            token = raw.split()[0]
        name = Path(token).name.lower()
        mapped = _EDITOR_ALIASES.get(name) or _EDITOR_ALIASES.get(raw.lower())
        if mapped:
            return mapped
    return None


def editor_capability() -> dict[str, object]:
    """Return available allowlisted editors and the selected preferred editor."""

    preference = _normalized_editor_preference()
    if preference == "disabled":
        return {"available": False, "preferred": None, "editors": []}

    available: list[dict[str, str]] = []
    resolved: dict[str, tuple[CodeEditorSpec, str]] = {}
    for spec in _EDITOR_SPECS:
        executable = shutil.which(spec.executable)
        if not executable:
            continue
        resolved[spec.editor_id] = (spec, executable)
        available.append({"id": spec.editor_id, "label": spec.label})

    preferred_id = preference if preference in resolved else None
    if preferred_id is None and available:
        preferred_id = available[0]["id"]
    preferred = next((editor for editor in available if editor["id"] == preferred_id), None)
    return {
        "available": preferred is not None,
        "preferred": preferred,
        "editors": available,
    }


def open_in_editor(
    path: Path,
    *,
    line: int = 1,
    column: int = 1,
    repo_root: Path,
) -> dict[str, object]:
    """Launch the preferred allowlisted editor at one exact local source location."""

    capability = editor_capability()
    preferred = capability.get("preferred")
    if not isinstance(preferred, dict):
        raise RuntimeError(
            "No supported local editor is available. Install Cursor or VS Code, or set LEMONCROW_CODE_EDITOR."
        )
    editor_id = str(preferred.get("id") or "")
    spec = next((item for item in _EDITOR_SPECS if item.editor_id == editor_id), None)
    if spec is None:
        raise RuntimeError("The configured code editor is not supported.")
    executable = shutil.which(spec.executable)
    if not executable:
        raise RuntimeError(f"{spec.label} is no longer available on PATH.")

    bounded_line = max(1, int(line))
    bounded_column = max(1, int(column))
    location = f"{path}:{bounded_line}:{bounded_column}"
    process = subprocess.Popen(
        [executable, "--goto", location],
        cwd=str(repo_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return {
        "opened": True,
        "editor": spec.editor_id,
        "label": spec.label,
        "line": bounded_line,
        "column": bounded_column,
        "pid": process.pid,
    }


__all__ = ["editor_capability", "open_in_editor"]
