"""P11-4: the Action's ``LemonCrow Proof`` check text read as per-part proof (no hosted store needed here).

The hosted contract on SQLite and PostgreSQL lives in ``enterprise/server/tests/test_hosted_part_proof_ci.py``; these
pin the reader against the Action's own renderer, so a wording change on either side fails here, not in production.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.review import part_proof as pp
from lemoncrow.pro.capabilities.review import part_proof_ci as ci
from lemoncrow.pro.capabilities.review.proof import report, run, wire

HEAD = "b" * 40
BASE = "a" * 40  # the base branch tip: the Snapshot's base_sha
MERGE = "e" * 40  # the merge base: what the Action plans on
TEST = "tests/test_app.py"
PARTS = frozenset({TEST, "pkg/app.py", "tests/test_other.py"})


def _revision(**kwargs: str) -> SimpleNamespace:
    fields = {"id": "r1", "head_sha": HEAD, "base_sha": BASE, "merge_base_sha": MERGE, "range_mode": "commit_range"}
    return SimpleNamespace(**{**fields, **kwargs})


def _row(path: str, head: str, base: str) -> dict[str, str]:
    outcome, reason = run.red_verdict(head, base, base_run=SimpleNamespace(status="passed", output="", report_text=""))
    return {"path": path, "test": "", "outcome": outcome, "reason": reason}


def _text(
    rows: list[dict[str, str]],
    *,
    head: str = HEAD,
    base: str = MERGE,
    sandbox: str = "bubblewrap",
    usable: bool = True,
) -> str:
    summary = report.Summary(
        head_sha=head,
        base_sha=base,
        sandbox=sandbox,
        sandbox_reason="sandboxed" if sandbox == "bubblewrap" else "no sandbox",
        findings=[],
        red=rows,
        red_overflow=0,
        recorded=True,
        notes=[],
        sandbox_usable=usable,
    )
    return report.check_summary(summary)[0]


def _outcomes(text: str, revision: object | None = None) -> dict[str, str] | None:
    parsed = ci.parse_report(text)
    assert parsed is not None
    shown = ci.outcomes_for(parsed, revision or _revision(), PARTS)
    return None if shown is None else {path: item.outcome for path, item in shown.items()}


@pytest.mark.parametrize(
    ("head", "base", "expected"),
    [
        ("passed", "failed", pp.PROVEN),
        ("passed", "passed", pp.EXERCISED),
        ("passed", "skipped", pp.EXERCISED),
        ("passed", "missing", pp.EXERCISED),
        ("failed", "passed", pp.FAILS),
        ("failed", "failed", pp.FAILS),
        ("skipped", "passed", pp.COULDNT_RUN),
        ("missing", "failed", pp.COULDNT_RUN),
    ],
)
def test_every_verdict_the_action_can_write_reads_back_as_the_honest_outcome(
    head: str, base: str, expected: str
) -> None:
    assert _outcomes(_text([_row(TEST, head, base)])) == {TEST: expected}


def test_a_differential_needs_the_reports_base_to_be_the_snapshots_base() -> None:
    rows = [_row(TEST, "passed", "failed"), _row("tests/test_other.py", "failed", "passed")]
    # Neither the merge base nor the tip: the base-side claim is dropped; what the head showed stays.
    assert _outcomes(_text(rows, base="d" * 40)) == {TEST: pp.EXERCISED, "tests/test_other.py": pp.FAILS}


def test_the_merge_base_or_the_base_tip_is_the_snapshots_base() -> None:
    rows = [_row(TEST, "passed", "failed")]
    assert _outcomes(_text(rows, base=MERGE)) == {TEST: pp.PROVEN}  # the Action plans on the merge base
    # With a merge base recorded, the tip is not the base: a commit only the base branch has can fail the test there.
    assert _outcomes(_text(rows, base=BASE)) == {TEST: pp.EXERCISED}
    # A Snapshot that never recorded its merge base matches on the tip, and one with neither claims nothing.
    assert _outcomes(_text(rows, base=BASE), _revision(merge_base_sha="")) == {TEST: pp.PROVEN}
    assert _outcomes(_text(rows, base=MERGE), _revision(merge_base_sha="")) == {TEST: pp.EXERCISED}
    assert _outcomes(_text(rows, base=MERGE), _revision(merge_base_sha="", base_sha="")) == {TEST: pp.EXERCISED}
    assert _outcomes(_text(rows, base=MERGE), _revision(merge_base_sha="not-a-sha", base_sha="")) == {
        TEST: pp.EXERCISED
    }


@pytest.mark.parametrize("variant", ["none", "missing", "garbage", "empty", "unquoted", "late", "bubblewrap-plus"])
def test_a_report_that_does_not_say_it_was_sandboxed_claims_nothing(variant: str) -> None:
    # P13-2: the head side is as forgeable as the base side when the pull request's code ran plainly, so no row of such
    # a report is believed, whichever CLI version wrote it. Every part it names says why.
    rows = [_row(TEST, "passed", "failed"), _row("tests/test_other.py", "failed", "passed")]
    honest = _text(rows)
    assert _outcomes(honest) == {TEST: pp.PROVEN, "tests/test_other.py": pp.FAILS}
    text = {
        "none": _text(rows, sandbox="none"),
        "missing": "\n".join(line for line in honest.splitlines() if not line.startswith("Sandbox:")),
        "garbage": honest.replace("Sandbox: `bubblewrap`", "Sandbox: `\u0000\u202e`"),
        "empty": honest.replace("Sandbox: `bubblewrap`", "Sandbox: ``"),
        "unquoted": honest.replace("Sandbox: `bubblewrap`", "Sandbox: bubblewrap"),
        # The line must be where the Action writes it, not somewhere a table cell or a finding could put one.
        "late": honest.replace("Sandbox: `bubblewrap`", "Sandbox: `none`") + "\n\nSandbox: `bubblewrap` (x).",
        "bubblewrap-plus": honest.replace("Sandbox: `bubblewrap`", "Sandbox: `bubblewrap2`"),
    }[variant]
    assert _outcomes(text) == {TEST: pp.COULDNT_RUN, "tests/test_other.py": pp.COULDNT_RUN}
    parsed = ci.parse_report(text)
    assert parsed is not None
    shown = ci.outcomes_for(parsed, _revision(), PARTS)
    assert shown is not None and {item.reason for item in shown.values()} == {ci.UNSANDBOXED} == {"proof_unsandboxed"}
    assert ci.UNSANDBOXED in pp.REASONS and ci.UNSANDBOXED in pp.UNTRUSTED_CI_REASONS
    assert "without a sandbox" in pp.REASONS[ci.UNSANDBOXED]


def test_an_unsandboxed_report_names_only_the_parts_it_has_rows_for() -> None:
    text = _text([_row(TEST, "passed", "failed"), _row("gone/not_a_part.py", "passed", "failed")], sandbox="none")
    assert _outcomes(text) == {TEST: pp.COULDNT_RUN}  # no claim, not even a refusal, about a file the Review lacks
    # Another head is dropped whole, sandboxed or not.
    assert _outcomes(_text([_row(TEST, "passed", "failed")], head="c" * 40, sandbox="none")) is None


def test_a_row_that_says_the_sandbox_was_missing_reads_as_its_own_couldnt_run() -> None:
    # The one word ``run._red_for_file`` writes when nothing ran for want of a sandbox; the report itself is believed.
    missing = {"path": TEST, "test": "", "outcome": "could_not_run", "reason": wire.RED_SANDBOX_MISSING}
    parsed = ci.parse_report(_text([missing, _row("tests/test_other.py", "passed", "failed")]))
    assert parsed is not None and parsed.sandboxed
    shown = ci.outcomes_for(parsed, _revision(), PARTS)
    assert shown == {
        TEST: pp.PartOutcome(pp.COULDNT_RUN, "proof_sandbox_missing"),
        "tests/test_other.py": pp.PartOutcome(pp.PROVEN),
    }
    assert ci.SANDBOX_MISSING == "proof_sandbox_missing" and ci.SANDBOX_MISSING in pp.REASONS
    assert ci.SANDBOX_MISSING not in pp.UNTRUSTED_CI_REASONS
    # Not the wording of any other could-not-run row, so it is never read as a skipped or undecided test.
    assert not ci._HEAD_NOT_PASSED.match(wire.RED_SANDBOX_MISSING)
    assert not ci._BASE_UNDECIDED.match(wire.RED_SANDBOX_MISSING)


def test_a_failure_under_a_changed_dependency_file_reads_as_its_own_couldnt_run_never_fails() -> None:
    # What ``run.red_verdict`` writes for a test that failed on head while the pull request changes a dependency file.
    verdict = run.red_verdict(
        "failed", "passed", base_run=SimpleNamespace(status="passed", output="", report_text=""), deps_changed=True
    )
    assert verdict == ("could_not_run", wire.RED_DEPS_CHANGED)
    plain = run.red_verdict("failed", "passed", base_run=SimpleNamespace(status="passed", output="", report_text=""))
    assert plain == ("could_not_run", wire.RED_HEAD_FAILED)
    row = {"path": TEST, "test": "", "outcome": verdict[0], "reason": verdict[1]}
    parsed = ci.parse_report(_text([row, _row("tests/test_other.py", "failed", "passed")]))
    assert parsed is not None
    shown = ci.outcomes_for(parsed, _revision(), PARTS)
    assert shown == {
        TEST: pp.PartOutcome(pp.COULDNT_RUN, "proof_deps_changed"),
        "tests/test_other.py": pp.PartOutcome(pp.FAILS),  # no dependency file changed for this row: still a failure
    }
    assert ci.DEPS_CHANGED == "proof_deps_changed" and ci.DEPS_CHANGED in pp.REASONS
    # CI's own believed word (like a missing sandbox), not a check that was refused: the "reported by CI" line stays.
    assert ci.DEPS_CHANGED not in pp.UNTRUSTED_CI_REASONS
    assert not ci._HEAD_NOT_PASSED.match(wire.RED_DEPS_CHANGED) and not ci._BASE_UNDECIDED.match(wire.RED_DEPS_CHANGED)
    assert wire.RED_DEPS_CHANGED not in (wire.RED_HEAD_FAILED, wire.RED_SANDBOX_MISSING)


def _records(monkeypatch: pytest.MonkeyPatch) -> list[pp.ProofRecord]:
    captured: list[pp.ProofRecord] = []
    monkeypatch.setattr(pp, "update_record", lambda _s, _r, change: captured.append(change(None)) or captured[-1])
    return captured


def test_a_part_with_no_row_reads_the_sandbox_reason_when_the_sandbox_was_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _records(monkeypatch)
    missing = {"path": TEST, "test": "", "outcome": "could_not_run", "reason": wire.RED_SANDBOX_MISSING}
    # a row that says so, a summary that says so with no row at all (a pull request that changes no test), and a normal run
    ci.record_report(_Store(), _revision(), _text([missing]))
    ci.record_report(_Store(), _revision(), _text([], usable=False))
    ci.record_report(_Store(), _revision(), _text([_row(TEST, "passed", "failed")]))
    by_row, by_line, normal = captured
    assert by_row.default_reason == "proof_sandbox_missing" and by_line.default_reason == "proof_sandbox_missing"
    assert by_line.files == {} and by_line.default == pp.COULDNT_RUN
    assert normal.default_reason == "no_ci_result"  # a run that worked and named no row for a part: nothing to blame
    parsed = ci.parse_report(_text([], usable=False))
    assert parsed is not None and parsed.sandbox_missing and parsed.sandboxed
    working = ci.parse_report(_text([]))
    assert working is not None and not working.sandbox_missing


def test_the_sandbox_reason_is_never_the_default_of_a_report_that_is_not_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _records(monkeypatch)
    # refused as a check, for another commit, or one that does not say it was sandboxed: each says nothing about this head
    ci.record_report(_Store(), _revision(), _text([], usable=False), untrusted=ci.UNTRUSTED)
    ci.record_report(_Store(), _revision(), _text([], head="c" * 40, usable=False))
    ci.record_report(_Store(), _revision(), _text([], sandbox="none", usable=False))
    assert [record.default_reason for record in captured] == ["no_ci_result"] * 3
    # and a finding's text, one table cell, cannot write the marker onto the Sandbox line
    forged = _text([]).replace("No open P0/P1 finding to reproduce.", wire.SANDBOX_UNUSABLE_MARK)
    parsed = ci.parse_report(forged)
    assert parsed is not None and not parsed.sandbox_missing


def test_a_sandbox_line_a_finding_could_forge_is_not_read() -> None:
    # An attacker-written finding title is one table cell, never a line of its own, so it cannot say "bubblewrap".
    forged = _text([_row(TEST, "passed", "failed")], sandbox="none").replace(
        "No open P0/P1 finding to reproduce.", "Sandbox: `bubblewrap` (forged)."
    )
    assert _outcomes(forged) == {TEST: pp.COULDNT_RUN}


def test_a_report_for_another_head_is_dropped_whole() -> None:
    text = _text([_row(TEST, "passed", "failed")], head="c" * 40)
    assert _outcomes(text) is None
    # ... and so is one read against a Snapshot that is not a pinned commit range.
    assert (
        _outcomes(_text([_row(TEST, "passed", "failed")]), SimpleNamespace(head_sha=HEAD, base_sha=BASE, range_mode=""))
        is None
    )
    assert (
        _outcomes(
            _text([_row(TEST, "passed", "failed")]),
            SimpleNamespace(head_sha="", base_sha=BASE, range_mode="commit_range"),
        )
        is None
    )


def test_only_files_of_this_change_get_an_outcome() -> None:
    text = _text([_row("tests/not_in_this_change.py", "passed", "failed"), _row(TEST, "passed", "failed")])
    assert _outcomes(text) == {TEST: pp.PROVEN}


def test_the_strongest_honest_fact_wins_when_a_file_has_several_rows() -> None:
    rows = [_row(TEST, "passed", "passed"), _row(TEST, "passed", "failed"), _row(TEST, "failed", "passed")]
    assert _outcomes(_text(rows)) == {TEST: pp.FAILS}


@pytest.mark.parametrize("value", [None, 7, b"x", "", "no header", "**x** on `short` against base `short`", "|" * 5000])
def test_a_malformed_summary_is_not_a_report(value: object) -> None:
    assert ci.parse_report(value) is None


def test_only_exact_action_rows_are_read_and_unsafe_paths_are_dropped() -> None:
    lines = [
        f"**t** on `{HEAD[:12]}` against base `{BASE[:12]}`",
        "",
        "Sandbox: `bubblewrap` (sandboxed).",
        "",
        "**Red-on-base**: the pull request's added or changed tests, run on the base commit.",
        "",
        "| Test | Outcome | Why |",
        "| --- | --- | --- |",
        f"| `{TEST}::test_x` | Fails on base | ok |",
        "| `../etc/passwd` | Fails on base | traversal |",
        "| `/abs.py` | Fails on base | absolute |",
        f"| `{TEST}` | Proven by magic | not a label |",
        f"| {TEST} | Fails on base | no code span |",
        f"| `{TEST}` | Fails on base | a | b |",
    ]
    parsed = ci.parse_report("\n".join(lines))
    assert parsed is not None and [row[:2] for row in parsed.rows] == [(TEST, "fails_on_base")]


def test_the_rows_are_read_from_the_red_on_base_section_only() -> None:
    header = f"**t** on `{HEAD[:12]}` against base `{MERGE[:12]}`\n\nSandbox: `bubblewrap` (x).\n\n"
    row = f"| `{TEST}` | Fails on base | x |"
    before = header + row + "\n\n"  # a row-shaped line before the section is not a Red-on-base row
    assert ci.parse_report(before) is not None and ci.parse_report(before).rows == ()  # type: ignore[union-attr]
    section = "**Red-on-base**: the pull request's added or changed tests, run on the base commit.\n\n"
    parsed = ci.parse_report(before + section + row)
    assert parsed is not None and [item[0] for item in parsed.rows] == [TEST]


def test_the_summary_the_section_and_the_rows_are_bounded_at_and_past_the_bound() -> None:
    header = f"**t** on `{HEAD[:12]}` against base `{MERGE[:12]}`\n\nSandbox: `bubblewrap` (x).\n\n"
    section = "**Red-on-base**: the pull request's added or changed tests, run on the base commit.\n\n"
    row = f"| `{TEST}` | Fails on base | x |\n"
    # Wide rows, so the character bound (not the row cap) is what ends the read.
    wide = f"| `{TEST}` | Fails on base | {'w' * 280} |\n"
    fits = (ci.MAX_SECTION - len(section)) // len(wide)
    assert fits < wire.MAX_ITEMS
    for count in (fits - 1, fits, fits + 1, fits + 50):
        parsed = ci.parse_report(header + section + wide * count)
        assert parsed is not None and len(parsed.rows) == min(count, fits)
    short = ci.parse_report(header + section + row * (wire.MAX_ITEMS + 50))
    assert short is not None and len(short.rows) == wire.MAX_ITEMS
    # Findings above the section never eat into it, whatever their size, up to the summary bound ...
    findings = "| f | P1 | x | y |\n" * 1500
    assert len(findings) > 4096 and len(header + findings) < ci.MAX_SUMMARY - ci.MAX_SECTION
    big = ci.parse_report(header + findings + section + row * 3)
    assert big is not None and len(big.rows) == 3
    # ... and a section that starts at or past the summary bound is not read at all.
    start = ci.MAX_SUMMARY - len(section) - len(row)
    inside = ci.parse_report(header + "x" * (start - len(header)) + "\n" + section + row)
    assert inside is not None and len(inside.rows) == 1
    past = ci.parse_report(header + "x" * (ci.MAX_SUMMARY - len(header)) + "\n" + section + row * 3)
    assert past is not None and past.rows == ()


def test_the_readers_reason_patterns_track_the_actions_wording() -> None:
    # If either side rewords a reason, the head-only/not-run rows would silently become "no claim": fail loudly.
    assert ci._HEAD_NOT_PASSED.match(wire.RED_HEAD_NOT_PASSED.format("skipped"))
    assert ci._BASE_UNDECIDED.match(wire.RED_BASE_UNDECIDED.format("missing"))
    assert not ci._HEAD_NOT_PASSED.match(wire.RED_HEAD_FAILED)
    assert set(ci._BY_LABEL.values()) == set(report.RED_LABELS)


def test_only_the_actions_completed_proof_check_speaks() -> None:
    ok = {"integration": "github-actions", "name": wire.CHECK_NAME, "state": "completed", "actor_kind": "app"}
    assert ci.reports_proof(**ok)
    for key, value in (
        ("integration", "other-ci"),
        ("integration", ""),
        ("name", wire.CHECK_NAME + " (fork)"),
        ("name", wire.CHECK_NAME.lower()),
        ("state", "in_progress"),
        ("actor_kind", "user"),
    ):
        assert not ci.reports_proof(**{**ok, key: value})


# --------------------------------------------------------------------------- #
# P12-2: which workflow posted the check
# --------------------------------------------------------------------------- #

WORKFLOW = ".github/workflows/review.yml"


PULL, TARGET = "42", "main"  # the Review's pull request number and base branch name


def _bound(
    path: str = WORKFLOW,
    *,
    head: str = HEAD,
    refs: tuple[str, ...] = (),
    pull: str = PULL,
    base_ref: str = TARGET,
) -> ci.WorkflowBinding:
    return ci.WorkflowBinding(bound=True, path=path, head_sha=head, refs=refs, pull=pull, base_ref=base_ref)


def _trust(binding: ci.WorkflowBinding | None, revision: object, changed: frozenset[str] | None) -> str:
    return ci.workflow_trust(binding, revision, changed, pull=PULL, base_ref=TARGET)


def test_a_workflow_the_pull_request_leaves_alone_is_trusted() -> None:
    changed = frozenset({TEST, "pkg/app.py", ".github/workflows/other.yml", ".github/actions-notes.md"})
    assert _trust(_bound(), _revision(), changed) == ""
    assert _trust(_bound(head=HEAD.upper()), _revision(), changed) == ""


@pytest.mark.parametrize(
    "touched",
    [
        {WORKFLOW},
        {WORKFLOW.upper()},
        {"./" + WORKFLOW},
        {".github/workflows/reusable.yml"},
        {".github/actions/setup/action.yml"},
        {".GitHub/Actions/setup/action.yml"},
        {".github/actions/a/b/c.sh"},
    ],
)
def test_a_pull_request_that_changes_the_workflow_its_reusable_part_or_a_local_action_is_untrusted(
    touched: set[str],
) -> None:
    binding = _bound(refs=(".github/workflows/reusable.yml",))
    assert _trust(binding, _revision(), frozenset({TEST} | touched)) != ""


@pytest.mark.parametrize(
    "binding",
    [
        None,
        ci.WorkflowBinding(),
        ci.WorkflowBinding(bound=False, path=WORKFLOW, head_sha=HEAD, pull=PULL, base_ref=TARGET),
        _bound(head="c" * 40),
        _bound(head=""),
        _bound(path=""),
        _bound(path="dynamic/bot/run"),
        _bound(path="workflows/review.yml"),
        _bound(path=".github/workflows/../../x.yml"),
    ],
)
def test_a_check_with_no_usable_binding_is_untrusted(binding: ci.WorkflowBinding | None) -> None:
    assert _trust(binding, _revision(), frozenset({TEST})) != ""


def test_unknown_changed_files_are_untrusted() -> None:
    assert _trust(_bound(), _revision(), None) != ""
    assert _trust(_bound(), _revision(head_sha=""), frozenset()) != ""


@pytest.mark.parametrize(
    "binding",
    [
        _bound(pull="43"),  # PR B of the same head: its run is not this pull request's
        _bound(pull=""),
        _bound(base_ref="evilbase"),
        _bound(base_ref=""),
    ],
)
def test_a_run_for_another_pull_request_or_base_branch_is_untrusted(binding: ci.WorkflowBinding) -> None:
    assert _trust(binding, _revision(), frozenset({TEST})) == (
        "the check's workflow run was for another pull request or base branch"
    )


@pytest.mark.parametrize(("pull", "base_ref"), [("", TARGET), (PULL, ""), ("", "")])
def test_a_review_that_does_not_know_its_pull_request_or_base_trusts_nothing(pull: str, base_ref: str) -> None:
    assert ci.workflow_trust(_bound(), _revision(), frozenset({TEST}), pull=pull, base_ref=base_ref) != ""


def test_changed_paths_reads_paths_and_rename_sources_and_refuses_a_malformed_packet() -> None:
    packet = {"files": [{"path": "a.py", "old_path": None}, {"path": "b.py", "old_path": "old/b.py"}]}
    assert ci.changed_paths(packet) == frozenset({"a.py", "b.py", "old/b.py"})
    assert ci.changed_paths({"files": []}) == frozenset()
    for bad in (None, [], {}, {"files": "x"}, {"files": [{"path": 3}]}, {"files": ["a.py"]}):
        assert ci.changed_paths(bad) is None


class _Store:
    def __init__(self) -> None:
        self.units = [SimpleNamespace(path=path, kind="file") for path in sorted(PARTS)]
        self.records: dict[str, object] = {}

    def list_units(self, _revision_id: str) -> list[SimpleNamespace]:
        return self.units


def test_an_untrusted_check_marks_every_part_it_reported_and_proves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[pp.ProofRecord] = []

    def update(_store: object, _revision: object, change: object) -> pp.ProofRecord:
        record = change(None)  # type: ignore[operator]
        captured.append(record)
        return record  # type: ignore[no-any-return]

    monkeypatch.setattr(pp, "update_record", update)
    rows = [_row(TEST, "passed", "failed"), _row("tests/test_other.py", "failed", "passed")]
    text = _text(rows)
    ci.record_report(_Store(), _revision(), text, untrusted=ci.UNTRUSTED)
    ci.record_report(_Store(), _revision(), text)
    ci.record_report(_Store(), _revision(), _text(rows, head="c" * 40), untrusted=ci.UNTRUSTED)
    untrusted, trusted, other_head = captured
    mark = pp.PartOutcome(pp.COULDNT_RUN, "untrusted_workflow")
    assert untrusted.files == {TEST: mark, "tests/test_other.py": mark} and untrusted.default_reason == "no_ci_result"
    assert untrusted.source == pp.CI_SOURCE and "untrusted_workflow" in pp.REASONS
    assert trusted.files[TEST].outcome == pp.PROVEN and trusted.files["tests/test_other.py"].outcome == pp.FAILS
    assert other_head.files == {}  # a report for another commit says nothing, trusted or not


@pytest.mark.parametrize("reason", pp.UNTRUSTED_CI_REASONS)
def test_each_reason_a_check_cannot_be_believed_for_reads_as_its_own_couldnt_run(
    reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[pp.ProofRecord] = []
    monkeypatch.setattr(pp, "update_record", lambda _s, _r, change: captured.append(change(None)) or captured[-1])
    ci.record_report(_Store(), _revision(), _text([_row(TEST, "passed", "failed")]), untrusted=reason)
    assert captured[0].files == {TEST: pp.PartOutcome(pp.COULDNT_RUN, reason)} and reason in pp.REASONS
    assert len(pp.REASONS[reason]) < 100 and "(" not in pp.REASONS[reason]  # a short label, no parenthetical
    # a reason this module does not know falls back to the generic one, never to a made-up label
    ci.record_report(_Store(), _revision(), _text([_row(TEST, "passed", "failed")]), untrusted="made_up")
    assert captured[1].files == {TEST: pp.PartOutcome(pp.COULDNT_RUN, ci.UNTRUSTED)}


def test_the_newest_check_decides_trusted_or_not_and_an_older_one_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # P12-2 round 3 (m2): the P11-4 rule again. A check nobody can verify that is newer than a trusted report replaces
    # it ("Couldn't run", never a proof) and the next genuine run restores the verdict: the documented insider ceiling.
    rows = [_row(TEST, "passed", "failed")]
    text = _text(rows)
    stored: list[pp.ProofRecord | None] = [None]

    def update(_store: object, _revision: object, change: object) -> pp.ProofRecord | None:
        stored[0] = change(stored[0])  # type: ignore[operator]
        return stored[0]

    monkeypatch.setattr(pp, "update_record", update)
    ci.record_report(_Store(), _revision(), text, run_order="2026-10-01T09:59:00Z|00000000000009000001:0001")
    trusted = stored[0]
    assert trusted is not None and trusted.files[TEST].outcome == pp.PROVEN
    # an unverifiable check that is newer replaces it, and says it could not run
    newer = "2026-10-02T00:00:00Z|00000000000009000003:0001"
    ci.record_report(_Store(), _revision(), text, run_order=newer, untrusted=ci.UNTRUSTED)
    assert stored[0] is not None and stored[0].files[TEST] == pp.PartOutcome(pp.COULDNT_RUN, ci.UNTRUSTED)
    # an older one, trusted or not, changes nothing
    held = stored[0]
    for order, why in (("2026-10-01T09:00:00Z|00000000000009000000:0001", ""), ("2026-10-01T08:00:00Z|1:0001", "x")):
        ci.record_report(_Store(), _revision(), text, run_order=order, untrusted=why)
        assert stored[0] == held
    # and a later genuine run restores the verdict
    ci.record_report(
        _Store(),
        _revision(),
        _text([_row(TEST, "passed", "passed")]),
        run_order="2026-10-03T10:30:00Z|00000000000009000004:0001",
    )
    assert stored[0] is not None and stored[0].files[TEST].outcome == pp.EXERCISED
