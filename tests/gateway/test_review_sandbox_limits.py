"""Host capacity for sandboxed Review work: concurrency slots, memory ceilings, build reuse."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.review import sandbox_limits, web_preview
from lemoncrow.pro.capabilities.review.sandbox_limits import (
    MEMORY_ENV,
    SandboxCapacityBusy,
    limit_note,
    limit_prefix,
    run_limited,
    run_limited_bounded,
    sandbox_memory_mb,
    sandbox_slot,
)


@pytest.fixture
def slots(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def install(size: int) -> None:
        monkeypatch.setattr(sandbox_limits, "_SLOTS", (size, threading.BoundedSemaphore(size)))

    return install


def test_a_busy_host_queues_then_reports_a_specific_reason(slots) -> None:  # type: ignore[no-untyped-def]
    slots(1)
    with sandbox_slot(1):
        started = time.monotonic()
        with pytest.raises(SandboxCapacityBusy, match=r"1 sandboxed builds, captures, or runs are already in progress"):
            with sandbox_slot(0.05):
                pass
        assert time.monotonic() - started >= 0.05  # it waited before giving up
    with sandbox_slot(0.05):  # released again
        pass


def test_concurrent_sandbox_work_never_exceeds_the_slot_pool(slots) -> None:  # type: ignore[no-untyped-def]
    slots(2)
    active = 0
    peak = 0
    guard = threading.Lock()

    def job(_index: int) -> None:
        nonlocal active, peak
        with sandbox_slot(5):
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with guard:
                active -= 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(job, range(6)))
    assert peak == 2


def test_concurrency_setting_is_clamped_and_zero_means_automatic(monkeypatch: pytest.MonkeyPatch) -> None:
    for _case_index, (raw, expected) in enumerate(
        [("3", 3), ("99", 16), ("0", "auto"), ("many", "auto"), (None, "auto")]
    ):
        with monkeypatch.context() as case_monkeypatch:
            if raw is None:
                case_monkeypatch.delenv(sandbox_limits.CONCURRENCY_ENV, raising=False)
            else:
                case_monkeypatch.setenv(sandbox_limits.CONCURRENCY_ENV, raw)
            automatic = max(1, min(4, (os.cpu_count() or 2) // 2))
            assert sandbox_limits.sandbox_concurrency() == (automatic if expected == "auto" else expected)


def test_memory_ceiling_is_clamped_and_zero_is_an_explicit_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    for _case_index, (raw, expected) in enumerate(
        [(None, 4096), ("2048", 2048), ("10", 256), ("999999", 65536), ("lots", 4096), ("0", 0)]
    ):
        with monkeypatch.context() as case_monkeypatch:
            if raw is None:
                case_monkeypatch.delenv(MEMORY_ENV, raising=False)
            else:
                case_monkeypatch.setenv(MEMORY_ENV, raw)
            assert sandbox_memory_mb() == expected
            if expected == 0:
                assert limit_prefix() == []
                assert limit_note(137) == ""


def test_cgroup_scope_caps_the_whole_tree_and_the_fallback_caps_each_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv(MEMORY_ENV, "1024")
    runner = ("/usr/bin/env", "XDG_RUNTIME_DIR=/run/user/1000", "/usr/bin/systemd-run")
    monkeypatch.setattr(sandbox_limits, "_cgroup_scope_runner", lambda: runner)
    scoped = limit_prefix()
    assert scoped[:5] == [*runner, "--user", "--scope"]
    assert f"--property=MemoryMax={1024 * 1024 * 1024}" in scoped
    assert "--property=MemorySwapMax=0" in scoped
    assert any(item.startswith("--property=TasksMax=") for item in scoped)
    assert scoped[-1] == "--"

    monkeypatch.setattr(sandbox_limits, "_cgroup_scope_runner", lambda: ())
    fallback = limit_prefix()
    assert fallback[:4] == [sys.executable, "-I", "-S", "-c"]
    assert "RLIMIT_DATA" in fallback[4] and fallback[5] == str(1024 * 1024 * 1024)


def test_the_memory_ceiling_is_enforced_on_this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real processes: whichever mechanism this host supports must actually bite."""
    monkeypatch.setenv(MEMORY_ENV, "256")
    # Darwin uses RSS sampling, so keep the allocation alive until the monitor observes it.
    grab = "import time; b = bytearray({}); b[::4096] = b'x' * len(b[::4096]); time.sleep(.15); print('allocated')"

    over = run_limited(
        [sys.executable, "-c", grab.format(700 * 1024 * 1024)], timeout=60, capture_output=True, text=True
    )
    within = run_limited(
        [sys.executable, "-c", grab.format(32 * 1024 * 1024)], timeout=60, capture_output=True, text=True
    )

    assert over.returncode != 0 and "allocated" not in over.stdout
    assert within.returncode == 0 and "allocated" in within.stdout


def test_the_limiter_works_from_the_cleared_environment_sandboxed_jobs_use() -> None:
    """Plugin sandboxes and runtime runners start jobs with only PATH and HOME set."""
    cleared = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp"}

    result = run_limited(
        [sys.executable, "-c", "print('ran')"], timeout=60, capture_output=True, text=True, env=cleared
    )

    assert (result.returncode, result.stdout.strip()) == (0, "ran"), result.stderr


def test_limit_note_explains_a_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MEMORY_ENV, "512")
    assert "512 MB memory limit" in limit_note(137)
    assert "512 MB memory limit" in limit_note(-9)
    assert limit_note(1) == ""


# --------------------------------------------------------------------------- #
# Content-keyed build reuse                                                    #
# --------------------------------------------------------------------------- #


def _revision(revision_id: str, *, base: str = "base-sha", fingerprint: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=revision_id,
        review_id="review-1",
        base_sha=base,
        head_sha="",
        range_mode="working_tree",
        source_fingerprint=fingerprint or f"fp-{revision_id}",
        tree_fingerprint="",
    )


def _root(tmp_path: Path, revision: SimpleNamespace, side: str, framework: str = "vite") -> Path:
    return web_preview._build_cache_root(
        tmp_path,
        revision,  # type: ignore[arg-type]
        side=side,
        framework=framework,
        project_root="frontend",
        dependency_repo=tmp_path / "checkout",
    )


def test_snapshots_sharing_a_side_share_its_build(tmp_path: Path) -> None:
    first, second = _revision("r1"), _revision("r2")

    assert _root(tmp_path, first, "old") == _root(tmp_path, second, "old")  # same base: one build
    assert _root(tmp_path, first, "new") != _root(tmp_path, second, "new")  # new content: its own build
    assert _root(tmp_path, first, "old") != _root(tmp_path, first, "new")
    assert _root(tmp_path, first, "old") != _root(tmp_path, first, "old", framework="next")
    # No stable identity for a side means no sharing at all.
    assert _root(tmp_path, _revision("r3", base=""), "old") != _root(tmp_path, _revision("r4", base=""), "old")


def test_reinstalling_checkout_dependencies_invalidates_a_shared_build(tmp_path: Path) -> None:
    project = tmp_path / "checkout" / "frontend"
    (project / "node_modules").mkdir(parents=True)
    (project / "package.json").write_text('{"devDependencies":{"vite":"7"}}')
    marker = project / "node_modules" / ".package-lock.json"
    marker.write_text("{}")
    revision = _revision("r1")
    before = _root(tmp_path, revision, "old")

    stat = marker.stat()
    os.utime(marker, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))  # npm install again
    assert _root(tmp_path, revision, "old") != before

    (project / "package.json").write_text('{"devDependencies":{"vite":"8"}}')
    assert _root(tmp_path, revision, "old") not in {before}


def test_builds_are_shared_across_reviews(tmp_path: Path) -> None:
    first = _revision("r1")
    other_review = SimpleNamespace(**{**vars(_revision("r9")), "review_id": "review-2"})

    # Same base content in another Review: the same build, not a per-Review copy.
    assert _root(tmp_path, first, "old") == _root(tmp_path, other_review, "old")
    assert _root(tmp_path, first, "old").parent == tmp_path / "review" / "web-previews" / "builds"


def test_the_build_cache_keeps_recent_builds_and_drops_idle_and_retired_ones(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.scratch_cache import wait_for_retired

    previews_root = tmp_path / "review" / "web-previews"
    builds = previews_root / "builds"
    now = time.time()
    entries = []
    for index in range(web_preview.KEEP_BUILDS + 3):
        entry = builds / f"build-{index}"
        entry.mkdir(parents=True)
        age = 7200 + index  # all idle; lower index = more recently used
        os.utime(entry, (now - age, now - age))
        entries.append(entry)
    fresh_extra = builds / "in-use"
    fresh_extra.mkdir()
    # Retired layouts: builds under each Review id, and before that each Snapshot.
    per_review = previews_root / "review-1"
    (per_review / "builds" / "old-key").mkdir(parents=True)
    os.utime(per_review, (now - 7200, now - 7200))
    per_snapshot = previews_root / "rev-1"
    (per_snapshot / "rrv-1").mkdir(parents=True)
    os.utime(per_snapshot, (now - 7200, now - 7200))
    current = entries[-1]

    web_preview._prune_web_preview_cache(tmp_path, current)

    survivors = {path.name for path in builds.iterdir() if not path.name.startswith(".trash-")}
    assert current.name in survivors  # the build being served is never pruned
    assert "in-use" in survivors  # recently used, even beyond the keep count
    assert len(survivors) == web_preview.KEEP_BUILDS
    assert not per_review.exists()
    assert not per_snapshot.exists()
    wait_for_retired()
    assert not [path for path in builds.iterdir() if path.name.startswith(".trash-")]


def test_the_limiter_wraps_real_subprocess_calls(monkeypatch: pytest.MonkeyPatch, slots) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sys, "platform", "linux")
    slots(1)
    seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        assert kwargs["timeout"] == 12
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(sandbox_limits.subprocess, "run", fake_run)
    run_limited(["bwrap", "true"], timeout=12)

    assert seen == [[*limit_prefix(), "bwrap", "true"]]


def test_bounded_review_capture_drains_large_independent_streams_without_unbounded_buffers(slots) -> None:  # type: ignore[no-untyped-def]
    slots(1)
    program = (
        "import sys;"
        "sys.stdout.write('A'*2000000+'stdout-END');sys.stdout.flush();"
        "sys.stderr.write('B'*2000000+'stderr-END');sys.stderr.flush()"
    )
    result = run_limited_bounded([sys.executable, "-c", program], timeout=8, max_output_bytes=128)
    assert result.returncode == 0
    assert result.stdout.endswith("stdout-END") and result.stderr.endswith("stderr-END")
    assert len(result.stdout.encode()) <= 128 and len(result.stderr.encode()) <= 128
    assert "stderr-END" not in result.stdout and "stdout-END" not in result.stderr


def test_bounded_review_capture_times_out_chatty_child_and_releases_slot(slots) -> None:  # type: ignore[no-untyped-def]
    slots(1)
    program = (
        "import sys,time;[(sys.stdout.write('x'*100000),sys.stdout.flush(),time.sleep(.001)) for _ in range(5000)]"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_limited_bounded([sys.executable, "-c", program], timeout=0.3, max_output_bytes=32)
    assert time.monotonic() - started < 3
    with sandbox_slot(0.1):
        pass


def test_bounded_review_capture_reaps_descendants_holding_pipes_after_parent_exit(slots) -> None:  # type: ignore[no-untyped-def]
    slots(1)
    # A grandchild keeps stdout/stderr open even after the direct parent exits.
    # The deadline must not leak that process group or hang forever.
    program = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_limited_bounded([sys.executable, "-c", program], timeout=0.3, max_output_bytes=32)
    assert time.monotonic() - started < 3
    with sandbox_slot(0.1):
        pass


def test_bounded_review_capture_refuses_unbounded_or_conflicting_stdio() -> None:
    with pytest.raises(ValueError, match=r"1\.\.65536"):
        run_limited_bounded(["true"], timeout=1, max_output_bytes=10_000_000)
    with pytest.raises(ValueError, match="owns stdout"):
        run_limited_bounded(["true"], timeout=1, stdout=subprocess.PIPE)
