"""Change-level Development: multi-agent attribution, incremental ingestion, frozen Snapshots.

Every test drives the real storage (``ReviewStore`` + Development tables), the
real ledger layout (``sessions/YYYY/MM/DD/<host>/<id>/run.json``) and, where the
contract is an HTTP one, the real Review API.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.infra.runtime.run_ledger import RunLedger
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.development import (
    DevelopmentStore,
    bind_session,
    development_view,
    legacy_events,
    push_events,
    record_feedback_handoff,
    reduce_events,
    render_projection,
)
from lemoncrow.pro.capabilities.review.development_ingest import DevelopmentIngestor
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_models import ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import (
    open_or_create_session,
    read_packet_json,
    snapshot_revision,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "development-test-token-0123456789abcdef"
PORT = 45998
BASE = f"http://127.0.0.1:{PORT}"
T0 = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_PREWARM", "0")


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


class Change:
    """A git working tree under Review plus the store that owns it."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.store_root = tmp_path / "store"
        self.repo.mkdir(parents=True)
        git = pygit2.init_repository(str(self.repo), initial_head="main")
        git.config["user.name"] = "Development Tester"
        git.config["user.email"] = "dev@example.test"
        for name in ("auth", "cache", "session"):
            self.write(f"src/{name}.py", f"def {name}():\n    return 1\n")
        git.index.add_all()
        git.index.write()
        tree = git.index.write_tree()
        sig = pygit2.Signature("Development Tester", "dev@example.test", 1700000000, 0)
        git.create_commit("HEAD", sig, sig, "base", tree, [])
        self.write("src/auth.py", "def auth():\n    return 2\n")
        self.store = ReviewStore(self.store_root)
        self.rng = resolve_rev_range(self.repo, None, working_tree=True)
        build = build_review_packet_with_blobs(self.repo, self.rng, store_root=self.store_root, with_patch_text=True)
        self.session: ReviewSession = open_or_create_session(self.store, self.repo, self.rng, title="Development")
        self.development = DevelopmentStore(self.store)
        self.ingestor = DevelopmentIngestor(self.store, source_store_root=self.store_root, repo_root=self.repo)
        self._initial_build = build

    def write(self, rel: str, text: str) -> None:
        target = self.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def ledger(
        self,
        host: str,
        session_id: str,
        events: list[dict[str, Any]],
        *,
        workspace: Path | None | str = "repo",
        **extra: Any,
    ) -> Path:
        path = session_dir(self.store_root, host, session_id) / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "session_id": session_id,
            "agent": extra.pop("agent", host),
            "events": events,
            "created_at": extra.pop("created_at", _at(0)),
            "updated_at": extra.pop("updated_at", _at(30)),
        }
        if workspace == "repo":
            payload["workspace_path"] = str(self.repo)
        elif workspace is not None:
            payload["workspace_path"] = str(workspace)
        payload.update(extra)
        path.write_text(json.dumps(payload), encoding="utf-8")
        # Distinct mtimes even on coarse filesystems.
        stamp = datetime.now().timestamp() + len(json.dumps(payload)) / 1e6
        os.utime(path, (stamp, stamp))
        return path

    def snapshot(self) -> Any:
        rng = resolve_rev_range(self.repo, None, working_tree=True)
        build = build_review_packet_with_blobs(self.repo, rng, store_root=self.store_root, with_patch_text=True)
        return snapshot_revision(self.store, self.session, self.repo, rng, store_root=self.store_root, build=build)

    def sync(self) -> int:
        return self.ingestor.sync(self.session, force=True)

    def view(self, revision: Any) -> dict[str, Any]:
        packet = read_packet_json(self.store, revision)
        attributions = [
            {
                "scope": item.scope,
                "scope_key": item.scope_key,
                "host": item.host,
                "session_id": item.session_id,
                "model": item.model,
                "certainty": item.certainty,
            }
            for item in self.store.list_revision_attributions(revision.id)
        ]
        numbers = {item.id: item.revision_number - 1 for item in self.store.list_revisions(self.session.id)}
        return development_view(
            self.development,
            change_id=self.session.id,
            revision_id=revision.id,
            revision_number=revision.revision_number - 1,
            revision_created_at=revision.created_at,
            packet=packet if isinstance(packet, dict) else None,
            attributions=attributions,
            revision_numbers=numbers,
            historical=False,
            live_enabled=True,
        )

    def live(self) -> dict[str, Any]:
        return render_projection(
            self.development.live_state(self.session.id),
            scope="live",
            source="events",
            sessions={item.key: item for item in self.development.list_sessions(self.session.id)},
        )

    def dump(self) -> str:
        with sqlite3.connect(self.store.db_path) as conn:
            rows: list[Any] = []
            for table in (
                "development_events",
                "development_sessions",
                "development_projections",
                "development_snapshots",
            ):
                rows.extend(conn.execute(f"SELECT * FROM {table}").fetchall())
        return repr(rows)


@pytest.fixture
def change(tmp_path: Path) -> Change:
    return Change(tmp_path)


def _edit(path: str, minutes: float, *, model: str = "", diff: str = "@@ -1 +1 @@\n-a\n+b") -> dict[str, Any]:
    return {
        "kind": "file_edit",
        "at": _at(minutes),
        "summary": f"edited {path}",
        "payload": {"path": path, "diff": diff, "event": "PostToolUse", "model": model},
    }


def _command(command: str, minutes: float, *, ok: bool = True, stdout: str = "") -> dict[str, Any]:
    return {
        "kind": "command_result",
        "at": _at(minutes),
        "summary": command,
        "payload": {"command": command, "ok": ok, "stdout": stdout, "stderr": "", "error_signature": ""},
    }


def _message(text: str, minutes: float, *, role: str = "assistant") -> dict[str, Any]:
    key = "prompt" if role == "user" else "message"
    return {
        "kind": "agent_message",
        "at": _at(minutes),
        "summary": f"{role}: {text[:40]}",
        "payload": {"role": role, key: text, "event": "VisibleTurn"},
    }


def _roles(projection: dict[str, Any]) -> dict[str, str]:
    return {item["session_id"]: item["role"] for item in projection["contributors"]}


# --------------------------------------------------------------------------- #
# 1-8: who counts as Development                                              #
# --------------------------------------------------------------------------- #


def test_read_only_agent_in_same_repository_is_not_an_author(change: Change) -> None:
    change.ledger(
        "claude",
        "reader",
        [
            _message("How does auth work?", 1, role="user"),
            {"kind": "tool_call", "at": _at(2), "summary": "read src/auth.py", "payload": {"tool": "read"}},
            _message("Auth returns a constant after the refactor.", 3),
        ],
    )

    assert change.sync() == 0

    live = change.live()
    assert live["contributors"] == []
    assert live["event_count"] == 0
    rows = {item.session_id: item for item in change.development.list_sessions(change.session.id)}
    assert rows["reader"].role == "observer"


def test_exact_editing_agent_is_an_author_with_its_paths(change: Change) -> None:
    change.ledger(
        "claude",
        "author",
        [
            _message("Make auth return 2", 1, role="user"),
            _edit(str(change.repo / "src/auth.py"), 2, model="claude-opus"),
            _message("Auth now returns 2 and the focused test passes.", 4),
        ],
        workspace=None,  # hook-created ledger: absolute paths are the anchor
    )

    assert change.sync() > 0

    live = change.live()
    [author] = live["contributors"]
    assert author["role"] == "author"
    assert author["host"] == "claude"
    assert author["model"] == "claude-opus"
    assert author["paths"] == ["src/auth.py"]
    assert live["intent"] == "Make auth return 2"
    assert live["headline"] == "Auth now returns 2 and the focused test passes."
    assert [item["path"] for item in live["changed_paths"]] == ["src/auth.py"]


def test_verifier_contributes_checks_but_is_never_counted_as_author(change: Change) -> None:
    change.ledger("claude", "author", [_edit("src/auth.py", 1)])
    bind_session(change.development, change.session.id, host="codex", session_id="verifier", purpose="verify")
    change.ledger(
        "codex",
        "verifier",
        [
            _command("uv run pytest tests/test_auth.py -q", 5, ok=True),
            _command("uv run mypy src", 6, ok=False),
            _command("ls -la", 7),
            _message("Everything looks fine to me.", 8),
        ],
    )

    change.sync()
    live = change.live()

    assert _roles(live) == {"author": "author", "verifier": "verifier"}
    assert live["counts"]["authors"] == 1
    assert live["counts"]["verifiers"] == 1
    assert live["verification"]["pass"] == 1
    assert live["verification"]["fail"] == 1
    verifier = next(item for item in live["contributors"] if item["session_id"] == "verifier")
    assert verifier["paths"] == []
    events = change.development.list_events(change.session.id, session=("codex", "verifier"), limit=50)
    assert {event.kind for event in events} <= {"test", "validation", "task_started", "task_completed"}
    assert not any("ls -la" in event.summary or "looks fine" in event.summary for event in events)


def test_benchmark_and_system_sessions_are_excluded_even_with_edits(change: Change) -> None:
    change.ledger("claude", "bench", [_edit("src/cache.py", 1)], agent="lemoncrow:benchmark")
    change.ledger("lemoncrow-server", "indexer", [_edit("src/session.py", 2)])

    change.sync()

    assert change.live()["contributors"] == []
    roles = {item.session_id: item.role for item in change.development.list_sessions(change.session.id)}
    assert roles == {"bench": "excluded", "indexer": "excluded"}
    assert change.development.event_count(change.session.id) == 0


def test_multiple_editing_agents_all_appear_as_contributors(change: Change) -> None:
    change.ledger("claude", "claude-1", [_edit("src/auth.py", 1), _edit("src/session.py", 3)])
    change.ledger("codex", "codex-1", [_edit("src/cache.py", 2)])

    change.sync()
    live = change.live()

    authors = {item["session_id"]: sorted(item["paths"]) for item in live["contributors"]}
    assert authors == {"claude-1": ["src/auth.py", "src/session.py"], "codex-1": ["src/cache.py"]}
    assert live["counts"]["authors"] == 2
    assert live["counts"]["files"] == 3


def test_subagent_investigation_alone_never_becomes_authorship(change: Change) -> None:
    change.ledger("opencode", "parent", [_edit("src/auth.py", 1)])
    change.ledger(
        "opencode",
        "child",
        [_message("Find callers of auth()", 2, role="user"), _message("auth() is called from session.py only.", 3)],
        parent_session_id="parent",
    )

    change.sync()
    live = change.live()

    assert _roles(live) == {"parent": "author"}
    rows = {item.session_id: item for item in change.development.list_sessions(change.session.id)}
    assert rows["child"].role == "investigator"
    assert rows["child"].parent_session_id == "parent"
    assert change.development.list_events(change.session.id, session=("opencode", "child"), limit=10) == []


def test_accepted_handoff_surfaces_a_fact_with_investigator_provenance(change: Change) -> None:
    change.ledger("opencode", "parent", [_edit("src/auth.py", 1)])
    change.ledger("opencode", "child", [_message("Callers traced.", 2)], parent_session_id="parent")
    change.sync()

    with pytest.raises(ValueError, match="no Development evidence"):
        push_events(
            change.development,
            change.session.id,
            [{"kind": "fact", "host": "opencode", "session_id": "child", "summary": "unaccepted"}],
        )
    result = push_events(
        change.development,
        change.session.id,
        [
            {
                "kind": "fact",
                "host": "opencode",
                "session_id": "child",
                "summary": "auth() is only called from session.py",
                "accepted_by": "opencode:parent",
                "source_event_id": "handoff-1",
            }
        ],
    )

    assert len(result.inserted) == 1
    live = change.live()
    assert _roles(live) == {"parent": "author", "child": "investigator"}
    [decision] = live["decisions"]
    assert decision["summary"] == "auth() is only called from session.py"
    assert (decision["host"], decision["session_id"], decision["role"]) == ("opencode", "child", "investigator")
    assert decision["status"] == "accepted"
    assert live["counts"]["authors"] == 1


def test_review_and_ask_sessions_do_not_pollute_development(change: Change) -> None:
    change.ledger("claude", "author", [_edit("src/auth.py", 1)])
    change.ledger(
        "claude",
        "reviewer",
        [
            _message("Reviewing the diff.", 2, role="user"),
            _command("uv run pytest -q", 3),
            _message("LGTM overall.", 4),
        ],
        agent="lemoncrow:review",
    )
    change.ledger(
        "claude",
        "fixing-reviewer",
        [_message("Apply the suggestion", 5, role="user"), _edit("src/cache.py", 6), _message("Applied it.", 7)],
        agent="lemoncrow:review",
    )
    bind_session(change.development, change.session.id, host="claude", session_id="asker", purpose="ask")

    change.sync()
    live = change.live()

    assert _roles(live) == {"author": "author", "fixing-reviewer": "author"}
    reviewer_events = change.development.list_events(change.session.id, session=("claude", "reviewer"), limit=10)
    assert reviewer_events == []
    fixing = change.development.list_events(change.session.id, session=("claude", "fixing-reviewer"), limit=10)
    assert {event.kind for event in fixing} == {"edit"}
    with pytest.raises(ValueError):
        push_events(
            change.development,
            change.session.id,
            [{"kind": "message", "host": "claude", "session_id": "asker", "summary": "What does auth do?"}],
        )


# --------------------------------------------------------------------------- #
# 9-11: stream integrity                                                      #
# --------------------------------------------------------------------------- #


def _stream_shape(change: Change) -> list[tuple[str, str, str]]:
    events = change.development.list_events(change.session.id, limit=200)
    return [(event.kind, event.session_id, event.summary) for event in sorted(events, key=lambda e: e.sequence)]


def test_event_ordering_is_deterministic_across_ingestion_order(tmp_path: Path) -> None:
    shapes = []
    for variant, order in (("a", ("claude", "codex")), ("b", ("codex", "claude"))):
        change = Change(tmp_path / variant)
        ledgers = {
            "claude": [_edit("src/auth.py", 1), _edit("src/session.py", 4)],
            "codex": [_edit("src/cache.py", 2), _command("pytest -q", 3)],
        }
        for host in order:
            change.ledger(host, f"{host}-s", ledgers[host])
        change.sync()
        shapes.append(_stream_shape(change))

    assert shapes[0] == shapes[1]
    edits = [row for row in shapes[0] if row[0] == "edit"]
    assert [row[2] for row in edits] == ["Edited src/auth.py", "Edited src/cache.py", "Edited src/session.py"]


def test_replayed_and_duplicate_events_are_idempotent(change: Change) -> None:
    path = change.ledger("claude", "author", [_edit("src/auth.py", 1), _command("pytest -q", 2)])
    first = change.sync()
    count = change.development.event_count(change.session.id)
    cursor = change.development.cursor(change.session.id)
    assert first == count > 0

    # Same bytes, new mtime: parsed again, nothing appended.
    stamp = path.stat().st_mtime + 5
    os.utime(path, (stamp, stamp))
    assert change.sync() == 0
    # Ledger rewritten with its head evicted and the tail replayed.
    raw = json.loads(path.read_text())
    raw["events"] = [*raw["events"][1:], _edit("src/cache.py", 3)]
    path.write_text(json.dumps(raw))
    os.utime(path, (stamp + 5, stamp + 5))
    assert change.sync() == 1
    # Explicit pushes are idempotent on their source id.
    body = [{"kind": "test", "host": "claude", "session_id": "author", "summary": "ci", "source_event_id": "ci-1"}]
    assert len(push_events(change.development, change.session.id, body).inserted) == 1
    assert len(push_events(change.development, change.session.id, body).inserted) == 0

    assert change.development.event_count(change.session.id) == count + 2
    assert change.development.cursor(change.session.id) == cursor + 2


def test_secrets_raw_prompts_and_reasoning_never_leak(change: Change) -> None:
    ledger = RunLedger(
        session_id="leaky",
        agent="claude",
        root=change.store_root,
        task="Fix auth",
        workspace_path=change.repo,
    )
    ledger.record_file_event("src/auth.py", "edit", "+API_KEY = 'DIFF_BODY_MUST_NOT_LEAK'")
    ledger.record_command(
        "pytest tests -q", True, stdout="STDOUT_MUST_NOT_LEAK token=abc", stderr="STDERR_MUST_NOT_LEAK"
    )
    ledger.record(
        "agent_message",
        "assistant: done",
        {"role": "assistant", "message": "Rotated it; token=supersecret-development-value is gone."},
    )
    ledger.record("reasoning", "PRIVATE_REASONING_MUST_NOT_LEAK", {"text": "PRIVATE_REASONING_DETAIL"})
    ledger.record_tool_call("read", {"path": "src/auth.py"}, "TOOL_OUTPUT_MUST_NOT_LEAK")
    ledger.record_call(
        operation="probe",
        model="gpt-test",
        input_tokens=1,
        output_tokens=1,
        prompt="RAW_MODEL_PROMPT_MUST_NOT_LEAK",
        response="RAW_MODEL_RESPONSE_MUST_NOT_LEAK",
    )
    ledger.add_verified_fact("The session secret is password=hunter2-development")
    ledger.persist()

    change.sync()
    revision = change.snapshot()
    rendered = change.dump() + json.dumps(change.view(revision)) + json.dumps(change.live())

    for secret in (
        "DIFF_BODY_MUST_NOT_LEAK",
        "STDOUT_MUST_NOT_LEAK",
        "STDERR_MUST_NOT_LEAK",
        "supersecret-development-value",
        "PRIVATE_REASONING",
        "TOOL_OUTPUT_MUST_NOT_LEAK",
        "RAW_MODEL_PROMPT_MUST_NOT_LEAK",
        "RAW_MODEL_RESPONSE_MUST_NOT_LEAK",
        "hunter2-development",
    ):
        assert secret not in rendered, secret
    assert "Edited src/auth.py" in rendered
    assert "pytest tests -q" in rendered


# --------------------------------------------------------------------------- #
# 12-14: Snapshots                                                            #
# --------------------------------------------------------------------------- #


def test_snapshot_projection_is_immutable_and_since_uses_only_newer_events(change: Change) -> None:
    change.ledger("claude", "author", [_edit("src/auth.py", 1), _command("pytest -q", 2)])
    first = change.snapshot()
    frozen = change.view(first)["snapshot"]
    watermark = frozen["watermark"]
    assert frozen["source"] == "events"
    assert watermark == change.development.cursor(change.session.id) > 0
    assert frozen["counts"]["authors"] == 1

    change.write("src/cache.py", "def cache():\n    return 3\n")
    change.ledger("codex", "late", [_edit("src/cache.py", 10), _command("pytest -q", 11, ok=False)])
    change.sync()

    after = change.view(first)
    assert after["snapshot"] == frozen
    # Not an artifact of caching: recomputing from events <= watermark agrees.
    with sqlite3.connect(change.store.db_path) as conn:
        conn.execute("UPDATE development_snapshots SET projection_json = '' WHERE revision_id = ?", (first.id,))
    assert change.view(first)["snapshot"] == frozen
    since = after["since"]
    assert since is not None
    assert _roles(since) == {"late": "author"}
    assert since["newer_paths"] == ["src/cache.py"]
    assert since["verification"]["fail"] == 1
    newer = change.development.list_events(change.session.id, after=watermark, limit=50)
    assert newer and all(event.sequence > watermark for event in newer)
    assert all(event.session_id == "late" for event in newer)

    second = change.snapshot()
    assert change.view(second)["snapshot"]["counts"]["authors"] == 2
    assert change.view(first)["snapshot"] == frozen


def test_since_folds_every_newer_event_and_checkpoints_exactly(change: Change, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import development as development_module

    # A tiny page forces the paged fold across many pages: nothing is capped.
    monkeypatch.setattr(development_module, "_FOLD_PAGE", 3)
    change.ledger("claude", "author", [_edit("src/auth.py", 1)])
    first = change.snapshot()
    watermark = change.view(first)["snapshot"]["watermark"]

    change.write("src/cache.py", "def cache():\n    return 3\n")
    late = [_edit("src/cache.py", 10)] + [_command(f"pytest -q -k case{i}", 11 + i) for i in range(8)]
    change.ledger("codex", "late", late)
    change.sync()
    cursor = change.development.cursor(change.session.id)
    since = change.view(first)["since"]
    assert since["event_count"] == cursor - watermark > 3
    assert since["verification"]["pass"] == 8
    assert "truncated" not in since

    def checkpoint() -> int:
        with sqlite3.connect(change.store.db_path) as conn:
            row = conn.execute(
                "SELECT since_cursor FROM development_snapshots WHERE revision_id = ?", (first.id,)
            ).fetchone()
        return int(row[0])

    assert checkpoint() == cursor

    change.ledger("codex", "late", late + [_command(f"pytest -q -k more{i}", 30 + i, ok=i != 0) for i in range(4)])
    change.sync()
    incremental = change.view(first)["since"]
    assert checkpoint() == change.development.cursor(change.session.id) > cursor
    assert incremental["verification"]["fail"] == 1
    # The checkpointed fold equals a from-scratch fold of the same range.
    with sqlite3.connect(change.store.db_path) as conn:
        conn.execute(
            "UPDATE development_snapshots SET since_cursor = 0, since_json = '' WHERE revision_id = ?", (first.id,)
        )
    assert change.view(first)["since"] == incremental


def test_legacy_packet_journal_produces_a_valid_projection(change: Change) -> None:
    journal = [
        {"id": "j1", "kind": "intent", "at": _at(0), "summary": "Harden auth", "source": "user"},
        {"id": "j2", "kind": "edit", "at": _at(1), "summary": "edited src/auth.py", "path": "src/auth.py"},
        {"id": "j3", "kind": "command", "at": _at(2), "summary": "pytest -q", "status": "pass"},
        {"id": "j4", "kind": "command", "at": _at(3), "summary": "ls", "status": "pass"},
        {"id": "j5", "kind": "decision", "at": _at(4), "summary": "Rejected: cache the token"},
        {"id": "j6", "kind": "question", "at": _at(5), "summary": "Open question: rotate keys?"},
        {"id": "j7", "kind": "tool", "at": _at(6), "summary": "read", "path": "src/auth.py"},
    ]
    provenance = {"certainty": "exact", "host": "claude", "session_id": "legacy-author"}

    events = legacy_events(change.session.id, journal, provenance=provenance)
    projection = render_projection(
        reduce_events(events),
        scope="snapshot",
        source="legacy_journal",
        snapshot_paths=["src/auth.py", "src/cache.py"],
    )

    assert projection["available"] is True
    assert projection["intent"] == "Harden auth"
    assert _roles(projection) == {"legacy-author": "author"}
    assert projection["verification"]["pass"] == 1  # `ls` is chatter, not a check
    assert [item["summary"] for item in projection["decisions"]] == ["Rejected: cache the token"]
    assert [item["summary"] for item in projection["open_items"]] == ["Open question: rotate keys?"]
    assert projection["unattributed_paths"] == ["src/cache.py"]

    # A Snapshot recorded before the stream existed reads its frozen journal.
    revision = change.snapshot()
    with sqlite3.connect(change.store.db_path) as conn:
        conn.execute("DELETE FROM development_snapshots WHERE revision_id = ?", (revision.id,))
    packet = dict(read_packet_json(change.store, revision) or {})
    packet["journal"] = journal
    packet["provenance"] = {**packet.get("provenance", {}), **provenance}
    view = development_view(
        change.development,
        change_id=change.session.id,
        revision_id=revision.id,
        revision_number=0,
        revision_created_at=revision.created_at,
        packet=packet,
        attributions=[],
        revision_numbers={revision.id: 0},
        historical=True,
        live_enabled=True,
    )
    assert view["snapshot"]["source"] == "legacy_journal"
    assert view["snapshot"]["watermark"] is None
    assert _roles(view["snapshot"]) == {"legacy-author": "author"}


def test_manual_changes_are_unattributed_never_credited_to_an_agent(change: Change) -> None:
    change.write("src/session.py", "def session():\n    return 9\n")  # nobody recorded this edit
    change.ledger("claude", "author", [_edit("src/auth.py", 1)])

    snapshot = change.view(change.snapshot())["snapshot"]

    assert _roles(snapshot) == {"author": "author"}
    assert snapshot["contributors"][0]["paths"] == ["src/auth.py"]
    assert snapshot["unattributed_paths"] == ["src/session.py"]


# --------------------------------------------------------------------------- #
# Feedback causation                                                          #
# --------------------------------------------------------------------------- #


def test_feedback_handoff_links_later_fix_without_creating_comments(change: Change) -> None:
    change.ledger("claude", "author", [_edit("src/auth.py", 1)])
    first = change.snapshot()
    handoff_at = datetime.now(UTC)
    record_feedback_handoff(
        change.development,
        change.session.id,
        host="claude",
        session_id="author",
        operation_id="op-1",
        annotations=[("ann-1", "Handle expired tokens")],
        state="sent",
    )
    later = handoff_at + timedelta(minutes=1)
    change.write("src/cache.py", "def cache():\n    return 4\n")
    change.ledger(
        "claude",
        "author",
        [
            _edit("src/auth.py", 1),
            {**_edit("src/cache.py", 0), "at": later.isoformat()},
            {**_command("pytest -q", 0), "at": (later + timedelta(seconds=5)).isoformat()},
        ],
    )
    change.sync()
    second = change.snapshot()

    view = change.view(second)["snapshot"]
    [feedback] = view["feedback"]
    assert feedback["causation_id"] == "feedback:op-1"
    assert feedback["annotation_ids"] == ["ann-1"]
    assert feedback["edit_count"] == 1
    assert feedback["paths"] == ["src/cache.py"]
    assert feedback["checks"] == {"pass": 1, "fail": 0}
    assert feedback["included_in_revision"] == second.revision_number - 1
    assert change.view(first)["snapshot"]["feedback"] == []
    assert change.store.list_annotations(change.session.id) == ()


def test_narrative_is_a_deterministic_account_of_the_projection(change: Change) -> None:
    change.write("src/cache.py", "def cache():\n    return 3\n")
    change.write("src/session.py", "def session():\n    return 9\n")
    change.ledger("claude", "a1", [_edit("src/auth.py", 1), _command("pytest -q", 2)])
    change.ledger("codex", "c1", [_edit("src/cache.py", 3), _command("ruff check .", 4, ok=False)])
    first = change.snapshot()

    narrative = change.view(first)["snapshot"]["narrative"]
    assert narrative == [
        "Claude and Codex changed 2 files — Claude: auth.py; Codex: cache.py.",
        "1 of 2 checks failed (run by Claude and Codex).",
        "1 changed file has no recorded author: session.py.",
    ]
    assert change.view(first)["snapshot"]["narrative"] == narrative


def test_agent_lifecycle_reaches_review_activity_once_per_transition(change: Change) -> None:
    change.ledger("claude", "reader", [_command("git status", 1)])
    change.ledger(
        "claude",
        "author",
        [_edit("src/auth.py", 1), _command("pytest -q", 2), _message("Auth now refreshes tokens.", 3)],
        status="completed",
    )
    change.sync()
    change.sync()
    record_feedback_handoff(
        change.development,
        change.session.id,
        host="claude",
        session_id="author",
        operation_id="op-9",
        annotations=[("ann-1", "Handle expiry"), ("ann-2", "Add a test")],
        state="sent",
    )

    activity = [
        item for item in change.store.list_activity_events(change.session.id) if item.kind.startswith("development.")
    ]
    assert sorted((item.kind, item.subject_id) for item in activity) == [
        ("development.contributor", "claude:author"),
        ("development.handoff", "claude:author"),
        ("development.session_ended", "claude:author"),
    ]
    by_kind = {item.kind: item for item in activity}
    assert by_kind["development.contributor"].summary == "Claude started authoring this Change"
    assert by_kind["development.contributor"].actor_type == "agent"
    assert by_kind["development.handoff"].summary == "Review feedback sent to Claude (2 comments)"
    assert json.loads(by_kind["development.handoff"].detail_json)["annotation_ids"] == ["ann-1", "ann-2"]
    assert by_kind["development.session_ended"].summary == "Claude finished (completed)"


# --------------------------------------------------------------------------- #
# 15-17: API, live-state and persistence                                      #
# --------------------------------------------------------------------------- #


def _client(change: Change, store: ReviewStore | None = None) -> TestClient:
    app = FastAPI()
    register_review_api(
        app,
        store or change.store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=change.repo,
        port=PORT,
        source_repo_root=change.repo,
        source_store_root=change.store_root,
    )
    return TestClient(app, base_url=BASE)


def _get(client: TestClient, path: str) -> Any:
    response = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200, response.text
    return response.json()


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    response = client.post(path, headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
    return response


def test_live_state_development_cursor_moves_only_on_development_changes(change: Change) -> None:
    change.ledger("claude", "author", [_edit("src/auth.py", 1)])
    revision = change.snapshot()
    client = _client(change)
    path = f"/api/reviews/{change.session.id}/live-state?wait_ms=0"

    first = _get(client, path)
    assert first["development_cursor"] == str(change.development.cursor(change.session.id))
    comment = client.post(
        f"/api/reviews/{change.session.id}/annotations",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "path": "src/auth.py",
            "start_line": 2,
            "body": "not development",
            "kind": "comment",
            "expected_revision_id": revision.id,
        },
    )
    assert comment.status_code == 201, comment.text
    after_comment = _get(client, path)
    assert after_comment["development_cursor"] == first["development_cursor"]
    assert after_comment["annotations_cursor"] != first["annotations_cursor"]

    pushed = _post(
        client,
        f"/api/reviews/{change.session.id}/development/events",
        {
            "events": [
                {"kind": "test", "host": "claude", "session_id": "author", "summary": "pytest -q", "status": "pass"}
            ]
        },
    )
    assert pushed.status_code == 201, pushed.text
    after_event = _get(client, f"{path}&cursor={after_comment['cursor']}")
    assert after_event["changed"] is True
    assert int(after_event["development_cursor"]) == int(first["development_cursor"]) + 1


def test_historical_development_api_returns_the_exact_frozen_state(change: Change) -> None:
    change.ledger("claude", "author", [_edit("src/auth.py", 1)])
    change.snapshot()
    client = _client(change)
    historical_path = f"/api/reviews/{change.session.id}/revisions/0/development"
    before = _get(client, historical_path)

    change.write("src/cache.py", "def cache():\n    return 5\n")
    change.ledger("codex", "late", [_edit("src/cache.py", 5)])
    change.snapshot()

    after = _get(client, historical_path)
    latest = _get(client, f"/api/reviews/{change.session.id}/development")
    assert after["historical"] is True
    assert after["snapshot"] == before["snapshot"]
    assert _roles(after["snapshot"]) == {"author": "author"}
    assert _roles(latest["snapshot"]) == {"author": "author", "late": "author"}
    assert latest["revision_number"] == 1

    frozen_timeline = _get(client, f"/api/reviews/{change.session.id}/development/events?revision=0&view=changes")
    assert [row["path"] for row in frozen_timeline["events"]] == ["src/auth.py"]
    since = _get(client, f"/api/reviews/{change.session.id}/development/events?revision=0&scope=since&view=changes")
    assert [row["path"] for row in since["events"]] == ["src/cache.py"]
    bad = client.get(
        f"/api/reviews/{change.session.id}/development/events?view=nope",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert bad.status_code == 422


def test_multi_agent_projection_survives_persistence_and_reload(change: Change) -> None:
    change.ledger("claude", "claude-1", [_edit("src/auth.py", 1), _command("pytest -q", 2)])
    change.ledger("codex", "codex-1", [_edit("src/cache.py", 3)])
    change.write("src/cache.py", "def cache():\n    return 6\n")
    revision = change.snapshot()
    original = change.view(revision)
    live = change.live()

    fresh_store = ReviewStore(change.store_root)
    fresh = DevelopmentStore(fresh_store)
    reloaded_live = render_projection(
        fresh.live_state(change.session.id),
        scope="live",
        source="events",
        sessions={item.key: item for item in fresh.list_sessions(change.session.id)},
    )
    client = _client(change, fresh_store)
    over_http = _get(client, f"/api/reviews/{change.session.id}/development")

    assert reloaded_live == live
    assert over_http["snapshot"] == json.loads(json.dumps(original["snapshot"]))
    assert _roles(over_http["snapshot"]) == {"claude-1": "author", "codex-1": "author"}


def test_development_routes_require_authentication(change: Change) -> None:
    change.snapshot()
    client = _client(change)
    for path in (
        f"/api/reviews/{change.session.id}/development",
        f"/api/reviews/{change.session.id}/revisions/0/development",
        f"/api/reviews/{change.session.id}/development/events",
    ):
        assert client.get(path).status_code in {401, 403}
    assert client.post(f"/api/reviews/{change.session.id}/development/events", json={"events": []}).status_code in {
        401,
        403,
    }
