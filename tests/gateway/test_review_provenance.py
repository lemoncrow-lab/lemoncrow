"""Agent provenance in review packets: the heuristic and, above all, its floor.

No session record anywhere stores a commit sha, so the correlation behind
``collect_provenance`` is a scored guess. The weak-evidence paths therefore get at least as much coverage
as the happy path here: an empty store, a store the correlator cannot open, a
near-tie, a host that records no reads, and a range with no candidate must all
produce a value -- and when the evidence is thin that value must be a
fully-blank ``unknown`` record rather than a plausible-looking guess.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from lemoncrow.core.foundation.history_store import HistoryStore
from lemoncrow.core.foundation.models import CommandRecord, FileEditRecord, Trace
from lemoncrow.pro.capabilities.review.models import ImpactSite, ProvenanceRecord
from lemoncrow.pro.capabilities.review.provenance import (
    collect_evidence,
    collect_provenance,
    link_impact_to_provenance,
    load_trace_for_session,
    split_files_touched,
)

_ANCHOR = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
_PACKET_PATHS = (
    "src/app/service.py",
    "src/app/router.py",
    "src/app/models.py",
    "src/app/views.py",
    "tests/test_service.py",
)


def _trace(
    *,
    trace_id: str,
    session_id: str,
    workspace: Path | str | None,
    edited: tuple[str, ...] = (),
    reads: tuple[str, ...] = (),
    commands: tuple[str | CommandRecord, ...] = (),
    created_at: datetime | None = None,
    host: str = "claude",
    model: str = "claude-opus-4",
    telemetry: dict[str, object] | None = None,
) -> Trace:
    touched: list[str | FileEditRecord] = [FileEditRecord(path=path, diff="@@\n") for path in edited]
    touched.extend(reads)
    return Trace(
        id=trace_id,
        session_id=session_id,
        agent="claude",
        domain="code",
        task="wire the router to the service",
        status="success",
        files_touched=touched,
        commands_run=list(commands),
        host=host,
        model=model,
        workspace_path=str(workspace) if workspace is not None else None,
        created_at=created_at or _ANCHOR - timedelta(hours=2),
        telemetry=dict(telemetry or {}),
    )


def _seed(root: Path, *traces: Trace) -> Path:
    store = HistoryStore(root)
    store.init()
    for trace in traces:
        store.record_trace(trace)
    return root


def _abs(repo: Path, *rels: str) -> tuple[str, ...]:
    return tuple(str(repo / rel) for rel in rels)


def _correlate(store_root: Path, repo_root: Path, **kwargs: object) -> ProvenanceRecord:
    """The correlator's verdict, reached through the packet's own entry point.

    ``collect_provenance`` is the only caller ``packet.py`` has, so testing the
    correlation through it is testing what ships. It returns the same record
    plus the evidence rows these assertions do not look at.
    """

    session_id = kwargs.pop("session_id", None)
    assert not kwargs, kwargs
    return collect_provenance(
        store_root,
        repo_root,
        _PACKET_PATHS,
        head_sha="a" * 40,
        head_commit_time=_ANCHOR,
        session_id=session_id,  # type: ignore[arg-type]
    )[0]


# ----- projection ---------------------------------------------------------- #


def test_split_files_touched_separates_reads_and_edits() -> None:
    trace = _trace(
        trace_id="t1",
        session_id="s1",
        workspace="/repo",
        edited=("/repo/src/a.py", "/repo/src/b.py"),
        reads=("/repo/docs/one.md", "/repo/docs/two.md"),
    )
    reads, edits = split_files_touched(trace)
    assert reads == ("/repo/docs/one.md", "/repo/docs/two.md")
    assert edits == ("/repo/src/a.py", "/repo/src/b.py")


def test_split_files_touched_is_empty_and_total_for_a_bare_trace() -> None:
    trace = _trace(trace_id="t1", session_id="s1", workspace=None)
    assert split_files_touched(trace) == ((), ())


# ----- correlation: the happy path ----------------------------------------- #


def test_correlation_matches_on_workspace_and_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS[:4]),
            reads=_abs(repo, "src/app/helpers.py"),
        ),
    )

    record = _correlate(store_root, repo)

    assert record.status == "matched"
    assert record.match_confidence >= 0.6
    # A scored match is "probable", never "exact": only an anchor is exact, and
    # every renderer keys its hedge off this word.
    assert record.certainty == "probable"
    assert record.host == "claude"
    assert record.model == "claude-opus-4"
    assert record.session_id == "s1"
    assert record.task == "wire the router to the service"
    assert record.files_inspected == _abs(repo, "src/app/helpers.py")
    assert len(record.files_changed) == 4
    assert record.match_reason  # always states which evidence fired


def test_matched_record_carries_commands_and_subagents(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS[:4]),
            commands=(
                CommandRecord(command="uv run pytest -q", exit_code=0),
                CommandRecord(command="uv run pytest -q", exit_code=0),
                "uv run ruff check src",
            ),
            telemetry={"subagent_names": {"reviewer": 2, "planner": 1, "": 4, "broken": "x"}},
        ),
    )

    record = _correlate(store_root, repo)

    assert record.commands_run == ("uv run pytest -q", "uv run ruff check src")
    assert record.subagents == (("planner", 1), ("reviewer", 2))


def test_explicit_session_id_short_circuits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        # Zero file overlap, wrong workspace, three weeks stale: nothing this
        # trace carries would ever score above the floor on its own.
        _trace(
            trace_id="t1",
            session_id="chosen",
            workspace=tmp_path / "elsewhere",
            edited=("/other/x.py",),
            created_at=_ANCHOR - timedelta(days=21),
        ),
    )

    assert _correlate(store_root, repo).status == "unknown"

    record = _correlate(store_root, repo, session_id="chosen")
    assert record.status == "matched"
    assert record.match_confidence == 1.0
    assert record.certainty == "exact"
    assert record.match_reason == "explicit --session-id"
    assert record.session_id == "chosen"


def test_explicit_session_id_that_is_absent_stays_unknown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS),
        ),
    )

    record = _correlate(store_root, repo, session_id="nope")

    # An explicit id must never silently fall back to the heuristic, even when
    # the heuristic would have matched a different session confidently.
    assert record.status == "unknown"
    assert record.session_id is None
    assert "nope" in record.match_reason


def test_load_trace_for_session_accepts_the_trace_id_too(tmp_path: Path) -> None:
    store_root = _seed(tmp_path / "store", _trace(trace_id="t1", session_id="s1", workspace="/repo"))
    assert load_trace_for_session(store_root, "s1") is not None
    assert load_trace_for_session(store_root, "t1") is not None
    assert load_trace_for_session(store_root, "missing") is None
    assert load_trace_for_session(store_root, "  ") is None


# ----- correlation: the weak-evidence floor -------------------------------- #


def test_correlation_unknown_when_no_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(tmp_path / "store")

    record = _correlate(store_root, repo)

    assert record.status == "unknown"
    assert record.host is None
    assert record.model is None
    assert record.session_id is None
    assert record.task is None
    assert record.files_inspected == ()
    assert record.files_changed == ()
    assert record.commands_run == ()
    assert record.subagents == ()
    assert record.uninspected_impacted == ()
    assert record.match_confidence == 0.0
    assert record.match_reason


def test_correlation_unknown_when_the_store_does_not_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    record = _correlate(tmp_path / "no-store-here", repo)

    assert record.status == "unknown"
    assert record.host is None


def test_correlation_unknown_when_the_store_is_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = tmp_path / "store"
    store_root.mkdir()
    # A file where the sqlite database should be: opening it raises, and the
    # contract is that a broken store degrades rather than propagating.
    (store_root / "lemoncrow_history.db").write_text("not a database", encoding="utf-8")

    record, evidence, degraded = collect_provenance(
        store_root,
        repo,
        _PACKET_PATHS,
        head_sha="a" * 40,
        head_commit_time=_ANCHOR,
    )

    assert record.status == "unknown"
    assert evidence == ()
    assert "provenance_store_missing" in degraded


def test_correlation_unknown_when_only_a_weak_candidate_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        # Recency alone is 0.15 -- well under the 0.35 ambiguity floor.
        _trace(trace_id="t1", session_id="s1", workspace=None, edited=("/other/thing.py",)),
    )

    record = _correlate(store_root, repo)

    assert record.status == "unknown"
    assert record.session_id is None
    assert record.files_changed == ()
    assert "0.35" in record.match_reason


def test_a_tie_is_not_a_match_and_names_nobody(tmp_path: Path) -> None:
    """Two equally-good candidates are two answers, so neither is printed.

    The old rule reported the winner of a ``sorted()`` tiebreak under an
    ``ambiguous`` badge, which still put one session's host, model and task on
    screen as the author. A coin flip is not evidence.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    edited = _abs(repo, *_PACKET_PATHS[:4])
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="s1", workspace=repo, edited=edited),
        _trace(trace_id="t2", session_id="s2", workspace=repo, edited=edited),
    )

    record = _correlate(store_root, repo)

    assert record.status == "unknown"
    assert record.certainty == "none"
    assert record.match_confidence == 0.0
    assert record.host is None
    assert record.model is None
    assert record.session_id is None
    assert record.task is None
    assert "none stands clear" in record.match_reason


def test_a_near_tie_inside_the_margin_is_also_not_a_match(tmp_path: Path) -> None:
    """Not just exact ties: anything inside ``_MATCH_MARGIN`` is undecided."""

    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        # 4/5 overlap vs 3/5 overlap is 0.08 apart -- inside the 0.15 margin.
        _trace(trace_id="t1", session_id="s1", workspace=repo, edited=_abs(repo, *_PACKET_PATHS[:4])),
        _trace(trace_id="t2", session_id="s2", workspace=repo, edited=_abs(repo, *_PACKET_PATHS[:3])),
    )

    record = _correlate(store_root, repo)

    assert record.status == "unknown"
    assert record.session_id is None


def test_a_tie_is_reported_in_degraded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    edited = _abs(repo, *_PACKET_PATHS[:4])
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="s1", workspace=repo, edited=edited),
        _trace(trace_id="t2", session_id="s2", workspace=repo, edited=edited),
    )

    _, _, degraded = collect_provenance(
        store_root,
        repo,
        _PACKET_PATHS,
        head_sha="",
        head_commit_time=_ANCHOR,
    )

    assert "provenance_ambiguous" in degraded
    assert degraded == tuple(sorted(degraded))


def test_a_matched_host_that_records_no_reads_is_named_in_degraded(tmp_path: Path) -> None:
    """R2 still has to be visible on the path where a match survives."""

    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="s1", workspace=repo, edited=_abs(repo, *_PACKET_PATHS)),
    )

    record, _, degraded = collect_provenance(
        store_root,
        repo,
        _PACKET_PATHS,
        head_sha="",
        head_commit_time=_ANCHOR,
    )

    assert record.status == "matched"
    assert "agent_reads_unrecorded" in degraded


def test_a_session_that_edited_nothing_reviewed_is_never_the_author(tmp_path: Path) -> None:
    """The shipped bug, at its own numbers.

    Eight sessions sat in the reviewed workspace inside the time window and
    edited *none* of the reviewed files. Presence scored 0.45 + 0.15 = 0.60,
    they all tied at the match floor, and the packet printed the alphabetically
    first trace id -- a read-only session -- as the author of every file, with
    its host, model and unrelated task as unhedged fact.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        *[_trace(trace_id=f"t{index}", session_id=f"s{index}", workspace=repo, edited=()) for index in range(8)],
    )

    record = _correlate(store_root, repo)

    assert record.status == "unknown"
    assert record.certainty == "none"
    assert record.host is None
    assert record.model is None
    assert record.task is None
    assert "no session recorded an edit" in record.match_reason


def test_editing_other_files_in_the_repo_is_not_authorship_either(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        # Same workspace, same window, real edits -- to files nobody reviewed.
        _trace(trace_id="t1", session_id="s1", workspace=repo, edited=_abs(repo, "docs/notes.md")),
    )

    record = _correlate(store_root, repo)

    assert record.status == "unknown"
    assert record.session_id is None


def test_the_only_authoring_session_wins_over_a_tied_crowd_of_bystanders(tmp_path: Path) -> None:
    """Presence alone ties at the floor; one recorded edit breaks the tie honestly.

    Without the authorship gate this is the shipped bug in miniature: three
    bystanders tie at 0.60 and one of them is printed as the author, while the
    session that actually edited a reviewed file scores lower and is discarded.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        *[
            _trace(trace_id=f"b{index}", session_id=f"bystander-{index}", workspace=repo, edited=())
            for index in range(3)
        ],
        _trace(trace_id="t9", session_id="author", workspace=repo, edited=_abs(repo, _PACKET_PATHS[0])),
    )

    record = _correlate(store_root, repo)

    assert record.session_id == "author"
    assert record.status == "matched"
    # Still a guess, and still says so: nothing recorded this session against
    # this range, the score merely fits.
    assert record.certainty == "probable"
    assert "1/5 changed files" in record.match_reason


def test_stale_candidate_outside_the_time_window_is_not_matched(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, _PACKET_PATHS[0]),
            created_at=_ANCHOR - timedelta(days=6),
        ),
    )

    record = _correlate(store_root, repo)

    # Workspace (0.45) + 1/5 overlap (0.08) with the time term fully decayed
    # lands under the match floor: reported, but never as a confident match.
    assert record.status == "ambiguous"
    assert record.certainty == "possible"
    assert record.match_confidence < 0.6


def test_generic_basenames_do_not_manufacture_overlap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        # A different project's __init__.py must not count as "the same file"
        # just because the basename matches.
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=None,
            edited=("/elsewhere/pkg/__init__.py",),
        ),
    )

    record = collect_provenance(
        store_root,
        repo,
        ("src/app/__init__.py",),
        head_sha="",
        head_commit_time=_ANCHOR,
    )[0]

    assert record.status == "unknown"


def test_working_tree_range_anchors_on_now(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS),
            created_at=datetime.now(tz=UTC) - timedelta(minutes=5),
        ),
    )

    # head_sha == "" and head_commit_time is None is exactly the working-tree /
    # staged shape: there is no commit, so the window anchors on now().
    record = collect_provenance(
        store_root,
        repo,
        _PACKET_PATHS,
        head_sha="",
        head_commit_time=None,
    )[0]

    assert record.status == "matched"
    assert record.match_confidence >= 0.6


# ----- the run-ledger git anchor ------------------------------------------- #


def test_run_ledger_git_anchor_beats_the_heuristic(tmp_path: Path) -> None:
    import json

    from lemoncrow.core.foundation.paths import session_dir

    repo = tmp_path / "repo"
    repo.mkdir()
    head = "b" * 40
    store_root = _seed(
        tmp_path / "store",
        # The anchored session has no workspace and no overlap; the decoy would
        # win every scored comparison.
        _trace(trace_id="t1", session_id="anchored", workspace=None),
        _trace(
            trace_id="t2",
            session_id="decoy",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS),
        ),
    )
    run_dir = session_dir(store_root, "claude", "anchored")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps({"session_id": "anchored", "git": {"head": head, "branch": "main"}}),
        encoding="utf-8",
    )

    record = collect_provenance(
        store_root,
        repo,
        _PACKET_PATHS,
        head_sha=head,
        head_commit_time=_ANCHOR,
    )[0]

    assert record.status == "matched"
    assert record.session_id == "anchored"
    assert record.match_confidence == 1.0
    # The ledger recorded this session against this very HEAD: not a guess.
    assert record.certainty == "exact"
    assert "git anchor" in record.match_reason


def test_run_ledger_without_a_git_key_falls_back_to_scoring(tmp_path: Path) -> None:
    import json

    from lemoncrow.core.foundation.paths import session_dir

    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS),
        ),
    )
    run_dir = session_dir(store_root, "claude", "s1")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")

    record = collect_provenance(
        store_root,
        repo,
        _PACKET_PATHS,
        head_sha="c" * 40,
        head_commit_time=_ANCHOR,
    )[0]

    assert record.status == "matched"
    assert "git anchor" not in record.match_reason


# ----- cross-fill ---------------------------------------------------------- #


def _site(path: str) -> ImpactSite:
    return ImpactSite(kind="untouched_caller", path=path, old="old", new=None, snippet="snippet")


def test_uninspected_impacted_excludes_inspected_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    record = ProvenanceRecord(
        status="matched",
        files_inspected=(str(repo / "src/app/seen.py"),),
        files_changed=(str(repo / "src/app/service.py"),),
        match_confidence=0.9,
        match_reason="test",
    )
    impact = (_site("src/app/seen.py:L12"), _site("src/app/service.py:L3"), _site("src/app/never.py:L44"))

    filled, linked = link_impact_to_provenance(record, impact)

    assert filled.uninspected_impacted == ("src/app/never.py",)
    assert [site.inspected_by_agent for site in linked] == [True, True, False]


def test_cross_fill_claims_nothing_when_provenance_is_unknown() -> None:
    record = ProvenanceRecord(status="unknown")
    impact = (_site("src/app/never.py:L44"),)

    filled, linked = link_impact_to_provenance(record, impact)

    assert filled.uninspected_impacted == ()
    assert linked[0].inspected_by_agent is None


def test_cross_fill_claims_nothing_when_the_host_records_no_paths() -> None:
    # R2: most hosts never record reads. "never opened" would be a finding
    # about the agent; this is a gap in the recording.
    record = ProvenanceRecord(status="matched", match_confidence=0.8, match_reason="test")
    impact = (_site("src/app/never.py:L44"),)

    filled, linked = link_impact_to_provenance(record, impact)

    assert filled.uninspected_impacted == ()
    assert linked[0].inspected_by_agent is None


def test_cross_fill_never_infers_a_read_list_from_the_edit_list(tmp_path: Path) -> None:
    """ "Agent did not inspect" is a claim about reads, so it needs a read list.

    A host that records edits but no reads (every host but Claude) would
    otherwise have every file it did not *edit* reported as never opened --
    printed directly under ``Agent inspected: not recorded for this host``,
    which is the same packet saying it does and does not know.
    """

    repo = tmp_path / "repo"
    record = ProvenanceRecord(
        status="matched",
        files_changed=(str(repo / "src/app/service.py"),),
        match_confidence=0.8,
        match_reason="test",
    )
    impact = (_site("src/app/service.py:L3"), _site("src/app/never.py:L44"))

    filled, linked = link_impact_to_provenance(record, impact)

    assert filled.uninspected_impacted == ()
    # The edited file was demonstrably open; the other one is simply unknown.
    assert [site.inspected_by_agent for site in linked] == [True, None]


# ----- evidence ------------------------------------------------------------ #


def _evidence_map(records: tuple[object, ...]) -> dict[str, str]:
    return {record.name: record.status for record in records}  # type: ignore[attr-defined]


def test_evidence_reports_not_run_not_pass(tmp_path: Path) -> None:
    trace = _trace(
        trace_id="t1",
        session_id="s1",
        workspace="/repo",
        commands=(CommandRecord(command="uv run ruff check src", exit_code=0),),
    )

    statuses = _evidence_map(collect_evidence(tmp_path, trace, "s1"))

    assert statuses["Full suite"] == "NOT_RUN"
    assert statuses["Focused tests"] == "NOT_RUN"
    assert statuses["Typecheck"] == "NOT_RUN"
    assert statuses["Lint"] == "PASS"


def test_evidence_splits_focused_from_full_and_reports_failures(tmp_path: Path) -> None:
    trace = _trace(
        trace_id="t1",
        session_id="s1",
        workspace="/repo",
        commands=(
            CommandRecord(command="uv run pytest -q tests/gateway/test_x.py", exit_code=1),
            CommandRecord(command="uv run pytest -q", exit_code=0),
            CommandRecord(command="uv run mypy src", exit_code=0),
        ),
    )

    records = collect_evidence(tmp_path, trace, "s1")
    statuses = _evidence_map(records)

    assert statuses["Focused tests"] == "FAIL"
    assert statuses["Full suite"] == "PASS"
    assert statuses["Typecheck"] == "PASS"
    sourced = [record for record in records if record.status in {"PASS", "FAIL"}]
    assert sourced and all(record.source == "session:s1" for record in sourced)


def test_evidence_is_unknown_when_no_exit_code_was_recorded(tmp_path: Path) -> None:
    # A bare string command record carries no exit code. Reporting PASS off a
    # command that merely appears in the transcript is the one thing this must
    # never do.
    trace = _trace(trace_id="t1", session_id="s1", workspace="/repo", commands=("uv run pytest -q",))

    statuses = _evidence_map(collect_evidence(tmp_path, trace, "s1"))

    assert statuses["Full suite"] == "UNKNOWN"


def test_evidence_one_unrecorded_run_downgrades_the_whole_category(tmp_path: Path) -> None:
    trace = _trace(
        trace_id="t1",
        session_id="s1",
        workspace="/repo",
        commands=(CommandRecord(command="uv run pytest -q", exit_code=0), "pytest -q --lf"),
    )

    assert _evidence_map(collect_evidence(tmp_path, trace, "s1"))["Full suite"] == "UNKNOWN"


def test_evidence_migration_check_is_unknown(tmp_path: Path) -> None:
    trace = _trace(
        trace_id="t1",
        session_id="s1",
        workspace="/repo",
        commands=(CommandRecord(command="alembic upgrade head", exit_code=0),),
    )

    records = collect_evidence(tmp_path, trace, "s1")
    migration = [record for record in records if record.name == "Migration check"]

    assert len(migration) == 1
    assert migration[0].status == "UNKNOWN"
    assert migration[0].detail == ""
    assert migration[0].source == "none"


def test_evidence_is_empty_without_a_resolvable_session(tmp_path: Path) -> None:
    assert collect_evidence(tmp_path, None, None) == ()
    assert collect_evidence(tmp_path, None, "missing") == ()


def test_collect_provenance_flags_missing_test_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS),
            commands=("uv run pytest -q",),
        ),
    )

    record, evidence, degraded = collect_provenance(
        store_root,
        repo,
        _PACKET_PATHS,
        head_sha="",
        head_commit_time=_ANCHOR,
    )

    assert record.status == "matched"
    assert evidence
    assert "test_evidence_unavailable" in degraded


# ----- the surfaces the claim is printed on -------------------------------- #


def _rendered(record: ProvenanceRecord) -> str:
    from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION, ChangedFile, ReviewPacket
    from lemoncrow.pro.capabilities.review.render import render_review

    packet = ReviewPacket(
        schema_version=SCHEMA_VERSION,
        generated_at="2026-09-07T12:00:00+00:00",
        repo_root="/repo",
        range_mode="commit_range",
        base_rev="main",
        head_rev="HEAD",
        base_sha="a" * 40,
        head_sha="b" * 40,
        files=(ChangedFile(path="src/app/service.py", old_path=None, status="modified"),),
        provenance=record,
    )
    text = render_review(packet, no_color=True)
    return text.split("EXECUTION EVIDENCE", 1)[1]


def test_a_scored_attribution_is_hedged_on_the_line_that_claims_it() -> None:
    """The number in ``--json`` does not un-assert the word in the terminal."""

    evidence = _rendered(
        ProvenanceRecord(
            status="ambiguous",
            host="claude",
            model="claude-opus-5",
            session_id="003e0173",
            match_confidence=0.45,
            certainty="possible",
            match_reason="workspace path, 1/84 changed files (score 0.45, below the 0.60 match floor)",
        )
    )

    assert "Generated with: claude · claude-opus-5 · 003e0173  [possible match, confidence 0.45]" in evidence
    assert "unconfirmed" in evidence


def test_an_anchored_attribution_is_not_hedged() -> None:
    evidence = _rendered(
        ProvenanceRecord(
            status="matched",
            host="claude",
            model="claude-opus-5",
            session_id="anchored",
            match_confidence=1.0,
            certainty="exact",
            match_reason="run ledger git anchor (HEAD bbbbbbbbbbbb)",
        )
    )

    assert "Generated with: claude · claude-opus-5 · anchored\n" in evidence
    assert "unconfirmed" not in evidence


def test_unknown_provenance_prints_the_reason_and_no_host() -> None:
    evidence = _rendered(
        ProvenanceRecord(status="unknown", match_reason="no session recorded an edit to any of the 84 reviewed files")
    )

    assert "Generated with: unknown" in evidence
    assert "no session recorded an edit" in evidence
    assert "claude" not in evidence


def test_the_html_view_hedges_the_same_claim() -> None:
    from lemoncrow.pro.capabilities.review.html import _provenance_html

    page = _provenance_html(
        ProvenanceRecord(
            status="ambiguous",
            host="claude",
            model="claude-opus-5",
            session_id="003e0173",
            match_confidence=0.45,
            certainty="possible",
            match_reason="workspace path, 1/84 changed files",
        ),
        {},
    )

    generated_row = page.split("<dt>Generated with</dt>", 1)[1].split("</dd>", 1)[0]
    assert "possible match" in generated_row
    assert "unconfirmed" in generated_row
    assert "0.45 (possible)" in page


# ----- the exact edit anchor (PR-R0a) -------------------------------------- #
#
# The one path in the module that reports a fact. The host's own hook stamps
# the edited path, its host and its model into run.json at the moment of the
# edit, so a session that names a reviewed file did write it -- no window, no
# weights, no floor. Every test here runs in `working_tree` mode (head_sha ==
# "") because that is `lc review`'s default and the one mode where the older
# git anchor can never fire.


def _write_run(
    store_root: Path,
    session_id: str,
    edited: tuple[str, ...],
    *,
    host: str = "claude",
    model: str = "",
    kind: str = "file_edit",
) -> None:
    import json

    from lemoncrow.core.foundation.paths import session_dir

    run_dir = session_dir(store_root, "claude", session_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "agent": "claude",
                "events": [
                    {
                        "kind": kind,
                        "at": "2026-09-07T10:00:00+00:00",
                        "summary": f"edited {path}",
                        "payload": {
                            "path": path,
                            "diff": "@@\n",
                            "event": "PostToolUse",
                            "session_id": session_id,
                            "host": host,
                            "model": model,
                            "at_head": "",
                        },
                    }
                    for path in edited
                ],
            }
        ),
        encoding="utf-8",
    )


def _recent() -> datetime:
    return datetime.now(tz=UTC) - timedelta(hours=1)


def _working_tree(store_root: Path, repo_root: Path, paths: tuple[str, ...] = _PACKET_PATHS) -> ProvenanceRecord:
    """Correlate the way `lc review` does by default: no head sha to anchor on."""

    return collect_provenance(store_root, repo_root, paths, head_sha="", head_commit_time=None)[0]


def test_a_recorded_edit_is_an_exact_anchor_in_working_tree_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        # The recorder scores nothing: no workspace, no files_touched. Before
        # this anchor existed it could not have been named at any score.
        _trace(trace_id="t1", session_id="recorder", workspace=None, created_at=_recent()),
    )
    _write_run(store_root, "recorder", _abs(repo, *_PACKET_PATHS[:2]))

    record = _working_tree(store_root, repo)

    assert record.status == "matched"
    assert record.session_id == "recorder"
    assert record.certainty == "exact"
    assert record.match_confidence == 1.0
    assert record.match_reason == "recorded at edit time"


def test_the_exact_anchor_beats_a_higher_scoring_bystander(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="recorder", workspace=None, created_at=_recent()),
        # Right directory, right time, every reviewed file in files_touched:
        # this wins the heuristic outright. It still loses to a record.
        _trace(
            trace_id="t2",
            session_id="loud-bystander",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS),
            created_at=_recent(),
        ),
    )
    _write_run(store_root, "recorder", _abs(repo, _PACKET_PATHS[0]))

    record = _working_tree(store_root, repo)

    assert record.session_id == "recorder"
    assert record.certainty == "exact"


def test_the_exact_anchor_names_a_host_the_heuristic_could_never_reach(tmp_path: Path) -> None:
    # `_same_workspace` is False whenever `Trace.workspace_path` is NULL, which
    # caps those importers at 0.55 -- under `_MATCH_FLOOR`. The payload the hook
    # wrote is the only place their host and model were ever recorded.
    repo = tmp_path / "repo"
    repo.mkdir()
    anonymous = Trace(
        id="t1",
        session_id="anonymous",
        agent="",
        host="",
        model="",
        domain="code",
        task="",
        status="success",
        workspace_path=None,
        created_at=_recent(),
    )
    store_root = _seed(tmp_path / "store", anonymous)
    _write_run(store_root, "anonymous", _abs(repo, _PACKET_PATHS[0]), host="codex", model="gpt-5-codex")

    record = _working_tree(store_root, repo)

    assert record.certainty == "exact"
    assert record.host == "codex"
    assert record.model == "gpt-5-codex"


def test_the_payload_never_overrides_what_the_importer_recorded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="recorder",
            workspace=None,
            host="claude",
            model="claude-opus-5",
            created_at=_recent(),
        ),
    )
    _write_run(store_root, "recorder", _abs(repo, _PACKET_PATHS[0]), host="codex", model="gpt-5-codex")

    record = _working_tree(store_root, repo)

    assert record.host == "claude"
    assert record.model == "claude-opus-5"


def test_two_recorded_authors_fall_back_to_the_heuristic(tmp_path: Path) -> None:
    # Mixed authorship is real (rev 1 Claude, rev 2 a human, rev 3 Codex).
    # Picking one would publish a coin flip as a recorded fact.
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="one", workspace=None, created_at=_recent()),
        _trace(trace_id="t2", session_id="two", workspace=None, created_at=_recent()),
    )
    _write_run(store_root, "one", _abs(repo, _PACKET_PATHS[0]))
    _write_run(store_root, "two", _abs(repo, _PACKET_PATHS[1]))

    record = _working_tree(store_root, repo)

    assert record.certainty != "exact"
    assert record.match_reason != "recorded at edit time"


def test_a_session_with_no_recorded_edit_leaves_the_heuristic_untouched(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS),
            created_at=_recent(),
        ),
    )
    _write_run(store_root, "s1", _abs(repo, _PACKET_PATHS[0]), kind="note")

    record = _working_tree(store_root, repo)

    assert record.status == "matched"
    assert record.certainty == "probable"
    assert "score" in record.match_reason


def test_a_reverted_edit_is_not_an_authoring_record(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="reverter", workspace=None, created_at=_recent()),
    )
    _write_run(store_root, "reverter", _abs(repo, _PACKET_PATHS[0]), kind="file_revert")

    record = _working_tree(store_root, repo)

    assert record.certainty != "exact"


def test_an_edit_in_another_checkout_does_not_claim_this_one(tmp_path: Path) -> None:
    # Tail matching cannot tell two checkouts apart, and this path reports a
    # fact rather than a score -- so an absolute recording outside the reviewed
    # tree is refused rather than tail-matched.
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other-checkout"
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="elsewhere", workspace=None, created_at=_recent()),
    )
    _write_run(store_root, "elsewhere", _abs(other, *_PACKET_PATHS))

    record = _working_tree(store_root, repo)

    assert record.certainty != "exact"


def test_a_generic_basename_alone_is_not_an_exact_anchor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="generic", workspace=None, created_at=_recent()),
    )
    # Same basename, different file. `__init__.py` is refused as basename-only
    # evidence by `_match_paths`, and this path inherits that refusal.
    _write_run(store_root, "generic", _abs(repo, "vendor/pkg/__init__.py"))

    record = _working_tree(store_root, repo, ("src/app/__init__.py",))

    assert record.certainty != "exact"


def test_a_run_json_the_correlator_cannot_read_is_not_an_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="s1",
            workspace=repo,
            edited=_abs(repo, *_PACKET_PATHS),
            created_at=_recent(),
        ),
    )
    from lemoncrow.core.foundation.paths import session_dir

    run_dir = session_dir(store_root, "claude", "s1")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text("{not json", encoding="utf-8")

    record = _working_tree(store_root, repo)

    # A value, never an exception -- and the heuristic still gets its turn.
    assert record.status == "matched"
    assert record.certainty == "probable"


def test_an_exact_edit_anchor_is_rendered_without_a_hedge() -> None:
    evidence = _rendered(
        ProvenanceRecord(
            status="matched",
            host="codex",
            model="gpt-5-codex",
            session_id="recorder",
            match_confidence=1.0,
            certainty="exact",
            match_reason="recorded at edit time",
        )
    )

    assert "Generated with: codex · gpt-5-codex · recorder\n" in evidence
    assert "Matched on: recorded at edit time" in evidence
    assert "unconfirmed" not in evidence
    assert "confidence" not in evidence


def test_a_relative_recording_from_another_project_is_not_an_exact_anchor(tmp_path: Path) -> None:
    # The `mcp__lc__edit` surface records whatever path string the agent typed
    # (`mcp_server.py:6825`) and codex's `file_change` items are workspace-
    # relative (`plugin_runtime.py:3964`), so a bare `src/app/service.py` in the
    # ledger names no tree at all. Accepting it let a session that edited an
    # identically-named file in an unrelated repository be reported as this
    # review's author at `certainty="exact"`, with no hedge in the terminal.
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(
            trace_id="t1",
            session_id="unrelated-project",
            workspace=tmp_path / "some-other-project",
            created_at=_recent(),
        ),
    )
    _write_run(store_root, "unrelated-project", _PACKET_PATHS[:2])

    record = _working_tree(store_root, repo)

    assert record.certainty != "exact"
    assert record.match_reason != "recorded at edit time"


def test_a_relative_recording_is_evidence_when_the_workspace_is_this_repo(tmp_path: Path) -> None:
    # The legitimate half of the rule above: the tree the path was relative to
    # is independently known, so the recording is still a fact.
    repo = tmp_path / "repo"
    repo.mkdir()
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="lc-edit", workspace=repo, created_at=_recent()),
    )
    _write_run(store_root, "lc-edit", _PACKET_PATHS[:2])

    record = _working_tree(store_root, repo)

    assert record.certainty == "exact"
    assert record.match_reason == "recorded at edit time"


def test_a_non_ascii_path_still_reaches_the_exact_anchor(tmp_path: Path) -> None:
    # `run_ledger` writes with the default `ensure_ascii=True`, so `café.py` is
    # stored escaped as `café.py`. The basename prefilter searched only the literal
    # spelling, so the session was silently invisible and dropped to a hedge.
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = ("src/app/café.py",)
    store_root = _seed(
        tmp_path / "store",
        _trace(trace_id="t1", session_id="unicode", workspace=None, created_at=_recent()),
    )
    _write_run(store_root, "unicode", _abs(repo, *paths))

    record = _working_tree(store_root, repo, paths)

    assert record.certainty == "exact"
    assert record.match_reason == "recorded at edit time"
