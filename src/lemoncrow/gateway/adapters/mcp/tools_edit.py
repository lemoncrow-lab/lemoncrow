"""Edit MCP handler registration and orchestration."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow_client.kit.contract_guard import weakening_message
from lemoncrow_client.kit.edit import NEW_ALIASES, parse_target

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool

EDIT_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["edits"],
    "additionalProperties": False,
    "properties": {
        "project_id": {
            "type": "string",
            "description": "Optional registered project id; routes edits to that project's isolated workspace.",
        },
        "edits": {
            "type": "array",
            "minItems": 1,
            "description": "File edits to apply in one batch.",
            "items": {
                "type": "object",
                "required": ["path"],
                "additionalProperties": False,
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, optionally suffixed with :Lx or :Lx-Ly.",
                    },
                    "old": {
                        "type": "string",
                        "description": "Exact text to replace.",
                    },
                    "new": {
                        "type": "string",
                        "description": "Replacement or new file content.",
                    },
                    "replace": {
                        "type": "boolean",
                        "description": "Create or replace the whole file.",
                    },
                },
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class EditHandlerHooks:
    anchor_snippet: Callable[..., Any]
    apply_edit_verify_gate: Callable[..., Any]
    attach_contract_literal_review: Callable[..., Any]
    claude_additional_dirs: Callable[..., Any]
    code_context_engine: Callable[..., Any]
    collect_touched_paths: Callable[..., Any]
    compact_applied_entries: Callable[..., Any]
    compute_and_record_diffs: Callable[..., Any]
    detect_test_weakening: Callable[..., Any]
    distinct_edited_files: Callable[..., Any]
    edit_path_locks: Callable[..., Any]
    edit_verify_enabled: Callable[..., Any]
    is_within_root: Callable[..., Any]
    line_digests: Callable[..., Any]
    normalize_edit_aliases: Callable[..., Any]
    range_read_sigs: Callable[..., Any]
    reindex_edited_files: Callable[..., Any]
    relocate_served_range: Callable[..., Any]
    require_edits: Callable[..., Any]
    restore_snapshots: Callable[..., Any]
    retarget_range_edit: Callable[..., Any]
    session_worktree_root: Callable[..., Any]
    silence_clean_edit_result: Callable[..., Any]
    snapshot_paths: Callable[..., Any]
    test_contract_guard_enabled: Callable[..., Any]
    workspace_path: Callable[..., Any]
    workspace_root: Callable[..., Any]
    edit_diag_cap: int
    edit_vcs_cap: int


_HooksFactory = Callable[[], EditHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_edit_handler_hooks(factory: _HooksFactory) -> None:
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> EditHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("edit handler hooks are not configured")
    return factory()


def _anchor_snippet(*args: Any, **kwargs: Any) -> Any:
    return _hooks().anchor_snippet(*args, **kwargs)


def _apply_edit_verify_gate(*args: Any, **kwargs: Any) -> Any:
    return _hooks().apply_edit_verify_gate(*args, **kwargs)


def _attach_contract_literal_review(*args: Any, **kwargs: Any) -> Any:
    return _hooks().attach_contract_literal_review(*args, **kwargs)


def _claude_additional_dirs(*args: Any, **kwargs: Any) -> Any:
    return _hooks().claude_additional_dirs(*args, **kwargs)


def _code_context_engine(*args: Any, **kwargs: Any) -> Any:
    return _hooks().code_context_engine(*args, **kwargs)


def _collect_touched_paths(*args: Any, **kwargs: Any) -> Any:
    return _hooks().collect_touched_paths(*args, **kwargs)


def _compact_applied_entries(*args: Any, **kwargs: Any) -> Any:
    return _hooks().compact_applied_entries(*args, **kwargs)


def _compute_and_record_diffs(*args: Any, **kwargs: Any) -> Any:
    return _hooks().compute_and_record_diffs(*args, **kwargs)


def _detect_test_weakening(*args: Any, **kwargs: Any) -> Any:
    return _hooks().detect_test_weakening(*args, **kwargs)


def _distinct_edited_files(*args: Any, **kwargs: Any) -> Any:
    return _hooks().distinct_edited_files(*args, **kwargs)


def _edit_path_locks(*args: Any, **kwargs: Any) -> Any:
    return _hooks().edit_path_locks(*args, **kwargs)


def _edit_verify_enabled(*args: Any, **kwargs: Any) -> Any:
    return _hooks().edit_verify_enabled(*args, **kwargs)


def _is_within_root(*args: Any, **kwargs: Any) -> Any:
    return _hooks().is_within_root(*args, **kwargs)


def _line_digests(*args: Any, **kwargs: Any) -> Any:
    return _hooks().line_digests(*args, **kwargs)


def _normalize_edit_aliases(*args: Any, **kwargs: Any) -> Any:
    return _hooks().normalize_edit_aliases(*args, **kwargs)


def _range_read_sigs(*args: Any, **kwargs: Any) -> Any:
    return _hooks().range_read_sigs(*args, **kwargs)


def _reindex_edited_files(*args: Any, **kwargs: Any) -> Any:
    return _hooks().reindex_edited_files(*args, **kwargs)


def _relocate_served_range(*args: Any, **kwargs: Any) -> Any:
    return _hooks().relocate_served_range(*args, **kwargs)


def _require_edits(*args: Any, **kwargs: Any) -> Any:
    return _hooks().require_edits(*args, **kwargs)


def _restore_snapshots(*args: Any, **kwargs: Any) -> Any:
    return _hooks().restore_snapshots(*args, **kwargs)


def _retarget_range_edit(*args: Any, **kwargs: Any) -> Any:
    return _hooks().retarget_range_edit(*args, **kwargs)


def _session_worktree_root(*args: Any, **kwargs: Any) -> Any:
    return _hooks().session_worktree_root(*args, **kwargs)


def _silence_clean_edit_result(*args: Any, **kwargs: Any) -> Any:
    return _hooks().silence_clean_edit_result(*args, **kwargs)


def _snapshot_paths(*args: Any, **kwargs: Any) -> Any:
    return _hooks().snapshot_paths(*args, **kwargs)


def _test_contract_guard_enabled(*args: Any, **kwargs: Any) -> Any:
    return _hooks().test_contract_guard_enabled(*args, **kwargs)


def _workspace_path(*args: Any, **kwargs: Any) -> Any:
    return _hooks().workspace_path(*args, **kwargs)


def _workspace_root(*args: Any, **kwargs: Any) -> Any:
    return _hooks().workspace_root(*args, **kwargs)


def recover_edit_args(args: dict[str, Any], known_params: frozenset[str]) -> dict[str, Any]:
    """Recover a flattened single-edit call into the canonical ``edits=[...]`` shape.

    LLMs (especially in a cold session that reads only the short tool
    description) regularly emit the edit descriptor at top level --
    ``edit(path=..., new=..., replace=True)`` -- instead of wrapping it in
    ``edits=[{...}]``. The intent is unambiguous: when ``edits`` is absent and
    the stray keys form an edit descriptor (a target path plus new content),
    wrap them into a single-entry ``edits`` list instead of rejecting the call.
    Anything else (a genuine typo, a path with no content) still surfaces the
    unknown-argument error.
    """
    if "edits" in args:
        return args
    stray = {key: value for key, value in args.items() if key not in known_params}
    if not stray:
        return args
    has_path = any(key in stray for key in ("path", "file_path"))
    has_content = any(key in stray for key in ("new_string", "new_body", "new_file", *NEW_ALIASES))
    if not (has_path and has_content):
        return args
    lifted = {key: value for key, value in args.items() if key in known_params}
    lifted["edits"] = [stray]
    return lifted


@mcp_tool(
    name="edit",
    input_schema=EDIT_TOOL_INPUT_SCHEMA,
    description=(
        "Batch file edits: edits=[{path: 'f.py:L10-L14', new}, ...] — many hunks per "
        "call, even same-file (ranges use the original snapshot). {path, old, new} "
        "only without a fresh range. Whole or brand-new file: {path, new, "
        "replace:true}. Minified-view line numbers → add :minified "
        "('f.py:minified:L10-L14'). No re-read after success."
    ),
    param_aliases={"post_edit_hooks": "hooks"},
    # Policy knobs, not agent choices: accepted by name (tests, power use) but
    # not advertised -- the defaults are right for LLM callers.
    hidden_params=(
        "atomic",
        "hooks",
        "post_edit_timeout_ms",
        "verify",
        "verify_checks",
        "verify_rollback",
        "verify_timeout_ms",
    ),
    recover_args=recover_edit_args,
)
def tool_smart_edit(
    edits: list[dict[str, Any]],
    project_id: str | None = None,
    atomic: bool = True,
    hooks: bool = True,
    post_edit_timeout_ms: int = 30_000,
    verify: bool = False,
    verify_checks: list[str] | None = None,
    verify_rollback: bool = True,
    verify_timeout_ms: int = 60_000,
) -> dict[str, Any]:
    """Apply many mechanical edits across files in one deterministic call.

    Prefer fresh range edits: {path: "foo.py:L10-L20", new: "..."}. Batch
    many range+new hunks in one call, including multiple hunks in the same file;
    ranges resolve against the original snapshot. Use old_string/new_string only
    when no fresh range is available.

    Descriptor shapes:
      - Rich: {path|file_path, new|new_string, old|old_string?, replace?}
      - Structured: notebook cell, symbol, or projection edits

    Returns ordinary successful hunks as {applied: ["path:line,start-end", ...]};
    failures and edits carrying special metadata remain structured.
    """
    _ = project_id  # consumed by request routing in normal MCP dispatch
    # Resolve the edit root the same way reads do (honors CLAUDE/LEMONCROW
    # workspace env + per-request project override) so write-confinement below
    # matches the active workspace.
    repo_root = _workspace_root()
    # A session that entered a git worktree keeps calling this same long-lived
    # server, whose _workspace_root() still names the MAIN CHECKOUT. A relative
    # edit path therefore resolved to the wrong copy of the file -- and passed
    # write-confinement unnoticed, because in-repo worktrees
    # (<repo>/.claude/worktrees/...) sit *under* that root. Resolve relative
    # paths against the worktree the session is demonstrably working in.
    #
    # This is an INFERENCE (from the last bash cwd), so it is never silent:
    # `resolved_against` rides on the result, survives the clean-success
    # squelch, and renders as a suffix on the one-liner. Absolute paths are
    # unaffected -- _resolve_snapshot_path only applies a root to relative ones.
    _session_worktree = _session_worktree_root(repo_root)
    _edit_root = _session_worktree or repo_root
    edits = [_normalize_edit_aliases(e) for e in edits]
    _require_edits(edits)

    paths = _collect_touched_paths(edits, repo_root=_edit_root)
    # Confine writes to the workspace root plus any additional directories from
    # Claude Code's additionalDirectories setting or LEMONCROW_ADDITIONAL_DIRS env.
    # Read tools accept any absolute path; writes need explicit opt-in.
    # Path("/tmp").resolve() as well as "/tmp": on macOS /tmp is a symlink to
    # /private/tmp, and the candidates below are resolved, so the bare literal
    # never matched and the /tmp allowance was dead on that platform.
    _extra_roots = [*_claude_additional_dirs(repo_root), Path("/tmp"), Path("/tmp").resolve()]
    if _session_worktree is not None:
        _extra_roots.append(_session_worktree)
    _allowed_edit_roots = [repo_root, _edit_root, *_extra_roots]

    _escaped_edit_paths = [
        str(_p) for _p in paths.values() if not any(_p == _r or _p.is_relative_to(_r) for _r in _allowed_edit_roots)
    ]
    if _escaped_edit_paths:
        return {
            "failed": [
                {
                    "paths": _escaped_edit_paths,
                    "error": (
                        "edit path escapes the workspace root; add the directory via "
                        "permissions.additionalDirectories (or the legacy top-level "
                        "additionalDirectories) in settings.json or settings.local.json under "
                        "~/.claude/ or <workspace>/.claude/ (applies immediately, no restart), "
                        "or LEMONCROW_ADDITIONAL_DIRS env on the lc MCP server entry (requires "
                        "reconnecting) to allow edits there"
                    ),
                }
            ],
            "rolled_back": True,
        }
    # Freshness guard for blind range edits (:Lx-Ly / replace_range with no old
    # anchor): the line numbers were copied from an earlier read/code_search,
    # so they index the file AS SERVED then. If this window's server never
    # served the file, or the lines it names no longer hold what was served
    # (formatter, another agent, our own earlier edit call), a blind splice
    # would replace the WRONG lines silently -- old-anchored edits self-verify,
    # blind ranges cannot. A file that merely CHANGED is not stale: the guard
    # relocates the served block first and only rejects what it cannot place
    # unambiguously, with the fix spelled out. LEMONCROW_RANGE_EDIT_GUARD=0
    # disables.
    if os.environ.get("LEMONCROW_RANGE_EDIT_GUARD", "1") != "0":
        from lemoncrow.pro.capabilities.source_projection import language_for_minify, translate_minified_line_range

        _session_sigs = _range_read_sigs()
        _stale: list[dict[str, Any]] = []
        for _i, _ed in enumerate(edits):
            if not isinstance(_ed, dict) or _ed.get("old_string") or _ed.get("replace") or _ed.get("overwrite"):
                continue
            if str(_ed.get("kind") or "") in ("symbol", "projection"):
                continue
            _raw = str(_ed.get("file_path") or _ed.get("path") or "")
            if not _raw:
                continue
            _spec = parse_target(_raw)
            _is_range = _spec.start_line is not None and "new_string" in _ed
            if not _is_range:
                continue
            _end_line = _spec.end_line or _spec.start_line
            try:
                _rp = _workspace_path(_spec.path).resolve()
                _st = _rp.stat()
            except OSError:
                continue  # missing file fails downstream with a clearer error
            _sig = _session_sigs.get(str(_rp))
            _cur = (_st.st_mtime_ns, _st.st_size)
            _stat_fresh = _sig is not None and _sig[:2] == _cur
            if _stat_fresh and _sig is not None and _sig[2] and not _spec.minified:
                continue
            # Everything below works off current disk content: read it ONCE and
            # share it between the relocation check, the :minified translation
            # and the retry anchor (each used to re-read the file itself).
            try:
                _disk_content: str | None = _rp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                _disk_content = None
            _served_digests = _sig[3] if _sig is not None else b""
            _cur_digests = _line_digests(_disk_content) if _disk_content is not None and _served_digests else b""
            # Drift is not staleness. That the file changed says nothing about
            # the lines THIS edit names: when they are still byte-identical to
            # what was served, the splice lands on exactly the content the
            # model saw, and when the whole block merely MOVED (an insertion
            # above it -- most often this session's own earlier edit), locating
            # it repairs the range instead of costing a re-read turn -- but a
            # move must carry the served neighbours with it, so a block that
            # is gone (or whose neighbourhood can't be found exactly once)
            # still fails rather than guessing. Requires an exact serve (a
            # lossy view's line numbers were never disk lines) and a snapshot.
            if (
                not _spec.minified
                and _sig is not None
                and _sig[2]
                and _served_digests
                and _cur_digests
                and _spec.start_line is not None
                and _end_line is not None
            ):
                _relocated = _relocate_served_range(_served_digests, _cur_digests, _spec.start_line, _end_line)
                if _relocated is not None:
                    if _relocated != (_spec.start_line, _end_line):
                        _retarget_range_edit(_ed, _spec.path, _relocated[0], _relocated[1])
                    continue
            # A range edit needs translation when the ledger says THIS window
            # actually served the file fresh (read/code_search, and the served
            # bytes are still on disk) -- :minified only changes HOW that fresh
            # serve is interpreted (as minified-view line numbers instead of
            # disk-direct ones); it is NOT a bypass of freshness itself. A
            # :minified flag against a file never read this window, or changed
            # since, has no honest anchor to translate against -- translating
            # it against whatever is on disk NOW would silently resolve to the
            # wrong lines if the file drifted (proven: a stale :minified range
            # survived translation against a since-edited file and renamed the
            # wrong function). Re-derive the same projection from current disk
            # content (deterministic when unchanged) and map the range back to
            # real disk lines -- self-verifying, fails closed (None) on
            # anything ambiguous (out of bounds, spans a dropped comment, not
            # minify-eligible); only then does the reject below fire.
            # Content-identical counts as fresh even when mtime/size moved (a
            # touch, or a rewrite of the same bytes): the projection re-derives
            # identically, so the translation is exactly the one the serve had.
            _ledger_fresh = _stat_fresh or (bool(_served_digests) and _served_digests == _cur_digests)
            if _ledger_fresh:
                _translated: tuple[int, int] | None = None
                _lang = language_for_minify(str(_spec.path))
                if _lang is not None and _spec.start_line is not None and _end_line is not None and _disk_content:
                    _translated = translate_minified_line_range(
                        _disk_content, _lang, _spec.start_line, _end_line, path=_spec.path
                    )
                if _translated is not None:
                    _retarget_range_edit(_ed, _spec.path, _translated[0], _translated[1])
                    continue
                _why = (
                    "was explicitly flagged :minified, but that range doesn't resolve unambiguously "
                    "against current disk content"
                    if _spec.minified
                    else "was last served here as a minified/summary view whose line numbers don't match disk"
                )
                _fix = "widen the range to include a touched comment explicitly, or pass old to anchor the edit"
            else:
                _why = (
                    "was explicitly flagged :minified, but wasn't read fresh in this window "
                    "(never served, or the file changed on disk since)"
                    if _spec.minified
                    else (
                        (
                            "changed on disk since this window last read it, and the lines it names no "
                            "longer hold what was served there (that content is gone, or now sits in "
                            "more than one place)"
                            if _served_digests
                            else "changed on disk since this window last read it"
                        )
                        if _sig is not None
                        else "was not served by read/code_search in this window"
                    )
                )
                _fix = "read the exact range first, or pass old to anchor the edit"
            # Ship the exact current disk content around the requested range so
            # the retry doesn't cost a separate read turn: the model can supply
            # old= from this excerpt directly (rewriting a stripped comment back
            # in if it belongs there), or narrow/widen the range itself. Center
            # on the real DISK region -- for a :minified-flagged range, _spec's
            # numbers are minified-space and would point at the wrong lines
            # here, so resolve each endpoint on its own (a single line rarely
            # hits the same dropped-comment ambiguity a wider span does) before
            # falling back to the raw numbers.
            # _spec.start_line/_end_line are only genuinely disk-space numbers
            # when the range wasn't :minified-flagged; for a flagged range that
            # failed to resolve at all below, there's no honest disk anchor to
            # show context around, so retry_with is omitted rather than shown
            # against the wrong lines.
            _ctx_center_start: int | None = _spec.start_line
            _ctx_center_end: int | None = _end_line
            if _spec.minified and _spec.start_line is not None and _end_line is not None:
                _ctx_center_start = _ctx_center_end = None
                _lang_ctx = language_for_minify(str(_spec.path))
                if _lang_ctx is not None and _disk_content:
                    _start_resolved = translate_minified_line_range(
                        _disk_content, _lang_ctx, _spec.start_line, _spec.start_line, path=_spec.path
                    )
                    _end_resolved = translate_minified_line_range(
                        _disk_content, _lang_ctx, _end_line, _end_line, path=_spec.path
                    )
                    if _start_resolved is not None:
                        _ctx_center_start = _start_resolved[0]
                    if _end_resolved is not None:
                        _ctx_center_end = _end_resolved[1]
            _retry_with: dict[str, Any] | None = None
            _cur_lines = _disk_content.splitlines(keepends=True) if _disk_content is not None else None
            if _cur_lines is not None and _ctx_center_start is not None and _ctx_center_end is not None:
                _ctx_start = max(1, _ctx_center_start - 2)
                _ctx_end = min(len(_cur_lines), _ctx_center_end + 2)
                if _ctx_start <= _ctx_end:
                    _retry_with = {
                        "path": f"{_spec.path}:L{_ctx_start}-L{_ctx_end}",
                        "old_string": _anchor_snippet("".join(_cur_lines[_ctx_start - 1 : _ctx_end])),
                        "hint": "disk content now -- retry with old= set to (part of) this, or a corrected :Lx-Ly range",
                    }
            _entry: dict[str, Any] = {
                "edit_index": _i,
                "edit_file": _raw,
                "error": (
                    f"blind range edit rejected: {_spec.path!r} {_why}, so its line numbers may point at different content now -- {_fix}"
                ),
            }
            if _retry_with is not None:
                _entry["retry_with"] = _retry_with
            _stale.append(_entry)
        if _stale:
            return {"applied": [], "failed": _stale, "rolled_back": True}
    # Serialize the snapshot/apply/write critical section per touched file so two
    # concurrent edit calls cannot read-modify-write the same file and lose one
    # update. Locks are ordered by path (inside _edit_path_locks) to avoid
    # deadlock and release on every return below via the ExitStack.
    # This call's post-apply content per display path, captured under the edit
    # lock. The verify gate (which runs after the lock releases) uses it to skip
    # restoring any file a concurrent edit moved on, avoiding a lost update.
    applied_content: dict[str, str | None] = {}
    with contextlib.ExitStack() as _edit_locks:
        for _lock in _edit_path_locks(list(paths.values())):
            _edit_locks.enter_context(_lock)
        snapshots = _snapshot_paths(paths)

        from lemoncrow.pro.capabilities.tool_supervision.rich_edit import apply_rich_edits

        result = apply_rich_edits(edits, atomic=atomic, repo_root=_edit_root, allowed_roots=_extra_roots)

        # Sync the long-lived engine's index-version cache so the next explore
        # call gets a cache miss and re-queries the FTS5 index (which the
        # background reindex thread and apply_rich_edits both keep up to date).
        # Without this the cached version never changes between tool calls, so
        # explore returns stale pre-edit results on every subsequent invocation.
        try:
            _code_context_engine(str(repo_root))._index_version_cached = None
        except Exception:
            pass

        if not result.get("failed") and not result.get("rolled_back"):
            if _test_contract_guard_enabled():
                weakenings = _detect_test_weakening(snapshots)
                if weakenings:
                    _restore_snapshots(snapshots)
                    return {
                        "failed": [
                            {
                                "paths": [w["path"] for w in weakenings],
                                "error": weakening_message(weakenings),
                            }
                        ],
                        "rolled_back": True,
                        "test_weakening": weakenings,
                    }
            if hooks:
                from lemoncrow.pro.capabilities.tool_supervision.post_edit_hooks import (
                    HookConfig,
                    run_post_edit_hooks,
                )

                try:
                    hook_result = run_post_edit_hooks(
                        [str(p) for p in paths.values()],
                        repo_root=repo_root,
                        # Disable lint-autofix and formatting: a silent
                        # linter/formatter rewriting the agent's just-applied
                        # edit is surprising and unwanted. Diagnostics
                        # (report-only) still run here.
                        config=HookConfig(
                            total_timeout_s=post_edit_timeout_ms / 1000,
                            run_lint_autofix=False,
                            run_format=False,
                            run_organize_imports=False,
                        ),
                    )
                    result["diagnostics"] = [
                        {
                            "file": d.file,
                            "line": d.line,
                            "col": d.col,
                            "severity": d.severity,
                            "message": d.message,
                            "code": d.code,
                            "source": d.source,
                        }
                        for d in hook_result.diagnostics
                    ]
                    result["hooks"] = {
                        "ran": hook_result.steps_ran,
                        "skipped": hook_result.steps_skipped,
                        "failed_steps": hook_result.steps_failed,
                        "total_ms": hook_result.total_ms,
                    }
                    if hook_result.vcs_status:
                        _vcs_lines = hook_result.vcs_status
                        if len(_vcs_lines) > _hooks().edit_vcs_cap:
                            _dropped_vcs = len(_vcs_lines) - _hooks().edit_vcs_cap
                            _vcs_lines = [*_vcs_lines[: _hooks().edit_vcs_cap], f"... +{_dropped_vcs} more"]
                        result["vcs_status"] = {"source": hook_result.vcs_source, "lines": _vcs_lines}
                except Exception as hook_exc:
                    logging.exception("Recovered from broad exception handler")
                    result["hooks"] = {"error": str(hook_exc)}
            # WS1 edit-loop correctness gate: optional executing parse + scoped
            # mypy/pytest verification with rollback. Opt-in via the `verify` arg or
            # the LEMONCROW_EDIT_VERIFY env var; fully fail-open.
            # Diffs are recorded to the ledger (audit/undo) but never surfaced
            # inline: a unified diff echoes old+new content back into context
            # (cache-write now, cache-read on every later turn) for a signal the
            # agent can get on demand by reading the file. The compact `applied`
            # line ranges confirm success; a non-exact match still exposes
            # match_mode on its applied entry, so the agent knows to re-read and
            # verify when a fuzzy match may have diverged from what was asked.
            _compute_and_record_diffs(snapshots)
            for _disp, _fp in paths.items():
                try:
                    applied_content[_disp] = _fp.read_text(encoding="utf-8") if _fp.exists() else None
                except Exception:
                    logging.exception("Recovered from broad exception handler")
                    applied_content[_disp] = None
            _applied = result.get("applied") or []
            # match_mode is only informative when it is not the default exact match.
            for entry in _applied:
                if isinstance(entry, dict) and entry.get("match_mode") == "exact":
                    entry.pop("match_mode", None)

    # WS1 edit-loop correctness gate: optional executing parse + scoped mypy/pytest
    # verification with rollback. Run OUTSIDE the per-file edit locks (released at
    # the end of the with-block above) so a slow verify (up to verify_timeout_ms)
    # can't serialize concurrent edits to the same file; the gate re-acquires the
    # locks only for its rollback restore-write. Opt-in via `verify` or
    # LEMONCROW_EDIT_VERIFY; fully fail-open.
    if not result.get("failed") and not result.get("rolled_back") and _edit_verify_enabled(verify):
        _apply_edit_verify_gate(
            result,
            touched=list(paths.values()),
            snapshots=snapshots,
            applied_content=applied_content,
            checks=verify_checks,
            rollback=verify_rollback,
            timeout_ms=verify_timeout_ms,
            repo_root=repo_root,
        )

    # Fold lint diagnostics (errors/warnings only) into FIXME so all must-act
    # signals surface under one key. Informational notes are dropped as noise.
    if "diagnostics" in result:

        def _diag_in_repo_root(d: dict[str, Any], root: Path) -> bool:
            raw = d.get("file", "")
            if not raw:
                return False
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            return _is_within_root(path.resolve(), root)

        result["diagnostics"] = [
            d
            for d in result["diagnostics"]
            if d.get("severity") in ("error", "warning") and _diag_in_repo_root(d, repo_root)
        ]
        if not result["diagnostics"]:
            result.pop("diagnostics")
        else:

            def _fmt_diag(d: dict[str, Any], root: Path) -> str:
                raw = d.get("file", "")
                try:
                    rel = str(Path(raw).relative_to(root))
                except ValueError:
                    rel = raw
                loc = f"{rel}:L{d['line']}" if d.get("line") else rel
                code = d.get("code", "")
                msg = d.get("message", "")
                return f"{loc} {code}: {msg}" if code else f"{loc}: {msg}"

            _diag_lines = [_fmt_diag(d, repo_root) for d in result.pop("diagnostics")]
            # Cap: a touched file with many pre-existing findings must not dump
            # an unbounded lint report into the edit result.
            if len(_diag_lines) > _hooks().edit_diag_cap:
                _dropped = len(_diag_lines) - _hooks().edit_diag_cap
                _diag_lines = [
                    *_diag_lines[: _hooks().edit_diag_cap],
                    f"... +{_dropped} more (run the linter for the full list)",
                ]
            result.setdefault("FIXME", {})["diagnostics"] = _diag_lines
    # Strip verbose hooks metadata — callers don't need step details.
    result.pop("hooks", None)

    # Honest cross-file batching credit: Claude Code's built-in MultiEdit already
    # batches multiple hunks within a single file into one call, so collapsing
    # same-file hunks is no saving vs a competent baseline. LemonCrow's genuine
    # advantage is only batching edits across *distinct files*, so credit
    # (distinct files - 1) calls. The dispatcher reads this and writes it into the
    # response's content[].saved.calls field.
    applied_entries = result.get("applied") or []
    distinct_files = _distinct_edited_files(applied_entries)
    if distinct_files > 1:
        result.setdefault("calls_saved", distinct_files - 1)
    # Avoided retry-loop credit: a hunk that landed via non-exact RECOVERY
    # (normalized/placeholder/fuzzy/minified re-match) is an edit a byte-exact
    # vanilla Edit would have rejected — costing a failed call, a re-read, and
    # a retry: 2 extra roundtrips per recovered file (the failed-edit → Read →
    # Edit chain). "range" is a caller-chosen mode and "noop" applied nothing
    # — no credit.
    if not result.get("rolled_back"):
        recovered_files = {
            str(entry.get("path", ""))
            for entry in applied_entries
            if isinstance(entry, dict)
            and str(entry.get("match_mode") or "") in ("normalized", "placeholder", "fuzzy", "minified")
        }
        if recovered_files:
            result["calls_saved"] = int(result.get("calls_saved", 0) or 0) + 2 * len(recovered_files)
    if applied_entries and not result.get("failed") and not result.get("rolled_back"):
        result["applied"] = _compact_applied_entries(applied_entries)
    if not result.get("failed") and not result.get("rolled_back"):
        _attach_contract_literal_review(
            result,
            edits,
            repo_root=repo_root,
            touched_paths=[str(p.relative_to(repo_root)) for p in paths.values() if p.is_relative_to(repo_root)],
        )
        # Incremental: refresh the shared index for the touched files now, so a
        # follow-up search/explore reflects this edit without the autosync lag.
        _reindex_edited_files(repo_root, [str(p) for p in paths.values()])
    # Disclose the worktree redirect. It is an inference, so a wrong one has to
    # be visible in the same breath as the write it misdirected.
    if _session_worktree is not None:
        result["resolved_against"] = str(_session_worktree)
    return _silence_clean_edit_result(result)


__all__ = [
    "EDIT_TOOL_INPUT_SCHEMA",
    "EditHandlerHooks",
    "configure_edit_handler_hooks",
    "recover_edit_args",
    "tool_smart_edit",
]
