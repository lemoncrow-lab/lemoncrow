"""The bot's GitLab calls: one summary note, one discussion per finding, replies and resolves, a commit status.

:class:`BotGitLab` is :class:`~lemoncrow.pro.capabilities.review.pr_bot.github.BotGitHub` for a GitLab merge request,
over REST API v4 (:class:`~lemoncrow.pro.capabilities.review.sources.gitlab.GitLabApi`); it satisfies
:class:`~lemoncrow.pro.capabilities.review.pr_bot.host.ReviewHost` and trades in the same rows (``Remote``,
``PullFacts``, ``InlineOutcome`` ...), so the runner's logic runs unchanged on top of it. Where GitLab differs:

* **Trust.** A note is the bot's only when its ``author.id`` is the id ``GET /user`` reports for the bot's token. Such a
  note is given ``author_type == "Bot"``, which is what every marker check of the runner reads; a note anyone else
  wrote, marker or not, is never the bot's.
* **No batch review.** Each finding is one ``POST .../discussions`` with a text ``position`` on GitLab's own diff
  (``diff_refs`` shas, both paths, ``new_line``, and ``old_line`` too when the line is unchanged context, which GitLab
  requires). A finding GitLab refuses to place (HTTP 400) is ``unplaced`` and listed in the summary; a run cut short
  after some were posted is adopted by marker on the next run, as on GitHub.
* **Multi-line findings** are anchored at their last line; the comment says which lines it covers, and a suggestion
  uses GitLab's ``suggestion:-N+0`` fence so it replaces exactly those lines (:func:`gitlab_suggestion_fence`).
* **Statuses only.** ``LemonCrow Review`` is an external commit status in the merge request's pipeline; ``error`` and
  ``failure`` are GitLab's ``failed``.
* **Proof of work** is the commit's statuses (GitLab reports a CI job as one), this job's own left out.
* **Not yet:** learning from reactions and resolves (:meth:`BotGitLab.feedback_threads` is empty, so nothing is
  learned) and chat (GitLab CI has no comment event).

:func:`decide_gitlab` is the GitLab gate, the counterpart of ``ci_env.decide``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from lemoncrow.pro.capabilities.review.github_placement import index_files, unplaceable_reason
from lemoncrow.pro.capabilities.review.pr_bot.ci_env import Gate, resolve_model_route
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.github import (
    BOT_TYPE,
    BotGitHub,
    FeedbackThread,
    InlineOutcome,
    PullFacts,
    ReactionTarget,
    Remote,
    ThreadOutcome,
    adopt_existing,
    is_denied,
)
from lemoncrow.pro.capabilities.review.pr_bot.proof_of_work import OUTCOMES, OWN_CONTEXTS, CiCheck, CiReport, refused
from lemoncrow.pro.capabilities.review.pr_bot.render import STATUS_CONTEXT, inline_comment, reply_text
from lemoncrow.pro.capabilities.review.pr_bot.state import SUMMARY_MARKER, ReviewState, parse_state
from lemoncrow.pro.capabilities.review.sources.gitlab import TOKEN_ENV, GitLabApi, GitLabApiError, MergeRequestRef

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.pr_bot.host import ReviewHost

_RESOLVED_MARKER: Final = re.compile(r"<!-- lemoncrow:resolved (\S+) -->")
#: GitLab's answer when a status is posted in the state it already has (``Cannot transition status via :enqueue from
#: :pending``): the status already says what was asked.
_SAME_STATE: Final = re.compile(r"cannot transition status via :\w+ from :(\w+)", re.IGNORECASE)
#: The ``state`` the runner asks for -> GitLab's commit status state.
STATUS_STATES: Final[Mapping[str, str]] = {
    "error": "failed",
    "failure": "failed",
    "pending": "pending",
    "running": "running",
    "success": "success",
}
#: GitLab caps a commit status description at 255 characters.
_DESCRIPTION_LIMIT: Final[int] = 255
#: The Developer role: what posting notes, resolving discussions and setting statuses needs.
DEVELOPER: Final[int] = 30
_ROLES: Final[Mapping[int, str]] = {
    0: "no access",
    5: "Minimal Access",
    10: "Guest",
    15: "Planner",
    20: "Reporter",
    30: "Developer",
    40: "Maintainer",
    50: "Owner",
}
#: The reaction names the runner uses (GitHub's) -> GitLab award emoji names.
EMOJI: Final[Mapping[str, str]] = {
    "+1": "thumbsup",
    "-1": "thumbsdown",
    "eyes": "eyes",
    "confused": "confused",
    "laugh": "laughing",
    "heart": "heart",
    "hooray": "tada",
    "rocket": "rocket",
}
_RUNNING: Final[frozenset[str]] = frozenset(
    {"created", "pending", "running", "preparing", "scheduled", "waiting_for_resource", "waiting_for_callback"}
)
#: What the summary says when GitLab refuses the commit statuses.
NOT_READABLE: Final[str] = "CI results not readable: the GitLab token needs the Reporter role and the read_api scope"


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# --------------------------------------------------------------------------- #
# Diff lines and suggestions
# --------------------------------------------------------------------------- #

_HUNK: Final = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _pair_for(diff: str, line: int, *, side: str) -> int | None:
    """The other side's number of *line* (on *side*, ``new`` or ``old``) when it is an unchanged context line of *diff*.

    ``None`` when the line was added (new side) or removed (old side), or is not in any hunk.
    """

    old = new = 0
    in_hunk = False
    for row in diff.splitlines():
        header = _HUNK.match(row)
        if header is not None:
            old, new, in_hunk = int(header[1]), int(header[2]), True
            continue
        if not in_hunk or row.startswith("\\"):
            continue  # text before the first hunk, or "\ No newline at end of file"
        if row.startswith("+"):
            if side == "new" and new == line:
                return None
            new += 1
        elif row.startswith("-"):
            if side == "old" and old == line:
                return None
            old += 1
        else:
            if (new if side == "new" else old) == line:
                return old if side == "new" else new
            old += 1
            new += 1
    return None


def _old_line_for(diff: str, new_line: int) -> int | None:
    """The old-side number of new-side *new_line* when it is a context line of *diff*; ``None`` for an added line."""

    return _pair_for(diff, new_line, side="new")


def _new_line_for(diff: str, old_line: int) -> int | None:
    """The new-side number of old-side *old_line* when it is a context line of *diff*; ``None`` for a removed line."""

    return _pair_for(diff, old_line, side="old")


def gitlab_suggestion_fence(finding: BotFinding) -> str:
    """The info string of a GitLab suggestion fence for *finding*: ``suggestion:-N+0``.

    The comment is anchored at the last line of the finding's span (:meth:`BotFinding.span`), so the suggestion reaches
    ``N = end - start`` lines above it and none below: exactly the lines a GitHub multi-line suggestion replaces.
    """

    start, end = finding.span
    return f"suggestion:-{max(end - start, 0)}+0"


def _inline_body(finding: BotFinding) -> str:
    """*finding* as a GitLab discussion body: :func:`render.inline_comment` with GitLab's suggestion fence
    (:func:`gitlab_suggestion_fence`) and the lines it covers."""

    body = inline_comment(finding, suggestion_fence=gitlab_suggestion_fence(finding))
    start, end = finding.span
    if end > start:
        title, _, rest = body.partition("\n")
        body = f"{title}\n\n<sub>Lines {start}-{end}</sub>\n{rest}"
    return body


@dataclass(frozen=True, slots=True)
class _MrFile:
    """One file of the merge request's diff, as ``GET .../diffs`` reports it."""

    path: str
    previous_path: str
    old_path: str
    patch: str | None


# --------------------------------------------------------------------------- #
# The bot
# --------------------------------------------------------------------------- #


class BotGitLab:
    """GitLab, as the bot sees one merge request."""

    def __init__(self, pr: MergeRequestRef, api: GitLabApi, *, pipeline_id: str = "") -> None:
        self.pr = pr
        self.api = api
        #: ``CI_PIPELINE_ID``: the status goes into this pipeline instead of one GitLab would pick or create.
        self.pipeline_id = pipeline_id.strip()
        self._me: tuple[int, str] | None = None
        #: Note id -> the id of the discussion it is in; filled by :meth:`review_comments` and by posting.
        self._discussion_of: dict[int, str] = {}
        self._resolved: dict[str, bool] = {}

    # ----- identity ----------------------------------------------------- #

    def bot_user(self) -> tuple[int, str]:
        """``(id, username)`` of the token's user (``GET /user``), read once. Raises when GitLab does not say."""

        if self._me is None:
            row = _mapping(self.api.get("user"))
            identity = _positive(row.get("id"))
            if not identity:
                raise GitLabApiError("GitLab did not report the token's user (GET /user)")
            self._me = (identity, str(row.get("username") or ""))
        return self._me

    def _remote(self, note: Mapping[str, Any], *, reply_to: int = 0) -> Remote | None:
        identity = _positive(note.get("id"))
        if not identity:
            return None
        author = _mapping(note.get("author"))
        mine = _positive(author.get("id")) == self.bot_user()[0]
        position = _mapping(note.get("position"))
        line = _positive(position.get("new_line")) or _positive(position.get("old_line"))
        return Remote(
            id=identity,
            body=str(note.get("body") or ""),
            author=str(author.get("username") or ""),
            author_type=BOT_TYPE if mine else "User",
            url=self.comment_url(identity),
            reply_to=reply_to,
            path=str(position.get("new_path") or position.get("old_path") or ""),
            line=line,
            start_line=line,
        )

    # ----- reads -------------------------------------------------------- #

    def _merge_request(self) -> dict[str, Any]:
        row = self.api.get(self.pr.api_path)
        if not isinstance(row, dict):
            raise GitLabApiError(f"GitLab returned an unexpected shape for {self.pr.url}")
        return row

    def pull_facts(self) -> PullFacts:
        row = self._merge_request()
        state = str(row.get("state") or "")
        source, target = row.get("source_project_id"), row.get("target_project_id")
        return PullFacts(
            number=self.pr.number,
            state={"opened": "open", "merged": "closed"}.get(state, state),
            draft=row.get("draft") is True or row.get("work_in_progress") is True,
            title=str(row.get("title") or ""),
            author=str(_mapping(row.get("author")).get("username") or ""),
            head_sha=str(_mapping(row.get("diff_refs")).get("head_sha") or row.get("sha") or ""),
            # The fork test compares the two: a merge request whose source project is not its target is a fork's.
            head_repo=str(source) if _positive(source) else "",
            base_repo=str(target) if _positive(target) else "",
            html_url=str(row.get("web_url") or self.pr.url),
            body=str(row.get("description") or ""),
            merged=state == "merged",
        )

    def issue(self, number: int) -> dict[str, Any]:
        """One issue of this project by iid; ``body`` is its description, as on GitHub."""

        row = self.api.get(f"{self.pr.project_path}/issues/{int(number)}")
        if not isinstance(row, dict):
            raise GitLabApiError(f"GitLab returned an unexpected shape for issue #{number}")
        return {**row, "body": str(row.get("description") or "")}

    def ci_results(self, sha: str, *, exclude_run: str = "") -> CiReport:
        """What CI reported on *sha*: its commit statuses, one per job and external status. Never raises.

        *exclude_run* is this job's id (``CI_JOB_ID``): GitLab reports the job doing the review as a status on the head
        too, and it is not proof of anything; neither is ``LemonCrow Review``. Only the latest status of each name is
        kept (a job retried, or run again by a second pipeline on the same commit, is its newest result).
        """

        try:
            rows = self.api.paginate(f"{self.pr.project_path}/repository/commits/{sha}/statuses")
        except GitLabApiError as exc:
            reason = NOT_READABLE if refused(str(exc)) else "CI results could not be read: GitLab did not answer"
            return CiReport(sha=sha, readable=False, reason=reason)
        own = str(exclude_run).strip()
        latest: dict[str, tuple[int, CiCheck]] = {}
        for row in rows:
            if str(row.get("sha") or sha) != sha:
                continue  # another commit's status: never shown as this one's
            name = str(row.get("name") or "") or "unnamed status"
            if name in OWN_CONTEXTS:
                continue
            identity = _positive(row.get("id"))
            target = str(row.get("target_url") or "").rstrip("/")
            if own and (str(identity) == own or target.endswith(f"/-/jobs/{own}")):
                continue
            check = CiCheck(name, *_ci_outcome(str(row.get("status") or ""), row.get("allow_failure") is True))
            if name not in latest or identity > latest[name][0]:
                latest[name] = (identity, check)
        checks = sorted(
            (check for _, check in latest.values()), key=lambda c: (OUTCOMES.index(c.outcome), c.name.lower())
        )
        return CiReport(sha=sha, checks=tuple(checks))

    def commits(self) -> list[tuple[str, str]] | None:
        """``(sha, message)`` of each of the merge request's commits, or ``None`` when they cannot be read."""

        try:
            rows = self.api.paginate(f"{self.pr.api_path}/commits")
        except GitLabApiError:
            return None
        return [(str(row.get("id") or ""), str(row.get("message") or "")) for row in rows]

    def approvals(self, *, trusted: bool = False) -> list[tuple[str, str]] | None:
        """``None``: GitLab's approvals name no commit, so none can be said to be on an older head. A project that wants
        them gone on a push turns on GitLab's own "Remove all approvals when commits are added"."""

        return None

    def commit_messages(self) -> list[str] | None:
        """The messages of the merge request's commits, or ``None`` when they cannot be read."""

        commits = self.commits()
        return None if commits is None else [message for _, message in commits]

    def _notes(self) -> list[Remote]:
        rows = self.api.paginate(f"{self.pr.api_path}/notes", {"sort": "asc", "order_by": "created_at"})
        return [
            remote
            for row in rows
            if row.get("system") is not True and row.get("type") != "DiffNote"
            for remote in [self._remote(row)]
            if remote is not None
        ]

    def summary(self) -> tuple[Remote | None, ReviewState | None]:
        """The bot's summary note (the oldest, if a race made two) and the state it carries."""

        found = [note for note in self._notes() if note.by_bot and SUMMARY_MARKER in note.body]
        if not found:
            return None, None
        first = min(found, key=lambda note: note.id)
        return first, parse_state(first.body)

    def issue_comments(self) -> list[Remote]:
        """The merge request's overview notes (not the diff ones, not GitLab's system notes), oldest first."""

        return sorted(self._notes(), key=lambda note: note.id)

    def review_comments(self) -> list[Remote]:
        """Every note of every diff discussion; a reply's ``reply_to`` is its discussion's first note."""

        found: list[Remote] = []
        for discussion in self.api.paginate(f"{self.pr.api_path}/discussions"):
            did = str(discussion.get("id") or "")
            notes = [_mapping(n) for n in discussion.get("notes") or () if _mapping(n).get("system") is not True]
            if not did or not notes:
                continue
            root = _positive(notes[0].get("id"))
            self._resolved[did] = notes[0].get("resolved") is True
            for note in notes:
                identity = _positive(note.get("id"))
                if not identity:
                    continue
                self._discussion_of[identity] = did
                if note.get("type") != "DiffNote":
                    continue
                remote = self._remote(note, reply_to=0 if identity == root else root)
                if remote is not None:
                    found.append(remote)
        return found

    def feedback_threads(self) -> list[FeedbackThread]:
        """Nothing yet: learning from reactions and resolves is GitHub-only, so a GitLab review learns nothing."""

        return []

    def permission_of(self, login: str) -> str:
        """``""``: no GitLab user is looked up (fails closed, like an unknown login on GitHub)."""

        return ""

    def probe_comment_access(self) -> None:
        """Raise a denial when the token's role cannot post notes or set statuses (below Developer); change nothing.

        Reads ``GET /user`` (who the bot is) and the project's ``permissions``. A project that reports no permissions
        block says nothing either way, and is let through, as any answer but a refusal is on GitHub.
        """

        self.bot_user()
        project = _mapping(self.api.get(self.pr.project_path))
        if "permissions" not in project:
            return
        permissions = _mapping(project.get("permissions"))
        level = max(
            _positive(_mapping(permissions.get("project_access")).get("access_level")),
            _positive(_mapping(permissions.get("group_access")).get("access_level")),
        )
        if level < DEVELOPER:
            raise GitLabApiError(
                f"GitLab GET {self.pr.project_path} (HTTP 403): resource not accessible by integration: the token's "
                f"role on {self.pr.slug} is {_ROLES.get(level, str(level))}; posting notes and statuses needs "
                "Developer or above",
                status=403,
            )

    # ----- the summary note --------------------------------------------- #

    def _written(self, row: object, body: str, what: str, *, reply_to: int = 0) -> Remote:
        written = self._remote(_mapping(row), reply_to=reply_to)
        if written is None:
            raise GitLabApiError(f"GitLab did not return the {what} on {self.pr.url}")
        return replace(written, body=body)

    def write_summary(self, body: str, existing: Remote | None) -> Remote:
        """Create the summary note, or update it in place -- and leave it alone when nothing changed."""

        if existing is not None:
            if existing.body == body:
                return existing
            row = self.api.put(f"{self.pr.api_path}/notes/{existing.id}", {"body": body})
        else:
            row = self.api.post(f"{self.pr.api_path}/notes", {"body": body})
        return self._written(row, body, "summary note")

    def post_comment(self, body: str) -> Remote:
        """A new overview note on the merge request."""

        return self._written(self.api.post(f"{self.pr.api_path}/notes", {"body": body}), body, "note")

    # ----- discussions -------------------------------------------------- #

    def comment_url(self, comment_id: int) -> str:
        return f"{self.pr.url}#note_{comment_id}"

    def _discussion(self, root_id: int) -> str:
        if root_id not in self._discussion_of:
            self.review_comments()
        did = self._discussion_of.get(root_id)
        if did is None:
            raise GitLabApiError(f"no discussion on {self.pr.url} holds note {root_id}")
        return did

    def reply_in_thread(self, root_id: int, body: str) -> Remote:
        """Reply in the discussion whose first note is *root_id*."""

        did = self._discussion(root_id)
        row = self.api.post(f"{self.pr.api_path}/discussions/{did}/notes", {"body": body})
        written = self._written(row, body, f"reply on discussion {did}", reply_to=root_id)
        self._discussion_of[written.id] = did
        return written

    def adopt_existing(
        self, findings: Sequence[BotFinding], comments: Sequence[Remote], *, tracked: Collection[str] = ()
    ) -> dict[str, Remote]:
        """Findings that already have a discussion of the bot's, found by marker (``github.adopt_existing``)."""

        return adopt_existing(findings, comments, tracked=tracked)

    def _diff_files(self) -> dict[str, _MrFile]:
        rows = self.api.paginate(f"{self.pr.api_path}/diffs")
        files = []
        for row in rows:
            new_path, old_path = str(row.get("new_path") or ""), str(row.get("old_path") or "")
            diff = str(row.get("diff") or "")
            files.append(
                _MrFile(
                    path=new_path or old_path,
                    previous_path=old_path if old_path and old_path != new_path else "",
                    old_path=old_path or new_path,
                    patch=diff if "@@" in diff else None,
                )
            )
        return index_files(files)

    @staticmethod
    def _position(finding: BotFinding, refs: Mapping[str, Any], file: _MrFile) -> dict[str, Any]:
        _, end = finding.span
        position: dict[str, Any] = {
            "position_type": "text",
            "base_sha": str(refs.get("base_sha") or ""),
            "start_sha": str(refs.get("start_sha") or ""),
            "head_sha": str(refs.get("head_sha") or ""),
            "old_path": file.old_path,
            "new_path": file.path,
        }
        patch = file.patch or ""
        if finding.side == "old":
            position["old_line"] = end
            paired = _new_line_for(patch, end)
            if paired is not None:
                position["new_line"] = paired
        else:
            position["new_line"] = end
            paired = _old_line_for(patch, end)
            if paired is not None:
                position["old_line"] = paired
        return position

    def post_inline(
        self,
        findings: Sequence[BotFinding],
        head_sha: str,
        *,
        dry_run: bool = False,
        tracked: Collection[str] = (),
    ) -> InlineOutcome:
        """Post each finding as its own discussion on its diff line.

        Findings GitLab cannot place are ``unplaced`` (the summary lists them); a merge request that moved on is
        reported, not posted onto code it no longer holds. A refusal of the token, a rate limit that outlasted the
        backoff or a server error stops the posting and raises: what was posted is adopted by marker next run.
        """

        if not findings:
            return InlineOutcome({}, ())
        existing = self.adopt_existing(findings, self.review_comments(), tracked=tracked)
        adopted = {fid: (comment.id, comment.url or self.comment_url(comment.id)) for fid, comment in existing.items()}
        as_found = {
            finding.id: finding.changed(
                line=existing[finding.id].start_line or finding.line,
                end_line=existing[finding.id].line or finding.end_line,
                suggestion="",
                suggestion_start=0,
                suggestion_end=0,
            )
            for finding in findings
            if finding.id in existing and existing[finding.id].line > 0
        }
        findings = [finding for finding in findings if finding.id not in existing]
        if not findings:
            return InlineOutcome(adopted, (), sent=as_found, adopted=tuple(adopted))
        refs = _mapping(self._merge_request().get("diff_refs"))
        files = self._diff_files()
        placed: list[tuple[BotFinding, _MrFile]] = []
        for finding in findings:
            if finding.line <= 0:
                continue
            finding = BotGitHub._placeable_anchor(BotGitHub._placeable_suggestion(finding, files), files)
            start, end = finding.span
            file = files.get(finding.path)
            if file is None or unplaceable_reason(
                patch=file.patch, anchor_side=finding.side, start_line=start, end_line=end
            ):
                continue
            placed.append((finding, file))
        placed_ids = {finding.id for finding, _ in placed}
        unplaced = [finding.id for finding in findings if finding.id not in placed_ids]

        def outcome(skipped: str = "", posted: Mapping[str, tuple[int, str]] | None = None) -> InlineOutcome:
            sent = {finding.id: finding for finding, _ in placed if posted is not None and finding.id in posted}
            return InlineOutcome(
                {**adopted, **(posted or {})},
                tuple(unplaced),
                skipped_reason=skipped,
                sent={**as_found, **sent},
                adopted=tuple(adopted),
            )

        if not placed or dry_run:
            return outcome("dry run" if dry_run and placed else "")
        live = str(refs.get("head_sha") or "")
        if live and live != head_sha:
            return outcome(f"the merge request moved to {live[:7]}; the next run reviews it")
        posted: dict[str, tuple[int, str]] = {}
        for finding, file in placed:
            try:
                row = _mapping(
                    self.api.post(
                        f"{self.pr.api_path}/discussions",
                        {"body": _inline_body(finding), "position": self._position(finding, refs, file)},
                    )
                )
            except GitLabApiError as exc:
                if exc.status != 400 or is_denied(exc):
                    raise
                unplaced.append(finding.id)  # GitLab would not put it on that line: the summary lists it
                continue
            notes = [_mapping(n) for n in row.get("notes") or ()]
            note_id = _positive(notes[0].get("id")) if notes else 0
            if not note_id:
                unplaced.append(finding.id)
                continue
            self._discussion_of[note_id] = str(row.get("id") or "")
            posted[finding.id] = (note_id, self.comment_url(note_id))
        return outcome(posted=posted)

    def reply_and_resolve(self, findings: Sequence[BotFinding], comments: Sequence[Remote]) -> ThreadOutcome:
        """Reply once on the discussion of each closed finding, then resolve it (best effort).

        A finding that is only *no longer reported* gets its reply and keeps its discussion open, as on GitHub.
        """

        already = {
            match[1]
            for comment in comments
            if comment.by_bot
            for match in [_RESOLVED_MARKER.search(comment.body)]
            if match is not None
        }
        replied = resolved = 0
        notes: list[str] = []
        for finding in findings:
            if finding.thread_id <= 0:
                continue
            try:
                did = self._discussion(finding.thread_id)
            except GitLabApiError as exc:
                notes.append(f"could not find the discussion for {finding.id}: {str(exc)[:160]}")
                continue
            if finding.id not in already:
                self.api.post(f"{self.pr.api_path}/discussions/{did}/notes", {"body": reply_text(finding)})
                replied += 1
            if finding.state == "unreported" or self._resolved.get(did):
                continue
            try:
                self.api.put(f"{self.pr.api_path}/discussions/{did}", params={"resolved": "true"})
                self._resolved[did] = True
                resolved += 1
            except GitLabApiError as exc:
                notes.append(f"could not resolve the discussion for {finding.id}: {str(exc)[:160]}")
        return ThreadOutcome(replied, resolved, tuple(notes))

    # ----- reactions ---------------------------------------------------- #

    def _awards_path(self, target: ReactionTarget) -> str:
        if target.kind == "pull":
            return f"{self.pr.api_path}/award_emoji"
        return f"{self.pr.api_path}/notes/{int(target.id)}/award_emoji"

    def add_reaction(self, target: ReactionTarget, content: str) -> tuple[int, str]:
        """Award *content* (GitHub's name: ``eyes``, ``+1``, ``confused`` ...) on *target*: ``(its id, who gave it)``.

        GitLab refuses a second identical award; the one already there is returned, so this may be called again.
        """

        name = EMOJI.get(content, content)
        try:
            row = _mapping(self.api.post(self._awards_path(target), {"name": name}))
        except GitLabApiError as exc:
            if exc.status not in {400, 404} or "already" not in str(exc).lower():
                raise
            login = self.bot_user()[1]
            ids = self.reactions_by(target, content, login)
            if not ids:
                raise
            return ids[0], login
        return _positive(row.get("id")), str(_mapping(row.get("user")).get("username") or "")

    def remove_reaction(self, target: ReactionTarget, reaction_id: int) -> None:
        self.api.delete(f"{self._awards_path(target)}/{int(reaction_id)}")

    def reactions_by(self, target: ReactionTarget, content: str, login: str) -> list[int]:
        """Ids of the *content* awards on *target* that *login* gave."""

        name = EMOJI.get(content, content)
        return [
            _positive(row.get("id"))
            for row in self.api.paginate(self._awards_path(target))
            if row.get("name") == name
            and _mapping(row.get("user")).get("username") == login
            and _positive(row.get("id"))
        ]

    # ----- the status --------------------------------------------------- #

    def set_status(
        self, sha: str, state: str, description: str, target_url: str = "", *, context: str = STATUS_CONTEXT
    ) -> None:
        """The ``LemonCrow Review`` commit status (or *context*) on the reviewed head, in this job's pipeline when known."""

        wanted = STATUS_STATES.get(state, "failed")
        body: dict[str, Any] = {
            "state": wanted,
            "name": context,
            "description": description[:_DESCRIPTION_LIMIT],
        }
        if target_url:
            body["target_url"] = target_url
        if self.pipeline_id.isdigit():
            body["pipeline_id"] = int(self.pipeline_id)
        try:
            self.api.post(f"{self.pr.project_path}/statuses/{sha}", body)
        except GitLabApiError as exc:
            same = _SAME_STATE.search(str(exc))
            if exc.status == 400 and same is not None and same[1].lower() == wanted:
                return  # it already says that
            raise


def _ci_outcome(status: str, allowed_to_fail: bool) -> tuple[str, str]:
    """``proof_of_work``'s outcome (and its detail) for a GitLab commit status."""

    if status == "success":
        return "passed", ""
    if status == "failed":
        return ("did_not_pass", "failed, allowed to fail") if allowed_to_fail else ("failed", "")
    if status in _RUNNING:
        return "running", ""
    return "did_not_pass", status or "no status"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

#: What a person does about a missing model key on GitLab.
NO_KEY_FIX: Final[str] = (
    "Add your model key as a masked CI/CD variable (Settings > CI/CD > Variables): ANTHROPIC_API_KEY or OPENAI_API_KEY."
)
#: The ``error`` status a merge request gets when the job has no model key.
NO_KEY_STATUS: Final[str] = "No model key: add an ANTHROPIC_API_KEY or OPENAI_API_KEY CI/CD variable"
NO_TOKEN_FIX: Final[str] = (
    f"No {TOKEN_ENV}: GitLab's job token cannot post notes or statuses. Create a project access token (role "
    f"Developer, scope api) and add it as the masked CI/CD variable {TOKEN_ENV} (docs/setup/gitlab-ci.md, 'Token')."
)


def decide_gitlab(
    env: Mapping[str, str],
    *,
    review_drafts: bool = False,
    provider: str = "auto",
    require_credentials: bool = True,
) -> Gate:
    """Whether this GitLab CI job should review a merge request, from its environment alone.

    In order: only a merge request pipeline is reviewed; a fork's merge request is skipped when its pipeline runs in the
    fork (no variables of the parent, so no token and no key) and refused when it runs in the parent project (the
    fork's own ``.gitlab-ci.yml`` with the parent's secrets: GitLab's ``pull_request_target``); a draft is skipped
    unless *review_drafts*; then the bot's token and the model key must be there.
    """

    def var(name: str) -> str:
        return str(env.get(name) or "").strip()

    source = var("CI_PIPELINE_SOURCE")
    if source != "merge_request_event" or not var("CI_MERGE_REQUEST_IID").isdigit():
        return Gate(
            False,
            "LemonCrow Review runs in merge request pipelines (CI_PIPELINE_SOURCE=merge_request_event); "
            f"this pipeline's source is {source or 'unknown'}, so there is nothing to review.",
            "warning",
            sha=var("CI_COMMIT_SHA"),
        )
    from_project, into_project = var("CI_MERGE_REQUEST_SOURCE_PROJECT_ID"), var("CI_MERGE_REQUEST_PROJECT_ID")
    if not from_project or from_project != into_project:
        if var("CI_PROJECT_ID") and var("CI_PROJECT_ID") == into_project:
            return Gate(
                False,
                "LemonCrow Review does not run a fork's merge request in the parent project's pipeline: that pipeline "
                "runs the fork's own .gitlab-ci.yml with the parent's CI/CD variables. Run it in the fork's pipeline "
                "(it is skipped there) and review the change by hand.",
                "error",
                1,
            )
        return Gate(
            False,
            "Merge request from a fork: its pipeline runs in the fork, without this project's CI/CD variables, so "
            "there is no token to comment with and no model key. Skipping.",
            skip="merge request from a fork",
            writable=False,
        )
    if var("CI_MERGE_REQUEST_DRAFT").lower() == "true" and not review_drafts:
        return Gate(
            False, "Draft merge request: skipped. Pass --review-drafts to review drafts.", skip="draft merge request"
        )
    if not var(TOKEN_ENV):
        return Gate(False, NO_TOKEN_FIX, "error", 1)
    if require_credentials and resolve_model_route(env, provider) is None:
        return Gate(
            False,
            f"No model key. {NO_KEY_FIX} (Or set LEMONCROW_LLM_BACKEND with its provider variables.)",
            "error",
            1,
            error_status=NO_KEY_STATUS,
        )
    return Gate(True)


if TYPE_CHECKING:

    def _is_a_review_host(host: BotGitLab) -> ReviewHost:
        return host


__all__ = [
    "DEVELOPER",
    "EMOJI",
    "NOT_READABLE",
    "NO_KEY_FIX",
    "NO_KEY_STATUS",
    "NO_TOKEN_FIX",
    "STATUS_STATES",
    "BotGitLab",
    "decide_gitlab",
    "gitlab_suggestion_fence",
]
