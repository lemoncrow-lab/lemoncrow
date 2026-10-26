"""The lines of the summary that say how the noise controls shaped this review, so nothing is left out silently.

Pure text. A file name, a glob or a rule key is repository text, so each goes through the summary's own neutralisers
(:func:`render.code`, :func:`render.clean`) before it is written; the model writes none of it.
"""

from __future__ import annotations

from collections.abc import Sequence

from lemoncrow.pro.capabilities.review.pr_bot.large_pr import MAX_NAMED
from lemoncrow.pro.capabilities.review.pr_bot.pr_intent import IntentNote
from lemoncrow.pro.capabilities.review.pr_bot.render import clean, code, plural

#: Config notes shown in the summary; the rest are in the job log.
MAX_NOTES: int = 4


def _globs(globs: Sequence[str]) -> str:
    shown = ", ".join(f"`{code(item)}`" for item in globs[:4])
    return shown + (f" and {len(globs) - 4} more" if len(globs) > 4 else "")


def large_pr_banner(*, total: int, limit: int, deferred: Sequence[str]) -> str:
    """The block quote that says a large pull request was cut: how many files, the limit, which were left, and what happens next."""

    if not deferred:
        return ""
    reviewed = max(total - len(deferred), 0)
    named = ", ".join(f"`{code(path)}`" for path in deferred[:MAX_NAMED])
    more = f" and {len(deferred) - MAX_NAMED} more" if len(deferred) > MAX_NAMED else ""
    left = "lower-risk file is" if len(deferred) == 1 else "lower-risk files are"
    first = "highest-risk file" if reviewed == 1 else f"{reviewed} highest-risk files"
    return (
        f"> **Large pull request: {plural(total, 'file')}, over the limit of {limit}.** Reviewed the {first} first. "
        f"{len(deferred)} {left} not reviewed yet: {named}{more}. They are reviewed next, highest risk first, on the "
        "next push or when the job is re-run; `max_files` in `.lemoncrow/review.yaml` changes the limit."
    )


def settings_line(
    *,
    ignored: Sequence[str],
    globs: Sequence[str],
    inline: bool,
    notes: Sequence[str],
    intent_skipped: Sequence[str],
) -> str:
    """One line of what the settings left out or could not do: ignored files, summary-only mode, config problems, unread intent."""

    parts: list[str] = []
    if ignored:
        parts.append(f"{plural(len(ignored), 'file')} left out by `review.ignore` ({_globs(globs)})")
    if not inline:
        parts.append("`inline_comments` is off: findings are listed here and not posted on the diff")
    parts.extend(f"config: {clean(note)}" for note in notes[:MAX_NOTES])
    if len(notes) > MAX_NOTES:
        parts.append(f"{len(notes) - MAX_NOTES} more config notes in the job log")
    parts.extend(f"intent: {clean(note)}" for note in intent_skipped)
    return " · ".join(parts)


def receipt_extra(*, intent: IntentNote | None, ignored: Sequence[str], globs: Sequence[str]) -> tuple[str, ...]:
    """The extra lines of the clean-review receipt (``What we checked``): the intent the review was told, and the files left out."""

    lines: list[str] = []
    if intent is not None:
        lines.append(intent.receipt())
    if ignored:
        lines.append(f"Left out by review.ignore: {plural(len(ignored), 'file')} ({_globs(globs)})")
    return tuple(lines)


__all__ = ["MAX_NOTES", "large_pr_banner", "receipt_extra", "settings_line"]
