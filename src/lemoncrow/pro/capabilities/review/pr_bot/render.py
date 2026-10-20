"""What the bot writes on the pull request: the summary comment, the inline comments, the agent prompt.

Pure text. Tone is terse and specific: a verdict, then the findings, then the proof
of work. One rule decides the verdict and the check status together (:func:`verdict`),
so the comment and the status can never disagree.

Everything a model wrote goes through :func:`clean` first. A finding is text derived
from code in the pull request, which is untrusted: it must not be able to forge the
hidden markers this bot finds its own comments and state by, mention people, load an
image, or close a ``<details>`` block early.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from lemoncrow.pro.capabilities.review.pr_bot.findings import (
    SEVERITY_BADGE,
    SEVERITY_MEANING,
    BotFinding,
    sort_key,
    trimmed,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import (
    FINDING_MARKER,
    MAX_DETAIL,
    SUMMARY_MARKER,
    ReviewState,
    Usage,
    dump_state,
    shrink,
    verdict_counts,
)

#: GitHub refuses a comment over 65,536 characters; stay clear of it.
MAX_COMMENT: Final[int] = 65_000
STATUS_CONTEXT: Final[str] = "LemonCrow Review"
#: Findings whose full text goes into the agent prompt; the rest are listed by location.
PROMPT_FINDINGS: Final[int] = 25

_ZWSP = "​"
_DANGEROUS_TAG = re.compile(
    r"<(?=/?(?:img|picture|source|video|audio|iframe|script|style|link|a|details|summary|!--))", re.IGNORECASE
)
_MENTION = re.compile(r"@(?=[A-Za-z0-9_])")


def clean(text: str) -> str:
    """Model-written *text* made inert: no markers, mentions, images, links or block tags it could forge."""

    cleaned = text.replace("-->", f"-{_ZWSP}->").replace("![", f"!{_ZWSP}[").replace("](", f"]{_ZWSP}(")
    return _MENTION.sub(f"@{_ZWSP}", _DANGEROUS_TAG.sub(f"<{_ZWSP}", cleaned))


def code(text: str) -> str:
    """*text* safe inside a Markdown code span on one line: a file name is attacker-chosen and may hold a backtick."""

    return " ".join(clean(text).replace("`", "'").split())


def fence_for(text: str) -> str:
    """A code fence longer than any backtick run inside *text*."""

    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def plural(count: int, singular: str, many: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (many or singular + 's')}"


def short(sha: str) -> str:
    return sha[:7]


# --------------------------------------------------------------------------- #
# verdict and check status
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Verdict:
    """``state`` is ``must_fix`` (an open P0), ``incomplete`` (not fully reviewed) or ``clean``."""

    state: str
    headline: str
    p0: int
    p1: int
    p2: int

    @property
    def counts(self) -> str:
        parts = [f"{self.p1} should-fix" if self.p1 else "", f"{self.p2} optional" if self.p2 else ""]
        return " · ".join(part for part in parts if part)


def verdict(findings: Sequence[BotFinding], *, complete: bool, reason: str = "") -> Verdict:
    """The one verdict line. An incomplete review never reads as clean, but a found P0 still reads as a P0."""

    counts = verdict_counts(list(findings))
    if counts["p0"]:
        return Verdict("must_fix", f"{counts['p0']} must-fix before merge", counts["p0"], counts["p1"], counts["p2"])
    if not complete:
        why = f": {reason}" if reason else ""
        return Verdict("incomplete", f"Review incomplete{why}", 0, counts["p1"], counts["p2"])
    return Verdict("clean", "No must-fix findings", 0, counts["p1"], counts["p2"])


def check_status(view: Verdict, *, fail_on: str) -> tuple[str, str]:
    """``(state, description)`` of the commit status; ``state`` is a GitHub status state.

    No P0: success. A P0 with ``fail_on == "p0"``: failure; without it the status
    stays green and says so. An incomplete review fails closed only when failing on
    P0 (a gate must not pass what it could not check) and is otherwise advisory.
    """

    gating = fail_on == "p0"
    if view.state == "must_fix":
        suffix = "" if gating else " (advisory: fail-on is off)"
        return ("failure" if gating else "success"), trimmed(view.headline + suffix, 140)
    if view.state == "incomplete":
        return ("error" if gating else "success"), trimmed(view.headline, 140)
    detail = f"; {view.counts}" if view.counts else ""
    return "success", trimmed(view.headline + detail, 140)


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #


def compact_count(value: int) -> str:
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return f"{value / 1_000_000:.1f}M".replace(".0M", "M")


def money(value: float) -> str:
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.3f}" if value < 0.1 else f"${value:.2f}"


def usage_text(usage: Usage) -> str:
    """``2 model calls, 14.2k tokens, ~3k of it repository context, est. $0.003`` -- the cost only when a price is known."""

    if usage.calls == 0:
        return "no model calls"
    parts = [plural(usage.calls, "model call")]
    if usage.input_tokens is None or usage.output_tokens is None:
        parts.append("tokens not reported")
    else:
        parts.append(f"{compact_count(usage.input_tokens + usage.output_tokens)} tokens")
    if usage.context_tokens:
        # The context is already inside the input tokens above (or unreported); this says how much of it there was.
        parts.append(f"~{compact_count(usage.context_tokens)} of it repository context")
    if usage.cost_usd is not None:
        parts.append(f"est. {money(usage.cost_usd)}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# the inline comment
# --------------------------------------------------------------------------- #


def inline_comment(finding: BotFinding) -> str:
    """A finding as an inline review comment: badge and title, why, the consequence, a suggestion if one exists.

    The suggestion is a GitHub ``suggestion`` fence, which replaces exactly the lines the comment covers
    (:meth:`BotFinding.span`, which is the lines the suggestion was verified for). The model's code is posted as
    it is: neutralising it would corrupt the edit, so a replacement that holds HTML comment delimiters is refused
    when it is parsed (``finding_replacement.parse_replacement``).
    """

    badge = f"**{SEVERITY_BADGE[finding.severity]} · {SEVERITY_MEANING[finding.severity]}**"
    lines = [f"{badge} — {clean(finding.title)}"]
    if finding.explanation:
        lines += ["", clean(finding.explanation)]
    if finding.consequence:
        lines += ["", f"**Consequence:** {clean(finding.consequence)}"]
    if finding.has_suggestion:
        fence = fence_for(finding.suggestion)
        lines += ["", f"{fence}suggestion", finding.suggestion.rstrip("\n"), fence]
    lines += ["", "<sub>LemonCrow AI review</sub>", FINDING_MARKER.format(finding_id=finding.id)]
    return "\n".join(lines)


def reply_text(finding: BotFinding) -> str:
    """The thread reply for a finding that is no longer open, with a marker so a re-run replies once."""

    if finding.state == "fixed":
        text = f"Fixed in {short(finding.resolved_in)}."
    else:
        text = f"No longer applies in {short(finding.resolved_in)}: the code it was about changed."
    return f"{text}\n\n<!-- lemoncrow:resolved {finding.id} -->"


# --------------------------------------------------------------------------- #
# the summary comment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RunFacts:
    """What this run did, for the status strip."""

    first_review: bool
    changed_lines: int
    changed_files: int
    new: int
    carried: int
    fixed: int
    stale: int
    complete: bool = True
    uninvestigated: int = 0


@dataclass(frozen=True, slots=True)
class Receipt:
    """What a clean review checked."""

    files: tuple[str, ...]
    targets: int
    candidates: int
    investigated: int
    rules: tuple[str, ...]
    unchanged: int = 0


@dataclass(frozen=True, slots=True)
class SummaryView:
    """Everything the summary shows; ``state`` is what its hidden block carries."""

    state: ReviewState
    findings: tuple[BotFinding, ...]
    facts: RunFacts
    receipt: Receipt
    pull_number: int
    repository: str
    head_sha: str
    commit_url: str = ""
    incomplete_reason: str = ""
    model_label: str = ""
    depth_label: str = "Auto"
    max_calls: int = 0
    fail_on: str = "none"


def strip_text(facts: RunFacts) -> str:
    lines = plural(facts.changed_lines, "changed line")
    if facts.first_review:
        parts = [f"Reviewed {lines} in {plural(facts.changed_files, 'file')}", f"{facts.new} new"]
    else:
        parts = [
            f"Re-reviewed {lines}",
            f"{facts.new} new",
            f"{facts.carried} carried forward",
            f"{facts.fixed + facts.stale} fixed since last review",
        ]
    if facts.uninvestigated:
        parts.append(f"{plural(facts.uninvestigated, 'risk signal')} not investigated yet")
    if not facts.complete:
        parts.append("incomplete")
    return " · ".join(parts)


def _where(finding: BotFinding) -> str:
    path = code(finding.path)
    if finding.line <= 0:
        return path
    start, end = finding.span
    span = f"{start}-{end}" if end > start else str(start)
    return f"{path}:{span}"


def _tag(finding: BotFinding) -> str:
    if finding.state == "new":
        return "new"
    if finding.state == "carried":
        return "carried forward, unverified" if finding.unverified else "carried forward"
    return f"fixed in {short(finding.resolved_in)}" if finding.state == "fixed" else "no longer applies"


def _row(finding: BotFinding, *, link: bool = True) -> str:
    title = clean(finding.title)
    label = f"[{title}]({finding.thread_url})" if link and finding.thread_url else title
    return f"- **{SEVERITY_BADGE[finding.severity]}** {label} — `{_where(finding)}` · {_tag(finding)}"


def _held_back(findings: Sequence[BotFinding], *, detailed: bool) -> list[str]:
    heading = plural(len(findings), "low-priority finding") + " held back"
    lines = ["", f"<details><summary>{heading}</summary>", ""]
    for finding in findings:
        lines.append(_row(finding, link=False))
        if detailed and finding.explanation:
            lines.append(f"  {clean(trimmed(finding.explanation, 240))}")
    lines += ["", "</details>"]
    return lines


def agent_prompt(
    findings: Sequence[BotFinding], *, pull_number: int, repository: str, head_sha: str, detailed: bool = True
) -> str:
    """A prompt to paste into Claude Code, Codex or Cursor that lists the open findings with file:line."""

    lines = [
        f"Fix the open review findings on pull request #{pull_number} ({repository}) at commit {short(head_sha)}.",
        "For each one: open the file at the location, confirm the problem is real, make the smallest change that fixes",
        "it, and run the project's tests. If a finding is wrong, say why and leave the code alone. Change nothing else.",
        "",
    ]
    for number, finding in enumerate(findings, 1):
        lines.append(f"{number}. [{SEVERITY_BADGE[finding.severity]}] {_where(finding)} - {clean(finding.title)}")
        if detailed and number <= PROMPT_FINDINGS:
            if finding.explanation:
                lines.append(f"   Why: {clean(trimmed(finding.explanation, 400))}")
            if finding.consequence:
                lines.append(f"   Consequence: {clean(trimmed(finding.consequence, 200))}")
            if finding.has_suggestion:
                lines += _suggested_edit(finding)
    return "\n".join(lines)


def _suggested_edit(finding: BotFinding) -> list[str]:
    """The checked replacement for a prompt: the lines it covers and what they become, in its own fence."""

    start, end = finding.span
    fence = fence_for(finding.suggestion)
    covers = f"lines {start}-{end}" if end > start else f"line {start}"
    # The code stays at column 0: its indentation is part of the edit.
    return [
        f"   Suggested edit, checked against this commit: replace {covers} with",
        fence,
        finding.suggestion.rstrip("\n"),
        fence,
    ]


def _visible(view: SummaryView, state: ReviewState, level: int) -> str:
    """The readable part of the comment at one detail level (0 full; higher drops explanation text too)."""

    findings = sorted(view.findings, key=sort_key)
    opened = [finding for finding in findings if finding.is_open]
    closed = [finding for finding in findings if not finding.is_open]
    shown = [finding for finding in opened if finding.severity != "p2"]
    held = [finding for finding in opened if finding.severity == "p2"]
    result = verdict(opened, complete=view.facts.complete, reason=view.incomplete_reason)

    head = f"**{result.headline}**" + (f" · {result.counts}" if result.counts else "")
    lines = [SUMMARY_MARKER, head, "", f"<sub>{strip_text(view.facts)}</sub>"]
    if state.findings_dropped:
        lines += [
            "",
            f"{plural(state.findings_dropped, 'more finding')} not tracked: over the {len(state.findings)}-finding limit.",
        ]
    if shown:
        lines.append("")
        lines += [_row(finding) for finding in shown]
    if held:
        lines += _held_back(held, detailed=level < 2)
    if not opened:
        lines += ["", "<details><summary>What we checked</summary>", ""]
        lines += _receipt_lines(view)
        lines += ["", "</details>"]
    if closed:
        lines += ["", f"<details><summary>{plural(len(closed), 'finding')} fixed since last review</summary>", ""]
        lines += [f"- ~~{clean(finding.title)}~~ — `{_where(finding)}` · {_tag(finding)}" for finding in closed]
        lines += ["", "</details>"]
    if opened:
        prompt = agent_prompt(
            opened,
            pull_number=view.pull_number,
            repository=view.repository,
            head_sha=view.head_sha,
            detailed=level < 3,
        )
        fence = fence_for(prompt)
        lines += [
            "",
            "<details><summary>Fix with your agent</summary>",
            "",
            "Paste this into Claude Code, Codex or Cursor, in a checkout of this branch.",
            "",
            f"{fence}text",
            prompt,
            fence,
            "",
            "</details>",
        ]
    lines += ["", "---", f"<sub>{_footer(view)}</sub>"]
    return "\n".join(lines)


def _receipt_lines(view: SummaryView) -> list[str]:
    receipt = view.receipt
    shown = ", ".join(f"`{code(path)}`" for path in receipt.files[:20])
    more = f" and {len(receipt.files) - 20} more" if len(receipt.files) > 20 else ""
    rules = ", ".join(f"`{code(rule)}`" for rule in receipt.rules[:10]) or "none configured"
    lines = [
        f"- Files: {plural(len(receipt.files), 'file')}" + (f" ({shown}{more})" if receipt.files else ""),
        f"- Targets in this change: {receipt.targets}; risk signals: {receipt.candidates}; "
        f"investigated by the model: {receipt.investigated}"
        + (" (the depth limit)" if receipt.investigated < receipt.candidates else ""),
        f"- Rules applied: {rules}",
        f"- Depth: {view.depth_label}" + (f", up to {plural(view.max_calls, 'model call')}" if view.max_calls else ""),
    ]
    if receipt.unchanged:
        lines.insert(2, f"- Unchanged since the last review, not re-checked: {receipt.unchanged}")
    return lines


def _footer(view: SummaryView) -> str:
    state = view.state
    reviewed = f"[{short(view.head_sha)}]({view.commit_url})" if view.commit_url else short(view.head_sha)
    parts = [
        clean(view.model_label) or "model not reported",
        view.depth_label,
        f"this review: {usage_text(state.usage)}",
    ]
    if state.reviews > 1:
        parts.append(f"all {state.reviews} reviews: {usage_text(state.usage_total)}")
    parts.append(f"reviewed {reviewed}")
    return " · ".join(parts)


def render_summary(view: SummaryView) -> tuple[str, ReviewState]:
    """The summary comment and the state it carries, always within :data:`MAX_COMMENT`.

    Detail is removed in order -- state text, then visible explanations, then the
    prompt's -- and only then is the visible list cut, with the cut said out loud.
    The state block is always last and always whole.
    """

    for level in range(MAX_DETAIL + 1):
        state = shrink(view.state, level)
        visible, block = _visible(view, state, level), dump_state(state)
        if len(visible) + len(block) + 2 <= MAX_COMMENT:
            return f"{visible}\n\n{block}", state
    state = shrink(view.state, MAX_DETAIL)
    block = dump_state(state)
    cut = replace(view, state=state, findings=tuple(sorted(view.findings, key=sort_key)[:30]))
    note = f"\n\n{plural(len(view.findings) - len(cut.findings), 'finding')} not shown here: the comment is at its size limit.\n"
    visible = _visible(cut, state, MAX_DETAIL)
    room = MAX_COMMENT - len(block) - len(note) - 2
    return f"{visible[:room]}{note}\n{block}", state
