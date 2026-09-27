"""Semantic change impact: detector sites, symbol mapping, and every degraded path.

Fixture repos are built with pygit2 in ``tmp_path``, so these exercise the same
rev-resolution and blob-loading code the product uses. ``LEMONCROW_AST_GREP_BIN``
is pointed at a rejected name in every test: ast-grep's managed bootstrap would
otherwise try to *download* a binary into each throwaway repo, making the suite
network-dependent. Detection therefore runs through the language-agnostic text
layer, which is exactly the degraded configuration these tests are about.
"""

from __future__ import annotations

import io
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.gitdiff import (
    collect_diff,
    head_symbol_filter,
    load_blobs,
    resolve_rev_range,
)
from lemoncrow.pro.capabilities.review.impact import ImpactResult, collect_impact
from lemoncrow.pro.capabilities.review.models import ChangedFile, ChangedSymbol, DiffHunk, ImpactSite


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject ast-grep discovery before it can reach the managed download path.

    The name ``sg`` is the one candidate ``_reject_reason`` refuses outright, so
    discovery returns "unavailable" without ever attempting a bootstrap.
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _signature(offset: int) -> pygit2.Signature:
    return pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)


def _init_repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    return repo


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = _signature(offset)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _impact(
    root: Path,
    rev: str | None = "HEAD~1",
    *,
    limit_sites: int = 40,
    head_filter: bool = False,
) -> ImpactResult:
    """Run the impact pass over *rev* exactly the way ``packet.py`` will.

    *head_filter* wires in the real ``exists_in_head`` predicate, which is what
    ``packet.py`` passes and what keeps every out-of-patch claim inside the
    revision it claims to be about.
    """

    rng = resolve_rev_range(root, rev)
    files = collect_diff(root, rng).files
    blobs = load_blobs(root, rng, files)
    return collect_impact(
        root,
        files,
        old_blobs=blobs.old,
        new_blobs=blobs.new,
        limit_sites=limit_sites,
        exists_in_head=head_symbol_filter(root, rng) if head_filter else None,
    )


def _sites(result: ImpactResult, kind: str) -> list[ImpactSite]:
    return [site for site in result.sites if site.kind == kind]


def _symbol(result: ImpactResult, name: str) -> ChangedSymbol | None:
    return next((symbol for symbol in result.symbols if symbol.symbol_name == name), None)


def _signature_change_repo(tmp_path: Path) -> Path:
    """`refresh(user)` gains a required `context`; an untouched job still calls it."""

    root = tmp_path / "signature"
    repo = _init_repo(root)
    _write(root, "src/auth.py", "def refresh(user):\n    return user\n")
    _write(
        root,
        "jobs/session_cleanup.py",
        "from src.auth import refresh\n\n\ndef sweep(user):\n    return refresh(user)\n",
    )
    _commit(repo, "base", 0)
    _write(root, "src/auth.py", "def refresh(user, context):\n    return (user, context)\n")
    _commit(repo, "require context", 60)
    return root


def test_signature_change_surfaces_untouched_caller(tmp_path: Path) -> None:
    result = _impact(_signature_change_repo(tmp_path))

    sites = _sites(result, "signature_change")
    assert sites, f"expected a signature_change site, got {result.sites!r} degraded={result.degraded!r}"
    assert any(site.path.startswith("jobs/session_cleanup.py:L") for site in sites)
    assert all(site.in_patch is False for site in sites)
    assert all(site.inspected_by_agent is None for site in sites)
    assert any("context" in (site.new or "") for site in sites)


def test_contract_literal_change_surfaces_old_consumer(tmp_path: Path) -> None:
    root = tmp_path / "literal"
    repo = _init_repo(root)
    _write(root, "src/state.py", 'INITIAL_STATE = "pending"\n\n\ndef initial():\n    return INITIAL_STATE\n')
    # The literal sits next to a delimiter: that is the "used as code, not prose"
    # shape the text-fallback gate keeps when ast-grep cannot run.
    _write(root, "src/consumer.py", 'WAITING = ("pending",)\n\n\ndef is_waiting(state):\n    return state in WAITING\n')
    _commit(repo, "base", 0)
    _write(root, "src/state.py", 'INITIAL_STATE = "queued"\n\n\ndef initial():\n    return INITIAL_STATE\n')
    _commit(repo, "rename status literal", 60)

    result = _impact(root)

    sites = _sites(result, "contract_literal")
    assert sites, f"expected a contract_literal site, got {result.sites!r} degraded={result.degraded!r}"
    assert any(site.path.startswith("src/consumer.py:L") for site in sites)
    assert any(site.old == "pending" for site in sites)
    assert all(site.in_patch is False for site in sites)


def test_apostrophe_in_prose_produces_no_contract_literal_site(tmp_path: Path) -> None:
    """The packet-level guard against the defect that shipped invented findings.

    Eight-line miniature of the real case: reviewing 983c233f2 put a single
    contract-literal warning at the top of ATTENTION -- for a SQL string that is
    present, unchanged, in both sides of the diff. A quote-counting scan has no
    notion of comments or apostrophes, so adding one comment containing ``caller's``
    flips the quote pairing for everything after it, and a literal the scan can no
    longer see reads as a literal the edit deleted. The consumer is genuine, the
    snippet is genuine, and the finding is invented.

    The edit here removes nothing at all, so the only correct site count is zero.
    """

    root = tmp_path / "apostrophe"
    repo = _init_repo(root)
    store = (
        '"""Session store."""\n\n\n'
        "def load(conn, session_id):\n"
        "    # The supervisor's queue is drained first; the ledger's floor is authoritative.\n"
        "    return conn.execute(\n"
        "        \"SELECT payload FROM traces WHERE json_extract(payload, '$.session_id') = ?\",\n"
        "        (session_id,),\n"
        "    ).fetchone()\n"
    )
    _write(root, "src/store.py", store)
    # The parallel consumer that turns a mis-scanned literal into a printed site.
    _write(
        root,
        "src/history_store.py",
        "def rows(conn, session_id):\n"
        "    return conn.execute(\n"
        "        \"SELECT payload FROM traces WHERE json_extract(payload, '$.session_id') = ?\",\n"
        "        (session_id,),\n"
        "    ).fetchall()\n",
    )
    _commit(repo, "base", 0)
    # Pure addition of one comment line. Not one literal changes.
    _write(
        root,
        "src/store.py",
        store.replace(
            "    return conn.execute(\n",
            "    # The caller's id is validated by the route before it reaches the store.\n"
            "    return conn.execute(\n",
        ),
    )
    _commit(repo, "document the id check", 60)

    result = _impact(root)

    assert _sites(result, "contract_literal") == []


def test_unreadable_language_names_the_loss_instead_of_guessing(tmp_path: Path) -> None:
    # No parser here can read a .lock file, so the honest packet reports no
    # literal site AND says a literal scan was skipped -- "nothing was removed"
    # and "nobody looked" are different claims and must not render alike.
    root = tmp_path / "unreadable"
    repo = _init_repo(root)
    _write(root, "conf/deps.lock", 'name = "legacy_key"\nver = "1.0"\n')
    _write(root, "src/consumer.py", 'CFG = {"legacy_key": 1}\n')
    _commit(repo, "base", 0)
    _write(root, "conf/deps.lock", 'ver = "1.0"\n')
    _commit(repo, "drop key", 60)

    result = _impact(root)

    assert _sites(result, "contract_literal") == []
    assert "contract_literal_unparsed" in result.degraded


def test_removed_symbol_surfaces_reference(tmp_path: Path) -> None:
    root = tmp_path / "removed"
    repo = _init_repo(root)
    _write(root, "src/loader.py", "def load_config():\n    return {}\n\n\ndef keep():\n    return 1\n")
    _write(root, "src/app.py", "from src.loader import load_config\n\n\ndef boot():\n    return load_config()\n")
    _commit(repo, "base", 0)
    _write(root, "src/loader.py", "def keep():\n    return 1\n")
    _commit(repo, "drop load_config", 60)

    result = _impact(root)

    sites = _sites(result, "removed_symbol")
    assert sites, f"expected a removed_symbol site, got {result.sites!r} degraded={result.degraded!r}"
    assert any(site.path.startswith("src/app.py:L") for site in sites)
    assert any(site.old == "load_config" for site in sites)


def test_impact_degrades_without_index(tmp_path: Path) -> None:
    root = _signature_change_repo(tmp_path)
    shutil.rmtree(root / ".lemoncrow" / "workspace", ignore_errors=True)

    result = _impact(root)

    assert result.index_status == "absent"
    assert "symbol_relations" in result.degraded
    assert "centrality" in result.degraded
    assert result.symbols, "tree-sitter must still name the changed definitions"
    assert all(symbol.source == "tree_sitter" for symbol in result.symbols)
    assert all(symbol.caller_count == -1 for symbol in result.symbols)
    assert all(symbol.usage_count == -1 for symbol in result.symbols)
    assert all(symbol.centrality_rank is None for symbol in result.symbols)
    assert _symbol(result, "refresh") is not None


def test_impact_never_raises_when_astgrep_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow.pro.capabilities.tool_supervision import edit_impact

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ast-grep exploded")

    monkeypatch.setattr(edit_impact, "_astgrep_detect", _boom)

    result = _impact(_signature_change_repo(tmp_path))

    assert "signature_change_impact" in result.degraded
    assert "astgrep_unavailable" in result.degraded
    assert _sites(result, "signature_change") == []
    assert result.symbols, "a failing detector must not cost the symbol list"


def test_detectors_run_concurrently_but_keep_failure_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from lemoncrow.pro.capabilities.review import impact

    barrier = threading.Barrier(4)

    def _run(result: Any = None, *, explode: bool = False, **_kwargs: Any) -> Any:
        barrier.wait(timeout=2)
        if explode:
            raise RuntimeError("detector failed")
        return result

    monkeypatch.setattr(
        impact,
        "contract_literal_impact",
        lambda *args, **kwargs: _run({"sites": [], "degraded": ["contract_literal_unparsed"]}, **kwargs),
    )
    monkeypatch.setattr(impact, "decorator_contract_impact", lambda *args, **kwargs: _run([], **kwargs))
    monkeypatch.setattr(impact, "symbol_contract_impact", lambda *args, **kwargs: _run([], **kwargs))
    monkeypatch.setattr(
        impact,
        "signature_change_impact",
        lambda *args, **kwargs: _run([], explode=True, **kwargs),
    )

    item = ChangedFile(path="src/app.py", old_path=None, status="modified")
    sites, degraded = impact._detector_sites(
        Path("."),
        [item],
        old_blobs={"src/app.py": "old"},
        new_blobs={"src/app.py": "new"},
        engine=None,
    )

    assert sites == []
    assert degraded == {"contract_literal_unparsed", "signature_change_impact"}


# --------------------------------------------------------------------------- #
# the managed ast-grep bootstrap, as the detector pool above reaches it        #
# --------------------------------------------------------------------------- #


def _astgrep_archive(payload: bytes) -> bytes:
    """A stand-in for the pinned release zip, carrying *payload* as the binary."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ast-grep", payload)
    return buffer.getvalue()


def _pin_stub_asset(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin a managed asset for whatever platform the suite runs on.

    The bootstrap declines outright on a platform with no pinned release, and the
    races below are about install mechanics, not about which archive is fetched.
    Every download is injected, so the stub's URL and digest are never used.
    """

    from lemoncrow.infra.code_intel.astgrep import binaries

    monkeypatch.setitem(
        binaries._MANAGED_ASSETS,
        binaries._platform_key(),
        binaries.ManagedAstGrepAsset(archive_name="stub.zip", url="https://example.invalid/stub.zip", sha256=""),
    )
    return binaries


def test_concurrent_bootstrap_downloads_once_and_installs_one_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first review in a repo without ast-grep races its own detector pool.

    ``_detector_sites`` submits four detectors at once and three of them reach
    binary discovery together. Unserialised, all three miss the same existence
    check, all three fetch the same ~20MB archive, and all three rewrite the same
    target while the others are reading it.
    """

    binaries = _pin_stub_asset(monkeypatch)

    payload = b"#!/bin/sh\nexit 0\n" + b"x" * 4096
    archive = _astgrep_archive(payload)
    downloads = 0
    counter_guard = threading.Lock()
    together = threading.Barrier(3)
    results: list[Any] = []
    results_guard = threading.Lock()

    def _download(_asset: Any) -> bytes:
        nonlocal downloads
        with counter_guard:
            downloads += 1
        # Hold the install open long enough that any unserialised sibling has
        # certainly passed the existence check by now.
        time.sleep(0.2)
        return archive

    def _bootstrap() -> None:
        together.wait(timeout=5)
        resolution = binaries.bootstrap_managed_astgrep(tmp_path, downloader=_download)
        with results_guard:
            results.append(resolution)

    threads = [threading.Thread(target=_bootstrap) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert downloads == 1, "concurrent detectors must not each pay for the same managed download"
    assert [resolution.available for resolution in results] == [True, True, True]
    installed = {resolution.path for resolution in results}
    assert len(installed) == 1
    target = installed.pop()
    assert target is not None
    assert target.read_bytes() == payload
    assert os.access(target, os.X_OK)


def test_bootstrap_never_exposes_a_half_written_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing may ever observe the managed binary at less than its full size.

    Writing straight to the final path publishes the file the instant it is
    opened, so a concurrent reader can spawn a truncated executable and a failed
    write leaves that wreckage behind for every later run to trust.

    Racing a watcher against a huge payload would only catch that by luck, so the
    proof is the publication itself: the only thing that may ever appear at the
    target is a rename of a file already written in full.
    """

    binaries = _pin_stub_asset(monkeypatch)

    payload = os.urandom(256 * 1024)
    archive = _astgrep_archive(payload)
    target = binaries._managed_install_root(tmp_path.resolve()) / "ast-grep"
    published: list[tuple[int, bool]] = []
    real_replace = os.replace

    def _spy(src: Any, dst: Any, **kwargs: Any) -> None:
        published.append((Path(src).stat().st_size, Path(dst).exists()))
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(binaries.os, "replace", _spy)

    partial_sizes: list[int] = []
    stop = threading.Event()

    def _watch() -> None:
        while not stop.is_set():
            try:
                size = target.stat().st_size
            except OSError:
                size = len(payload)
            if size != len(payload):
                partial_sizes.append(size)
            time.sleep(0.001)

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    try:
        resolution = binaries.bootstrap_managed_astgrep(tmp_path, downloader=lambda _asset: archive)
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert resolution.available
    assert target.read_bytes() == payload
    assert published == [(len(payload), False)], "the binary must appear only as a rename of a complete file"
    assert not partial_sizes, f"target was visible at {sorted(set(partial_sizes))} bytes before it was complete"
    # A temp file must never outlive the install it was written for.
    assert sorted(item.name for item in target.parent.iterdir()) == ["ast-grep"]


def test_a_failing_bootstrap_does_not_serialise_the_detector_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serialising the attempt must not multiply the price of a failure.

    On a machine with no network every detector's bootstrap fails the same way,
    after a full ``urlopen(timeout=60)``. Holding the pool behind one attempt is
    right -- one download, not three -- but a caller queued behind an attempt
    that has already failed has its answer, and re-running it would charge the
    review that timeout once per detector instead of once per review.
    """

    binaries = _pin_stub_asset(monkeypatch)

    delay = 0.3
    attempts = 0
    counter_guard = threading.Lock()
    together = threading.Barrier(3)
    results: list[Any] = []
    results_guard = threading.Lock()

    def _download(_asset: Any) -> bytes:
        nonlocal attempts
        with counter_guard:
            attempts += 1
        time.sleep(delay)
        raise OSError("no network")

    def _bootstrap() -> None:
        together.wait(timeout=5)
        resolution = binaries.bootstrap_managed_astgrep(tmp_path, downloader=_download)
        with results_guard:
            results.append(resolution)

    threads = [threading.Thread(target=_bootstrap) for _ in range(3)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    elapsed = time.monotonic() - started

    assert attempts == 1, "a bootstrap that just failed must not be re-run by everyone queued behind it"
    assert [resolution.available for resolution in results] == [False, False, False]
    assert all("bootstrap failed" in (resolution.reason or "") for resolution in results)
    assert elapsed < 2 * delay, f"three failing bootstraps cost {elapsed:.2f}s where one costs {delay:.2f}s"


def test_a_refusing_probe_never_answers_for_a_caller_that_would_have_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adopting a concurrent failure must not adopt the probe's refusal.

    ``_astgrep_ready`` bootstraps with a downloader that raises instead of
    fetching. A detector queued behind that probe would be told the binary cannot
    be installed, when nobody ever tried -- so only an attempt that ran the same
    fetch may answer for a caller.
    """

    binaries = _pin_stub_asset(monkeypatch)

    payload = b"#!/bin/sh\nexit 0\n"
    archive = _astgrep_archive(payload)
    probing = threading.Event()

    def _refuse(_asset: Any) -> bytes:
        probing.set()
        # Hold the attempt open so the real bootstrap is certainly queued behind
        # it when it fails.
        time.sleep(0.3)
        raise OSError("availability probe does not download")

    probe_result: list[Any] = []
    probe = threading.Thread(
        target=lambda: probe_result.append(binaries.bootstrap_managed_astgrep(tmp_path, downloader=_refuse))
    )
    probe.start()
    assert probing.wait(timeout=5)
    resolution = binaries.bootstrap_managed_astgrep(tmp_path, downloader=lambda _asset: archive)
    probe.join(timeout=5)

    assert [item.available for item in probe_result] == [False]
    assert resolution.available, "a probe's refusal is not an answer for a real bootstrap"
    assert resolution.path is not None
    assert resolution.path.read_bytes() == payload


def test_an_abandoned_bootstrap_lockfile_is_reclaimed_not_waited_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `kill -9` leaves a lockfile behind; the next bootstrap must clear it.

    The waiter polls for the reclaim, so the wait budget has to be the larger of
    the two constants. Below the stale threshold the poll can never reach the age
    that would free the lock: every later bootstrap in the checkout burns the
    whole budget and then steps around the dead lockfile, leaving it for the next
    one to burn the budget on too.
    """

    binaries = _pin_stub_asset(monkeypatch)

    assert binaries._BOOTSTRAP_LOCK_WAIT_SECONDS > binaries._BOOTSTRAP_LOCK_STALE_SECONDS

    payload = b"#!/bin/sh\nexit 0\n"
    archive = _astgrep_archive(payload)
    target = binaries._managed_install_root(tmp_path.resolve()) / "ast-grep"
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name("ast-grep.lock")
    lock_path.write_text("")
    orphaned_at = time.time() - 5.0
    os.utime(lock_path, (orphaned_at, orphaned_at))

    monkeypatch.setattr(binaries, "_BOOTSTRAP_LOCK_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(binaries, "_BOOTSTRAP_LOCK_STALE_SECONDS", 1.0)

    started = time.monotonic()
    resolution = binaries.bootstrap_managed_astgrep(tmp_path, downloader=lambda _asset: archive)
    elapsed = time.monotonic() - started

    assert resolution.available
    assert target.read_bytes() == payload
    assert not lock_path.exists(), "an abandoned lockfile must be reclaimed, not stepped around"
    assert elapsed < 1.0, f"waited {elapsed:.2f}s for a lockfile that no live holder could own"


def test_a_stale_lock_that_cannot_be_removed_gives_up_instead_of_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclaiming a stale lock can fail, and the retry after it must not busy-loop.

    The reclaim path retried the O_EXCL create the instant ``unlink()`` raised.
    Nothing about that failure changes between attempts -- the create raises
    FileExistsError again, the age check calls the lock abandoned again, the
    unlink fails again -- so the loop ran flat out on one core for the whole wait
    budget, on every bootstrap in the checkout. A lock that cannot be taken is not
    worth a CPU: give it up and install unlocked, which is safe because publishing
    is an ``os.replace()`` of a fully written temp file.
    """

    binaries = _pin_stub_asset(monkeypatch)

    payload = b"#!/bin/sh\nexit 0\n"
    archive = _astgrep_archive(payload)
    target = binaries._managed_install_root(tmp_path.resolve()) / "ast-grep"
    target.parent.mkdir(parents=True, exist_ok=True)
    # A directory at the lock path is the unreclaimable lock with no patching: it
    # is stat-able (so the age check calls it abandoned), O_EXCL still refuses to
    # create over it, and ``unlink()`` on it fails however often it is retried --
    # the same shape as a read-only or sticky install root.
    lock_path = target.with_name("ast-grep.lock")
    lock_path.mkdir()
    orphaned_at = time.time() - 5.0
    os.utime(lock_path, (orphaned_at, orphaned_at))

    monkeypatch.setattr(binaries, "_BOOTSTRAP_LOCK_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(binaries, "_BOOTSTRAP_LOCK_STALE_SECONDS", 1.0)

    started = time.monotonic()
    cpu_before = time.process_time()
    resolution = binaries.bootstrap_managed_astgrep(tmp_path, downloader=lambda _asset: archive)
    elapsed = time.monotonic() - started
    cpu = time.process_time() - cpu_before

    assert resolution.available, "a lock that cannot be taken must not stop the install"
    assert target.read_bytes() == payload
    assert lock_path.is_dir(), "nothing here can remove it; reporting otherwise would be a lie"
    assert elapsed < 1.0, f"waited {elapsed:.2f}s on a lock that could never be reclaimed"
    assert cpu < 0.5, f"burned {cpu:.2f}s of CPU spinning on a lock that could never be reclaimed"


def test_the_astgrep_availability_probe_leaves_the_checkout_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every review probes ast-grep, and a probe is a read.

    ``_astgrep_ready`` asks the bootstrap whether the managed binary is already
    installed, with a downloader that refuses to fetch. Creating the managed
    install tree -- or a lockfile inside it -- on the way to that refusal leaves
    a working copy dirty after a read-only review command.
    """

    from lemoncrow.pro.capabilities.review.impact import _astgrep_ready

    _pin_stub_asset(monkeypatch)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.delenv("LEMONCROW_AST_GREP_BIN", raising=False)
    monkeypatch.setenv("PATH", str(empty_bin))

    assert _astgrep_ready(repo_root) is False
    assert list(repo_root.rglob("*")) == [], "an availability probe must not create anything"


def test_the_installed_binary_keeps_the_permissions_the_umask_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that sets umask 0o077 means it, including for a downloaded binary.

    Publishing a fixed 0o755 makes the managed ast-grep group- and other-readable
    on exactly the hardened multi-user hosts whose umask said it should not be.
    """

    binaries = _pin_stub_asset(monkeypatch)
    archive = _astgrep_archive(b"#!/bin/sh\nexit 0\n")

    modes: dict[int, int] = {}
    for umask in (0o077, 0o022):
        root = tmp_path / f"umask-{umask:03o}"
        root.mkdir()
        previous = os.umask(umask)
        try:
            resolution = binaries.bootstrap_managed_astgrep(root, downloader=lambda _asset: archive)
        finally:
            os.umask(previous)
        assert resolution.available
        assert resolution.path is not None
        modes[umask] = resolution.path.stat().st_mode & 0o777

    assert modes == {0o077: 0o711, 0o022: 0o755}


def test_deleted_file_symbols_resolved_from_base_side(tmp_path: Path) -> None:
    root = tmp_path / "deleted"
    repo = _init_repo(root)
    _write(root, "src/keep.py", "def keep():\n    return 1\n")
    _write(root, "src/gone.py", "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
    _commit(repo, "base", 0)
    (root / "src" / "gone.py").unlink()
    _commit(repo, "drop module", 60)

    result = _impact(root)

    deleted = {symbol.symbol_name for symbol in result.symbols if symbol.change == "deleted"}
    assert {"alpha", "beta"} <= deleted
    for name in ("alpha", "beta"):
        symbol = _symbol(result, name)
        assert symbol is not None
        assert symbol.file_path == "src/gone.py"
        assert symbol.source == "tree_sitter"
        assert symbol.kind == "function"


def test_modified_file_without_pure_deletion_does_not_parse_old_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import impact

    item = ChangedFile(
        path="src/app.py",
        old_path=None,
        status="modified",
        hunks=(
            DiffHunk(
                old_start=1,
                old_lines=1,
                new_start=1,
                new_lines=1,
                header="@@ -1 +1 @@",
                old_ranges=((1, 1),),
                new_ranges=((1, 1),),
            ),
        ),
    )

    def _unexpected_parse(*_args: Any, **_kwargs: Any) -> tuple[()]:
        raise AssertionError("old-side definitions must not be parsed without a pure deletion")

    monkeypatch.setattr(impact, "_definition_tags", _unexpected_parse)
    assert impact._deleted_symbols(item, "def old():\n    return 1\n") == []


def test_changed_symbols_carry_centrality_when_indexed(tmp_path: Path) -> None:
    root = tmp_path / "indexed"
    repo = _init_repo(root)
    _write(root, "src/core.py", "def helper(value):\n    return value\n")
    _write(
        root,
        "src/caller.py",
        "from src.core import helper\n\n\ndef run(value):\n    return helper(value)\n",
    )
    _commit(repo, "base", 0)
    _write(root, "src/core.py", "def helper(value):\n    return value + 1\n")
    _commit(repo, "tweak helper", 60)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    CodeContextEngine(root, autosync_enabled=False).index_repo(force=True)

    result = _impact(root)

    assert result.index_status == "fresh"
    symbol = _symbol(result, "helper")
    assert symbol is not None
    assert symbol.source == "index"
    assert symbol.caller_count >= 1
    assert symbol.usage_count >= 1
    assert symbol.centrality_rank is not None
    assert symbol.centrality_percentile is not None
    assert 0.0 <= symbol.centrality_percentile <= 1.0
    assert "centrality" not in result.degraded
    assert "symbol_relations" not in result.degraded
    assert "symbol_line_ranges" not in result.degraded


def test_index_that_never_saw_the_file_reports_stale_not_fresh(tmp_path: Path) -> None:
    """A working-tree review runs against an index built at HEAD: that is drift.

    The index still answers -- drifted line ranges beat no ranges -- so the packet
    keeps using it and says ``stale`` plus ``symbol_line_ranges`` rather than
    silently presenting a HEAD-shaped answer as current.
    """

    root = tmp_path / "stale"
    repo = _init_repo(root)
    _write(root, "src/core.py", "def helper(value):\n    return value\n")
    _commit(repo, "base", 0)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    CodeContextEngine(root, autosync_enabled=False).index_repo(force=True)
    _write(root, "src/fresh.py", "def brand_new():\n    return helper(1)\n")

    result = _impact(root, rev=None)

    assert result.index_status == "stale"
    assert "symbol_line_ranges" in result.degraded
    symbol = _symbol(result, "brand_new")
    assert symbol is not None
    assert symbol.source == "tree_sitter"


def test_untouched_caller_sites_name_files_outside_the_patch(tmp_path: Path) -> None:
    root = tmp_path / "fanout"
    repo = _init_repo(root)
    _write(root, "src/core.py", "def helper(value):\n    return value\n")
    _write(
        root,
        "src/caller.py",
        "from src.core import helper\n\n\ndef run(value):\n    return helper(value)\n",
    )
    _commit(repo, "base", 0)
    _write(root, "src/core.py", "def helper(value):\n    return value + 1\n")
    _commit(repo, "tweak helper", 60)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    CodeContextEngine(root, autosync_enabled=False).index_repo(force=True)

    result = _impact(root)

    sites = _sites(result, "untouched_caller")
    assert sites, f"expected an untouched_caller site, got {result.sites!r}"
    assert all(site.in_patch is False for site in sites)
    assert any(site.path.startswith("src/caller.py") for site in sites)
    assert all(site.old == "helper" for site in sites)


def test_untouched_callers_stay_within_the_changed_language(tmp_path: Path) -> None:
    """A same-named symbol in another language is not a caller (risk R5).

    The index keeps one flat symbol namespace and the caller lookup folds in
    cross-language references, so a shared name would otherwise manufacture a
    broken-caller claim across a language boundary.
    """

    root = tmp_path / "crosslang"
    repo = _init_repo(root)
    _write(root, "src/core.py", "def release(value):\n    return value\n")
    _write(root, "web/app.ts", "export function release(value: number) {\n  return value;\n}\n")
    _write(root, "web/use.ts", "import { release } from './app';\n\nexport const go = () => release(1);\n")
    _commit(repo, "base", 0)
    _write(root, "src/core.py", "def release(value):\n    return value + 1\n")
    _commit(repo, "tweak release", 60)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    CodeContextEngine(root, autosync_enabled=False).index_repo(force=True)

    result = _impact(root)

    assert all(not site.path.endswith(".ts") for site in _sites(result, "untouched_caller"))


def test_docs_only_change_reports_no_symbols(tmp_path: Path) -> None:
    """A markdown heading is a symbol to the index, but not to a code reviewer."""

    root = tmp_path / "docs"
    repo = _init_repo(root)
    _write(root, "docs/guide.md", "# Guide\n\nintro\n")
    _commit(repo, "base", 0)
    _write(root, "docs/guide.md", "# Guide\n\nintro\n\n## Details\n\nmore\n")
    _commit(repo, "expand guide", 60)

    result = _impact(root)

    assert result.symbols == ()


def test_binary_only_diff_returns_empty_result(tmp_path: Path) -> None:
    root = tmp_path / "binary"
    repo = _init_repo(root)
    _write(root, "README.md", "seed\n")
    _commit(repo, "base", 0)
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
    _commit(repo, "add binary", 60)

    result = _impact(root)

    assert result.symbols == ()
    assert result.sites == ()
    assert result.index_status == "absent"
    assert result.degraded == ()


def test_site_cap_is_deterministic(tmp_path: Path) -> None:
    root = _signature_change_repo(tmp_path)

    first = _impact(root, limit_sites=1)
    second = _impact(root, limit_sites=1)

    assert len(first.sites) <= 1
    assert first.sites == second.sites


# --- caller attribution: the two guards a real run forced ---------------------


class _StubEngine:
    """A `tool_callers` stand-in: the graph queries are the only thing under test."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def tool_callers(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return self.payload


def _changed_symbol(name: str = "serve", file_path: str = "src/app.py") -> ChangedSymbol:
    return ChangedSymbol(
        symbol_name=name,
        qualified_name=name,
        kind="function",
        file_path=file_path,
        start_line=1,
        end_line=5,
        change="modified",
        caller_count=7,
        source="index",
    )


def _callers_payload(
    target_path: str,
    merged: int | None = None,
    caller_path: str = "other/consumer.py",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "target": {"name": "serve", "path": target_path},
        "related": [{"path": caller_path, "line": 12, "qualified_name": "boot"}],
    }
    if merged is not None:
        payload["ambiguity"] = {"merged_target_count": merged, "note": "merged for callers"}
    return payload


def test_unambiguous_symbol_yields_caller_sites_carrying_their_source(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.impact import _untouched_caller_sites

    engine = _StubEngine(_callers_payload("src/app.py"))
    sites, degraded = _untouched_caller_sites(engine, [_changed_symbol()], frozenset({"src/app.py"}), limit=10)

    assert [site.path for site in sites] == ["other/consumer.py:L12"]
    # No revision to check the geometry against, so nothing is claimed about it.
    assert degraded == set()
    assert sites[0].kind == "untouched_caller"
    assert sites[0].source_path == "src/app.py"
    assert sites[0].in_patch is False


def test_ambiguous_symbol_names_are_never_expanded_into_callers(tmp_path: Path) -> None:
    """Measured on this repo: `serve` merged 20 same-named definitions.

    The engine answers such a query with the union of every same-named symbol's
    callers, so attributing them to the definition this diff touched invents
    fourteen findings out of four real ones.
    """

    from lemoncrow.pro.capabilities.review.impact import _untouched_caller_sites

    engine = _StubEngine(_callers_payload("src/app.py", merged=20))
    sites, _degraded = _untouched_caller_sites(engine, [_changed_symbol()], frozenset({"src/app.py"}), limit=10)

    assert sites == []
    assert engine.calls == 1, "the guard reads the answer, it does not skip the query"


def test_callers_of_a_same_named_symbol_in_another_file_are_dropped(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.impact import _untouched_caller_sites

    engine = _StubEngine(_callers_payload("vendor/other.py"))
    sites, _degraded = _untouched_caller_sites(engine, [_changed_symbol()], frozenset({"src/app.py"}), limit=10)

    assert sites == []


def test_the_reviewed_revision_and_not_the_index_decides_where_a_site_points() -> None:
    """The three answers ``exists_in_head`` may give, and what each costs the packet.

    The index's ``line`` is a workspace coordinate. Printed beside a path in a
    reviewed commit range it is a location the reviewer opens and does not find,
    so the revision under review is asked where the site actually is: ``None`` to
    drop it, ``0`` for "real, but nothing here knows where", a line otherwise.
    Any answer that is not the index's own is a fact about the index, and the
    packet reports it rather than quietly preferring one of the two numbers.
    """

    from lemoncrow.pro.capabilities.review.impact import _untouched_caller_sites

    def _sites(answer: int | None) -> tuple[list[Any], set[str]]:
        return _untouched_caller_sites(
            _StubEngine(_callers_payload("src/app.py")),
            [_changed_symbol()],
            frozenset({"src/app.py"}),
            limit=10,
            exists_in_head=lambda _path, _symbol, _caller: answer,
        )

    # Agreement with the index: the line stands and nothing is degraded.
    sites, degraded = _sites(12)
    assert [site.path for site in sites] == ["other/consumer.py:L12"]
    assert degraded == set()

    # Disagreement: the reviewed revision wins, and the footer is told why.
    sites, degraded = _sites(4)
    assert [site.path for site in sites] == ["other/consumer.py:L4"]
    assert degraded == {"caller_line_ranges"}

    # Unlocatable: the site survives without a line rather than borrowing one.
    sites, degraded = _sites(0)
    assert [site.path for site in sites] == ["other/consumer.py"]
    assert degraded == {"caller_line_ranges"}

    # Absent from the reviewed revision: no site at all.
    sites, degraded = _sites(None)
    assert sites == []


def test_module_private_names_do_not_take_callers_from_other_packages() -> None:
    """The measured defect, with the index's own numbers: ``_chip``.

    ``_chip`` is defined four times in this repo. ``tool_callers`` answers a
    query for it with **one** target in ``review/html.py``, **no** ``ambiguity``
    block at all, and four callers -- three of which belong to the other three
    ``_chip``s. Both existing guards read the payload's own account of itself and
    so both pass, and three fabricated "untouched callers" reach the packet as
    fact. A leading underscore is Python's module-private marker: a caller
    outside the defining package is not this definition's caller, whatever the
    index says.
    """

    from lemoncrow.pro.capabilities.review.impact import _untouched_caller_sites

    payload: dict[str, Any] = {
        "target": {"name": "_chip", "path": "src/pro/review/html.py"},
        "related": [
            {"path": "src/gateway/cli/commands/audit.py", "line": 420, "qualified_name": "_render_audit_rich"},
            {"path": "src/gateway/cli/commands/project.py", "line": 490, "qualified_name": "_render"},
            {"path": "src/gateway/cli/commands/sessions.py", "line": 692, "qualified_name": "_render_stats_rich"},
        ],
    }
    engine = _StubEngine(payload)
    symbol = _changed_symbol("_chip", "src/pro/review/html.py")

    sites, _degraded = _untouched_caller_sites(engine, [symbol], frozenset({"src/pro/review/html.py"}), limit=10)

    assert sites == []
    assert engine.calls == 1, "the guard reads the answer, it does not skip the query"


def test_module_private_callers_inside_the_same_package_survive() -> None:
    """``from ._shared import _emit`` is ordinary, so a sibling module is a real caller.

    The gate has to be package-private, not file-private: making it stricter
    would drop every genuine private helper shared across one package.
    """

    from lemoncrow.pro.capabilities.review.impact import _untouched_caller_sites

    payload: dict[str, Any] = {
        "target": {"name": "_emit", "path": "src/pro/review/_shared.py"},
        "related": [{"path": "src/pro/review/render.py", "line": 31, "qualified_name": "render"}],
    }
    sites, _degraded = _untouched_caller_sites(
        _StubEngine(payload),
        [_changed_symbol("_emit", "src/pro/review/_shared.py")],
        frozenset({"src/pro/review/_shared.py"}),
        limit=10,
    )

    assert [site.path for site in sites] == ["src/pro/review/render.py:L31"]


def test_private_scope_gate_is_pythons_rule_only() -> None:
    """Dunder names are public protocol, and other languages get their own rules."""

    from lemoncrow.pro.capabilities.review.impact import _out_of_private_scope

    assert _out_of_private_scope("_chip", "a/html.py", "b/audit.py") is True
    assert _out_of_private_scope("_chip", "a/html.py", "a/other.py") is False
    assert _out_of_private_scope("_chip", "a/html.py", "a/html.py") is False
    assert _out_of_private_scope("__mangled", "a/html.py", "a/other.py") is True
    assert _out_of_private_scope("__init__", "a/html.py", "b/other.py") is False
    assert _out_of_private_scope("chip", "a/html.py", "b/audit.py") is False
    assert _out_of_private_scope("_chip", "a/app.ts", "b/other.ts") is False


def test_unreported_merge_of_private_namesakes_blanks_the_counts() -> None:
    """The index reporting no ambiguity is not evidence that there is none.

    ``_merged_target_count`` returns 1 when the payload carries no ``ambiguity``
    block, which is exactly what a merged private-name query looks like. The
    counts must be dropped and the packet must say it is under-claiming.
    """

    from lemoncrow.pro.capabilities.review.impact import _enrich

    engine = _EnrichStubEngine(
        callers=4,
        usages=4,
        resolutions={"_chip": _callers_payload("src/pro/review/html.py", caller_path="src/gateway/audit.py")},
    )
    symbols, degraded, _ = _enrich(engine, [_changed_symbol("_chip", "src/pro/review/html.py")])

    assert symbols[0].caller_count == 0
    assert symbols[0].usage_count == 0
    assert symbols[0].centrality_rank is None
    assert "ambiguous_symbol_counts" in degraded


def test_detector_sites_name_the_changed_file_they_came_from(tmp_path: Path) -> None:
    result = _impact(_signature_change_repo(tmp_path))

    sites = _sites(result, "signature_change")
    assert sites
    assert all(site.source_path == "src/auth.py" for site in sites)


def test_prose_mentioning_a_symbol_does_not_steal_its_attribution(tmp_path: Path) -> None:
    """A README that names the function is not where the contract lives."""

    root = tmp_path / "with_docs"
    repo = _init_repo(root)
    _write(root, "src/auth.py", "def refresh(user):\n    return user\n")
    _write(root, "jobs/cleanup.py", "from src.auth import refresh\n\n\ndef sweep(u):\n    return refresh(u)\n")
    _write(root, "README.md", "# app\n")
    _commit(repo, "base", 0)
    _write(root, "src/auth.py", "def refresh(user, context):\n    return (user, context)\n")
    _write(root, "README.md", "# app\n\nrefresh now takes a context.\n")
    _commit(repo, "require context", 60)

    result = _impact(root)

    sites = _sites(result, "signature_change")
    assert sites
    assert all(site.source_path == "src/auth.py" for site in sites)


# --- count attribution: a namesake's callers are not this symbol's ------------


def test_symbol_does_not_inherit_a_namesakes_callers(tmp_path: Path) -> None:
    """Two `helper`s, one changed: the changed one must not borrow the other's fan-out.

    The index is a single flat namespace and both ``badge_counts_batch`` and
    ``call_graph_centrality`` are keyed on the bare name, so an unqualified
    lookup hands the untouched, uncalled ``src/core.py::helper`` the two callers
    that belong to ``lib/helpers.py::helper``. The raw-count assertion below is
    the control: it shows the borrowed number really is sitting there in the
    index, waiting to be misattributed.
    """

    root = tmp_path / "namesake"
    repo = _init_repo(root)
    _write(root, "src/core.py", "def helper(value):\n    return value\n")
    _write(root, "lib/helpers.py", "def helper(value):\n    return value * 2\n")
    _write(root, "lib/a.py", "from lib.helpers import helper\n\n\ndef a():\n    return helper(1)\n")
    _write(root, "lib/b.py", "from lib.helpers import helper\n\n\ndef b():\n    return helper(2)\n")
    _commit(repo, "base", 0)
    _write(root, "src/core.py", "def helper(value):\n    return value + 1\n")
    _commit(repo, "tweak the uncalled helper", 60)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    engine = CodeContextEngine(root, autosync_enabled=False)
    engine.index_repo(force=True)
    assert engine.badge_counts_batch(["helper"])["helper"]["callers"] > 0, "fixture must have a namesake to borrow from"

    result = _impact(root)

    symbol = _symbol(result, "helper")
    assert symbol is not None
    assert symbol.file_path == "src/core.py"
    assert symbol.source == "index"
    assert symbol.caller_count == 0
    assert symbol.usage_count == 0
    assert symbol.centrality_rank is None
    assert symbol.centrality_percentile is None
    assert "ambiguous_symbol_counts" in result.degraded
    assert not [site for site in _sites(result, "untouched_caller") if site.old == "helper"]


class _EnrichStubEngine:
    """The three engine calls enrichment makes, with scripted answers."""

    def __init__(self, *, callers: int, usages: int, resolutions: dict[str, dict[str, Any]]) -> None:
        self.callers = callers
        self.usages = usages
        self.resolutions = resolutions
        self.resolved: list[str] = []

    def badge_counts_batch(self, symbol_names: list[str]) -> dict[str, dict[str, int]]:
        return {name: {"callers": self.callers, "callees": 0, "usages": self.usages} for name in symbol_names}

    def call_graph_centrality(self, **_kwargs: Any) -> dict[str, Any]:
        return {"node_count": 1000, "ranking": [{"symbol": "_invoke"}]}

    def tool_callers(self, *, symbol_name: str, **_kwargs: Any) -> dict[str, Any]:
        self.resolved.append(symbol_name)
        return self.resolutions[symbol_name]


class _BatchEnrichStubEngine(_EnrichStubEngine):
    def __init__(self, *, callers: int, usages: int, resolutions: dict[str, dict[str, Any]]) -> None:
        super().__init__(callers=callers, usages=usages, resolutions=resolutions)
        self.batches: list[list[str]] = []

    def tool_callers_batch(self, symbol_names: list[str], **_kwargs: Any) -> dict[str, dict[str, Any]]:
        self.batches.append(list(symbol_names))
        return {name: self.resolutions[name] for name in symbol_names if name in self.resolutions}

    def tool_callers(self, *, symbol_name: str, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"scalar caller resolution should not run for {symbol_name}")


def test_enrichment_batches_name_qualification_when_the_engine_supports_it() -> None:
    from lemoncrow.pro.capabilities.review.impact import _enrich

    engine = _BatchEnrichStubEngine(
        callers=6,
        usages=9,
        resolutions={
            "_invoke": _callers_payload("tests/gateway/test_cli_review.py", caller_path="tests/gateway/helper.py"),
            "serve": _callers_payload("src/app.py"),
        },
    )
    symbols, degraded, resolutions = _enrich(
        engine,
        [
            _changed_symbol("_invoke", "tests/gateway/test_cli_review.py"),
            _changed_symbol("serve", "src/app.py"),
        ],
    )

    assert engine.batches == [["_invoke", "serve"]]
    assert set(resolutions) == {"_invoke", "serve"}
    assert all(symbol.caller_count == 6 for symbol in symbols)
    assert "symbol_relations" not in degraded


def test_unattributable_counts_are_zeroed_rather_than_borrowed() -> None:
    """The measured defect: a test helper reading back 174 borrowed callers."""

    from lemoncrow.pro.capabilities.review.impact import _enrich

    engine = _EnrichStubEngine(
        callers=174,
        usages=210,
        resolutions={"_invoke": _callers_payload("tests/gateway/test_cli.py", merged=19)},
    )
    symbols, degraded, resolutions = _enrich(engine, [_changed_symbol("_invoke", "tests/gateway/test_cli_review.py")])

    assert symbols[0].caller_count == 0
    assert symbols[0].usage_count == 0
    assert symbols[0].centrality_rank is None
    assert symbols[0].centrality_percentile is None
    assert "ambiguous_symbol_counts" in degraded
    assert engine.resolved == ["_invoke"], "the count is qualified by one query, not guessed"
    assert "_invoke" in resolutions, "the answer is handed on so the caller-site pass does not repeat it"


def test_attributable_counts_survive_qualification() -> None:
    from lemoncrow.pro.capabilities.review.impact import _enrich

    engine = _EnrichStubEngine(
        callers=6,
        usages=9,
        # The caller is a sibling module: `_invoke` is module-private, so a
        # caller anywhere else would be a namesake's, not this definition's.
        resolutions={
            "_invoke": _callers_payload(
                "tests/gateway/test_cli_review.py",
                caller_path="tests/gateway/test_cli_review_helpers.py",
            )
        },
    )
    symbols, degraded, _ = _enrich(engine, [_changed_symbol("_invoke", "tests/gateway/test_cli_review.py")])

    assert symbols[0].caller_count == 6
    assert symbols[0].usage_count == 9
    assert symbols[0].centrality_rank == 1
    assert symbols[0].centrality_percentile == 1.0
    assert "ambiguous_symbol_counts" not in degraded


def test_resolution_budget_exhaustion_reports_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget that runs out drops the claim; it never falls back to the raw count.

    A symbol the budget never reached is ``-1`` ("the index was not asked"), not
    ``0`` ("the index was asked and pointed elsewhere"), and the degraded reason
    says the resolution pass was truncated rather than that the counts were
    ambiguous -- nothing about this symbol was ambiguous, we simply stopped.
    """

    from lemoncrow.pro.capabilities.review import impact as impact_module

    monkeypatch.setattr(impact_module, "_MAX_RESOLVE_QUERIES", 0)
    engine = _EnrichStubEngine(
        callers=6,
        usages=9,
        resolutions={"_invoke": _callers_payload("tests/gateway/test_cli_review.py")},
    )
    symbols, degraded, _ = impact_module._enrich(
        engine, [_changed_symbol("_invoke", "tests/gateway/test_cli_review.py")]
    )

    assert engine.resolved == []
    assert symbols[0].caller_count == -1
    assert symbols[0].usage_count == -1
    assert symbols[0].centrality_rank is None
    assert impact_module._SIGNAL_RESOLUTION_TRUNCATED in degraded


def test_missing_index_counts_stay_unknown_rather_than_becoming_zero() -> None:
    """-1 ("nothing answered") and 0 ("answered, not this symbol's") stay distinct."""

    from lemoncrow.pro.capabilities.review.impact import _enrich

    class _Broken(_EnrichStubEngine):
        def badge_counts_batch(self, symbol_names: list[str]) -> dict[str, dict[str, int]]:
            raise RuntimeError("no relations table")

    engine = _Broken(callers=0, usages=0, resolutions={})
    symbols, degraded, _ = _enrich(engine, [_changed_symbol("_invoke", "tests/gateway/test_cli_review.py")])

    assert symbols[0].caller_count == -1
    assert symbols[0].usage_count == -1
    assert "symbol_relations" in degraded


def test_caller_sites_reuse_the_resolution_enrichment_already_paid_for() -> None:
    from lemoncrow.pro.capabilities.review.impact import _Resolution, _untouched_caller_sites

    payload = _callers_payload("src/app.py")
    engine = _StubEngine(payload)
    cached = {"serve": _Resolution(payload=payload, merged_targets=1, definition_path="src/app.py")}
    sites, _degraded = _untouched_caller_sites(
        engine,
        [_changed_symbol()],
        frozenset({"src/app.py"}),
        limit=10,
        resolutions=cached,
    )

    assert engine.calls == 0, "the shared cache is what makes one query serve both passes"
    assert [site.path for site in sites] == ["other/consumer.py:L12"]


# --- what counts as a definition: the fabrication a real run produced ---------


_VITEST_SUITE = """import { afterEach, describe, expect, it } from "vitest";

import { fetchOverview } from "./reviewApi";

export function navigateTo(url: string): void {
  window.history.replaceState({}, "", url);
}

afterEach(() => {
  fetchOverview();
});
"""

_TSX_TERNARY = """export function Shell({ bare }: { bare: boolean }) {
  return (
    <div
      className={
        bare
          ? "min-h-full font-mono text-neutral-200"
          : "min-h-full bg-gradient-to-b font-mono"
      }
    >
      {bare}
    </div>
  );
}
"""


def _ts_repo(tmp_path: Path, name: str, rel: str, first: str, second: str) -> Path:
    root = tmp_path / name
    repo = _init_repo(root)
    _write(root, rel, first)
    _commit(repo, "base", 0)
    _write(root, rel, second)
    _commit(repo, "edit", 60)
    return root


def test_an_imported_binding_is_never_a_changed_symbol(tmp_path: Path) -> None:
    """`afterEach`, imported from vitest, ranked #2 in this repo's own ATTENTION.

    The tag extractor calls an `import_statement` a "definition" because it does
    introduce a name. That name then keyed the bare-name caller and centrality
    lookups, and the index answered with every other file's import of the same
    vitest helper: twelve "known callers" and six "untouched impacted sites" for
    a brand-new test file that nothing in the repository calls.
    """

    root = _ts_repo(tmp_path, "vitest", "src/reviewApi.test.ts", "export const seed = 1;\n", _VITEST_SUITE)

    result = _impact(root)

    names = {symbol.symbol_name for symbol in result.symbols}
    assert "afterEach" not in names, "an import binding is a use of someone else's definition"
    assert "describe" not in names
    assert "fetchOverview" not in names
    assert "navigateTo" in names, "the file's own function is still a changed symbol"
    assert "non_definition_symbols" in result.degraded


def test_a_string_literal_is_never_a_changed_symbol(tmp_path: Path) -> None:
    """`.tsx` is parsed with the TypeScript grammar, which has no JSX.

    The ternary inside `className` is therefore reinterpreted as type syntax and
    the *string literal* is tagged as a `property_signature` whose name is
    `"min-h-full font-mono text-neutral-200"` -- a legitimate node kind carrying
    something that is not a name at all.
    """

    root = _ts_repo(tmp_path, "tsx", "src/App.tsx", "export const seed = 1;\n", _TSX_TERNARY)

    result = _impact(root)

    for symbol in result.symbols:
        assert '"' not in symbol.symbol_name, f"string literal reported as a symbol: {symbol.symbol_name!r}"
        assert " " not in symbol.symbol_name
    assert "non_definition_symbols" in result.degraded


def test_a_json_key_is_never_a_changed_symbol(tmp_path: Path) -> None:
    root = _ts_repo(
        tmp_path,
        "json",
        "package.json",
        '{\n  "name": "app"\n}\n',
        '{\n  "name": "app",\n  "dependencies": {\n    "react": "19.0.0"\n  }\n}\n',
    )

    result = _impact(root)

    assert {symbol.symbol_name for symbol in result.symbols} == set()


def test_definition_kinds_partition_every_language_the_index_covers(tmp_path: Path) -> None:
    """No node kind may be silently neither -- adding a language must fail here.

    The gate is an allowlist, so an unclassified kind under-claims rather than
    fabricating. This test is what makes that a decision instead of an accident.
    """

    from lemoncrow.pro.capabilities.review.impact import _DEFINITION_NODE_KINDS, _NON_DEFINITION_NODE_KINDS
    from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import (
        SUPPORTED_LANGUAGES,
        definition_node_kinds,
    )

    universe: set[str] = set()
    for language in SUPPORTED_LANGUAGES:
        universe |= set(definition_node_kinds(language))

    assert universe - _DEFINITION_NODE_KINDS - _NON_DEFINITION_NODE_KINDS == set()
    assert _DEFINITION_NODE_KINDS & _NON_DEFINITION_NODE_KINDS == set()
    # The four the finding names, whatever language produces them.
    for kind in ("import_statement", "pair", "block_mapping_pair", "call"):
        assert kind in _NON_DEFINITION_NODE_KINDS


def test_a_call_expression_is_not_a_definition() -> None:
    from lemoncrow.infra.tree_sitter.tags import Tag
    from lemoncrow.pro.capabilities.review.impact import _is_definition_tag

    def _tag(name: str, node_kind: str | None) -> Tag:
        return Tag(name, "definition", "a.rb", 1, (0, 1), node_kind=node_kind)

    assert _is_definition_tag(_tag("attr_accessor", "call")) is False
    assert _is_definition_tag(_tag("afterEach", "import_statement")) is False
    assert _is_definition_tag(_tag("dependencies", "pair")) is False
    assert _is_definition_tag(_tag('"min-h-full font-mono"', "property_signature")) is False
    assert _is_definition_tag(_tag("workspace…", "property_signature")) is False
    assert _is_definition_tag(_tag("Shell", "function_declaration")) is True
    assert _is_definition_tag(_tag("$el", "lexical_declaration")) is True
    # Python and the regex fallback carry no node kind and emit only declarations.
    assert _is_definition_tag(_tag("build_review_packet", None)) is True


# --- which repository a caller lives in --------------------------------------


def test_a_submodules_file_is_never_cited_as_a_caller(tmp_path: Path) -> None:
    """`landing/` and `lemoncode/` are submodules; this change cannot reach them.

    The index is keyed to the *workspace*, which contains whatever is checked out
    under it. `head_path_filter` does not catch this in working-tree mode: the
    filesystem is the authority there and the submodule is right on disk.
    """

    from lemoncrow.pro.capabilities.review.impact import _foreign_path_filter, _untouched_caller_sites

    root = tmp_path / "outer"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "landing" / "functions").mkdir(parents=True)
    # A submodule's checkout carries a `.git` *file* pointing at the superproject.
    (root / "landing" / ".git").write_text("gitdir: ../.git/modules/landing\n", encoding="utf-8")

    engine = _StubEngine(_callers_payload("src/app.py", caller_path="landing/functions/api/license.py"))
    sites, degraded = _untouched_caller_sites(
        engine,
        [_changed_symbol()],
        frozenset({"src/app.py"}),
        limit=10,
        is_foreign=_foreign_path_filter(root),
    )

    assert sites == []
    assert "cross_repository_sites" in degraded


def test_callers_inside_this_repository_survive_the_foreign_gate(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.impact import _foreign_path_filter, _untouched_caller_sites

    root = tmp_path / "outer"
    (root / "other").mkdir(parents=True)
    (root / ".git").mkdir()

    engine = _StubEngine(_callers_payload("src/app.py"))
    sites, degraded = _untouched_caller_sites(
        engine,
        [_changed_symbol()],
        frozenset({"src/app.py"}),
        limit=10,
        is_foreign=_foreign_path_filter(root),
    )

    assert [site.path for site in sites] == ["other/consumer.py:L12"]
    assert degraded == set()


def test_foreign_paths_are_anything_outside_this_repositorys_tree(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.impact import _foreign_path_filter

    root = tmp_path / "outer"
    (root / "vendor" / "dep").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "vendor" / "dep" / ".git").mkdir()

    is_foreign = _foreign_path_filter(root)

    assert is_foreign("src/app.py") is False
    assert is_foreign("src/app.py:L12") is False
    assert is_foreign("vendor/dep/index.js:L4") is True
    assert is_foreign("/etc/passwd") is True
    assert is_foreign("../sibling/app.py") is True
    # The repository root's own .git is not a foreign marker.
    assert is_foreign("app.py") is False


# --- did this definition actually change? ------------------------------------


def test_python_parse_once_keeps_legacy_definition_set_and_body_windows() -> None:
    from lemoncrow.infra.tree_sitter.tags import extract_tags_from_text
    from lemoncrow.pro.capabilities.review.impact import _definition_analysis, _is_definition_tag, symbol_windows

    text = (
        "CONST = (\n"
        "    1\n"
        ")\n"
        "typed: int = 2\n"
        "def outer(value):\n"
        "    local = value\n"
        "    def inner():\n"
        "        return local\n"
        "    return inner()\n"
        "class Box:\n"
        "    def method(self):\n"
        "        return 1\n"
    )
    legacy = {
        (tag.name, tag.line)
        for tag in extract_tags_from_text(text, "src/sample.py")
        if tag.kind == "definition" and _is_definition_tag(tag)
    }
    analysis = _definition_analysis(text, "src/sample.py")

    assert (
        {(tag.name, tag.line) for tag in analysis.definitions}
        == legacy
        == {
            ("CONST", 1),
            ("typed", 4),
            ("outer", 5),
            ("inner", 7),
            ("Box", 10),
            ("method", 11),
        }
    )
    windows = {window.name: (window.start_line, window.end_line) for window in symbol_windows(text, "src/sample.py")}
    assert windows == {
        "CONST": (1, 3),
        "typed": (4, 4),
        "outer": (5, 9),
        "inner": (7, 8),
        "Box": (10, 12),
        "method": (11, 12),
    }


def test_deleting_the_tail_of_a_body_is_a_change_not_a_disappearance(tmp_path: Path) -> None:
    """``body in old_blob`` asked whether the bytes appear anywhere, not whether they moved.

    An edit that only deletes the trailing lines of a definition leaves a
    remainder that is a verbatim *prefix* of what that body used to be, so the
    substring guard dropped a genuinely changed definition. Measured on this
    fixture before the fix: ``symbols: []``, zero impact sites, no ATTENTION
    section, three untouched callers unreported -- under a footer still reading
    ``index: fresh`` with nothing in ``degraded``. The guard only runs when the
    index has a row for the file, which is why this builds a real index.
    """

    root = tmp_path / "trailing"
    repo = _init_repo(root)
    _write(
        root,
        "lib/m.py",
        "def alpha(value):\n"
        "    total = value + 1\n"
        "    if total < 0:\n"
        "        raise ValueError('neg')\n"
        "    return total\n",
    )
    for name in ("one", "two", "three"):
        _write(root, f"app/{name}.py", f"from lib.m import alpha\n\n\ndef {name}():\n    return alpha(1)\n")
    _commit(repo, "base", 0)
    _write(root, "lib/m.py", "def alpha(value):\n    total = value + 1\n")
    _commit(repo, "drop the trailing guard", 60)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    CodeContextEngine(root, autosync_enabled=False).index_repo(force=True)

    result = _impact(root)

    assert result.index_status == "fresh"
    alpha = _symbol(result, "alpha")
    assert alpha is not None, f"expected `alpha`, got {result.symbols!r} degraded={result.degraded!r}"
    assert alpha.change == "modified"
    assert alpha.source == "index"
    assert _sites(result, "untouched_caller"), "the callers the dropped symbol was hiding"


def test_a_base_blob_that_does_not_parse_does_not_change_every_definition() -> None:
    """The exact-body guard needs the base side to parse; when it cannot, fall back.

    ``symbol_windows`` returns nothing at all for a blob the tag extractor
    cannot handle -- a missing colon is enough (measured: ``_bodies_by_name``
    returns ``{}`` for this ``old_text``). With no base body to compare against,
    reporting the definition as changed makes the commit that FIXES a syntax
    error claim every hunk-touched definition in the file, byte-identical ones
    included, with real untouched callers attached and nothing in ``degraded``.
    ``beta`` here is byte-identical and only dragged into the hunk's span.
    """

    from lemoncrow.pro.capabilities.review.impact import _bodies_by_name, _definition_tags, _index_symbols

    old_text = "def alpha(value)\n    return value\n\n\ndef beta(value):\n    return value * 2\n"
    new_text = "def alpha(value):\n    return value\n\n\ndef beta(value):\n    return value * 2\n"
    assert _bodies_by_name(old_text, "lib/m.py") == {}, "fixture must have an unparseable base side"

    item = ChangedFile(
        path="lib/m.py",
        old_path=None,
        status="modified",
        hunks=(DiffHunk(old_start=1, old_lines=6, new_start=1, new_lines=6, header="@@"),),
    )
    entries = [{"name": "alpha", "kind": "function"}, {"name": "beta", "kind": "function"}]

    named, _degraded = _index_symbols(
        item,
        entries,
        _definition_tags(new_text, item.path),
        old_text=old_text,
        new_text=new_text,
    )

    assert [symbol.symbol_name for symbol in named] == ["alpha"]


def test_a_definition_byte_identical_to_its_own_old_self_is_still_dropped() -> None:
    """The guard the fix must keep: a definition the edit only *moved*.

    ``beta`` is dragged into the hunk's span by an insertion above it and is
    byte-identical to the ``beta`` the base blob held, so it must not reach the
    packet -- every warning built on it would be a warning about nothing.
    ``alpha`` on the same blob really did change and must.
    """

    from lemoncrow.pro.capabilities.review.impact import _definition_tags, _index_symbols

    old_text = "def alpha(value):\n    return value\n\n\ndef beta(value):\n    return value * 2\n"
    new_text = "def alpha(value):\n    return value + 1\n\n\ndef beta(value):\n    return value * 2\n"
    item = ChangedFile(
        path="lib/m.py",
        old_path=None,
        status="modified",
        hunks=(DiffHunk(old_start=1, old_lines=6, new_start=1, new_lines=6, header="@@"),),
    )
    entries = [{"name": "alpha", "kind": "function"}, {"name": "beta", "kind": "function"}]

    named, _degraded = _index_symbols(
        item,
        entries,
        _definition_tags(new_text, item.path),
        old_text=old_text,
        new_text=new_text,
    )

    assert [symbol.symbol_name for symbol in named] == ["alpha"]


# --- the reviewed revision decides which detector sites are real -------------


def test_detector_sites_in_a_file_the_reviewed_revision_lacked_are_dropped(tmp_path: Path) -> None:
    """The detectors read the workspace; the packet is about a commit.

    ``app/late.py`` is added two commits after the one under review, and the
    index is built at the checkout. Before the fix the packet's top ATTENTION
    block cited ``app/late.py:L1`` and ``app/late.py:L5`` for a symbol removed
    in the reviewed commit -- a file that commit had never heard of -- with the
    footer reading ``index: fresh`` and nothing in ``degraded``.
    """

    root = tmp_path / "latefile"
    repo = _init_repo(root)
    _write(root, "lib/api.py", "LEGACY_HELPER = 1\n\n\ndef keep():\n    return 2\n")
    _write(root, "app/user.py", "from lib.api import LEGACY_HELPER\n\n\ndef use():\n    return LEGACY_HELPER\n")
    base = _commit(repo, "base", 0)
    _write(root, "lib/api.py", "def keep():\n    return 2\n")
    reviewed = _commit(repo, "remove LEGACY_HELPER", 60)
    _write(root, "app/late.py", "from lib.api import LEGACY_HELPER\n\n\ndef late():\n    return LEGACY_HELPER\n")
    _commit(repo, "later work", 120)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    CodeContextEngine(root, autosync_enabled=False).index_repo(force=True)

    unfiltered = _impact(root, f"{base}..{reviewed}")
    assert any(
        site.path.startswith("app/late.py") for site in unfiltered.sites
    ), "fixture must actually reach the file added later, or it proves nothing"

    result = _impact(root, f"{base}..{reviewed}", head_filter=True)

    assert [site.path for site in result.sites if site.path.startswith("app/late.py")] == []
    assert "sites_absent_from_revision" in result.degraded


def test_a_detector_site_whose_file_the_workspace_shifted_is_cited_without_a_line(tmp_path: Path) -> None:
    """The file outlived the reviewed revision; its *line numbers* did not.

    ``app/user.py`` still names ``LEGACY_HELPER`` at the reviewed commit, so the
    existence gate keeps the site. But fifty lines are prepended to that file
    one commit later and the detectors read the workspace, so the packet cited
    ``app/user.py:L51`` and ``:L55`` in a file that is five lines long at the
    revision under review -- past its end, with nothing in ``degraded``. The
    workspace and the reviewed revision disagree about where the token is, so
    the workspace's coordinate is not the reviewed revision's and may not be
    printed as if it were: the site is cited as a bare path instead.
    """

    root = tmp_path / "shifted"
    repo = _init_repo(root)
    _write(root, "lib/api.py", "LEGACY_HELPER = 1\n\n\ndef keep():\n    return 2\n")
    consumer = "from lib.api import LEGACY_HELPER\n\n\ndef use():\n    return LEGACY_HELPER\n"
    _write(root, "app/user.py", consumer)
    base = _commit(repo, "base", 0)
    _write(root, "lib/api.py", "def keep():\n    return 2\n")
    reviewed = _commit(repo, "remove LEGACY_HELPER", 60)
    _write(root, "app/user.py", "".join(f"# pad {index}\n" for index in range(50)) + consumer)
    _commit(repo, "pad the consumer", 120)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    CodeContextEngine(root, autosync_enabled=False).index_repo(force=True)

    unfiltered = _impact(root, f"{base}..{reviewed}")
    assert [
        site.path for site in unfiltered.sites if site.path.startswith("app/user.py:L")
    ], "fixture must cite the workspace's shifted lines, or it proves nothing"

    result = _impact(root, f"{base}..{reviewed}", head_filter=True)

    cited = [site.path for site in result.sites if site.path.startswith("app/user.py")]
    assert cited == ["app/user.py"], f"expected one bare-path site, got {cited!r}"
    assert "caller_line_ranges" in result.degraded


def test_a_detector_site_the_workspace_and_the_revision_agree_on_keeps_its_line(tmp_path: Path) -> None:
    """The withheld line is for disagreement only, not for every reviewed range.

    Nothing moved between the reviewed commit and the checkout here, so the
    detector's coordinate *is* a coordinate in the reviewed revision and the
    reviewer keeps a clickable line.
    """

    result = _impact(_signature_change_repo(tmp_path), head_filter=True)

    sites = _sites(result, "signature_change")
    assert [site.path for site in sites] == ["jobs/session_cleanup.py:L5"]
    assert "caller_line_ranges" not in result.degraded


def test_a_detector_site_the_reviewed_revision_does_contain_survives(tmp_path: Path) -> None:
    """The gate is existence plus geometry, and it never re-anchors a site.

    The revision is asked where it puts the site's own token, so the answer can
    be compared with the workspace's answer for the same token. It is never
    adopted as the site's line: ``head_symbol_filter`` locates the *first
    mention* of a name, which in a consumer file is the ``import`` line, and a
    site moved there would claim the import is the use. With no workspace copy
    to disagree with -- as here -- the detector's own line stands.
    """

    from lemoncrow.pro.capabilities.review.impact import _sites_in_reviewed_revision

    site = ImpactSite(kind="removed_symbol", path="app/user.py:L5", old="LEGACY_HELPER", new=None, snippet="")
    asked: list[tuple[str, str, str]] = []

    def _present(path: str, symbol: str, caller: str) -> int | None:
        asked.append((path, symbol, caller))
        return 1

    kept, dropped, unanchored = _sites_in_reviewed_revision(tmp_path, [site], _present)

    assert kept == [site]
    assert dropped == 0
    assert unanchored == 0
    assert asked == [("app/user.py", "LEGACY_HELPER", "LEGACY_HELPER")]


# --- the site cap may not delete a whole class of finding --------------------


def test_detector_sites_cannot_starve_the_untouched_caller_pass(tmp_path: Path) -> None:
    """25 removed constants must not cost the four untouched callers of `target`.

    The caller budget was ``limit_sites - len(detector_sites)``, so at the
    shipped default ``--limit 40`` the caller pass ran with a budget of zero and
    the packet reported 40 ``removed_symbol`` sites as if that were the total --
    indistinguishable from a review whose caller pass found nothing. The same
    command at ``--limit 200`` reported 54 sites including all four callers.
    """

    root = tmp_path / "starve"
    repo = _init_repo(root)
    constants = "".join(f'CONST_{index:02d} = "value-{index:02d}"\n' for index in range(25))
    _write(root, "lib/api.py", constants + "\n\ndef target(value):\n    return value\n")
    for number in range(2):
        body = "".join(f"    _ = CONST_{index:02d}\n" for index in range(25))
        _write(root, f"lib/consume{number}.py", f"from lib.api import *\n\n\ndef c{number}():\n{body}")
    for number in range(4):
        _write(
            root,
            f"app/call{number}.py",
            f"from lib.api import target\n\n\ndef c{number}():\n    return target({number})\n",
        )
    _commit(repo, "base", 0)
    _write(root, "lib/api.py", "def target(value):\n    return value + 1\n")
    _commit(repo, "drop the constants", 60)

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    CodeContextEngine(root, autosync_enabled=False).index_repo(force=True)

    uncapped = _impact(root, limit_sites=200)
    assert len(_sites(uncapped, "untouched_caller")) == 4, "fixture must have callers to starve"
    assert len(uncapped.sites) > 40, "fixture must overflow the shipped default"
    assert "impact_sites_truncated" not in uncapped.degraded

    capped = _impact(root, limit_sites=40)

    assert len(capped.sites) == 40
    assert len(_sites(capped, "untouched_caller")) == 4
    assert "impact_sites_truncated" in capped.degraded


def test_the_site_cap_names_what_it_dropped_and_keeps_the_caller_reserve() -> None:
    from lemoncrow.pro.capabilities.review.impact import _CALLER_SITE_RESERVE, _cap_sites

    detectors = [
        ImpactSite(kind="removed_symbol", path=f"a/{index}.py:L1", old=f"S{index}", new=None, snippet="")
        for index in range(30)
    ]
    callers = [
        ImpactSite(kind="untouched_caller", path=f"b/{index}.py:L1", old="target", new=None, snippet="")
        for index in range(12)
    ]

    kept, dropped = _cap_sites([*detectors, *callers], 40)

    assert len(kept) == 40
    assert dropped == 2
    # The reserve is a floor, not a ceiling: spare budget after the detectors
    # still goes to callers in order.
    assert len([site for site in kept if site.kind == "untouched_caller"]) >= _CALLER_SITE_RESERVE

    # A list that fits is returned whole and reports no loss.
    kept, dropped = _cap_sites(detectors[:3], 10)
    assert len(kept) == 3
    assert dropped == 0


def test_a_small_limit_is_not_handed_to_the_caller_pass_whole() -> None:
    """``--limit 5`` must not evict every detector site for weaker callers."""

    from lemoncrow.pro.capabilities.review.impact import _cap_sites

    detectors = [
        ImpactSite(kind="removed_symbol", path=f"a/{index}.py:L1", old=f"S{index}", new=None, snippet="")
        for index in range(30)
    ]
    callers = [
        ImpactSite(kind="untouched_caller", path=f"b/{index}.py:L1", old="target", new=None, snippet="")
        for index in range(12)
    ]

    for limit in (1, 3, 5, 8):
        kept, _ = _cap_sites([*detectors, *callers], limit)
        kinds = {site.kind for site in kept}
        assert "removed_symbol" in kinds, f"limit {limit} dropped every detector site: {kinds}"
