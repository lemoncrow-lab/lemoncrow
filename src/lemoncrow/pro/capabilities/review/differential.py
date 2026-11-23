"""Base-vs-head check differential (Phase 9R items 5, 6, 8, 27).

Every check recipe execution produces attempts per Snapshot side. This module
turns them into a classification that says what the Change did, never more:

* per test where the runner reports tests (JUnit XML -- pytest and most
  runners, jest/vitest JSON, ``go test -json``, ``cargo test`` output), per
  recipe otherwise;
* introduced failure, pre-existing failure, fixed, new, removed, newly
  skipped, flaky, quarantined, not comparable, not run, unchanged;
* a differing environment fingerprint between the sides is *not comparable*:
  nothing is called introduced, pre-existing or fixed across environments;
* mixed outcomes over reruns are *flaky*, which is never PASS;
* trusted-base quarantine excludes a test from the verdict and is reported;
* agent claims (``classify_check`` Development events, caller-attested
  results) are reconciled with the sandboxed head run as confirmed,
  contradicted or not reproduced;
* test adequacy: a test the Change added or edited must fail on base source
  and pass on head; one that passes on base too does not demonstrate the
  change.

Only attention classes become ``test_outcome`` behavior targets
(:class:`CheckDifferentialProducer`); pre-existing failures, quarantined and
unchanged results are counted, not raised.

lc-debt: mutation testing (does the new test kill a mutant of the changed
lines?) is not implemented; adequacy is the base-fails/head-passes probe only.
Upgrade: run a bounded mutant set of the changed hunks through the same probe.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, ClassVar, Final, Literal, cast

from lemoncrow.pro.capabilities.review.behavior import (
    BEHAVIOR_CONTRACT_VERSION,
    BehaviorContext,
    BehaviorDelta,
    BehaviorEvidenceRef,
    BehaviorObservation,
    BehaviorProduction,
    BehaviorSeverity,
    ExecutedScope,
)
from lemoncrow.pro.capabilities.review.recipes import CheckPolicy, CheckRecipe

CHECK_DIFFERENTIAL_MEDIA_TYPE: Final[str] = "application/vnd.lemoncrow.check-differential+json;version=1"
CHECK_DIFFERENTIAL_PRODUCER_ID: Final[str] = "lemoncrow.check_recipes"
DIFFERENTIAL_VERSION: Final[int] = 1

TestStatus = Literal["passed", "failed", "skipped"]
SideStatus = Literal["passed", "failed", "skipped", "flaky", "missing", "not_run"]
DifferentialClass = Literal[
    "introduced_failure",
    "pre_existing_failure",
    "fixed",
    "new",
    "removed",
    "newly_skipped",
    "flaky",
    "quarantined",
    "not_comparable",
    "not_run",
    "not_compared",
    "unchanged",
]
DIFFERENTIAL_CLASSES: Final[tuple[str, ...]] = (
    "introduced_failure",
    "pre_existing_failure",
    "fixed",
    "new",
    "removed",
    "newly_skipped",
    "flaky",
    "quarantined",
    "not_comparable",
    "not_run",
    "not_compared",
    "unchanged",
)
#: Classes that are deltas and demand attention; the rest are counted only.
ATTENTION_CLASSES: Final[frozenset[str]] = frozenset(
    {"introduced_failure", "fixed", "new", "removed", "newly_skipped", "flaky", "not_comparable", "not_run"}
)
Verdict = Literal["PASS", "FAIL", "UNKNOWN", "NOT_RUN"]
AdequacyVerdict = Literal["demonstrates", "not_demonstrating", "not_assessed"]
ClaimVerdict = Literal["confirmed", "contradicted", "not_reproduced"]

_MAX_REPORT_CASES: Final[int] = 20_000
_MAX_UNITS_STORED: Final[int] = 400
_MAX_DELTAS_PER_CLASS: Final[int] = 24


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# runner reports
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TestCase:
    __test__: ClassVar[bool] = False  # not a pytest class

    id: str
    status: TestStatus
    file: str = ""


@dataclass(frozen=True, slots=True)
class TestReport:
    """Parsed per-test results; ``complete`` only when the whole report was read."""

    __test__: ClassVar[bool] = False  # not a pytest class
    format: str
    cases: tuple[TestCase, ...] = ()
    error: str = ""
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return not self.error and not self.truncated

    def statuses(self) -> dict[str, TestStatus]:
        out: dict[str, TestStatus] = {}
        for case in self.cases:
            previous = out.get(case.id)
            # A duplicated id (parametrized names collapsing) keeps its worst outcome.
            if previous is None or case.status == "failed" or (previous == "skipped" and case.status == "passed"):
                out[case.id] = case.status
        return out

    def files(self) -> dict[str, str]:
        return {case.id: case.file for case in self.cases if case.file}


def _strip_sandbox(path: str) -> str:
    for prefix in ("/workspace/", "/snapshot/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path.lstrip("./") if path.startswith("./") else path


def _parse_junit(text: str) -> TestReport:
    # Reports are written by reviewed code: entity expansion is refused outright. The
    # whole text is searched (a long prolog comment must not hide a DTD); a literal
    # "<!DOCTYPE" in CDATA output also refuses, which only costs per-test detail.
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        return TestReport("junit", error="JUnit report declares a DTD; refused")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        return TestReport("junit", error=f"JUnit report is not valid XML: {exc}"[:300])
    cases: list[TestCase] = []
    for node in root.iter("testcase"):
        if len(cases) >= _MAX_REPORT_CASES:
            return TestReport("junit", tuple(cases), truncated=True)
        name = str(node.get("name") or "").strip()
        classname = str(node.get("classname") or "").strip()
        if not name:
            continue
        children = {child.tag for child in node}
        status: TestStatus = (
            "failed" if children & {"failure", "error"} else "skipped" if "skipped" in children else "passed"
        )
        cases.append(
            TestCase(
                id=f"{classname}::{name}" if classname else name,
                status=status,
                file=_strip_sandbox(str(node.get("file") or "")),
            )
        )
    return TestReport("junit", tuple(cases))


def _parse_jest(text: str) -> TestReport:
    try:
        decoded = json.loads(text)
    except ValueError as exc:
        return TestReport("jest-json", error=f"jest/vitest JSON report is invalid: {exc}"[:300])
    results = decoded.get("testResults") if isinstance(decoded, Mapping) else None
    if not isinstance(results, list):
        return TestReport("jest-json", error="jest/vitest JSON report has no testResults")
    cases: list[TestCase] = []
    for suite in results:
        if not isinstance(suite, Mapping):
            continue
        file = _strip_sandbox(str(suite.get("name") or suite.get("testFilePath") or ""))
        for row in suite.get("assertionResults") or ():
            if len(cases) >= _MAX_REPORT_CASES:
                return TestReport("jest-json", tuple(cases), truncated=True)
            if not isinstance(row, Mapping):
                continue
            title = str(row.get("fullName") or row.get("title") or "").strip()
            raw = str(row.get("status") or "").lower()
            status: TestStatus | None = (
                "passed"
                if raw == "passed"
                else (
                    "failed"
                    if raw == "failed"
                    else "skipped" if raw in {"pending", "skipped", "todo", "disabled"} else None
                )
            )
            if title and status is not None:
                cases.append(TestCase(id=f"{file}::{title}" if file else title, status=status, file=file))
    return TestReport("jest-json", tuple(cases))


def _parse_go(text: str) -> TestReport:
    outcomes: dict[str, TestStatus] = {}
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(row, Mapping) or not row.get("Test"):
            continue
        action = str(row.get("Action") or "")
        if action not in {"pass", "fail", "skip"}:
            continue
        key = f"{row.get('Package') or ''}::{row['Test']}"
        if key not in outcomes and len(outcomes) >= _MAX_REPORT_CASES:
            return TestReport("go-json", _cases(outcomes), truncated=True)
        outcomes[key] = "passed" if action == "pass" else "failed" if action == "fail" else "skipped"
    if bad and not outcomes:
        return TestReport("go-json", error="go test -json output is not JSON")
    return TestReport("go-json", _cases(outcomes))


def _cases(outcomes: Mapping[str, TestStatus]) -> tuple[TestCase, ...]:
    return tuple(TestCase(id=key, status=value) for key, value in outcomes.items())


_CARGO_LINE = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b", re.MULTILINE)


def _parse_cargo(text: str) -> TestReport:
    outcomes: dict[str, TestStatus] = {}
    for match in _CARGO_LINE.finditer(text):
        if len(outcomes) >= _MAX_REPORT_CASES:
            return TestReport("cargo", _cases(outcomes), truncated=True)
        outcomes[match.group(1)] = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}[
            match.group(2)
        ]  # type: ignore[assignment]
    return TestReport("cargo", _cases(outcomes))


def parse_report(fmt: str, text: str | None, *, truncated: bool = False) -> TestReport | None:
    """Per-test results from a runner report; ``None`` when the recipe has none."""

    if not fmt:
        return None
    if not text or not text.strip():
        return TestReport(fmt, error="the runner wrote no report")
    parser = {"junit": _parse_junit, "jest-json": _parse_jest, "go-json": _parse_go, "cargo": _parse_cargo}.get(fmt)
    if parser is None:
        return TestReport(fmt, error=f"unknown report format {fmt}")
    report = parser(text)
    if truncated and not report.error:
        return TestReport(report.format, report.cases, truncated=True)
    if not report.error and not report.cases:
        return TestReport(fmt, error="the report lists no tests")
    return report


# --------------------------------------------------------------------------- #
# attempts and classification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """One sandboxed execution of one recipe on one side."""

    side: Literal["old", "new"]
    attempt: int
    status: Literal["passed", "failed", "not_run"]
    variant: Literal["run", "probe", "fix"] = "run"
    reason: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    environment: str = ""
    executor_class: str = ""
    evidence_id: str = ""
    evidence_ref: str = ""
    report: TestReport | None = None
    changed_paths: tuple[str, ...] = ()
    reused: bool = False
    #: Where a reused run actually executed (R6): ``{"kind": "snapshot" | "shared_base", ...}``.
    reused_from: Mapping[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        provenance = {"reused_from": dict(self.reused_from)} if self.reused_from else {}
        return {
            **provenance,
            "side": self.side,
            "attempt": self.attempt,
            "variant": self.variant,
            "status": self.status,
            "reason": self.reason[:500],
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "environment": self.environment,
            "executor_class": self.executor_class,
            "evidence_id": self.evidence_id,
            "evidence_ref": self.evidence_ref,
            "report": (
                None
                if self.report is None
                else {
                    "format": self.report.format,
                    "tests": len(self.report.cases),
                    "complete": self.report.complete,
                    "error": self.report.error,
                }
            ),
            "changed_paths": list(self.changed_paths[:64]),
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class UnitResult:
    """One classified unit: a test, or the recipe itself (``subject == ""``)."""

    subject: str
    klass: DifferentialClass
    base: SideStatus
    head: SideStatus
    base_attempts: tuple[str, ...] = ()
    head_attempts: tuple[str, ...] = ()
    quarantine: str = ""
    file: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "class": self.klass,
            "base": self.base,
            "head": self.head,
            "base_attempts": list(self.base_attempts),
            "head_attempts": list(self.head_attempts),
            "quarantine": self.quarantine,
            "file": self.file,
        }


@dataclass(frozen=True, slots=True)
class AdequacyResult:
    test: str
    file: str
    verdict: AdequacyVerdict
    base_probe: str
    head: str
    reason: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "test": self.test,
            "file": self.file,
            "verdict": self.verdict,
            "base_probe": self.base_probe,
            "head": self.head,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AgentClaim:
    """A check an agent or caller says it ran, with the outcome it reported."""

    key: str
    command: str
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    origin: Literal["development", "caller_attested"]
    ref: str = ""
    at: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "command": self.command[:400],
            "status": self.status,
            "origin": self.origin,
            "ref": self.ref,
            "at": self.at,
        }


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claim: AgentClaim
    recipe_id: str
    verdict: ClaimVerdict
    sandbox: str
    reason: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "claim": self.claim.to_wire(),
            "recipe_id": self.recipe_id,
            "verdict": self.verdict,
            "sandbox": self.sandbox,
            "reason": self.reason,
        }


def _side_status(outcomes: Sequence[str]) -> SideStatus:
    present = [item for item in outcomes if item in {"passed", "failed", "skipped"}]
    if not present:
        return "missing"
    if "failed" in present and "passed" in present:
        return "flaky"
    if "failed" in present:
        return "failed"
    if "passed" in present:
        return "passed"
    return "skipped"


def _classify(base: SideStatus, head: SideStatus, *, comparable: bool) -> DifferentialClass:
    if head == "not_run":
        return "not_run"
    if "flaky" in {base, head}:
        return "flaky"
    if base == "not_run":
        return "not_comparable"
    if not comparable:
        # Across environments only "nothing changed and nothing failed" is stated.
        return "unchanged" if base == head and head in {"passed", "skipped"} else "not_comparable"
    if base == "missing" and head == "missing":
        return "unchanged"
    if base == "missing":
        return "introduced_failure" if head == "failed" else "new"
    if head == "missing":
        return "removed"
    if base == "passed" and head == "failed":
        return "introduced_failure"
    if base == "failed" and head == "failed":
        return "pre_existing_failure"
    if base == "failed" and head == "passed":
        return "fixed"
    if head == "skipped" and base in {"passed", "failed"}:
        return "newly_skipped"
    if base == "skipped" and head == "failed":
        return "introduced_failure"
    return "unchanged"


@dataclass(frozen=True, slots=True)
class RecipeDifferential:
    """The classified outcome of one recipe on one Snapshot."""

    recipe: CheckRecipe
    sides: tuple[str, ...]
    granularity: Literal["test", "recipe"]
    comparable: bool | None
    environment: Mapping[str, Any]
    executor_class: str
    head_verdict: Verdict
    base_verdict: Verdict | Literal[""]
    reasons: tuple[str, ...]
    recipe_unit: UnitResult
    tests: tuple[UnitResult, ...]
    counts: Mapping[str, int]
    attempts: tuple[AttemptOutcome, ...]
    reruns: Mapping[str, Any] = field(default_factory=dict)
    adequacy: tuple[AdequacyResult, ...] = ()
    claims: tuple[ClaimResult, ...] = ()
    autofix: Mapping[str, Any] = field(default_factory=dict)
    head_guard: str = ""
    #: R6 execution economy: test selection and how the base side was decided.
    economy: Mapping[str, Any] = field(default_factory=dict)
    #: Every test file the run selected, untruncated (``economy.selection.tests`` keeps 200). Never stored.
    selected_files: tuple[str, ...] = ()

    def attention(self) -> tuple[UnitResult, ...]:
        units = tuple(unit for unit in self.tests if unit.klass in ATTENTION_CLASSES)
        explained = {unit.klass for unit in units}
        recipe_level = self.recipe_unit
        # The recipe-level outcome is raised only when no test explains it
        # (collection error, crash, coverage gate, a recipe without a report).
        if recipe_level.klass in ATTENTION_CLASSES and not (
            recipe_level.klass in explained or (recipe_level.klass == "flaky" and "flaky" in explained)
        ):
            return (recipe_level, *units)
        return units

    def to_wire(self) -> dict[str, Any]:
        stored = sorted(
            self.tests,
            key=lambda unit: (
                unit.klass not in ATTENTION_CLASSES,
                DIFFERENTIAL_CLASSES.index(unit.klass),
                unit.subject,
            ),
        )
        economy = {"economy": dict(self.economy)} if self.economy else {}
        return {
            **economy,
            "v": DIFFERENTIAL_VERSION,
            "recipe": {
                "id": self.recipe.id,
                "kind": self.recipe.kind,
                "title": self.recipe.title or self.recipe.id,
                "entry_id": self.recipe.entry_id,
                "kind_entry": self.recipe.kind_entry,
                "digest": self.recipe.digest,
                "command": list(self.recipe.command),
                "cwd": self.recipe.cwd,
                "origin": self.recipe.origin,
            },
            "sides": list(self.sides),
            "granularity": self.granularity,
            "comparable": self.comparable,
            "environment": dict(self.environment),
            "executor_class": self.executor_class,
            "head_verdict": self.head_verdict,
            "base_verdict": self.base_verdict,
            "reasons": list(self.reasons),
            "recipe_unit": self.recipe_unit.to_wire(),
            "tests": [unit.to_wire() for unit in stored[:_MAX_UNITS_STORED]],
            "tests_total": len(self.tests),
            "counts": dict(self.counts),
            "attention": [unit.to_wire() for unit in self.attention()[:_MAX_UNITS_STORED]],
            "attempts": [attempt.to_wire() for attempt in self.attempts],
            "reruns": dict(self.reruns),
            "adequacy": [row.to_wire() for row in self.adequacy],
            "claims": [row.to_wire() for row in self.claims],
            "autofix": dict(self.autofix),
            "head_guard": self.head_guard,
        }


def _recipe_side_status(attempts: Sequence[AttemptOutcome], *, detect_changes: bool) -> SideStatus:
    ran = [attempt for attempt in attempts if attempt.status != "not_run"]
    if not ran:
        return "not_run"
    outcomes = [
        "failed" if attempt.status == "failed" or (detect_changes and attempt.changed_paths) else "passed"
        for attempt in ran
    ]
    return _side_status(outcomes)


def classify_recipe(
    recipe: CheckRecipe,
    attempts: Sequence[AttemptOutcome],
    *,
    policy: CheckPolicy,
    sides: Sequence[str],
    environment_differences: Sequence[str] = (),
    head_guard: str = "",
    reruns: Mapping[str, Any] | None = None,
) -> RecipeDifferential:
    """Classify one recipe's attempts; fail closed wherever evidence is missing."""

    runs = [attempt for attempt in attempts if attempt.variant == "run"]
    base_runs = [attempt for attempt in runs if attempt.side == "old"]
    head_runs = [attempt for attempt in runs if attempt.side == "new"]
    compared = "old" in sides
    base_status: SideStatus = (
        _recipe_side_status(base_runs, detect_changes=recipe.detect_changes) if compared else "missing"
    )
    head_status = _recipe_side_status(head_runs, detect_changes=recipe.detect_changes)

    def environment(side_runs: Sequence[AttemptOutcome]) -> str:
        return next((attempt.environment for attempt in side_runs if attempt.environment), "")

    base_env, head_env = environment(base_runs), environment(head_runs)
    comparable: bool | None = None
    if compared and base_status != "not_run" and head_status != "not_run":
        # Literal fingerprint equality, fail closed: an unknown environment is not comparable.
        comparable = bool(base_env) and base_env == head_env

    def attempt_marks(side_runs: Sequence[AttemptOutcome]) -> tuple[str, ...]:
        return tuple(
            (
                "not_run"
                if attempt.status == "not_run"
                else (
                    "failed"
                    if attempt.status == "failed" or (recipe.detect_changes and attempt.changed_paths)
                    else "passed"
                )
            )
            for attempt in side_runs
        )

    recipe_class = (
        _classify(base_status, head_status, comparable=bool(comparable))
        if compared
        else ("not_run" if head_status == "not_run" else "flaky" if head_status == "flaky" else "not_compared")
    )
    recipe_unit = UnitResult(
        subject="",
        klass=recipe_class,
        base=base_status if compared else "missing",
        head=head_status,
        base_attempts=attempt_marks(base_runs),
        head_attempts=attempt_marks(head_runs),
    )

    # Per-test granularity only when every executed attempt that matters has a complete report.
    def reports(side_runs: Sequence[AttemptOutcome]) -> list[TestReport] | None:
        executed = [attempt for attempt in side_runs if attempt.status != "not_run"]
        if not executed:
            return []
        out: list[TestReport] = []
        for attempt in executed:
            if attempt.report is None or not attempt.report.complete:
                return None
            out.append(attempt.report)
        return out

    head_reports = reports(head_runs) if recipe.report else None
    base_reports = reports(base_runs) if recipe.report and compared else ([] if recipe.report else None)
    reasons: list[str] = []
    tests: list[UnitResult] = []
    granularity: Literal["test", "recipe"] = "recipe"
    if head_reports and base_reports is not None:
        granularity = "test"
        base_by_attempt = [report.statuses() for report in base_reports]
        head_by_attempt = [report.statuses() for report in head_reports]
        files: dict[str, str] = {}
        for report in (*base_reports, *head_reports):
            files.update(report.files())
        ids = sorted({key for rows in (*base_by_attempt, *head_by_attempt) for key in rows})
        base_executed = compared and base_status != "not_run"
        for test_id in ids:
            head_marks = tuple(rows.get(test_id, "missing") for rows in head_by_attempt)
            base_marks = tuple(rows.get(test_id, "missing") for rows in base_by_attempt)
            head_side = _side_status(head_marks)
            base_side: SideStatus = (
                _side_status(base_marks) if base_executed else ("not_run" if compared else "missing")
            )
            rule = policy.quarantined(recipe.id, test_id)
            if rule is not None:
                klass: DifferentialClass = "quarantined"
            elif compared:
                klass = _classify(base_side, head_side, comparable=bool(comparable))
            else:
                klass = "flaky" if head_side == "flaky" else "not_compared"
            tests.append(
                UnitResult(
                    subject=test_id,
                    klass=klass,
                    base=base_side,
                    head=head_side,
                    base_attempts=base_marks,
                    head_attempts=head_marks,
                    quarantine=(rule.reason or "quarantined by trusted-base check_policy") if rule else "",
                    file=files.get(test_id, ""),
                )
            )
    elif recipe.report:
        problem = next(
            (
                attempt.report.error or "report truncated"
                for attempt in runs
                if attempt.status != "not_run" and attempt.report is not None and not attempt.report.complete
            ),
            "",
        )
        reasons.append(
            "per-test results unavailable" + (f" ({problem})" if problem else "") + "; classified per recipe"
        )

    # Head verdict: the truth of the head run, never better than the evidence.
    head_verdict: Verdict
    quarantined = [unit for unit in tests if unit.klass == "quarantined" and unit.head in {"failed", "flaky"}]
    others = [unit for unit in tests if unit.klass != "quarantined" and unit.head == "failed"]
    unquarantined_flaky = any(unit.head == "flaky" and unit.klass != "quarantined" for unit in tests)
    # Every failing or flaky head test is quarantined: the trusted base excludes them
    # from the verdict (they stay reported). Needs complete per-test reports.
    only_quarantined = (
        granularity == "test"
        and bool(quarantined)
        and not others
        and not unquarantined_flaky
        and not recipe.detect_changes
    )
    if head_status == "not_run":
        head_verdict = "NOT_RUN"
        reasons.insert(0, head_guard or next((a.reason for a in head_runs if a.reason), "head side was not executed"))
    elif head_status in {"failed", "flaky"} and only_quarantined:
        head_verdict = "PASS"
        reasons.insert(
            0, f"{len(quarantined)} quarantined failing test(s) excluded by the trusted-base quarantine list"
        )
        recipe_unit = replace(recipe_unit, klass="quarantined", quarantine="failures come only from quarantined tests")
    elif head_status == "flaky" or unquarantined_flaky:
        head_verdict = "UNKNOWN"
        reasons.insert(0, "flaky: outcomes differ across reruns, so the result is never PASS")
    elif head_status == "failed":
        head_verdict = "FAIL"
        changed = next((a.changed_paths for a in head_runs if a.changed_paths), ())
        reasons.insert(
            0,
            (
                f"running the check changed {len(changed)} file(s)"
                if recipe.detect_changes and changed
                else f"{len(others)} failing test(s)" if others else "the recipe exited non-zero"
            ),
        )
    else:
        head_verdict = "PASS"
        reasons.insert(0, f"{len(head_runs)} sandboxed run(s) passed")
    base_verdict: Verdict | Literal[""] = ""
    if compared:
        base_verdict = {
            "not_run": "NOT_RUN",
            "flaky": "UNKNOWN",
            "failed": "FAIL",
            "passed": "PASS",
            "missing": "NOT_RUN",
            "skipped": "UNKNOWN",
        }[
            base_status
        ]  # type: ignore[assignment]
    if comparable is False:
        reasons.append(
            "base and head ran in different environments: "
            + (", ".join(environment_differences) if environment_differences else "fingerprints differ")
            + "; nothing is classified as introduced or pre-existing"
        )
    counts: dict[str, int] = {}
    for unit in tests or [recipe_unit]:
        counts[unit.klass] = counts.get(unit.klass, 0) + 1
    executor = _least_trusted(attempt.executor_class for attempt in runs if attempt.status != "not_run")
    return RecipeDifferential(
        recipe=recipe,
        sides=tuple(sides),
        granularity=granularity,
        comparable=comparable,
        environment={
            "base": base_env,
            "head": head_env,
            "differences": list(environment_differences),
        },
        executor_class=executor,
        head_verdict=head_verdict,
        base_verdict=base_verdict,
        reasons=tuple(reasons),
        recipe_unit=recipe_unit,
        tests=tuple(tests),
        counts=counts,
        attempts=tuple(attempts),
        reruns=dict(reruns or {}),
        head_guard=head_guard,
    )


def _least_trusted(classes: Iterable[str]) -> str:
    present = {item for item in classes if item}
    if not present:
        return ""
    if "author_machine" in present:
        return "author_machine"
    if "customer_worker" in present:
        return "customer_worker"
    return sorted(present)[0]


def combine_verdicts(verdicts: Sequence[Verdict]) -> Verdict:
    """One kind-level verdict over its recipes: PASS only when every recipe passed."""

    if not verdicts:
        return "NOT_RUN"
    if "FAIL" in verdicts:
        return "FAIL"
    if "UNKNOWN" in verdicts:
        return "UNKNOWN"
    if "NOT_RUN" in verdicts:
        return "NOT_RUN" if all(item == "NOT_RUN" for item in verdicts) else "UNKNOWN"
    return "PASS"


# --------------------------------------------------------------------------- #
# test adequacy (item 27)
# --------------------------------------------------------------------------- #

_TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|"
    r"(^|/)conftest\.py$"
)


def is_test_path(path: str) -> bool:
    return bool(_TEST_FILE.search(path))


#: Test definitions by line: Python ``def test_*``, Go ``func Test*``, Rust ``fn``, and jest/vitest
#: ``it("title")`` / ``test("title")``. A naive line heuristic, used only to *narrow* which tests of a
#: modified file are adequacy candidates; anything it cannot name stays a candidate.
_TEST_DEFINITION = re.compile(
    r"^\s*(?:async\s+)?def\s+(test\w*)|^\s*func\s+(Test\w+)|^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)|"
    r"\b(?:it|test)(?:\.\w+)?\(\s*(['\"`])(.+?)\4"
)
#: Above this many lines a modified test file is not diffed; every test in it stays a candidate.
_MAX_DIFF_LINES: Final = 20_000


@dataclass(frozen=True, slots=True)
class TestFileChange:
    """Which tests of one head test file the change added or edited."""

    __test__: ClassVar[bool] = False  # not a pytest class

    path: str
    #: Added file, or too large to diff: every test in it is a candidate.
    whole: bool
    #: Names whose definition span (definition line up to the next definition) overlaps a
    #: changed head line, plus names absent from the base file.
    touched: frozenset[str] = frozenset()
    #: Every name the heuristic found on head; a reported test matching none stays a candidate.
    defined: frozenset[str] = frozenset()


def _definitions(lines: Sequence[str]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _TEST_DEFINITION.search(line)
        if match is None:
            continue
        name = next((group for group in (match.group(1), match.group(2), match.group(3), match.group(5)) if group), "")
        if name:
            found.append((index, name.strip()))
    return found


def diff_test_file(path: str, base_text: str | None, head_text: str) -> TestFileChange:
    """Diff one test file base -> head and name the tests whose definition the change touched."""

    import difflib

    if base_text is None:
        return TestFileChange(path, whole=True)
    base_lines, head_lines = base_text.splitlines(), head_text.splitlines()
    if max(len(base_lines), len(head_lines)) > _MAX_DIFF_LINES:
        return TestFileChange(path, whole=True)
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(a=base_lines, b=head_lines, autojunk=False)
    for tag, _start, _end, head_start, head_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if head_start == head_end:  # pure deletion: the surrounding head lines changed meaning
            changed.update({max(head_start - 1, 0), head_start})
        else:
            changed.update(range(head_start, head_end))
    definitions = _definitions(head_lines)
    base_names = {name for _line, name in _definitions(base_lines)}
    touched: set[str] = set()
    for index, (line, name) in enumerate(definitions):
        end = definitions[index + 1][0] if index + 1 < len(definitions) else len(head_lines)
        if name not in base_names or any(line <= number < end for number in changed):
            touched.add(name)
    return TestFileChange(
        path, whole=False, touched=frozenset(touched), defined=frozenset(name for _line, name in definitions)
    )


def _names_match(test_id: str, name: str) -> bool:
    leaf = test_id.rpartition("::")[2].strip()
    return leaf == name or leaf.startswith((name + "[", name + "/")) or leaf.endswith(" " + name)


def is_adequacy_candidate(unit: UnitResult, change: TestFileChange | None) -> bool:
    """New tests, tests of added files and tests whose definition changed; unknown names stay in."""

    if change is None or change.whole or unit.klass == "new":
        return True
    if any(_names_match(unit.subject, name) for name in change.touched):
        return True
    return not any(_names_match(unit.subject, name) for name in change.defined)


def resolve_test_file(test_id: str, reported_file: str, recipe: CheckRecipe, candidates: Iterable[str]) -> str:
    """The repository path of a test, matched against *candidates*; ``""`` when unknown."""

    pool = set(candidates)
    prefix = "" if recipe.cwd in {"", "."} else recipe.cwd.rstrip("/") + "/"
    if reported_file:
        for option in (reported_file, prefix + reported_file):
            if option in pool:
                return option
    head, _, _name = test_id.partition("::")
    if recipe.report == "junit" and head:
        parts = head.replace("/", ".").split(".")
        for size in range(len(parts), 0, -1):
            option = prefix + "/".join(parts[:size]) + ".py"
            if option in pool:
                return option
    if recipe.report == "go-json" and head:
        package = head.split("/")
        for option in sorted(pool):
            if not option.endswith("_test.go"):
                continue
            parent = PurePosixPath(option).parent.parts
            if parent and tuple(package[-len(parent) :]) == parent:
                return option
    return ""


def assess_adequacy(
    recipe: CheckRecipe,
    differential: RecipeDifferential,
    changed_test_files: Sequence[str],
    probes: Sequence[AttemptOutcome],
    changes: Mapping[str, TestFileChange] | None = None,
) -> tuple[AdequacyResult, ...]:
    """Each added or edited test that passes on head must fail on base source.

    *changes* narrows a modified file to the tests the change touched (``diff_test_file``);
    without it every reported test of a changed test file is a candidate.
    """

    if not changed_test_files:
        return ()
    head_tests = [unit for unit in differential.tests if unit.head != "missing"]
    probe_runs = [attempt for attempt in probes if attempt.variant == "probe"]
    probe_reports = [attempt.report for attempt in probe_runs if attempt.status != "not_run" and attempt.report]
    probe_complete = bool(probe_reports) and all(report.complete for report in probe_reports)
    probe_reason = next((attempt.reason for attempt in probe_runs if attempt.reason), "")
    by_attempt = [report.statuses() for report in probe_reports] if probe_complete else []
    results: list[AdequacyResult] = []
    mapped_files: set[str] = set()
    for unit in head_tests:
        path = resolve_test_file(unit.subject, unit.file, recipe, changed_test_files)
        if not path:
            continue
        mapped_files.add(path)
        if not is_adequacy_candidate(unit, (changes or {}).get(path)):
            continue
        if unit.head != "passed":
            results.append(AdequacyResult(unit.subject, path, "not_assessed", "", unit.head, f"head {unit.head}"))
            continue
        if not probe_runs:
            results.append(AdequacyResult(unit.subject, path, "not_assessed", "", unit.head, "base probe was not run"))
            continue
        if not probe_complete:
            results.append(
                AdequacyResult(
                    unit.subject,
                    path,
                    "not_assessed",
                    "not_run",
                    unit.head,
                    probe_reason or "base probe produced no complete report",
                )
            )
            continue
        probe = _side_status(tuple(rows.get(unit.subject, "missing") for rows in by_attempt))
        if probe == "failed":
            results.append(
                AdequacyResult(unit.subject, path, "demonstrates", probe, unit.head, "fails on base, passes on head")
            )
        elif probe == "passed":
            results.append(
                AdequacyResult(
                    unit.subject,
                    path,
                    "not_demonstrating",
                    probe,
                    unit.head,
                    "passes on base source too, so it does not demonstrate the change",
                )
            )
        else:
            results.append(AdequacyResult(unit.subject, path, "not_assessed", probe, unit.head, f"base probe {probe}"))
    for path in changed_test_files:
        if path not in mapped_files and not path.endswith("conftest.py"):
            results.append(
                AdequacyResult(
                    "",
                    path,
                    "not_assessed",
                    "",
                    "",
                    (
                        "no reported test maps to this changed test file"
                        if differential.granularity == "test"
                        else "the recipe reports no per-test results"
                    ),
                )
            )
    return tuple(results)


# --------------------------------------------------------------------------- #
# claims vs proof (item 8)
# --------------------------------------------------------------------------- #

_STATUS_WORDS: Final[Mapping[str, Literal["PASS", "FAIL"]]] = {
    "pass": "PASS",
    "passed": "PASS",
    "ok": "PASS",
    "success": "PASS",
    "succeeded": "PASS",
    "fail": "FAIL",
    "failed": "FAIL",
    "failure": "FAIL",
    "error": "FAIL",
}


def claim_status(value: str) -> Literal["PASS", "FAIL", "UNKNOWN"]:
    return _STATUS_WORDS.get(str(value or "").strip().lower(), "UNKNOWN")


def _normalize(text: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9:_./-]+", " ", text.lower()).split()) + " "


_SIGNATURES: Final[Mapping[str, tuple[str, ...]]] = {
    "pytest": ("pytest", "py.test"),
    "ruff-lint": ("ruff check", "ruff"),
    "ruff-format": ("ruff format",),
    "black-format": ("black",),
    "mypy": ("mypy",),
    "pyright": ("pyright",),
    "flake8": ("flake8",),
    "cargo-test": ("cargo test",),
    "cargo-clippy": ("cargo clippy", "clippy"),
    "cargo-fmt": ("cargo fmt",),
    "cargo-check": ("cargo check",),
    "go-test": ("go test",),
    "go-vet": ("go vet",),
    "gofmt": ("gofmt",),
    "pre-commit": ("pre-commit run", "pre-commit"),
}


def recipe_signatures(recipe: CheckRecipe) -> tuple[str, ...]:
    """Command phrases an agent would type to run *recipe* (lower case, longest first)."""

    phrases: list[str] = list(_SIGNATURES.get(recipe.id, ()))
    if recipe.id.startswith("make-"):
        phrases.append("make " + recipe.id[len("make-") :])
    command = recipe.command
    if command[:2] == ("sh", "-c") and len(command) > 2:
        body = command[2].strip().split()
        if body:
            phrases.append(" ".join(body[:2]).lower())
            phrases.append(body[0].lower())
    elif command:
        tool = PurePosixPath(command[0]).name.lower()
        if tool in {"python", "python3"} and len(command) > 2 and command[1] == "-m":
            tool = command[2].lower()
        phrases.append(tool)
        if len(command) > 1 and not command[1].startswith("-"):
            phrases.append(f"{tool} {command[1].lower()}")
    if recipe.definition.endswith("package.json"):
        script = recipe.source.rpartition("scripts.")[2]
        if script:
            for runner in ("npm", "pnpm", "yarn", "bun"):
                phrases.extend((f"{runner} run {script}", f"{runner} {script}"))
    unique = [phrase for phrase in dict.fromkeys(item.strip() for item in phrases) if phrase and phrase != "sh"]
    return tuple(sorted(unique, key=lambda item: (-len(item), item)))


def match_claim(claim: AgentClaim, recipes: Sequence[CheckRecipe]) -> CheckRecipe | None:
    """The recipe a claimed command corresponds to: the longest matching phrase wins."""

    text = _normalize(claim.command)
    best: tuple[int, str] | None = None
    chosen: CheckRecipe | None = None
    for recipe in recipes:
        for phrase in recipe_signatures(recipe):
            if _normalize(phrase) in text:
                score = (len(phrase), recipe.id)
                if best is None or score[0] > best[0]:
                    best, chosen = score, recipe
                elif score[0] == best[0] and chosen is not None and chosen.id != recipe.id:
                    # Two recipes match equally: ambiguous, so no proof is implied.
                    chosen = None
                break
    return chosen


def reconcile_claims(
    claims: Sequence[AgentClaim],
    recipes: Sequence[CheckRecipe],
    head_verdicts: Mapping[str, str],
) -> tuple[ClaimResult, ...]:
    """Each claim against the sandboxed head re-execution of its recipe."""

    out: list[ClaimResult] = []
    for claim in claims:
        recipe = match_claim(claim, recipes)
        if recipe is None:
            out.append(ClaimResult(claim, "", "not_reproduced", "", "no single trusted-base recipe runs this command"))
            continue
        sandbox = head_verdicts.get(recipe.id, "")
        if sandbox not in {"PASS", "FAIL"}:
            out.append(
                ClaimResult(
                    claim,
                    recipe.id,
                    "not_reproduced",
                    sandbox,
                    f"sandboxed re-execution is {sandbox or 'not run'}" + (" (flaky)" if sandbox == "UNKNOWN" else ""),
                )
            )
            continue
        if claim.status == "UNKNOWN":
            out.append(ClaimResult(claim, recipe.id, "not_reproduced", sandbox, "the claim states no outcome"))
            continue
        if claim.status == sandbox:
            out.append(ClaimResult(claim, recipe.id, "confirmed", sandbox, f"sandbox agrees: {sandbox}"))
        else:
            out.append(
                ClaimResult(
                    claim,
                    recipe.id,
                    "contradicted",
                    sandbox,
                    f"claimed {claim.status}; sandboxed re-execution is {sandbox}",
                )
            )
    return tuple(out)


# --------------------------------------------------------------------------- #
# behavior producer: only deltas demand attention
# --------------------------------------------------------------------------- #

_SEVERITY: Final[Mapping[str, BehaviorSeverity]] = {
    "introduced_failure": "high",
    "not_comparable": "medium",
    "flaky": "medium",
    "removed": "medium",
    "newly_skipped": "medium",
    "not_run": "medium",
    "fixed": "low",
    "new": "low",
}
_TITLES: Final[Mapping[str, str]] = {
    "introduced_failure": "Introduced failure",
    "not_comparable": "Not comparable",
    "flaky": "Flaky",
    "removed": "Test removed",
    "newly_skipped": "Newly skipped",
    "not_run": "Not run on head",
    "fixed": "Fixed",
    "new": "New test",
}


def _read_json(context: BehaviorContext, item: Any) -> Mapping[str, Any] | None:
    payload = context.read_artifact(item)
    if payload is None or not item.content_hash or hashlib.sha256(payload).hexdigest() != item.content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


class CheckDifferentialProducer:
    """``test_outcome`` behavior targets from persisted check differentials."""

    producer_id = CHECK_DIFFERENTIAL_PRODUCER_ID
    contract_version = BEHAVIOR_CONTRACT_VERSION

    def produce(self, context: BehaviorContext) -> BehaviorProduction:
        newest: dict[str, tuple[Any, Mapping[str, Any]]] = {}
        degraded: list[str] = []
        rows = sorted(
            (
                item
                for item in context.evidence
                if item.mime_type == CHECK_DIFFERENTIAL_MEDIA_TYPE
                and item.revision_id == context.revision.id
                and item.execution_intent != "explicit_historical"
            ),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        for item in rows:
            record = _read_json(context, item)
            if record is None:
                degraded.append(f"check differential {item.id} is missing or does not match its digest")
                continue
            recipe = record.get("recipe")
            recipe_id = str(recipe.get("id") or "") if isinstance(recipe, Mapping) else ""
            if recipe_id:
                newest.setdefault(recipe_id, (item, record))
        deltas: list[BehaviorDelta] = []
        executed: list[ExecutedScope] = []
        unchanged = 0
        for recipe_id in sorted(newest):
            item, record = newest[recipe_id]
            produced, quiet = self._deltas(context, item, record)
            deltas.extend(produced)
            unchanged += quiet
            for attempt in record.get("attempts") or ():
                if not isinstance(attempt, Mapping) or attempt.get("status") == "not_run":
                    continue
                ref = str(attempt.get("evidence_ref") or "")
                if ref:
                    executed.append(
                        ExecutedScope(
                            producer=self.producer_id,
                            run_ref=ref,
                            side="base" if attempt.get("side") == "old" else "head",
                            target_ids=(f"checks.recipe:{recipe_id}",),
                        )
                    )
        return BehaviorProduction(
            deltas=tuple(deltas), executed=tuple(executed), unchanged=unchanged, degraded=tuple(degraded)
        )

    def _deltas(
        self, context: BehaviorContext, item: Any, record: Mapping[str, Any]
    ) -> tuple[list[BehaviorDelta], int]:
        recipe = record.get("recipe") if isinstance(record.get("recipe"), Mapping) else {}
        assert isinstance(recipe, Mapping)
        recipe_id = str(recipe.get("id") or "")
        title = str(recipe.get("title") or recipe_id)
        environment = record.get("environment") if isinstance(record.get("environment"), Mapping) else {}
        assert isinstance(environment, Mapping)
        base_env, head_env = str(environment.get("base") or ""), str(environment.get("head") or "")
        fingerprint = (
            head_env
            if base_env in {"", head_env}
            else "envpair:sha256:" + _digest({"base": base_env, "head": head_env})
        )
        executor = str(record.get("executor_class") or "")
        executor = executor if executor in {"author_machine", "customer_worker", "managed_sandbox"} else ""
        attempts = [row for row in record.get("attempts") or () if isinstance(row, Mapping)]

        def side_ref(side: str) -> str:
            return next(
                (
                    str(row.get("evidence_ref") or "")
                    for row in attempts
                    if row.get("side") == side and row.get("variant") == "run" and row.get("evidence_ref")
                ),
                "",
            )

        base_ref, head_ref = side_ref("old"), side_ref("new")
        refs: list[BehaviorEvidenceRef] = [
            BehaviorEvidenceRef(
                ref=f"artifact:{item.id}",
                kind="artifact",
                label=f"{title} · differential",
                digest=item.content_hash,
                role="record",
                evidence_id=item.id,
                media_type=item.mime_type,
            )
        ]
        for side, ref in (("base", base_ref), ("head", head_ref)):
            if ref:
                refs.append(BehaviorEvidenceRef(ref=ref, kind="check", label=f"{title} · {side} run", side=side))
        common = {
            "producer": self.producer_id,
            "evidence": tuple(refs),
            "environment_fingerprint": fingerprint,
            "executor_class": cast(Any, executor),
            "target_ids": (str(recipe.get("entry_id") or f"checks.recipe:{recipe_id}"),),
        }
        deltas: list[BehaviorDelta] = []
        per_class: dict[str, int] = {}
        overflow: dict[str, int] = {}
        for unit in record.get("attention") or ():
            if not isinstance(unit, Mapping):
                continue
            klass = str(unit.get("class") or "")
            if klass not in ATTENTION_CLASSES:
                continue
            if per_class.get(klass, 0) >= _MAX_DELTAS_PER_CLASS:
                overflow[klass] = overflow.get(klass, 0) + 1
                continue
            per_class[klass] = per_class.get(klass, 0) + 1
            subject = str(unit.get("subject") or "")
            base_marks = [str(value) for value in unit.get("base_attempts") or ()]
            head_marks = [str(value) for value in unit.get("head_attempts") or ()]
            base_status, head_status = str(unit.get("base") or ""), str(unit.get("head") or "")
            name = subject or f"{title} (whole recipe)"
            deltas.append(
                BehaviorDelta(
                    kind="test_outcome",
                    subject=f"{recipe_id}::{subject}" if subject else f"{recipe_id}::recipe",
                    title=f"{_TITLES[klass]} · {name}"[:200],
                    summary=_unit_summary(klass, base_status, head_status, record),
                    base=(
                        BehaviorObservation(
                            "base", base_status, _digest(base_marks), " ".join(base_marks) or base_status, base_ref
                        )
                        if base_status not in {"", "missing"} or base_marks
                        else None
                    ),
                    head=BehaviorObservation(
                        "head", head_status, _digest(head_marks), " ".join(head_marks) or head_status, head_ref
                    ),
                    change="added" if klass == "new" else "removed" if klass == "removed" else "changed",
                    severity=_SEVERITY[klass],
                    critical=klass == "introduced_failure",
                    paths=(str(unit.get("file")),) if unit.get("file") else (),
                    facts=(
                        ("Class", klass.replace("_", " ")),
                        ("Base", " ".join(base_marks) or base_status or "not run"),
                        ("Head", " ".join(head_marks) or head_status),
                        ("Recipe", recipe_id),
                    ),
                    **common,
                )
            )
        for klass, extra in sorted(overflow.items()):
            deltas.append(
                BehaviorDelta(
                    kind="test_outcome",
                    subject=f"{recipe_id}::more:{klass}",
                    title=f"{_TITLES[klass]} · {extra} more in {title}",
                    summary=f"{extra} further {klass.replace('_', ' ')} result(s); see the differential record",
                    head=BehaviorObservation("head", klass, _digest([klass, extra]), str(extra), head_ref),
                    severity=_SEVERITY[klass],
                    critical=klass == "introduced_failure",
                    facts=(("Class", klass.replace("_", " ")), ("Count", str(extra))),
                    **common,
                )
            )
        for row in record.get("adequacy") or ():
            if not isinstance(row, Mapping) or row.get("verdict") != "not_demonstrating":
                continue
            test = str(row.get("test") or "")
            deltas.append(
                BehaviorDelta(
                    kind="test_outcome",
                    subject=f"{recipe_id}::adequacy:{test}",
                    title=f"Test does not demonstrate the change · {test}"[:200],
                    summary="This changed test passes on base source too, so it does not demonstrate the change.",
                    base=BehaviorObservation("base", "passed", _digest(["probe", "passed"]), "base probe passed"),
                    head=BehaviorObservation("head", "passed", _digest(["head", "passed"]), "head passed", head_ref),
                    change="added",
                    severity="medium",
                    paths=(str(row.get("file")),) if row.get("file") else (),
                    facts=(("Adequacy", "not demonstrating"), ("Recipe", recipe_id)),
                    **common,
                )
            )
        for row in record.get("claims") or ():
            if not isinstance(row, Mapping) or row.get("verdict") != "contradicted":
                continue
            claim = row.get("claim") if isinstance(row.get("claim"), Mapping) else {}
            assert isinstance(claim, Mapping)
            claimed, sandbox = str(claim.get("status") or ""), str(row.get("sandbox") or "")
            deltas.append(
                BehaviorDelta(
                    kind="test_outcome",
                    subject=f"{recipe_id}::claim:{claim.get('key') or ''}",
                    title=f"Claim contradicted · {str(claim.get('command') or recipe_id)[:120]}",
                    summary=f"The agent reported {claimed}; sandboxed re-execution of {recipe_id} is {sandbox}.",
                    head=BehaviorObservation("head", sandbox, _digest([claimed, sandbox]), sandbox, head_ref),
                    severity="high" if claimed == "PASS" else "medium",
                    critical=claimed == "PASS" and sandbox == "FAIL",
                    facts=(("Claimed", claimed), ("Sandbox", sandbox), ("Recipe", recipe_id)),
                    **common,
                )
            )
        counts = record.get("counts") if isinstance(record.get("counts"), Mapping) else {}
        assert isinstance(counts, Mapping)
        quiet = sum(
            int(value)
            for key, value in counts.items()
            if key in {"unchanged", "pre_existing_failure", "quarantined"} and isinstance(value, int)
        )
        return deltas, quiet


def _unit_summary(klass: str, base: str, head: str, record: Mapping[str, Any]) -> str:
    if klass == "introduced_failure":
        return f"Fails on head where the base {'had no such test' if base == 'missing' else base}."
    if klass == "not_comparable":
        reasons = [str(item) for item in record.get("reasons") or () if "environment" in str(item)]
        return reasons[0][:400] if reasons else f"Base {base}, head {head}; the sides cannot be compared."
    if klass == "flaky":
        return "Outcomes differ across reruns; flaky results never count as PASS."
    if klass == "not_run":
        return str(record.get("head_guard") or next(iter(record.get("reasons") or ()), "Head side was not run."))[:400]
    if klass == "removed":
        return "Present on base, absent on head."
    if klass == "newly_skipped":
        return f"Ran on base ({base}); skipped on head."
    if klass == "fixed":
        return "Fails on base; passes on head."
    return "Absent on base; present on head."


__all__ = [
    "ATTENTION_CLASSES",
    "CHECK_DIFFERENTIAL_MEDIA_TYPE",
    "CHECK_DIFFERENTIAL_PRODUCER_ID",
    "DIFFERENTIAL_CLASSES",
    "AdequacyResult",
    "AgentClaim",
    "AttemptOutcome",
    "CheckDifferentialProducer",
    "ClaimResult",
    "RecipeDifferential",
    "TestCase",
    "TestFileChange",
    "TestReport",
    "UnitResult",
    "assess_adequacy",
    "claim_status",
    "classify_recipe",
    "combine_verdicts",
    "diff_test_file",
    "is_adequacy_candidate",
    "is_test_path",
    "match_claim",
    "parse_report",
    "recipe_signatures",
    "reconcile_claims",
    "resolve_test_file",
]
