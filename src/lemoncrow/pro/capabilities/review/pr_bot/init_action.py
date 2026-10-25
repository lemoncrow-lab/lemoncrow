"""The workflow ``lc review init-action`` writes: the one in ``docs/setup/github-action.md``, not a second copy of it.

``lemoncrow-review.workflow.yml`` beside this module is the minimal workflow the setup guide leads with, byte for byte
(a test holds the guide's block and this file equal), so the guide and the command cannot drift. The options only *add*
to that sample the lines the guide shows as additions:

* ``chat=True`` adds the two comment triggers and the job condition the guide's "Chat in pull request threads" shows;
* ``provider`` picks which key the sample passes (``anthropic-api-key``, or ``openai-api-key`` for OpenAI);
* ``fail_on="p0"`` adds the ``fail-on: p0`` line the guide's options table describes.

An edit whose anchor is gone from the sample raises :class:`WorkflowTemplateError`, which the test of every option
combination turns into a failing test the day the guide changes shape.

The pin. The sample carries one placeholder line, :data:`PIN_LINE`, and so do the README and the guide; the person
replaces ``<sha>`` with the release commit the release notes print (:data:`RELEASES_URL`). Nothing here suggests a
branch, a moving tag or "the latest commit": the commit is the version that reviews.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

TEMPLATE: Final[str] = "lemoncrow-review.workflow.yml"
WORKFLOW_PATH: Final[str] = ".github/workflows/lemoncrow-review.yml"
PROVIDERS: Final[tuple[str, ...]] = ("anthropic", "openai")
FAIL_ON: Final[tuple[str, ...]] = ("none", "p0")
SECRETS: Final[dict[str, str]] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
#: The Action's input that carries each provider's key.
KEY_INPUTS: Final[dict[str, str]] = {"anthropic": "anthropic-api-key", "openai": "openai-api-key"}

#: The one pin line every page shows: ``<sha>`` is replaced with the commit the release notes print. The version after
#: ``#`` is a reminder of what that commit is; the README, the guide and the sample are held to this exact line by a test.
PIN_PLACEHOLDER: Final[str] = "<sha>"
PIN_VERSION: Final[str] = "v0.9.0"
PIN_LINE: Final[str] = f"lemoncrow-lab/lemoncrow@{PIN_PLACEHOLDER}  # {PIN_VERSION}"
#: Where the release commit to pin is printed: the release notes of every release carry the ready-to-copy line.
RELEASES_URL: Final[str] = "https://github.com/lemoncrow-lab/lemoncrow/releases/latest"
GUIDE_PIN_URL: Final[str] = (
    "https://github.com/lemoncrow-lab/lemoncrow/blob/main/docs/setup/github-action.md#pin-the-action"
)

_ON_TYPES = "    types: [opened, synchronize, reopened, ready_for_review]\n"
CHAT_TRIGGERS: Final[str] = "  issue_comment: {types: [created]}\n  pull_request_review_comment: {types: [created]}\n"
_RUNS_ON = "    runs-on: ubuntu-latest\n"
#: Start a runner for a comment only when it mentions the bot (a comment on a plain issue never starts one).
CHAT_CONDITION: Final[str] = (
    "    if: >-\n"
    "      github.event_name == 'pull_request'\n"
    "      || (github.event_name == 'issue_comment' && github.event.issue.pull_request"
    " && contains(github.event.comment.body, '@lemoncrow'))\n"
    "      || (github.event_name == 'pull_request_review_comment'"
    " && contains(github.event.comment.body, '@lemoncrow'))\n"
)
_ANTHROPIC_KEY = "          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}\n"
_OPENAI_KEY = "          openai-api-key: ${{ secrets.OPENAI_API_KEY }}\n"
GATING: Final[str] = (
    '          # Fails the "LemonCrow Review" check (and this job) while a P0 finding is open. Remove it to stay advisory.\n'
    "          fail-on: p0\n"
)


class WorkflowTemplateError(RuntimeError):
    """The packaged sample no longer has a place an option edits."""


def template_path() -> Path:
    return Path(__file__).resolve().with_name(TEMPLATE)


def sample() -> str:
    """The sample workflow exactly as the guide shows it."""

    return template_path().read_text(encoding="utf-8")


def _edited(text: str, anchor: str, replacement: str, what: str) -> str:
    if text.count(anchor) != 1:
        raise WorkflowTemplateError(f"the sample workflow no longer has {what}")
    return text.replace(anchor, replacement, 1)


def render_workflow(*, provider: str = "anthropic", fail_on: str = "none", chat: bool = False) -> str:
    """The workflow file for these options."""

    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
    if fail_on not in FAIL_ON:
        raise ValueError(f"fail-on must be one of {', '.join(FAIL_ON)}")
    text = sample()
    if chat:
        text = _edited(text, _ON_TYPES, _ON_TYPES + CHAT_TRIGGERS, "the pull_request trigger")
        text = _edited(text, _RUNS_ON, _RUNS_ON + CHAT_CONDITION, "the job's runs-on line")
    if provider == "openai":
        text = _edited(text, _ANTHROPIC_KEY, _OPENAI_KEY, "the Anthropic key line")
    if fail_on == "p0":
        key = _OPENAI_KEY if provider == "openai" else _ANTHROPIC_KEY
        text = _edited(text, key, key + GATING, "the key line the fail-on setting follows")
    return text


def next_steps(*, provider: str, chat: bool, path: str = WORKFLOW_PATH) -> list[tuple[str, tuple[str, ...]]]:
    """What the person still has to do, which no command can do for them: ``(step, command lines)`` in order."""

    secret = SECRETS[provider]
    steps: list[tuple[str, tuple[str, ...]]] = [
        (
            f"Add your model key as a repository secret named {secret} (Settings > Secrets and variables > Actions).",
            (f"gh secret set {secret}",),
        ),
        (
            f"Pin the Action: replace {PIN_PLACEHOLDER} in {path} with the release commit shown at {RELEASES_URL} "
            f'(see "Pin the Action" in the guide: {GUIDE_PIN_URL}).',
            (),
        ),
        (
            "Commit the file to your default branch, then open a pull request: the bot posts a summary and a "
            "`LemonCrow Review` status when the run finishes.",
            (),
        ),
    ]
    if chat:
        steps.append(
            ("Chat is on: mention @lemoncrow in a pull request comment or review thread.", ("@lemoncrow help",))
        )
    return steps


__all__ = [
    "CHAT_CONDITION",
    "CHAT_TRIGGERS",
    "FAIL_ON",
    "GATING",
    "GUIDE_PIN_URL",
    "KEY_INPUTS",
    "PIN_LINE",
    "PIN_PLACEHOLDER",
    "PIN_VERSION",
    "PROVIDERS",
    "RELEASES_URL",
    "WORKFLOW_PATH",
    "WorkflowTemplateError",
    "next_steps",
    "render_workflow",
    "sample",
    "template_path",
]
