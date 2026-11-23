"""Claude's /code-review findings land in lc review (ReportFindings PostToolUse hook)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest

from lemoncrow.core.capabilities.session_comment_capture import capture_reported_findings, message_edit_scopes
from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.pro.capabilities.review import hosted, reported_findings
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_comments import SessionCommentIngestor
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"
SID = "claude-review"
PRICE = {
    "file": "app.py",
    "line": 2,
    "summary": "price() returns 2 without validating the caller's tier.",
    "short_summary": "Unvalidated price",
    "failure_scenario": "tier=None -> 2 is charged",
    "severity": "important",
    "category": "correctness",
}


def _change(tmp_path: Path) -> tuple[Path, Path, ReviewStore, Any, Any]:
    repo, root = tmp_path / "repo", (tmp_path / "store").resolve()
    repo.mkdir()
    git = pygit2.init_repository(str(repo), initial_head="main")
    (repo / "app.py").write_text("def price():\n    return 1\n")
    git.index.add_all()
    git.index.write()
    signature = pygit2.Signature("Tester", "tester@example.invalid")
    git.create_commit("HEAD", signature, signature, "base", git.index.write_tree(), [])
    (repo / "app.py").write_text("def price():\n    return 2\n")
    store = ReviewStore(root)
    rng = resolve_rev_range(repo, working_tree=True)
    build = build_review_packet_with_blobs(repo, rng, store_root=root, with_patch_text=True, unbounded_patch_text=True)
    session = open_or_create_session(store, repo, rng, title="reported findings")
    revision = snapshot_revision(store, session, repo, rng, store_root=root, build=build)
    return repo, root, store, session, revision


def _report(repo: Path, *findings: dict[str, Any], tool_use_id: str = "toolu_1") -> dict[str, Any]:
    return {
        "session_id": SID,
        "cwd": str(repo),
        "tool_name": "ReportFindings",
        "tool_use_id": tool_use_id,
        "tool_input": {"findings": list(findings)},
    }


def test_findings_open_on_the_snapshot_they_name_without_another_capture(tmp_path: Path) -> None:
    # The Snapshot predates the report (an unchanged tree reuses it): the
    # first-Snapshot-after-capture rule alone would leave it pending forever.
    repo, root, store, session, revision = _change(tmp_path)
    report = _report(repo, PRICE)
    assert capture_reported_findings(root, report, host="claude", review_id=session.id, revision_id=revision.id) == 1
    SessionCommentIngestor(store, root).sync(session)
    [finding] = store.list_annotations(session.id)
    assert (finding.anchor.path, finding.anchor.start_line, finding.revision_id) == ("app.py", 2, revision.id)
    assert finding.anchor_method != "file" and finding.source == "ai_review"
    assert finding.title == "Unvalidated price" and finding.created_by == "claude"
    assert "Failure scenario: tier=None -> 2 is charged" in finding.body and "Severity: important" in finding.body
    [summary] = store.list_discussion_comments(session.id)
    assert summary.revision_id == revision.id
    assert summary.body == "Claude code review: 1 finding\n\n- important · app.py line 2 · Unvalidated price"


def test_a_fix_rereport_adds_its_outcomes_without_judging_or_duplicating_the_finding(tmp_path: Path) -> None:
    repo, root, store, session, revision = _change(tmp_path)
    ingestor = SessionCommentIngestor(store, root)
    first = _report(repo, PRICE)
    assert capture_reported_findings(root, first, host="claude", review_id=session.id, revision_id=revision.id) == 1
    ingestor.sync(session)
    assert capture_reported_findings(root, first, host="claude", review_id=session.id, revision_id=revision.id) == 0
    fixed = _report(repo, dict(PRICE, outcome="fixed"), tool_use_id="toolu_2")
    assert capture_reported_findings(root, fixed, host="claude", review_id=session.id, revision_id=revision.id) == 1
    ingestor.sync(session)
    # Only the person marks an AI finding fixed; the agent's claim is the summary's.
    [finding] = store.list_annotations(session.id)
    assert finding.state == "open" and finding.finding_disposition in {"", "open"}
    bodies = sorted(comment.body for comment in store.list_discussion_comments(session.id))
    assert bodies == [
        "Claude code review: 1 finding\n\n- important · app.py line 2 · Unvalidated price",
        "Claude code review: 1 finding (1 fixed)\n\n- important · app.py line 2 · Unvalidated price · fixed",
    ]


def test_a_finding_first_reported_already_fixed_is_not_opened(tmp_path: Path) -> None:
    repo, root, store, session, revision = _change(tmp_path)
    report = _report(repo, dict(PRICE, outcome="fixed"), dict(PRICE, line=1, summary="Skipped one", outcome="skipped"))
    assert capture_reported_findings(root, report, host="claude", review_id=session.id, revision_id=revision.id)
    SessionCommentIngestor(store, root).sync(session)
    [finding] = store.list_annotations(session.id)
    assert (finding.anchor.start_line, finding.body.splitlines()[0]) == (1, "Skipped one")


def test_a_clean_report_is_one_discussion_and_never_claims_the_turns_edits(tmp_path: Path) -> None:
    repo, root, store, session, revision = _change(tmp_path)
    run = session_dir(root, "claude", SID) / "run.json"
    run.parent.mkdir(parents=True, exist_ok=True)
    edit = {"kind": "file_edit", "at": "2026-01-01T00:00:00+00:00", "payload": {"path": "app.py"}}
    run.write_text(json.dumps({"session_id": SID, "events": [edit]}))
    assert capture_reported_findings(root, _report(repo), host="claude", review_id=session.id, revision_id=revision.id)
    SessionCommentIngestor(store, root).sync(session)
    assert not store.list_annotations(session.id)
    [summary] = store.list_discussion_comments(session.id)
    assert (summary.body, summary.revision_id) == ("Claude code review: 0 findings", revision.id)
    # The later reply still owns the edit made before the review.
    events = json.loads(run.read_text())["events"]
    events.append(
        {
            "kind": "agent_message",
            "at": "2099-01-01T00:00:00+00:00",
            "payload": {"role": "assistant", "event": "VisibleOutput"},
        }
    )
    assert message_edit_scopes(events) == {2: ("app.py",)}


def test_a_finding_outside_the_change_stays_a_discussion_comment(tmp_path: Path) -> None:
    repo, root, store, session, revision = _change(tmp_path)
    elsewhere = dict(PRICE, file="billing/missing.py", short_summary="Elsewhere")
    report = _report(repo, elsewhere)
    assert capture_reported_findings(root, report, host="claude", review_id=session.id, revision_id=revision.id)
    SessionCommentIngestor(store, root).sync(session)
    assert not store.list_annotations(session.id)
    comments = store.list_discussion_comments(session.id)
    assert any(comment.title == "Elsewhere" and comment.revision_id == revision.id for comment in comments)


def test_the_hook_path_captures_publishes_records_then_imports_before_naming_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, root, store, session, revision = _change(tmp_path)
    calls: list[tuple[Any, ...]] = []

    def capture(_config: Any, _repo: Path, rng: Any, **kwargs: Any) -> Any:
        calls.append(("capture", rng.mode, kwargs["defer_post_ready"], kwargs.get("with_workspace", False)))
        return SimpleNamespace(
            review_id=session.id,
            canonical_url="http://127.0.0.1:1/r/token",
            response={"revision": {"id": revision.id, "revision_number": 0}},
        )

    def request(_config: Any, method: str, path: str, *, body: Any = None) -> dict[str, Any]:
        calls.append((method, path))
        if method == "GET":
            SessionCommentIngestor(store, root).sync(session)
        return {}

    monkeypatch.setattr(hosted, "active_local_review_base", lambda *_args, **_kwargs: ("", ""))
    monkeypatch.setattr(hosted, "capture_server_review", capture)
    monkeypatch.setattr(hosted, "server_review_request", request)
    hosted_config = SimpleNamespace(hosted=True)
    assert (
        reported_findings.record_reported_findings(
            _report(repo, PRICE), host="claude", store_root=root, config=hosted_config  # type: ignore[arg-type]
        )
        is None
    )
    assert not calls
    result = reported_findings.record_reported_findings(
        _report(repo, PRICE), host="claude", store_root=root, config=SimpleNamespace(hosted=False)  # type: ignore[arg-type]
    )
    assert result is not None and result.revision_id == revision.id
    assert calls == [
        ("capture", "working_tree", True, False),
        ("POST", f"/api/reviews/{session.id}/revisions/0/publish"),
        ("GET", f"/api/reviews/{session.id}/annotations"),
    ]
    assert len(store.list_annotations(session.id)) == 1
    assert result.url == "http://127.0.0.1:1/r/token"


def test_a_committed_change_is_captured_against_the_branch_review_like_a_bare_lc_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, root, _store, _session, _revision = _change(tmp_path)
    git = pygit2.Repository(str(repo))
    frozen = str(git.head.target)
    git.index.add_all()
    git.index.write()
    signature = pygit2.Signature("Tester", "tester@example.invalid")
    git.create_commit("HEAD", signature, signature, "fix", git.index.write_tree(), [git.head.target])
    ranges: list[Any] = []

    def capture(_config: Any, _repo: Path, rng: Any, **_kwargs: Any) -> Any:
        ranges.append(rng)
        return SimpleNamespace(review_id="r", canonical_url="u", response={"revision": {}})

    monkeypatch.setattr(hosted, "active_local_review_base", lambda *_args, **_kwargs: ("r", frozen))
    monkeypatch.setattr(hosted, "capture_server_review", capture)
    config = SimpleNamespace(hosted=False)
    reported_findings.record_reported_findings(_report(repo), host="claude", store_root=root, config=config)  # type: ignore[arg-type]
    [rng] = ranges
    assert (rng.mode, rng.base_sha) == ("working_tree", frozen)


def _hook() -> Any:
    spec = importlib.util.spec_from_file_location("review_findings_hook", HOOKS / "review_findings.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fire_and_forget(monkeypatch: pytest.MonkeyPatch, url: str | None) -> list[tuple[dict[str, Any], str, Path]]:
    """The hook's two moves, recorded: name the URL, detach the worker. Capturing here fails the test."""
    spawned: list[tuple[dict[str, Any], str, Path]] = []

    def capture(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the hook captured inline instead of detaching the worker")

    monkeypatch.setattr(reported_findings, "record_reported_findings", capture)
    monkeypatch.setattr(reported_findings, "branch_review_url", lambda *_args, **_kwargs: url)
    monkeypatch.setattr(
        reported_findings,
        "spawn_reported_findings",
        lambda payload, *, host, store_root: spawned.append((payload, host, store_root)),
    )
    return spawned


def test_the_hook_is_wired_prints_the_review_url_and_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    groups = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PostToolUse"]
    [wired] = [group for group in groups if group.get("matcher") == "ReportFindings"]
    assert "hooks/review_findings.py" in wired["hooks"][0]["command"]

    assert wired["hooks"][0]["timeout"] <= 30

    hook = _hook()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("LEMONCROW_IN_REVIEW", raising=False)
    monkeypatch.setattr(hook, "_dormant", lambda: False)

    def run(payload: Any) -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert hook.main() == 0
        return capsys.readouterr().out

    spawned = _fire_and_forget(monkeypatch, "http://127.0.0.1:1/r/t")
    assert run({"tool_name": "Bash", "tool_input": {"command": "ls"}}) == ""
    clean = {"tool_name": "ReportFindings", "tool_input": {"findings": []}, "cwd": str(tmp_path)}
    message = json.loads(run(clean))["systemMessage"]
    assert message == "lc review: 0 findings from Claude code review → http://127.0.0.1:1/r/t"
    assert [(host, root) for _payload, host, root in spawned] == [("claude", tmp_path / "root")]

    # No branch Review yet: the worker still runs, and the line says where the link will be.
    spawned = _fire_and_forget(monkeypatch, "")
    message = json.loads(run(clean))["systemMessage"]
    assert message.endswith("→ capturing a new Review; `lc review status` shows its link")
    assert len(spawned) == 1

    # Hosted or not a checkout: nothing is detached and nothing is printed.
    spawned = _fire_and_forget(monkeypatch, None)
    assert run(clean) == "" and spawned == []

    def down(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Review server unreachable")

    monkeypatch.setattr(reported_findings, "branch_review_url", down)
    assert run(clean) == ""


FORK_RESULT = (
    "```json\n"
    + json.dumps([{**PRICE, "summary": "`reserve()` runs before the ```claim``` loop."}], indent=2)
    + "\n```\n\nI found 1 issue. The [least certain] one is noted above.\n"
)


def _fork(tmp_path: Path, skill: str, message: str = FORK_RESULT) -> dict[str, Any]:
    subagents = tmp_path / "session" / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    (subagents / "agent-a1.forked-skill.json").write_text(json.dumps({"skillName": skill}))
    return {
        "session_id": SID,
        "cwd": str(tmp_path),
        "hook_event_name": "SubagentStop",
        "agent_id": "a1",
        "agent_type": "general-purpose",
        "agent_transcript_path": str(subagents / "agent-a1.jsonl"),
        "last_assistant_message": message,
    }


def test_a_background_code_review_is_reported_from_its_final_json(tmp_path: Path) -> None:
    report = reported_findings.forked_review_payload(_fork(tmp_path, "code-review"))
    assert report is not None
    assert (report["tool_name"], report["tool_use_id"]) == ("ReportFindings", "subagent:a1")
    [finding] = report["tool_input"]["findings"]
    assert (finding["file"], finding["line"], finding["summary"]) == (
        "app.py",
        2,
        "`reserve()` runs before the ```claim``` loop.",
    )
    clean = reported_findings.forked_review_payload(_fork(tmp_path, "code-review", "[]"))
    assert clean is not None and clean["tool_input"]["findings"] == []

    assert reported_findings.forked_review_payload(_fork(tmp_path, "simplify")) is None
    assert reported_findings.forked_review_payload(_fork(tmp_path, "code-review", "No findings block.")) is None
    assert reported_findings.forked_review_payload({**_fork(tmp_path, "code-review"), "agent_id": ""}) is not None
    unforked = _fork(tmp_path, "code-review")
    (tmp_path / "session" / "subagents" / "agent-a1.forked-skill.json").unlink()
    assert reported_findings.forked_review_payload(unforked) is None


def test_the_hook_reports_a_background_code_review_when_its_subagent_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    groups = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["SubagentStop"]
    assert any("hooks/review_findings.py" in hook["command"] for group in groups for hook in group["hooks"])

    assert all(hook.get("timeout", 0) <= 30 for group in groups for hook in group["hooks"])

    hook = _hook()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("LEMONCROW_IN_REVIEW", raising=False)
    monkeypatch.setattr(hook, "_dormant", lambda: False)
    spawned = _fire_and_forget(monkeypatch, "http://127.0.0.1:1/r/t")

    def run(payload: Any) -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert hook.main() == 0
        return capsys.readouterr().out

    assert run(_fork(tmp_path, "simplify")) == ""
    assert spawned == []
    message = json.loads(run(_fork(tmp_path, "code-review")))["systemMessage"]
    assert message == "lc review: 1 finding from Claude code review → http://127.0.0.1:1/r/t"
    [(report, _host, _root)] = spawned
    assert report["tool_use_id"] == "subagent:a1" and len(report["tool_input"]["findings"]) == 1


def test_the_url_is_the_branch_reviews_named_without_capturing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, root, _store, session, _revision = _change(tmp_path)
    config = SimpleNamespace(hosted=False, endpoint=lambda path: f"http://127.0.0.1:1{path}")
    reads: list[str] = []

    def overview(_config: Any, method: str, path: str, **_kwargs: Any) -> Any:
        reads.append(f"{method} {path}")
        return {"session": {"id": session.id, "ref": "r/abc12345"}}

    def capture(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("naming the URL must not capture")

    monkeypatch.setattr(hosted, "capture_server_review", capture)
    monkeypatch.setattr(hosted, "server_review_request", overview)
    monkeypatch.setattr(hosted, "active_local_review_base", lambda *_args, **_kwargs: (session.id, "base"))
    url = reported_findings.branch_review_url(_report(repo), store_root=root, config=config)  # type: ignore[arg-type]
    assert url == "http://127.0.0.1:1/r/abc12345"
    assert reads == [f"GET /api/reviews/{session.id}"]

    monkeypatch.setattr(hosted, "active_local_review_base", lambda *_args, **_kwargs: ("", ""))
    assert reported_findings.branch_review_url(_report(repo), store_root=root, config=config) == ""  # type: ignore[arg-type]
    hosted_config = SimpleNamespace(hosted=True)
    assert reported_findings.branch_review_url(_report(repo), store_root=root, config=hosted_config) is None  # type: ignore[arg-type]
    outside = tmp_path / "not-a-checkout"
    outside.mkdir()
    assert reported_findings.branch_review_url(_report(outside), store_root=root, config=config) is None  # type: ignore[arg-type]


def test_the_worker_is_detached_and_reads_its_report_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[tuple[list[str], dict[str, Any], bytes]] = []

    class Worker:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            class Pipe(io.BytesIO):
                def close(pipe) -> None:
                    if not pipe.closed:
                        launched.append((argv, kwargs, pipe.getvalue()))
                    super().close()

            self.stdin = Pipe()

    monkeypatch.setattr("subprocess.Popen", Worker)
    report = _report(tmp_path, PRICE)
    reported_findings.spawn_reported_findings(report, host="claude", store_root=tmp_path / "root")
    [(argv, kwargs, sent)] = launched
    assert argv[1:] == [
        "-m",
        "lemoncrow.pro.capabilities.review.reported_findings",
        "--host",
        "claude",
        "--store-root",
        str(tmp_path / "root"),
    ]
    assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
    assert json.loads(sent) == report

    received: list[tuple[dict[str, Any], str, Path]] = []

    def record(payload: dict[str, Any], *, host: str, store_root: Path) -> Any:
        received.append((payload, host, store_root))
        return reported_findings.ReportedReview(url="u", review_id="r", revision_id="s", findings=1)

    monkeypatch.setattr(reported_findings, "record_reported_findings", record)
    monkeypatch.setattr("sys.stdin", io.StringIO(sent.decode()))
    assert reported_findings.main(["--host", "claude", "--store-root", str(tmp_path / "root")]) == 0
    assert received == [(report, "claude", tmp_path / "root")]


def test_a_ledger_event_with_a_non_object_payload_never_stops_a_report(tmp_path: Path) -> None:
    repo, root, _store, session, revision = _change(tmp_path)
    run_file = session_dir(root, "claude", SID) / "run.json"
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text(json.dumps({"session_id": SID, "events": [{"kind": "note", "payload": []}]}))
    report = _report(repo, PRICE)
    assert capture_reported_findings(root, report, host="claude", review_id=session.id, revision_id=revision.id) == 1
    assert capture_reported_findings(root, report, host="claude", review_id=session.id, revision_id=revision.id) == 0
