"""The workflow ``lc review init-action`` writes: the one in ``docs/setup/github-action.md``, not a second copy of it.

``lemoncrow-review.workflow.yml`` beside this module is the sample workflow of the setup guide, byte for byte (a test
holds the guide's block and this file equal), so the guide and the command cannot drift. The options only *edit* that
sample at places it names itself:

* the default is the review-only install the guide describes ("Delete these two entries (and the `if:` below) for a
  review-only install"); ``chat=True`` keeps the comment triggers and is exactly the sample;
* ``provider`` picks which model key the sample passes (the sample shows Anthropic and names OpenAI in a comment);
* ``fail_on="p0"`` turns on the sample's own suggestion to fail the check while a P0 is open.

An edit whose anchor is gone from the sample raises :class:`WorkflowTemplateError`, which the test of every option
combination turns into a failing test the day the guide changes shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

TEMPLATE: Final[str] = "lemoncrow-review.workflow.yml"
WORKFLOW_PATH: Final[str] = ".github/workflows/lemoncrow-review.yml"
PROVIDERS: Final[tuple[str, ...]] = ("anthropic", "openai")
FAIL_ON: Final[tuple[str, ...]] = ("none", "p0")
SECRETS: Final[dict[str, str]] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
PIN_PLACEHOLDER: Final[str] = "<commit-sha>"

_CHAT_TRIGGERS = re.compile(r"  # Chat: .*?\n(?=\npermissions:)", re.DOTALL)
_CHAT_CONDITION = re.compile(r"    # Start a runner for a comment .*?\n    if: >-\n(?:      .*\n)+(?=    steps:)")
_ANTHROPIC_KEY = (
    "          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n"
    "          # or: OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}\n"
)
_OPENAI_KEY = (
    "          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}\n"
    "          # or: ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n"
)
_ADVISORY = re.compile(r"          # Advisory while you trial it: .*?          fail-on: none\n", re.DOTALL)
_GATING = (
    '          # Fails the "LemonCrow Review" check while a P0 is open. Add the check to branch protection to block\n'
    "          # merges. Set `none` to keep the check advisory.\n"
    "          fail-on: p0\n"
)


class WorkflowTemplateError(RuntimeError):
    """The packaged sample no longer has a place an option edits."""


def template_path() -> Path:
    return Path(__file__).resolve().with_name(TEMPLATE)


def sample() -> str:
    """The sample workflow exactly as the guide shows it."""

    return template_path().read_text(encoding="utf-8")


def _edited(text: str, pattern: re.Pattern[str] | str, replacement: str, what: str) -> str:
    if isinstance(pattern, str):
        if pattern not in text:
            raise WorkflowTemplateError(f"the sample workflow no longer has {what}")
        return text.replace(pattern, replacement, 1)
    edited, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise WorkflowTemplateError(f"the sample workflow no longer has {what}")
    return edited


def render_workflow(*, provider: str = "anthropic", fail_on: str = "none", chat: bool = False) -> str:
    """The workflow file for these options."""

    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
    if fail_on not in FAIL_ON:
        raise ValueError(f"fail-on must be one of {', '.join(FAIL_ON)}")
    text = sample()
    if not chat:
        text = _edited(text, _CHAT_TRIGGERS, "", "the comment triggers")
        text = _edited(text, _CHAT_CONDITION, "", "the condition on comment events")
    if provider == "openai":
        text = _edited(text, _ANTHROPIC_KEY, _OPENAI_KEY, "the Anthropic key line")
    if fail_on == "p0":
        text = _edited(text, _ADVISORY, _GATING, "the advisory fail-on setting")
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
            f"Pin the Action: replace {PIN_PLACEHOLDER} in {path} with the full 40-character SHA of a commit of "
            "lemoncrow-lab/lemoncrow. There is no moving tag, on purpose.",
            ("gh api repos/lemoncrow-lab/lemoncrow/commits/HEAD --jq .sha",),
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
    "FAIL_ON",
    "PIN_PLACEHOLDER",
    "PROVIDERS",
    "WORKFLOW_PATH",
    "WorkflowTemplateError",
    "next_steps",
    "render_workflow",
    "sample",
    "template_path",
]
