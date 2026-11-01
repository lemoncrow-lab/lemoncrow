"""The code host a pull request bot run talks to, as the runner sees it: GitHub (``BotGitHub``) or GitLab (``BotGitLab``).

:class:`ReviewHost` is every call the runner and its helpers (chat, learning, reactions, intent, proof refresh) make on
the host object; :class:`ChangeRef` is what they read off the change it reviews. Both are structural: a host satisfies
them by having the members, nothing registers.

The row types the calls trade in (:class:`~lemoncrow.pro.capabilities.review.pr_bot.github.Remote`,
``PullFacts``, ``InlineOutcome`` ...) stay GitHub's: they carry nothing GitHub-only that a GitLab host cannot fill, and a
GitLab note marks itself the bot's by ``author_type == "Bot"`` the same way (``pr_bot.gitlab``).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
    from lemoncrow.pro.capabilities.review.pr_bot.github import (
        FeedbackThread,
        InlineOutcome,
        PullFacts,
        ReactionTarget,
        Remote,
        ThreadOutcome,
    )
    from lemoncrow.pro.capabilities.review.pr_bot.proof_of_work import CiReport
    from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState


class ChangeRef(Protocol):
    """The durable identity of one reviewed change: a GitHub pull request or a GitLab merge request."""

    @property
    def owner(self) -> str: ...

    @property
    def slug(self) -> str:
        """``owner/repo`` on GitHub; the project's full path (``group/sub/project``) on GitLab."""
        ...

    @property
    def number(self) -> int: ...

    @property
    def host(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def source_ref(self) -> str: ...

    def commit_url(self, sha: str) -> str: ...


class ReviewHost(Protocol):
    """Every call the bot makes on its host for one change."""

    @property
    def pr(self) -> ChangeRef: ...

    # ----- reads -------------------------------------------------------- #

    def pull_facts(self) -> PullFacts: ...

    def issue(self, number: int) -> dict[str, Any]: ...

    def ci_results(self, sha: str, *, exclude_run: str = "") -> CiReport: ...

    def commits(self) -> list[tuple[str, str]] | None: ...

    def commit_messages(self) -> list[str] | None: ...

    def approvals(self, *, trusted: bool = False) -> list[tuple[str, str]] | None: ...

    def summary(self) -> tuple[Remote | None, ReviewState | None]: ...

    def review_comments(self) -> list[Remote]: ...

    def issue_comments(self) -> list[Remote]: ...

    def feedback_threads(self) -> list[FeedbackThread]: ...

    def permission_of(self, login: str) -> str: ...

    def probe_comment_access(self) -> None: ...

    # ----- writes ------------------------------------------------------- #

    def write_summary(self, body: str, existing: Remote | None) -> Remote: ...

    def post_comment(self, body: str) -> Remote: ...

    def reply_in_thread(self, root_id: int, body: str) -> Remote: ...

    def comment_url(self, comment_id: int) -> str: ...

    def adopt_existing(
        self, findings: Sequence[BotFinding], comments: Sequence[Remote], *, tracked: Collection[str] = ()
    ) -> dict[str, Remote]: ...

    def post_inline(
        self,
        findings: Sequence[BotFinding],
        head_sha: str,
        *,
        dry_run: bool = False,
        tracked: Collection[str] = (),
    ) -> InlineOutcome: ...

    def reply_and_resolve(self, findings: Sequence[BotFinding], comments: Sequence[Remote]) -> ThreadOutcome: ...

    def set_status(
        self, sha: str, state: str, description: str, target_url: str = "", *, context: str = ...
    ) -> None: ...

    # ----- reactions ---------------------------------------------------- #

    def add_reaction(self, target: ReactionTarget, content: str) -> tuple[int, str]: ...

    def remove_reaction(self, target: ReactionTarget, reaction_id: int) -> None: ...

    def reactions_by(self, target: ReactionTarget, content: str, login: str) -> list[int]: ...


def is_gitlab(environ: Mapping[str, str]) -> bool:
    """Whether this run is a GitLab CI job (``GITLAB_CI=true``), whatever pipeline it is in.

    A GitLab job that is not in a merge request pipeline is still GitLab's: its gate refuses it
    (``pr_bot.gitlab.decide_gitlab``) rather than letting it fall through to the GitHub path.
    """

    return str(environ.get("GITLAB_CI") or "").strip().lower() == "true"


__all__ = ["ChangeRef", "ReviewHost", "is_gitlab"]
