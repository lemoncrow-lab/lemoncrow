"""SSE watches real writes and closes native watcher tasks with the subscriber."""

import asyncio
import json

from lemoncrow.pro.capabilities.review import state_events as module


def payload(frame):
    return json.loads(frame.split("data: ", 1)[1])


def test_source_notifications_and_quiet_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "QUIET_SECONDS", 0.1)
    path = tmp_path / "source.py"
    path.write_text("before")
    calls = []

    def project(_changes):
        fingerprint = path.read_text() if path.exists() else "deleted"
        calls.append(fingerprint)
        return {"fingerprint": fingerprint, "changed": fingerprint != "before"}

    async def run():
        stream = module.state_events([tmp_path], project, event="source-state", settle=True)
        initial = payload(await anext(stream))
        assert initial == {"fingerprint": "before", "changed": False, "settled": False}
        assert payload(await anext(stream))["settled"] is True
        path.write_text("after")
        changed = payload(await asyncio.wait_for(anext(stream), 3))
        assert changed["fingerprint"] == "after"
        assert changed["settled"] is False
        settled = payload(await asyncio.wait_for(anext(stream), 3))
        assert settled["settled"] is True
        assert settled["fingerprint"] == "after"
        path.unlink()
        assert payload(await asyncio.wait_for(anext(stream), 3))["fingerprint"] == "deleted"
        await stream.aclose()
        count = len(calls)
        await asyncio.sleep(0.2)
        assert len(calls) == count
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]

    asyncio.run(run())


def test_live_stream_suppresses_unchanged_state_and_does_not_probe_on_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.1)
    path = tmp_path / "context.json"
    path.write_text("1")
    calls = []

    def project(changes):
        calls.append(changes)
        return {"cursor": path.read_text()}

    async def run():
        stream = module.state_events([tmp_path], project, event="live-state")
        assert payload(await anext(stream)) == {"cursor": "1"}
        assert await anext(stream) == ": keepalive\n\n"
        assert len(calls) == 1
        path.write_text("2")
        frame = await asyncio.wait_for(anext(stream), 3)
        while frame.startswith(":"):
            frame = await asyncio.wait_for(anext(stream), 3)
        assert payload(frame) == {"cursor": "2"}
        assert len(calls) == 2
        await stream.aclose()

    asyncio.run(run())


def test_polled_streams_hold_no_request_thread_and_still_report_changes(tmp_path, monkeypatch):
    """Many open polled streams leave the loop's shared thread limiter free for requests."""

    import anyio.to_thread

    monkeypatch.setattr(module, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.1)
    state = {"cursor": "1"}

    async def run():
        limiter = anyio.to_thread.current_default_thread_limiter()
        streams = [
            module.state_events([tmp_path], lambda _: dict(state), event="live-state", filesystem=False)
            for _ in range(limiter.total_tokens + 5)
        ]
        for stream in streams:
            assert payload(await anext(stream)) == {"cursor": "1"}
        await asyncio.sleep(0.12)
        assert limiter.borrowed_tokens == 0
        # A synchronous request handler still gets a thread at once.
        assert await asyncio.wait_for(anyio.to_thread.run_sync(lambda: "served"), 1) == "served"
        first = streams[0]
        assert await asyncio.wait_for(anext(first), 1) == ": keepalive\n\n"
        state["cursor"] = "2"
        frame = await asyncio.wait_for(anext(first), 1)
        while frame.startswith(":"):
            frame = await asyncio.wait_for(anext(first), 1)
        assert payload(frame) == {"cursor": "2"}
        for stream in streams:
            await stream.aclose()

    asyncio.run(run())


def test_polled_source_stream_settles_after_two_agreeing_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "POLL_SECONDS", 0.02)
    state = {"fingerprint": "before"}

    async def run():
        stream = module.state_events(
            [tmp_path], lambda _: dict(state), event="source-state", settle=True, filesystem=False
        )
        assert payload(await anext(stream)) == {"fingerprint": "before", "settled": False}
        assert payload(await asyncio.wait_for(anext(stream), 1)) == {"fingerprint": "before", "settled": True}
        state["fingerprint"] = "after"
        assert payload(await asyncio.wait_for(anext(stream), 1)) == {"fingerprint": "after", "settled": False}
        assert payload(await asyncio.wait_for(anext(stream), 1)) == {"fingerprint": "after", "settled": True}
        await stream.aclose()

    asyncio.run(run())
