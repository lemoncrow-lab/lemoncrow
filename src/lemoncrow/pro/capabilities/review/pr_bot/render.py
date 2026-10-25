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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from lemoncrow.pro.capabilities.review.pr_bot import attribution, impact
from lemoncrow.pro.capabilities.review.pr_bot.findings import (
    SEVERITY_BADGE,
    SEVERITY_MEANING,
    BotFinding,
    sort_key,
    trimmed,
)
from lemoncrow.pro.capabilities.review.pr_bot.settings import NoisePolicy
from lemoncrow.pro.capabilities.review.pr_bot.state import (
    DECLINED_MARKER,
    FINDING_MARKER,
    FINGERPRINT_MARKER,
    LEARNED_MARKER,
    MAX_DETAIL,
    SUMMARY_MARKER,
    ReviewState,
    Usage,
    dump_state,
    shrink,
    verdict_counts,
)
from lemoncrow.pro.capabilities.review.pr_bot.suppression import REASON_WORDS, Suppression, category_label

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.rule_suggestions import RuleSuggestion

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
#: A fingerprint as ``findings.fingerprint_for`` writes it; anything else is never put in a comment's marker.
FINGERPRINT = re.compile(r"fp-[0-9a-f]{16}")


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
    """``state`` is ``must_fix`` (an open P0), ``budget`` (this pull request's review budget is spent, so nothing new
    was reviewed), ``incomplete`` (the review could not finish), ``partial`` (it finished but left risk signals
    unexamined) or ``clean`` (it examined every risk signal and found nothing to fix first)."""

    state: str
    headline: str
    p0: int
    p1: int
    p2: int
    #: Open findings below the repository's threshold (``review.finding_threshold`` / ``strictness``): not in the counts
    #: above, said here so they are never silent.
    held: int = 0

    @property
    def counts(self) -> str:
        parts = [
            f"{self.p1} should-fix" if self.p1 else "",
            f"{self.p2} optional" if self.p2 else "",
            f"{self.held} held back" if self.held else "",
        ]
        return " · ".join(part for part in parts if part)


def verdict(
    findings: Sequence[BotFinding],
    *,
    complete: bool,
    reason: str = "",
    signals: int = 0,
    uninvestigated: int = 0,
    budget: str = "",
    held: int = 0,
    deferred_files: int = 0,
    total_files: int = 0,
) -> Verdict:
    """The one verdict line. A review that could not finish, or left risk signals unexamined, never reads as clean.

    ``signals`` is how many changed targets carry a risk signal and ``uninvestigated`` how many of them no model
    call reached; Auto looks at two however many there are. ``budget`` is why the pull request's review budget is
    spent, when it is. A found P0 (even an earlier one nobody could re-check) still reads as a P0.

    ``held`` is how many open findings sit below the repository's threshold: *findings* must hold only what is
    surfaced, and ``held`` only says how many were left out. ``deferred_files`` of ``total_files`` is a large pull
    request cut to its highest-risk files (``large_pr``): a partial review, whatever the risk signals say.
    """

    counts = verdict_counts(list(findings))
    p1, p2 = counts["p1"], counts["p2"]
    if counts["p0"]:
        return Verdict("must_fix", f"{counts['p0']} must-fix before merge", counts["p0"], p1, p2, held)
    if budget:
        return Verdict("budget", f"Review budget reached: {budget}", 0, p1, p2, held)
    if not complete:
        why = f": {reason}" if reason else ""
        return Verdict("incomplete", f"Review incomplete{why}", 0, p1, p2, held)
    if deferred_files > 0:
        total = max(total_files, deferred_files)
        headline = f"Partially reviewed: {total - deferred_files} of {total} files, highest risk first"
        return Verdict("partial", headline, 0, p1, p2, held)
    if uninvestigated > 0:
        total = max(signals, uninvestigated)
        noun = "risk signal" if total == 1 else "risk signals"
        headline = f"Partially reviewed: {total - uninvestigated} of {total} {noun} examined"
        return Verdict("partial", headline, 0, p1, p2, held)
    return Verdict("clean", "No must-fix findings", 0, p1, p2, held)


def check_status(view: Verdict, *, fail_on: str) -> tuple[str, str]:
    """``(state, description)`` of the commit status; ``state`` is a GitHub status state.

    No P0 and every risk signal examined: success. A P0 with ``fail_on == "p0"``:
    failure; without it the status stays green and says so. An incomplete review
    (a model call failed, the budget ran out) fails closed only when failing on P0:
    a gate must not pass what it could not check. A *partial* review finished but
    left risk signals unexamined; it is ``success`` either way, because failing it
    would block every pull request with more risk signals than Auto's two
    investigations for a reason its author cannot fix, but its description says
    ``Partially reviewed: N of M risk signals examined`` and never ``No must-fix
    findings``, so a green check cannot be read as full coverage. The unexamined
    signals compete for the next push's investigations; ``depth: thorough`` covers more.
    """

    gating = fail_on == "p0"
    if view.state == "must_fix":
        suffix = "" if gating else " (advisory: fail-on is off)"
        return ("failure" if gating else "success"), trimmed(view.headline + suffix, 140)
    if view.state == "budget":
        # Neutral: a commit status has no neutral state, so this is a success that says nothing was reviewed. A review
        # that stopped because its owner capped it must not block a merge; an earlier open P0 still does (above).
        return "success", trimmed(f"{view.headline}; this push was not reviewed", 140)
    if view.state == "incomplete":
        return ("error" if gating else "success"), trimmed(view.headline, 140)
    detail = f"; {view.counts}" if view.counts else ""
    return "success", trimmed(view.headline + detail, 140)  # clean, or partial: the headline says which


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


def trailer(finding: BotFinding) -> str:
    """What a finding relied on, in one line: ``Rule used: `key` · Context used: `path:1-9` (caller) · +2 more``.

    Built only from the finding's validated rule and evidence refs (:attr:`BotFinding.used`, see :mod:`attribution`),
    never from the title, explanation or consequence a model wrote. At most :data:`attribution.MAX_SHOWN` items are
    named, rules first; the rest are counted. Each name is a rule key or a file path, both of which a repository chooses,
    so they go through :func:`code` like every other path in a comment. ``""`` when the finding cites none of them.
    """

    listed, more = attribution.items(finding.used)
    shown = listed[: attribution.MAX_SHOWN]
    if not shown:
        return ""
    groups: dict[str, list[str]] = {}
    for item in shown:
        names = groups.setdefault(item.group, [])
        if item.group == "rule":
            names.append(f"`{trimmed(code(item.text), 60)}`")
        elif item.group == "ctx":
            names.append(f"`{trimmed(code(item.text), 80)}` ({item.detail})")
    parts = [
        f"{attribution.GROUPS[group]}: {', '.join(names)}" if names else attribution.GROUPS[group]
        for group, names in groups.items()
    ]
    rest = len(listed) - len(shown) + more
    if rest:
        parts.append(f"+{rest} more")
    return " · ".join(parts)


def inline_comment(finding: BotFinding) -> str:
    """A finding as an inline review comment: badge and title, why, the consequence, a suggestion if one exists.

    The suggestion is a GitHub ``suggestion`` fence, which replaces exactly the lines the comment covers
    (:meth:`BotFinding.span`, which is the lines the suggestion was verified for). The model's code is posted as
    it is: neutralising it would corrupt the edit, so a replacement that holds HTML comment delimiters is refused
    when it is parsed (``finding_replacement.parse_replacement``).
    """

    flag = " · security" if finding.scan else ""
    badge = f"**{SEVERITY_BADGE[finding.severity]} · {SEVERITY_MEANING[finding.severity]}{flag}**"
    lines = [f"{badge} — {clean(finding.title)}"]
    if finding.explanation:
        lines += ["", clean(finding.explanation)]
    if finding.consequence:
        lines += ["", f"**Consequence:** {clean(finding.consequence)}"]
    if finding.has_suggestion:
        fence = fence_for(finding.suggestion)
        lines += ["", f"{fence}suggestion", finding.suggestion.rstrip("\n"), fence]
    if trail := trailer(finding):
        lines += ["", f"<sub>{trail}</sub>"]
    if finding.static_check:
        # The scanner rule behind it (a raw result, or the evidence an investigator's finding cited).
        lines += ["", f"Static check: {code(finding.static_check)}"]
    if finding.scan:
        lines += ["", "<sub>LemonCrow security scan (Opengrep) · not yet confirmed by the AI review</sub>"]
    else:
        lines += ["", "<sub>LemonCrow AI review</sub>"]
    if FINGERPRINT.fullmatch(finding.fingerprint):
        lines.append(FINGERPRINT_MARKER.format(fingerprint=finding.fingerprint))
    lines.append(FINDING_MARKER.format(finding_id=finding.id))
    return "\n".join(lines)


def reply_text(finding: BotFinding) -> str:
    """The thread reply for a finding that is no longer open, with a marker so a re-run replies once."""

    if finding.state == "fixed":
        text = f"Fixed in {short(finding.resolved_in)}."
    elif finding.state == "unreported":
        text = (
            f"No longer reported in {short(finding.resolved_in)}: the code around it changed and this review did not "
            "find the problem again, but nothing showed that it was fixed, so this thread is left open for you to "
            "resolve."
        )
    else:
        text = f"No longer applies in {short(finding.resolved_in)}: the code it was about changed."
    return f"{text}\n\n<!-- lemoncrow:resolved {finding.id} -->"


CHAT_MARKER: Final[str] = "<!-- lemoncrow:chat {comment_id} -->"
#: The longest chat answer posted; the model is asked to be brief, this is what holds when it is not.
MAX_CHAT_ANSWER: Final[int] = 3_000


def chat_reply(
    answer: str, *, comment_id: int, usage: Usage | None = None, note: str = "", trusted: bool = False
) -> str:
    """A chat answer as a comment: neutralised text, a one-line cost, and the marker that makes a re-run answer once.

    *comment_id* is the comment being answered. The text is whatever a model (or this module) wrote about code and
    comments that came from other people, so it goes through :func:`clean`: no marker, mention, image, link target or
    HTML block it wrote survives. The only marker in the comment is the one added here.

    *trusted* is for text this module's callers built themselves (the help text, a budget notice, a "learned" reply),
    where every piece that came from a person or a model has been cleaned already: it keeps its own markup (a collapsed
    block, a hidden confirmation marker) and the ``@lemoncrow`` a person is meant to copy.
    """

    text = answer.strip() if trusted else clean(answer.strip())
    if len(text) > MAX_CHAT_ANSWER and not trusted:
        text = text[: MAX_CHAT_ANSWER - 1].rstrip() + "…"
    lines = [text]
    footer = " · ".join(part for part in (note, usage_text(usage) if usage is not None and usage.calls else "") if part)
    if footer:
        lines += ["", f"<sub>{clean(footer)}</sub>"]
    lines.append(CHAT_MARKER.format(comment_id=comment_id))
    return "\n".join(lines)


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
    unreported: int = 0
    #: Findings held back by feedback in this run (G9): a person said not to flag them on this pull request.
    suppressed: int = 0
    #: Files a large pull request left for the next run (``large_pr``) of the ``total_files`` it has.
    deferred_files: int = 0
    total_files: int = 0


@dataclass(frozen=True, slots=True)
class Receipt:
    """What a clean review checked."""

    files: tuple[str, ...]
    targets: int
    candidates: int
    investigated: int
    rules: tuple[str, ...]
    unchanged: int = 0
    #: Import warnings, said out loud rather than silently dropped (``ImportedConfig.receipt_lines``).
    imported: tuple[str, ...] = ()
    #: More lines of the receipt, written by ``notices.receipt_extra``: the intent read, the files left out.
    extra: tuple[str, ...] = ()
    #: What the security scan did (``security.SecurityScan.receipt``), and whether it could not run: that is said in the
    #: summary's status strip too, not only in the collapsed receipt.
    security: str = ""
    security_unavailable: bool = False


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
    #: Why this pull request's review budget is spent (``3 of 3 reviews used``); empty when it is not.
    budget: str = ""
    #: Rules worth offering for what people dismissed (:func:`learning.suggest`), shown as YAML to copy.
    suggestions: tuple[RuleSuggestion, ...] = ()
    #: The collapsed "Change diagram" block (G17, :mod:`diagram`), already built from the Architecture Delta; empty for none.
    #: It is the first thing :func:`render_summary` gives up when the comment is too long.
    diagram: str = ""
    #: The named risks this review examined and what became of each (:func:`impact.risk_rows`), found first. The
    #: collapsed "Risks examined" block lists up to :data:`impact.MAX_ROWS` of them; empty: no risk signal in the change.
    risks: tuple[impact.RiskRow, ...] = ()
    #: The noise controls in force (``settings``): which findings are rows, listed, or held back below the threshold.
    noise: NoisePolicy = field(default_factory=NoisePolicy)
    #: A block quote under the status strip (a large pull request cut to its riskiest files) and one small line of what
    #: the settings left out or could not do; both built by ``notices``, empty for none.
    banner: str = ""
    notes: str = ""


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
    if facts.unreported:
        parts.append(f"{facts.unreported} no longer reported")
    if facts.suppressed:
        parts.append(f"{facts.suppressed} dismissed by feedback")
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
    if finding.state == "unreported":
        return f"no longer reported in {short(finding.resolved_in)}, not confirmed fixed"
    if finding.state == "suppressed":
        return "dismissed by feedback"
    return f"fixed in {short(finding.resolved_in)}" if finding.state == "fixed" else "no longer applies"


def _row(finding: BotFinding, *, link: bool = True) -> str:
    title = clean(finding.title)
    label = f"[{title}]({finding.thread_url})" if link and finding.thread_url else title
    flag = " · security" if finding.scan else ""
    return f"- **{SEVERITY_BADGE[finding.severity]}** {label} — `{_where(finding)}` · {_tag(finding)}{flag}"


def _trail_line(finding: BotFinding) -> list[str]:
    """The indented attribution line under a finding's row in the summary, or nothing."""

    trail = trailer(finding)
    return [f"  <sub>{trail}</sub>"] if trail else []


def _held_back(findings: Sequence[BotFinding], *, detailed: bool, trails: bool = True) -> list[str]:
    heading = plural(len(findings), "low-priority finding") + " held back"
    lines = ["", f"<details><summary>{heading}</summary>", ""]
    for finding in findings:
        lines.append(_row(finding, link=False))
        if detailed and finding.explanation:
            lines.append(f"  {clean(trimmed(finding.explanation, 240))}")
        if trails:
            lines += _trail_line(finding)
    lines += ["", "</details>"]
    return lines


def _below_threshold(findings: Sequence[BotFinding], noise: NoisePolicy, *, trails: bool = True) -> list[str]:
    """Findings under the repository's threshold: one line each, never posted on the diff, in no verdict count."""

    heading = plural(len(findings), "finding") + f" held back below {noise.label}"
    lines = ["", f"<details><summary>{heading}</summary>", ""]
    for finding in findings:
        lines.append(_row(finding, link=False))
        if trails:
            lines += _trail_line(finding)
    lines += [
        "",
        f"Below this repository's threshold ({noise.label}, from {noise.threshold_source}): not posted on the diff and "
        "not counted in the verdict or the check. A P0 is never held back.",
        "",
        "</details>",
    ]
    return lines


def scope_phrase(row: Suppression) -> str:
    """What a suppression holds back, in a sentence (the category and the globs are code spans)."""

    where = f" in {', '.join(f'`{code(path)}`' for path in row.paths)}" if row.paths else ""
    if row.fingerprint:
        place = f" in `{code(row.path)}`" if row.path else ""
        return f'"{clean(row.title)}"{place}' if row.title else f"this finding{place}"
    if row.category:
        return f"`{code(category_label(row.category))}` findings{where}"
    return f'findings that mention "{clean(row.pattern)}"{where}'


def how_phrase(row: Suppression) -> str:
    """Who taught the bot a suppression and how, for a sentence. The login is in a code span, so it pings no one."""

    person = f"`{code(row.by)}`" if row.by else "someone"
    if row.source == "reaction":
        return f"thumbs-down from {person}"
    if row.source == "resolved":
        return f"thread resolved by {person} with the code unchanged"
    if row.source == "reply":
        return f"reply from {person}: {REASON_WORDS.get(row.reason, 'not wanted')}"
    return f"{person} asked for it"


def must_fix_notice(*, own: bool) -> str:
    """Why a must-fix stays when its author, the bot's own account or someone with less standing dismisses it."""

    if own:
        return "A must-fix on your own pull request needs a reviewer other than the author to dismiss it."
    return "A must-fix can be dismissed only by an owner, member or collaborator other than the pull request's author."


#: Beside a learned instruction that covers a P0 it may not hold back.
MUST_FIX_STAYS: Final[str] = (
    "a must-fix (P0) it covers stays open: only an owner, member or collaborator other than the author can dismiss one"
)


def declined_reply(row: Suppression, *, own: bool) -> str:
    """The reply in a must-fix's thread when feedback on it was recorded and refused: the P0 and the check stay."""

    return "\n".join(
        [
            must_fix_notice(own=own),
            "",
            f"Recorded: {how_phrase(row)}. The P0 and the check stay as they are. A different owner, member or "
            "collaborator can dismiss it with a thumbs-down, a reply that says so, or an `ignore` command.",
            "",
            DECLINED_MARKER.format(suppression_id=row.id),
        ]
    )


def learned_reply(
    row: Suppression,
    rule: str,
    *,
    matched: Sequence[BotFinding] = (),
    marker: bool = True,
    stays: Sequence[BotFinding] = (),
    own: bool = False,
) -> str:
    """The "learned" confirmation: what will not be flagged, why, and the rule that would make it permanent.

    *rule* is the YAML for ``.lemoncrow/review.yaml`` (:func:`rule_suggestions.rule_yaml`). The bot never writes it: a
    person copies it, and it governs reviews once it is merged to the base branch the Action reads it from. *marker*
    makes a thread's confirmation say itself once; a reply to a command carries the chat marker instead. *stays* are
    the open must-fix findings the instruction covers but its giver may not dismiss (*own*: because they opened the
    pull request); they are named, and left open.
    """

    lines = [
        f"Learned: won't flag {scope_phrase(row)} on this pull request. {how_phrase(row)[0].upper()}{how_phrase(row)[1:]}."
    ]
    if row.note and row.source in {"reaction", "resolved"}:
        lines.append(f'Their words: "{clean(row.note)}"')
    if matched:
        titles = "; ".join(clean(finding.title) for finding in matched[:5])
        more = f" and {len(matched) - 5} more" if len(matched) > 5 else ""
        lines += ["", f"{plural(len(matched), 'open finding')} taken off the verdict: {titles}{more}."]
    if stays:
        titles = "; ".join(clean(finding.title) for finding in stays[:5])
        more = f" and {len(stays) - 5} more" if len(stays) > 5 else ""
        verb = "stays" if len(stays) == 1 else "stay"
        lines += [
            "",
            f"{plural(len(stays), 'must-fix finding')} it covers {verb} open: {titles}{more}. {must_fix_notice(own=own)}",
        ]
    fence = fence_for(rule)
    lines += [
        "",
        "Make it a repo rule: add the rule below to `.lemoncrow/review.yaml` on your default branch. The Action reads "
        "that file from the pull request's merge base, so the rule takes effect once it is merged; it never writes to "
        "your repository.",
        "",
        "<details><summary>Rule for .lemoncrow/review.yaml</summary>",
        "",
        f"{fence}yaml",
        rule.rstrip("\n"),
        fence,
        "",
        "</details>",
    ]
    if marker:
        lines += ["", LEARNED_MARKER.format(suppression_id=row.id)]
    return "\n".join(lines)


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

    start, end = finding.suggestion_start, finding.suggestion_end
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
    closed = [
        finding for finding in findings if not finding.is_open and finding.state not in {"unreported", "suppressed"}
    ]
    unreported = [finding for finding in findings if finding.state == "unreported"]
    dismissed = [finding for finding in findings if finding.state == "suppressed"]
    # What the repository's threshold lets through: below it a finding is counted and listed once, never a row, an
    # inline comment, a part of the verdict or of the fix prompt. A P0 is always surfaced.
    below = view.noise.held_back(opened)
    opened = [finding for finding in opened if view.noise.tier(finding.severity) != "held"]
    shown = [finding for finding in opened if view.noise.tier(finding.severity) == "row"]
    held = [finding for finding in opened if view.noise.tier(finding.severity) == "listed"]
    result = verdict(
        opened,
        complete=view.facts.complete,
        reason=view.incomplete_reason,
        signals=view.receipt.candidates,
        uninvestigated=view.facts.uninvestigated,
        budget=view.budget,
        held=len(below),
        deferred_files=view.facts.deferred_files,
        total_files=view.facts.total_files,
    )

    head = f"**{result.headline}**" + (f" · {result.counts}" if result.counts else "")
    strip = (
        f"This push was not reviewed; the findings below are from the last review, of {short(view.head_sha)}. "
        "Raise max-usd-per-pr or max-runs-per-pr to continue."
        if view.budget
        else strip_text(view.facts)
    )
    if view.receipt.security_unavailable and not view.budget:
        strip += " · security scan unavailable"
    lines = [SUMMARY_MARKER, head]
    weighed = impact.Impact.from_state(state.impact)
    if weighed is not None:
        lines += ["", f"**{weighed.label}** · {weighed.summary()} <sub>(from the change itself, not a model)</sub>"]
    lines += ["", f"<sub>{strip}</sub>"]
    if view.banner:
        lines += ["", view.banner]
    if view.notes:
        lines += ["", f"<sub>{view.notes}</sub>"]
    if state.findings_dropped:
        lines += [
            "",
            f"{plural(state.findings_dropped, 'more finding')} not tracked: over the {len(state.findings)}-finding limit.",
        ]
    if shown:
        lines.append("")
        for finding in shown:
            lines.append(_row(finding))
            if level < 1:
                lines += _trail_line(finding)
    if held:
        lines += _held_back(held, detailed=level < 2, trails=level < 1)
    if below:
        lines += _below_threshold(below, view.noise, trails=level < 1)
    if view.risks and level < 2:
        # Before the diagram, which is what gives way first, and after the findings, which it explains.
        lines += ["", *_risk_lines(view.risks)]
    if view.diagram:
        # After the findings and before the receipt: a picture of the change, not part of the verdict.
        lines += ["", view.diagram]
    if not opened:
        lines += ["", "<details><summary>What we checked</summary>", ""]
        lines += _receipt_lines(view)
        lines += ["", "</details>"]
    if closed:
        lines += ["", f"<details><summary>{plural(len(closed), 'finding')} fixed since last review</summary>", ""]
        lines += [f"- ~~{clean(finding.title)}~~ — `{_where(finding)}` · {_tag(finding)}" for finding in closed]
        lines += ["", "</details>"]
    if dismissed:
        by_id = {row.id: row for row in state.suppressions}
        lines += ["", f"<details><summary>{plural(len(dismissed), 'finding')} dismissed by feedback</summary>", ""]
        for finding in dismissed:
            row = by_id.get(finding.suppressed_by)
            why = how_phrase(row) if row is not None else "dismissed by feedback"
            badge = f"**{SEVERITY_BADGE[finding.severity]}** " if finding.severity == "p0" else ""
            lines.append(f"- {badge}{clean(finding.title)} — `{_where(finding)}` · {why}")
        lines += ["", "Held back on this pull request only; they count toward no verdict.", "", "</details>"]
    if unreported:
        heading = plural(len(unreported), "finding") + " no longer reported, not confirmed fixed"
        lines += ["", f"<details><summary>{heading}</summary>", ""]
        lines += [f"- {clean(finding.title)} — `{_where(finding)}` · {_tag(finding)}" for finding in unreported]
        lines += [
            "",
            "The code around them changed and this review did not find them again. Their threads stay open.",
            "",
        ]
        lines += ["</details>"]
    if state.suppressions and level < 3:
        heading = f"{plural(len(state.suppressions), 'instruction')} learned on this pull request"
        lines += ["", f"<details><summary>{heading}</summary>", ""]
        must_fix = [finding for finding in opened if finding.severity == "p0"]
        lines += [
            f"- Not flagging {scope_phrase(row)} · {how_phrase(row)}"
            + (f" · {MUST_FIX_STAYS}" if any(row.declines(finding) for finding in must_fix) else "")
            for row in state.suppressions
        ]
        lines += ["", "</details>"]
    if view.suggestions and level < MAX_DETAIL:
        from lemoncrow.pro.capabilities.review.rule_suggestions import rule_yaml

        text = rule_yaml(view.suggestions)
        fence = fence_for(text)
        lines += [
            "",
            "<details><summary>Suggested rules from feedback</summary>",
            "",
            "Copy these into `.lemoncrow/review.yaml` on your default branch (add the rows under `rules:` if the file "
            "has one) to make them repo-wide. The Action reads that file from the pull request's merge base, so a rule "
            "takes effect once it is merged; nothing here is applied until then, and the bot never writes to your "
            "repository.",
            "",
            f"{fence}yaml",
            text.rstrip("\n"),
            fence,
            "",
            "</details>",
        ]
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


_OUTCOME_TEXT: Final[dict[str, str]] = {
    "found": "Found",
    "pending": "Not investigated yet",
    "cleared": "Examined, nothing found",
}
#: A risk whose open findings all sit below the repository's threshold: the verdict counts none of them, so it must not
#: read as a plain "Found".
_HELD_TEXT: Final[str] = "Found (held back)"


def _outcome_text(row: impact.RiskRow) -> str:
    return _HELD_TEXT if row.held_back else _OUTCOME_TEXT[row.outcome]


def _risk_row(row: impact.RiskRow) -> str:
    """``- **Found** — `src/auth.py:1-4`: security · 1 open finding``. Every word is fixed or a path, rule key or number."""

    # A rule's key says more than the word "rule", so the key stands in for it.
    named = [category for category in row.categories if not (category == "critical_rule" and row.rules)]
    what = ", ".join(impact.CATEGORY_WORDS.get(category, category.replace("_", " ")) for category in named)
    parts = [f"- **{_outcome_text(row)}** — `{trimmed(code(row.scope), 100)}`" + (f": {what}" if what else "")]
    if row.rules:
        parts.append("rule " + ", ".join(f"`{trimmed(code(key), 40)}`" for key in row.rules[:2]))
    if row.outcome == "found":
        mixed = f" ({row.held} held back)" if row.held and not row.held_back else ""
        parts.append(plural(row.findings, "open finding") + mixed)
    elif row.earlier:
        parts.append("examined in an earlier review, unchanged since")
    return " · ".join(parts)


def _tally(rows: Sequence[impact.RiskRow]) -> list[tuple[str, int]]:
    """``(words, count)`` for each outcome that has rows, in list order; a held-back risk is counted apart from a found one."""

    tally = impact.counts(rows)
    held = sum(1 for row in rows if row.held_back)
    counted = (
        (_OUTCOME_TEXT["found"], tally["found"] - held),
        (_HELD_TEXT, held),
        (_OUTCOME_TEXT["pending"], tally["pending"]),
        (_OUTCOME_TEXT["cleared"], tally["cleared"]),
    )
    return [(words, count) for words, count in counted if count]


def _risk_lines(rows: Sequence[impact.RiskRow]) -> list[str]:
    """The collapsed "Risks examined" block: a count line, then up to :data:`impact.MAX_ROWS` rows and ``+N more``."""

    tally = impact.counts(rows)
    held = sum(1 for row in rows if row.held_back)
    heading = f"Risks examined: {tally['found'] + tally['cleared']} of {len(rows)}"
    if tally["found"] - held:
        heading += f" · {tally['found'] - held} found"
    if held:
        heading += f" · {held} found (held back)"
    if tally["pending"]:
        heading += f" · {tally['pending']} not investigated yet"
    lines = [f"<details><summary>{heading}</summary>", ""]
    lines += [_risk_row(row) for row in rows[: impact.MAX_ROWS]]
    rest = rows[impact.MAX_ROWS :]
    if rest:
        lines.append(
            f"- +{len(rest)} more (" + ", ".join(f"{count} {words.lower()}" for words, count in _tally(rest)) + ")"
        )
    explained = (
        'These are the places in the change that carry a risk signal, found without a model. "Examined, nothing found" '
        "means a model looked at that code and reported nothing, not that it is safe."
    )
    if held:
        explained += (
            ' "Found (held back)" means the only findings about that code sit below this repository\'s threshold: '
            "they are listed above and counted in no verdict."
        )
    lines += ["", explained, "", "</details>"]
    return lines


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
    if receipt.imported:
        lines.append("- Imported config: " + "; ".join(code(item) for item in receipt.imported[:6]))
    lines.extend(f"- {item}" for item in receipt.extra)
    if receipt.security:
        lines.append(f"- {code(receipt.security)}")
    return lines


def footer_text(state: ReviewState, *, model_label: str, depth_label: str, head_sha: str, commit_url: str) -> str:
    """The summary's last line. Everything in it is held by the state, so a chat answer can rewrite it in place."""

    reviewed = f"[{short(head_sha)}]({commit_url})" if commit_url else short(head_sha)
    parts = [
        clean(model_label) or "model not reported",
        depth_label,
        f"this review: {usage_text(state.usage)}",
    ]
    if state.reviews > 1:
        parts.append(f"all {state.reviews} reviews: {usage_text(state.usage_total)}")
    if state.chats:
        parts.append(f"{plural(state.chats, 'chat answer')}: {usage_text(state.chat_usage)}")
    parts.append(f"reviewed {reviewed}")
    return " · ".join(parts)


def _footer(view: SummaryView) -> str:
    return footer_text(
        view.state,
        model_label=view.model_label,
        depth_label=view.depth_label,
        head_sha=view.head_sha,
        commit_url=view.commit_url,
    )


_FOOTER_OPEN = "\n---\n<sub>"


def with_footer(body: str, footer: str) -> str:
    """*body* with its footer line replaced by *footer*; *body* itself when it has no footer (a cut comment)."""

    start = body.rfind(_FOOTER_OPEN)
    if start < 0:
        return body
    end = body.find("</sub>", start)
    if end < 0:
        return body
    return f"{body[: start + len(_FOOTER_OPEN)]}{footer}{body[end:]}"


def render_summary(view: SummaryView) -> tuple[str, ReviewState]:
    """The summary comment and the state it carries, always within :data:`MAX_COMMENT`.

    The change diagram is the first thing given up: before any finding's text is shortened, the whole diagram goes
    (and the state stops recording it, since nothing was drawn). Then detail is removed in order -- state text, then
    visible explanations, then the prompt's -- and only then is the visible list cut, with the cut said out loud.
    The state block is always last and always whole.
    """

    if view.diagram:
        state = shrink(view.state, 0)
        visible, block = _visible(view, state, 0), dump_state(state)
        if len(visible) + len(block) + 2 <= MAX_COMMENT:
            return f"{visible}\n\n{block}", state
        view = replace(view, diagram="", state=replace(view.state, diagram=""))
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
