"""The DIY baseline reviewer: one plain prompt over the unified diff, the way a team wires up a homemade review Action.

This is the competitor that matters most: a prompt such as "Review this diff, list bugs as file:line: severity:
description" in a Claude Code or Codex GitHub Action. It gets no repository context, no triage, no investigation and no
verification pass: the diff goes in, a list of bugs comes out, in a single model call. Reviewing the same pull requests
through the same drivers, ceilings and scoring as LemonCrow is what makes the two comparable.

The prompt lives in ``evals/review/diy_review_prompt.v<N>.txt`` so it can be published and versioned; its text and
SHA-256 go into every report that used it. The structured fields the model fills (``file``, ``line``, ``severity``,
``description``) are the ``file:line: severity: description`` of the plain prompt: the drivers need a schema, and a
line of text would be parsed into exactly these four fields anyway.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

DIY_PROMPT_FILE: Final[str] = "diy_review_prompt.v1.txt"
#: About 100k tokens of diff. A homemade Action sends what fits; a larger diff is cut here and the run says so.
DEFAULT_MAX_DIFF_CHARS: Final[int] = 400_000
TRUNCATION_NOTICE: Final[str] = "[diff truncated by the benchmark: {shown} of {total} characters shown]"

DIY_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "line", "severity", "description"],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "description": {"type": "string"},
                },
            },
        }
    },
}
_SEVERITY: Final[Mapping[str, str]] = {"critical": "p0", "high": "p1", "medium": "p2", "low": "p2"}
_VERSION = re.compile(r"\.v(\d+)\.[A-Za-z0-9]+$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")
_BUG_MARKER = re.compile(r"BUG:\s*(.+)")


class DiyPromptError(ValueError):
    """The DIY prompt file is missing or empty."""


@dataclass(frozen=True, slots=True)
class DiyPrompt:
    text: str
    file: str
    version: str
    sha256: str

    def to_wire(self, *, with_text: bool = True) -> dict[str, Any]:
        return {
            "file": self.file,
            "version": self.version,
            "sha256": self.sha256,
            **({"text": self.text} if with_text else {}),
        }


def load_diy_prompt(path: Path) -> DiyPrompt:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiyPromptError(f"cannot read the DIY review prompt {path}: {exc}") from exc
    text = raw.strip()
    if not text:
        raise DiyPromptError(f"the DIY review prompt {path} is empty")
    found = _VERSION.search(path.name)
    return DiyPrompt(
        text=text,
        file=path.name,
        version=f"v{found.group(1)}" if found else "custom",
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def user_message(diff: str, *, max_chars: int) -> tuple[str, bool]:
    """``(the message the model gets, whether the diff was cut)``: the diff in a fence, cut at a line end past *max_chars*."""

    shown = diff
    truncated = len(diff) > max_chars
    if truncated:
        cut = diff.rfind("\n", 0, max_chars)
        shown = diff[: cut if cut > 0 else max_chars]
        shown += "\n" + TRUNCATION_NOTICE.format(shown=len(shown), total=len(diff))
    return f"```diff\n{shown}\n```", truncated


@dataclass(frozen=True, slots=True)
class DiyFinding:
    path: str
    line: int
    severity: str  # p0 | p1 | p2
    description: str

    @property
    def title(self) -> str:
        first = self.description.strip().splitlines()[0] if self.description.strip() else ""
        return first[:120]


def _clean_path(raw: str) -> str:
    path = raw.strip().strip("`")
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path


def parse_findings(result: Mapping[str, Any]) -> list[DiyFinding]:
    """The findings in a model's structured answer; a row without a path or a description is dropped, never repaired."""

    rows = result.get("findings")
    out: list[DiyFinding] = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, Mapping):
            continue
        path = _clean_path(str(row.get("file") or ""))
        description = str(row.get("description") or "").strip()
        line = row.get("line")
        if not path or not description:
            continue
        out.append(
            DiyFinding(
                path=path,
                line=line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else 0,
                severity=_SEVERITY.get(str(row.get("severity") or "").strip().lower(), "p2"),
                description=description,
            )
        )
    return sorted(out, key=lambda item: (item.path, item.line, item.description))


def scripted_answer(diff_message: str) -> dict[str, Any]:
    """What the ``fake`` driver answers: it reads the diff like a model would and reports what it is scripted to.

    A ``BUG: <title>`` marker on an added line is a high-severity finding on that line. A DIY reviewer is also noisier
    than a triaged one, so every added function without a docstring is a low-severity finding too (the false alarms a
    plain "list the bugs" prompt tends to raise). Lines are the new file's, from the hunk headers.
    """

    findings: list[dict[str, Any]] = []
    path = ""
    new_line = 0
    in_hunk = False
    lines = diff_message.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            path, in_hunk = "", False
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:].strip()
            path = "" if target == "/dev/null" else _clean_path(target)
        elif (hunk := _HUNK.match(line)) is not None:
            new_line, in_hunk = int(hunk.group(1)), True
        elif in_hunk and line.startswith("+"):
            content = line[1:]
            marker = _BUG_MARKER.search(content)
            following = lines[index + 1] if index + 1 < len(lines) else ""
            if marker is not None:
                findings.append(
                    {
                        "file": path,
                        "line": new_line,
                        "severity": "high",
                        "description": f"{marker.group(1).strip()}.",
                    }
                )
            elif content.lstrip().startswith("def ") and '"""' not in following:
                name = content.lstrip()[4:].split("(")[0]
                findings.append(
                    {
                        "file": path,
                        "line": new_line,
                        "severity": "low",
                        "description": f"Function {name} has no docstring.",
                    }
                )
            new_line += 1
        elif in_hunk and line.startswith(" "):
            new_line += 1
    return {"findings": findings}


__all__ = [
    "DEFAULT_MAX_DIFF_CHARS",
    "DIY_PROMPT_FILE",
    "DIY_SCHEMA",
    "DiyFinding",
    "DiyPrompt",
    "DiyPromptError",
    "load_diy_prompt",
    "parse_findings",
    "scripted_answer",
    "user_message",
]
