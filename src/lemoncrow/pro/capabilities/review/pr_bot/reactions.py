"""Reactions as the review's status: eyes while it runs, a thumbs up when it is done, confused when it could not run.

The reaction goes where the person who started the review will look: on the ``@lemoncrow review`` comment that asked for
it, or on the pull request itself for a run a push or an open started. When a review starts the bot reacts with eyes; when
it ends the eyes come off and a thumbs up (the review ran to its end, whatever it found) or a confused face (it could not
run, or could not finish) goes on. On the pull request itself an earlier run's opposite reaction is taken off too, so the
pull request shows only the latest outcome.

**Best effort, always.** A reaction is a courtesy and a refused one never fails a review, changes its status or turns the
job red: every call here is caught, and a refusal because the token may not react (below) is not even a note in the job
log, since it is the expected outcome on the sample workflow.

**Which permission.** GitHub's REST reference lists the three endpoints under different permissions:

========================================================  ==========================================
reaction on                                               token permission
========================================================  ==========================================
a review-thread comment (``pulls/comments/{id}``)         ``pull-requests: write``
a conversation comment (``issues/comments/{id}``)         ``issues: write`` (not ``pull-requests``)
the pull request itself (``issues/{number}``)             ``issues: write`` (not ``pull-requests``)
========================================================  ==========================================

The sample workflow grants ``contents: read``, ``pull-requests: write`` and ``statuses: write``, so out of the box only a
trigger typed in a review thread gets its reactions; a trigger in the conversation and an automatic run's pull request
need ``issues: write`` too, which is optional (the guide's permissions table). Without it nothing breaks: the review runs, the
commit status says ``pending`` and then the verdict, and the reaction is skipped.
"""

from __future__ import annotations

from typing import Final

from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub, ReactionTarget, is_denied
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

EYES: Final[str] = "eyes"
DONE: Final[str] = "+1"
FAILED: Final[str] = "confused"


class Signal:
    """One review's reactions on one target: :meth:`start` when it begins, :meth:`finish` when it ends.

    Nothing here raises for a GitHub refusal. :attr:`state` says what happened (``""`` nothing yet, ``eyes``, ``done``,
    ``failed``, or ``denied`` when the token may not react) and :attr:`notes` carries the one-line causes of any other
    failure, for the job log.
    """

    def __init__(self, github: BotGitHub, target: ReactionTarget) -> None:
        self.github = github
        self.target = target
        self.state = ""
        self.notes: list[str] = []
        self._eyes = 0
        self._login = ""
        self._denied = False

    @property
    def started(self) -> bool:
        return self._eyes > 0 or self.state in {"eyes", "done", "failed"}

    def start(self) -> None:
        """Eyes: the review has seen the request and is running."""

        try:
            self._eyes, self._login = self.github.add_reaction(self.target, EYES)
            self.state = "eyes"
        except GitHubCliError as exc:
            self._refused("react", exc)

    def finish(self, ok: bool) -> None:
        """Thumbs up when *ok* (the review ran to its end), confused when not; the eyes come off either way."""

        if self._denied:
            return  # the permission that refused the eyes refuses these
        content, opposite = (DONE, FAILED) if ok else (FAILED, DONE)
        try:
            _, login = self.github.add_reaction(self.target, content)
            self._login = login or self._login
            self.state = "done" if ok else "failed"
        except GitHubCliError as exc:
            self._refused("react", exc)
            return
        try:
            if self._eyes:
                self.github.remove_reaction(self.target, self._eyes)
            if self.target.kind == "pull" and self._login:
                # An earlier run's reaction of the other kind, by this same identity, is no longer true.
                for stale in self.github.reactions_by(self.target, opposite, self._login):
                    self.github.remove_reaction(self.target, stale)
        except GitHubCliError as exc:
            self._refused("clear a reaction", exc)

    def _refused(self, what: str, exc: GitHubCliError) -> None:
        if is_denied(exc):
            self._denied = True
            self.state = "denied"
        else:
            self.notes.append(f"could not {what}: {str(exc)[:160]}")

    @property
    def published(self) -> str:
        """What the run reports under ``published["reaction"]``: the reaction left on the target, or why none."""

        return {"done": "thumbs up", "failed": "confused", "eyes": "eyes", "denied": "not permitted"}.get(
            self.state, ""
        )


def ran(report: object) -> bool:
    """Whether a review ran to its end, which is what the thumbs up says: it was not skipped and its verdict is a finished
    one (must-fix, partial or clean). A review that could not finish (a model call failed, the call cap ran out) or did not
    run (the review budget is spent, the effective plan runs no AI) is the confused face. What it *found* is not part of
    this: a review that found a P0 ran."""

    verdict = getattr(report, "verdict", None)
    return (
        not getattr(report, "skipped", True)
        and verdict is not None
        and verdict.state in {"must_fix", "partial", "clean"}
    )


__all__ = ["DONE", "EYES", "FAILED", "Signal", "ran"]
