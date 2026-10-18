"""``lc review`` — what changed, what it affects, and who made it.

A deterministic, LLM-free review surface: one libgit2 pass over a revision
range produces a packet that names the changed files, the reading order, the
places the change reaches outside the patch, and the agent session that
produced it. No model is called and no verdict is issued — the packet is
evidence for a human reviewer, which is why every rendering ends in
``Human review  REQUIRED``.

The default range is the working tree, not a commit range, because the moment a
developer actually reviews agent output is the one where the agent has stopped
and nothing is committed yet. It is also the only mode whose line numbers are
exact by construction -- the index and the tree agree -- where a commit range
depends on index geometry that can have drifted.

Heavy imports stay inside the callback so ``lc --help`` never pays for pygit2.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import click

from lemoncrow.gateway.cli.commands._shared import _emit
from lemoncrow.gateway.cli.commands.review_record_cli import ReviewCommand

if TYPE_CHECKING:  # `from __future__ import annotations` keeps this import out of the runtime path.
    from collections.abc import Callable, Mapping, Sequence

    from lemoncrow.pro.capabilities.review.gitdiff import RevRange
    from lemoncrow.pro.capabilities.review.models import ReviewPacket
    from lemoncrow.pro.capabilities.review.packet import PacketBuild
    from lemoncrow.pro.capabilities.review.session_models import (
        Annotation,
        FrontierEntry,
        ReviewMark,
        ReviewRevision,
        ReviewSession,
        ReviewUnit,
    )
    from lemoncrow.pro.capabilities.review.sources.local import RefreshResult
    from lemoncrow.pro.capabilities.review.store import ReviewStore
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

_CLEAN_TREE_NOTICE = "working tree clean — reviewing the last commit instead"


def _review_scope_label(rng: Any) -> str:
    if rng.mode == "working_tree":
        return f"{rng.base_rev or 'HEAD'} → working tree"
    if rng.mode == "staged":
        return f"{rng.base_rev or 'HEAD'} → staged index"
    return f"{rng.base_rev} → {rng.head_rev}"


# Mirrors ``session_models.MARK_STATES``. Restated rather than imported because
# ``click.Choice`` needs the values at decoration time, and this module's whole
# import discipline is that ``lc --help`` pays for nothing. Pinned equal to the
# real tuple by ``test_cli_review.py::test_mark_state_choices_match_the_model``.
_MARK_STATE_CHOICES = ("unreviewed", "reviewed", "needs_changes", "changed_since_review", "unknown")

# Mirrors ``session_models.ANNOTATION_KINDS``, restated for the same reason and
# pinned by ``test_cli_review.py::test_annotation_kind_choices_match_the_model``.
# Plan §5.5 caps the dispositions at these four; adding a fifth here without
# adding it there would let the CLI record something no other surface can read.
_ANNOTATION_KIND_CHOICES = ("comment", "request_change", "suggestion", "looks_good")


class _ReviewCliProgress:
    """Compact stage feedback for the expensive browser Review preparation path."""

    _DONE_SUFFIX = "_done"

    def __init__(self, *, enabled: bool, config: Any = None) -> None:
        self.enabled = enabled
        self._started: dict[str, float] = {}
        self._announced = False
        self._remote: Any = None
        self.browser_opened = False
        self._job_id = ""
        self._feedback_failed = False
        self.preparation_url = ""
        if enabled and config is not None and not config.hosted:
            from lemoncrow_client.errors import ClientError
            from lemoncrow_client.session import RemoteSession

            self._remote = RemoteSession(config)
            try:
                row = self._remote.review_reader_request("POST", "/api/review-preparations", body={}, timeout_s=2.0)
                self._job_id = str(row["id"])
                self.preparation_url = config.endpoint(f"/reviews/preparing/{self._job_id}")
            except (ClientError, KeyError, OSError, TypeError, ValueError):
                # Older/unavailable servers keep the existing capture-and-open path.
                self._remote.close()
                self._remote = None

    def open_browser(self, config: Any) -> None:
        if not self.preparation_url or self.browser_opened:
            return
        from urllib.parse import urlsplit

        from lemoncrow_client.errors import ClientError

        self("browser", "Opening Review Reader")
        try:
            if config.token:
                self._remote.pair_local_review_browser(urlsplit(self.preparation_url).path, timeout_s=2.0)
            if not _launch_browser_url(self.preparation_url):
                click.echo(f"(could not open a browser; open {self.preparation_url} manually.)", err=True)
            self.browser_opened = True
            self("browser_done", "Review Reader opened; preparation continues")
        except ClientError as exc:
            click.echo(f"  ! {_review_server_click_error(exc, hosted=False)}", err=True)

    def __enter__(self) -> _ReviewCliProgress:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._publish("failed" if exc_type else "ready", str(exc) if exc else "Review ready")
        if self._remote is not None:
            self._remote.close()

    def _publish(self, stage: str, detail: str, review_url: str = "") -> bool:
        if not self._job_id or self._remote is None:
            return False
        if self._feedback_failed and stage not in {"", "ready", "failed"}:
            return False
        from lemoncrow_client.errors import ClientError

        try:
            self._remote.review_reader_request(
                "POST",
                f"/api/review-preparations/{self._job_id}",
                body={"stage": stage, "detail": detail[:2048], "review_url": review_url},
                timeout_s=2.0,
            )
        except (ClientError, OSError, TypeError, ValueError):
            # Feedback cannot abort source capture or repeatedly wait on a busy server.
            self._feedback_failed = True
            return False
        self._feedback_failed = False
        return True

    def source_ready(self, url: str) -> bool:
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        return self._publish("", "", parsed.path + (f"#{parsed.fragment}" if parsed.fragment else ""))

    def __call__(self, stage: str, detail: str) -> None:
        self._publish(stage, detail)
        if not self.enabled:
            return
        if not self._announced:
            click.echo("Preparing review…", err=True)
            self._announced = True
        if stage.endswith(self._DONE_SUFFIX):
            key = stage[: -len(self._DONE_SUFFIX)]
            started = self._started.pop(key, None)
            elapsed = "" if started is None else f" · {time.monotonic() - started:.1f}s"
            click.echo(f"  ✓ {detail}{elapsed}", err=True)
            return
        if stage not in self._started:
            self._started[stage] = time.monotonic()
            click.echo(f"  → {detail}…", err=True)


@click.command("review", cls=ReviewCommand)
@click.argument("rev", required=False, default=None)
@click.option("--base", "base_rev", default=None, help="Explicit base revision (overrides REV).")
@click.option("--head", "head_rev", default=None, help="Explicit head revision. Default: HEAD.")
@click.option(
    "--pr",
    "pr_spec",
    default=None,
    metavar="NUMBER|URL",
    help=(
        "Review a GitHub pull request (NUMBER, OWNER/REPO#NUMBER or URL) from inside a clone of its "
        "repository. Rerun after new pushes: the same Review gets a new Revision."
    ),
)
@click.option(
    "--publish",
    "publish_pr",
    is_flag=True,
    help="With --pr, post your comments and verdict to the pull request as one GitHub review via your `gh` login.",
)
@click.option("--dry-run", "dry_run", is_flag=True, help="With --publish, show what would be posted and post nothing.")
@click.option(
    "--title",
    "snapshot_title",
    default=None,
    metavar="TITLE",
    help="Title this Snapshot for the reviewer; a hosted server applies it only to a new Review.",
)
@click.option(
    "--intent",
    "intent_specs",
    multiple=True,
    metavar="KIND:REF",
    help=(
        "Link an intent source (issue, spec, plan or incident) for pre-merge checks, e.g. spec:docs/auth.md "
        "or issue:#12. A repository file is captured as text; links are recorded as references. Repeatable."
    ),
)
@click.option(
    "--description-file",
    "description_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Record this file as the Change description for pre-merge checks (first line is the title).",
)
@click.option("--staged", is_flag=True, help="Review the staged index against HEAD.")
@click.option(
    "--working-tree",
    is_flag=True,
    help="Review uncommitted working-tree changes (the default; kept for scripts).",
)
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Repository to review. Default: the resolved workspace root.",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.option(
    "--setup",
    "setup_mode",
    is_flag=True,
    help="Inspect this repository's review surfaces/providers instead of opening a review.",
)
@click.option(
    "--write-review-config",
    is_flag=True,
    help="With --setup, write high-confidence detected surfaces to .lemoncrow/review.yaml.",
)
@click.option("--limit", default=40, show_default=True, type=int, help="Max files in the review order.")
@click.option("--no-impact", is_flag=True, help="Skip change-impact analysis (diff-only packet).")
@click.option("--no-provenance", is_flag=True, help="Skip agent-session correlation.")
@click.option("--session-id", default=None, help="Force provenance correlation to this session id.")
@click.option(
    "--html",
    "html_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Also write a self-contained HTML report to PATH.",
)
@click.option(
    "--open/--no-open",
    "open_html",
    default=None,
    help="Open the durable Review Reader in a browser (default). --no-open keeps this invocation terminal-only unless another durable-state option is used.",
)
@click.option(
    "--serve-workspace",
    "serve_workspace_flag",
    is_flag=True,
    hidden=True,
    help="Run the loopback review workspace in the foreground (used by --open).",
)
@click.option(
    "--track",
    is_flag=True,
    help="Update the durable review even when you are not opening the browser.",
)
@click.option("--units", "show_units", is_flag=True, help="List the reviewable units and their keys (implies --track).")
@click.option(
    "--mark",
    "mark_targets",
    multiple=True,
    metavar="TARGET",
    help=(
        "Record a verdict on a review target: the label a surface printed for it, a hun:/sym: unit key, "
        "or a repo-relative file path. A path -- or the file's fil: key -- is the imprecise spelling: it "
        "widens onto every target in that file rather than marking the file itself, and names them on "
        "stderr. Repeatable; implies --track."
    ),
)
@click.option(
    "--mark-state",
    type=click.Choice(list(_MARK_STATE_CHOICES)),
    default="reviewed",
    show_default=True,
    help="State recorded by --mark.",
)
@click.option("--marks", "show_marks", is_flag=True, help="Show the marks stored for this review (implies --track).")
@click.option(
    "--comment",
    "comment_body",
    default=None,
    metavar="TEXT",
    help="Leave a comment. Requires --on PATH:L10 (implies --track).",
)
@click.option(
    "--on",
    "comment_target",
    default=None,
    metavar="PATH:L10[-L20]",
    help="Where --comment attaches: a repo-relative path and a line or line range.",
)
@click.option(
    "--comment-kind",
    type=click.Choice(list(_ANNOTATION_KIND_CHOICES)),
    default="comment",
    show_default=True,
    help="Disposition recorded by --comment.",
)
@click.option(
    "--reply-to",
    "reply_to",
    default=None,
    metavar="ANNOTATION_ID",
    help="Attach --comment to an existing comment as a reply.",
)
@click.option(
    "--comments",
    "show_comments",
    is_flag=True,
    help="Show the comments stored for this review, anchors included (implies --track).",
)
@click.option(
    "--since-my-review",
    "since_my_review",
    is_flag=True,
    help="Reconcile against your last review: what changed since you looked, what is new (implies --track).",
)
@click.option(
    "--feedback",
    "send_feedback",
    is_flag=True,
    help="Print this review's human feedback as Markdown to hand back to the author or agent (implies --track).",
)
@click.option(
    "--finish",
    "finish_review",
    is_flag=True,
    help="Finish the LemonCrow review and report any work still outstanding (implies --track).",
)
@click.option(
    "--reopen-review",
    "reopen_review",
    is_flag=True,
    help="Undo --finish: put the review back in the open state.",
)
@click.option(
    "--check",
    "check_name",
    default=None,
    metavar="NAME",
    help=(
        "Run --run COMMAND in this checkout and record its result as check NAME on the exact Snapshot it ran "
        "against: PASS on exit 0, FAIL otherwise, UNKNOWN if the checkout changed while it ran. Exits with the "
        "command's status, so local git or agent hooks can call it."
    ),
)
@click.option(
    "--run",
    "check_command",
    default=None,
    metavar="COMMAND",
    help="The command --check runs, split like a shell command line and run without a shell.",
)
@click.option(
    "--reproduce",
    "reproduce_target",
    default=None,
    metavar="EVIDENCE_ID|RECORD.json",
    help=(
        "Replay one hermetic execution result from this checkout in the same offline sandbox and compare its "
        "outcome and environment fingerprint. Exits 0 only when the recorded source and stage outcomes reproduce. "
        "A preview result compares its source and dependency stages; its render is labelled not compared."
    ),
)
@click.option(
    "--trusted-base",
    "trusted_base_ref",
    default=None,
    metavar="REF",
    help=(
        "With --reproduce: the commit whose .lemoncrow/review.yaml supplies the execution policy "
        "(default: origin/HEAD, else HEAD). The record's own policy is only compared."
    ),
)
@click.option(
    "--use-recorded-policy",
    is_flag=True,
    default=False,
    help="With --reproduce: replay under the record's policy when it differs from the trusted base's (warns).",
)
@click.option(
    "--discard-review",
    "discard_review",
    is_flag=True,
    help="Make this review read-only and disposable; retention may permanently delete it and its evidence.",
)
@click.option(
    "--new",
    "new_review",
    is_flag=True,
    help="Start a new Review from origin/<branch> (or its default-branch fork point). Use --base COMMIT to choose the start.",
)
@click.option(
    "--continue",
    "continue_review",
    metavar="REVIEW_ID",
    default=None,
    help="Attach the current branch to an existing open Review.",
)
@click.option(
    "--current",
    "show_current",
    is_flag=True,
    help="Show this workspace's currently bound Review, author session, and feedback state without capturing.",
)
@click.option(
    "--resume-author",
    is_flag=True,
    help="Continue the exact author session bound to this workspace's current Review without creating a new session.",
)
@click.option(
    "--list-reviews",
    is_flag=True,
    help="List existing durable reviews without capturing a new Revision.",
)
@click.option(
    "--show-review",
    metavar="REVIEW_ID",
    default=None,
    help="Show one existing review without capturing a new Revision.",
)
@click.option(
    "--open-review",
    metavar="REVIEW_ID",
    default=None,
    help="Open one existing review by ID without capturing a new Revision.",
)
@click.option(
    "--fix",
    "fix_snapshot",
    metavar="REVIEW.N",
    default=None,
    help="Create or reuse a local fix worktree for Snapshot N of an existing Review.",
)
@click.option(
    "--apply-fix",
    "apply_fix_snapshot",
    metavar="REVIEW.N",
    default=None,
    help="Apply the latest captured fix for Snapshot N back to the main worktree.",
)
@click.option(
    "--with-conflict-markers",
    is_flag=True,
    help="With --apply-fix, write Git conflict markers instead of stopping on text conflicts.",
)
@click.option(
    "--review-status",
    type=click.Choice(["open", "finished", "archived", "all"]),
    default="all",
    show_default=True,
    help="Status filter used by --list-reviews.",
)
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Uncapped text output: every finding and call site, the file table, full reasons.",
)
@click.option("--no-color", is_flag=True, help="Disable ANSI colour / Rich output.")
@click.pass_context
def review_cmd(
    ctx: click.Context,
    rev: str | None,
    base_rev: str | None,
    head_rev: str | None,
    pr_spec: str | None,
    publish_pr: bool,
    dry_run: bool,
    snapshot_title: str | None,
    intent_specs: tuple[str, ...],
    description_file: Path | None,
    staged: bool,
    working_tree: bool,
    repo_root: Path | None,
    as_json: bool,
    setup_mode: bool,
    write_review_config: bool,
    limit: int,
    no_impact: bool,
    no_provenance: bool,
    session_id: str | None,
    html_path: Path | None,
    open_html: bool | None,
    serve_workspace_flag: bool,
    track: bool,
    show_units: bool,
    mark_targets: tuple[str, ...],
    mark_state: str,
    show_marks: bool,
    comment_body: str | None,
    comment_target: str | None,
    comment_kind: str,
    reply_to: str | None,
    show_comments: bool,
    since_my_review: bool,
    send_feedback: bool,
    finish_review: bool,
    reopen_review: bool,
    check_name: str | None,
    check_command: str | None,
    reproduce_target: str | None,
    trusted_base_ref: str | None,
    use_recorded_policy: bool,
    discard_review: bool,
    new_review: bool,
    continue_review: str | None,
    show_current: bool,
    resume_author: bool,
    list_reviews: bool,
    show_review: str | None,
    open_review: str | None,
    fix_snapshot: str | None,
    apply_fix_snapshot: str | None,
    with_conflict_markers: bool,
    review_status: str,
    show_all: bool,
    no_color: bool,
) -> None:
    """Review the current change.

    With no range flags, reviews uncommitted working-tree changes against HEAD.
    On a clean tree, reviews the last commit instead and says so. Use --staged,
    REV, or --base/--head to choose a different change.

    Normal `lc review` updates one durable Review for this repository/source and
    opens its stable Reader URL. Run it again after edits: the Review ID and URL
    stay the same, and changed content becomes a new immutable revision. Existing
    human review state is reconciled against that revision.

    Local and hosted installs use the configured LEMONCROW_URL. `lc review` does
    not start a per-repository server. Use --no-open for terminal-only inspection;
    add --track when a terminal-only invocation should still update durable Review
    state. Stateful options such as comments, marks, feedback, and finish also use
    the durable Review automatically.

    The browser Reader is optimized for the human loop: read the diff, leave
    feedback, rerun after fixes, and return to the same URL to review only what
    changed. ``--current`` reports that same Review/agent binding in the terminal;
    ``--resume-author`` continues its exact author session without copying IDs.
    """
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root, resolve_rev_range

    obj = ctx.obj or {}
    store_root = Path(obj.get("root") or default_store_root())
    try:
        from lemoncrow_client.config import load_config as load_client_config
        from lemoncrow_client.errors import ClientError

        client_config = load_client_config(cwd=repo_root or Path.cwd())
    except ClientError as exc:
        raise click.ClickException(exc.message) from exc
    if snapshot_title is not None:
        snapshot_title = snapshot_title.strip()
        if not snapshot_title or len(snapshot_title) > 120 or any(ord(char) < 32 for char in snapshot_title):
            raise click.ClickException("--title must be 1-120 characters on one line")
    management_actions = (
        int(list_reviews) + int(bool(show_review)) + int(bool(open_review)) + int(show_current) + int(resume_author)
    )
    fix_actions = int(bool(fix_snapshot)) + int(bool(apply_fix_snapshot))
    if fix_actions > 1:
        raise click.ClickException("--fix and --apply-fix are mutually exclusive")
    if with_conflict_markers and not apply_fix_snapshot:
        raise click.ClickException("--with-conflict-markers requires --apply-fix")
    if snapshot_title is not None and (
        management_actions or fix_actions or setup_mode or write_review_config or publish_pr
    ):
        raise click.ClickException("--title requires a Snapshot capture")
    if (intent_specs or description_file is not None) and (
        management_actions or fix_actions or setup_mode or write_review_config or publish_pr or check_name
    ):
        raise click.ClickException("--intent and --description-file require a Snapshot capture")
    if management_actions > 1:
        raise click.ClickException(
            "--current, --resume-author, --list-reviews, --show-review and --open-review are mutually exclusive"
        )
    if management_actions and fix_actions:
        raise click.ClickException("--fix/--apply-fix cannot be combined with other Review management actions")
    if new_review and continue_review:
        raise click.ClickException("--new and --continue are mutually exclusive")
    if (new_review or continue_review) and (management_actions or fix_actions):
        raise click.ClickException("--new/--continue cannot be combined with Review management-only actions")
    if reproduce_target is None and (trusted_base_ref is not None or use_recorded_policy):
        raise click.ClickException("--trusted-base and --use-recorded-policy go with --reproduce")
    if reproduce_target is not None:
        ctx.exit(
            _reproduce_execution(
                client_config,
                reproduce_target,
                repo_root=repo_root or detect_repo_root(Path.cwd()),
                store_root=store_root,
                as_json=as_json,
                trusted_base=trusted_base_ref,
                use_recorded_policy=use_recorded_policy,
            )
        )
    if (check_name is None) != (check_command is None):
        raise click.ClickException("--check NAME and --run COMMAND go together")
    if check_name is not None:
        combined = [
            flag
            for flag, used in (
                ("--current/--resume-author/--list-reviews/--show-review/--open-review", bool(management_actions)),
                ("--fix/--apply-fix", bool(fix_actions)),
                ("--setup/--write-review-config", setup_mode or write_review_config),
                ("--publish", publish_pr),
                ("--html", html_path is not None),
                ("--units", show_units),
                ("--mark", bool(mark_targets)),
                ("--marks", show_marks),
                ("--comment", comment_body is not None),
                ("--comments", show_comments),
                ("--since-my-review", since_my_review),
                ("--feedback", send_feedback),
                ("--finish", finish_review),
                ("--reopen-review", reopen_review),
                ("--discard-review", discard_review),
            )
            if used
        ]
        if combined:
            raise click.ClickException(f"--check runs on its own; drop {', '.join(combined)}")
    judgment = [
        flag
        for flag, used in (
            ("--mark", bool(mark_targets)),
            ("--comment", comment_body is not None),
            ("--finish", finish_review),
            ("--reopen-review", reopen_review),
            ("--discard-review", discard_review),
            ("--publish", publish_pr and not dry_run),
        )
        if used
    ]
    if judgment:
        from lemoncrow.core.foundation.paths import agent_session_host

        agent_host = agent_session_host()
        if agent_host:
            # These record *human* review judgment. From the coding agent's own
            # shell they would let the agent approve, comment or publish as the
            # reviewer; capture, reads and --check stay available to agents.
            raise click.ClickException(
                f"{', '.join(judgment)} record(s) human review judgment and cannot run inside an agent "
                f"session ({agent_host}); use the Review Reader or your own terminal"
            )
    if fix_actions:
        _manage_fix_line(
            client_config,
            fix_snapshot=fix_snapshot,
            apply_fix_snapshot=apply_fix_snapshot,
            with_conflict_markers=with_conflict_markers,
            as_json=as_json,
        )
        return
    if show_current or resume_author:
        try:
            current_root = detect_repo_root(repo_root)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        _manage_current_review(
            client_config,
            store_root=store_root,
            repo_root=current_root,
            resume_author=resume_author,
            as_json=as_json,
        )
        return
    if management_actions:
        _manage_server_reviews(
            client_config,
            list_reviews=list_reviews,
            show_review=show_review,
            open_review=open_review,
            status_filter=review_status,
            as_json=as_json,
        )
        return
    if write_review_config:
        setup_mode = True
    if setup_mode:
        from lemoncrow.pro.capabilities.review.review_setup import (
            inspect_review_setup,
            render_review_setup,
            write_detected_review_config,
        )

        try:
            resolved_root = detect_repo_root(repo_root)
            report = inspect_review_setup(resolved_root)
            written = None
            if write_review_config:
                written = write_detected_review_config(report)
        except (FileExistsError, OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        if as_json:
            payload = report.to_payload()
            if written is not None:
                payload["written_config"] = str(written)
            _emit(payload, as_json=True)
        else:
            text = render_review_setup(report)
            if written is not None:
                text += f"\n\nWrote  {written}"
            _emit(text, as_json=False)
        return

    effective_open = _effective_open(
        open_html,
        as_json=as_json,
        html_path=html_path,
        show_units=show_units,
        mark_targets=mark_targets,
        show_marks=show_marks,
        comment_body=comment_body,
        show_comments=show_comments,
        since_my_review=since_my_review,
        send_feedback=send_feedback,
        finish_review=finish_review,
        reopen_review=reopen_review,
        discard_review=discard_review,
        show_all=show_all,
        no_color=no_color,
    )
    durable_review_requested = (
        track
        or snapshot_title is not None
        or bool(intent_specs)
        or description_file is not None
        or effective_open
        or show_units
        or bool(mark_targets)
        or show_marks
        or comment_body is not None
        or show_comments
        or since_my_review
        or send_feedback
        or finish_review
        or reopen_review
        or discard_review
        or new_review
        or bool(continue_review)
    )

    if serve_workspace_flag:
        raise click.ClickException(
            "the per-repository Review workspace server is retired; `lc review` uses LEMONCROW_URL in both local and hosted mode"
        )
    if publish_pr and pr_spec is None:
        raise click.ClickException("--publish needs --pr NUMBER|URL")
    if dry_run and not publish_pr:
        raise click.ClickException("--dry-run only applies to --publish")
    if pr_spec is not None and (rev or base_rev or head_rev or staged or working_tree):
        raise click.ClickException("--pr picks the range itself; drop REV, --base, --head, --staged and --working-tree")
    if pr_spec is not None and (new_review or continue_review):
        raise click.ClickException(
            "--new/--continue apply to local branch Reviews; --pr already has stable PR identity"
        )
    if publish_pr:
        assert pr_spec is not None
        _publish_pull_request(client_config, detect_repo_root(repo_root), pr_spec, dry_run=dry_run, as_json=as_json)
        return
    try:
        resolved_root = detect_repo_root(repo_root)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    intent_payload = _intent_payload(resolved_root, intent_specs, description_file)
    with _ReviewCliProgress(enabled=effective_open and check_name is None, config=client_config) as progress:
        progress.open_browser(client_config)
        progress("range", "Resolving review range")
        source_ref_override = ""
        title_override = snapshot_title or ""
        try:
            if pr_spec is not None:
                from dataclasses import replace

                from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError, resolve_pull_request

                try:
                    pull = resolve_pull_request(resolved_root, pr_spec)
                except GitHubCliError as exc:
                    raise click.ClickException(str(exc)) from exc
                rng = replace(
                    resolve_rev_range(resolved_root, base=pull.merge_base_sha, head=pull.head_sha),
                    title=pull.review_title,
                )
                source_ref_override = pull.ref.source_ref
                title_override = snapshot_title or pull.review_title
            else:
                bare_request = _is_bare(rev, base_rev, head_rev, staged=staged, working_tree=working_tree)
                branch_review_base = ""
                implicit_working_tree = bool(bare_request and (new_review or continue_review))
                if bare_request and durable_review_requested and not implicit_working_tree:
                    _active_review_id, branch_review_base = _active_local_branch_review(
                        client_config, store_root=store_root, repo_root=resolved_root
                    )
                    implicit_working_tree = bool(branch_review_base)
                if new_review:
                    from lemoncrow.pro.capabilities.review.gitdiff import resolve_new_review_range

                    if head_rev is not None:
                        raise ValueError(
                            "--new captures the working tree or index; use --base to choose its starting commit"
                        )
                    creation_base = base_rev if base_rev is not None else rev
                    if creation_base is not None and ".." in creation_base:
                        raise ValueError("--new requires one base commit, not a revision range")
                    rng = resolve_new_review_range(resolved_root, base=creation_base, staged=staged)
                else:
                    rng = resolve_rev_range(
                        resolved_root,
                        rev,
                        base=base_rev,
                        head=head_rev,
                        staged=staged,
                        working_tree=working_tree or implicit_working_tree,
                    )
                if branch_review_base and rng.mode == "working_tree":
                    from lemoncrow.pro.capabilities.review.gitdiff import with_review_base

                    rng = with_review_base(resolved_root, branch_review_base, rng)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        progress("range_done", f"Review scope: {_review_scope_label(rng)}")

        if (new_review or continue_review) and rng.mode not in {"working_tree", "staged"}:
            raise click.ClickException(
                "--new/--continue require a mutable branch Review; use --working-tree or --staged"
            )

        if check_name is not None:
            assert check_command is not None
            ctx.exit(
                _review_server_check(
                    client_config,
                    resolved_root,
                    rng,
                    name=check_name,
                    command=check_command,
                    store_root=store_root,
                    session_id=session_id,
                    limit=limit,
                    no_impact=no_impact,
                    no_provenance=no_provenance,
                    as_json=as_json,
                    source_ref_override=source_ref_override,
                    title_override=title_override,
                    snapshot_title=snapshot_title or "",
                )
            )

        # A bare `lc review` asks "is what my agent just did safe to commit?", and on
        # a clean tree the honest answer is about the last commit rather than an
        # empty packet. `resolve_rev_range` already makes that substitution; saying
        # nothing about it would leave the reader to work out which change they are
        # looking at from the range label alone.
        fell_back = (
            pr_spec is None
            and _is_bare(rev, base_rev, head_rev, staged=staged, working_tree=working_tree)
            and rng.mode != "working_tree"
        )

        # One packet, whether or not it is persisted. Building a second one for the
        # store would cost the whole impact pass twice and, worse, could disagree
        # with the one on screen -- a reviewer must never mark a packet they were
        # not shown. Hunk bodies are captured only when the packet is being kept.
        #
        # When it is kept, the build hands back the blob texts it actually read, and
        # those -- not a second read of the working tree -- are what gets
        # fingerprinted. A file saved between two reads would otherwise be stored as
        # one thing and marked as another.
        # `--open` implies tracking: the workspace *is* the durable session, and
        # there is nothing for it to open unless the revision was recorded.
        if comment_body is not None and not comment_target and not reply_to:
            raise click.ClickException("--comment needs --on PATH:L10 (or --reply-to an existing comment)")
        if comment_target and comment_body is None:
            raise click.ClickException("--on needs --comment TEXT")
        status_flags = [
            name
            for name, wanted in (
                ("--finish", finish_review),
                ("--reopen-review", reopen_review),
                ("--discard-review", discard_review),
            )
            if wanted
        ]
        if len(status_flags) > 1:
            raise click.ClickException(f"{' and '.join(status_flags)} ask for opposite things; pick one")

        server_review = durable_review_requested
        if not server_review:
            from lemoncrow.pro.capabilities.review.packet import build_review_packet
            from lemoncrow.pro.capabilities.review.render import render_review

            # Explicit terminal-only review remains useful offline. It creates no
            # durable state and therefore needs neither a local server nor a hosted
            # one. The moment the caller asks to open, track, mark, comment or
            # finish, the one server-backed path below owns the operation.
            packet = build_review_packet(
                resolved_root,
                rng,
                store_root=store_root,
                with_impact=not no_impact,
                with_provenance=not no_provenance,
                session_id=session_id,
                limit=limit,
            )
            written = _write_html(packet, html_path) if html_path is not None else None
            if as_json:
                if written is not None:
                    click.echo(f"HTML report: {written}", err=True)
                _emit(packet.to_dict(), as_json=True)
                return
            if fell_back:
                click.secho(_CLEAN_TREE_NOTICE, dim=not no_color)
            _emit(render_review(packet, no_color=no_color, show_all=show_all), as_json=False)
            if written is not None:
                click.echo(f"\nHTML report: {written}")
            return

        _review_server_capture(
            client_config,
            resolved_root,
            rng,
            store_root=store_root,
            session_id=session_id,
            limit=limit,
            no_impact=no_impact,
            no_provenance=no_provenance,
            html_path=html_path,
            effective_open=effective_open,
            as_json=as_json,
            show_all=show_all,
            no_color=no_color,
            fell_back=fell_back,
            show_units=show_units,
            mark_targets=mark_targets,
            mark_state=mark_state,
            show_marks=show_marks,
            comment_body=comment_body,
            comment_target=comment_target,
            comment_kind=comment_kind,
            reply_to=reply_to,
            show_comments=show_comments,
            since_my_review=since_my_review,
            send_feedback=send_feedback,
            finish_review=finish_review,
            reopen_review=reopen_review,
            discard_review=discard_review,
            force_new_review=new_review,
            continue_review_id=continue_review or "",
            source_ref_override=source_ref_override,
            title_override=title_override,
            snapshot_title=snapshot_title or "",
            cli_progress=progress,
            intent_payload=intent_payload,
        )
        return


_MAX_INTENT_FILE_BYTES = 65536


def _intent_payload(repo_root: Path, specs: tuple[str, ...], description_file: Path | None) -> dict[str, Any] | None:
    """The ``POST /intent`` body for ``--intent``/``--description-file`` (``None`` when neither is given).

    Repository files are read from the working tree, bounded, and only inside
    the repository; URLs and issue numbers are recorded as references and are
    never fetched from here.
    """

    from lemoncrow.pro.capabilities.review.intent import MAX_INTENT_SOURCES, parse_cli_intent

    if not specs and description_file is None:
        return None
    if len(specs) > MAX_INTENT_SOURCES:
        raise click.ClickException(f"--intent accepts at most {MAX_INTENT_SOURCES} sources")

    def read_bounded(path: Path) -> str:
        with path.open("rb") as handle:
            return handle.read(_MAX_INTENT_FILE_BYTES).decode("utf-8", errors="replace")

    root = repo_root.resolve()
    references: list[dict[str, Any]] = []
    for spec in specs:
        try:
            reference = parse_cli_intent(spec)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        raw = spec.partition(":")[2].strip()
        row: dict[str, Any] = {"kind": reference.kind, "ref": raw}
        if reference.ref.startswith("file:"):
            relative = reference.ref.removeprefix("file:")
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise click.ClickException(f"--intent {spec!r} names no file inside this repository")
            row.update({"title": relative, "body": read_bounded(target)})
        references.append(row)
    payload: dict[str, Any] = {"references": references}
    if description_file is not None:
        text = read_bounded(description_file)
        title, _newline, body = text.strip().partition("\n")
        payload["description"] = {"title": title.strip().lstrip("#").strip()[:300], "body": body.strip()}
    return payload


def _publish_pull_request(config: Any, repo_root: Path, pr_spec: str, *, dry_run: bool, as_json: bool) -> None:
    """Preview, then post, the PR's LemonCrow Review as one GitHub review."""

    if config.hosted:
        raise click.ClickException(
            "publishing to GitHub runs only on your local LemonCrow server, where your `gh` login lives"
        )
    from urllib.parse import quote

    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted import server_review_request, server_review_rows
    from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError, pull_request_ref

    try:
        pr = pull_request_ref(repo_root, pr_spec)
    except (GitHubCliError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        rows = [row for row in server_review_rows(config) if row.get("source_ref") == pr.source_ref]
        if not rows:
            raise click.ClickException(f"no Review of {pr.url} yet; run `lc review --pr {pr.number}` first")
        review_id = str(rows[0].get("id") or "")
        base = f"/api/reviews/{quote(review_id, safe='')}/github"
        preview = server_review_request(config, "POST", f"{base}/preview", body={})
        if dry_run or preview.get("empty") or preview.get("stale"):
            _emit_publish_preview(preview, as_json=as_json)
            if preview.get("stale"):
                raise click.ClickException(
                    f"{pr.url} has new commits since this Review; run `lc review --pr {pr.number}` to refresh, then publish"
                )
            if preview.get("empty") and not dry_run:
                raise click.ClickException("there is nothing new to publish")
            return
        published = server_review_request(
            config,
            "POST",
            f"{base}/publish",
            body={
                "operation_id": preview.get("operation_id"),
                "expected_revision_id": preview.get("revision_id"),
                "expected_plan_hash": preview.get("plan_hash"),
            },
        )
    except ClientError as exc:
        raise _review_server_click_error(exc, hosted=False) from exc
    if as_json:
        _emit({"preview": preview, "published": published}, as_json=True)
        return
    _emit_publish_preview(preview, as_json=False)
    click.echo(f"\nPublished  {published.get('html_url') or pr.url}")


def _emit_publish_preview(preview: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        _emit(preview, as_json=True)
        return
    pr_raw = preview.get("pr")
    pr = pr_raw if isinstance(pr_raw, dict) else {}
    inline = [item for item in preview.get("inline") or [] if isinstance(item, dict)]
    folded = [item for item in preview.get("folded") or [] if isinstance(item, dict)]
    click.echo(f"GitHub review  {pr.get('url', '')}")
    click.echo(f"Event          {preview.get('event', 'COMMENT')}")
    click.echo(f"Commit         {str(preview.get('commit_id') or '')[:12]}")
    click.echo(f"Inline         {len(inline)}")
    for item in inline:
        start, line = int(item.get("start_line") or 0), int(item.get("line") or 0)
        span = f"L{start}" + (f"-L{line}" if line != start else "")
        click.echo(f"  {item.get('path')}:{span} ({item.get('side')})")
    click.echo(f"In body        {len(folded)}")
    for item in folded:
        click.echo(f"  {item.get('location')}: {item.get('reason')}")
    for notice in preview.get("notices") or []:
        click.echo(f"Note           {notice}")
    if preview.get("empty"):
        click.echo("Nothing new to publish.")


def _review_server_capture(
    config: Any,
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    session_id: str | None,
    limit: int,
    no_impact: bool,
    no_provenance: bool,
    html_path: Path | None,
    effective_open: bool,
    as_json: bool,
    show_all: bool,
    no_color: bool,
    fell_back: bool,
    show_units: bool,
    mark_targets: tuple[str, ...],
    mark_state: str,
    show_marks: bool,
    comment_body: str | None,
    comment_target: str | None,
    comment_kind: str,
    reply_to: str | None,
    show_comments: bool,
    since_my_review: bool,
    send_feedback: bool,
    finish_review: bool,
    reopen_review: bool,
    discard_review: bool,
    force_new_review: bool = False,
    continue_review_id: str = "",
    source_ref_override: str = "",
    title_override: str = "",
    snapshot_title: str = "",
    cli_progress: _ReviewCliProgress | None = None,
    intent_payload: Mapping[str, Any] | None = None,
) -> None:
    """Publish the diff, then apply terminal Review actions on that server."""
    if config.hosted and not config.authenticated:
        raise click.ClickException(
            "Hosted Review requires sign-in; authenticate to the configured LemonCrow server first"
        )

    from contextlib import nullcontext

    from lemoncrow_client.errors import ClientError

    progress = cli_progress or _ReviewCliProgress(enabled=effective_open, config=config)
    with progress if cli_progress is None else nullcontext(progress):
        progress.open_browser(config)
        browser_opened = progress.browser_opened
        progress("capture", "Starting source preparation")
        from lemoncrow.pro.capabilities.review.hosted import capture_server_review, server_review_request
        from lemoncrow.pro.capabilities.review.render import render_review

        progress("capture_done", "Source preparation started")

        def open_ready(capture: Any) -> None:
            nonlocal browser_opened
            if not effective_open or (browser_opened and not progress.preparation_url):
                return
            review_value = capture.response.get("review")
            review_row = review_value if isinstance(review_value, dict) else {}
            browser_id = str(review_row.get("ref") or f"r/{capture.review_id}")
            if not browser_opened:
                progress("browser", "Pairing and opening Review Reader on the ready source diff")
            try:
                if not config.hosted and rng.mode == "working_tree":
                    from lemoncrow.pro.capabilities.review.live_runtime import claim_live_review_binding

                    revision_value = capture.response.get("revision")
                    revision_row = revision_value if isinstance(revision_value, dict) else {}
                    expected_revision_id = str(revision_row.get("id") or "")
                    claimed = claim_live_review_binding(
                        store_root,
                        repo_root,
                        server_url=config.url,
                        expected_revision_id=expected_revision_id,
                    )
                    if claimed is None:
                        raise click.ClickException(
                            "prepared Review changed before it could be claimed; completing capture before opening"
                        )
                    # The browser follows the durable Change URL. The exact Snapshot
                    # remains addressable through /rr/<snapshot>, but the default
                    # interactive Review route should stay stable as new Snapshots
                    # arrive underneath it. `capture.review_url` carries the short
                    # /r/<token> plus temporary preparation state in the fragment.
                    open_url = capture.review_url
                else:
                    open_url = capture.review_url
                if browser_opened and progress.preparation_url and progress.source_ready(open_url):
                    return
                _open_server_review(open_url, config=config, review_id=browser_id, open_browser=True)
                browser_opened = True
                progress("browser_done", "Review Reader opened; remaining context continues")
            except click.ClickException as exc:
                # Browser pairing/opening must not strand an otherwise valid Review
                # before its base/runtime context has finished attaching. Surface the
                # warning and let the capture finalize; the canonical URL is printed.
                click.echo(f"  ! {exc}", err=True)

        try:
            captured = capture_server_review(
                config,
                repo_root,
                rng,
                store_root=store_root,
                session_id=session_id,
                limit=limit,
                with_impact=not no_impact,
                with_provenance=not no_provenance,
                restore_archived=reopen_review,
                progress=progress,
                ready=open_ready,
                source_ref_override=source_ref_override,
                title_override=title_override,
                advance_claimed_binding=True,
                defer_post_ready=bool(
                    effective_open and not config.hosted and rng.mode in {"working_tree", "commit_range"}
                ),
                force_new_review=force_new_review,
                continue_review_id=continue_review_id,
                reuse_closed_review=finish_review or reopen_review or discard_review,
            )
        except ClientError as exc:
            raise _review_server_click_error(exc, hosted=bool(config.hosted)) from exc
        except ValueError as exc:
            raise _review_capture_value_error(exc, rng=rng, force_new_review=force_new_review) from exc

        revision_value = captured.response.get("revision")
        revision_row = revision_value if isinstance(revision_value, dict) else {}
        revision_number = revision_row.get("revision_number")
        if not isinstance(revision_number, int):
            raise click.ClickException("Review server did not return a Snapshot number")
        try:
            server_review_request(
                config,
                "POST",
                f"/api/reviews/{quote(captured.review_id, safe='')}/revisions/{revision_number}/publish",
                body={},
            )
            if intent_payload is not None:
                # Linked intent is recorded on the Snapshot just published.
                server_review_request(
                    config,
                    "POST",
                    f"/api/reviews/{quote(captured.review_id, safe='')}/intent",
                    body={**intent_payload, "expected_revision_id": str(revision_row.get("id") or "")},
                )
        except ClientError as exc:
            raise _review_server_click_error(exc, hosted=bool(config.hosted)) from exc

        _warn_unapplied_title(captured.response, snapshot_title)
        packet = captured.build.packet
        written = _write_html(packet, html_path) if html_path is not None else None
        review_value = captured.response.get("review")
        review_row = review_value if isinstance(review_value, dict) else {}
        browser_review_id = str(review_row.get("ref") or f"r/{captured.review_id}")
        try:
            terminal_state = _server_terminal_state(
                config,
                captured.review_id,
                mark_targets=mark_targets,
                mark_state=mark_state,
                show_units=show_units,
                show_marks=show_marks,
                comment_body=comment_body,
                comment_target=comment_target,
                comment_kind=comment_kind,
                reply_to=reply_to,
                show_comments=show_comments,
                since_my_review=since_my_review,
                send_feedback=send_feedback,
                finish_review=finish_review,
                reopen_review=reopen_review,
                discard_review=discard_review,
                progress=progress,
            )
            public_review_id = str(review_row.get("short_id") or "")
            revision_value = captured.response.get("revision")
            revision_row = revision_value if isinstance(revision_value, dict) else {}
            public_revision_id = str(revision_row.get("short_id") or "")
            if public_review_id:
                terminal_state["review_id"] = f"r/{public_review_id}"
            if public_revision_id:
                terminal_state["revision_id"] = f"rr/{public_revision_id}"
        except ClientError as exc:
            raise _review_server_click_error(exc, hosted=bool(config.hosted)) from exc

        if as_json:
            if written is not None:
                click.echo(f"HTML report: {written}", err=True)
            click.echo(json.dumps(terminal_state, ensure_ascii=False), err=True)
            _emit(packet.to_dict(), as_json=True)
            if not browser_opened:
                progress("browser", "Pairing and opening Review Reader")
                _open_server_review(
                    captured.review_url, config=config, review_id=browser_review_id, open_browser=effective_open
                )
                progress("browser_done", "Review Reader opened")
            return

        if fell_back:
            click.secho(_CLEAN_TREE_NOTICE, dim=not no_color)
        _emit(render_review(packet, no_color=no_color, show_all=show_all), as_json=False)
        click.echo(_render_state(terminal_state))
        click.echo(f"\nReview  {captured.canonical_url}")
        if written is not None:
            click.echo(f"HTML report: {written}")
        if not browser_opened:
            progress("browser", "Pairing and opening Review Reader")
            _open_server_review(
                captured.review_url, config=config, review_id=browser_review_id, open_browser=effective_open
            )
            progress("browser_done", "Review Reader opened")


def _warn_unapplied_title(response: dict[str, Any], requested: str) -> None:
    """Say so when a capture could not title its Snapshot as ``--title`` asked.

    A local server keeps the title as the Snapshot's author title. A hosted
    server has no per-Snapshot title: it titles only a Review the capture
    creates, so a Snapshot joining an existing hosted Review keeps that title.
    """

    if not requested:
        return
    review_value = response.get("review")
    review_row = review_value if isinstance(review_value, dict) else {}
    title = str(review_row.get("title") or "")
    if requested not in {title, str(review_row.get("auto_title") or "")}:
        click.echo(f'  ! --title was not applied; the Review this Snapshot joined keeps the title "{title}"', err=True)


def _reproduce_execution(
    config: Any,
    target: str,
    *,
    repo_root: Path,
    store_root: Path,
    as_json: bool,
    trusted_base: str | None = None,
    use_recorded_policy: bool = False,
) -> int:
    """``--reproduce``: replay one recorded hermetic execution from this checkout.

    The policy comes from the local trusted base; the record's policy is only
    compared. A mismatch fails unless ``--use-recorded-policy`` is passed.
    """

    from urllib.parse import quote

    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hermetic import HermeticPolicy, HermeticUnavailable
    from lemoncrow.pro.capabilities.review.reproduce import (
        ReproductionError,
        default_trusted_base,
        load_execution_record,
        recorded_policy_matches,
        reproduce_execution,
        trusted_base_commands,
        trusted_base_policy,
    )

    record_path = Path(target)
    raw: Any
    if record_path.is_file():
        try:
            raw = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise click.ClickException(f"{target} is not a JSON execution record: {exc}") from exc
    else:
        from lemoncrow.pro.capabilities.review.hosted import server_review_request

        try:
            raw = server_review_request(config, "GET", f"/api/evidence/{quote(target, safe='')}/content")
        except ClientError as exc:
            raise _review_server_click_error(exc, hosted=bool(config.hosted)) from exc
    if not isinstance(raw, dict):
        raise click.ClickException("execution record must be a JSON object")
    try:
        original = load_execution_record(raw)
        base = trusted_base or default_trusted_base(repo_root)
        local_policy = trusted_base_policy(repo_root, base)
        policy: HermeticPolicy | None = local_policy
        source = f"trusted_base:{base}"
        if not recorded_policy_matches(original, local_policy):
            message = (
                f"the record's execution policy differs from the trusted base's ({base}:.lemoncrow/review.yaml); "
                "organization policy or a changed trusted base can cause this"
            )
            if not use_recorded_policy:
                click.echo(f"policy mismatch: {message}", err=True)
                click.echo("pass --use-recorded-policy to replay under the record's policy instead", err=True)
                return 1
            click.echo(f"warning: {message}; replaying under the RECORDED policy (--use-recorded-policy)", err=True)
            policy, source = None, "recorded"
        outcome = reproduce_execution(
            original,
            repo=repo_root,
            store_root=store_root,
            policy=policy,
            policy_source=source,
            # Even under --use-recorded-policy the record never chooses what runs.
            trusted_commands=trusted_base_commands(repo_root, base),
        )
    except (ReproductionError, HermeticUnavailable) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(outcome.to_wire(), as_json=True)
    else:
        workspace = {True: "match", False: "differs", None: "unverifiable"}[outcome.workspace_match]
        click.echo(f"{'reproduced' if outcome.reproduced else 'NOT reproduced'}  {original.identity}")
        click.echo(f"  source: {outcome.materialized} ({workspace})")
        click.echo(f"  policy: {outcome.policy_source}")
        click.echo(f"  compared: {', '.join(outcome.compared_outputs)}")
        for stage, before, after in outcome.compared:
            click.echo(f"  {stage}: recorded {before} -> replayed {after}")
        for stage, recorded, reason in outcome.not_compared:
            click.echo(f"  {stage}: recorded {recorded}, not compared ({reason})")
        for stage in outcome.not_replayed:
            click.echo(f"  {stage}: not replayable locally")
        if outcome.environment_differences:
            click.echo(f"  environment differs: {', '.join(outcome.environment_differences)}")
        click.echo(f"  {outcome.replay.verification.statement}")
    return 0 if outcome.reproduced else 1


def _checkout_identity(repo_root: Path) -> str:
    """What a command run in *repo_root* sees: HEAD plus every non-ignored working-tree change."""

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state

    working = resolve_rev_range(repo_root, working_tree=True)
    return f"{working.base_sha}\0{source_state(repo_root, working).fingerprint}"


def _review_server_check(
    config: Any,
    repo_root: Path,
    rng: RevRange,
    *,
    name: str,
    command: str,
    store_root: Path,
    session_id: str | None,
    limit: int,
    no_impact: bool,
    no_provenance: bool,
    as_json: bool,
    source_ref_override: str = "",
    title_override: str = "",
    snapshot_title: str = "",
) -> int:
    """Run one local check and record it on the exact Snapshot it ran against.

    The command runs in the checkout, so its result can describe a Snapshot only
    when the checkout *is* that Snapshot: a working-tree Review, or the commit
    HEAD checks out on a clean tree. The checkout is fingerprinted around the
    capture and the run; if it moved, the result is recorded as UNKNOWN rather
    than lending a PASS/FAIL to source the command never saw.
    """

    import shlex
    import subprocess
    from urllib.parse import quote

    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted import capture_server_review, server_review_request

    if config.hosted and not config.authenticated:
        raise click.ClickException(
            "Hosted Review requires sign-in; authenticate to the configured LemonCrow server first"
        )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise click.ClickException(f"--run could not be parsed: {exc}") from exc
    if not name.strip() or not argv:
        raise click.ClickException("--check needs a NAME and --run a COMMAND")
    try:
        before = _checkout_identity(repo_root)
        head = before.split("\0", 1)[0]
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    in_checkout = rng.mode == "working_tree" or (
        rng.mode == "commit_range" and not rng.dirty and bool(rng.head_sha) and rng.head_sha == head
    )
    if not in_checkout:
        raise click.ClickException(
            "--check can only bind to a Snapshot the checkout holds: a working-tree Review, or the commit HEAD "
            "checks out on a clean tree; drop REV/--base/--head/--staged/--pr or check out that commit first"
        )
    try:
        captured = capture_server_review(
            config,
            repo_root,
            rng,
            store_root=store_root,
            session_id=session_id,
            limit=limit,
            with_impact=not no_impact,
            with_provenance=not no_provenance,
            source_ref_override=source_ref_override,
            title_override=title_override,
        )
    except ClientError as exc:
        raise _review_server_click_error(exc, hosted=bool(config.hosted)) from exc
    except ValueError as exc:
        raise _review_capture_value_error(exc, rng=rng, force_new_review=False) from exc
    _warn_unapplied_title(captured.response, snapshot_title)
    revision_value = captured.response.get("revision")
    revision = revision_value if isinstance(revision_value, dict) else {}
    revision_id = str(revision.get("id") or "")
    if not revision_id:
        raise click.ClickException("the Review server captured the Snapshot without returning its id")
    if _checkout_identity(repo_root) != before:
        raise click.ClickException("the checkout changed while its Snapshot was captured; run the check again")

    started = time.monotonic()
    exit_code: int | None
    try:
        # JSON stays alone on stdout; the command's own output goes to stderr.
        exit_code = subprocess.run(argv, cwd=repo_root, check=False, stdout=2 if as_json else None).returncode
        failure = ""
    except OSError as exc:
        exit_code, failure = None, str(exc)
    elapsed = time.monotonic() - started
    bound = _checkout_identity(repo_root) == before
    if exit_code is None:
        status, detail = "NOT_RUN", f"`{command}` could not start: {failure}"
    elif not bound:
        status = "UNKNOWN"
        detail = (
            f"`{command}` exited {exit_code} after {elapsed:.1f}s, but the checkout changed while it ran, "
            "so the result describes no Snapshot."
        )
    else:
        status = "PASS" if exit_code == 0 else "FAIL"
        detail = f"`{command}` exited {exit_code} after {elapsed:.1f}s."
    try:
        server_review_request(
            config,
            "POST",
            f"/api/reviews/{quote(captured.review_id, safe='')}/verification",
            body={
                "revision_id": revision_id,
                "name": name.strip(),
                "status": status,
                "source": "test",
                "source_ref": command[:500],
                "detail": detail,
            },
        )
    except ClientError as exc:
        raise _review_server_click_error(exc, hosted=bool(config.hosted)) from exc
    revision_number = revision.get("revision_number")
    if as_json:
        _emit(
            {
                "review_id": captured.review_id,
                "revision_id": revision_id,
                "revision_number": revision_number,
                "name": name.strip(),
                "status": status,
                "exit_code": exit_code,
                "duration_s": round(elapsed, 3),
                "detail": detail,
            },
            as_json=True,
        )
    else:
        click.echo(f"{status}  {name.strip()}  {elapsed:.1f}s  Revision {revision_number}  {captured.canonical_url}")
    if status == "PASS":
        return 0
    # A signal-killed command reports a negative status; hooks still need a failure.
    return exit_code if status == "FAIL" and exit_code is not None and exit_code > 0 else 1


def _review_capture_value_error(
    exc: ValueError,
    *,
    rng: RevRange,
    force_new_review: bool,
) -> click.ClickException:
    """Turn a stale mutable-Review base into an actionable recovery error."""

    import re

    message = str(exc)
    # The saved base is the range's own on a bare `lc review`; with --continue,
    # --working-tree or --staged the capture switches to it itself, so the lost
    # revision is read from the error rather than from this range.
    lost = re.fullmatch(r"cannot resolve revision '([0-9a-f]{7,64})'", message)
    if lost is not None and not force_new_review and rng.mode in {"working_tree", "staged"}:
        return click.ClickException(
            f"cannot continue the current Review: saved base revision {lost.group(1)!r} is no longer available "
            "in this checkout (for example after a rebase, history rewrite, or pruning).\n"
            "Start a new Review from the current branch state with:\n"
            "  lc review --new"
        )
    return click.ClickException(message)


def _review_server_click_error(exc: Any, *, hosted: bool) -> click.ClickException:
    """Translate server failures without silently substituting a non-diff report."""

    message = str(exc.message)
    if "discarded and read-only" in message and "--reopen-review" not in message:
        message = f"{message}; use --reopen-review to restore it"
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and "request body exceeds" in message:
        limit, sent = details.get("limit"), details.get("declared")
        if isinstance(limit, int) and isinstance(sent, int):
            message = f"{message} ({sent:,} bytes sent, limit {limit:,})"
        elif isinstance(limit, int):
            message = f"{message} (limit {limit:,} bytes)"

    if not hosted:
        from lemoncrow_client.errors import ErrorCode

        code = getattr(exc, "code", None)
        timed_out = isinstance(details, dict) and details.get("timed_out") is True
        if code is ErrorCode.SERVER_UNREACHABLE and timed_out:
            message = (
                f"{message}. The server answered too slowly; it may be busy rather than offline. "
                "Check it with `bash ~/.lemoncrow/install/scripts/local_server.sh status` and retry; "
                "restart only if the health/status check is unhealthy"
            )
        elif code in {ErrorCode.SERVER_UNREACHABLE, ErrorCode.SERVER_SESSION_UNAVAILABLE}:
            message = (
                f"{message}. Restart the local server with "
                "`bash ~/.lemoncrow/install/scripts/local_server.sh restart`; if that fails, repair the install "
                "with `make oss-prod` from a checkout or `lc update --force` for a release install"
            )
    return click.ClickException(f"Review server failed: {message}")


def _server_annotation_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize Reader annotation JSON for the terminal renderer."""

    start = int(row.get("start_line") or 0)
    end = int(row.get("end_line") or start)
    path = str(row.get("path") or "")
    location = path if start <= 0 else f"{path}:L{start}" + (f"-L{end}" if end != start else "")
    return {**row, "location": location}


def _attached_terminal_refusal(reference: Any, url: Any) -> str:
    """Why ``--mark``/``--comment``/``--reply-to`` do nothing on a Review attached to hosted (RC5)."""

    where = str(reference or "a hosted Review")
    return (
        f"this Review is attached to hosted {where}, which owns judgments and comments; "
        "terminal --mark, --comment and --reply-to are not queued for sync. "
        + (f"Act on the hosted Review at {url}" if url else "Act on the hosted Review")
        + ", or in the Reader, where your own writes queue for `lc review hosted sync`"
    )


def _server_terminal_state(
    config: Any,
    review_id: str,
    *,
    mark_targets: tuple[str, ...],
    mark_state: str,
    show_units: bool,
    show_marks: bool,
    comment_body: str | None,
    comment_target: str | None,
    comment_kind: str,
    reply_to: str | None,
    show_comments: bool,
    since_my_review: bool,
    send_feedback: bool,
    finish_review: bool,
    reopen_review: bool,
    discard_review: bool,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Apply terminal actions through the same Reader API used by the browser."""

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from urllib.parse import quote

    from lemoncrow.pro.capabilities.review.hosted import server_review_request
    from lemoncrow.pro.capabilities.review.sources.local import parse_line_target

    base = f"/api/reviews/{quote(review_id, safe='')}"

    read_steps = {
        "": ("state_overview", "Loading review overview", "Review overview loaded"),
        "/units": ("state_units", "Loading review targets and marks", "Review targets and marks loaded"),
        "/annotations": ("state_annotations", "Loading review comments", "Review comments loaded"),
    }

    def request(method: str, suffix: str = "", body: dict[str, Any] | None = None) -> dict[str, Any]:
        step = read_steps.get(suffix) if method == "GET" else None
        if step is not None and progress is not None:
            progress(step[0], step[1])
        result = server_review_request(config, method, f"{base}{suffix}", body=body)
        if step is not None and progress is not None:
            progress(f"{step[0]}_done", step[2])
        return result

    def parallel_reads(suffixes: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        # Progress stays on the caller thread: its terminal output and startup
        # session are shared, while every Reader request owns its own client.
        for suffix in suffixes:
            step = read_steps[suffix]
            if progress is not None:
                progress(step[0], step[1])
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=len(suffixes), thread_name_prefix="review-state") as pool:
            futures = {
                pool.submit(server_review_request, config, "GET", f"{base}{suffix}"): suffix for suffix in suffixes
            }
            for future in as_completed(futures):
                suffix = futures[future]
                results[suffix] = future.result()
                if progress is not None:
                    step = read_steps[suffix]
                    progress(f"{step[0]}_done", step[2])
        return results

    has_actions = bool(
        mark_targets or comment_body is not None or send_feedback or finish_review or reopen_review or discard_review
    )
    initial_reads = {} if has_actions else parallel_reads(("", "/units", "/annotations"))

    # Snapshot the reviewer-owned frontier before this invocation mutates any
    # judgments. A verdict discarded by capture belongs to the revision the
    # reviewer arrived at and must still be reported on the very command that
    # advances their frontier past it.
    overview_before = request("GET") if has_actions else initial_reads[""]
    revision_raw = overview_before.get("revision")
    revision_before = revision_raw if isinstance(revision_raw, dict) else {}
    expected_revision_id = str(revision_before.get("id") or "")
    if not expected_revision_id:
        raise click.ClickException("Review server did not return the current Snapshot identity")
    session_raw = overview_before.get("session")
    attached_raw = session_raw.get("hosted") if isinstance(session_raw, dict) else None
    if isinstance(attached_raw, dict) and (mark_targets or comment_body is not None):
        # Hosted owns judgments and comments once attached (RC5). The Reader queues a
        # human's own writes for their offline sync; a terminal (or agent) write never
        # enters that queue.
        raise click.ClickException(_attached_terminal_refusal(attached_raw.get("reference"), attached_raw.get("url")))

    def refuse_queued(response: dict[str, Any]) -> None:
        """A write the server queued for hosted (attached while this ran) is withdrawn, never left queued."""

        if response.get("queued") is not True:
            return
        from lemoncrow_client.errors import ClientError

        raw_actions = response.get("actions")
        stranded: list[str] = []
        for action in raw_actions if isinstance(raw_actions, list) else ():
            action_id = str(action.get("id") or "") if isinstance(action, dict) else ""
            if not action_id:
                continue
            try:
                request("POST", f"/hosted/actions/{quote(action_id, safe='')}", {"op": "discard"})
            except ClientError:
                stranded.append(action_id)
        message = _attached_terminal_refusal(response.get("reference"), None)
        if stranded:
            message += f"; could not withdraw queued action(s) {', '.join(stranded)}: discard them in the Reader"
        else:
            message += "; the write it queued was withdrawn"
        raise click.ClickException(message)

    needs_frontier = bool(
        since_my_review
        or mark_targets
        or comment_body is not None
        or send_feedback
        or finish_review
        or reopen_review
        or discard_review
    )
    frontier_before = request("GET", "/frontier") if needs_frontier else {}

    path_marks: list[dict[str, Any]] = []
    downgraded: list[dict[str, str]] = []
    shadowed: list[dict[str, Any]] = []
    for target in mark_targets:
        marked = request(
            "POST",
            "/marks",
            {"unit_key": target, "state": mark_state, "expected_revision_id": expected_revision_id},
        )
        refuse_queued(marked)
        raw_rows = marked.get("marks")
        rows = [dict(item) for item in raw_rows if isinstance(item, dict)] if isinstance(raw_rows, list) else []
        labels = [str(item.get("label") or item.get("path") or item.get("unit_key") or "") for item in rows]
        widened = len(rows) > 1 or bool(
            rows and str(rows[0].get("unit_key") or "") != target and labels and labels[0] != target
        )
        if widened:
            click.echo(
                f"{target}: recorded on {len(rows)} review target"
                + ("" if len(rows) == 1 else "s")
                + (f" -- {', '.join(labels)}" if labels else ""),
                err=True,
            )
            path_marks.append(
                {
                    "target": target,
                    "covered": [
                        {"unit_key": str(item.get("unit_key") or ""), "label": label}
                        for item, label in zip(rows, labels, strict=True)
                    ],
                }
            )
        for item in rows:
            if item.get("downgraded"):
                note = str(item.get("note") or "requested verdict was downgraded")
                downgraded.append(
                    {
                        "target": target,
                        "requested": mark_state,
                        "recorded": str(item.get("recorded") or ""),
                        "reason": note,
                    }
                )
                click.echo(f"{target}: {note}", err=True)
        raw_shadowed = marked.get("shadowed")
        if isinstance(raw_shadowed, list) and raw_shadowed:
            shadowed.append({"target": target, "shadowed": raw_shadowed})
            click.echo(
                f"{target}: also the label of another review target; the repository file wins. "
                "Use the other target's unit key to mark it instead.",
                err=True,
            )

    created_annotation: dict[str, Any] | None = None
    if comment_body is not None:
        if reply_to:
            current = request("GET", "/annotations")
            raw_annotations = current.get("annotations")
            parent = (
                next(
                    (dict(item) for item in raw_annotations if isinstance(item, dict) and item.get("id") == reply_to),
                    None,
                )
                if isinstance(raw_annotations, list)
                else None
            )
            if parent is None:
                raise click.ClickException(f"no comment {reply_to!r} exists in Review {review_id}")
            path = str(parent.get("path") or "")
            start = int(parent.get("start_line") or 0)
            end = int(parent.get("end_line") or start)
        else:
            if not comment_target:
                raise click.ClickException("--comment needs --on PATH:L10 (or --reply-to an existing comment)")
            try:
                path, start, end = parse_line_target(comment_target)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
        created = request(
            "POST",
            "/annotations",
            {
                "path": path,
                "body": comment_body,
                "kind": comment_kind,
                "parent_id": reply_to or "",
                "file_level": start <= 0,
                "range": {"start_line": start, "end_line": end},
                "expected_revision_id": expected_revision_id,
            },
        )
        refuse_queued(created)
        raw_created = created.get("annotation")
        if isinstance(raw_created, dict):
            created_annotation = _server_annotation_row(dict(raw_created))

    feedback = request("POST", "/feedback/export", {}) if send_feedback else None

    closure: dict[str, Any] | None = None
    if finish_review or reopen_review or discard_review:
        wanted = "open" if reopen_review else ("archived" if discard_review else "finished")
        finish_body: dict[str, Any] = {"status": wanted}
        if wanted != "open":
            finish_body["expected_revision_id"] = expected_revision_id
        closure = request("POST", "/finish", finish_body)

    mutated = bool(mark_targets or comment_body is not None or finish_review or reopen_review or discard_review)
    final_reads = (
        parallel_reads(("", "/units", "/annotations") if mutated else ("/units", "/annotations"))
        if has_actions
        else initial_reads
    )
    overview = final_reads[""] if mutated else overview_before
    units_payload = final_reads["/units"]
    annotations_payload = final_reads["/annotations"]

    raw_units = units_payload.get("units")
    units = [dict(item) for item in raw_units if isinstance(item, dict)] if isinstance(raw_units, list) else []
    raw_annotations = annotations_payload.get("annotations")
    annotations = (
        [_server_annotation_row(dict(item)) for item in raw_annotations if isinstance(item, dict)]
        if isinstance(raw_annotations, list)
        else []
    )

    unit_kinds: dict[str, int] = {}
    mark_counts: dict[str, int] = {}
    marks: list[dict[str, Any]] = []
    stale_marks = 0
    for unit in units:
        kind = str(unit.get("kind") or "")
        unit_kinds[kind] = unit_kinds.get(kind, 0) + 1
        recorded = str(unit.get("state") or "unreviewed")
        if recorded != "unreviewed":
            mark_counts[recorded] = mark_counts.get(recorded, 0) + 1
            content_changed = bool(unit.get("changed_since_mark"))
            stale_marks += int(content_changed)
            marks.append(
                {
                    "state": recorded,
                    "content": "changed" if content_changed else "same",
                    "kind": kind,
                    "path": str(unit.get("path") or ""),
                    "symbol": str(unit.get("symbol") or ""),
                    "label": str(unit.get("label") or unit.get("path") or unit.get("unit_key") or ""),
                    "unit_key": str(unit.get("unit_key") or ""),
                }
            )

    session_value = overview.get("session")
    revision_value = overview.get("revision")
    progress_value = overview.get("progress")
    counts_value = annotations_payload.get("counts")
    discarded_value = frontier_before.get("discarded_verdicts")
    if not isinstance(discarded_value, list) or not discarded_value:
        discarded_value = overview.get("discarded")
    session: dict[str, Any] = dict(session_value) if isinstance(session_value, dict) else {}
    revision: dict[str, Any] = dict(revision_value) if isinstance(revision_value, dict) else {}
    progress_data: dict[str, Any] = dict(progress_value) if isinstance(progress_value, dict) else {}
    counts: dict[str, Any] = dict(counts_value) if isinstance(counts_value, dict) else {}
    discarded = list(discarded_value) if isinstance(discarded_value, list) else []
    state: dict[str, Any] = {
        "review_id": str(session.get("ref") or f"r/{review_id}"),
        "revision_id": str(revision.get("ref") or (f"rr/{revision.get('id')}" if revision.get("id") else "")),
        "revision_number": int(revision.get("revision_number") or 0),
        "range_mode": str(revision.get("range_mode") or session.get("range_mode") or ""),
        "degraded": [
            str(item.get("name") or "") for item in (overview.get("degraded") or []) if isinstance(item, dict)
        ],
        "unit_count": len(units),
        "unit_kinds": unit_kinds,
        "target_count": int(overview.get("target_count") or progress_data.get("target_count") or 0),
        "target_progress": progress_data,
        "mark_counts": mark_counts,
        "stale_marks": stale_marks,
        "downgraded_marks": downgraded,
        "annotation_counts": counts,
        "discarded_verdicts": discarded,
    }
    if path_marks:
        state["path_marks"] = path_marks
    if shadowed:
        state["shadowed_marks"] = shadowed
    if created_annotation is not None:
        state["created_annotation"] = created_annotation
    if show_units:
        state["units"] = units
    if show_marks:
        state["marks"] = marks
    if show_comments:
        state["annotations"] = annotations
    if feedback is not None:
        state["feedback"] = feedback
    if closure is not None:
        state["closure"] = closure
    if since_my_review:
        state["frontier"] = frontier_before
    return state


def _review_reference_matches(identifier: str, reference: str) -> bool:
    """Match an exact internal Review id or its canonical ``r/<id>`` ref."""

    return identifier == reference or f"r/{identifier}" == reference


def _manage_current_review(
    config: Any,
    *,
    store_root: Path,
    repo_root: Path,
    resume_author: bool,
    as_json: bool,
) -> None:
    """Resolve terminal continuity from the same durable live Review binding."""

    from urllib.parse import quote

    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted import server_review_request, server_review_url
    from lemoncrow.pro.capabilities.review.live_runtime import load_live_review_binding

    binding = load_live_review_binding(store_root, repo_root, server_url=config.url)
    if binding is None:
        raise click.ClickException(
            "this workspace has no current Review binding; run `lcr` (or `lc review --open`) once to establish it"
        )

    review_id = binding.review_id
    base = f"/api/reviews/{quote(review_id, safe='')}"
    try:
        overview = server_review_request(config, "GET", base)
        annotations = server_review_request(config, "GET", f"{base}/annotations")
        journal = server_review_request(config, "GET", f"{base}/journal")
        source = server_review_request(config, "GET", f"{base}/source-state")
    except ClientError as exc:
        raise _review_server_click_error(exc, hosted=bool(config.hosted)) from exc

    session_raw = overview.get("session")
    session = session_raw if isinstance(session_raw, dict) else {}
    revision_raw = overview.get("revision")
    revision = revision_raw if isinstance(revision_raw, dict) else {}
    provenance_raw = overview.get("provenance")
    provenance = provenance_raw if isinstance(provenance_raw, dict) else {}
    feedback_raw = annotations.get("feedback")
    feedback = feedback_raw if isinstance(feedback_raw, dict) else {}
    delivery_raw = annotations.get("delivery")
    delivery = delivery_raw if isinstance(delivery_raw, dict) else {}

    exact_provenance = str(provenance.get("certainty") or "") == "exact"
    author_host = str(
        delivery.get("host") or (provenance.get("host") if exact_provenance else "") or binding.author_host
    )
    author_session_id = str(
        delivery.get("session_id")
        or (provenance.get("session_id") if exact_provenance else "")
        or binding.author_session_id
    )
    review_ref = str(session.get("ref") or f"r/{review_id}")
    reader_url = server_review_url(config, review_ref)
    revision_number_raw = revision.get("revision_number")
    current_revision_number = (
        int(revision_number_raw) if revision_number_raw is not None else int(binding.revision_number or 0)
    )
    latest_revision_number_raw = overview.get("latest_revision_number")
    latest_revision_number = (
        int(latest_revision_number_raw) if latest_revision_number_raw is not None else current_revision_number
    )
    reviewed_revision_number = overview.get("last_reviewed_revision_number")
    commented_revision_number = overview.get("last_commented_revision_number")
    viewing_base_revision_number = overview.get("default_base_revision_number")
    viewing_target_revision_number = overview.get("default_target_revision_number")
    status: dict[str, Any] = {
        "review_id": review_id,
        "review_ref": review_ref,
        "reader_url": reader_url,
        "review_status": str(session.get("status") or ""),
        "revision_id": str(revision.get("id") or binding.revision_id),
        "revision_ref": str(revision.get("ref") or f"rr/{revision.get('short_id') or binding.revision_id}"),
        "revision_number": current_revision_number,
        "latest_revision_number": latest_revision_number,
        "last_reviewed_revision_number": reviewed_revision_number,
        "last_commented_revision_number": commented_revision_number,
        "viewing": {
            "base_revision_number": viewing_base_revision_number,
            "target_revision_number": viewing_target_revision_number,
        },
        "author": {
            "host": author_host,
            "session_id": author_session_id,
            "target_ref": f"{author_host}:{author_session_id}" if author_host and author_session_id else "",
            "status": str(journal.get("status") or ""),
            "task": str(journal.get("task") or ""),
            "updated_at": str(journal.get("updated_at") or ""),
        },
        "feedback": {
            "open_total": int(feedback.get("open_total") or 0),
            "unpublished": int(feedback.get("unpublished") or 0),
            "published": int(feedback.get("published") or 0),
            "in_flight": int(feedback.get("in_flight") or 0),
            "addressed": int(feedback.get("addressed") or 0),
        },
        "source": source,
        "binding": {
            "source_fingerprint": binding.source_fingerprint,
            "updated_at": binding.updated_at,
            "claimed_at": binding.claimed_at,
        },
    }

    if resume_author:
        if not author_host or not author_session_id:
            raise click.ClickException(
                f"{review_ref} has no exact author session to resume; open the Review to inspect provenance details"
            )
        from lemoncrow.pro.capabilities.review.delivery import deliver_prompt_to_agent_session

        prompt = (
            f"Continue the existing authoring task in this exact session for LemonCrow Review {review_ref}. "
            "Preserve the current session context, incorporate any Review feedback already delivered to this session, "
            "run the relevant verification, and continue from where you left off. "
            "Do not treat this continuity prompt as new reviewer feedback."
        )
        result = deliver_prompt_to_agent_session(author_host, author_session_id, repo_root, prompt)
        resume_payload = {
            "state": result.state,
            "target_ref": result.target_ref,
            "remote_ref": result.remote_ref,
            "message": result.message,
        }
        status["resume"] = resume_payload
        if not result.accepted and result.state != "sent":
            raise click.ClickException(result.message or f"could not resume {author_host}:{author_session_id}")

    if as_json:
        _emit(status, as_json=True)
        return

    feedback_value = status.get("feedback")
    feedback_state: dict[str, Any] = feedback_value if isinstance(feedback_value, dict) else {}
    author_value = status.get("author")
    author: dict[str, Any] = author_value if isinstance(author_value, dict) else {}
    source_value = status.get("source")
    source_state: dict[str, Any] = source_value if isinstance(source_value, dict) else {}
    resume_value = status.get("resume")
    resume_state: dict[str, Any] = resume_value if isinstance(resume_value, dict) else {}
    viewing_value = status.get("viewing")
    viewing = viewing_value if isinstance(viewing_value, dict) else {}
    viewing_base = viewing.get("base_revision_number")
    viewing_target = viewing.get("target_revision_number")
    viewing_text = (
        f"Revision {viewing_base} → Revision {viewing_target}"
        if viewing_base is not None and viewing_target is not None and viewing_base != viewing_target
        else f"Revision {viewing_target}" if viewing_target is not None else f"Revision {status['revision_number']}"
    )
    reviewed_text = (
        f"Revision {status['last_reviewed_revision_number']}"
        if status.get("last_reviewed_revision_number") is not None
        else "—"
    )
    commented_text = (
        f"Revision {status['last_commented_revision_number']}"
        if status.get("last_commented_revision_number") is not None
        else "—"
    )
    click.echo(
        f"Review    {review_ref} · {status['review_status'] or 'open'}\n"
        f"Viewing   {viewing_text}\n"
        f"Latest    Revision {status['latest_revision_number']}\n"
        f"Reviewed  {reviewed_text}\n"
        f"Commented {commented_text}\n"
        f"Reader    {reader_url}\n"
        + (
            f"Author    {str(author['host']).capitalize()} · {author['session_id']}"
            + (f" · {author['status']}" if author.get("status") else "")
            + "\n"
            if author.get("host") and author.get("session_id")
            else "Author    no exact session bound\n"
        )
        + (f"Task      {author['task']}\n" if author.get("task") else "")
        + (
            "Feedback  "
            f"{feedback_state['unpublished']} unsent · {feedback_state['in_flight']} in flight · "
            f"{feedback_state['published']} sent · {feedback_state['addressed']} addressed\n"
        )
        + (
            f"Source    {source_state.get('path_count', 0)} newer file(s) · {source_state.get('fingerprint', '')}\n"
            if source_state.get("supported") and source_state.get("changed")
            else (
                "Source    matches frozen Review Snapshot\n"
                if source_state.get("supported")
                else f"Source    unavailable · {source_state.get('reason') or 'source state is not available'}\n"
            )
        )
        + (f"Resume    {resume_state.get('state', '')} · {resume_state.get('message', '')}" if resume_state else "")
    )


def _manage_server_reviews(
    config: Any,
    *,
    list_reviews: bool,
    show_review: str | None,
    open_review: str | None,
    status_filter: str,
    as_json: bool,
) -> None:
    """List/show/open Reviews through the configured LemonCrow server."""
    if config.hosted and not config.authenticated:
        raise click.ClickException(
            "Hosted Review requires sign-in; authenticate to the configured LemonCrow server first"
        )
    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted import server_review_detail, server_review_rows, server_review_url

    try:
        all_rows = server_review_rows(config)
        rows = all_rows if status_filter == "all" else [row for row in all_rows if row.get("status") == status_filter]
        wanted_id = show_review or open_review or ""
        if wanted_id:
            matches = [
                item
                for item in all_rows
                if item.get("ref") == wanted_id or _review_reference_matches(str(item.get("id") or ""), wanted_id)
            ]
            if not matches:
                raise click.ClickException(f"no such Review {wanted_id!r}")
            if len(matches) > 1:
                raise click.ClickException(f"ambiguous Review reference {wanted_id!r} across repositories")
            row = matches[0]
            canonical_id = str(row.get("id") or wanted_id)
            review_ref = str(row.get("ref") or f"r/{canonical_id}")
            repo_id = row.get("repo_id")
            if not isinstance(repo_id, str) or not repo_id:
                raise click.ClickException(f"Review {wanted_id!r} has no repository scope")
            if open_review:
                review_path = row.get("review_path")
                url = (
                    config.endpoint(review_path)
                    if isinstance(review_path, str) and review_path.startswith("/r/")
                    else server_review_url(config, review_ref)
                )
                click.echo(f"Review  {url}")
                _open_server_review(
                    url,
                    config=config,
                    review_id=review_ref,
                    open_browser=True,
                )
                return
            detail = server_review_detail(config, repo_id, canonical_id)
            if as_json:
                _emit(detail, as_json=True)
            else:
                review_value = detail.get("review")
                revision_value = detail.get("revision")
                review = review_value if isinstance(review_value, dict) else {}
                revision = revision_value if isinstance(revision_value, dict) else {}
                click.echo(
                    f"{review.get('ref') or f'r/{review.get("id", wanted_id)}'}  {review.get('status', '')}  "
                    f"Revision {revision.get('revision_number', 0)}\n"
                    f"{review.get('title') or '(untitled review)'}\n"
                    f"Repository  {repo_id}\n"
                    f"Revisions   {detail.get('revision_count', 0)}\n"
                    f"Updated     {review.get('updated_at', '')}"
                )
            return

        if not list_reviews:
            return
        if as_json:
            _emit({"reviews": rows, "status_filter": status_filter}, as_json=True)
            return
        if not rows:
            click.echo("No reviews found.")
            return
        click.echo("REVIEW ID             STATUS  REVISION  TITLE")
        for row in rows:
            title = str(row.get("title") or "(untitled)").replace("\n", " ")
            shown_id = str(row.get("ref") or (f"r/{row.get('id')}" if row.get("id") else ""))
            click.echo(
                f"{shown_id:<21} {row.get('status') or ''!s:<10} {int(row.get('revision_number') or 0):>8}  {title}"
            )
    except ClientError as exc:
        raise click.ClickException(f"Review server failed: {exc.message}") from exc


def _parse_fix_snapshot_ref(value: str) -> tuple[str, int]:
    raw = value.strip()
    try:
        review_ref, number_raw = raw.rsplit(".", 1)
        number = int(number_raw)
    except (ValueError, TypeError) as exc:
        raise click.ClickException("fix target must look like REVIEW.N, for example k7q2.3") from exc
    review_ref = review_ref.removeprefix("r/").strip()
    if not review_ref or number < 0:
        raise click.ClickException("fix target must look like REVIEW.N, for example k7q2.3")
    return review_ref, number


def _manage_fix_line(
    config: Any,
    *,
    fix_snapshot: str | None,
    apply_fix_snapshot: str | None,
    with_conflict_markers: bool,
    as_json: bool,
) -> None:
    """Start or apply one local Snapshot fix line through the Review service."""

    if config.hosted:
        raise click.ClickException("Fix lines are available for local Reviews only")

    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted import server_review_request

    wanted = fix_snapshot or apply_fix_snapshot or ""
    review_ref, snapshot_number = _parse_fix_snapshot_ref(wanted)
    try:
        overview = server_review_request(config, "GET", f"/api/reviews/{review_ref}")
        raw_session = overview.get("session") if isinstance(overview, dict) else None
        session = raw_session if isinstance(raw_session, dict) else {}
        review_id = str(session.get("id") or "").strip()
        if not review_id:
            raise click.ClickException(f"no such Review {review_ref!r}")

        if fix_snapshot:
            history = server_review_request(config, "GET", f"/api/reviews/{review_id}/revisions")
            revisions = history.get("revisions") if isinstance(history, dict) else None
            rows = [row for row in revisions or [] if isinstance(row, dict)]
            matches = [row for row in rows if int(row.get("revision_number") or -1) == snapshot_number]
            if len(matches) != 1:
                raise click.ClickException(f"Review {review_ref!r} has no unique Snapshot {snapshot_number}")
            revision_id = str(matches[0].get("id") or "")
            result = server_review_request(
                config,
                "POST",
                f"/api/reviews/{review_id}/snapshots/{revision_id}/fix-line",
                body={},
            )
            if as_json:
                _emit(result, as_json=True)
                return
            line = result.get("fix_line") if isinstance(result, dict) else None
            wire = line if isinstance(line, dict) else {}
            path = str(wire.get("worktree_path") or "")
            branch = str(wire.get("branch") or "")
            click.echo(path)
            if branch:
                click.echo(f"Branch  {branch}", err=True)
            return

        listed = server_review_request(config, "GET", f"/api/reviews/{review_id}/fix-lines")
        raw_lines = listed.get("fix_lines") if isinstance(listed, dict) else None
        lines = [row for row in raw_lines or [] if isinstance(row, dict)]
        matching = [row for row in lines if int(row.get("snapshot_number") or -1) == snapshot_number]
        open_lines = [row for row in matching if str(row.get("status") or "") in {"fixing", "applied_with_conflicts"}]
        line = (open_lines or matching)[-1] if (open_lines or matching) else None
        if line is None:
            raise click.ClickException(
                f"Snapshot {snapshot_number} has no fix line; run lc review --fix {wanted} first"
            )
        fix_id = str(line.get("id") or "")
        result = server_review_request(
            config,
            "POST",
            f"/api/reviews/{review_id}/fix-lines/{fix_id}/apply",
            body={"with_conflict_markers": bool(with_conflict_markers)},
        )
        if as_json:
            _emit(result, as_json=True)
            if not result.get("applied"):
                raise click.exceptions.Exit(1)
            return
        plan = result.get("plan") if isinstance(result, dict) else None
        plan_wire = plan if isinstance(plan, dict) else {}
        for warning in plan_wire.get("warnings") or []:
            click.echo(f"Warning: {warning}", err=True)
        if not result.get("applied"):
            conflicts = [str(path) for path in plan_wire.get("conflicts") or []]
            if conflicts:
                click.echo("Fix conflicts with the main worktree:", err=True)
                for path in conflicts:
                    click.echo(f"  {path}", err=True)
            raise click.ClickException("nothing was written; keep fixing or rerun with --with-conflict-markers")
        fix_wire = result.get("fix_line") if isinstance(result, dict) else None
        applied = fix_wire.get("applied_files") if isinstance(fix_wire, dict) else []
        files = [str(path) for path in applied or []]
        click.echo(f"Applied fix to main as uncommitted changes ({len(files)} file{'s' if len(files) != 1 else ''}).")
        for path in files:
            click.echo(f"  {path}")
    except ClientError as exc:
        raise click.ClickException(f"Review server failed: {exc.message}") from exc


def _open_server_review(url: str, *, config: Any, review_id: str, open_browser: bool) -> None:
    """Open a credential-free Reader URL with local browser pairing.

    Hosted Review relies on its normal browser identity shell. Local Review
    first uses the machine bearer over HTTP to arm the exact clean URL, then the
    browser claims a separate Review-only capability. No machine or browser
    credential is ever placed in the URL.
    """
    del review_id
    if not open_browser:
        return

    if not config.hosted and config.token:
        from urllib.parse import urlsplit

        from lemoncrow_client.errors import ClientError

        from lemoncrow.pro.capabilities.review.hosted import pair_local_review_browser

        path = urlsplit(url).path or "/reviews"
        try:
            pair_local_review_browser(config, path)
        except ClientError as exc:
            raise _review_server_click_error(exc, hosted=False) from exc

    opened = _launch_browser_url(url)
    if not opened:
        click.echo(f"(could not open a browser; open {url} manually.)", err=True)


def arm_local_browser_url(url: str) -> None:
    """Best-effort CLI pairing before opening a local console *url*.

    The local server mints a browser capability only after the machine bearer
    arms the exact path (or the browser renews a still-valid capability). A
    failure here is not fatal: the page then says how to reconnect.
    """
    from urllib.parse import urlsplit

    try:
        from lemoncrow_client.config import load_config as load_client_config
        from lemoncrow_client.errors import ClientError

        from lemoncrow.pro.capabilities.review.hosted import pair_local_review_browser
    except ImportError:
        return
    try:
        config = load_client_config(product="review")
        target = urlsplit(url)
        scheme, host, port = config.endpoint_origin
        target_port = target.port or (443 if target.scheme == "https" else 80)
        same_origin = (target.scheme, target.hostname, target_port) == (scheme, host, port)
        if config.hosted or not config.token or not same_origin:
            return
        pair_local_review_browser(config, target.path or "/")
    except (ClientError, OSError, ValueError):
        return


def _launch_browser_url(url: str) -> bool:
    """Launch *url* in the graphical desktop browser.

    Do not use Python's controller return value as proof that a desktop browser
    accepted the URL: Chrome and desktop opener helpers may exit immediately
    after handing the URL to an already-running browser.  Successful process
    creation is the useful contract here.  On Linux we also avoid Python's
    browser fallback chain entirely so a failed GUI launch can never fall back
    to links/lynx/w3m and hijack the terminal.
    """

    import os
    import shutil
    import subprocess
    import sys
    import webbrowser

    if os.environ.get("LEMONCROW_NO_BROWSER", "").strip() not in {"", "0"}:
        # Tests/CI: print the URL instead of launching a desktop browser.
        click.echo(f"Review URL: {url}")
        return True

    def spawn(argv: list[str]) -> bool:
        try:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    if sys.platform == "darwin":
        opener = shutil.which("open")
        return bool(opener and spawn([opener, url]))

    if os.name == "nt":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False

        # Let the desktop choose its configured graphical browser.  We count a
        # successful spawn, not the helper's eventual exit status: xdg-open may
        # hand the URL to an existing Chrome process and exit immediately.
        opener = shutil.which("xdg-open")
        if opener and spawn([opener, url]):
            return True

        # Minimal GUI-only fallback when xdg-open is unavailable/broken.
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "firefox",
            "brave-browser",
            "brave-browser-stable",
            "microsoft-edge",
            "vivaldi",
        ):
            executable = shutil.which(name)
            if executable and spawn([executable, url]):
                return True
        return False

    # Non-Linux Unix fallback.  Linux intentionally avoids this because
    # webbrowser.open() may continue into terminal-only browsers.
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _review_state(
    repo_root: Path,
    rng: RevRange,
    build: PacketBuild,
    *,
    store_root: Path,
    session_id: str | None,
    limit: int,
    mark_targets: tuple[str, ...],
    mark_state: str,
    show_units: bool,
    show_marks: bool,
    show_comments: bool = False,
    comment_body: str | None = None,
    comment_target: str | None = None,
    comment_kind: str = "comment",
    reply_to: str | None = None,
    since_my_review: bool = False,
    send_feedback: bool = False,
    finish_review: bool = False,
    reopen_review: bool = False,
    discard_review: bool = False,
    project_annotations: bool = True,
) -> dict[str, Any]:
    """Persist this review and apply any marks; return what to print.

    Every step is idempotent: the session is reopened rather than re-created, and
    an unchanged tree fingerprints to the revision that is already stored, so
    running this twice costs one extra fingerprint and moves nothing.

    ``--since-my-review`` does not change what is *recorded*; it only asks for
    the frontier to be printed. Recording a revision and reconciling the marks
    and comment anchors onto it is one operation for every flag, which is why
    there is one call here and no branch. The branch is what shipped the defect:
    a plain run persisted a revision without reconciling, and the next run --
    finding the latest revision already equal to the one it was about to take --
    concluded nothing had happened and left a stale ``reviewed`` and a comment
    labelled "file unchanged since the comment" standing over rewritten code,
    permanently. Marks are applied *after* the reconciliation, so ``--mark``
    records a verdict on the revision the reviewer is being shown rather than on
    the one it replaced.
    """

    from lemoncrow.pro.capabilities.review.session_models import ReviewSessionStatus
    from lemoncrow.pro.capabilities.review.sources.local import (
        annotation_counts,
        coerce_mark_state,
        mark_downgrade_note,
        mark_unit,
        open_or_create_session,
        refresh,
        resolve_mark_units,
        set_review_status,
    )
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    store = ReviewStore(store_root)
    try:
        session = open_or_create_session(
            store,
            repo_root,
            rng,
            title=build.packet.title or rng.title,
            restore_archived=reopen_review,
            reuse_closed=finish_review or reopen_review or discard_review,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    refreshed = refresh(
        store,
        session,
        repo_root,
        store_root=store_root,
        rng=rng,
        session_id=session_id,
        limit=limit,
        build=build,
        project_annotations=project_annotations,
    )
    revision = refreshed.revision
    store.publish_snapshot(session.id, revision.id)
    units = store.list_units(revision.id)
    by_key = {unit.unit_key: unit for unit in units}

    try:
        state = coerce_mark_state(mark_state)
    except ValueError as exc:  # pragma: no cover - click.Choice already refuses these
        raise click.ClickException(str(exc)) from exc

    downgraded: list[dict[str, str]] = []
    path_marks: list[dict[str, Any]] = []
    shadowed_marks: list[dict[str, Any]] = []
    # The reader's target list is the most expensive thing this command derives
    # -- annotations, marks, frontier and the whole projection -- and the tally
    # below has to derive it again *after* the verdicts land, because a verdict
    # recorded in between is exactly what the tally has to show. So it is asked
    # for here only when a spelling actually needs widening: a precise
    # `hun:`/`sym:` key names its own judgment and resolves against no target
    # list at all, and an invocation that marks by unit key now derives once
    # instead of twice. When it is needed it is still derived before the first
    # verdict lands, so a `--mark PATH` expands onto the targets the reviewer was
    # just shown rather than onto whatever this invocation's own verdicts turn
    # those targets into.
    needs_scope = any(target not in by_key or by_key[target].kind == "file" for target in mark_targets)
    mark_scope = _current_targets(store, session, revision, units, build) if needs_scope else ()
    label_by_key = {item.unit_key: item.label for item in mark_scope}
    for target in mark_targets:
        resolution = resolve_mark_units(units, mark_scope, target)
        covered = resolution.units
        if not covered:
            # Never a silent no-op: a reviewer who typed a stale path would
            # otherwise believe they had recorded something.
            raise click.ClickException(f"no review area matches {target!r} in Revision {revision.revision_number - 1}")
        if resolution.shadowed:
            # One word, two readings: a file the repository really contains, and
            # some other file's target label spelled identically. The file wins
            # -- the reviewer named something that exists -- but the reading that
            # lost has to be said out loud, or the verdict quietly answers the
            # wrong one of the two questions. Stderr, like every other
            # disclosure here, so it survives `--json`.
            others = ", ".join(f"{unit.unit_key} in {unit.path}" for unit in resolution.shadowed)
            click.echo(
                f"{target}: matched the file in the repository; it is also the label of another "
                f"review target -- mark that one by its unit key instead: {others}",
                err=True,
            )
            shadowed_marks.append(
                {
                    "target": target,
                    "shadowed": [{"unit_key": unit.unit_key, "path": unit.path} for unit in resolution.shadowed],
                }
            )
        labels = [label_by_key.get(unit.unit_key, unit.path) for unit in covered]
        if len(covered) > 1 or (labels[0] != target and covered[0].unit_key != target):
            # The verdict landed somewhere other than the one thing the reviewer
            # typed, which only the imprecise spellings -- a path, a `fil:` key --
            # can do. Reader spec §29.3: a bare path must not silently stand in
            # for a judgment on the whole file, so name the targets it landed on
            # -- on stderr, so it survives `--json` like the downgrade note below,
            # and using each target's own label so the terminal says what the
            # browser and the feedback bundle say. A spelling that resolved to
            # exactly the target it named -- a unit key, a target label, or a path
            # whose only target is the file itself -- widened nothing, and
            # announcing the word the reviewer just typed back at them is noise.
            counted = f"{len(covered)} review target" + ("" if len(covered) == 1 else "s")
            click.echo(f"{target}: recorded on {counted} -- {', '.join(labels)}", err=True)
            path_marks.append(
                {
                    "target": target,
                    "covered": [
                        {"unit_key": unit.unit_key, "label": label} for unit, label in zip(covered, labels, strict=True)
                    ],
                }
            )
        for unit in covered:
            recorded = mark_unit(
                store,
                session,
                revision,
                unit,
                state=state,
                expected_current_revision_id=revision.id,
            )
            note = mark_downgrade_note(unit, state, recorded.state)
            if note:
                # Said here, at the moment it happens, and on stderr so it survives
                # `--json`. A downgrade that only shows up in an aggregate count is
                # a downgrade the reviewer never learns about: they asked to record
                # `reviewed` and something weaker went into the store.
                click.echo(f"{target}: {note}", err=True)
                downgraded.append(
                    {
                        "target": target,
                        "unit_key": unit.unit_key,
                        "requested": state,
                        "recorded": recorded.state,
                        "reason": note,
                    }
                )

    # Comments are written after the marks and against the same revision the
    # reviewer was just shown, so a `--mark ... --comment ...` in one invocation
    # records both against one content fingerprint rather than straddling two.
    created_annotation = None
    if comment_body is not None:
        created_annotation = _add_comment(
            store,
            session,
            revision,
            build,
            body=comment_body,
            target=comment_target,
            kind=comment_kind,
            reply_to=reply_to,
        )

    kinds: dict[str, int] = {}
    for unit in units:
        kinds[unit.kind] = kinds.get(unit.kind, 0) + 1

    marks = store.list_marks(session.id)
    # A mark whose content has moved is still stored as `reviewed` -- promoting it
    # to `changed_since_review` is revision reconciliation's decision, not this
    # command's. But a bare "3 reviewed" over three rewritten units tells the
    # reader something false, so the count of moved marks rides alongside it.
    stale = sum(1 for mark in marks if _content_status(mark.content_fingerprint, by_key.get(mark.unit_key)) != "same")
    annotations = store.list_annotations(session.id)
    from lemoncrow.pro.capabilities.review.targets import progress_payload, review_progress

    targets = _current_targets(store, session, revision, units, build)
    target_progress = review_progress(targets)
    payload: dict[str, Any] = {
        "review_id": session.id,
        "revision_id": revision.id,
        "revision_number": revision.revision_number - 1,
        "range_mode": revision.range_mode,
        # No `tree_fingerprint` and no `packet_path`: both were shipped here
        # from the first day, rendered by nothing and read by nobody. The
        # revision is identified by its id and its number, and under `--json`
        # the packet those two fields pointed at is already on stdout.
        "degraded": list(revision.degraded),
        "unit_count": len(units),
        "unit_kinds": kinds,
        "target_count": target_progress.target_count,
        "target_progress": progress_payload(target_progress),
        "mark_counts": _mark_counts(marks),
        "stale_marks": stale,
        "downgraded_marks": downgraded,
    }
    if path_marks:
        # One row per `--mark` that was widened -- the same condition as the
        # stderr disclosure above, so the two surfaces cannot disagree about
        # whether anything was widened. A `--mark` that resolved to exactly the
        # target it named adds no row, and an empty list every run would be one
        # more key a consumer has to read to learn nothing.
        payload["path_marks"] = path_marks
    if shadowed_marks:
        # Same rule for the collision disclosure: present only when a real path
        # outranked a target label spelled the same way, so `--json` and stderr
        # tell one story about which reading was taken.
        payload["shadowed_marks"] = shadowed_marks

    payload["annotation_counts"] = annotation_counts(annotations)
    if created_annotation is not None:
        payload["created_annotation"] = _annotation_row(created_annotation)
    if show_comments:
        payload["annotations"] = [_annotation_row(item) for item in annotations]
    ambiguous = _ambiguous_symbols(units)
    if show_units:
        payload["units"] = [
            {
                "unit_key": unit.unit_key,
                "kind": unit.kind,
                "path": unit.path,
                "symbol": unit.symbol,
                "ordinal": unit.ordinal,
                "label": _unit_label(
                    unit.kind,
                    unit.path,
                    unit.symbol,
                    unit.ordinal,
                    start_line=unit.start_line,
                    ambiguous=(unit.path, unit.symbol) in ambiguous,
                ),
                "attention_rank": unit.attention_rank,
                "fingerprint_method": unit.fingerprint_method,
            }
            for unit in units
        ]
    if show_marks:
        payload["marks"] = [_mark_row(mark, by_key.get(mark.unit_key), ambiguous) for mark in marks]
    # Unconditional, and deliberately *not* inside the `--since-my-review`
    # block: the reviewer has to be told whichever command they typed. Read out
    # of the store against this revision rather than off this invocation's
    # reconciliation, so it is told on every run and not only on whichever one
    # happened to reconcile first.
    discarded = _discarded_verdicts(store, session, revision, refreshed)
    if discarded:
        payload["discarded_verdicts"] = discarded
    if since_my_review:
        payload["frontier"] = _frontier_payload(store, session, revision, refreshed, units, marks, discarded)
    if send_feedback:
        payload["feedback"] = _feedback_payload(store, session, revision, units, annotations)
    if finish_review or reopen_review or discard_review:
        # Last, and after the marks and comments this invocation recorded: the
        # tally has to describe the review the human is actually closing, not
        # the one they opened the command on.
        wanted_status: ReviewSessionStatus = "finished"
        if discard_review:
            wanted_status = "archived"
        elif reopen_review:
            wanted_status = "open"
        closure = set_review_status(
            store,
            session,
            revision,
            status=wanted_status,
        )
        payload["closure"] = {
            "status": closure.status,
            "target_count": closure.target_count,
            "reviewed_targets": closure.reviewed_targets,
            "unreviewed_targets": closure.unreviewed_targets,
            "changed_since_review": closure.changed_since_review,
            "needs_changes": closure.needs_changes,
            "unknown_targets": closure.unknown_targets,
            "open_comments": closure.open_comments,
            "orphaned_comments": closure.orphaned_comments,
        }
    return payload


def _current_targets(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    units: Sequence[ReviewUnit],
    build: PacketBuild,
) -> tuple[ReviewTarget, ...]:
    """The reader's target list for *revision*, as the store has it right now.

    Asked twice in a run that widens a ``--mark`` -- once to expand the path
    onto the targets it covers, and once afterwards for the tally that gets
    printed -- because a verdict recorded in between is exactly what the second
    answer has to show. A run whose ``--mark`` spellings are all precise unit
    keys expands nothing and asks only once; see the caller.
    """

    from lemoncrow.pro.capabilities.review.revisions import compute_frontier
    from lemoncrow.pro.capabilities.review.targets import derive_review_targets

    annotations = store.list_annotations(session.id)
    frontier = compute_frontier(
        session.id,
        session.reviewer_id,
        revision,
        units,
        store.list_marks(session.id),
        annotations=annotations,
    )
    return derive_review_targets(units, frontier.entries, build.packet.to_dict(), annotations=annotations)


def _feedback_payload(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    units: Sequence[ReviewUnit],
    annotations: Sequence[Annotation],
) -> dict[str, Any]:
    """``--feedback``: the review's comments as one Markdown document.

    The same :mod:`...review.feedback` bundle the workspace's *Send feedback*
    button renders, through the same function, so the two surfaces cannot drift
    into two different documents. It renders; it delivers nothing, and the
    caller prints that fact rather than leaving the reader to assume an agent
    was told.
    """

    from lemoncrow.pro.capabilities.review.feedback import build_bundle, packet_context, render_markdown
    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    packet = read_packet_json(store, revision)
    bundle = build_bundle(
        session,
        revision,
        annotations,
        units,
        context=packet_context(packet),
        title=(packet.get("title") if packet is not None else "") or session.title,
    )
    return {
        "markdown": render_markdown(bundle),
        "open": len(bundle.items),
        "orphaned": len(bundle.orphaned),
        "resolved": len(bundle.resolved),
    }


def _add_comment(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    build: PacketBuild,
    *,
    body: str,
    target: str | None,
    kind: str,
    reply_to: str | None,
) -> Annotation:
    """Record one ``--comment``, anchored to the blobs this packet was built from.

    The text handed to the anchor is ``build.blobs.new`` -- the exact bytes the
    packet on screen was computed from -- and never a fresh read of the working
    tree. A file saved between the two reads would otherwise be anchored against
    content the reviewer was never shown, and the comment would appear to have
    moved on the very next revision.

    A reply inherits its parent's position: a thread whose replies could drift to
    different lines is not a thread.
    """

    from lemoncrow.pro.capabilities.review.sources.local import annotate, parse_line_target

    parent_id = reply_to or ""
    if target:
        try:
            path, start, end = parse_line_target(target)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        parent = store.get_annotation(parent_id)
        if parent is None or parent.review_id != session.id:
            raise click.ClickException(f"no comment {parent_id!r} in this review to reply to")
        path, start, end = parent.anchor.path, parent.anchor.start_line, parent.anchor.end_line

    try:
        return annotate(
            store,
            session,
            revision,
            path=path,
            start_line=start,
            end_line=end,
            body=body,
            kind=kind,
            parent_id=parent_id,
            new_text=build.blobs.new.get(path),
            expected_current_revision_id=revision.id,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _annotation_row(annotation: Annotation) -> dict[str, Any]:
    """One comment, named the way the terminal and the workspace both name it.

    ``anchored`` is computed rather than inferred from the state alone, because
    the two can disagree honestly: a comment that was never anchorable is
    ``open`` and unanchored, and a reader who saw only the state would take its
    line number for a claim.

    ``symbol`` is the definition the comment sits in **now** and is empty when
    no rung located it; the name it was written against moves to
    ``origin_symbol``, which every surface renders as "originally in".
    """

    from lemoncrow.pro.capabilities.review.session_models import ANCHOR_METHOD_LABELS, anchor_symbol_claim

    anchor = annotation.anchor
    location = f"{anchor.path}:L{anchor.start_line}"
    if anchor.end_line > anchor.start_line:
        location = f"{location}-L{anchor.end_line}"
    symbol, origin_symbol = anchor_symbol_claim(annotation.anchor_method, anchor.symbol_qualified_name)
    return {
        "id": annotation.id,
        "kind": annotation.kind,
        "state": annotation.state,
        "path": anchor.path,
        "start_line": anchor.start_line,
        "end_line": anchor.end_line,
        "location": location,
        "symbol": symbol,
        "origin_symbol": origin_symbol,
        "unit_key": anchor.unit_key,
        "parent_id": annotation.parent_id,
        "body": annotation.body,
        "source": annotation.source,
        "source_id": annotation.source_id,
        "title": annotation.title,
        "anchor_method": annotation.anchor_method,
        "anchor_method_label": ANCHOR_METHOD_LABELS.get(annotation.anchor_method, annotation.anchor_method),
        "anchor_detail": annotation.anchor_detail,
        "anchored": annotation.state not in ("orphaned", "obsolete")
        and annotation.anchor_method not in ("orphaned", "removed", "unresolved"),
    }


def _discarded_verdicts(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    refreshed: RefreshResult,
) -> list[dict[str, Any]]:
    """The reviewer's verdicts that were destroyed getting to *revision*, named so they can be read.

    ``Reconciliation.dropped`` was computed and shipped in the JSON from the
    first day and printed nowhere, which made "your verdict was thrown away" the
    one thing this tool did silently. A reviewer who approved a symbol an agent
    then renamed had their approval deleted -- correctly, there is nothing left
    to approve -- and learned about it never.

    Read out of ``review_discarded_marks`` rather than off this invocation's
    :attr:`RefreshResult.discarded`, because a destroyed verdict is a property
    of the *revision*, not of the process that noticed it. Reconciliation runs
    once per revision, so the in-process list is non-empty only for whichever
    command reconciled first: type ``--marks`` before ``--since-my-review`` and
    the banner was printed on no run at all, while the row proving the loss sat
    on disk with the words that belong on screen. It is the one thing here that
    looking again cannot rediscover, so looking again is exactly what has to
    show it.

    The store already holds the right rows: the stale old-key row of every mark
    a rename re-keyed is filtered out before it is written (that verdict was
    carried, not thrown away), the unit's descriptive fields were copied in
    while it still existed so the label survives its unit, and the insert is
    idempotent on ``(review, reviewer, unit, content, revision)`` so run three
    reads exactly what run one did.

    Scoped to the window the frontier block beside it is scoped to -- from the
    revision this reviewer last saw to the one in front of them -- and not to
    the single revision reconciliation happened to be standing on when it
    deleted the mark. Those are different revisions from the moment the agent
    edits again, and asking for the narrow one is what made a destroyed approval
    survive exactly one command: at revision 3 the frontier still named the unit
    as gone while this list came back empty, so ``LEFT THE REVIEW`` printed
    "nothing here to re-read" over an approval this tool had deleted. See
    :meth:`ReviewStore.discarded_marks_since` for when it stops being news.
    """

    baseline = refreshed.previous_revision
    return [
        {
            "unit_key": record.unit_key,
            "kind": record.kind,
            "state": record.state,
            "label": _unit_label(
                record.kind,
                record.path,
                record.symbol,
                record.ordinal,
                start_line=record.start_line,
            )
            or record.unit_key,
            "reason": record.reason,
        }
        for record in store.discarded_marks_since(
            session.id,
            reviewer_id=session.reviewer_id,
            after_revision_number=baseline.revision_number if baseline is not None else 0,
            absent_from_revision_id=revision.id,
        )
    ]


def _removed_units(
    store: ReviewStore,
    refreshed: RefreshResult,
    discarded: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The units that left the review, named rather than counted.

    ``Reconciliation.removed`` shipped as a bare integer and was rendered by no
    surface at all, which is the worst of both: a consumer could see that four
    things had gone and no consumer could see *which*. A file an agent deleted
    and a symbol it inlined are the two events a reviewer most wants named,
    because neither leaves anything behind to click on.

    Named out of the revision the reviewer last saw -- the last revision that
    still contained them. The bare ``unit_key`` is the fallback, never a guess.

    ``discarded_state`` carries the verdict that left with the unit, when one
    did. "Nothing here to re-read" is true of a unit nobody had judged and false
    of one a human approved, and printing the second sentence under the first is
    how a destroyed approval reads as housekeeping.
    """

    removed = refreshed.reconciliation.removed
    if not removed:
        return []
    previous = refreshed.previous_revision
    known = {unit.unit_key: unit for unit in (store.list_units(previous.id) if previous is not None else ())}
    verdicts = {str(item["unit_key"]): str(item["state"]) for item in discarded}
    rows: list[dict[str, Any]] = []
    for key in removed:
        unit = known.get(key)
        rows.append(
            {
                "unit_key": key,
                "kind": unit.kind if unit is not None else "",
                "label": (
                    _unit_label(unit.kind, unit.path, unit.symbol, unit.ordinal, start_line=unit.start_line)
                    if unit is not None
                    else ""
                )
                or key,
                "discarded_state": verdicts.get(key, ""),
            }
        )
    return sorted(rows, key=lambda row: (row["label"], row["unit_key"]))


def _frontier_payload(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    refreshed: RefreshResult,
    units: Sequence[ReviewUnit],
    marks: Sequence[ReviewMark],
    discarded: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """The "what changed since I reviewed" block, recomputed after any --mark.

    The frontier inside *refreshed* was computed before this invocation's marks
    were applied, and printing that one would show a reviewer their own
    just-recorded verdict missing. It is derived, never stored, precisely so it
    can be recomputed here for nothing.
    """

    from lemoncrow.pro.capabilities.review.revisions import compute_frontier, group_frontier
    from lemoncrow.pro.capabilities.review.session_models import ANCHOR_METHOD_LABELS
    from lemoncrow.pro.capabilities.review.sources.local import unseen_units

    frontier = compute_frontier(
        session.id,
        "local",
        revision,
        units,
        marks,
        annotations=store.list_annotations(session.id),
    )
    # `new` is "content you have not looked at", never "a key the revision you
    # last reviewed happened not to hold". The two agree until the tree goes
    # backwards, and then the second one hands a reviewer their own approved
    # work back as though it had just been written.
    groups = group_frontier(frontier, unseen_units(store, session.id, units, refreshed.reconciliation.added))
    previous = refreshed.previous_revision
    ambiguous = _ambiguous_symbols(units)
    return {
        "previous_revision_number": previous.revision_number - 1 if previous is not None else None,
        "changed_since_review": [_frontier_row(entry, ambiguous) for entry in groups.changed_since_review],
        "new": [_frontier_row(entry, ambiguous) for entry in groups.new],
        "unresolved": [_frontier_row(entry, ambiguous) for entry in groups.unresolved],
        "unchanged_reviewed": [_frontier_row(entry, ambiguous) for entry in groups.unchanged_reviewed],
        "not_yet_reviewed": len(groups.not_yet_reviewed),
        "removed_units": _removed_units(store, refreshed, discarded),
        # Repeated from the block above so `--since-my-review --json` is a
        # complete answer to "where does my review stand" on its own: a consumer
        # reading only the frontier must not have to know that the one
        # irrecoverable loss is reported somewhere else in the document.
        "discarded_verdicts": list(discarded),
        "notes": list(refreshed.reconciliation.notes),
        "open_annotations": len(frontier.unresolved_annotation_ids),
        "orphaned_annotations": len(frontier.orphaned_annotation_ids),
        "annotation_moves": [
            {
                "annotation_id": move.annotation_id,
                "path": move.path,
                "status": move.status,
                "method": move.method,
                "method_label": ANCHOR_METHOD_LABELS.get(move.method, move.method),
                "detail": move.detail,
                "from_line": move.from_line,
                "to_line": move.to_line,
            }
            for move in refreshed.moves
        ],
    }


def _frontier_row(entry: FrontierEntry, ambiguous: frozenset[tuple[str, str]]) -> dict[str, Any]:
    """One frontier entry, named the same way the UNITS and MARKS listings name it."""

    return {
        "unit_key": entry.unit_key,
        "kind": entry.kind,
        "path": entry.path,
        "symbol": entry.symbol,
        "state": entry.state,
        "label": _unit_label(
            entry.kind,
            entry.path,
            entry.symbol,
            entry.ordinal,
            start_line=entry.start_line,
            ambiguous=(entry.path, entry.symbol) in ambiguous,
        )
        or entry.unit_key,
        "attention_rank": entry.attention_rank,
        "changed_since_mark": entry.changed_since_mark,
        "reviewed_revision_id": entry.reviewed_revision_id,
    }


def _mark_row(
    mark: ReviewMark, unit: ReviewUnit | None, ambiguous: frozenset[tuple[str, str]] = frozenset()
) -> dict[str, Any]:
    """One mark, named by what it actually covers.

    A file mark, a hunk mark and a symbol mark on ``src/app.py`` are three
    different claims about three different amounts of reading. Printing all
    three as the bare path renders them as identical rows and makes per-unit
    marking look broken, so the label carries the kind's own suffix and the row
    carries the kind itself.

    *unit* is ``None`` when the mark's unit is not in this revision at all. The
    ``unit_key`` is then the only honest name for it -- inventing a path from a
    key we cannot resolve would be a guess.
    """

    kind = unit.kind if unit is not None else ""
    path = unit.path if unit is not None else ""
    symbol = unit.symbol if unit is not None else ""
    ordinal = unit.ordinal if unit is not None else 0
    start_line = unit.start_line if unit is not None else 0
    return {
        "unit_key": mark.unit_key,
        "state": mark.state,
        "kind": kind,
        "path": path,
        "symbol": symbol,
        "ordinal": ordinal,
        "label": _unit_label(kind, path, symbol, ordinal, start_line=start_line, ambiguous=(path, symbol) in ambiguous)
        or mark.unit_key,
        "reviewed_revision_id": mark.reviewed_revision_id,
        "actor_type": mark.actor_type,
        "content": _content_status(mark.content_fingerprint, unit),
        "updated_at": mark.updated_at,
    }


def _unit_label(
    kind: str, path: str, symbol: str, ordinal: int, *, start_line: int = 0, ambiguous: bool = False
) -> str:
    """How one reviewable unit is written on a terminal line.

    ``src/app.py`` for the file, ``src/app.py::parse`` for a symbol,
    ``src/app.py#2`` for the third hunk. One spelling, used by the UNITS, MARKS
    and frontier listings, so a row printed in one can be found in the other.
    ``""`` when there is no path to name -- the caller decides what to fall back
    to.

    *ambiguous* says this file holds another unit with the same name, and the
    label then carries a disambiguator. Two rows both reading ``svc.py::run``
    have distinct ``unit_key``s, so nothing false survives in the store -- but
    on screen they are one row printed twice, and a reviewer who clicks either
    arrives at a method they did not mean. The line number is used where there
    is one, because it is the thing that actually takes a human to the right
    definition; the ordinal is the fallback for a deleted symbol that has no
    line in this revision.
    """

    if not path:
        return ""
    if kind == "symbol":
        if not symbol:
            return path
        base = f"{path}::{symbol}"
        if not ambiguous:
            return base
        return f"{base}@L{start_line}" if start_line else f"{base}#{ordinal}"
    if kind == "dependency" and symbol:
        return symbol
    if kind == "hunk":
        return f"{path}#{ordinal}"
    return path


def _ambiguous_symbols(units: Sequence[ReviewUnit]) -> frozenset[tuple[str, str]]:
    """``(path, symbol)`` pairs this revision holds more than one unit for.

    Computed over the whole revision rather than guessed per row: a label can
    only know it needs a disambiguator by seeing the sibling that makes it
    ambiguous. Nesting has usually already separated them (``Reader.run`` and
    ``Writer.run``), so this is the backstop for the cases it cannot -- two
    module-level definitions of one name, an overload set, a deleted symbol
    whose replacement kept the name.
    """

    seen: set[tuple[str, str]] = set()
    repeated: set[tuple[str, str]] = set()
    for unit in units:
        if unit.kind != "symbol" or not unit.symbol:
            continue
        key = (unit.path, unit.symbol)
        if key in seen:
            repeated.add(key)
        seen.add(key)
    return frozenset(repeated)


def _mark_counts(marks: Sequence[ReviewMark]) -> dict[str, int]:
    """Marks per state, so "how far through am I" is one number per word."""

    counts: dict[str, int] = {}
    for mark in marks:
        counts[mark.state] = counts.get(mark.state, 0) + 1
    return counts


def _content_status(fingerprint: str, unit: ReviewUnit | None) -> str:
    """Whether the marked content is still what is on screen.

    A plain comparison, not a verdict: ``changed`` says the fingerprint moved,
    and what that *means* for the mark is revision reconciliation's decision, not
    this listing's.
    """

    if unit is None:
        return "gone"
    return "same" if unit.content_fingerprint == fingerprint else "changed"


def _render_state(state: dict[str, Any]) -> str:
    """The tracked-review block appended under the packet, in terminal shape."""

    kinds = state.get("unit_kinds") or {}
    breakdown = " · ".join(f"{count} {name}" for name, count in sorted(kinds.items()))
    target_progress = state.get("target_progress") or {}
    target_count = int(state.get("target_count") or 0)
    reviewed_targets = int(target_progress.get("reviewed") or 0)
    lines = [
        "",
        f"REVIEW SESSION  {state['review_id']}  Revision {state['revision_number']}  ({state['range_mode']})",
    ]
    revision_id = str(state.get("revision_id") or "")
    if revision_id:
        lines.append(f"  checkpoint {revision_id}")
    lines.extend(
        [
            f"  files   {int(kinds.get('file') or 0)}",
            f"  review areas {target_count}  ({reviewed_targets}/{target_count} reviewed)",
            f"  units   {state['unit_count']} raw" + (f"  ({breakdown})" if breakdown else ""),
        ]
    )
    counts = state.get("mark_counts") or {}
    summary = " · ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    stale = int(state.get("stale_marks") or 0)
    if stale:
        # Never let the headline count read as "this much is done" when the
        # content underneath it has since changed.
        summary = f"{summary}  ({stale} against content that has since changed)"
    lines.append(f"  marks   {summary or 'none yet'}")

    comments = state.get("annotation_counts") or {}
    if comments:
        # Printed in the headline, not only under `--comments`: an unresolved
        # objection that a reviewer has to ask for is an objection that gets
        # committed over.
        order = ("open", "orphaned", "obsolete", "resolved")
        parts = [f"{comments[name]} {name}" for name in order if comments.get(name)]
        parts.extend(f"{count} {name}" for name, count in sorted(comments.items()) if name not in order)
        lines.append(f"  comments {' · '.join(parts)}")

    discarded = state.get("discarded_verdicts") or []
    if discarded:
        # Above everything else this command has to say. A verdict a human
        # recorded and this run deleted is the only thing on the screen that
        # cannot be discovered by looking again later.
        lines.append("")
        lines.append(f"VERDICTS DISCARDED  ({len(discarded)})")
        for item in discarded:
            kind = item["kind"] or "?"
            lines.append(f"  {item['state']:<20} {kind:<7} {item['label']}")
            lines.append(f"    {item['reason']}")

    created = state.get("created_annotation")
    if created is not None:
        lines.append("")
        lines.append(f"COMMENT RECORDED  {created['id']}")
        lines.append(
            f"  {created['kind']:<15} {created['location']}" + (f"  {created['symbol']}" if created["symbol"] else "")
        )
        if not created["anchored"]:
            lines.append(f"  not anchored: {created['anchor_detail']}")

    units = state.get("units")
    if units is not None:
        lines.append("")
        lines.append("UNITS")
        for unit in units:
            lines.append(f"  {unit['unit_key']:<30} {unit['kind']:<7} {unit['label']}")

    marks = state.get("marks")
    if marks is not None:
        lines.append("")
        lines.append("MARKS")
        if not marks:
            lines.append("  none recorded")
        for mark in marks:
            # kind + label, never the bare path: file, hunk and symbol marks on
            # one file are three different claims and must not print alike.
            kind = mark["kind"] or "?"
            lines.append(f"  {mark['state']:<20} {mark['content']:<8} {kind:<7} {mark['label']}")

    annotations = state.get("annotations")
    if annotations is not None:
        lines.extend(_render_comments(annotations))

    frontier = state.get("frontier")
    if frontier is not None:
        lines.extend(_render_frontier(frontier, state))

    downgraded = state.get("downgraded_marks") or []
    if downgraded:
        lines.append("")
        lines.append("NOT RECORDED AS ASKED")
        for item in downgraded:
            lines.append(f"  {item['target']}: {item['reason']}")

    closure = state.get("closure")
    if closure is not None:
        lines.extend(_render_closure(closure))

    feedback = state.get("feedback")
    if feedback is not None:
        lines.extend(_render_feedback(feedback))
    return "\n".join(lines)


def _render_closure(closure: dict[str, Any]) -> list[str]:
    """Render a human status choice beside the exact target-based remainder."""

    status = str(closure["status"])
    verb = {"finished": "REVIEW FINISHED", "archived": "REVIEW DISCARDED"}.get(status, "REVIEW REOPENED")
    lines = ["", f"{verb}  {closure['target_count']} targets"]
    outstanding = [
        ("still unreviewed", closure["unreviewed_targets"]),
        ("changed since reviewed", closure["changed_since_review"]),
        ("still needs changes", closure["needs_changes"]),
        ("unknown identity", closure["unknown_targets"]),
        ("comments still open", closure["open_comments"]),
        ("comments orphaned", closure["orphaned_comments"]),
    ]
    lines.append(f"  {'reviewed':<24} {closure['reviewed_targets']}")
    for label, count in outstanding:
        if count:
            lines.append(f"  {label:<24} {count}")
    if status in ("finished", "archived") and not any(count for _, count in outstanding):
        lines.append("  nothing outstanding")
    if status == "archived":
        lines.append("  read-only now; retention may permanently delete this review and its evidence")
    return lines


def _render_feedback(feedback: dict[str, Any]) -> list[str]:
    """``--feedback``: the bundle, and the fact that nothing was delivered.

    The disclaimer is not boilerplate. This command renders Markdown and stops;
    a reviewer who read ``FEEDBACK BUNDLE`` and assumed the agent had been told
    would stop watching for a reply that is never coming.
    """

    counts = f"{feedback['open']} open · {feedback['orphaned']} orphaned · {feedback['resolved']} resolved"
    return [
        "",
        f"FEEDBACK BUNDLE  ({counts})",
        "  rendered here and nowhere else — nothing was sent to any agent, host or pull request",
        "",
        *feedback["markdown"].rstrip("\n").splitlines(),
    ]


_COMMENT_BODY_CHARS = 96


def _render_comments(rows: Sequence[dict[str, Any]]) -> list[str]:
    """The ``--comments`` block: every comment, and *how* it still points anywhere.

    Every row states its rung, in words. A comment re-found by rung 4 -- its
    text happened to occur exactly once in a file whose function has since been
    renamed and re-documented -- and a comment whose file never changed at all
    are two completely different claims, and drawing them with the same weight
    is how the second one's credibility gets lent to the first. Plan SS4.4:
    *never silently move a comment to a different line after a revision*, and a
    move the reader is not told about is a silent one whether or not the line
    number happened to change.

    An unanchored or orphaned comment prints the reason too, rather than being
    filtered out or quietly listed beside a line number.
    """

    lines = ["", "ANNOTATIONS"]
    if not rows:
        lines.append("  none recorded")
        return lines
    for row in rows:
        body = " ".join(str(row["body"]).split())
        if len(body) > _COMMENT_BODY_CHARS:
            body = body[: _COMMENT_BODY_CHARS - 1] + "…"
        marker = "  " if not row["parent_id"] else "    ↳ "
        source = str(row.get("source") or "human").upper().replace("_", " ")
        lines.append(f"{marker}{source:<11} {row['state']:<10} {row['kind']:<15} {row['location']}")
        if row.get("title"):
            lines.append(f"    {row['title']}")
        lines.append(f"    {body}")
        anchor = f"    anchor: {row['anchor_method_label']}"
        if row["symbol"]:
            # The definition is re-derived on every relocation, so this names
            # where the comment sits *now*, not what it was written against.
            anchor = f"{anchor} · in {row['symbol']}"
        elif row["origin_symbol"]:
            # Nothing was located, so the stored name is history, not an
            # address. "not relocated · in gone_forever" reads as a definition
            # the reader can go open; "originally in" reads as what it is.
            anchor = f"{anchor} · originally in {row['origin_symbol']}"
        lines.append(anchor)
        if not row["anchored"]:
            lines.append(f"      {row['anchor_detail'] or 'no reason recorded'}")
    return lines


_FRONTIER_GROUPS = (
    ("changed_since_review", "CHANGED SINCE MY REVIEW"),
    ("new", "NEW SINCE MY LAST REVIEW"),
    ("unresolved", "STILL UNRESOLVED"),
    ("unchanged_reviewed", "UNCHANGED REVIEWED"),
)
_FRONTIER_ROWS = 12


def _render_frontier(frontier: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """The ``--since-my-review`` block: what your marks are worth on this revision.

    Ordered by what it costs the reader, never alphabetically. *Changed since my
    review* is first because it is the only group no ordinary diff viewer can
    show -- code a human approved and an agent then rewrote -- and putting it
    under a list of files that are still fine is how it gets missed.

    ``UNCHANGED REVIEWED`` is listed as a count rather than expanded: it is the
    group whose entire value is that the reader does **not** have to look at it.
    """

    counts = {
        "changed since my review": len(frontier["changed_since_review"]),
        "new": len(frontier["new"]),
        "still unresolved": len(frontier["unresolved"]),
        "unchanged reviewed": len(frontier["unchanged_reviewed"]),
        "not yet reviewed": int(frontier["not_yet_reviewed"]),
    }
    current = int(state["revision_number"])
    previous_raw = frontier.get("previous_revision_number")
    previous = int(previous_raw) if previous_raw is not None else None
    if previous is None:
        # No mark yet, so no Snapshot has been looked at. Not "first Snapshot":
        # this may well be the fifth, and saying otherwise would date the
        # reviewer's position to the tree's history rather than to their own.
        headline = f"SINCE MY REVIEW  Revision {current}  (nothing reviewed yet)"
    elif previous == current:
        # Idempotency is a feature and has to be visible, or a reviewer who ran
        # the command twice will read an unchanged frontier as a broken one.
        # Keyed on their last review rather than on this invocation's write:
        # whether *this* command was the one that recorded the Snapshot is an
        # accident of typing order and must not change what they are told.
        headline = f"SINCE MY REVIEW  Revision {current}  (unchanged since your last review)"
    elif previous > current:
        # An undo puts the tree back on a Snapshot already on file, so the
        # number in front of the reviewer is *lower* than the one they last
        # recorded a verdict against. Name the Snapshot movement directly.
        headline = f"SINCE MY REVIEW  Revision {current}  (the tree went back; you last saw Revision {previous})"
    else:
        headline = f"SINCE MY REVIEW  Revision {current}  (you last saw Revision {previous})"

    lines = ["", headline]
    for label, count in counts.items():
        lines.append(f"  {label:<24} {count}")
    annotations = int(frontier["open_annotations"])
    orphaned = int(frontier["orphaned_annotations"])
    if annotations or orphaned:
        lines.append(f"  {'comments':<24} {annotations} open · {orphaned} orphaned")

    for key, title in _FRONTIER_GROUPS:
        rows = frontier[key]
        if not rows:
            continue
        lines.append("")
        lines.append(f"{title}  ({len(rows)})")
        if key == "unchanged_reviewed":
            plural = "judgment" if len(rows) == 1 else "judgments"
            lines.append(f"  {len(rows)} {plural} still valid — nothing to re-read")
            continue
        for row in rows[:_FRONTIER_ROWS]:
            lines.append(f"  {row['label']}")
        if len(rows) > _FRONTIER_ROWS:
            lines.append(f"  … {len(rows) - _FRONTIER_ROWS} more")

    left = frontier["removed_units"]
    if left:
        # The other half of "nothing about your review disappears quietly". A
        # unit that left the review takes any comment thread and any verdict on
        # it out of every other list on this screen, and it is the one
        # disappearance a reviewer cannot find by scrolling.
        lines.append("")
        lines.append(f"LEFT THE REVIEW  ({len(left)})")
        for row in left[:_FRONTIER_ROWS]:
            line = f"  {(row['kind'] or '?'):<7} {row['label']}"
            verdict = str(row.get("discarded_state") or "")
            if verdict:
                line = f"{line}  — your '{verdict}' verdict went with it"
            lines.append(line)
        if len(left) > _FRONTIER_ROWS:
            lines.append(f"  … {len(left) - _FRONTIER_ROWS} more")
        if any(row.get("discarded_state") for row in left):
            # "Nothing here to re-read" is a true sentence about a unit nobody
            # had judged and a false one about a unit a human approved: their
            # verdict was destroyed, and the destruction is the thing to read.
            lines.append("  no longer in the change set — see VERDICTS DISCARDED above")
        else:
            lines.append("  no longer in the change set — nothing here to re-read")

    # Every attempt, including the ones that landed on the line they started on.
    # Filtering on `status != "unchanged"` was the same mistake twice: it hid
    # every re-find that happened not to move, and it did so on the surface
    # whose entire job is to say what the ladder did.
    moves = list(frontier["annotation_moves"])
    if moves:
        lines.append("")
        lines.append(f"COMMENT ANCHORS  ({len(moves)})")
        for move in moves[:_FRONTIER_ROWS]:
            where = f"L{move['from_line']}→L{move['to_line']}" if move["from_line"] != move["to_line"] else "same line"
            lines.append(f"  {move['status']:<11} {move['path']} {where}")
            lines.append(f"    {move['method_label']} — {move['detail']}")
        if len(moves) > _FRONTIER_ROWS:
            lines.append(f"  … {len(moves) - _FRONTIER_ROWS} more")

    for note in frontier["notes"]:
        lines.append(f"  note: {note}")
    return lines


def _active_local_branch_review(config: Any, *, store_root: Path, repo_root: Path) -> tuple[str, str]:
    """Return the active working-tree Review and its frozen base."""

    from lemoncrow.pro.capabilities.review.hosted import active_local_review_base

    return active_local_review_base(config, repo_root=repo_root, store_root=store_root, range_mode="working_tree")


def _effective_open(
    requested: bool | None,
    *,
    as_json: bool,
    html_path: Path | None,
    show_units: bool,
    mark_targets: tuple[str, ...],
    show_marks: bool,
    comment_body: str | None,
    show_comments: bool,
    since_my_review: bool,
    send_feedback: bool,
    finish_review: bool,
    reopen_review: bool,
    discard_review: bool,
    show_all: bool,
    no_color: bool,
) -> bool:
    """Choose browser vs terminal without surprising terminal/machine actions."""

    if requested is not None:
        return requested
    return not (
        as_json
        or html_path is not None
        or show_units
        or bool(mark_targets)
        or show_marks
        or comment_body is not None
        or show_comments
        or since_my_review
        or send_feedback
        or finish_review
        or reopen_review
        or discard_review
        or show_all
        or no_color
    )


def _is_bare(rev: str | None, base_rev: str | None, head_rev: str | None, *, staged: bool, working_tree: bool) -> bool:
    """True when the user asked for no range at all, so the default picked one."""

    return rev is None and base_rev is None and head_rev is None and not staged and not working_tree


def _write_html(packet: ReviewPacket, path: Path) -> Path:
    """Render and write the standalone report, or fail with a readable message."""

    from lemoncrow.pro.capabilities.review.html import render_html

    resolved = path.expanduser()
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(render_html(packet), encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"cannot write {resolved}: {exc}") from exc
    return resolved


__all__ = ["review_cmd"]
