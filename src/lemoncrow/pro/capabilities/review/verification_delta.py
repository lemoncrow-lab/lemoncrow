"""Verification weakening and spec deltas (Phase 9R items 26 and 28).

Both analyses read the exact base and head text of a Snapshot's changed files
and never execute anything.

*Verification weakening* is a change that makes the existing checks prove
less: newly skipped/xfail/disabled/focused tests, removed or loosened
assertions, new lint/type/security/coverage suppressions, lowered coverage or
quality thresholds and grown ignore lists, bulk snapshot/golden rewrites, and
removed or newly conditional CI steps. Every weakening delta carries the typed
fact ``("auto_approval", "blocked")``; :func:`auto_approval_blockers` is the
seam Phase 10 auto-approval consumes. A weakening blocks auto-approval whatever
its judgment: accepting it is a human decision, which auto-approval is not.

A *spec delta* renders added, removed, renamed and changed test cases and
changed snapshot/golden files as behavior statements (``refresh with an expired
token: now expects 403 (was 401)``) instead of test-file churn. Deterministic
only; the behavior narrative (one budgeted synthesis call, ``behavior.py``) may
rephrase them and falls back to these lines.

Pure functions; ``routine.py`` keeps refusing directive comments as routine,
this module makes the new ones visible.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any

import yaml

from lemoncrow.pro.capabilities.review.behavior import (
    BehaviorDelta,
    BehaviorObservation,
    behavior_key,
)

VERIFICATION_ANALYSIS_VERSION = 1
"""Bump on any change to what counts as weakening or how a spec statement reads."""

AUTO_APPROVAL_FACT = "auto_approval"
AUTO_APPROVAL_BLOCKED = "blocked"

_MAX_BYTES = 1 << 20
_MAX_STATEMENTS = 12
_BULK_SNAPSHOT_FILES = 3
_BULK_SNAPSHOT_ENTRIES = 5
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True, slots=True)
class ChangedText:
    """One changed file of a Snapshot with the exact text of both sides.

    ``base``/``head`` are ``""`` on the side where the file does not exist.
    ``category`` is :func:`~lemoncrow.pro.capabilities.review.gitdiff.classify_path`.
    """

    path: str
    old_path: str
    status: str
    base: str
    head: str
    category: str = ""


@dataclass(frozen=True, slots=True)
class AutoApprovalBlocker:
    """A behavior target that makes a Snapshot ineligible for auto-approval."""

    code: str
    behavior_key: str
    title: str
    paths: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {"code": self.code, "behavior_key": self.behavior_key, "title": self.title, "paths": list(self.paths)}


def auto_approval_blockers(deltas: Iterable[BehaviorDelta]) -> tuple[AutoApprovalBlocker, ...]:
    """Every delta carrying the typed auto-approval-blocking fact, judged or not."""

    return tuple(
        AutoApprovalBlocker(
            code=delta.kind,
            behavior_key=behavior_key(delta),
            title=delta.title,
            paths=delta.paths,
        )
        for delta in deltas
        if (AUTO_APPROVAL_FACT, AUTO_APPROVAL_BLOCKED) in delta.facts
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def _basename(path: str) -> str:
    return PurePosixPath(path).name


def _comment_only(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(("#", "//", "/*", "*")) and not stripped.startswith("#[")


# --------------------------------------------------------------------------- #
# path classes
# --------------------------------------------------------------------------- #

_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|e2e|integration_tests)/|(?:^|/)test_[^/]+$|_test\.[a-z]+$|\.(?:test|spec)\.[a-z]+$"
    r"|Tests?\.(?:java|kt|cs)$"
)
_SNAPSHOT_PATH = re.compile(
    r"(?:^|/)(?:__snapshots__|__screenshots__|goldens?|__golden__)/|-snapshots/|\.(?:snap|ambr|golden)$|\.approved\."
)
_GHA_PATH = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
_CI_PATHS = re.compile(
    r"^(?:\.gitlab-ci\.ya?ml|\.circleci/config\.ya?ml|azure-pipelines\.ya?ml|bitbucket-pipelines\.ya?ml|"
    r"\.travis\.ya?ml|Jenkinsfile|\.buildkite/.+\.ya?ml|\.pre-commit-config\.ya?ml)$"
)
_THRESHOLD_BASENAMES = re.compile(
    r"^(?:pyproject\.toml|setup\.cfg|tox\.ini|mypy\.ini|\.mypy\.ini|\.coveragerc|pytest\.ini|\.pylintrc|\.flake8|"
    r"tsconfig[\w.-]*\.json|jest\.config\.[cm]?[jt]s|vitest\.config\.[cm]?[jt]s|vite\.config\.[cm]?[jt]s|"
    r"\.nycrc(?:\.json|\.ya?ml)?|\.?codecov\.ya?ml|\.eslintrc(?:\.\w+)?|eslint\.config\.[cm]?[jt]s|\.?ruff\.toml|"
    r"package\.json|sonar-project\.properties|\.golangci\.ya?ml|clippy\.toml|Cargo\.toml|\.rubocop\.ya?ml|"
    r"phpstan\.neon(?:\.dist)?|Makefile)$"
)
_CODE_SUFFIXES = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".go", ".rs", ".rb"}
    | {".java", ".kt", ".kts", ".scala", ".cs", ".php", ".swift", ".sh", ".vue", ".svelte"}
)


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path)) and _suffix(path) in _CODE_SUFFIXES


def is_snapshot_path(path: str) -> bool:
    return bool(_SNAPSHOT_PATH.search(path))


def is_ci_path(path: str) -> bool:
    return bool(_GHA_PATH.match(path) or _CI_PATHS.match(path))


def is_threshold_path(path: str) -> bool:
    return bool(_THRESHOLD_BASENAMES.match(_basename(path)))


def verification_relevant(path: str) -> bool:
    """Whether these analyses read *path* at all."""

    return _suffix(path) in _CODE_SUFFIXES or is_snapshot_path(path) or is_ci_path(path) or is_threshold_path(path)


# --------------------------------------------------------------------------- #
# weakening
# --------------------------------------------------------------------------- #

_SKIP_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "skipped",
        re.compile(
            r"@pytest\.mark\.(?:skip|skipif)\b|@unittest\.skip\w*|\bpytest\.skip\s*\(|\bself\.skipTest\s*\("
            r"|\b(?:it|test|describe|context|suite)\.(?:skip|todo|fixme)\s*\(|\b(?:xit|xtest|xdescribe|xcontext)\s*\("
            r"|\bt\.Skip(?:f|Now)?\s*\(|#\[ignore\b|@(?:Disabled|Ignore)\b|^\s*(?:xit|xspecify|skip|pending)\b"
        ),
    ),
    ("expected_failure", re.compile(r"@pytest\.mark\.xfail\b|\bpytest\.xfail\s*\(|@unittest\.expectedFailure\b")),
    ("focused", re.compile(r"\b(?:it|test|describe|context|suite)\.only\s*\(|\b(?:fit|fdescribe)\s*\(")),
)
_SKIP_LABELS = {
    "skipped": "newly skipped or disabled",
    "expected_failure": "newly marked as expected to fail",
    "focused": "newly focused with .only (other tests in the file stop running)",
}
_FIRST_STRING = re.compile(r"""["'`]([^"'`]{1,160})["'`]""")
_DEF_NAME = re.compile(r"\b(?:def|fn|func|void|fun)\s+(`[^`]{1,120}`|\w+)")

_ASSERTION = re.compile(
    r"^\s*assert\b|\bself\.assert\w+\s*\(|\bpytest\.raises\s*\(|\bexpect\s*\(|\bassert\w*!\s*\("
    r"|\b(?:assert|require)\.\w+\s*\(|\bassert\s*\(|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\("
    r"|\bAssert\.\w+\s*\(|\bassert(?:Equals|True|False|Null|NotNull|That|Throws)\s*\("
)
_STRICT_ASSERTION = re.compile(
    r"\.(?:toBe|toEqual|toStrictEqual|toHaveBeenCalledWith|toHaveBeenCalledTimes|toThrow|toHaveLength|"
    r"toContainEqual|toMatchObject)\s*\(|\bassert(?:Equal|Equals|Is|In|Raises|CountEqual|DictEqual|ListEqual|"
    r"AlmostEqual|Regex)\s*\(|\bassert_(?:eq|ne)!|^\s*assert\b[^#\n]*(?:==|!=|<=|>=|<|>|\bin\b|\bis\b)"
    r"|\bassert_called(?:_once)?_with\s*\(|\bpytest\.raises\s*\(|\b(?:assert|require)\.(?:Equal|Exactly|ErrorIs|Len)\s*\("
)
_WEAK_ASSERTION = re.compile(
    r"\.(?:toBeTruthy|toBeDefined|toBeFalsy|toBeUndefined|toBeNull|toBeInstanceOf)\s*\(|\bexpect\.(?:anything|any)\s*\("
    r"|\bassert(?:True|False|IsNotNone|IsNone)\s*\(|\bassert_called\s*\("
    r"|\b(?:assert|require)\.(?:True|False|NotNil|Nil|NotEmpty)\s*\(|^\s*assert\s+[\w.()\[\]'\"]+\s*(?:#.*)?$"
)

_COMMENT_SUPPRESSIONS: tuple[tuple[str, str, str], ...] = (
    ("type", "type: ignore", r"type:\s*ignore\b"),
    ("type", "pyright: ignore", r"pyright:\s*ignore\b"),
    ("type", "mypy: ignore-errors", r"mypy:\s*ignore-errors\b"),
    ("type", "@ts-ignore", r"@ts-ignore\b"),
    ("type", "@ts-expect-error", r"@ts-expect-error\b"),
    ("type", "@ts-nocheck", r"@ts-nocheck\b"),
    ("lint", "noqa", r"(?i:noqa)\b"),
    ("lint", "pylint: disable", r"pylint:\s*disable\b"),
    ("lint", "eslint-disable", r"eslint-disable"),
    ("lint", "nolint", r"nolint\b"),
    ("lint", "rubocop:disable", r"rubocop:\s*disable\b"),
    ("lint", "biome-ignore", r"biome-ignore\b"),
    ("lint", "deno-lint-ignore", r"deno-lint-ignore\b"),
    ("lint", "NOSONAR", r"NOSONAR\b"),
    ("security", "nosec", r"nosec\b"),
    ("security", "nosemgrep", r"nosemgrep\b"),
    ("security", "gitleaks:allow", r"gitleaks:allow\b"),
    ("security", "allowlist secret", r"pragma:\s*allowlist\s+secret\b"),
    ("security", "lgtm/codeql", r"(?:lgtm|codeql)\s*\["),
    ("coverage", "pragma: no cover", r"pragma:\s*no\s*cover\b"),
    ("coverage", "istanbul ignore", r"istanbul\s+ignore\b"),
    ("coverage", "c8/v8 ignore", r"\b[cv]8\s+ignore\b"),
)
# A directive only counts inside a comment; code that merely names one does not.
_SUPPRESSION_RES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    *(
        (group, label, re.compile(r"(?:#|//|/\*|<!--|\{/\*)[^\n]*?" + pattern))
        for group, label, pattern in _COMMENT_SUPPRESSIONS
    ),
    ("lint", "#[allow(...)]", re.compile(r"^\s*#!?\[allow\(")),
    ("lint", "@SuppressWarnings", re.compile(r"@SuppressWarnings\s*\(")),
)
_SUPPRESSION_LABELS = {"type": "type-check", "lint": "lint", "security": "security", "coverage": "coverage"}

_KEY_VALUE = re.compile(r"""^\s*["']?([A-Za-z_@][\w.@/-]*)["']?\s*[:=]\s*["']?([^"',\s#}\]]+)""")
_CLI_THRESHOLDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cov_fail_under", re.compile(r"--cov-fail-under[= ](\d+(?:\.\d+)?)")),
    ("fail_under", re.compile(r"--fail-under[= ](\d+(?:\.\d+)?)")),
    ("max_warnings", re.compile(r"--max-warnings[= ](\d+)")),
)
_HIGHER_IS_STRICTER = frozenset(
    {"fail_under", "cov_fail_under", "minimum_coverage", "min_coverage", "coverage_threshold", "target"}
)
_COVERAGE_KEYS = frozenset({"branches", "functions", "lines", "statements"})
_COVERAGE_FILES = re.compile(r"^(?:jest|vitest|vite)\.config\.|^\.nycrc|^package\.json$")
_LOWER_IS_STRICTER = frozenset({"max_warnings", "max_complexity", "max_issues", "max_errors", "threshold"})
_STRICT_FLAGS = frozenset(
    {
        "strict",
        "noimplicitany",
        "strictnullchecks",
        "strictfunctiontypes",
        "noimplicitreturns",
        "nounusedlocals",
        "nounusedparameters",
        "nouncheckedindexedaccess",
        "exactoptionalpropertytypes",
        "disallow_untyped_defs",
        "disallow_any_generics",
        "disallow_incomplete_defs",
        "check_untyped_defs",
        "warn_unused_ignores",
        "warn_return_any",
        "no_implicit_optional",
        "strict_optional",
        "fail_on_warnings",
        "warnings_as_errors",
        "werror",
        "treat_warnings_as_errors",
    }
)
_LOOSE_FLAGS = frozenset({"ignore_errors", "ignore_missing_imports", "allow_failure", "continue_on_error"})
_IGNORE_KEYS = frozenset(
    {
        "ignore",
        "extend_ignore",
        "per_file_ignores",
        "omit",
        "exclude",
        "extend_exclude",
        "ignorepatterns",
        "testpathignorepatterns",
        "coveragepathignorepatterns",
        "norecursedirs",
        "collect_ignore",
        "ignore_paths",
        "exclude_lines",
        "deselect",
        "disable",
    }
)
_ESLINT_RULE = re.compile(r"""["']([\w@/-]+)["']\s*:\s*\[?\s*["']?(error|warn|off|0|1|2)\b""")
_ESLINT_RANK = {"error": 2, "2": 2, "warn": 1, "1": 1, "off": 0, "0": 0}
_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})


def _norm_key(key: str) -> str:
    return key.strip().lower().replace("-", "_").rsplit(".", 1)[-1]


def _number(value: str) -> float | None:
    try:
        return float(value.rstrip("%"))
    except ValueError:
        return None


def _weakening(
    producer: str,
    *,
    rule: str,
    scope: str,
    title: str,
    summary: str,
    base_state: object,
    head_state: object,
    paths: Sequence[str],
    details: Sequence[tuple[str, str]] = (),
    change: str = "changed",
) -> BehaviorDelta:
    return BehaviorDelta(
        kind="verification_weakening",
        producer=producer,
        subject=f"weakening:{rule}:{scope}",
        title=title,
        summary=summary,
        base=BehaviorObservation("base", status="before", digest=_digest(base_state)[:32], summary=_brief(base_state)),
        head=BehaviorObservation("head", status="after", digest=_digest(head_state)[:32], summary=_brief(head_state)),
        change="added" if change == "added" else "changed",
        severity="high",
        critical=True,
        paths=tuple(dict.fromkeys(paths)),
        facts=(
            (AUTO_APPROVAL_FACT, AUTO_APPROVAL_BLOCKED),
            ("rule", rule),
            *details[: _MAX_STATEMENTS - 2],
        ),
        environment_fingerprint=f"static-verification:v{VERIFICATION_ANALYSIS_VERSION}",
    )


def _brief(state: object) -> str:
    if isinstance(state, Mapping):
        return "; ".join(f"{key}: {value}" for key, value in list(state.items())[:6])[:300]
    if isinstance(state, (list, tuple)):
        return ", ".join(str(item) for item in list(state)[:6])[:300]
    return str(state)[:300]


def _plural(count: int, one: str, many: str = "") -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _marker_names(text: str) -> dict[str, list[str]]:
    """Marker kind -> the test each marker applies to (best effort, in order)."""

    lines = text.splitlines()
    out: dict[str, list[str]] = {kind: [] for kind, _ in _SKIP_MARKERS}
    for index, line in enumerate(lines):
        if _comment_only(line) and not line.lstrip().startswith("#["):
            continue
        decorator = line.lstrip().startswith(("@", "#["))
        for kind, pattern in _SKIP_MARKERS:
            for match in pattern.finditer(line):
                name = ""
                if not decorator:
                    quoted = _FIRST_STRING.search(line[match.end() :])
                    name = quoted.group(1) if quoted else ""
                if not name:
                    # A decorator/attribute applies to the next definition.
                    for ahead in lines[index : index + 6]:
                        found = _DEF_NAME.search(ahead)
                        if found:
                            name = found.group(1).strip("`")
                            break
                if not name:
                    for behind in reversed(lines[max(0, index - 40) : index]):
                        found = _DEF_NAME.search(behind)
                        if found:
                            name = found.group(1).strip("`")
                            break
                out[kind].append(name or f"line {index + 1}")
    return out


def _assertion_counts(text: str) -> tuple[int, int, int]:
    total = strict = weak = 0
    for line in text.splitlines():
        if not line.strip() or _comment_only(line):
            continue
        if _ASSERTION.search(line):
            total += 1
        if _STRICT_ASSERTION.search(line):
            strict += 1
        elif _WEAK_ASSERTION.search(line):
            weak += 1
    return total, strict, weak


def _suppression_counts(text: str) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = {}
    for line in text.splitlines():
        for group, label, pattern in _SUPPRESSION_RES:
            if pattern.search(line):
                out.setdefault(group, Counter())[label] += 1
    return out


def _settings(path: str, text: str) -> tuple[dict[str, str], set[str], dict[str, int]]:
    """``(key -> value, ignore-list entries, eslint rule -> severity rank)``."""

    values: dict[str, str] = {}
    ignores: set[str] = set()
    rules: dict[str, int] = {}
    name = _basename(path)
    eslint = name.startswith((".eslintrc", "eslint.config."))
    collecting = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", ";")):
            continue
        for key, pattern in _CLI_THRESHOLDS:
            for found in pattern.finditer(line):
                values[key] = found.group(1)
        if eslint:
            for found in _ESLINT_RULE.finditer(line):
                rules[found.group(1)] = _ESLINT_RANK[found.group(2)]
        match = _KEY_VALUE.match(line)
        key = _norm_key(match.group(1)) if match else ""
        if match and key:
            values.setdefault(key, match.group(2))
        if key in _IGNORE_KEYS or collecting:
            tail = line.split("=", 1)[1] if ("=" in line and key in _IGNORE_KEYS) else line
            if key in _IGNORE_KEYS and ":" in tail and "=" not in line:
                tail = tail.split(":", 1)[1]
            for token in re.findall(r"""["']([^"']{1,200})["']""", tail) or [
                part.strip() for part in re.split(r"[,\s]+", tail.strip(" []{}")) if part.strip()
            ]:
                ignores.add(f"{collecting or key}:{token}")
            if key in _IGNORE_KEYS:
                opened = tail.count("[") - tail.count("]")
                collecting = key if opened > 0 or (tail.strip() == "" and "=" in line) else ""
            elif "]" in line or (collecting and not line.startswith((" ", "\t"))):
                collecting = ""
    return values, ignores, rules


def _threshold_changes(path: str, base: str, head: str) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """``(lowered thresholds/flags, new ignore entries, lowered lint rules)``."""

    before, before_ignores, before_rules = _settings(path, base)
    after, after_ignores, after_rules = _settings(path, head)
    coverage_file = bool(_COVERAGE_FILES.match(_basename(path)))
    lowered: list[tuple[str, str]] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new or old is None:
            continue
        old_num = _number(old)
        new_num = None if new is None else _number(new)
        stricter_high = key in _HIGHER_IS_STRICTER or (coverage_file and key in _COVERAGE_KEYS)
        if stricter_high and old_num is not None and (new is None or (new_num is not None and new_num < old_num)):
            lowered.append((key, f"{old} → {new if new is not None else 'removed'}"))
        elif key in _LOWER_IS_STRICTER and old_num is not None and new_num is not None and new_num > old_num:
            lowered.append((key, f"{old} → {new}"))
        elif key in _STRICT_FLAGS and old.lower() in _TRUE and (new is None or new.lower() in _FALSE):
            lowered.append((key, f"{old} → {new if new is not None else 'removed'}"))
        elif key in _LOOSE_FLAGS and new is not None and new.lower() in _TRUE and old.lower() not in _TRUE:
            lowered.append((key, f"{old} → {new}"))
    for key in sorted(set(after) - set(before)):
        new = after[key]
        if key in _LOOSE_FLAGS and new.lower() in _TRUE:
            lowered.append((key, f"unset → {new}"))
    rules = [
        f"{rule}: {before_rules[rule]}→{after_rules.get(rule, 0)}"
        for rule in sorted(before_rules)
        if rule in after_rules and after_rules[rule] < before_rules[rule]
    ]
    return lowered, sorted(after_ignores - before_ignores), rules


def _gha_steps(text: str) -> tuple[dict[str, bool], dict[str, bool]]:
    """``(job -> conditional, "job/step" -> conditional)`` of a workflow."""

    try:
        doc = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError:
        return {}, {}
    jobs_out: dict[str, bool] = {}
    steps_out: dict[str, bool] = {}
    jobs = doc.get("jobs") if isinstance(doc, Mapping) else None
    for job_id, job in jobs.items() if isinstance(jobs, Mapping) else ():
        if not isinstance(job, Mapping):
            continue
        jobs_out[str(job_id)] = "if" in job or bool(job.get("continue-on-error"))
        for step in job.get("steps") or () if isinstance(job.get("steps"), list) else ():
            if not isinstance(step, Mapping):
                continue
            label = " ".join(str(step.get("run") or step.get("uses") or step.get("name") or "").split())[:160]
            if label:
                steps_out[f"{job_id}/{label}"] = "if" in step or bool(step.get("continue-on-error"))
    return jobs_out, steps_out


_GITLAB_RESERVED = frozenset(
    {"stages", "variables", "default", "include", "workflow", "image", "services", "before_script", "after_script"}
    | {"cache", "pages"}
)


def _gitlab_jobs(text: str) -> dict[str, bool]:
    try:
        doc = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError:
        return {}
    out: dict[str, bool] = {}
    for key, job in doc.items() if isinstance(doc, Mapping) else ():
        name = str(key)
        if name in _GITLAB_RESERVED or name.startswith(".") or not isinstance(job, Mapping):
            continue
        out[name] = bool(job.get("allow_failure")) or job.get("when") == "manual"
    return out


def _precommit_hooks(text: str) -> set[str]:
    try:
        doc = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError:
        return set()
    hooks: set[str] = set()
    repos = doc.get("repos") if isinstance(doc, Mapping) else None
    for repo in repos if isinstance(repos, list) else ():
        for hook in repo.get("hooks") or () if isinstance(repo, Mapping) else ():
            if isinstance(hook, Mapping) and hook.get("id"):
                hooks.add(str(hook["id"]))
    return hooks


def _check_commands(text: str) -> Counter[str]:
    from lemoncrow.pro.capabilities.review.development import classify_check

    out: Counter[str] = Counter()
    for line in text.splitlines():
        command = " ".join(line.strip().lstrip("-").strip().split())
        if command and not command.startswith("#") and classify_check(command):
            out[command[:160]] += 1
    return out


def _ci_deltas(producer: str, item: ChangedText) -> list[BehaviorDelta]:
    path = item.path
    out: list[BehaviorDelta] = []
    removed: list[str] = []
    conditional: list[str] = []
    if _GHA_PATH.match(path):
        base_jobs, base_steps = _gha_steps(item.base)
        head_jobs, head_steps = _gha_steps(item.head)
        removed = [f"job {job}" for job in sorted(set(base_jobs) - set(head_jobs))]
        removed += [
            f"step {step}" for step in sorted(set(base_steps) - set(head_steps)) if step.split("/", 1)[0] in head_jobs
        ]
        conditional = [f"job {job}" for job in sorted(head_jobs) if head_jobs[job] and base_jobs.get(job) is False]
        conditional += [
            f"step {step}" for step in sorted(head_steps) if head_steps[step] and base_steps.get(step) is False
        ]
    elif _basename(path).startswith(".pre-commit-config"):
        removed = [f"hook {hook}" for hook in sorted(_precommit_hooks(item.base) - _precommit_hooks(item.head))]
    elif _basename(path).startswith(".gitlab-ci"):
        base_jobs = _gitlab_jobs(item.base)
        head_jobs = _gitlab_jobs(item.head)
        removed = [f"job {job}" for job in sorted(set(base_jobs) - set(head_jobs))]
        conditional = [f"job {job}" for job in sorted(head_jobs) if head_jobs[job] and base_jobs.get(job) is False]
    else:
        gone = _check_commands(item.base) - _check_commands(item.head)
        removed = [f"command {command}" for command in sorted(gone)]
    if removed:
        out.append(
            _weakening(
                producer,
                rule="ci_step_removed",
                scope=path,
                title=f"{_plural(len(removed), 'CI step')} removed in {path}",
                summary="Removed: " + "; ".join(removed[:6]) + (" …" if len(removed) > 6 else ""),
                base_state={"present": removed},
                head_state={"removed": removed},
                paths=(path,),
                details=[(f"removed {index + 1}", value) for index, value in enumerate(removed[:8])],
            )
        )
    if conditional:
        out.append(
            _weakening(
                producer,
                rule="ci_step_conditional",
                scope=path,
                title=f"{_plural(len(conditional), 'CI step')} newly conditional in {path}",
                summary="Now conditional or allowed to fail: " + "; ".join(conditional[:6]),
                base_state={"unconditional": conditional},
                head_state={"conditional": conditional},
                paths=(path,),
                details=[(f"conditional {index + 1}", value) for index, value in enumerate(conditional[:8])],
            )
        )
    return out


def _snapshot_entries(path: str, text: str) -> dict[str, str]:
    """Snapshot entry name -> content digest (jest ``.snap``, syrupy ``.ambr``)."""

    pattern: re.Pattern[str] | None = None
    if path.endswith(".snap"):
        pattern = re.compile(r"^exports\[`(.+?)`\]\s*=", re.M)
    elif path.endswith(".ambr"):
        pattern = re.compile(r"^# name: (.+)$", re.M)
    if pattern is None:
        return {}
    matches = list(pattern.finditer(text))
    out: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        out[match.group(1).strip()] = hashlib.sha256(text[match.end() : end].encode()).hexdigest()[:16]
    return out


# A vanished test whose lines mostly survive under a new name in the same file is that test, renamed.
_RENAMED_CASE_RATIO = 0.6
# lc-debt: pairs vanished and new tests only up to this many comparisons; past it each vanished test counts as lost (the loud side); index bodies by line to go further.
_MAX_RENAME_COMPARISONS = 2_500


def _renamed_to(case: TestCase, candidates: Mapping[str, TestCase]) -> str | None:
    """The key of the new test *case* most resembles, when it resembles one enough."""

    lines = case.body.splitlines()
    matcher = SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(lines)
    best, best_ratio = None, _RENAMED_CASE_RATIO
    for key, other in candidates.items():
        matcher.set_seq1(other.body.splitlines())
        if matcher.quick_ratio() >= best_ratio and (ratio := matcher.ratio()) >= best_ratio:
            best, best_ratio = key, ratio
    return best


def _lost_assertions(item: ChangedText, moved_bodies: frozenset[str]) -> tuple[int, list[str]]:
    """Assertions the change takes away, per test case: ``(count, test names)``.

    A case that disappears with its body reappearing anywhere in the Snapshot
    was renamed or moved, not deleted; one whose body mostly reappears under a
    new name in the same file is compared with that test. Without recognizable
    cases the file's assertion count is compared instead.
    """

    before = test_cases(item.old_path or item.path, item.base)
    if not before:
        old, new = _assertion_counts(item.base)[0], _assertion_counts(item.head)[0]
        return max(0, old - new), []
    after = test_cases(item.path, item.head)
    unpaired = {key: case for key, case in after.items() if key not in before}
    if sum(key not in after for key in before) * len(unpaired) > _MAX_RENAME_COMPARISONS:
        unpaired = {}
    lost = 0
    names: list[str] = []
    for key, case in before.items():
        old = _assertion_counts(case.body)[0]
        if key in after:
            gone = max(0, old - _assertion_counts(after[key].body)[0])
        elif case.body and case.body in moved_bodies:
            gone = 0
        else:
            twin = _renamed_to(case, unpaired)
            gone = old if twin is None else max(0, old - _assertion_counts(unpaired.pop(twin).body)[0])
        if gone:
            lost += gone
            names.append(case.prose)
    return lost, names


def weakening_deltas(producer: str, files: Sequence[ChangedText]) -> list[BehaviorDelta]:
    """Every verification weakening among *files* (base vs head)."""

    out: list[BehaviorDelta] = []
    rewritten_snapshots: list[str] = []
    moved_bodies = frozenset(
        case.body
        for item in files
        if is_test_path(item.path) and len(item.head) <= _MAX_BYTES
        for case in test_cases(item.path, item.head).values()
        if case.body
    )
    for item in files:
        if len(item.base) > _MAX_BYTES or len(item.head) > _MAX_BYTES:
            continue
        path = item.path
        suffix = _suffix(path)
        if is_snapshot_path(path) or is_snapshot_path(item.old_path):
            if item.status in {"modified", "deleted", "renamed"} and item.base:
                before = _snapshot_entries(item.old_path or path, item.base)
                after = _snapshot_entries(path, item.head)
                changed = sum(1 for key, value in before.items() if after.get(key) != value)
                rewritten_snapshots.append(path)
                if changed >= _BULK_SNAPSHOT_ENTRIES:
                    out.append(
                        _weakening(
                            producer,
                            rule="snapshot_bulk",
                            scope=path,
                            title=f"{_plural(changed, 'snapshot')} rewritten in {path}",
                            summary=(
                                f"{changed} of {len(before)} recorded snapshots changed or were removed at once; "
                                "a bulk update accepts whatever the code now outputs"
                            ),
                            base_state=before,
                            head_state=after,
                            paths=(path,),
                        )
                    )
            continue
        if is_test_path(item.old_path) or is_test_path(path):
            base_markers = _marker_names(item.base)
            head_markers = _marker_names(item.head)
            for kind, _pattern in _SKIP_MARKERS:
                before_names, after_names = base_markers[kind], head_markers[kind]
                if len(after_names) <= len(before_names):
                    continue
                if kind != "focused" and not item.base:
                    continue  # a new file disabled nothing; a focus marker there still hides the other tests
                added = list((Counter(after_names) - Counter(before_names)).elements()) or after_names
                out.append(
                    _weakening(
                        producer,
                        rule=f"test_{kind}",
                        scope=path,
                        title=f"{_plural(len(after_names) - len(before_names), 'test')} {_SKIP_LABELS[kind]}",
                        summary=f"{path}: " + ", ".join(added[:6]) + (" …" if len(added) > 6 else ""),
                        base_state={"count": len(before_names)},
                        head_state={"count": len(after_names), "tests": sorted(added)},
                        paths=(path,),
                        details=[(f"test {index + 1}", name) for index, name in enumerate(added[:8])],
                    )
                )
            base_total, base_strict, base_weak = _assertion_counts(item.base)
            head_total, head_strict, head_weak = _assertion_counts(item.head)
            lost, losing = _lost_assertions(item, moved_bodies)
            if lost:
                where = "the test file was deleted" if not item.head else ", ".join(losing[:6])
                out.append(
                    _weakening(
                        producer,
                        rule="assertions_removed",
                        scope=path,
                        title=f"{_plural(lost, 'assertion')} removed in {path}",
                        summary=f"Fewer checks run: {where or f'{base_total} → {head_total} assertions'}",
                        base_state={"assertions": base_total},
                        head_state={"assertions": head_total, "lost": lost, "tests": losing},
                        paths=(path,),
                        details=[(f"test {index + 1}", name) for index, name in enumerate(losing[:8])],
                    )
                )
            if head_strict < base_strict and head_weak > base_weak:
                out.append(
                    _weakening(
                        producer,
                        rule="assertions_loosened",
                        scope=path,
                        title=f"Assertions loosened in {path}",
                        summary=(
                            f"Exact checks {base_strict} → {head_strict} while truthiness/existence checks "
                            f"{base_weak} → {head_weak}"
                        ),
                        base_state={"exact": base_strict, "loose": base_weak},
                        head_state={"exact": head_strict, "loose": head_weak},
                        paths=(path,),
                    )
                )
        if suffix in _CODE_SUFFIXES:
            before_suppressions = _suppression_counts(item.base)
            after_suppressions = _suppression_counts(item.head)
            for group, counts in sorted(after_suppressions.items()):
                grown = counts - before_suppressions.get(group, Counter())
                if not grown:
                    continue
                added_total = sum(grown.values())
                label = _SUPPRESSION_LABELS[group]
                out.append(
                    _weakening(
                        producer,
                        rule=f"{group}_suppression",
                        scope=path,
                        title=f"{_plural(added_total, f'new {label} suppression')} in {path}",
                        summary="Added: " + ", ".join(f"{name} x{count}" for name, count in sorted(grown.items())),
                        base_state=dict(before_suppressions.get(group, Counter())),
                        head_state=dict(counts),
                        paths=(path,),
                        details=[(name, f"+{count}") for name, count in sorted(grown.items())],
                        change="added",
                    )
                )
        if is_threshold_path(path) or is_ci_path(path):
            lowered, ignores, rules = _threshold_changes(path, item.base, item.head)
            if lowered:
                out.append(
                    _weakening(
                        producer,
                        rule="threshold_lowered",
                        scope=path,
                        title=f"{_plural(len(lowered), 'quality threshold')} lowered in {path}",
                        summary="; ".join(f"{key}: {value}" for key, value in lowered[:6]),
                        base_state=[key for key, _ in lowered],
                        head_state=lowered,
                        paths=(path,),
                        details=lowered[:8],
                    )
                )
            if ignores and item.base:
                out.append(
                    _weakening(
                        producer,
                        rule="ignore_list_grown",
                        scope=path,
                        title=f"{_plural(len(ignores), 'new ignore entry', 'new ignore entries')} in {path}",
                        summary="Now excluded from checks: " + ", ".join(ignores[:6]),
                        base_state=[],
                        head_state=ignores,
                        paths=(path,),
                        details=[(f"ignored {index + 1}", value) for index, value in enumerate(ignores[:8])],
                    )
                )
            if rules:
                out.append(
                    _weakening(
                        producer,
                        rule="lint_rule_lowered",
                        scope=path,
                        title=f"{_plural(len(rules), 'lint rule')} lowered in {path}",
                        summary="; ".join(rules[:6]),
                        base_state=[],
                        head_state=rules,
                        paths=(path,),
                    )
                )
        if is_ci_path(path) or is_ci_path(item.old_path):
            out.extend(_ci_deltas(producer, item))
    if len(rewritten_snapshots) >= _BULK_SNAPSHOT_FILES:
        out.append(
            _weakening(
                producer,
                rule="snapshot_bulk",
                scope="*",
                title=f"{len(rewritten_snapshots)} snapshot/golden files rewritten together",
                summary="Recorded expected outputs were updated in bulk: " + ", ".join(rewritten_snapshots[:6]),
                base_state=sorted(rewritten_snapshots),
                head_state=sorted(rewritten_snapshots),
                paths=rewritten_snapshots,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# spec delta
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TestCase:
    key: str
    prose: str
    body: str


_PY_CLASS = re.compile(r"^(\s*)class\s+(\w+)")
_PY_TEST = re.compile(r"^(\s*)(?:async\s+)?def\s+(test\w*)\s*\(")
_JS_CASE = re.compile(
    r"""^(\s*)(?:it|test)(?:\.(?:only|skip|todo|concurrent|fails))?\s*\(\s*(["'`])((?:\\.|(?!\2).){1,300})\2"""
)
_JS_DESCRIBE = re.compile(r"""^(\s*)(?:describe|context|suite)(?:\.\w+)?\s*\(\s*(["'`])((?:\\.|(?!\2).){1,200})\2""")
_GO_TEST = re.compile(r"^func\s+(Test\w+)\s*\(\s*\w+\s+\*testing\.T\s*\)")
_ANNOTATED_TEST = re.compile(r"^\s*(?:#\[(?:tokio::)?test\b|@Test\b|@ParameterizedTest\b)")
_RB_CASE = re.compile(r"""^(\s*)it\s+["'](.{1,300}?)["']""")
_JS_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte"})


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block(lines: list[str], start: int, indent: int) -> str:
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and _indent(line) <= indent and not line.strip().startswith((")", "}", "]")):
            break
        body.append(line.strip())
        if line.strip() and _indent(line) <= indent:
            break
    return "\n".join(item for item in body if item)


def humanize_test_name(name: str) -> str:
    text = re.sub(r"^(?:test_?|Test)", "", name)
    text = text.replace("_", " ") if "_" in text else _CAMEL.sub(" ", text)
    return " ".join(text.split()).lower() or name


def test_cases(path: str, text: str) -> dict[str, TestCase]:
    """Test case key -> case, for the languages Review reads."""

    if not text or len(text) > _MAX_BYTES:
        return {}
    lines = text.splitlines()
    suffix = _suffix(path)
    out: dict[str, TestCase] = {}
    if suffix == ".py":
        classes: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = _PY_CLASS.match(line)
            if match:
                indent = len(match.group(1))
                classes = [row for row in classes if row[0] < indent] + [(indent, match.group(2))]
                continue
            match = _PY_TEST.match(line)
            if not match:
                continue
            indent = len(match.group(1))
            classes = [row for row in classes if row[0] < indent]
            owner = classes[-1][1] if classes else ""
            key = f"{owner}.{match.group(2)}" if owner else match.group(2)
            prose = humanize_test_name(match.group(2))
            if owner:
                prose = f"{humanize_test_name(owner)}: {prose}"
            out[key] = TestCase(key, prose, _block(lines, index, indent))
    elif suffix in _JS_SUFFIXES:
        scopes: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = _JS_DESCRIBE.match(line)
            if match:
                indent = len(match.group(1))
                scopes = [row for row in scopes if row[0] < indent] + [(indent, match.group(3))]
                continue
            match = _JS_CASE.match(line)
            if not match:
                continue
            indent = len(match.group(1))
            scopes = [row for row in scopes if row[0] < indent]
            prose = " > ".join([*(name for _, name in scopes), match.group(3)])
            out[prose] = TestCase(prose, prose, _block(lines, index, indent))
    elif suffix == ".go":
        for index, line in enumerate(lines):
            match = _GO_TEST.match(line)
            if match:
                out[match.group(1)] = TestCase(
                    match.group(1), humanize_test_name(match.group(1)), _block(lines, index, 0)
                )
    elif suffix in {".rs", ".java", ".kt"}:
        for index, line in enumerate(lines):
            if not _ANNOTATED_TEST.match(line):
                continue
            for ahead in range(index + 1, min(len(lines), index + 6)):
                found = _DEF_NAME.search(lines[ahead])
                if found:
                    name = found.group(1).strip("`")
                    out[name] = TestCase(name, humanize_test_name(name), _block(lines, ahead, _indent(lines[ahead])))
                    break
    elif suffix == ".rb":
        for index, line in enumerate(lines):
            match = _RB_CASE.match(line)
            if match:
                out[match.group(2)] = TestCase(
                    match.group(2), match.group(2), _block(lines, index, len(match.group(1)))
                )
    return out


_EXPECT_PAIRS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^assert\s+(.+?)\s*==\s*(.+?)\s*(?:#.*)?$"),
    re.compile(r"^expect\((.+?)\)\.(?:toBe|toEqual|toStrictEqual)\((.+)\)\s*;?$"),
    re.compile(r"\bassert(?:Equal|Equals)\(\s*(.+?)\s*,\s*(.+?)\s*\)\s*;?$"),
    re.compile(r"^assert_eq!\(\s*(.+?)\s*,\s*(.+?)\s*\)\s*;?$"),
    re.compile(r"\b(?:assert|require)\.Equal\(\s*t\s*,\s*(.+?)\s*,\s*(.+?)\s*\)$"),
)


def _expectations(body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in body.splitlines():
        for pattern in _EXPECT_PAIRS:
            match = pattern.search(line.strip().removeprefix("self."))
            if match:
                out.append((match.group(1), match.group(2)))
                break
    return out


def _changed_expectation(before: str, after: str) -> tuple[str, str] | None:
    """``(old, new)`` expected value of the one assertion whose other side stayed put."""

    old_pairs = [pair for pair in _expectations(before) if pair not in _expectations(after)]
    new_pairs = [pair for pair in _expectations(after) if pair not in _expectations(before)]
    for old_left, old_right in old_pairs:
        for new_left, new_right in new_pairs:
            if old_left == new_left:
                return old_right, new_right
            if old_right == new_right:
                return old_left, new_left
    return None


def _short(value: str, limit: int = 60) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def spec_statements(path: str, base: str, head: str, *, old_path: str = "") -> list[tuple[str, str]]:
    """``(verb, statement)`` for every spec change of one test or snapshot file."""

    if is_snapshot_path(path):
        before = _snapshot_entries(old_path or path, base)
        after = _snapshot_entries(path, head)
        if before or after:
            entries = [("new snapshot", f"new snapshot: {key}") for key in sorted(set(after) - set(before))]
            entries += [("snapshot removed", f"snapshot removed: {key}") for key in sorted(set(before) - set(after))]
            entries += [
                ("snapshot changed", f"snapshot output changed: {key}")
                for key in sorted(set(before) & set(after))
                if before[key] != after[key]
            ]
            return entries
        if base == head:
            return []
        old_lines, new_lines = Counter(base.splitlines()), Counter(head.splitlines())
        plus = sum((new_lines - old_lines).values())
        minus = sum((old_lines - new_lines).values())
        verb = "golden added" if not base else "golden removed" if not head else "golden changed"
        return [(verb, f"{verb.replace('golden', 'expected output')}: {path} (+{plus} -{minus} lines)")]
    before_cases = test_cases(old_path or path, base)
    after_cases = test_cases(path, head)
    added = {key: case for key, case in after_cases.items() if key not in before_cases}
    removed = {key: case for key, case in before_cases.items() if key not in after_cases}
    rows: list[tuple[str, str]] = []
    for _old_key, old_case in sorted(removed.items()):
        match = next((key for key, case in sorted(added.items()) if case.body and case.body == old_case.body), "")
        if match:
            rows.append(("reworded", f"reworded: {old_case.prose} → {added.pop(match).prose}"))
        else:
            rows.append(("no longer checked", f"no longer checked: {old_case.prose}"))
    rows.extend(("new expectation", f"new expectation: {case.prose}") for _, case in sorted(added.items()))
    for key in sorted(set(before_cases) & set(after_cases)):
        old_case, new_case = before_cases[key], after_cases[key]
        if old_case.body == new_case.body:
            continue
        moved = _changed_expectation(old_case.body, new_case.body)
        if moved is not None:
            rows.append(("now expects", f"{new_case.prose}: now expects {_short(moved[1])} (was {_short(moved[0])})"))
        else:
            rows.append(("expectations changed", f"{new_case.prose}: expectations changed"))
    return rows


def spec_deltas(producer: str, files: Sequence[ChangedText]) -> list[BehaviorDelta]:
    """One spec delta per changed test or snapshot file whose cases changed."""

    out: list[BehaviorDelta] = []
    for item in files:
        path = item.path
        if not (is_test_path(path) or is_test_path(item.old_path) or is_snapshot_path(path)):
            continue
        if len(item.base) > _MAX_BYTES or len(item.head) > _MAX_BYTES:
            continue
        rows = spec_statements(path, item.base, item.head, old_path=item.old_path)
        if not rows:
            continue
        statements = [text for _, text in rows]
        title = statements[0] if len(statements) == 1 else f"{_plural(len(statements), 'spec change')} in {path}"
        out.append(
            BehaviorDelta(
                kind="spec_delta",
                producer=producer,
                subject=f"spec:{path}",
                title=title,
                summary="; ".join(statements[:6]) + (" …" if len(statements) > 6 else ""),
                base=BehaviorObservation(
                    "base",
                    status="before",
                    digest=(
                        _digest(sorted(test_cases(item.old_path or path, item.base).items(), key=str))[:32]
                        if not is_snapshot_path(path)
                        else _digest(item.base)[:32]
                    ),
                    summary="specification before this change",
                ),
                head=BehaviorObservation(
                    "head",
                    status="after",
                    digest=_digest(statements)[:32],
                    summary=f"{_plural(len(statements), 'statement')}",
                ),
                change="changed",
                severity="medium",
                paths=(path,),
                facts=tuple(
                    (f"{index + 1}. {verb}", text) for index, (verb, text) in enumerate(rows[:_MAX_STATEMENTS])
                ),
                environment_fingerprint=f"static-verification:v{VERIFICATION_ANALYSIS_VERSION}",
            )
        )
    return out


__all__ = [
    "AUTO_APPROVAL_BLOCKED",
    "AUTO_APPROVAL_FACT",
    "VERIFICATION_ANALYSIS_VERSION",
    "AutoApprovalBlocker",
    "ChangedText",
    "TestCase",
    "auto_approval_blockers",
    "humanize_test_name",
    "is_ci_path",
    "is_snapshot_path",
    "is_test_path",
    "is_threshold_path",
    "spec_deltas",
    "spec_statements",
    "test_cases",
    "verification_relevant",
    "weakening_deltas",
]
