"""The Change Story as a small, deterministic SVG: LemonCrow's own renderer, with no dependency, font or URL.

The same steps as the summary's walkthrough (:func:`change_story.walkthrough`), drawn as an arc diagram: one row per
step, top to bottom in walk order, and one arc per relationship a step was reached by, on the right of the rows. The
layout is a pure function of the steps (integer coordinates, a fixed row height, no measuring of text), so the same
story gives the same bytes.

**Encoding.** A row's border and an arc's colour are the change: green added, red removed, amber changed, grey
unchanged or context. An arc is solid only when the relationship is a fact of this commit (``snapshot``) or was
``observed`` in a recorded run; ``inferred`` and ``current_index`` arcs are dashed, and a row whose file is current-index
context has a dashed border. The legend under the rows says so in words, and every row says it in text too.

**Themes.** The picture carries its own background, so it reads the same wherever it is shown; an embedded
``prefers-color-scheme`` rule switches to a dark palette when the viewer's system is dark. No font is loaded: the text
uses the platform's sans-serif.

**Where it goes.** GitHub strips an inline ``<svg>`` from comments and from job summaries, and does not render an
image from a ``data:`` URL, so the pull request comment keeps the Mermaid block (which GitHub draws itself) and this
file is written for the workflow to upload as an artifact (:func:`write_artifact`, :data:`ARTIFACT_ENV`).
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from lemoncrow.pro.capabilities.review.pr_bot.change_story import (
    EXACTNESS_WORDS,
    ChangeStory,
    StoryStep,
    findings_words,
    more_count,
    role_words,
    via_words,
)

#: Where the workflow wants the file; unset, nothing is written.
ARTIFACT_ENV: Final[str] = "LEMONCROW_REVIEW_CHANGE_STORY_SVG"
#: The most rows one picture draws; the rest are counted under it.
MAX_NODES: Final[int] = 12

WIDTH: Final[int] = 760
_PAD: Final[int] = 16
_TOP: Final[int] = 60
_ROW: Final[int] = 52
_BOX_H: Final[int] = 40
_BOX_W: Final[int] = 560
_ARC_X: Final[int] = _PAD + _BOX_W
_LINE1_CHARS: Final[int] = 64
_LINE2_CHARS: Final[int] = 92

_STYLE: Final[str] = (
    "text{font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif}"
    ".h{font-size:15px;font-weight:600}.t1{font-size:13px;font-weight:600}.t2{font-size:11px}"
    ".bg{fill:#ffffff}.fg{fill:#1f2328}.mu{fill:#59636e}.box{fill:#f6f8fa;stroke-width:1.5}"
    ".e{fill:none;stroke-width:1.5}.e.changed{stroke-width:3}.dash{stroke-dasharray:6 4}.idx{stroke-dasharray:4 3}"
    ".added{stroke:#1a7f37}.removed{stroke:#cf222e}.changed{stroke:#9a6700}.context{stroke:#818b98}"
    ".f-added{fill:#1a7f37}.f-removed{fill:#cf222e}.f-changed{fill:#9a6700}.f-context{fill:#818b98}"
    "@media (prefers-color-scheme:dark){"
    ".bg{fill:#0d1117}.fg{fill:#e6edf3}.mu{fill:#9198a1}.box{fill:#161b22}"
    ".added{stroke:#3fb950}.removed{stroke:#f85149}.changed{stroke:#d29922}.context{stroke:#9198a1}"
    ".f-added{fill:#3fb950}.f-removed{fill:#f85149}.f-changed{fill:#d29922}.f-context{fill:#9198a1}}"
)
_TONES: Final[tuple[str, ...]] = ("added", "removed", "changed", "context")


def _tone(change: str) -> str:
    return change if change in {"added", "removed", "changed"} else "context"


def _text(value: str) -> str:
    """*value* as XML character data: the five specials escaped, control and format characters dropped to ``?``."""

    out: list[str] = []
    for char in value:
        if char == "&":
            out.append("&amp;")
        elif char == "<":
            out.append("&lt;")
        elif char == ">":
            out.append("&gt;")
        elif char == '"':
            out.append("&quot;")
        elif char == "'":
            out.append("&#39;")
        elif unicodedata.category(char)[0] == "C":
            out.append("?")
        else:
            out.append(char)
    return "".join(out)


def _cut(value: str, limit: int, *, tail: bool = False) -> str:
    if len(value) <= limit:
        return value
    return "\N{HORIZONTAL ELLIPSIS}" + value[-(limit - 1) :] if tail else value[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def step_lines(step: StoryStep, previous: StoryStep | None, *, findings_known: bool) -> tuple[str, str]:
    """The two lines of a row, as plain text: the number and file, then what the step is and how it was reached."""

    name = step.path if step.via is None or not step.title else step.title
    first = f"{step.index + 1}. {name or '(no path)'}"
    parts = [role_words(step)]
    if step.via is not None:
        parts.append(via_words(step, previous))
    if step.exactness != "snapshot" and (step.via is None or step.via.exactness != step.exactness):
        parts.append(f"file {EXACTNESS_WORDS[step.exactness]}")
    if findings_known and (said := findings_words(step)):
        parts.append(said)
    return first, " \N{MIDDLE DOT} ".join(parts)


def render_svg(story: ChangeStory, *, limit: int = MAX_NODES) -> str:
    """The SVG text for *story*: at most *limit* rows, byte-identical for the same story."""

    shown = list(story.steps[: max(1, min(limit, MAX_NODES))])
    row_of = {step.node_id: index for index, step in enumerate(shown)}
    more = more_count(story, len(shown))
    legend_y = _TOP + len(shown) * _ROW + 10
    height = legend_y + (64 if more else 46)
    described: list[str] = [story.order_note]

    body: list[str] = [
        f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{height}" rx="8"/>',
        f'<text class="fg h" x="{_PAD}" y="28">Change Story</text>',
        f'<text class="mu t2" x="{_PAD}" y="46">{_text(_cut(story.order_note, 120))}</text>',
    ]
    for index, step in enumerate(shown):
        previous = story.steps[step.index - 1] if step.index > 0 else None
        first, second = step_lines(step, previous, findings_known=story.findings_known)
        described.append(f"{first}: {second}")
        y = _TOP + index * _ROW
        border = f"box {_tone(step.change)}" + (" idx" if step.exactness != "snapshot" else "")
        body += [
            f'<g id="step-{index + 1}">',
            f'<rect class="{border}" x="{_PAD}" y="{y}" width="{_BOX_W}" height="{_BOX_H}" rx="6"/>',
            f'<text class="fg t1" x="{_PAD + 12}" y="{y + 17}">{_text(_cut(first, _LINE1_CHARS, tail=True))}</text>',
            f'<text class="mu t2" x="{_PAD + 12}" y="{y + 32}">{_text(_cut(second, _LINE2_CHARS))}</text>',
            "</g>",
        ]
    for step in shown:
        via = step.via
        if via is None or via.from_id not in row_of or via.to_id not in row_of:
            continue
        start, end = row_of[via.from_id], row_of[via.to_id]
        y1 = _TOP + start * _ROW + _BOX_H // 2
        y2 = _TOP + end * _ROW + _BOX_H // 2
        bend = _ARC_X + min(170, 28 + 16 * abs(start - end))
        tone = _tone(via.change)
        dashed = "" if via.recorded else " dash"
        body.append(
            f'<path class="e {tone}{dashed}" d="M {_ARC_X} {y1} C {bend} {y1} {bend} {y2} {_ARC_X + 2} {y2}" '
            f'marker-end="url(#m-{tone})"><title>{_text(via.from_name)} {_text(via.relation or "relates to")} '
            f"{_text(via.to_name)}: {_text(EXACTNESS_WORDS[via.exactness])}</title></path>"
        )
    legend = [
        f'<line class="e context" x1="{_PAD}" y1="{legend_y + 6}" x2="{_PAD + 28}" y2="{legend_y + 6}"/>',
        f'<text class="mu t2" x="{_PAD + 36}" y="{legend_y + 10}">solid: in this commit, or observed in a recorded run'
        "</text>",
        f'<line class="e context dash" x1="{_PAD + 340}" y1="{legend_y + 6}" x2="{_PAD + 368}" y2="{legend_y + 6}"/>',
        f'<text class="mu t2" x="{_PAD + 376}" y="{legend_y + 10}">dashed: inferred or from the current index, not this'
        " commit</text>",
    ]
    x = _PAD
    for tone, word in zip(_TONES, ("added", "removed", "changed", "unchanged or context"), strict=True):
        legend.append(
            f'<rect class="box {tone}" x="{x}" y="{legend_y + 20}" width="14" height="10" rx="2"/>'
            f'<text class="mu t2" x="{x + 20}" y="{legend_y + 29}">{word}</text>'
        )
        x += 20 + 8 * len(word) + 16
    if more:
        legend.append(f'<text class="mu t2" x="{_PAD}" y="{legend_y + 50}">{more} more in the Review.</text>')
        described.append(f"{more} more in the Review.")
    markers = "".join(
        f'<marker id="m-{tone}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" '
        f'orient="auto-start-reverse"><path class="f-{tone}" d="M 0 0 L 8 4 L 0 8 z"/></marker>'
        for tone in _TONES
    )
    head = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="cs-title cs-desc">'
    )
    return "\n".join(
        [
            head,
            '<title id="cs-title">Change Story</title>',
            f'<desc id="cs-desc">{_text(" ".join(described))}</desc>',
            f"<style>{_STYLE}</style>",
            f"<defs>{markers}</defs>",
            *body,
            *legend,
            "</svg>",
            "",
        ]
    )


def write_artifact(story: ChangeStory, environ: Mapping[str, str] | None = None) -> Path | None:
    """Write :func:`render_svg` to the path :data:`ARTIFACT_ENV` names, for the workflow to upload; ``None`` when it
    names none or the file cannot be written (a picture never fails a review)."""

    raw = str((os.environ if environ is None else environ).get(ARTIFACT_ENV) or "").strip()
    if not raw or not story.steps:
        return None
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_svg(story), encoding="utf-8")
    except OSError:
        return None
    return path


__all__ = ["ARTIFACT_ENV", "MAX_NODES", "render_svg", "step_lines", "write_artifact"]
