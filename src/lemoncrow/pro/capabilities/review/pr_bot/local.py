"""``lc review --ai`` on a change that has no pull request: the working tree, the index or a commit range.

The same engine path the pull request bot runs, with GitHub taken out of it:

1. the range is captured into a throwaway Review store (:func:`engine.capture_range`, which is what
   :func:`engine.capture_pull_request` builds on);
2. the AI stage reviews only what changed since the last review of the same scope (:func:`engine.run_ai_stage`), on
   the caller's own model route, through the same Review API endpoint, run plan, hard call cap, intelligence policy
   and kill switch;
3. :func:`reconcile.reconcile` decides what became of every finding, which is where
   :func:`findings.severity_for` rates each one, so a finding has the severity here that it would have on a pull
   request. The repository's noise controls from the trusted base's ``.lemoncrow/review.yaml`` apply as they do there:
   ``ignore`` globs keep files out of the review, a ``rules`` severity rates the findings that cite the rule, and
   ``finding_threshold`` / ``strictness`` hold back the open findings below it (:meth:`settings.NoisePolicy.held_back`,
   the rule the bot uses). A held-back finding is tracked and listed apart, is in no verdict count and never counts
   toward ``--fail-on``; a P0 is never held back;
4. the result is printed for a terminal, or as JSON, and *nothing is posted anywhere*.

What a pull request keeps in its summary comment's hidden block, a local review keeps in a small file of the same
format (:func:`state.dump_state`) under the store root, one per scope: the working tree, the index, or a range
identified by its base and the branch it ends on. A re-run on the same tree finds every reviewed unit unchanged and
makes no model call.

Exit codes, for a hook to act on (documented in ``docs/reference/cli.md``):

* ``0`` -- clean: nothing open at or above ``--fail-on`` (a finding held back by the repository's threshold is not
  open for this). An incomplete review is also ``0`` (the review fails open, so a flaky provider never blocks a push)
  unless ``--strict``;
* ``1`` -- a finding at or above ``--fail-on`` is open (``p0`` by default);
* ``2`` -- the review was incomplete (no model, the kill switch, the call or dollar budget, risk signals left
  unexamined) and ``--strict`` was given;
* ``3`` -- usage error: a flag combination or a revision that cannot be used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from lemoncrow.pro.capabilities.review.pr_bot import ci_env
from lemoncrow.pro.capabilities.review.pr_bot.findings import (
    SEVERITY_BADGE,
    SEVERITY_MEANING,
    BotFinding,
    sort_key,
    trimmed,
)
from lemoncrow.pro.capabilities.review.pr_bot.reconcile import reconcile
from lemoncrow.pro.capabilities.review.pr_bot.render import (
    Verdict,
    plural,
    short,
    usage_text,
    verdict,
)
from lemoncrow.pro.capabilities.review.pr_bot.runner import RunOptions, budget_reached
from lemoncrow.pro.capabilities.review.pr_bot.settings import NoisePolicy
from lemoncrow.pro.capabilities.review.pr_bot.state import (
    MAX_DETAIL,
    MAX_STATE_BYTES,
    ReviewState,
    Usage,
    cap_findings,
    dump_state,
    parse_state,
    shrink,
)

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.gitdiff import RevRange

#: ``--fail-on`` for a local review: which open findings make the exit code 1.
LOCAL_FAIL_ON: Final[tuple[str, ...]] = ("none", "p0", "p1")
DEFAULT_FAIL_ON: Final[str] = "p0"
EXIT_CLEAN: Final[int] = 0
EXIT_FOUND: Final[int] = 1
EXIT_INCOMPLETE: Final[int] = 2
EXIT_USAGE: Final[int] = 3
#: The ``schema`` of :meth:`LocalReport.to_wire`.
WIRE_SCHEMA: Final[int] = 1
#: Carried states kept per repository; the oldest are removed past it.
MAX_STATES_PER_REPO: Final[int] = 64
STATE_DIR: Final[str] = "review-local-ai"
_SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_CONTROL: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class LocalReviewError(RuntimeError):
    """The review cannot start for a reason in how it was asked for (exit code 3)."""


@dataclass(frozen=True)
class LocalOptions:
    """What to review and how strictly. ``rev``/``base``/``head``/``staged``/``working_tree`` choose the range the way
    ``lc review`` does; none of them reviews the working tree, else the last commit on a clean tree."""

    repo_root: Path
    store_root: Path
    rev: str | None = None
    base: str | None = None
    head: str | None = None
    staged: bool = False
    working_tree: bool = False
    depth: str = "auto"
    fail_on: str = DEFAULT_FAIL_ON
    strict: bool = False
    with_impact: bool = True
    #: Ignore the result carried from the last review of this scope and review everything again.
    fresh: bool = False
    #: Ceilings on this scope's cumulative spend, read from the carried state (the same rule as a pull request's).
    max_usd: float | None = None
    max_runs: int | None = None
    #: Names the branch a range ends on, for the identity of its carried result. Default: the current branch for
    #: ``HEAD``, else the revision as written.
    label: str = ""


@dataclass
class LocalReport:
    """The outcome of one local review. ``exit_code`` is the process's exit status."""

    scope_mode: str = ""
    scope_label: str = ""
    base: str = ""
    head: str = ""
    depth: str = "auto"
    fail_on: str = DEFAULT_FAIL_ON
    strict: bool = False
    exit_code: int = EXIT_CLEAN
    exit_reason: str = ""
    #: There was nothing to review: no changed file in the range.
    empty: bool = False
    #: No model call was made because every changed unit was already reviewed, unchanged, last time.
    cached: bool = False
    verdict: Verdict | None = None
    #: Every tracked finding, the held-back ones included (lowering the threshold shows them again with no new review).
    findings: tuple[BotFinding, ...] = ()
    #: The open findings below the repository's threshold: in :attr:`findings`, in no verdict count, never in the exit code.
    held: tuple[BotFinding, ...] = ()
    #: The noise controls this review ran under (the trusted base's ``.lemoncrow/review.yaml``), for the words about ``held``.
    noise: NoisePolicy = field(default_factory=NoisePolicy)
    state: ReviewState | None = None
    complete: bool = True
    incomplete_reason: str = ""
    files: tuple[str, ...] = ()
    targets: int = 0
    rereviewed: int = 0
    unchanged: int = 0
    candidates: int = 0
    investigated: int = 0
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        return not self.complete or (
            self.verdict is not None and self.verdict.state in {"incomplete", "partial", "budget"}
        )

    def open_findings(self) -> tuple[BotFinding, ...]:
        """The open findings the repository's threshold lets through: what the verdict and ``--fail-on`` count."""

        held = {finding.id for finding in self.held}
        return tuple(finding for finding in self.findings if finding.is_open and finding.id not in held)

    def to_wire(self) -> dict[str, Any]:
        """The machine-readable result (``--json``). ``schema`` is bumped when a field changes meaning."""

        return {
            "schema": WIRE_SCHEMA,
            "kind": "local_review",
            "scope": {"mode": self.scope_mode, "label": self.scope_label, "base": self.base, "head": self.head},
            "empty": self.empty,
            "cached": self.cached,
            "verdict": (
                None
                if self.verdict is None
                else {
                    "state": self.verdict.state,
                    "headline": self.verdict.headline,
                    "p0": self.verdict.p0,
                    "p1": self.verdict.p1,
                    "p2": self.verdict.p2,
                    # As the pull request bot's result: absent when nothing is held back.
                    **({"held_back": self.verdict.held} if self.verdict.held else {}),
                }
            ),
            "complete": not self.incomplete,
            "incomplete_reason": self.incomplete_reason,
            "exit": {
                "code": self.exit_code,
                "reason": self.exit_reason,
                "fail_on": self.fail_on,
                "strict": self.strict,
            },
            "findings": [finding.to_wire() for finding in self.findings],
            # The open findings below the repository's threshold (also among "findings"); absent when there are none,
            # so a result without the noise controls reads exactly as it always did.
            **({"held_back": [finding.to_wire() for finding in self.held]} if self.held else {}),
            "coverage": {
                "files": list(self.files),
                "targets": self.targets,
                "rereviewed": self.rereviewed,
                "unchanged": self.unchanged,
                "candidates": self.candidates,
                "investigated": self.investigated,
            },
            "depth": self.depth,
            "model": self.model,
            "usage": self.usage.to_wire(),
            "usage_total": None if self.state is None else self.state.usage_total.to_wire(),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# the exit code
# --------------------------------------------------------------------------- #


def exit_decision(*, p0: int, p1: int, incomplete: bool, reason: str, fail_on: str, strict: bool) -> tuple[int, str]:
    """``(exit code, why)``: 1 for an open finding at or above *fail_on*, else 2 for an incomplete review under
    *strict*, else 0. A found P0 outranks an incomplete review: it is something to act on, and the other is not."""

    found = {"none": 0, "p0": p0, "p1": p0 + p1}[fail_on]
    if found:
        where = "a P0" if fail_on == "p0" else "a P0 or P1"
        return EXIT_FOUND, f"{plural(found, 'open finding')} at or above --fail-on {fail_on} ({where})"
    if incomplete and strict:
        return EXIT_INCOMPLETE, f"the review is incomplete and --strict is set: {reason or 'see the coverage above'}"
    return EXIT_CLEAN, ""


def _decided(report: LocalReport) -> LocalReport:
    verdict_now = report.verdict
    counts = (0, 0) if verdict_now is None else (verdict_now.p0, verdict_now.p1)
    report.exit_code, report.exit_reason = exit_decision(
        p0=counts[0],
        p1=counts[1],
        incomplete=report.incomplete,
        reason=report.incomplete_reason,
        fail_on=report.fail_on,
        strict=report.strict,
    )
    return report


def _judged(report: LocalReport, noise: NoisePolicy, findings: Sequence[BotFinding], **details: Any) -> Verdict:
    """The verdict over what *noise* lets through; the findings below its threshold are kept on *report*, counted in the
    verdict's ``held`` and in nothing else. The same rule and the same call the pull request bot makes."""

    below = noise.held_back(findings)
    held = {finding.id for finding in below}
    report.noise, report.held = noise, tuple(below)
    return verdict([finding for finding in findings if finding.id not in held], held=len(below), **details)


# --------------------------------------------------------------------------- #
# the carried result
# --------------------------------------------------------------------------- #


def _head_label(repo_root: Path, rng: RevRange, options: LocalOptions) -> str:
    from lemoncrow.pro.capabilities.review.gitdiff import current_branch_ref

    if options.label:
        return options.label
    named = rng.head_rev if rng.mode == "commit_range" else ""
    if not named or named == "HEAD" or _SHA.match(named):
        return current_branch_ref(repo_root) or "detached"
    return named


def scope_key(repo_root: Path, rng: RevRange, options: LocalOptions) -> str:
    """The identity of the thing being reviewed over time: what a re-run should continue from.

    The working tree and the index are one scope each per branch, so commits made in between do not start over. A range
    is its base commit and the branch it ends on, so the next push of a branch (same base, new head) is incremental while
    another branch cut from the same base is not mixed in with it. Unit fingerprints, not this key, decide what is
    reused, so a collision costs a model call and never a wrong answer.
    """

    base = rng.base_sha if rng.mode == "commit_range" else ""
    document = json.dumps([rng.mode, base, _head_label(repo_root, rng, options)], separators=(",", ":"))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]


def state_path(options: LocalOptions, key: str) -> Path:
    repo = hashlib.sha256(str(options.repo_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return options.store_root / STATE_DIR / repo / f"{key}.state"


def load_state(path: Path) -> ReviewState | None:
    """The state a previous review left for this scope, or ``None`` (absent, unreadable or not a state at all)."""

    try:
        return parse_state(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def save_state(path: Path, state: ReviewState) -> ReviewState:
    """Write *state* so that :func:`load_state` reads it back; detail is shed, as in a comment, until it fits."""

    for level in range(MAX_DETAIL + 1):
        fitted = shrink(state, level)
        block = dump_state(fitted)
        if len(block) <= MAX_STATE_BYTES:
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(block, encoding="utf-8")
    os.chmod(scratch, 0o600)
    scratch.replace(path)
    _prune(path.parent)
    return fitted


def _prune(folder: Path) -> None:
    try:
        rows = sorted(folder.glob("*.state"), key=lambda row: row.stat().st_mtime, reverse=True)
        for stale in rows[MAX_STATES_PER_REPO:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass  # housekeeping only


# --------------------------------------------------------------------------- #
# the review
# --------------------------------------------------------------------------- #

NO_ROUTE: Final[str] = (
    "no model route is configured: set ANTHROPIC_API_KEY or OPENAI_API_KEY, or LEMONCROW_LLM_BACKEND "
    "(openai, litellm, ollama, or an agent CLI: claude, codex, opencode) with its provider variables"
)


def _model_problem(environ: Mapping[str, str]) -> str:
    """Why no model can be used (the kill switch, or no route), or ``""``. The same resolver every model call goes through."""

    from lemoncrow.pro.capabilities.review.pr_bot import engine

    route = ci_env.resolve_model_route(environ, str(environ.get("LEMONCROW_REVIEW_PROVIDER") or "auto"))
    if route is not None:
        ci_env.apply_model_route(route, os.environ)  # the drivers read the process environment
    status, why = engine.route_problem()
    if status == "disabled":
        return f"AI review is turned off ({why})"
    if status:
        return f"{NO_ROUTE}. {why}".strip()
    return ""


def resolve_range(options: LocalOptions) -> RevRange:
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range

    if options.depth not in ("auto", "thorough", "deep"):
        raise LocalReviewError("--depth must be auto, thorough or deep")
    if options.fail_on not in LOCAL_FAIL_ON:
        raise LocalReviewError(f"--fail-on must be one of {', '.join(LOCAL_FAIL_ON)}")
    if options.max_usd is not None and options.max_usd <= 0:
        raise LocalReviewError("--max-usd-per-pr must be above 0")
    if options.max_runs is not None and options.max_runs < 1:
        raise LocalReviewError("--max-runs-per-pr must be at least 1")
    try:
        return resolve_rev_range(
            options.repo_root,
            options.rev,
            base=options.base,
            head=options.head,
            staged=options.staged,
            working_tree=options.working_tree,
        )
    except ValueError as exc:
        raise LocalReviewError(str(exc)) from exc


def scope_label(rng: RevRange) -> str:
    if rng.mode == "working_tree":
        return f"working tree against {rng.base_rev or 'HEAD'}"
    if rng.mode == "staged":
        return f"staged changes against {rng.base_rev or 'HEAD'}"
    return f"{_shown(rng.base_rev)}..{_shown(rng.head_rev)}"


def _shown(rev: str) -> str:
    return rev[:7] if _SHA.match(rev) else rev


def _is_bare(options: LocalOptions) -> bool:
    return not (options.rev or options.base or options.head or options.staged or options.working_tree)


def run_local_review(
    options: LocalOptions,
    *,
    structured_executor: Callable[..., Any] | None = None,
    env: Mapping[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> LocalReport:
    """Review one local change on the caller's own model route; post nothing. Raises :class:`LocalReviewError` for a
    request that cannot be served (exit code 3). Every other failure is an incomplete review, never an exception, so a
    hook is not blocked by an error that is not a finding."""

    from lemoncrow.pro.capabilities.review.pr_bot import engine

    environ = os.environ if env is None else env
    say = progress or (lambda _message: None)
    rng = resolve_range(options)
    repo_root = options.repo_root
    report = LocalReport(
        scope_mode=rng.mode,
        scope_label=scope_label(rng),
        base=rng.base_sha,
        head=rng.head_sha,
        depth=options.depth,
        fail_on=options.fail_on,
        strict=options.strict,
    )
    if _is_bare(options) and rng.mode == "commit_range":
        report.notes.append("working tree clean: reviewing the last commit instead")
    path = state_path(options, scope_key(repo_root, rng, options))
    previous = None if options.fresh else load_state(path)
    limits = RunOptions(
        pr="local", repo_root=repo_root, max_usd_per_pr=options.max_usd, max_runs_per_pr=options.max_runs
    )
    spent = budget_reached(limits, previous)
    if previous is not None and spent:
        return _decided(_over_budget(report, previous, spent))

    problem = ""
    ai = None
    head_id = rng.head_sha
    try:
        with tempfile.TemporaryDirectory(prefix="lemoncrow-local-review-") as scratch:
            say("Capturing the change")
            captured = engine.capture_range(repo_root, rng, Path(scratch), with_impact=options.with_impact)
            head_id = rng.head_sha or hashlib.sha1(captured.revision.tree_fingerprint.encode("utf-8")).hexdigest()
            report.head = head_id
            if not _changed_files(captured):
                report.empty = True
                report.verdict = verdict((), complete=True)
                return _decided(report)
            problem = _model_problem(environ)
            if not problem:
                say("Running the AI review" if previous is None else "Re-reviewing what changed")
                ai = engine.run_ai_stage(
                    captured, depth=options.depth, previous=previous, structured_executor=structured_executor
                )
    except Exception as exc:  # a bug or a refusal in the review is an incomplete review, never a found P0 (exit 1)
        problem = f"the review could not run ({type(exc).__name__}: {trimmed(str(exc), 200)})"
    if ai is None or not ai.ran:
        reason = problem or (ai.reason if ai is not None else "") or "the effective run plan does not run AI review"
        report.complete = False
        report.incomplete_reason = reason
        report.findings = previous.findings if previous is not None else ()
        # No capture, so no trusted base was read: the threshold is the one the last review ran under.
        report.verdict = _judged(
            report,
            NoisePolicy.from_view(previous.view if previous else ""),
            report.findings,
            complete=False,
            reason=reason,
        )
        return _decided(report)

    noise = ai.noise  # the trusted base's .lemoncrow/review.yaml, read by the AI stage (ignore globs are applied there)
    merged = reconcile(
        previous,
        ai.findings,
        locations=ai.locations,
        current_targets=ai.target_ids,
        rereviewed=ai.rereviewed,
        covered=ai.covered,
        head=short(head_id),
        answers=ai.answers,
        intact=ai.intact,
        rule_severity=noise.rule_severity,
    )
    tracked, dropped = cap_findings(list(merged.findings))
    reason = ""
    if ai.errors:
        reason = "model calls failed: " + trimmed(ai.errors[0], 160)
    elif not ai.complete:
        reason = ai.reason
    uninvestigated = max(ai.candidates - ai.investigated, 0)
    view = _judged(
        report,
        noise,
        tracked,
        complete=ai.complete,
        reason=reason,
        signals=ai.candidates,
        uninvestigated=uninvestigated,
    )
    same_tree = previous is not None and previous.head_sha == head_id and previous.complete
    noop = ai.status == "nothing_changed" and same_tree
    state = ReviewState(
        head_sha=head_id,
        base_sha=rng.base_sha if _SHA.match(rng.base_sha) else "",
        merge_base_sha=rng.merge_base_sha if _SHA.match(rng.merge_base_sha) else "",
        pull_request=f"local:{report.scope_label}"[:200],
        depth=options.depth,
        model=ai.model,
        policy=ai.policy,
        complete=ai.complete,
        reviews=1 if previous is None else previous.reviews + (0 if noop else 1),
        reviewed_units=ai.reviewed_units,
        units_truncated=ai.units_truncated,
        findings=tuple(tracked),
        findings_dropped=dropped,
        usage=ai.usage,
        usage_total=ai.usage if previous is None else previous.usage_total.plus(ai.usage),
        view=noise.view_code(),
    )
    try:
        state = save_state(path, state)
    except OSError as exc:
        report.notes.append(f"could not save the result for the next run: {exc}")
    report.state = state
    report.findings = tuple(tracked)
    report.verdict = view
    report.complete = ai.complete and not ai.errors
    if not ai.complete or ai.errors:
        report.incomplete_reason = reason
    elif view.state == "partial":
        report.incomplete_reason = view.headline  # finished, but risk signals were left unexamined
    report.files = ai.files
    report.targets = len(ai.target_ids)
    report.rereviewed = len(ai.rereviewed)
    report.unchanged = ai.unchanged
    report.candidates = ai.candidates
    report.investigated = ai.investigated
    report.usage = ai.usage
    report.model = ai.model
    report.cached = ai.status == "nothing_changed"
    report.notes.extend(f"config: {note}" for note in noise.notes)
    if ai.ignored:
        report.notes.append(f"{plural(len(ai.ignored), 'file')} left out by review.ignore")
    if ai.errors:
        report.notes.append("model call errors: " + " | ".join(error[:200] for error in ai.errors))
    return _decided(report)


def _changed_files(captured: Any) -> bool:
    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    packet = read_packet_json(captured.store, captured.revision) or {}
    files = packet.get("files")
    return bool(files) if isinstance(files, list) else True


def _over_budget(report: LocalReport, previous: ReviewState, spent: str) -> LocalReport:
    """Say the budget is reached instead of reviewing: no model call, and the findings of the last review stand."""

    kept = tuple(
        finding.changed(state="carried") if finding.is_open else finding
        for finding in previous.findings
        if finding.is_open or finding.state == "unreported"
    )
    report.findings = kept
    report.verdict = _judged(report, NoisePolicy.from_view(previous.view), kept, complete=True, budget=spent)
    report.complete = False
    report.incomplete_reason = report.verdict.headline
    report.usage = Usage()
    report.state = replace(previous, findings=kept, usage=Usage())
    return report


# --------------------------------------------------------------------------- #
# the terminal
# --------------------------------------------------------------------------- #


def plain(text: str) -> str:
    """Model- and file-supplied *text* made safe for a terminal: no control or escape characters."""

    return _CONTROL.sub("", text)


_BADGE_COLOR: Final[Mapping[str, str]] = {"p0": "red", "p1": "yellow", "p2": "bright_black"}


def render_text(report: LocalReport, *, style: Callable[..., str] | None = None, show_all: bool = False) -> str:
    """The report for a terminal. *style* is ``click.style`` (or ``None`` for plain text)."""

    def paint(text: str, **kwargs: Any) -> str:
        return style(text, **kwargs) if style is not None else text

    lines = [paint(f"LemonCrow review · {report.scope_label}", bold=True)]
    if report.empty:
        lines.append("Nothing to review: no changed files in this range.")
        return "\n".join(lines) + "\n"
    view = report.verdict
    opened = sorted(report.open_findings(), key=sort_key)
    if view is not None:
        mark = {"must_fix": "red", "clean": "green"}.get(view.state, "yellow")
        counts = f" · {view.counts}" if view.counts else ""
        lines.append(paint(plain(view.headline) + counts, fg=mark, bold=True))
    if report.cached:
        lines.append("Nothing changed since the last review of this scope: its results are reused, with no model call.")
    lines.append("")
    shown = [finding for finding in opened if finding.severity != "p2"]
    listed = [finding for finding in opened if finding.severity == "p2"]
    for finding in shown:
        lines.extend(_finding_lines(finding, paint))
    if listed:
        lines.append(paint(f"{plural(len(listed), 'optional finding')} (P2):", dim=True))
        lines.extend(_one_line_rows(listed, show_all=show_all))
        lines.append("")
    if report.held:
        # Below the repository's threshold: tracked, shown here once, in no verdict count and never in the exit code.
        noise = report.noise
        heading = f"{plural(len(report.held), 'finding')} held back below {noise.label} (from {noise.threshold_source})"
        lines.append(paint(f"{heading}; not counted toward --fail-on. A P0 is never held back:", dim=True))
        lines.extend(_one_line_rows(sorted(report.held, key=sort_key), show_all=show_all))
        lines.append("")
    closed = [finding for finding in report.findings if finding.state == "fixed"]
    if closed:
        lines.append(f"Fixed since the last review: {', '.join(plain(finding.title) for finding in closed)}")
        lines.append("")
    if report.state is not None:
        lines.append(_coverage_line(report))
        lines.append(f"Model: {report.model or 'none'} · depth {report.depth} · {usage_text(report.usage)}")
    shown_reason = report.incomplete_reason
    if report.incomplete and shown_reason and (view is None or shown_reason not in view.headline):
        lines.append(paint(f"Incomplete: {plain(shown_reason)}", fg="yellow"))
    for note in report.notes:
        lines.append(f"Note: {plain(note)}")
    if report.exit_code:
        lines.append(paint(f"Exit {report.exit_code}: {report.exit_reason}", fg="red"))
    return "\n".join(lines) + "\n"


def _one_line_rows(findings: Sequence[BotFinding], *, show_all: bool) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        lines.append(f"  {finding.severity.upper()}  {_where(finding)}  {plain(finding.title)}")
        if show_all and finding.explanation:
            lines.append(f"      {plain(finding.explanation)}")
    return lines


def _where(finding: BotFinding) -> str:
    path = plain(finding.path)
    if finding.line <= 0:
        return path
    return f"{path}:{finding.line}" + (f"-{finding.end_line}" if finding.end_line > finding.line else "")


def _finding_lines(finding: BotFinding, paint: Callable[..., str]) -> list[str]:
    badge = SEVERITY_BADGE.get(finding.severity, finding.severity.upper())
    meaning = SEVERITY_MEANING.get(finding.severity, "")
    head = paint(f"{badge} · {meaning}", fg=_BADGE_COLOR.get(finding.severity, "white"), bold=True)
    lines = [f"{head}  {_where(finding)}  [{plain(finding.category)}]", f"    {plain(finding.title)}"]
    if finding.explanation:
        lines.append(f"    {plain(finding.explanation)}")
    if finding.consequence:
        lines.append(f"    Consequence: {plain(finding.consequence)}")
    if finding.has_suggestion:
        lines.append(f"    Suggested replacement for lines {finding.suggestion_start}-{finding.suggestion_end}:")
        lines.extend(f"      | {plain(row)}" for row in finding.suggestion.splitlines())
    if finding.state == "carried":
        lines.append(f"    (carried from the last review, first seen {finding.first_seen or 'earlier'})")
    lines.append("")
    return lines


def _coverage_line(report: LocalReport) -> str:
    if report.targets == 0:
        return "Reviewed: no reviewable change in this range."
    reviewed = report.rereviewed
    text = f"Reviewed {plural(reviewed, 'target')} in this run"
    if report.unchanged:
        text += f"; {report.unchanged} unchanged since the last review (carried, no model call)"
    if report.candidates and report.investigated < report.candidates:
        text += f"; {report.investigated} of {plural(report.candidates, 'risk signal')} examined"
    return text + f" · {plural(len(report.files), 'file')} in the range"


__all__ = [
    "DEFAULT_FAIL_ON",
    "EXIT_CLEAN",
    "EXIT_FOUND",
    "EXIT_INCOMPLETE",
    "EXIT_USAGE",
    "LOCAL_FAIL_ON",
    "LocalOptions",
    "LocalReport",
    "LocalReviewError",
    "exit_decision",
    "load_state",
    "render_text",
    "run_local_review",
    "save_state",
    "scope_key",
    "state_path",
]
