"""``scan`` and ``codemod``: the kit's security scanner and ast-grep adapter.

The engines are the main package's: :mod:`lemoncrow_client.kit.security_scan`
runs the bundled SAST rule pack and the Python taint check, and
:mod:`lemoncrow_client.kit.astgrep` runs ast-grep for both tools. What this
module adds is the one thing the client does differently on purpose: it never
fetches a binary. ast-grep comes from ``LEMONCROW_AST_GREP_BIN`` or ``PATH``;
``go install ...@latest`` and friends are among the findings that got the
previous install rejected, and a client that quietly restored the behaviour
behind a different tool name would be worse than one that never had it.
Without ast-grep, ``codemod`` refuses by name, and ``scan`` still runs its
taint check and says the rule pack was skipped.

``codemod`` previews a rewrite as a diff unless ``dry_run=false``; the files it
writes become an overlay revision before the result is returned -- the same
contract ``edit`` has, for the same reason.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ..errors import AgentAction, ClientError, ErrorCode
from . import LocalContext, LocalResult, remap_aliases, text_result
from .arguments import bool_arg, int_arg, text_arg

if TYPE_CHECKING:
    from ..kit.astgrep import AstGrepAdapter

__all__ = ["run_codemod", "run_scan"]

_PROGRAM: Final[str] = "ast-grep"
_CODEMOD_ALIASES: Final[Mapping[str, str]] = {"file_glob": "glob"}


def _adapters(context: LocalContext) -> Callable[[Path], AstGrepAdapter]:
    """Adapters that find ast-grep the way this client may: override or ``PATH``."""
    from ..kit.astgrep import AstGrepAdapter, AstGrepToolUnavailable, discover_astgrep

    def resolve(root: Path) -> Path:
        resolution = discover_astgrep(root, env=context.environment)
        if not resolution.available or resolution.path is None:
            raise AstGrepToolUnavailable(resolution.to_payload())
        return resolution.path

    return lambda root: AstGrepAdapter(root, resolve_binary=resolve)


def _unavailable(payload: Mapping[str, Any]) -> ClientError:
    return ClientError(
        ErrorCode.LOCAL_TOOL_UNAVAILABLE,
        (
            f"{_PROGRAM} is not on PATH. The thin client never downloads or installs a "
            f"binary; ask your platform team to provision {_PROGRAM} (or point "
            f"LEMONCROW_AST_GREP_BIN at one), or use grep/code_search"
        ),
        details={"program": _PROGRAM, "checked": list(payload.get("checked") or ())},
        action=AgentAction.USE_CLIENT_TOOL,
    )


def run_scan(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """The bundled SAST rule pack plus the Python taint check, over the worktree or a path in it."""
    from ..kit.present import payload_text, strip_empty_values
    from ..kit.security_scan import run_scan_tool

    root_arg = text_arg("scan", arguments, "repo_root") or "."
    root = context.resolve(root_arg)
    path = text_arg("scan", arguments, "path")
    if path:
        # Confined like every client path; the scanner still gets it as spelled,
        # so findings name files exactly as the main package's scan does.
        context.resolve(path if Path(path).is_absolute() else str(Path(root_arg) / path))
    payload = run_scan_tool(
        root,
        path=path or None,
        include_taint=bool_arg("scan", arguments, "include_taint", True),
        include_rules=bool_arg("scan", arguments, "include_rules", True),
        adapter_factory=_adapters(context),
    )
    skipped = str(payload["summary"].get("rules_skipped") or "")
    return LocalResult(
        content=({"type": "text", "text": payload_text(strip_empty_values(payload), None)},),
        degraded=bool(skipped),
        degraded_reason=f"rule pack skipped: {skipped}" if skipped else "",
    )


def run_codemod(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """AST-shape search, or a rewrite previewed as a diff and applied only with ``dry_run=false``."""
    from ..kit.astgrep import AstGrepToolUnavailable, bound_rewrite_diff, one_based, render_pattern_text
    from ..kit.present import payload_text, strip_empty_values

    args = remap_aliases(arguments, _CODEMOD_ALIASES)
    pattern = text_arg("codemod", args, "pattern")
    if not pattern:
        raise ClientError(ErrorCode.PAYLOAD_INVALID, "pattern is required", action=AgentAction.FIX_REQUEST)
    language = text_arg("codemod", args, "language") or None
    glob = text_arg("codemod", args, "glob") or None
    rewrite = text_arg("codemod", args, "rewrite")
    limit = int_arg("codemod", args, "limit", 20)
    dry_run = bool_arg("codemod", args, "dry_run", True)
    adapter = _adapters(context)(context.repo_root)
    try:
        if rewrite is None:
            found = one_based(adapter.search(pattern=pattern, language=language, file_glob=glob, limit=limit))
            matches = [match.to_dict() for match in found.matches]
            total = found.total_matches if found.total_matches is not None else len(matches)
            text = render_pattern_text({"matches": matches, "truncated": found.truncated, "total_matches": total})
            return text_result(text or "- no matches")
        outcome = adapter.rewrite(pattern=pattern, rewrite=rewrite, language=language, file_glob=glob, dry_run=dry_run)
    except AstGrepToolUnavailable as exc:
        raise _unavailable(exc.payload) from exc
    except RuntimeError as exc:
        # A malformed pattern, an unknown language, a timeout: ast-grep's own words.
        return text_result(str(exc), is_error=True)
    payload = strip_empty_values({"diff": bound_rewrite_diff(outcome.diff), "files_changed": outcome.files_changed})
    return LocalResult(
        content=({"type": "text", "text": payload_text(payload, None)},),
        changed_paths=() if dry_run else tuple(outcome.files_changed),
    )
