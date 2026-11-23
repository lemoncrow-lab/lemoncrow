"""Per-part proof: one deterministic outcome per (review, revision, changed file).

The outcome answers "what do the repository's own tests say about this part?" with no
model involved. Reach is file-level, from the trusted base's import graph
(:func:`test_impact.changed_reach`): a test "reaches" a changed file when that file is in the
test's import closure. It never claims symbol, line or branch coverage.

Outcomes (the whole vocabulary; a part is never blank):

``proven``       a test that reaches the part fails on the base and passes on this revision
``exercised``    a test that reaches the part passes on this revision
``fails``        a test that reaches the part fails on this revision
``not_tested``   no test reaches the part
``couldnt_run``  the tests could not run, with a reason code (:data:`REASONS`)
``not_run_yet``  tests reach the part but have not run (a large change runs the parts in view first)
``over_budget``  the review's metered minutes ran out before they ran

Storage. The outcomes are *computed when the tests run* (the run holds the full per-test
result and the import graph) and stored as one compact row per Snapshot in the existing
``review_derived`` table (kind ``part_proof``, same table on SQLite and PostgreSQL, deleted with
the Snapshot by the existing cascade). They are not re-derived on read: the stored differential
keeps at most 400 tests, and rebuilding reach needs the import graph. The row is stamped with
the Snapshot's exact base and head identity (the commit in ``commit_range`` mode, the content
fingerprint otherwise): a row for another identity is refused on write and never served on read,
and a new Snapshot starts with no row.

A record is *complete* once the automatic run has finished its plan (or a reviewer's run left no
part undecided). Before the first batch the run writes ``not_run_yet`` for every part some test
reaches, so a part a test reaches never reads "not tested" while tests are still to run, and an
interrupted run leaves an incomplete record that the client resumes. The stamp carries a version
counter and every write is a ``compare_and_set_derived_result`` with a bounded retry, so several
writer processes never lose a batch.

Source. A record's ``source`` says who ran the tests. ``"auto"`` and ``"manual"`` are runs LemonCrow made
(the sandboxed run above). ``"ci"`` is a hosted review's record projected from the repository's own CI result
(:mod:`part_proof_ci`): the same vocabulary and storage, but LemonCrow ran nothing, hosted has no import
graph, and the surface labels it "Reported by CI on <sha>, not run by LemonCrow".

lc-debt: file-level reach, no coverage map (see :func:`test_impact.changed_reach`). Upgrade:
coverage collected under the sandbox.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Final

from lemoncrow.pro.capabilities.review.differential import resolve_test_file
from lemoncrow.pro.capabilities.review.recipes import MAX_SELECTED_TEST_ARGS
from lemoncrow.pro.capabilities.review.snapshot import revision_side_identity
from lemoncrow.pro.capabilities.review.test_impact import ChangedReach

KIND: Final[str] = "part_proof"
SCOPE: Final[str] = ""
VERSION: Final[int] = 1
#: Parts in view run first, in one recipe run of at most this many test files.
FIRST_BATCH_TESTS: Final[int] = 40
#: Then the rest, in one more run of at most this many; anything beyond is "not run yet".
REST_BATCH_TESTS: Final[int] = 60

PROVEN: Final = "proven"
EXERCISED: Final = "exercised"
FAILS: Final = "fails"
NOT_TESTED: Final = "not_tested"
COULDNT_RUN: Final = "couldnt_run"
NOT_RUN_YET: Final = "not_run_yet"
OVER_BUDGET: Final = "over_budget"
#: Weakest first. A merge keeps the stronger fact: a failing reaching test always shows.
OUTCOMES: Final[tuple[str, ...]] = (NOT_TESTED, NOT_RUN_YET, OVER_BUDGET, COULDNT_RUN, EXERCISED, PROVEN, FAILS)

#: Why a part "couldn't run" (the reason code is part of the wire, the text is what the reviewer reads).
REASONS: Final[Mapping[str, str]] = {
    "no_sandbox": "no sandbox is available here, and tests never run unsandboxed on their own",
    "no_recipe": "this repo declares no test recipe",
    "hosted": "hosted reviews do not run tests automatically",
    "no_map": "the import graph for this base is not available, so reach is unknown",
    "too_large": "the change is too large to map to tests",
    "foreign_tests": "this repo's tests are in languages outside the import graph, so reach is unknown",
    "base_unreadable": "the trusted base is not readable yet",
    "unattributed": "the test report did not say which test file failed",
    "error": "the test run did not complete; a reviewer's Run test can try it again",
    "historical": "only the current revision runs tests on its own",
    "runtime_read": "a test may read this file at run time, which the import graph cannot see",
    "doctest": "this documentation file may be collected as a doctest, which the import graph cannot see",
    "shadowing": "an added file may satisfy an import the base could not resolve, so reach is unknown",
    "no_tests": "no Python or JS/TS test files were recognized for this recipe",
    "flaky": "a test that reaches this file failed, then passed on a rerun (flaky), so it proves nothing",
    "deleted": "this file was deleted, so no test claim is made about it",
    "disabled": "per-part proof is turned off in this repo's review settings (check_policy.part_proof)",
    "not_planned": "the review plan does not schedule this repo's test recipe",
    "past_bound": "the tests that reach this file are past this revision's test bound, so a reviewer's Run test is what runs them",
    "no_ci_result": "CI reported no test result for this file, and a hosted review does not map files to tests itself",
    "ci_not_run": "CI reported this changed test as not run on the head (skipped or missing)",
    # P12-2: why a CI check is not believed. The longer explanation of each is in docs/setup/proof.md.
    "untrusted_workflow": "untrusted workflow",
    "workflow_unreadable": "the GitHub App cannot read workflow runs; it needs Actions: Read",
    "proof_report_missing": "the proof report artifact is missing or expired",
    "proof_report_invalid": "the proof report artifact could not be read",
    "proof_report_unattested": "the proof report is not attested by GitHub for this pull request",
    # P13-2: a report that says the pull request's code ran without a sandbox is not believed, attested or not.
    "proof_unsandboxed": "the proof ran without a sandbox, so what it reported cannot be believed",
    # P13-2: the attested proof job had no working sandbox, so it ran nothing. CI's own word, believed: not untrusted.
    "proof_sandbox_missing": "the CI proof job has no working bubblewrap sandbox, so it ran nothing",
    # P13-2: the test failed on head, but the pull request changes a dependency file the proof run did not install (it
    # installs the base's), so the failure may be the old dependencies', not the change's. CI's own word, believed: not
    # untrusted.
    "proof_deps_changed": "the pull request changes a dependency file the CI proof run does not install, so a failure on head may come from the base's dependencies",
}

#: The reasons a CI check is not believed (its parts read ``couldnt_run`` with one of these, and no "reported by CI").
UNTRUSTED_CI_REASONS: Final[tuple[str, ...]] = (
    "untrusted_workflow",
    "workflow_unreadable",
    "proof_report_missing",
    "proof_report_invalid",
    "proof_report_unattested",
    "proof_unsandboxed",
)

LABELS: Final[Mapping[str, str]] = {
    PROVEN: "Proven",
    EXERCISED: "Exercised",
    FAILS: "Fails",
    NOT_TESTED: "Not tested",
    COULDNT_RUN: "Couldn't run",
    NOT_RUN_YET: "Not run yet",
    OVER_BUDGET: "Not run: over this review's budget",
}

DETAILS: Final[Mapping[str, str]] = {
    PROVEN: "A test that reaches this file fails on the base and passes here.",
    EXERCISED: "A test that reaches this file passes. Reach is by the file's imports, not by line or symbol.",
    FAILS: "A test that reaches this file fails.",
    NOT_TESTED: "No test reaches this file through the import graph.",
    NOT_RUN_YET: "Tests reach this file but have not run yet.",
    OVER_BUDGET: "Tests reach this file; they did not run because this review's minutes are spent.",
}

#: ``ProofRecord.source`` of a record built from what the repository's CI reported (hosted reviews). LemonCrow ran
#: nothing for it, and every surface says so (:mod:`part_proof_ci`). Any other source was run by LemonCrow.
CI_SOURCE: Final[str] = "ci"

#: What each outcome means when CI reported it: the test is the changed test file itself, not an import-graph reach.
CI_DETAILS: Final[Mapping[str, str]] = {
    PROVEN: "CI ran this changed test on the base and on the head: it fails on the base and passes on the head.",
    EXERCISED: "CI ran this changed test on the head and it passes. CI did not show it failing on the base.",
    FAILS: "CI ran this changed test on the head and it fails.",
}


class ProofCommitMismatch(ValueError):
    """A result was offered for a Snapshot whose base or head identity it was not produced at."""


@dataclass(frozen=True, slots=True)
class PartOutcome:
    outcome: str
    reason: str = ""
    tests: tuple[str, ...] = ()
    reaching: int = 0

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"o": self.outcome}
        if self.reason:
            wire["r"] = self.reason
        if self.tests:
            wire["t"] = list(self.tests)
        if self.reaching:
            wire["n"] = self.reaching
        return wire

    @classmethod
    def from_wire(cls, raw: Any) -> PartOutcome | None:
        if not isinstance(raw, Mapping) or raw.get("o") not in OUTCOMES:
            return None
        reason = str(raw.get("r") or "")
        return cls(
            outcome=str(raw["o"]),
            reason=reason if reason in REASONS else ("error" if raw["o"] == COULDNT_RUN else ""),
            tests=tuple(str(item) for item in (raw.get("t") or ())[:3]),
            reaching=int(raw.get("n") or 0),
        )


@dataclass(frozen=True, slots=True)
class ProofRecord:
    """Everything stored for one Snapshot, pinned to its exact base and head identity."""

    revision_id: str
    head_identity: str
    base_identity: str
    files: Mapping[str, PartOutcome] = field(default_factory=dict)
    #: Test files already executed for this Snapshot (a later batch never re-runs them).
    ran_tests: tuple[str, ...] = ()
    #: What a part with no entry shows: ``not_tested`` once every reaching test ran or was ruled out.
    default: str = NOT_TESTED
    default_reason: str = ""
    source: str = "auto"
    seconds: float = 0.0
    #: The run finished its plan (or nothing is left undecided). ``False`` means a run may resume.
    complete: bool = False
    #: Write counter; it is part of the row stamp, which is what makes the write a compare-and-set.
    version: int = 0
    #: Test file -> how many automatic attempts at it never really started. It caps the automatic retries.
    tries: Mapping[str, int] = field(default_factory=dict)
    #: A CI-reported record: the order key of the check run it reports (a late older run never overwrites it).
    ci_order: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "v": VERSION,
            "ver": self.version,
            "complete": self.complete,
            "revision_id": self.revision_id,
            "head_identity": self.head_identity,
            "base_identity": self.base_identity,
            "default": self.default,
            "default_reason": self.default_reason,
            "files": {path: row.to_wire() for path, row in sorted(self.files.items())},
            "ran_tests": list(self.ran_tests),
            "source": self.source,
            "seconds": round(self.seconds, 3),
            **({"tries": dict(sorted(self.tries.items()))} if self.tries else {}),
            **({"ci_order": self.ci_order} if self.ci_order else {}),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ProofRecord | None:
        if raw.get("v") != VERSION or raw.get("default") not in OUTCOMES:
            return None
        rows = raw.get("files")
        if not isinstance(rows, Mapping):
            return None
        files = {str(path): row for path, value in rows.items() if (row := PartOutcome.from_wire(value)) is not None}
        return cls(
            revision_id=str(raw.get("revision_id") or ""),
            head_identity=str(raw.get("head_identity") or ""),
            base_identity=str(raw.get("base_identity") or ""),
            files=files,
            ran_tests=tuple(str(item) for item in raw.get("ran_tests") or ()),
            default=str(raw["default"]),
            default_reason=str(raw.get("default_reason") or ""),
            source=str(raw.get("source") or "auto"),
            seconds=float(raw.get("seconds") or 0.0),
            complete=bool(raw.get("complete", False)),
            version=int(raw.get("ver") or 0),
            tries=_tries_of(raw.get("tries")),
            ci_order=str(raw.get("ci_order") or "")[:100],
        )


def _tries_of(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[str(key)] = count
    return out


# --------------------------------------------------------------------------- #
# commit pinning
# --------------------------------------------------------------------------- #


def identities(revision: Any) -> tuple[str, str]:
    """``(head, base)`` content identity of one Snapshot: the commit when there is one."""

    return revision_side_identity(revision, "new"), revision_side_identity(revision, "old")


def stamp_of(revision: Any, version: int = 0) -> str:
    """The row stamp: the exact base and head identity plus the write counter (for compare-and-set)."""

    head, base = identities(revision)
    digest = hashlib.sha256(
        json.dumps([VERSION, str(revision.id), head, base], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"part-proof:v{VERSION}:{digest[:32]}:{version}"


def pinned_to(record: ProofRecord, revision: Any) -> bool:
    head, base = identities(revision)
    return record.revision_id == str(revision.id) and record.head_identity == head and record.base_identity == base


# --------------------------------------------------------------------------- #
# derivation (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TestEvidence:
    """What one recipe run established about test *files* on this Snapshot."""

    __test__ = False  # not a pytest class

    ran: frozenset[str] = frozenset()
    passed: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    proven: frozenset[str] = frozenset()
    #: Test files with a head test that failed and then passed on a rerun: neither a pass nor a failure.
    flaky: frozenset[str] = frozenset()
    #: ``""`` when the recipe ran; else a :data:`REASONS` code.
    not_run: str = ""
    #: The head failed but no failing test file could be named.
    unattributed: bool = False


def reason_code(text: str) -> str:
    """Map a runner's refusal text to a reason code."""

    lowered = text.lower()
    if "bubblewrap" in lowered or "sandbox" in lowered or "unsandboxed" in lowered:
        return "no_sandbox"
    return "error"


def evidence_from_differential(differential: Any, selected: Iterable[str], *, cwd: str = "") -> TestEvidence:
    """Fold one recipe's classified result into test-file evidence (pure; no store, no I/O)."""

    chosen = frozenset(selected)
    prefix = "" if cwd in {"", "."} else cwd.rstrip("/") + "/"

    def norm(path: str) -> str:
        if not path:
            return ""
        if path in chosen:
            return path
        return prefix + path if prefix + path in chosen else path

    def locate(reported: str, test_id: str) -> str:
        """The selected file a result belongs to; a reported file outside them falls back to the test's id."""

        path = norm(reported)
        return path if path in chosen else resolve_test_file(test_id, "", recipe, chosen) or path

    verdict = str(getattr(differential, "head_verdict", "") or "")
    if verdict in {"NOT_RUN", "UNKNOWN"} and not getattr(differential, "tests", ()):
        reasons = " ".join(str(item) for item in getattr(differential, "reasons", ()))
        attempts = " ".join(str(getattr(item, "reason", "") or "") for item in getattr(differential, "attempts", ()))
        return TestEvidence(not_run=reason_code(f"{reasons} {attempts}"))
    failed: set[str] = set()
    passed: set[str] = set()
    flaky: set[str] = set()
    recipe: Any = getattr(differential, "recipe", None) or SimpleNamespace(cwd=cwd or ".", report="junit")
    for unit in getattr(differential, "tests", ()):
        # A report naming no usable file is resolved from the test's id (``tests.test_calc::test_sub``), never guessed.
        file = locate(str(getattr(unit, "file", "") or ""), str(getattr(unit, "subject", "") or ""))
        if not file:
            continue
        if unit.head == "failed":
            failed.add(file)
        elif unit.head == "flaky":
            # The existing check-run vocabulary: a test that passed only on a rerun is flaky, not failing.
            flaky.add(file)
        elif unit.head == "passed":
            passed.add(file)
    flaky -= failed
    passed -= failed | flaky
    unattributed = verdict == "FAIL" and not failed
    if verdict == "PASS":
        # A passing recipe means no selected test failed, whether or not each result names its file.
        passed = set(chosen) - failed - flaky
    proven = {
        locate(str(row.file), str(getattr(row, "test", "") or ""))
        for row in getattr(differential, "adequacy", ())
        if getattr(row, "verdict", "") == "demonstrates" and row.file
    }
    proven |= {
        locate(str(unit.file), str(getattr(unit, "subject", "") or ""))
        for unit in getattr(differential, "tests", ())
        if unit.klass == "fixed" and unit.file
    }
    proven -= failed | flaky
    ran = frozenset(chosen) if verdict in {"PASS", "FAIL"} else frozenset(passed | failed)
    return TestEvidence(
        ran=ran,
        passed=frozenset(passed) | frozenset(proven & ran),
        failed=frozenset(failed),
        proven=frozenset(proven),
        flaky=frozenset(flaky),
        unattributed=unattributed,
    )


def unknown_reason(why: str) -> str:
    """The reason code for a reach the import graph could not decide (*why* is ``ChangedReach.why_unknown``)."""

    if "languages" in why:
        return "foreign_tests"
    if "shadow" in why:
        return "shadowing"
    return "no_map" if "map" in why else "too_large"


def derive_batch(
    reach: ChangedReach,
    evidence: TestEvidence,
    batch: Sequence[str],
    *,
    pending: str = NOT_RUN_YET,
) -> dict[str, PartOutcome]:
    """The outcome of every changed file one recipe run decides, plus the pending ones.

    A file whose reaching tests were not part of *batch* is *pending* (``not_run_yet`` or
    ``over_budget``); a file nothing reaches is left out (the record's default is "not tested").
    When nothing at all could be decided (``reach.whole``) only deleted files are listed: the
    record's default carries the reason once.
    """

    in_batch = frozenset(batch)
    out: dict[str, PartOutcome] = {}
    for path, tests in reach.tests.items():
        if not tests:
            continue
        reaching = len(tests)
        here = [item for item in tests if item in in_batch]
        if not here:
            out[path] = PartOutcome(pending, reaching=reaching)
            continue
        if evidence.not_run:
            out[path] = PartOutcome(COULDNT_RUN, evidence.not_run, tuple(here[:3]), reaching)
            continue
        failing = [item for item in here if item in evidence.failed]
        proving = [item for item in here if item in evidence.proven]
        passing = [item for item in here if item in evidence.passed]
        flaking = [item for item in here if item in evidence.flaky]
        if failing:
            out[path] = PartOutcome(FAILS, "", tuple(failing[:3]), reaching)
        elif proving:
            out[path] = PartOutcome(PROVEN, "", tuple(proving[:3]), reaching)
        elif passing:
            out[path] = PartOutcome(EXERCISED, "", tuple(passing[:3]), reaching)
        elif flaking:
            out[path] = PartOutcome(COULDNT_RUN, "flaky", tuple(flaking[:3]), reaching)
        elif evidence.unattributed:
            out[path] = PartOutcome(COULDNT_RUN, "unattributed", tuple(here[:3]), reaching)
        else:
            out[path] = PartOutcome(COULDNT_RUN, "error", tuple(here[:3]), reaching)
    if not reach.whole:
        for path in reach.unknown:
            out[path] = PartOutcome(COULDNT_RUN, reach.reasons.get(path) or unknown_reason(reach.why_unknown))
    for path in reach.deleted:
        out[path] = PartOutcome(COULDNT_RUN, "deleted")
    return out


def undecided_reason(reach: ChangedReach) -> str:
    """The reason code when nothing about this recipe's reach could be decided, else ``""``."""

    if reach.whole:
        return reach.whole
    # A reach built without a cause (every path unknown, nothing found) is undecided as a whole.
    return unknown_reason(reach.why_unknown) if not reach.tests and not reach.reasons and reach.unknown else ""


def stronger(first: PartOutcome | None, second: PartOutcome | None) -> PartOutcome | None:
    """The stronger of two facts about one file (see :data:`OUTCOMES`)."""

    if first is None or second is None:
        return first or second
    return second if OUTCOMES.index(second.outcome) > OUTCOMES.index(first.outcome) else first


def merged(
    old: ProofRecord | None,
    files: Mapping[str, PartOutcome],
    *,
    revision: Any,
    ran: Iterable[str] = (),
    default: str | None = None,
    default_reason: str = "",
    source: str = "auto",
    seconds: float = 0.0,
    replace: bool = False,
    complete: bool | None = None,
    tries: Mapping[str, int] | None = None,
) -> ProofRecord:
    """The record after one more batch. A fresh run (``replace``) overwrites; an auto batch keeps the stronger fact.

    A fresh run overwrites what it decided with two exceptions: a part it only started or could not
    decide never loses an earlier fact, and a run that merely *exercised* a part never replaces a
    *proven* one (a head-only run cannot say whether the test still fails on the base).
    """

    head, base = identities(revision)
    keep = dict(old.files) if old is not None and pinned_to(old, revision) else {}
    for path, row in files.items():
        waiting = row.outcome in {NOT_RUN_YET, OVER_BUDGET}
        # A fresh run overwrites what it decided; a part it did not reach never loses an earlier fact.
        before = keep.get(path)
        if replace and not waiting:
            held = before is not None and before.outcome == PROVEN and row.outcome == EXERCISED
            keep[path] = before if held and before is not None else row
        else:
            keep[path] = stronger(before, row) or row
    prior = old if old is not None and pinned_to(old, revision) else None
    return ProofRecord(
        revision_id=str(revision.id),
        head_identity=head,
        base_identity=base,
        files=keep,
        ran_tests=tuple(dict.fromkeys((*(prior.ran_tests if prior else ()), *ran))),
        default=default if default is not None else (prior.default if prior else NOT_TESTED),
        default_reason=default_reason if default is not None else (prior.default_reason if prior else ""),
        source=source,
        seconds=(prior.seconds if prior else 0.0) + seconds,
        complete=complete if complete is not None else (prior.complete if prior else False),
        version=prior.version if prior else 0,
        # Counts only grow: a concurrent writer's higher count is never lowered.
        tries={
            key: max((prior.tries if prior else {}).get(key, 0), (tries or {}).get(key, 0))
            for key in {*(prior.tries if prior else {}), *(tries or {})}
        },
    )


# --------------------------------------------------------------------------- #
# batches: the parts in view first, within a bound
# --------------------------------------------------------------------------- #


def plan_batches(
    reach: ChangedReach,
    in_view: Sequence[str],
    ran: Iterable[str] = (),
    *,
    first: int = FIRST_BATCH_TESTS,
    rest: int = REST_BATCH_TESTS,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(tests for the parts in view, further tests)``; tests past both bounds wait for a later run."""

    first = max(0, min(first, MAX_SELECTED_TEST_ARGS))
    rest = max(0, min(rest, MAX_SELECTED_TEST_ARGS))
    done = frozenset(ran)
    order = [path for path in dict.fromkeys(in_view) if reach.tests.get(path)]
    order += [path for path in sorted(reach.tests) if reach.tests[path] and path not in set(order)]
    front: list[str] = []
    back: list[str] = []
    views = frozenset(in_view)
    for path in order:
        for test in reach.tests[path]:
            if test in done or test in front or test in back:
                continue
            if path in views and len(front) < first:
                front.append(test)
            elif len(back) < rest:
                back.append(test)
    return tuple(front), tuple(back)


# --------------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------------- #


def local_sandbox() -> str:
    """The sandbox a command run would use here (``seatbelt``, ``bubblewrap``), or ``""`` for none."""

    if sys.platform == "darwin":
        from lemoncrow.pro.environment.host_tools import host_executable

        return "seatbelt" if host_executable("sandbox-exec") else ""
    return "bubblewrap" if (shutil.which("bwrap") or shutil.which("bubblewrap")) else ""


def eligibility(catalog: Any, *, run_on: str, sandbox: str | None = None) -> tuple[list[Any], str]:
    """``(declared test recipes, "")`` when part proof runs on its own here, else ``([], reason code)``."""

    if run_on != "local":
        return [], "hosted"
    if not getattr(getattr(catalog, "policy", None), "part_proof", True):
        return [], "disabled"
    if getattr(catalog, "base_state", "present") == "provisional":
        return [], "base_unreadable"
    recipes = [item for item in catalog.configured() if item.kind == "test"]
    if not recipes:
        return [], "no_recipe"
    if not (local_sandbox() if sandbox is None else sandbox):
        return [], "no_sandbox"
    return recipes, ""


# --------------------------------------------------------------------------- #
# storage: one stamped row per Snapshot in review_derived
# --------------------------------------------------------------------------- #

#: Compare-and-set attempts for one update before the write is given up as contended.
MAX_WRITE_ATTEMPTS: Final[int] = 24


def _record_of(revision: Any, row: tuple[str, Mapping[str, Any]] | None) -> ProofRecord | None:
    """The record a stored ``(stamp, payload)`` row is for this exact Snapshot, else ``None``."""

    if row is None:
        return None
    stamp, payload = row
    record = ProofRecord.from_wire(payload)
    if record is None or not pinned_to(record, revision) or stamp != stamp_of(revision, record.version):
        return None
    return record


def read_records(store: Any, revisions: Iterable[Any]) -> dict[str, ProofRecord]:
    """The stored records of these Snapshots in one read. A row that is not for the exact identity is dropped."""

    by_id = {str(item.id): item for item in revisions}
    if not by_id:
        return {}
    rows = store.derived_results(KIND, list(by_id))
    out: dict[str, ProofRecord] = {}
    for (revision_id, scope), row in rows.items():
        revision = by_id.get(revision_id)
        if revision is None or scope != SCOPE:
            continue
        record = _record_of(revision, row)
        if record is not None:
            out[revision_id] = record
    return out


def _row(revision: Any, record: ProofRecord) -> Any:
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    return DerivedResult(
        review_id=str(revision.review_id),
        revision_id=str(revision.id),
        kind=KIND,
        scope_key=SCOPE,
        stamp=stamp_of(revision, record.version),
        payload=record.to_wire(),
    )


def _check_pinned(revision: Any, record: ProofRecord) -> None:
    if not pinned_to(record, revision):
        raise ProofCommitMismatch(
            f"a proof record for {record.head_identity[:12] or 'another head'} cannot be stored on revision "
            f"{str(revision.id)[:12]} ({identities(revision)[0][:12]})"
        )


def write_record(store: Any, revision: Any, record: ProofRecord) -> None:
    """Store *record* for *revision*; refused unless it was produced at this exact base and head."""

    _check_pinned(revision, record)
    store.put_derived_results([_row(revision, record)])


def update_record(store: Any, revision: Any, change: Callable[[ProofRecord | None], ProofRecord]) -> ProofRecord:
    """Read-modify-write one Snapshot's record: ``change(old | None) -> ProofRecord`` (pure; it may run again).

    The write is a compare-and-set on the row stamp (identity plus version), so a concurrent writer in
    this or any other process makes this attempt retry from the newer row instead of overwriting it.
    """

    revision_id = str(revision.id)
    for _ in range(MAX_WRITE_ATTEMPTS):
        row = store.derived_results(KIND, [revision_id]).get((revision_id, SCOPE))
        old = _record_of(revision, row)
        new = replace(change(old), version=(old.version + 1) if old is not None else 1)
        _check_pinned(revision, new)
        if row is None:
            created, _stamp, _payload = store.insert_derived_result(_row(revision, new))
            if created:
                return new
        elif store.compare_and_set_derived_result(_row(revision, new), expected_stamp=row[0]):
            return new
    raise RuntimeError(f"part proof for revision {revision_id[:12]} is being written by another run; try again")


# --------------------------------------------------------------------------- #
# the wire a surface reads
# --------------------------------------------------------------------------- #


def projection(
    revision: Any, record: ProofRecord | None, *, fallback: str, fallback_reason: str = "", running: bool = False
) -> dict[str, Any]:
    """The sparse batch answer for one Snapshot: a default plus the exceptions, and the vocabulary.

    With no record, every part shows *fallback* (``not_run_yet`` when a run is coming,
    ``couldnt_run`` plus a reason otherwise), so a part is never blank.
    """

    head, base = identities(revision)
    default = record.default if record is not None else fallback
    reason = record.default_reason if record is not None else fallback_reason
    return {
        "revision_id": str(revision.id),
        "head_identity": head,
        "base_identity": base,
        "reach": "file",
        "default": PartOutcome(default, reason if default == COULDNT_RUN else "").to_wire(),
        "files": {path: row.to_wire() for path, row in sorted(record.files.items())} if record is not None else {},
        "recorded": record is not None,
        "complete": record.complete if record is not None else False,
        "running": running,
        "source": record.source if record is not None else "",
        "labels": dict(LABELS),
        "details": (
            ({**DETAILS, **CI_DETAILS} if record.source == CI_SOURCE else dict(DETAILS))
            if record is not None
            else {**DETAILS, NOT_RUN_YET: "No tests have been run for this revision yet."}
        ),
        "reasons": dict(REASONS),
    }
