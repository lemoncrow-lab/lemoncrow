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


@pytest.mark.parametrize(("raw", "expected"), [("3", 3), ("99", 16), ("0", "auto"), ("many", "auto"), (None, "auto")])
def test_concurrency_setting_is_clamped_and_zero_means_automatic(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int | str
) -> None:
    if raw is None:
        monkeypatch.delenv(sandbox_limits.CONCURRENCY_ENV, raising=False)
    else:
        monkeypatch.setenv(sandbox_limits.CONCURRENCY_ENV, raw)
    automatic = max(1, min(4, (os.cpu_count() or 2) // 2))
    assert sandbox_limits.sandbox_concurrency() == (automatic if expected == "auto" else expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 4096), ("2048", 2048), ("10", 256), ("999999", 65536), ("lots", 4096), ("0", 0)],
)
def test_memory_ceiling_is_clamped_and_zero_is_an_explicit_opt_out(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int
) -> None:
    if raw is None:
        monkeypatch.delenv(MEMORY_ENV, raising=False)
    else:
        monkeypatch.setenv(MEMORY_ENV, raw)
    assert sandbox_memory_mb() == expected
    if expected == 0:
        assert limit_prefix() == []
        assert limit_note(137) == ""


def test_cgroup_scope_caps_the_whole_tree_and_the_fallback_caps_each_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    grab = "b = bytearray({}); b[::4096] = b'x' * len(b[::4096]); print('allocated')"

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


def test_the_build_cache_keeps_recent_builds_and_drops_idle_and_retired_ones(tmp_path: Path) -> None:
    review_root = tmp_path / "review" / "web-previews" / "review-1"
    builds = review_root / "builds"
    now = time.time()
    entries = []
    for index in range(web_preview.KEEP_BUILDS_PER_REVIEW + 3):
        entry = builds / f"build-{index}"
        entry.mkdir(parents=True)
        age = 7200 + index  # all idle; lower index = more recently used
        os.utime(entry, (now - age, now - age))
        entries.append(entry)
    fresh_extra = builds / "in-use"
    fresh_extra.mkdir()
    retired = review_root / "01a0-old-per-snapshot-layout"
    retired.mkdir()
    os.utime(retired, (now - 7200, now - 7200))
    current = entries[-1]

    web_preview._prune_web_preview_cache(tmp_path, SimpleNamespace(review_id="review-1"), current)  # type: ignore[arg-type]

    survivors = {path.name for path in builds.iterdir()}
    assert current.name in survivors  # the build being served is never pruned
    assert "in-use" in survivors  # recently used, even beyond the keep count
    assert len(survivors) == web_preview.KEEP_BUILDS_PER_REVIEW
    assert not retired.exists()


def test_the_limiter_wraps_real_subprocess_calls(monkeypatch: pytest.MonkeyPatch, slots) -> None:  # type: ignore[no-untyped-def]
    slots(1)
    seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        assert kwargs["timeout"] == 12
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(sandbox_limits.subprocess, "run", fake_run)
    run_limited(["bwrap", "true"], timeout=12)

    assert seen == [[*limit_prefix(), "bwrap", "true"]]
