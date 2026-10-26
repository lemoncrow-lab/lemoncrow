"""Another review tool's settings, mapped to LemonCrow's (G10): the key map behind ``lc review config import``.

A team that moves over usually keeps a settings file beside its rules: JSON with a strictness, ignore patterns, comment
types, a free-text brief and a severity per rule. The rules in such a file already import (``config_import``: ``{id,
rule, scope, enabled}`` in a file ``import.rules_files`` lists). This module is the other half, the settings, as a plain
key map. It names no product and adds no new input format: the file is read as the JSON or YAML it is, and every
setting lands on a LemonCrow setting that already exists or is reported as dropped, with the reason and the closest
thing LemonCrow has.

The mapping, and what each one is allowed to do:

* ``strictness`` -> ``review.strictness`` (the same 1 to 3 scale).
* ``ignorePatterns`` (gitignore lines) -> ``review.ignore`` (path globs, which keep a file out of the review and out of
  every prompt). Negation and match-everything patterns are not carried.
* ``commentTypes`` -> nothing to set for the defect types (LemonCrow reports defects only); an empty list ->
  ``review.inline_comments: false``; ``style`` and ``info`` have no counterpart and are dropped with the closest advice.
* ``instructions`` -> a conventions file listed in ``import.agent_docs_globs``. Free text is not a rule (a rule is checked
  for violations), and conventions are context the model is told never to follow, which the report says plainly.
* a rule's ``severity`` -> ``review.rules.<key>.severity``. **A settings file can never make a P0**: the most it can write
  is ``p1``, which is also the most an imported (advisory) rule can reach through ``pr_bot.settings.resolve_policy``. A P0
  needs a critical rule or evidence from outside the model's own code (``criticality.severity_for``), and a file is neither.
* ``fileChangeLimit`` -> ``review.max_files``; ``updateSummaryOnly`` -> ``review.inline_comments: false``.

Nothing runs at review time except :func:`setting_note`, which gives a setting key found in a rules file a note that says
what it would have been and whether ``.lemoncrow/review.yaml`` already carries it. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import yaml

from lemoncrow.pro.capabilities.review.pr_bot.settings import MAX_FILES_CEILING, MAX_IGNORE

Outcome = Literal["carried", "mapped", "dropped"]
#: A place in ``.lemoncrow/review.yaml``: ``("review", "strictness")``, ``("review", "rules", key)``,
#: ``("import", "agent_docs_globs", path)``.
Need = tuple[str, ...]

REVIEW_CONFIG: Final[str] = ".lemoncrow/review.yaml"
#: Where the free-text brief is written, and listed under ``import.agent_docs_globs``.
INSTRUCTIONS_PATH: Final[str] = ".lemoncrow/review-instructions.md"
#: The most a settings file may set a rule to. Only a critical rule, or evidence beyond the model's own code, is a P0.
SEVERITY_CEILING: Final[str] = "p1"
_SHOWN: Final[int] = 5  # names listed in one report row before "and N more"

_DEFECT_TYPES: Final[frozenset[str]] = frozenset({"logic", "syntax"})
_SEVERITY_WORDS: Final[Mapping[str, str]] = {
    "high": "p1",
    "medium": "p2",
    "low": "p2",
    "critical": "p1",
    "p0": "p1",
    "p1": "p1",
    "p2": "p2",
}
_PAST_THE_CEILING: Final[frozenset[str]] = frozenset({"critical", "p0"})
_EVERYTHING: Final[frozenset[str]] = frozenset({"*", "**", "**/*", "**/**"})


@dataclass(frozen=True, slots=True)
class ReportRow:
    """One setting of the file and what became of it."""

    setting: str
    outcome: Outcome
    target: str = ""
    detail: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"setting": self.setting, "outcome": self.outcome, "target": self.target, "detail": self.detail}

    def sentence(self) -> str:
        """The row as one sentence for a note: the LemonCrow setting it went to, then why or how."""

        if self.outcome == "dropped" or not self.target:
            return self.detail
        return f"Closest: {self.target}. {self.detail}"


@dataclass(frozen=True, slots=True)
class SettingsPlan:
    """What a settings file asks ``.lemoncrow/review.yaml`` to hold, and the report of how each setting was handled."""

    review: Mapping[str, Any]  # for the ``review:`` block
    imports: Mapping[str, Any]  # additions to the ``import:`` block (the caller adds ``rules_files``)
    instructions: str  # text for ``INSTRUCTIONS_PATH``; empty when there is none
    rows: tuple[ReportRow, ...]
    needs: tuple[Need, ...]

    def counts(self) -> dict[str, int]:
        return {
            outcome: sum(1 for row in self.rows if row.outcome == outcome)
            for outcome in ("carried", "mapped", "dropped")
        }


class _Builder:
    def __init__(self) -> None:
        self.review: dict[str, Any] = {}
        self.imports: dict[str, Any] = {}
        self.instructions = ""
        self.rows: list[ReportRow] = []
        self.needs: list[Need] = []

    def row(self, setting: str, outcome: Outcome, target: str = "", detail: str = "") -> None:
        self.rows.append(ReportRow(setting, outcome, target, detail))

    def need(self, *place: str) -> None:
        if place not in self.needs:
            self.needs.append(place)

    def plan(self) -> SettingsPlan:
        return SettingsPlan(self.review, self.imports, self.instructions, tuple(self.rows), tuple(self.needs))


def _show(value: object, limit: int = 40) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _names(items: Sequence[str]) -> str:
    shown = ", ".join(items[:_SHOWN])
    return shown + (f" and {len(items) - _SHOWN} more" if len(items) > _SHOWN else "")


# --------------------------------------------------------------------------- #
# strictness
# --------------------------------------------------------------------------- #


def _strictness(b: _Builder, value: object) -> None:
    level: object = int(value) if isinstance(value, str) and value.strip().isdigit() else value
    if isinstance(level, bool) or not isinstance(level, int) or level not in (1, 2, 3):
        b.row(
            "strictness",
            "dropped",
            detail=f"{_show(value)} is not 1, 2 or 3, so nothing was set. Closest: review.strictness (1, 2 or 3).",
        )
        return
    detail = "Same 1 to 3 scale: 1 reports everything, 2 is the default, 3 never comments on P2."
    if level == 2:
        detail += (
            " The default differs: at 2 LemonCrow holds P2 findings in a collapsed block with no inline comment; "
            "use 1 for inline P2 comments."
        )
    b.review["strictness"] = level
    b.need("review", "strictness")
    b.row("strictness", "carried", f"review.strictness: {level}", detail)


# --------------------------------------------------------------------------- #
# ignorePatterns: gitignore lines -> path globs
# --------------------------------------------------------------------------- #


def _has_wildcard(part: str) -> bool:
    return any(char in part for char in "*?[")


def gitignore_globs(lines: Sequence[str]) -> tuple[list[str], dict[str, list[str]]]:
    """``(globs, problems)`` for gitignore-style *lines*, in LemonCrow's path dialect (``*`` also crosses ``/``).

    A name with no slash matches at any depth, a slash in the middle or a leading ``/`` anchors it at the root, a trailing
    ``/`` or ``/**`` means a directory and everything under it, and a leading ``**/`` means at any depth. ``problems`` maps
    ``negation``, ``everything``, ``unsafe`` to the lines that were not carried: a negated line, a pattern that would leave
    every file out, and one that is not a safe repository-relative glob (``..``, a backslash escape, ``~``).
    """

    from lemoncrow.pro.capabilities.review.rules import _safe_path

    globs: dict[str, None] = {}
    problems: dict[str, list[str]] = {"negation": [], "everything": [], "unsafe": []}
    for raw in lines:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith(("\\#", "\\!")):
            text = text[1:]
        elif text.startswith("!"):
            problems["negation"].append(raw.strip())
            continue
        anchored = text.startswith("/")
        text = text.lstrip("/")
        directory = text.endswith("/")
        text = text.rstrip("/")
        deep = False
        while text.startswith("**/"):
            text, deep = text[3:], True
        if text.endswith("/**"):
            text, directory = text[:-3], True
        if not text or text in _EVERYTHING or text == ".":
            problems["everything"].append(raw.strip())
            continue
        if "\\" in text or text.startswith("~") or ".." in text.split("/"):
            problems["unsafe"].append(raw.strip())
            continue
        anywhere = deep or (not anchored and "/" not in text)
        last = text.rsplit("/", 1)[-1]
        forms = [f"{text}/**"] if directory else [text] if _has_wildcard(last) else [text, f"{text}/**"]
        prefixes = ("", "**/") if anywhere and not text.startswith("*") else ("",)
        try:
            for prefix in prefixes:
                for form in forms:
                    globs.setdefault(_safe_path(prefix + form, field="ignorePatterns"))
        except ValueError:
            problems["unsafe"].append(raw.strip())
    return list(globs), {kind: found for kind, found in problems.items() if found}


def _ignore(b: _Builder, value: object) -> None:
    if isinstance(value, str):
        lines = value.splitlines()
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        lines = [part for item in value for part in item.splitlines()]
    else:
        b.row(
            "ignorePatterns",
            "dropped",
            detail="Must be a string of patterns, one per line, or a list of them, so nothing was set. Closest: review.ignore.",
        )
        return
    globs, problems = gitignore_globs(lines)
    patterns = sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))
    patterns -= sum(len(found) for found in problems.values())  # the lines that are carried
    if globs:
        kept = globs[:MAX_IGNORE]
        b.review["ignore"] = kept
        b.need("review", "ignore")
        b.row(
            "ignorePatterns",
            "carried",
            f"review.ignore: {len(kept)} globs",
            f"{patterns} pattern lines became {len(kept)} globs. LemonCrow also hides an ignored file from the model as "
            "context (callers, tests), not only from the review.",
        )
        if len(globs) > MAX_IGNORE:
            b.row(
                "ignorePatterns",
                "dropped",
                detail=f"{len(globs) - MAX_IGNORE} globs are over the limit of {MAX_IGNORE} and were left out. "
                "Closest: merge overlapping patterns in review.ignore.",
            )
    elif not problems:
        b.row("ignorePatterns", "carried", detail="No patterns, so nothing was set.")
    if "negation" in problems:
        found = problems["negation"]
        b.row(
            "ignorePatterns",
            "dropped",
            detail=f"Negation ({_names(found)}) cannot be expressed, so files it re-includes stay ignored and more is "
            "left out than before. Closest: narrow the earlier pattern in review.ignore.",
        )
    if "everything" in problems:
        found = problems["everything"]
        b.row(
            "ignorePatterns",
            "dropped",
            detail=f"{_names(found)} would leave every file out of the review; not carried.",
        )
    if "unsafe" in problems:
        found = problems["unsafe"]
        b.row(
            "ignorePatterns",
            "dropped",
            detail=f"{_names(found)} is not a repository-relative glob (no .., ~ or backslash); not carried.",
        )


# --------------------------------------------------------------------------- #
# commentTypes
# --------------------------------------------------------------------------- #


def _comment_types(b: _Builder, value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        b.row(
            "commentTypes",
            "dropped",
            detail="Must be a list of logic, syntax, style or info, so nothing was set. "
            "Closest: review.inline_comments and review.strictness, which shape what is posted.",
        )
        return
    types = list(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
    if not types:
        b.review["inline_comments"] = False
        b.need("review", "inline_comments")
        b.row(
            "commentTypes",
            "mapped",
            "review.inline_comments: false",
            "An empty list posts no comments. Summary only is closest: nothing goes on the diff, findings are still "
            "listed in the summary and the check still runs.",
        )
        return
    for kind in types:
        label = f"commentTypes: {kind}"
        if kind in _DEFECT_TYPES:
            b.row(
                label,
                "carried",
                "built in",
                "LemonCrow reports defects (correctness, security, API compatibility, concurrency, persistence, "
                "performance, verification); there is nothing to set.",
            )
        elif kind == "style":
            b.row(
                label,
                "dropped",
                detail="LemonCrow posts no style comments and cannot be told to. Closest: state the convention as a rule "
                "(in a file import.rules_files lists), which is checked as a defect, or list the style guide in "
                "import.agent_docs_globs as context.",
            )
        elif kind == "info":
            b.row(
                label,
                "dropped",
                detail="LemonCrow posts no informational comments; the summary carries the overview. Nothing to turn on.",
            )
        else:
            b.row(label, "dropped", detail=f"{kind!r} is not one of logic, syntax, style or info; ignored.")
    if not _DEFECT_TYPES.intersection(types):
        b.row(
            "commentTypes",
            "dropped",
            detail="This list leaves out logic and syntax, but defect findings cannot be switched off by type. "
            "Closest: review.strictness 3 (P0 and P1 only) or review.inline_comments: false (summary only).",
        )


# --------------------------------------------------------------------------- #
# instructions
# --------------------------------------------------------------------------- #


def _instructions(b: _Builder, value: object) -> None:
    if not isinstance(value, str):
        b.row(
            "instructions",
            "dropped",
            detail="Must be text, so nothing was set. Closest: a file listed in import.agent_docs_globs.",
        )
        return
    text = value.strip()
    if not text:
        return
    b.instructions = text
    b.imports["agent_docs_globs"] = [INSTRUCTIONS_PATH]
    b.need("import", "agent_docs_globs", INSTRUCTIONS_PATH)
    b.row(
        "instructions",
        "mapped",
        f"{INSTRUCTIONS_PATH}, listed in import.agent_docs_globs",
        f"{len(text):,} characters, sent to the investigator as project conventions: context the model is told never "
        "to follow as an instruction, inside the conventions budget (600 to 2,000 tokens a call, shared with AGENTS.md). "
        'Free text is not a rule, and a directive such as "do not comment on X" is not enforced. What is enforced: '
        "review.ignore (paths) and review.strictness (volume).",
    )


# --------------------------------------------------------------------------- #
# fileChangeLimit, updateSummaryOnly
# --------------------------------------------------------------------------- #


def _file_limit(b: _Builder, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_FILES_CEILING:
        b.row(
            "fileChangeLimit",
            "dropped",
            detail=f"{_show(value)} is not a whole number from 0 to {MAX_FILES_CEILING:,}, so nothing was set. "
            "Closest: review.max_files.",
        )
        return
    b.review["max_files"] = value
    b.need("review", "max_files")
    b.row(
        "fileChangeLimit",
        "mapped",
        f"review.max_files: {value}",
        "Over the limit a pull request is not skipped: the highest-risk files are reviewed first and the next run "
        "takes the rest.",
    )


def _summary_only(b: _Builder, value: object) -> None:
    if value is True:
        b.review["inline_comments"] = False
        b.need("review", "inline_comments")
        b.row(
            "updateSummaryOnly",
            "carried",
            "review.inline_comments: false",
            "Summary only: nothing is posted on the diff and every finding is listed in the summary.",
        )
    elif value is False:
        b.row("updateSummaryOnly", "carried", "built in", "Inline comments stay on, which is LemonCrow's default.")
    else:
        b.row(
            "updateSummaryOnly",
            "dropped",
            detail=f"{_show(value)} is not true or false, so nothing was set. Closest: review.inline_comments.",
        )


# --------------------------------------------------------------------------- #
# settings with no counterpart
# --------------------------------------------------------------------------- #

#: ``(keys, why LemonCrow has no setting, the closest thing it has)``.
_NO_COUNTERPART: Final[tuple[tuple[tuple[str, ...], str, str], ...]] = (
    (
        ("autoReview", "triggerOnUpdates", "skipReview", "triggerOnDrafts"),
        "when a review runs is decided by the workflow, not by a policy file",
        "the workflow's on: triggers and the Action inputs on-push and review-drafts",
    ),
    (
        (
            "ignoreKeywords",
            "labels",
            "disabledLabels",
            "includeBranches",
            "excludeBranches",
            "includeAuthors",
            "excludeAuthors",
        ),
        "which pull requests are reviewed is decided by the workflow's own filters",
        "branches:, paths-ignore: and an if: condition on the job (the setup guide's Filters of the workflow)",
    ),
    (
        ("effort",),
        "LemonCrow's depth is its own ladder (auto, thorough, deep), not the other tool's tiers",
        "review.effort in .lemoncrow/review.yaml, or the Action's depth input",
    ),
    (
        ("statusCheck", "statusCommentsEnabled"),
        "the LemonCrow Review commit status is always set",
        "the Action's fail-on input, which decides whether a P0 fails it",
    ),
    (
        ("shouldUpdateDescription",),
        "LemonCrow edits only its own summary comment, never the pull request description",
        "none; the overview is in the summary comment",
    ),
    (
        ("fixWithAI",),
        "a collapsed fix prompt is always part of the summary and of an inline comment",
        "none; it cannot be turned off",
    ),
    (
        (
            "summarySection",
            "issuesTableSection",
            "confidenceScoreSection",
            "sequenceDiagramSection",
            "commentsOutsideDiffSection",
            "includeConfidenceScore",
        ),
        "the summary's layout is fixed",
        "the Action's diagram input (auto or off) for the diagram",
    ),
    (("hideFooter", "comment"), "the footer and the comment text are fixed", "none"),
    (
        ("autoApprove",),
        "LemonCrow never approves a pull request: its reviews are comments only",
        "the LemonCrow Review status and the Action's fail-on input",
    ),
    (("patternRepositories", "context"), "LemonCrow reads only this repository", "none"),
    (
        ("customContext",),
        "it is a legacy container for rules and context files",
        "copy its rules into a file import.rules_files lists, and its files into import.agent_docs_globs",
    ),
    (
        ("disabledRules",),
        "it switches off a rule a parent folder's config defines, and LemonCrow has no folder cascade",
        "enabled: false on the rule itself",
    ),
)
_DROPPED: Final[Mapping[str, tuple[str, str]]] = {
    key: (why, closest) for keys, why, closest in _NO_COUNTERPART for key in keys
}

_HANDLERS: Final[Mapping[str, Callable[[_Builder, object], None]]] = {
    "strictness": _strictness,
    "ignorePatterns": _ignore,
    "commentTypes": _comment_types,
    "instructions": _instructions,
    "fileChangeLimit": _file_limit,
    "updateSummaryOnly": _summary_only,
}
#: Every key of a settings file this module has something to say about (``rules`` is the loader's, plus its severities).
KNOWN_KEYS: Final[frozenset[str]] = frozenset({*_HANDLERS, *_DROPPED})


# --------------------------------------------------------------------------- #
# a rule's severity
# --------------------------------------------------------------------------- #


def _rule_severities(b: _Builder, rows: object, path: str) -> None:
    """One report row per severity word, for the rules the loader will actually import; the key written is the loader's."""

    from lemoncrow.pro.capabilities.review.config_import import _key_id, _scope_globs

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return
    buckets: dict[tuple[str, str], list[str]] = {}
    invalid: list[tuple[str, str]] = []
    severities: dict[str, dict[str, str]] = {}
    for entry in rows:
        if not isinstance(entry, Mapping) or "severity" not in entry:
            continue
        raw_id = entry.get("id")
        rule_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else ""
        if not rule_id or not isinstance(entry.get("rule"), str) or not entry["rule"].strip():
            continue
        if entry.get("enabled", True) is False:
            continue
        try:
            _scope_globs(entry.get("scope"))
        except ValueError:
            continue
        key = f"imported:{path}:{_key_id(rule_id)}"
        word = str(entry["severity"]).strip().lower()
        level = _SEVERITY_WORDS.get(word)
        if level is None:
            invalid.append((rule_id, word))
            continue
        level = _clamped(level)
        severities[key] = {"severity": level}
        buckets.setdefault((word, level), []).append(rule_id)
    if severities:
        b.review["rules"] = severities
        for key in severities:
            b.need("review", "rules", key)
    for (word, level), ids in buckets.items():
        outcome: Outcome = "carried" if word in ("p1", "p2") else "mapped"
        b.row(
            f"severity: {word} ({_count(ids, 'rule')}: {_names(ids)})",
            outcome,
            f"review.rules.<key>.severity: {level}",
            _severity_detail(word, level),
        )
    if invalid:
        listed = _names([f"{rule_id} {word!r}" for rule_id, word in invalid])
        b.row(
            f"severity ({_count(invalid, 'rule')}: {listed})",
            "dropped",
            detail="Not low, medium or high, so no severity was set and the ordinary ladder applies. "
            "Closest: review.rules.<key>.severity (p1 or p2).",
        )


def _clamped(level: str) -> str:
    """*level* as a settings file may set it: p1 or p2, never p0 (the severity ladder gives a P0 to evidence, not to a file)."""

    return level if level in ("p1", "p2") else SEVERITY_CEILING


def _count(items: Sequence[object], noun: str) -> str:
    return f"{len(items)} {noun}{'s' if len(items) != 1 else ''}"


def _severity_detail(word: str, level: str) -> str:
    if word in _PAST_THE_CEILING:
        return (
            "Asked for P0, written as p1: P0 needs a critical rule or evidence from outside the model's own code "
            "(criticality.severity_for), which a settings file is not. A finding citing the rule is P1."
        )
    if level == "p1":
        return (
            "p1 is the most a settings file can set: P0 needs a critical rule or evidence from outside the model's own "
            "code (criticality.severity_for). A finding citing the rule is P1."
        )
    return "LemonCrow has no rule severity below P2, so medium and low both become p2. A finding citing the rule is P2."


def translate(raw: Mapping[str, Any], *, path: str) -> SettingsPlan:
    """The plan for the settings file *raw* (a parsed JSON or YAML object) that lives at repository path *path*.

    *path* is the file's repository-relative path: it is part of the key a rule gets when ``import.rules_files`` lists the
    file, so it is what ``review.rules.<key>.severity`` is written against.
    """

    b = _Builder()
    for key in _HANDLERS:
        if key in raw:
            _HANDLERS[key](b, raw[key])
    for key in sorted(str(key) for key in raw if str(key) in _DROPPED):
        why, closest = _DROPPED[key]
        b.row(key, "dropped", detail=f"{why[:1].upper()}{why[1:]}, so nothing was set. Closest: {closest}.")
    for key in sorted(str(key) for key in raw if str(key) not in KNOWN_KEYS and key != "rules"):
        b.row(key, "dropped", detail="LemonCrow does not read this key; ignored.")
    if "rules" in raw:
        _rule_severities(b, raw["rules"], path)
    return b.plan()


# --------------------------------------------------------------------------- #
# what review.yaml already holds, and the note a rules file's setting gets
# --------------------------------------------------------------------------- #


def configured_settings(content: str) -> dict[str, Mapping[str, Any]]:
    """The ``review:`` and ``import:`` blocks of a ``.lemoncrow/review.yaml`` text; empty when it does not parse."""

    try:
        document = yaml.safe_load(content) if content.strip() else None
    except (yaml.YAMLError, RecursionError):
        return {}
    if not isinstance(document, Mapping):
        return {}
    return {name: document[name] for name in ("review", "import") if isinstance(document.get(name), Mapping)}


def _is_set(configured: Mapping[str, Mapping[str, Any]], need: Need) -> bool:
    section = configured.get(need[0])
    if section is None:
        return False
    name = need[1]
    if name == "strictness":
        return "strictness" in section or "finding_threshold" in section
    if name == "rules":
        rules = section.get("rules")
        row = rules.get(need[2]) if isinstance(rules, Mapping) else None
        return isinstance(row, Mapping) and "severity" in row
    if name == "agent_docs_globs":
        listed = section.get(name)
        listed = [listed] if isinstance(listed, str) else listed
        return isinstance(listed, list) and need[2] in listed
    return name in section


def setting_note(
    key: str, value: object, *, path: str, configured: Mapping[str, Mapping[str, Any]]
) -> tuple[Literal["info", "warning"], str] | None:
    """The note for a setting *key* found in the rules file *path*, or ``None`` when *key* is not a setting this knows.

    ``info`` when ``.lemoncrow/review.yaml`` (*configured*, from :func:`configured_settings`) already carries what the
    setting maps to, or when LemonCrow needs nothing set for it; otherwise a ``warning`` that says what was not applied,
    where it would go, and the command that writes it.
    """

    if key not in KNOWN_KEYS:
        return None
    plan = translate({key: value}, path=path)
    dropped = [row for row in plan.rows if row.outcome == "dropped"]
    carried = all(_is_set(configured, need) for need in plan.needs)
    if not dropped and carried:
        return "info", f"{key}: carried by {REVIEW_CONFIG}" if plan.needs else f"{key}: nothing to set in LemonCrow"
    if carried:  # the part LemonCrow can take is in review.yaml; say what it cannot
        return "warning", f"{key}: {' '.join(row.detail for row in dropped)}"
    shown = " ".join(row.sentence() for row in plan.rows if row.target != "built in")
    return (
        "warning",
        f"{key} is a setting, and a rules file holds rules, so it is not applied here. {shown} "
        "Run `lc review config import <folder>` to write it.",
    )


def severity_note(
    rule_id: str, value: object, *, path: str, configured: Mapping[str, Mapping[str, Any]]
) -> tuple[Literal["info", "warning"], str]:
    """The note for a rule entry's ``severity`` in the rules file *path*: carried by ``review.rules`` or not applied."""

    plan = translate({"rules": [{"id": rule_id, "rule": "x", "severity": value}]}, path=path)
    label = f"rule {rule_id!r} severity"
    if any(row.outcome == "dropped" for row in plan.rows):
        return "warning", f"{label}: {' '.join(row.detail for row in plan.rows)}"
    if all(_is_set(configured, need) for need in plan.needs):
        return "info", f"{label}: carried by {REVIEW_CONFIG}"
    key = plan.needs[0][2] if plan.needs else ""
    shown = " ".join(row.sentence() for row in plan.rows).replace("<key>", key)
    return (
        "warning",
        f"{label} is not applied from a rules file. {shown} Run `lc review config import <folder>` to write it.",
    )


__all__ = [
    "INSTRUCTIONS_PATH",
    "KNOWN_KEYS",
    "SEVERITY_CEILING",
    "ReportRow",
    "SettingsPlan",
    "configured_settings",
    "gitignore_globs",
    "setting_note",
    "severity_note",
    "translate",
]
