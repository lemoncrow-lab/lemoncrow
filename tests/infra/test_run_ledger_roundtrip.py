"""``RunLedger.persist()`` -> ``load()`` must return the run that was written.

An append-only ledger whose event timestamps reset to load time is not a
ledger. ``load`` dropped ``LedgerEvent.at`` and both run-level bounds, so every
reloaded event claimed to have happened just now -- silently rewriting the one
field that says *when* an edit was made, which is exactly the field the review
correlator joins on.

The one thing that deliberately does NOT round-trip is the ``git`` anchor when
the file has none: a resumed session must re-resolve it rather than inherit a
blank. That behaviour is asserted here so a future "fix" has to argue with it.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lemoncrow.infra.runtime import run_ledger as run_ledger_mod
from lemoncrow.infra.runtime.run_ledger import RunLedger

_T0 = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def _ledger(root: Path, *, workspace: Path | None = None) -> RunLedger:
    led = RunLedger(
        session_id="sid-roundtrip",
        agent="claude",
        root=root,
        task="round trip",
        domain="code",
        workspace_path=str(workspace) if workspace else None,
    )
    led.created_at = _T0
    return led


def test_event_timestamps_survive_a_round_trip(tmp_path: Path) -> None:
    led = _ledger(tmp_path / "store")
    first = led.record_file_event(path="/repo/src/a.py", event="edit", diff="@@\n")
    second = led.record_file_event(path="/repo/src/b.py", event="edit", diff="@@\n")
    # Backdate them so "reset to load time" cannot pass by coincidence.
    first.at = _T0 + timedelta(minutes=5)
    second.at = _T0 + timedelta(minutes=9)
    path = led.persist()

    reloaded = RunLedger.load(path)

    assert [event.at for event in reloaded.events] == [first.at, second.at]


def test_run_bounds_survive_a_round_trip(tmp_path: Path) -> None:
    led = _ledger(tmp_path / "store")
    led.record("note", "hello")  # record() stamps updated_at, so backdate after
    led.updated_at = _T0 + timedelta(hours=2)
    path = led.persist()

    reloaded = RunLedger.load(path)

    assert reloaded.created_at == _T0
    assert reloaded.updated_at == _T0 + timedelta(hours=2)


def test_the_authoring_payload_survives_a_round_trip(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "HEAD").write_text("b" * 40 + "\n", encoding="utf-8")
    led = _ledger(tmp_path / "store", workspace=workspace)

    led.record_file_event(path="/repo/src/a.py", event="edit", diff="@@\n", model="claude-opus-5")
    reloaded = RunLedger.load(led.persist())

    payload = reloaded.events[0].payload
    assert payload["path"] == "/repo/src/a.py"
    assert payload["session_id"] == "sid-roundtrip"
    assert payload["host"] == "claude"
    assert payload["model"] == "claude-opus-5"
    assert payload["at_head"] == "b" * 40


def test_an_unparseable_timestamp_falls_back_to_now_rather_than_raising(tmp_path: Path) -> None:
    led = _ledger(tmp_path / "store")
    led.record("note", "hello")
    path = led.persist()
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap["events"][0]["at"] = "not a timestamp"
    snap["created_at"] = ""
    path.write_text(json.dumps(snap), encoding="utf-8")

    reloaded = RunLedger.load(path)

    assert reloaded.events[0].at.tzinfo is not None
    assert reloaded.created_at.tzinfo is not None


def test_a_naive_timestamp_is_read_as_utc(tmp_path: Path) -> None:
    # Mixing an aware default with a naive restore leaves the two uncomparable,
    # which would blow up the correlator's window arithmetic rather than skew it.
    led = _ledger(tmp_path / "store")
    led.record("note", "hello")
    path = led.persist()
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap["events"][0]["at"] = "2026-09-07T09:05:00"
    path.write_text(json.dumps(snap), encoding="utf-8")

    reloaded = RunLedger.load(path)

    assert reloaded.events[0].at == datetime(2026, 9, 7, 9, 5, tzinfo=UTC)


def test_a_ledger_without_a_git_anchor_still_re_resolves_on_resume(tmp_path: Path) -> None:
    led = _ledger(tmp_path / "store")
    led.record("note", "hello")
    path = led.persist()
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap.pop("git", None)
    path.write_text(json.dumps(snap), encoding="utf-8")

    reloaded = RunLedger.load(path)

    # Deliberately left unset so a resumed session resolves the anchor itself.
    # This is NOT a backfill hook: re-resolving later would stamp the wrong sha.
    assert reloaded._git is None


# ---------------------------------------------------------------------------
# Merge-on-persist
#
# ``persist`` is not the only writer of run.json: the Claude PostToolUse hooks
# append file_edit/command_result events to the same file from a separate
# process. Writing a snapshot of in-memory state straight over it deleted every
# one of them -- including the at_head/model/host provenance ``lc review``
# joins a diff to its author on -- so a persist has to merge, not replace.
# ---------------------------------------------------------------------------


def _run_json(root: Path) -> Path:
    from lemoncrow.core.foundation.paths import session_dir

    return session_dir(root, "claude", "sid-roundtrip") / "run.json"


def _hook_event(name: str, at: datetime, *, kind: str = "file_edit") -> dict[str, Any]:
    """The event shape integrations/claude/plugin/hooks/post_tool_use.py writes.

    ``at`` is spelled the way that hook spells it (``+00:00``), which is *not*
    how pydantic serializes the same instant (``Z``) -- the merge has to read
    them as one event, not two.
    """
    return {
        "kind": kind,
        "at": at.isoformat(),
        "summary": f"edited {name}",
        "payload": {
            "path": f"/repo/src/{name}",
            "diff": "@@\n",
            "event": "PostToolUse",
            "session_id": "sid-roundtrip",
            "host": "claude",
            "model": "claude-opus-5",
            "at_head": "c" * 40,
        },
    }


def _write_out_of_process(root: Path, events: list[dict[str, Any]], touched: list[str] | None = None) -> Path:
    path = _run_json(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "sid-roundtrip",
                "agent": "claude",
                "events": events,
                "files_touched": touched or [],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_events_appended_out_of_process_survive_a_persist(tmp_path: Path) -> None:
    root = tmp_path / "store"
    path = _write_out_of_process(
        root,
        [_hook_event("a.py", _T0 + timedelta(minutes=1))],
        touched=["/repo/src/a.py"],
    )

    # A never-load()ed ledger for the same session -- exactly what the MCP
    # server's process-global _get_ledger() hands /orchestrate.
    _ledger(root).persist()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert [event["summary"] for event in written["events"]] == ["edited a.py"]
    assert written["events"][0]["payload"]["at_head"] == "c" * 40
    assert written["events"][0]["payload"]["model"] == "claude-opus-5"
    assert written["files_touched"] == ["/repo/src/a.py"]


def test_a_reloaded_hook_event_is_not_duplicated_by_later_persists(tmp_path: Path) -> None:
    root = tmp_path / "store"
    path = _write_out_of_process(root, [_hook_event("a.py", _T0 + timedelta(minutes=1))])

    reloaded = RunLedger.load(path)
    reloaded.persist(root)
    reloaded.persist(root)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert [event["summary"] for event in written["events"]] == ["edited a.py"]


def test_the_same_event_appended_twice_out_of_process_stays_two_events(tmp_path: Path) -> None:
    """Two identical edits are two facts. De-duplication must not eat one."""

    root = tmp_path / "store"
    repeated = _hook_event("a.py", _T0 + timedelta(minutes=1))
    later = _hook_event("a.py", _T0 + timedelta(minutes=2))
    path = _write_out_of_process(root, [repeated, json.loads(json.dumps(repeated)), later])

    _ledger(root).persist()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert len(written["events"]) == 3


def test_the_merged_events_are_ordered_by_when_they_happened(tmp_path: Path) -> None:
    root = tmp_path / "store"
    path = _write_out_of_process(root, [_hook_event("a.py", _T0 + timedelta(minutes=5))])

    led = _ledger(root)
    led.record("note", "before the edit").at = _T0 + timedelta(minutes=1)
    led.record("note", "after the edit").at = _T0 + timedelta(minutes=9)
    led.persist()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert [event["summary"] for event in written["events"]] == [
        "before the edit",
        "edited a.py",
        "after the edit",
    ]


def test_the_derived_counters_describe_the_events_actually_written(tmp_path: Path) -> None:
    root = tmp_path / "store"
    recovered = _hook_event("a.py", _T0 + timedelta(minutes=1), kind="tool_call")
    # Keep the hook marker: `payload.event` is what identifies an event as
    # appended out of process, and only those are folded back in on persist.
    recovered["payload"] = {"tool": "Edit", "output_chars": 120, "event": "PostToolUse"}
    path = _write_out_of_process(root, [recovered])

    _ledger(root).persist()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["tool_call_count"] == 1
    assert written["total_tool_output_chars"] == 120


def test_a_loaded_ledger_preserves_the_persisted_cost_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "store"
    path = _write_out_of_process(root, [])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cost"] = {
        "calls": [{"model": "fixture", "cost_usd": 1.25}],
        "total_cost_usd": 1.25,
        "total_input_tokens": 100,
        "total_output_tokens": 25,
    }
    payload["token_count"] = 125
    path.write_text(json.dumps(payload), encoding="utf-8")

    RunLedger.load(path).persist(root)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["cost"] == payload["cost"]
    assert written["token_count"] == 125


def test_bash_hook_command_results_survive_a_later_persist(tmp_path: Path) -> None:
    root = tmp_path / "store"
    event = _hook_event("bash", _T0 + timedelta(minutes=1), kind="command_result")
    event["payload"] = {
        "event": "PostToolUseBash",
        "command": "pytest -q",
        "stdout": "ok",
        "stderr": "",
        "return_code": 0,
    }
    path = _write_out_of_process(root, [event])

    _ledger(root).persist()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert [item["kind"] for item in written["events"]] == ["command_result"]
    assert written["events"][0]["payload"]["command"] == "pytest -q"


def test_hook_writer_cannot_be_overwritten_between_persist_read_and_replace(tmp_path: Path, monkeypatch) -> None:
    """The per-run lock covers the whole read -> merge -> replace transaction."""

    from lemoncrow.core.foundation.run_file_io import RunFileLock, atomic_write_json

    root = tmp_path / "store"
    path = _write_out_of_process(root, [_hook_event("a.py", _T0 + timedelta(minutes=1))])
    ledger = _ledger(root)
    merge_read = threading.Event()
    release_persist = threading.Event()
    hook_done = threading.Event()
    original_merge = run_ledger_mod._merge_with_persisted

    def paused_merge(snapshot: dict[str, Any], run_path: Path) -> dict[str, Any]:
        payload = original_merge(snapshot, run_path)
        merge_read.set()
        release_persist.wait(5)
        return payload

    monkeypatch.setattr(run_ledger_mod, "_merge_with_persisted", paused_merge)
    persist_thread = threading.Thread(target=ledger.persist, daemon=True)
    persist_thread.start()
    assert merge_read.wait(5)

    def hook_writer() -> None:
        with RunFileLock(path):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.setdefault("events", []).append(_hook_event("b.py", _T0 + timedelta(minutes=2)))
            atomic_write_json(path, payload)
        hook_done.set()

    hook_thread = threading.Thread(target=hook_writer, daemon=True)
    hook_thread.start()
    assert not hook_done.wait(0.1), "hook writer bypassed the persist transaction lock"

    release_persist.set()
    persist_thread.join(5)
    hook_thread.join(5)
    assert not persist_thread.is_alive()
    assert not hook_thread.is_alive()
    assert hook_done.is_set()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert [event["summary"] for event in written["events"]] == ["edited a.py", "edited b.py"]


def test_a_corrupt_run_json_never_blocks_the_persist(tmp_path: Path) -> None:
    root = tmp_path / "store"
    path = _write_out_of_process(root, [])
    path.write_text("{ not json", encoding="utf-8")

    led = _ledger(root)
    led.record("note", "hello")
    led.persist()

    assert [event["summary"] for event in json.loads(path.read_text(encoding="utf-8"))["events"]] == ["hello"]
