"""``lc resume-context``: the brief, and above all its bounds.

The product claim is that the artifact stays small no matter how large the
session was, so the bound gets more coverage here than the happy path: a
session with 500 edited files and 100 learnings must render in the same handful
of lines as a trivial one. The degraded paths matter for the same reason a
review packet's do -- a continuation brief that raises when a run ledger is
corrupt is worse than one that simply has less to say.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import pygit2
import pytest
from click.testing import CliRunner

from lemoncrow.core.foundation.history_store import HistoryStore
from lemoncrow.core.foundation.models import CommandRecord, FileEditRecord, Trace, TraceLearning
from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.resume_context.builder import (
    MAX_DECISIONS,
    MAX_FILES,
    MAX_RENDER_LINES,
    MAX_SYMBOLS,
    MAX_TEXT_CHARS,
    MAX_UNRESOLVED,
    ResumeContext,
    build_resume_context,
    render_resume_context,
)
from lemoncrow.pro.capabilities.review.models import EvidenceRecord

_SESSION = "11111111-2222-3333-4444-555555555555"
_WORKSPACE = "/home/dev/proj"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject ast-grep discovery before it can reach the managed download path.

    ``--symbols`` runs the impact detectors, whose managed bootstrap would try
    to *download* a binary into each throwaway repo. ``sg`` is the one candidate
    name ``_reject_reason`` refuses outright.
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# ----- fixtures ------------------------------------------------------------ #


def _trace(
    *,
    session_id: str = _SESSION,
    task: str = "wire the router to the service",
    edited: tuple[str, ...] = (),
    reads: tuple[str, ...] = (),
    commands: tuple[str | CommandRecord, ...] = (),
    learnings: tuple[str, ...] = (),
    reasoning: tuple[str, ...] = (),
    host: str = "claude",
    model: str = "claude-opus-4",
    workspace: str | None = _WORKSPACE,
) -> Trace:
    touched: list[str | FileEditRecord] = [FileEditRecord(path=path, diff="@@\n") for path in edited]
    touched.extend(reads)
    return Trace(
        id=f"trace-{session_id}",
        session_id=session_id,
        agent="claude",
        domain="code",
        task=task,
        status="success",
        files_touched=touched,
        commands_run=list(commands),
        learnings=[TraceLearning(kind="note", text=text) for text in learnings],
        reasoning=list(reasoning),
        host=host,
        model=model,
        workspace_path=workspace,
        created_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
    )


def _seed_trace(root: Path, trace: Trace) -> Path:
    store = HistoryStore(root)
    store.init()
    store.record_trace(trace)
    return root


def _seed_ledger(root: Path, payload: dict[str, Any], *, session_id: str = _SESSION, host: str = "claude") -> Path:
    directory = session_dir(root, host, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    run_file = directory / "run.json"
    run_file.write_text(json.dumps(payload), encoding="utf-8")
    return run_file


def _invoke(root: Path, args: list[str]) -> Any:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), "resume-context", *args], catch_exceptions=False)


def _ctx() -> click.Context:
    return click.Context(cli, info_name="lc")


def _saturated() -> ResumeContext:
    """A context at every cap at once -- the worst case the renderer must survive."""

    long_text = "x" * MAX_TEXT_CHARS
    return ResumeContext(
        schema_version=1,
        session_id=_SESSION,
        host="claude",
        model="claude-opus-4",
        goal=long_text,
        decisions=tuple(f"{index} {long_text}" for index in range(MAX_DECISIONS)),
        files_changed=tuple(f"src/pkg/module_{index:03d}.py" for index in range(MAX_FILES)),
        files_inspected=tuple(f"docs/note_{index:03d}.md" for index in range(MAX_FILES)),
        important_symbols=tuple(f"symbol_number_{index:03d}" for index in range(MAX_SYMBOLS)),
        test_state=tuple(
            EvidenceRecord(name=f"Check {index}", status="UNKNOWN", detail=long_text, source="none")
            for index in range(8)
        ),
        unresolved=tuple(f"{index} {long_text}" for index in range(MAX_UNRESOLVED)),
        generated_at="2026-09-08T00:00:00+00:00",
    )


def _fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"

    def _commit(message: str, offset: int) -> None:
        repo.index.add_all()
        repo.index.write()
        tree = repo.index.write_tree()
        parents = [] if repo.head_is_unborn else [repo.head.target]
        signature = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000 + offset, 0)
        repo.create_commit("HEAD", signature, signature, message, tree, parents)

    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    _commit("seed", 0)
    (root / "src" / "app.py").write_text("def one():\n    return 2\n", encoding="utf-8")
    _commit("change the return value", 60)
    return root


# ----- registration -------------------------------------------------------- #


def test_resume_context_registered() -> None:
    ctx = _ctx()
    assert "resume-context" in cli.list_commands(ctx)
    assert cli.get_command(ctx, "resume-context") is not None

    result = CliRunner().invoke(cli, ["resume-context", "--help"])
    assert result.exit_code == 0, result.output
    assert "--symbols" in result.output
    assert "--json" in result.output


# ----- the bound ----------------------------------------------------------- #


def test_resume_context_is_bounded(tmp_path: Path) -> None:
    """A huge session must still produce a small artifact -- that is the product."""

    store = tmp_path / "store"
    _seed_trace(
        store,
        _trace(
            edited=tuple(f"{_WORKSPACE}/src/pkg/mod_{index:03d}.py" for index in range(500)),
            reads=tuple(f"{_WORKSPACE}/docs/page_{index:03d}.md" for index in range(500)),
            learnings=tuple(f"learning {index}: " + "y" * 400 for index in range(100)),
            reasoning=tuple(f"reasoning {index}" for index in range(100)),
        ),
    )
    _seed_ledger(store, {"open_questions": [f"question {i}" for i in range(40)], "current_blockers": ["blocked"]})

    context = build_resume_context(store, _SESSION)
    assert len(context.files_changed) <= MAX_FILES
    assert len(context.files_inspected) <= MAX_FILES
    assert len(context.decisions) <= MAX_DECISIONS
    assert len(context.unresolved) <= MAX_UNRESOLVED
    assert all(len(entry) <= MAX_TEXT_CHARS for entry in context.decisions)
    assert all(len(entry) <= MAX_TEXT_CHARS for entry in context.unresolved)

    result = _invoke(store, [_SESSION])
    assert result.exit_code == 0, result.output
    assert len(result.output.splitlines()) < MAX_RENDER_LINES


def test_render_is_bounded_for_a_saturated_context() -> None:
    """Every field at its cap simultaneously still renders under the line budget."""

    rendered = render_resume_context(_saturated())
    assert len(rendered.splitlines()) < MAX_RENDER_LINES
    assert max(len(line) for line in rendered.splitlines()) <= MAX_TEXT_CHARS + 8


def test_render_names_the_truncation_it_performed() -> None:
    rendered = render_resume_context(_saturated())
    assert f"FILES CHANGED ({MAX_FILES})" in rendered
    assert "more" in rendered


# ----- sourcing ------------------------------------------------------------ #


def test_resume_context_json_shape(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(store, _trace(edited=(f"{_WORKSPACE}/src/a.py",)))

    result = _invoke(store, [_SESSION, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for key in (
        "schema_version",
        "session_id",
        "host",
        "model",
        "goal",
        "decisions",
        "files_changed",
        "files_inspected",
        "important_symbols",
        "test_state",
        "unresolved",
        "generated_at",
    ):
        assert key in payload, key
    assert payload["schema_version"] == 1
    assert payload["session_id"] == _SESSION
    assert payload["host"] == "claude"
    assert payload["model"] == "claude-opus-4"


def test_resume_context_symbols_empty_without_flag(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(store, _trace(edited=(f"{_WORKSPACE}/src/a.py",)))

    result = _invoke(store, [_SESSION, "--json"])
    assert json.loads(result.output)["important_symbols"] == []
    assert build_resume_context(store, _SESSION).important_symbols == ()


def test_resume_context_symbols_populated_with_flag_in_a_repo(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(store, _trace(edited=(f"{_WORKSPACE}/src/a.py",)))
    repo_root = _fixture_repo(tmp_path)

    result = _invoke(store, [_SESSION, "--symbols", "--repo-root", str(repo_root), "--json"])
    assert result.exit_code == 0, result.output
    symbols = json.loads(result.output)["important_symbols"]
    assert isinstance(symbols, list)
    assert len(symbols) <= MAX_SYMBOLS
    assert "one" in symbols


def test_resume_context_symbols_degrade_outside_a_repo(tmp_path: Path) -> None:
    """`--symbols` against a non-repository must still produce a brief."""

    store = tmp_path / "store"
    _seed_trace(store, _trace(edited=(f"{_WORKSPACE}/src/a.py",)))
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    result = _invoke(store, [_SESSION, "--symbols", "--repo-root", str(outside), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["important_symbols"] == []


def test_resume_context_reports_unknown_goal(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(store, _trace(task="", host="", model=""))

    context = build_resume_context(store, _SESSION)
    assert context.goal == "unknown"
    # `agent` still names the host; only a trace with neither falls all the way back.
    assert context.model == "unknown"
    assert "unknown" in render_resume_context(context)


def test_decisions_take_learnings_first_then_reasoning_and_dedupe(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(
        store,
        _trace(
            learnings=("the router owns retries", "the router owns retries"),
            reasoning=("THE ROUTER OWNS RETRIES", "the service is idempotent"),
        ),
    )

    decisions = build_resume_context(store, _SESSION).decisions
    assert decisions == ("the router owns retries", "the service is idempotent")


def test_decisions_collapse_multiline_entries(tmp_path: Path) -> None:
    """One recorded learning must never become twelve rendered lines."""

    store = tmp_path / "store"
    _seed_trace(store, _trace(learnings=("first line\nsecond line\n\nthird line",)))

    assert build_resume_context(store, _SESSION).decisions == ("first line second line third line",)


def test_files_are_repo_relative_sorted_and_split(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(
        store,
        _trace(
            edited=(f"{_WORKSPACE}/src/b.py", f"{_WORKSPACE}/src/a.py"),
            reads=(f"{_WORKSPACE}/docs/guide.md",),
        ),
    )

    context = build_resume_context(store, _SESSION)
    assert context.files_changed == ("src/a.py", "src/b.py")
    assert context.files_inspected == ("docs/guide.md",)


def test_unresolved_comes_from_the_run_ledger(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(store, _trace())
    _seed_ledger(
        store,
        {"open_questions": ["is the retry budget shared?"], "current_blockers": ["staging is down"]},
    )

    context = build_resume_context(store, _SESSION)
    assert context.unresolved == ("is the retry budget shared?", "staging is down")
    assert "staging is down" in render_resume_context(context)


# ----- evidence ------------------------------------------------------------ #


def test_test_state_reports_recorded_commands(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(
        store,
        _trace(commands=(CommandRecord(command="uv run pytest -q tests/test_service.py", exit_code=0),)),
    )

    context = build_resume_context(store, _SESSION)
    by_name = {record.name: record for record in context.test_state}
    assert by_name["Focused tests"].status == "PASS"
    assert by_name["Migration check"].status == "UNKNOWN"


def test_test_state_never_synthesizes_a_pass(tmp_path: Path) -> None:
    """A bare-string command carries no exit code, so it can only be UNKNOWN."""

    store = tmp_path / "store"
    _seed_trace(store, _trace(commands=("uv run pytest -q",)))

    statuses = {record.status for record in build_resume_context(store, _SESSION).test_state}
    assert "PASS" not in statuses


def test_test_state_is_empty_and_named_without_a_trace(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_ledger(store, {"agent": "codex", "task": "port the importer", "files_touched": ["src/importer.py"]})

    context = build_resume_context(store, _SESSION)
    assert context.test_state == ()
    assert "no verification commands recorded" in render_resume_context(context)


# ----- degradation --------------------------------------------------------- #


def test_resume_context_unknown_session_errors(tmp_path: Path) -> None:
    result = _invoke(tmp_path / "store", ["no-such-session"])
    assert result.exit_code != 0
    assert "no-such-session" in result.output
    assert "Traceback" not in result.output


def test_ledger_only_session_still_builds(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_ledger(
        store,
        {
            "agent": "codex",
            "task": "port the importer",
            "workspace_path": _WORKSPACE,
            "files_touched": [f"{_WORKSPACE}/src/importer.py"],
            "open_questions": ["which encoding?"],
        },
    )

    context = build_resume_context(store, _SESSION)
    assert context.host == "codex"
    assert context.goal == "port the importer"
    assert context.files_changed == ("src/importer.py",)
    assert context.files_inspected == ()
    assert context.unresolved == ("which encoding?",)


def test_corrupt_run_ledger_degrades_instead_of_raising(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed_trace(store, _trace(edited=(f"{_WORKSPACE}/src/a.py",)))
    directory = session_dir(store, "claude", _SESSION)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text("{not json", encoding="utf-8")

    context = build_resume_context(store, _SESSION)
    assert context.unresolved == ()
    assert context.files_changed == ("src/a.py",)


def test_ledger_with_hostile_shapes_degrades(tmp_path: Path) -> None:
    """A ledger whose keys are the wrong type must not take the brief down."""

    store = tmp_path / "store"
    _seed_trace(store, _trace())
    _seed_ledger(store, {"open_questions": "not a list", "current_blockers": [None, 3, "real blocker"]})

    assert build_resume_context(store, _SESSION).unresolved == ("real blocker",)


def test_missing_store_is_an_error_not_a_crash(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=_SESSION):
        build_resume_context(tmp_path / "nowhere", _SESSION)


def test_blank_session_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session id"):
        build_resume_context(tmp_path / "store", "   ")


def test_paths_stay_absolute_without_an_anchor(tmp_path: Path) -> None:
    """No workspace and no repo root means no honest way to relativise."""

    store = tmp_path / "store"
    _seed_trace(store, _trace(edited=("/elsewhere/src/a.py",), workspace=None))

    assert build_resume_context(store, _SESSION).files_changed == ("/elsewhere/src/a.py",)
