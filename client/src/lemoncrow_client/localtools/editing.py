"""``edit``: writes the working tree, then tells the sync layer what changed.

The matrix's reason for this row is "writes the working tree; pushes changed
blobs after", and the second half is the part that makes the whole design work.
The server answers ``read`` and ``code_search`` from its own copy of the
content; that copy is current because this executor reports the paths it wrote
and :mod:`lemoncrow_client.blobs` commits them as an overlay revision *before*
the tool result is returned. "Edit then read" is therefore a protocol
guarantee, not a timing assumption.

The edit itself is :func:`lemoncrow_client.kit.edit.apply_edits`, the engine the
main package runs too: every line range in one call refers to the file as the
caller read it, anchors match exactly or through typography and ``...``
placeholders, Python that no longer parses is refused, each file is written
once and atomically, and any failure rolls the whole batch back with a retry
hint. What this module adds is specific to the client:

* every path is confined with :meth:`LocalContext.resolve` first -- no ``..``,
  no absolute path outside the worktree, no symlink escape;
* edits that need the server (a ``kind``, or ``:minified`` line numbers) are
  refused rather than guessed at;
* the test-contract guard rolls back an edit that only weakens a test;
* the changed paths are reported so the blob service can push them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import AgentAction, ClientError, ErrorCode
from . import LocalContext, LocalResult

if TYPE_CHECKING:
    from ..kit.fsio import FileSnapshot

__all__ = ["run_edit"]


def _refuse(message: str, **details: Any) -> ClientError:
    return ClientError(ErrorCode.PAYLOAD_INVALID, message, details=details, action=AgentAction.FIX_REQUEST)


def _entries(raw_edits: object) -> list[Mapping[str, Any]]:
    if not isinstance(raw_edits, Sequence) or isinstance(raw_edits, (str, bytes)) or not raw_edits:
        raise _refuse("edits must be a non-empty array")
    entries: list[Mapping[str, Any]] = []
    for entry in raw_edits:
        if not isinstance(entry, Mapping):
            raise _refuse("edits[] entries must be objects")
        entries.append(entry)
    return entries


def _prepare(
    context: LocalContext, entries: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], dict[str, Path]]:
    """Confine each path, refuse what needs the server, normalize aliases.

    Returns the engine's descriptors, each descriptor's repository-relative
    path, and the distinct target files in first-seen order.
    """
    from ..kit.edit import normalize_edit_aliases, parse_target

    edits: list[dict[str, Any]] = []
    relatives: list[str] = []
    targets: dict[str, Path] = {}
    for entry in entries:
        kind = entry.get("kind")
        if kind:
            raise _refuse(f"{kind} edits need the LemonCrow server's index; use old/new or a line range", kind=kind)
        raw_path = entry.get("path", entry.get("file_path"))
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise _refuse("edits[].path is required")
        spelled = raw_path.strip()
        spec = parse_target(spelled)
        if spec.minified:
            raise _refuse(
                ":minified line numbers need the server's projection; use disk line numbers or old/new",
                path=spelled,
            )
        target = context.resolve(spec.path)
        relative = context.relative(target)
        edit = normalize_edit_aliases(entry)
        edit.pop("file_path", None)
        edit["path"] = spelled
        edits.append(edit)
        relatives.append(relative)
        targets.setdefault(relative, target)
    return edits, relatives, targets


def _changed(snapshot: FileSnapshot) -> bool:
    if not snapshot.path.exists():
        return snapshot.existed
    if not snapshot.existed:
        return True
    try:
        return snapshot.path.read_text(encoding="utf-8") != snapshot.content
    except (OSError, UnicodeDecodeError):
        return True


def _summary(relative: str, entry: Mapping[str, Any], existed: bool, new_text: str) -> str:
    """One status line per applied hunk; never the file content."""
    kind = entry.get("kind")
    if kind == "replace":
        return f"{'replaced' if existed else 'created'} {relative} ({len(new_text)} chars)"
    if kind == "notebook":
        return f"edited {relative} (notebook)"
    if entry.get("already_applied"):
        return f"unchanged {relative} (already applied)"
    mode = entry.get("match_mode")
    if mode == "range":
        hunk = entry["hunks"][0]
        return f"replaced {relative}:L{hunk['line_start']}-L{hunk['line_end']}"
    if mode == "exact":
        return f"edited {relative} (1 replacement)"
    return f"edited {relative} (1 replacement, {mode} match)"


def _summarize_file_edits(relative: str, summaries: list[str]) -> str:
    """Collapse a same-file batch into one model-facing status line."""
    if len(summaries) == 1:
        return summaries[0]
    exact = f"edited {relative} (1 replacement)"
    if all(summary == exact for summary in summaries):
        return f"edited {relative} ({len(summaries)} replacements)"
    return f"edited {relative} ({len(summaries)} edits)"


def run_edit(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """Apply a batch of edits and report each changed path once."""
    from ..kit import fsio
    from ..kit.contract_guard import detect_weakening, guard_enabled, weakening_message
    from ..kit.edit import apply_edits, require_content

    entries = _entries(arguments.get("edits"))
    edits, relatives, targets = _prepare(context, entries)
    try:
        require_content(edits)
    except ValueError as exc:
        raise _refuse(str(exc)) from exc

    before = fsio.snapshot(targets)
    result = apply_edits(edits, root=context.repo_root)
    failures = result.get("failed") or []
    if failures:
        failure = dict(failures[0])
        raise _refuse(str(failure.pop("error", "") or "edit failed"), **failure)

    changed = [relative for relative, snapshot in before.items() if _changed(snapshot)]
    if changed and guard_enabled(context.environment):
        findings = detect_weakening(before, session_created=context.memory.created_files)
        if findings:
            fsio.restore(before)
            raise _refuse(weakening_message(findings), test_weakening=findings)
    for snapshot in before.values():
        if not snapshot.existed and snapshot.path.exists():
            context.memory.created_files.add(str(snapshot.path.resolve()))

    summaries_by_path: dict[str, list[str]] = {}
    for edit, relative, entry in zip(edits, relatives, result.get("applied") or [], strict=False):
        new_text = str(edit.get("new_string", ""))
        summary = _summary(relative, entry, before[relative].existed, new_text)
        summaries_by_path.setdefault(relative, []).append(summary)
    lines = [_summarize_file_edits(path, summaries) for path, summaries in summaries_by_path.items()]
    return LocalResult(
        content=({"type": "text", "text": "\n".join(lines)},),
        changed_paths=tuple(changed),
        structured={"changed_paths": list(changed), "edits": len(entries)},
    )
