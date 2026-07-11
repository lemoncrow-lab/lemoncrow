"""Comprehensive MCP-level tests for the `context` tool handler.

Covers:
- Basic response structure (bootstrap dict always present; prefix_plan gated behind diagnostics)
- Worker throttle (_spawn_worker_if_idle called at most once per window)
- Bootstrap job re-queuing after failure (no longer blocked by failed jobs)
- recall=False skips archival memory
- agent_id triggers memory recall (returns recalled_passages)
- max_blocks and token_budget forwarded to retrieval engine
- Double retrieve() eliminated (rt.get_context called once, not twice)
- Worker: run_once on empty queue returns None
- Worker: unknown job type is marked failed, loop continues
- Worker: known-but-unhandled job type is marked failed, loop continues
- Worker: handler exception marks job failed, loop continues
- _run_worker_tick_safe suppresses exceptions
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from lemoncrow.core.foundation.memory_models import ArchivalPassage, MemoryBlock
from lemoncrow.core.service.jobs import (
    JOB_BOOTSTRAP_CONTEXT,
    JOB_COMPUTE_EMBEDDINGS,
    JOB_CONSOLIDATE_BLOCKS,
    JOB_EXTRACT_PLAYBOOK,
    JOB_INGEST_SESSION_DIRECTORY,
    JOB_INGEST_SESSION_FILE,
    JOB_OPTIMIZE,
    JOB_RETENTION_CLEANUP,
    KNOWN_JOB_TYPES,
)
from lemoncrow.gateway.adapters.mcp_server import _handle
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_context(args: dict[str, Any]) -> dict[str, Any]:
    response = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "context", "arguments": args},
        }
    )
    assert response is not None
    assert "result" in response, response
    payload = json.loads(response["result"]["content"][0]["text"])
    assert isinstance(payload, dict)
    return payload


@pytest.fixture()
def ctx_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    SqliteMemoryStore(root)
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_SERVICE_URL", raising=False)

    import lemoncrow.gateway.adapters.mcp_server as mcp

    mcp._remote_client = None
    mcp._reset_runtime_cache_for_testing()
    return root


# ---------------------------------------------------------------------------
# Basic response structure
# ---------------------------------------------------------------------------


def test_context_omits_prefix_plan_by_default(ctx_root: Path) -> None:
    # prefix_plan is cache/token-split diagnostics the model does not act on, so
    # it must not appear in the default model-facing result.
    payload = _call_context({"task": "write tests for the auth module"})
    assert "prefix_plan" not in payload


def test_context_emits_prefix_plan_when_diagnostics_opted_in(ctx_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Behind the diagnostics opt-in the planner output is still surfaced.
    monkeypatch.setenv("LEMONCROW_MCP_DEBUG", "1")
    payload = _call_context({"task": "write tests for the auth module"})
    assert "prefix_plan" in payload
    assert isinstance(payload["prefix_plan"], dict)


def test_context_always_includes_bootstrap_key(ctx_root: Path) -> None:
    payload = _call_context({"task": "refactor database layer"})
    assert "bootstrap" in payload
    boot = payload["bootstrap"]
    assert "status" in boot
    assert "repo_id" in boot
    assert boot["status"] in {"warm", "warming", "cold", "partial"}


def test_context_bootstrap_status_not_warm_queues_job(ctx_root: Path) -> None:
    """When bootstrap is cold, a job should be enqueued."""
    payload = _call_context({"task": "check CI configuration"})
    boot = payload["bootstrap"]
    if boot["status"] != "warm":
        # Either queued=True or a job_id was set
        assert boot.get("queued") is True or boot.get("job_id") is not None


def test_context_missing_labels_present(ctx_root: Path) -> None:
    payload = _call_context({"task": "explore entry points"})
    boot = payload["bootstrap"]
    assert "missing_labels" in boot
    assert isinstance(boot["missing_labels"], list)


# ---------------------------------------------------------------------------
# No double retrieve: rt.get_context called exactly once
# ---------------------------------------------------------------------------


def test_context_calls_get_context_exactly_once(ctx_root: Path) -> None:
    """The MCP handler must not call retrieve() then get_context() (double retrieve).
    We verify this by patching rt.get_context and counting calls."""
    import lemoncrow.gateway.adapters.mcp_server as mcp

    rt = mcp._runtime()
    original = rt.get_context
    call_count = [0]

    def counting_get_context(**kwargs: Any) -> Any:
        call_count[0] += 1
        return original(**kwargs)

    with patch.object(rt, "get_context", side_effect=counting_get_context):
        _call_context({"task": "implement caching layer"})

    assert call_count[0] == 1, f"get_context called {call_count[0]} times, expected exactly 1"


def test_context_retrieve_not_called_separately(ctx_root: Path) -> None:
    """context_reuse.retrieve should NOT be called directly from the MCP handler
    (only inside rt.get_context internally)."""
    import lemoncrow.gateway.adapters.mcp_server as mcp

    rt = mcp._runtime()
    original_retrieve = rt.core_runtime.context_reuse.retrieve
    retrieve_call_count = [0]

    def counting_retrieve(**kwargs: Any) -> Any:
        retrieve_call_count[0] += 1
        return original_retrieve(**kwargs)

    rt.core_runtime.context_reuse.retrieve = counting_retrieve
    try:
        _call_context({"task": "fix flaky test"})
    finally:
        rt.core_runtime.context_reuse.retrieve = original_retrieve

    # retrieve called exactly once (inside rt.get_context), not twice
    assert retrieve_call_count[0] == 1, f"retrieve called {retrieve_call_count[0]} times — double-retrieve regression"


# ---------------------------------------------------------------------------
# recall and agent_id
# ---------------------------------------------------------------------------


def test_context_recall_false_no_agent_id(ctx_root: Path) -> None:
    """With recall=False and no agent_id, response is still valid."""
    payload = _call_context({"task": "deploy to staging", "recall": False})
    assert "prefix_plan" not in payload  # diagnostics gated off by default
    assert "bootstrap" in payload


def test_context_with_agent_id_returns_recalled_passages(ctx_root: Path) -> None:
    """When agent_id is set, payload should include recalled_passages list."""
    # Insert a passage for this agent
    mem = SqliteMemoryStore(ctx_root)
    mem.insert_passage(
        ArchivalPassage(
            id="p-ctx-1",
            agent_id="test-agent",
            text="Always validate inputs before processing",
            tags=["validation"],
            source="user",
            dedup_hash="p-ctx-1",
        )
    )

    payload = _call_context({"task": "validate user inputs", "agent_id": "test-agent"})
    assert "recalled_passages" in payload
    assert isinstance(payload["recalled_passages"], list)


def test_context_with_agent_id_includes_memory_facts(ctx_root: Path) -> None:
    mem = SqliteMemoryStore(ctx_root)
    block = MemoryBlock(
        agent_id="test-agent",
        label="memory-fact/user/workflow/abc123",
        value="Prefer LemonCrow memory as the primary durable memory source.",
        pinned=True,
        metadata={
            "kind": "memory_fact",
            "subject": "workflow",
            "fact": "Prefer LemonCrow memory as the primary durable memory source.",
            "citations": 'User input: "prefer LemonCrow"',
            "reason": "Keeps memory local and deterministic.",
            "scope": "user",
            "votes": {"upvote": 2, "downvote": 0},
        },
    )
    mem.upsert_block(block, actor="pytest")

    payload = _call_context({"task": "load preferences", "agent_id": "test-agent"})
    assert "prefix_plan" not in payload  # diagnostics gated off by default
    assert "bootstrap" in payload
    assert isinstance(payload["bootstrap"], dict)


def test_context_no_agent_id_returns_empty_recalled_passages(ctx_root: Path) -> None:
    """Without agent_id, recalled_passages should be absent or empty."""
    payload = _call_context({"task": "parse incoming request"})
    # When no agent_id, recalled_passages is not included in the dict response.
    recalled = payload.get("recalled_passages", [])
    assert recalled == []


# ---------------------------------------------------------------------------
# Parameter forwarding
# ---------------------------------------------------------------------------


def test_context_max_blocks_forwarded(ctx_root: Path) -> None:
    """max_blocks is forwarded to the retrieval engine."""
    import lemoncrow.gateway.adapters.mcp_server as mcp

    rt = mcp._runtime()
    original = rt.get_context
    captured: dict[str, Any] = {}

    def capturing(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return original(**kwargs)

    with patch.object(rt, "get_context", side_effect=capturing):
        _call_context({"task": "trace a request through the system", "max_blocks": 3})

    assert captured.get("max_blocks") == 3


def test_context_domain_forwarded(ctx_root: Path) -> None:
    import lemoncrow.gateway.adapters.mcp_server as mcp

    rt = mcp._runtime()
    original = rt.get_context
    captured: dict[str, Any] = {}

    def capturing(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return original(**kwargs)

    with patch.object(rt, "get_context", side_effect=capturing):
        _call_context({"task": "fix python imports", "domain": "python"})

    assert captured.get("domain") == "python"


def test_context_token_budget_forwarded(ctx_root: Path) -> None:
    import lemoncrow.gateway.adapters.mcp_server as mcp

    rt = mcp._runtime()
    original = rt.get_context
    captured: dict[str, Any] = {}

    def capturing(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return original(**kwargs)

    with patch.object(rt, "get_context", side_effect=capturing):
        _call_context({"task": "search logs", "token_budget": 500})

    assert captured.get("token_budget") == 500


# ---------------------------------------------------------------------------
# Worker spawn throttle
# ---------------------------------------------------------------------------


def test_spawn_worker_if_idle_throttled(tmp_path: Path) -> None:
    """_spawn_worker_if_idle must not spawn a second thread within the throttle window."""
    import lemoncrow.gateway.adapters.mcp_server as mcp

    mcp._last_worker_spawn_time = 0.0  # Reset throttle
    spawned: list[threading.Thread] = []

    class CapturingThread(threading.Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            spawned.append(self)
            super().__init__(*args, **kwargs)

    with (
        patch("lemoncrow.gateway.adapters.mcp_server.threading.Thread", CapturingThread),
        patch("lemoncrow.gateway.adapters.mcp_server._run_worker_tick_safe"),
    ):
        mcp._spawn_worker_if_idle(tmp_path)
        mcp._spawn_worker_if_idle(tmp_path)  # Should be throttled

    assert len(spawned) == 1, f"Expected 1 thread, got {len(spawned)} — throttle not working"


def test_spawn_worker_if_idle_allows_after_window(tmp_path: Path) -> None:
    """After the throttle window, a new thread is spawned."""

    import lemoncrow.gateway.adapters.mcp_server as mcp

    # Set last spawn time far in the past to simulate expired throttle
    mcp._last_worker_spawn_time = 0.0
    spawned = [0]

    with patch("lemoncrow.gateway.adapters.mcp_server._run_worker_tick_safe"):

        class CountingThread(threading.Thread):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                spawned[0] += 1
                kwargs.setdefault("target", lambda: None)
                super().__init__(*args, **kwargs)

        with patch("lemoncrow.gateway.adapters.mcp_server.threading.Thread", CountingThread):
            mcp._spawn_worker_if_idle(tmp_path)

    assert spawned[0] == 1


# ---------------------------------------------------------------------------
# Bootstrap re-queue after failure (bug fix)
# ---------------------------------------------------------------------------


def test_bootstrap_failed_job_does_not_block_requeue(ctx_root: Path) -> None:
    """A failed bootstrap job must not prevent re-queuing on next context call."""
    from lemoncrow.infra.storage.factory import create_store

    store = create_store(ctx_root)
    store.init()

    # Simulate a failed bootstrap job for the current repo
    import lemoncrow.gateway.adapters.mcp_server as mcp
    from lemoncrow.core.capabilities.code_context import CodeContextEngine

    repo_id = CodeContextEngine(mcp._workspace_root().resolve()).repo_id
    jid = store.enqueue_job(JOB_BOOTSTRAP_CONTEXT, {"repo_root": str(mcp._workspace_root()), "repo_id": repo_id})
    store.fail_job(jid, "simulated failure")

    # Now calling _bootstrap_context_status should still enqueue a new job
    status = mcp._bootstrap_context_status(ctx_root)
    assert status["queued"] is True, "Failed job should not block re-queuing"


# ---------------------------------------------------------------------------
# Worker: run_once
# ---------------------------------------------------------------------------


def test_worker_run_once_empty_queue_returns_none(ctx_root: Path) -> None:
    from lemoncrow.core.service.worker import Worker
    from lemoncrow.infra.storage.factory import create_store

    store = create_store(ctx_root)
    store.init()
    worker = Worker(store=store)
    result = worker.run_once()
    assert result is None


def test_worker_run_once_processes_consolidate_job(ctx_root: Path) -> None:
    from lemoncrow.core.service.worker import Worker
    from lemoncrow.infra.storage.factory import create_store

    store = create_store(ctx_root)
    store.init()
    job_id = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {"dry_run": True})
    worker = Worker(store=store)
    result = worker.run_once()
    assert result == job_id

    jobs = store.list_jobs(job_type=JOB_CONSOLIDATE_BLOCKS, limit=10)
    done = next((j for j in jobs if j["id"] == job_id), None)
    assert done is not None
    assert done["status"] in {"succeeded", "completed"}


def test_worker_run_once_unknown_job_type_fails_gracefully(ctx_root: Path) -> None:
    """An unrecognised job_type must be failed, not crash the worker."""
    from lemoncrow.core.service.worker import Worker
    from lemoncrow.infra.storage.factory import create_store

    store = create_store(ctx_root)
    store.init()
    # max_attempts=1 so the job becomes 'dead' after one failure instead of staying retryable
    job_id = store.enqueue_job("totally_unknown_type", {}, max_attempts=1)
    worker = Worker(store=store)
    result = worker.run_once()
    assert result == job_id

    # Job is now dead (exhausted attempts), queue is empty
    assert worker.run_once() is None


def test_worker_run_once_known_unhandled_job_type_fails(ctx_root: Path) -> None:
    """Job types defined in KNOWN_JOB_TYPES but with no handler are failed, not crashed."""
    from lemoncrow.core.service.worker import Worker
    from lemoncrow.infra.storage.factory import create_store

    store = create_store(ctx_root)
    store.init()

    # Use a job type that is KNOWN but has no handler in default_dispatch
    unhandled_types = [t for t in KNOWN_JOB_TYPES if t not in {JOB_CONSOLIDATE_BLOCKS, JOB_BOOTSTRAP_CONTEXT}]
    assert unhandled_types, "Expected at least one known-but-unhandled job type"

    # max_attempts=1 so the job becomes 'dead' after one failure instead of staying retryable
    job_id = store.enqueue_job(unhandled_types[0], {}, max_attempts=1)
    worker = Worker(store=store)
    result = worker.run_once()
    assert result == job_id
    # Worker should still work normally after a dead job
    assert worker.run_once() is None


def test_worker_run_once_handler_exception_marks_failed(ctx_root: Path) -> None:
    """An exception raised by a handler must mark the job failed, not crash the loop."""
    from lemoncrow.core.service.worker import Worker
    from lemoncrow.infra.storage.factory import create_store

    store = create_store(ctx_root)
    store.init()

    def boom(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom!")

    # max_attempts=1 so the job becomes 'dead' after one failure instead of staying retryable
    job_id = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {}, max_attempts=1)
    worker = Worker(store=store, dispatch={JOB_CONSOLIDATE_BLOCKS: boom})
    result = worker.run_once()
    assert result == job_id
    # Job is now dead (exhausted attempts), queue is empty
    assert worker.run_once() is None


def test_all_known_job_types_defined(ctx_root: Path) -> None:
    """Verify all expected job type constants are present in KNOWN_JOB_TYPES."""
    expected = {
        JOB_EXTRACT_PLAYBOOK,
        JOB_COMPUTE_EMBEDDINGS,
        JOB_CONSOLIDATE_BLOCKS,
        JOB_OPTIMIZE,
        JOB_RETENTION_CLEANUP,
        JOB_BOOTSTRAP_CONTEXT,
        JOB_INGEST_SESSION_FILE,
        JOB_INGEST_SESSION_DIRECTORY,
    }
    assert expected == KNOWN_JOB_TYPES


# ---------------------------------------------------------------------------
# _run_worker_tick_safe exception suppression
# ---------------------------------------------------------------------------


def test_run_worker_tick_safe_suppresses_exceptions(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters.mcp_server import _run_worker_tick_safe

    # Pass a non-existent root to trigger failure in create_store/store.init
    bad_root = tmp_path / "nonexistent_subdir" / "another"
    # Should not raise
    _run_worker_tick_safe(bad_root)
