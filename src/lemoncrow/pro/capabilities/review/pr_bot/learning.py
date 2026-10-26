"""Learning from feedback (G9): reactions, resolved threads, replies and ``ignore`` commands, applied on the next push.

A reviewer that waits for weeks of consistent reactions before it adapts is slow to correct. This applies feedback in
the run that sees it (the next push, re-run or command), for the pull request the feedback is on, and says so in the
thread:

1. **Read** what people did to the bot's inline comments since the last run (:func:`read_signals`): a thumbs-down, a
   thumbs-up, a reply that says it is not a bug or will not be fixed, a thread resolved by a person while the finding
   still stands and its code has not changed. GitHub sends no event for a reaction, so a reaction is seen by the next
   run (a push, a re-run, or an ``ignore``/``recheck`` command, which re-render the summary); a reply or a command is
   also an event the chat mode sees.
2. **Authorise** each person (:class:`People`): the same ``author_association`` allow-list chat uses. A reaction
   carries no association, so it is taken from a comment of theirs on the pull request, else looked up as the
   repository permission (write or above counts as a collaborator), and refused when GitHub will not say. A must-fix
   (P0) asks for more (:func:`may_dismiss_must_fix`): an owner, member or collaborator who did not open the pull
   request. Without that, with ``fail-on: p0`` the author could dismiss the finding that blocks their own merge.
3. **Suppress** (:func:`learn`): each dismissal becomes a :class:`~suppression.Suppression` in the summary's hidden state
   and is applied at once to the finding it came from and to any finding the same push reports for the same concern.
   A thumbs-up does not suppress anything; it marks the finding ``confirmed`` (kept for analytics). Feedback from
   someone who may not dismiss a P0 is recorded the same way but leaves a P0 open (``Suppression.dismisses_p0``).
4. **Confirm** in the thread (:func:`confirmations`), once per thread and suppression, with the rule it could become;
   or, for a P0 the feedback could not dismiss, say so (:func:`refusals`).
5. **Suggest** (:func:`suggest`): wider learning is never silent. The summary gets a collapsed "Suggested rules from
   feedback" block with YAML for ``.lemoncrow/review.yaml``, built with ``rule_suggestions`` so the existing
   rule loader accepts it. The bot writes nothing to the repository; the Action reads that file from the merge base, so
   a suggested rule governs reviews once a person has merged it.

Everything a person wrote is text, kept short and inert (:func:`suppression.fragment`) before it goes into the state, a
comment or a YAML scalar.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from lemoncrow.pro.capabilities.review.pr_bot import suppression as sup
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub, FeedbackThread, Remote, ThreadComment
from lemoncrow.pro.capabilities.review.pr_bot.render import declined_reply, learned_reply, must_fix_notice
from lemoncrow.pro.capabilities.review.rule_suggestions import (
    RuleSuggestion,
    path_family,
    rule_yaml,
    suggestion_identity,
)

# lc-debt: a suggestion is a `rules:` row written in words, because `.lemoncrow/review.yaml` has no key that suppresses a
# category under a path (the investigator reads the rule; a per-rule question may ask it); and a suppression cannot be
# taken back from a comment; upgrade: an `ignore:` section in the policy file loaded with `rules`, and an `unignore` command.
_LEARNED = re.compile(r"<!-- lemoncrow:learned (sup-[0-9a-f]{10}) -->")
_DECLINED = re.compile(r"<!-- lemoncrow:declined (sup-[0-9a-f]{10}) -->")
THUMBS_UP: Final[str] = "THUMBS_UP"
THUMBS_DOWN: Final[str] = "THUMBS_DOWN"

# --------------------------------------------------------------------------- #
# what a reply means
# --------------------------------------------------------------------------- #

#: In this order; the first that matches names the reason, in the vocabulary of a Review's finding dismissal.
_REPLY_REASONS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "incorrect",
        re.compile(
            r"\bnot an? (?:bug|issue|problem|error|defect)\b|\bno (?:bug|issue|problem) here\b|\bfalse[- ]positive\b"
            r"|\bincorrect\b|\bthis is wrong\b|\bisn'?t (?:a bug|an issue|a problem)\b|\bnothing wrong\b",
            re.IGNORECASE,
        ),
    ),
    (
        "accepted_risk",
        re.compile(
            r"\bwon'?t[- ]?fix\b|\bwill not fix\b|\bwontfix\b|\bby design\b|\bas designed\b|\bintentional(?:ly)?\b"
            r"|\bon purpose\b|\b(?:working|works) as intended\b|\baccepted risk\b|\bknown (?:limitation|issue)\b"
            r"|\bexpected behaviou?r\b|\bharmless\b|\b(?:safe|fine) to ignore\b|\bcan be ignored\b|\bignore (?:this|it)\b",
            re.IGNORECASE,
        ),
    ),
    ("duplicate", re.compile(r"\bduplicate\b|\bdupe\b|\bsame as (?:the )?(?:above|other|previous)\b", re.IGNORECASE)),
    (
        "not_applicable",
        re.compile(
            r"\bnot applicable\b|\bn/a\b|\b(?:doesn'?t|does not) apply\b|\birrelevant\b|\bout of scope\b",
            re.IGNORECASE,
        ),
    ),
)


def classify_reply(body: str, mentions: Sequence[str] = ("@lemoncrow",)) -> str:
    """The dismissal reason a reply states (``incorrect``, ``accepted_risk``, ``duplicate``, ``not_applicable``), else ``""``.

    High precision on purpose: a reply is a dismissal only when it says so in a known phrase, and never when it asks
    (a question mark), addresses the bot (that is chat, not feedback), or only quotes someone. A reply that merely
    gives a reason ("the tests use bare asserts") is kept as the *note* of a thumbs-down or a resolve, not read alone.
    """

    from lemoncrow.pro.capabilities.review.pr_bot.ci_env import find_mention

    text = re.sub(r"(?m)^\s*>.*$", " ", re.sub(r"```.*?```", " ", body, flags=re.DOTALL))
    if "?" in text or find_mention(text, mentions) is not None:
        return ""
    for reason, pattern in _REPLY_REASONS:
        if pattern.search(text):
            return reason
    return ""


# --------------------------------------------------------------------------- #
# who counts
# --------------------------------------------------------------------------- #


def is_bot_login(login: str, kind: str = "") -> bool:
    return not login or login.lower().endswith("[bot]") or kind.lower() == "bot"


def may_dismiss_must_fix(login: str, *, association: str, author: str, kind: str = "") -> bool:
    """Whether *login* may dismiss a must-fix (P0): an owner, member or collaborator who did not open the pull request.

    *association* is theirs as GitHub reports it and *author* the login of the pull request's author. Fails closed: with
    no known author nobody may, and a bot or an app never does. ``CONTRIBUTOR`` is refused on purpose
    (:data:`suppression.P0_DISMISSERS` says why). Whether they may teach the bot at all is :meth:`People.may`'s question.
    """

    return (
        not is_bot_login(login, kind)
        and bool(author)
        and login.lower() != author.lower()
        and association.upper() in sup.P0_DISMISSERS
    )


class People:
    """Whether a login may teach the bot, by the same ``author_association`` allow-list chat uses.

    *known* maps a login to an association GitHub put on a comment of theirs in this pull request. For anyone else,
    *lookup* returns the repository permission (``admin``, ``maintain``, ``write``, ... or ``""`` when GitHub would not
    say): write or above counts as a collaborator, the repository's own owner as its owner. An answer of ``""`` is a no.
    *author* is the pull request's author, who may teach the bot like anyone else on the list but may not dismiss a
    must-fix (:meth:`may_dismiss_p0`).
    """

    def __init__(
        self,
        allowed: Collection[str],
        known: Mapping[str, str],
        *,
        owner: str,
        lookup: Callable[[str], str],
        author: str = "",
    ) -> None:
        self._author = author
        self._allowed = frozenset(allowed)
        self._known = {login.lower(): association.upper() for login, association in known.items() if association}
        self._owner = owner.lower()
        self._lookup = lookup
        self._cache: dict[str, str] = {}

    def association(self, login: str) -> str:
        key = login.lower()
        if key in self._known:
            return self._known[key]
        if key not in self._cache:
            permission = self._lookup(login).lower()
            if permission in {"admin", "maintain", "write"}:
                self._cache[key] = "OWNER" if key == self._owner else "COLLABORATOR"
            else:
                self._cache[key] = "NONE"
        return self._cache[key]

    def may(self, login: str, kind: str = "") -> bool:
        return not is_bot_login(login, kind) and self.association(login) in self._allowed

    def may_dismiss_p0(self, login: str, kind: str = "") -> bool:
        """Whether *login* may teach the bot and may also dismiss a must-fix: see :func:`may_dismiss_must_fix`."""

        return self.may(login, kind) and may_dismiss_must_fix(
            login, association=self.association(login), author=self._author, kind=kind
        )


# --------------------------------------------------------------------------- #
# the signals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Signal:
    """One person's feedback on one open finding. ``kind``: ``down``, ``reply``, ``resolved`` or ``up``.

    ``dismisses_p0``: whether *by* may dismiss a must-fix (:func:`may_dismiss_must_fix`). When it is false and the
    finding is a P0, the feedback is recorded and the P0 stays.
    """

    finding: BotFinding
    kind: str
    by: str
    reason: str = ""
    note: str = ""
    dismisses_p0: bool = False


def read_signals(
    threads: Sequence[FeedbackThread],
    findings: Sequence[BotFinding],
    people: People,
    *,
    code_changed: Callable[[BotFinding], bool],
) -> list[Signal]:
    """The feedback on each open, posted finding that an allowed person gave since it was posted.

    A thumbs-down wins over a reply, a reply over a resolve, any of them over a thumbs-up (which is only ever
    ``up``). A resolved thread counts only when the finding's code has not changed (*code_changed* is false): a
    person who fixed the code and resolved the thread has not disagreed with anything. The note is the last thing an
    allowed person said in the thread, whatever the signal.

    On a must-fix (P0) the people who may dismiss one are heard first, so the author's thumbs-down cannot shadow a
    maintainer's reply; when none of them said anything, what the others did is still returned, marked as unable to
    dismiss it, so it is recorded and answered and the P0 stays.
    """

    by_root = {thread.root_id: thread for thread in threads}
    signals: list[Signal] = []
    for finding in findings:
        if not finding.is_open or finding.thread_id <= 0 or finding.category in sup.PROTECTED:
            continue
        thread = by_root.get(finding.thread_id)
        root = next((c for c in thread.comments if c.id == finding.thread_id), None) if thread is not None else None
        if thread is None or root is None:
            continue
        # The account that wrote the finding's comment is the bot's own, whatever its login looks like.
        dismisser = _dismissers(people, root.login)
        signal: Signal | None = None
        if finding.severity == sup.MUST_FIX:
            heard = _feedback(finding, thread, root, voice=dismisser, dismisser=dismisser, code_changed=code_changed)
            signal = heard if heard is not None and heard.kind != "up" else None
        if signal is None:
            signal = _feedback(finding, thread, root, voice=people.may, dismisser=dismisser, code_changed=code_changed)
        if signal is not None:
            signals.append(signal)
    return signals


def _dismissers(people: People, bot_login: str) -> Callable[[str, str], bool]:
    """Who may dismiss a must-fix here: the people who may, except the account that wrote the comment (the bot)."""

    def may(login: str, kind: str = "") -> bool:
        return login.lower() != bot_login.lower() and people.may_dismiss_p0(login, kind)

    return may


def _feedback(
    finding: BotFinding,
    thread: FeedbackThread,
    root: ThreadComment,
    *,
    voice: Callable[[str, str], bool],
    dismisser: Callable[[str, str], bool],
    code_changed: Callable[[BotFinding], bool],
) -> Signal | None:
    """The one signal the people *voice* admits give on *finding*, in the order thumbs-down, reply, resolve, thumbs-up."""

    spoken = [c for c in thread.comments if c.id != root.id and voice(c.login, c.kind)]
    note = sup.fragment(spoken[-1].body, sup.MAX_NOTE) if spoken else ""
    down = next((r.login for r in root.reactions if r.content == THUMBS_DOWN and voice(r.login, "")), "")
    up = next((r.login for r in root.reactions if r.content == THUMBS_UP and voice(r.login, "")), "")
    said = next(((c, classify_reply(c.body)) for c in reversed(spoken) if classify_reply(c.body)), None)
    if down:
        return Signal(finding, "down", down, said[1] if said else "", note, dismisser(down, ""))
    if said is not None:
        by = said[0]
        return Signal(
            finding, "reply", by.login, said[1], sup.fragment(by.body, sup.MAX_NOTE), dismisser(by.login, by.kind)
        )
    if thread.resolved and voice(thread.resolved_by, thread.resolved_by_kind) and not code_changed(finding):
        by_login, by_kind = thread.resolved_by, thread.resolved_by_kind
        return Signal(finding, "resolved", by_login, "", note, dismisser(by_login, by_kind))
    if up:
        return Signal(finding, "up", up)
    return None


_SOURCE_OF: Final[Mapping[str, str]] = {"down": "reaction", "reply": "reply", "resolved": "resolved"}


def suppression_from(signal: Signal, head: str) -> sup.Suppression:
    """The suppression a dismissing signal teaches: this very concern (its fingerprint), on this pull request."""

    finding = signal.finding
    return sup.make_suppression(
        _SOURCE_OF[signal.kind],
        fingerprint=finding.fingerprint,
        # A finding recorded before fingerprints existed is held back by its title instead.
        pattern="" if finding.fingerprint else finding.title,
        paths=() if finding.fingerprint else (finding.path,),
        category=finding.category,
        path=finding.path,
        reason=signal.reason,
        note=signal.note,
        by=signal.by,
        title=finding.title,
        thread_id=finding.thread_id,
        added_in=head,
        dismisses_p0=signal.dismisses_p0,
    )


@dataclass(frozen=True)
class Learning:
    """What a run learned: the findings after suppression, every suppression in force, and which are new."""

    findings: tuple[BotFinding, ...]
    suppressions: tuple[sup.Suppression, ...]
    added: tuple[sup.Suppression, ...] = ()
    changed: bool = False


def learn(
    findings: Sequence[BotFinding],
    existing: Sequence[sup.Suppression],
    signals: Sequence[Signal],
    *,
    commands: Sequence[sup.Suppression] = (),
    head: str,
) -> Learning:
    """Apply what people said: new suppressions from *signals* and *commands*, then every suppression in force.

    A thumbs-up marks its finding ``confirmed`` and changes nothing else. The result is the same whether a
    suppression was just learned or learned runs ago, which is what makes a reaction take effect on the next push
    and keep taking effect on the ones after.
    """

    taught = [suppression_from(signal, head) for signal in signals if signal.kind in _SOURCE_OF]
    confirmed = {signal.finding.id for signal in signals if signal.kind == "up"}
    known = {row.id for row in existing}
    suppressions = sup.merge(existing, [*commands, *taught])
    held = sup.apply(list(findings), suppressions, head)
    held = [f.changed(confirmed=True) if f.id in confirmed and f.is_open and not f.confirmed else f for f in held]
    added = tuple(row for row in suppressions if row.id not in known)
    return Learning(tuple(held), suppressions, added, changed=held != list(findings) or bool(added))


# --------------------------------------------------------------------------- #
# the confirmation in the thread
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Confirmation:
    """A thread that is owed a "learned" reply: the finding that was held back and why."""

    thread_id: int
    finding: BotFinding
    suppression: sup.Suppression


def confirmations(
    findings: Sequence[BotFinding],
    suppressions: Sequence[sup.Suppression],
    comments: Sequence[Remote],
    *,
    quiet: Collection[str] = (),
) -> list[Confirmation]:
    """Held-back findings whose thread has not been told yet, one per thread and suppression.

    "Told" is a reply of the bot's in the thread carrying the suppression's marker, so a re-run, or the next push
    that reports the same concern again, never repeats it. *quiet* are suppressions a person gave by command: the
    reply to the command is their confirmation.
    """

    told = {
        (comment.reply_to, match[1])
        for comment in comments
        if comment.by_bot and comment.reply_to
        for match in _LEARNED.finditer(comment.body)
    }
    by_id = {row.id: row for row in suppressions}
    owed: list[Confirmation] = []
    for finding in findings:
        row = by_id.get(finding.suppressed_by)
        if finding.state != "suppressed" or row is None or finding.thread_id <= 0 or row.id in quiet:
            continue
        if (finding.thread_id, row.id) not in told:
            owed.append(Confirmation(finding.thread_id, finding, row))
    return owed


def post_confirmations(github: BotGitHub, owed: Sequence[Confirmation], slug: str) -> int:
    """Tell each owed thread what was learned, with the rule it could become. Returns how many were posted."""

    posted = 0
    for item in owed:
        rule = rule_yaml([suggestion_for(slug, [item.suppression])])
        github.reply_in_thread(item.thread_id, learned_reply(item.suppression, rule))
        posted += 1
    return posted


@dataclass(frozen=True, slots=True)
class Refusal:
    """A must-fix whose thread is owed an answer: feedback on it was recorded, and it stays.

    ``own``: the feedback came from the pull request's author, so the reply says "your own pull request".
    """

    thread_id: int
    finding: BotFinding
    suppression: sup.Suppression
    own: bool


def refusals(
    findings: Sequence[BotFinding],
    suppressions: Sequence[sup.Suppression],
    comments: Sequence[Remote],
    *,
    author: str,
) -> list[Refusal]:
    """Open must-fix findings that a recorded dismissal is about but may not hold back, whose thread has not been told.

    "Told" is a reply of the bot's in the thread carrying the suppression's ``declined`` marker, so a re-run, or a
    second thumbs-down or reply on the same finding, never repeats it. A command is answered by the reply to the command.
    """

    told = {
        (comment.reply_to, match[1])
        for comment in comments
        if comment.by_bot and comment.reply_to
        for match in _DECLINED.finditer(comment.body)
    }
    owed: list[Refusal] = []
    for finding in findings:
        if not finding.is_open or finding.thread_id <= 0:
            continue
        row = next((row for row in suppressions if row.source != "command" and row.declines(finding)), None)
        if row is not None and (finding.thread_id, row.id) not in told:
            own = bool(author) and row.by.lower() == author.lower()
            owed.append(Refusal(finding.thread_id, finding, row, own))
    return owed


def post_refusals(github: BotGitHub, owed: Sequence[Refusal]) -> int:
    """Tell each owed thread that its must-fix stays, and why. Returns how many were posted."""

    for item in owed:
        github.reply_in_thread(item.thread_id, declined_reply(item.suppression, own=item.own))
    return len(owed)


# --------------------------------------------------------------------------- #
# the rule it could become
# --------------------------------------------------------------------------- #

category_label = sup.category_label


def _where(paths: Sequence[str]) -> str:
    return f" in {', '.join(paths)}" if paths else ""


def _statement(rows: Sequence[sup.Suppression], paths: Sequence[str]) -> str:
    """The rule text for one or more suppressions. Every word a person or a model wrote went through ``fragment``."""

    first = rows[0]
    where = _where(paths)
    if first.source == "command":
        what = f"{category_label(first.category)} findings" if first.category else f'findings about "{first.pattern}"'
        text = f"Do not report {what}{where}. A reviewer asked for them to be ignored."
    elif len(rows) > 1:
        label = category_label(first.category) if first.category else "such"
        text = f"Do not report {label} findings{where}: reviewers dismissed {len(rows)} of them in review."
    else:
        text = f'Do not report findings like "{first.title}"{where}: a reviewer dismissed it as {sup.REASON_WORDS[first.reason]}.'
    note = next((row.note for row in rows if row.note), "")
    if note and first.source != "command":
        text += f' Reviewer\'s words: "{note}".'
    return text[:3_900]


def suggestion_for(slug: str, rows: Sequence[sup.Suppression]) -> RuleSuggestion:
    """A rule suggestion (``rule_suggestions.RuleSuggestion``) for suppressions that say the same thing."""

    first = rows[0]
    paths = path_family([row.path for row in rows if row.path]) if first.source != "command" else first.paths
    statement = _statement(rows, paths)
    return RuleSuggestion(
        id=suggestion_identity(repository_scope=slug, normalized_feedback=statement.lower(), path_scope=paths),
        repository_scope=slug,
        candidate_statement=statement,
        path_scope=tuple(paths),
        supporting_feedback_refs=tuple(f"suppression:{row.id}" for row in rows),
    )


def suggest(
    slug: str, suppressions: Sequence[sup.Suppression], findings: Sequence[BotFinding] = ()
) -> tuple[RuleSuggestion, ...]:
    """The rules worth offering for what was dismissed on this pull request.

    A command is a rule on its own. Dismissals are grouped by category and directory, and a group is offered when
    a person said why in words, or when it repeats (two or more): one thumbs-down is not yet a pattern. A dismissal of
    one finding that its giver could not make, because that finding is an open must-fix (it is in *findings*), is not
    a reviewer's judgment and is not offered; a command about a whole category still is, for a maintainer to weigh.
    """

    groups: dict[tuple[str, ...], list[sup.Suppression]] = {}
    for row in suppressions:
        if row.fingerprint and any(row.declines(finding) for finding in findings if finding.is_open):
            continue
        directory = row.path.rpartition("/")[0] if row.path else ""
        key = (row.id,) if row.source == "command" else ("learned", row.category, directory)
        groups.setdefault(key, []).append(row)
    offered = [rows for rows in groups.values() if any(row.explicit for row in rows) or len(rows) >= 2]
    return tuple(suggestion_for(slug, rows) for rows in offered)


# --------------------------------------------------------------------------- #
# `@lemoncrow ignore ...`
# --------------------------------------------------------------------------- #

IGNORE_USAGE: Final[str] = (
    "Say what to ignore: a category (`correctness`, `security`, `api`, `concurrency`, `persistence`, `performance`, "
    "`verification`) or words from a finding's title, optionally followed by `in <path glob>`, for example "
    '`ignore verification in tests/**` or `ignore "missing null check" in src/legacy/**`. In a finding\'s thread, '
    "`ignore` alone dismisses that finding."
)
_THIS: Final = re.compile(r"^(?:this|it|this one|this finding|this thread|these)?\W*$", re.IGNORECASE)
_FILLER_HEAD: Final = re.compile(
    r"^(?:all|any|the|every|category|findings? about|findings? of|warnings? about)\s+", re.IGNORECASE
)
_FILLER_TAIL: Final = re.compile(r"\s+(?:findings?|warnings?|comments?|issues?|suggestions?|errors?)$", re.IGNORECASE)


@dataclass(frozen=True)
class Ignore:
    """What an ``ignore`` command asks: a suppression, or why there is none (a clarification to post)."""

    suppression: sup.Suppression | None = None
    problem: str = ""


def parse_ignore(
    text: str,
    *,
    finding: BotFinding | None,
    by: str,
    thread_id: int,
    head: str,
    dismisses_p0: bool = False,
    author: str = "",
) -> Ignore:
    """Read ``<category or words> [in <path glob>]`` (the text after ``ignore``).

    *dismisses_p0*: whether *by* may dismiss a must-fix (:func:`may_dismiss_must_fix`). When not, ``ignore`` alone in
    the thread of an open P0 is refused, and a category or words that cover a P0 are recorded without covering it: the
    suppression holds back the P1 and P2 findings it names and leaves a P0 open. *author* is the pull request's author,
    only to word the refusal.
    """

    body = text.strip()
    if re.fullmatch(r"in\s+\S+", body, re.IGNORECASE):  # a scope with nothing to scope
        return Ignore(problem=IGNORE_USAGE)
    if _THIS.match(body):
        if finding is None:
            return Ignore(problem="Which finding? " + IGNORE_USAGE)
        if finding.category in sup.PROTECTED:
            return Ignore(problem=_protected())
        if finding.is_open and finding.severity == sup.MUST_FIX and not dismisses_p0:
            return Ignore(problem=must_fix_notice(own=bool(author) and by.lower() == author.lower()))
        return Ignore(suppression=_for_finding(finding, by, head, dismisses_p0))
    what, _, tail = body.rpartition(" in ")
    globs = tuple(sup.safe_glob(token) for token in re.split(r"[,\s]+", tail.strip()) if token)
    scoped = bool(what.strip()) and bool(globs) and all(globs) and all(re.search(r"[/*.]", g) for g in globs)
    if not scoped:
        what, globs = body, ()
    what = _FILLER_TAIL.sub("", _FILLER_HEAD.sub("", what.strip().strip("`'\" "))).strip().strip("`'\" ")
    if len(re.findall(r"[A-Za-z0-9]", what)) < 3:
        return Ignore(problem=IGNORE_USAGE)
    category = sup.category_named(what)
    if category in sup.PROTECTED:
        return Ignore(problem=_protected())
    return Ignore(
        suppression=sup.make_suppression(
            "command",
            category=category,
            pattern="" if category else what,
            paths=globs,
            by=by,
            thread_id=thread_id,
            added_in=head,
            dismisses_p0=dismisses_p0,
        )
    )


def _for_finding(finding: BotFinding, by: str, head: str, dismisses_p0: bool) -> sup.Suppression:
    """ "ignore" in a finding's thread: that very concern, said in words."""

    return replace(suppression_from(Signal(finding, "down", by, dismisses_p0=dismisses_p0), head), source="command")


def _protected() -> str:
    return (
        "That finding comes from a rule in `.lemoncrow/review.yaml`, which the trusted base owns: a comment on a pull "
        "request cannot switch it off. Change or remove the rule in the repository instead."
    )


__all__ = [
    "IGNORE_USAGE",
    "THUMBS_DOWN",
    "THUMBS_UP",
    "Confirmation",
    "Ignore",
    "Learning",
    "People",
    "Refusal",
    "Signal",
    "category_label",
    "classify_reply",
    "confirmations",
    "is_bot_login",
    "learn",
    "may_dismiss_must_fix",
    "parse_ignore",
    "post_confirmations",
    "post_refusals",
    "read_signals",
    "refusals",
    "suggest",
    "suggestion_for",
    "suppression_from",
]
