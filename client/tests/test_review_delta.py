"""Review Anywhere P2: Git manifest parity, durable cursor and real HTTP delta."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from lemoncrow_client.blobs import BlobService
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.review import _capture, resolve_review_range
from lemoncrow_client.review_delta import DeltaCursor, snapshot


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, text=True, check=True, capture_output=True).stdout.strip()


def test_git_snapshot_overlay_staged_rebase_rename_delete_and_cursor(worktree: Path, state_dir: Path) -> None:
    cursor = DeltaCursor.load(worktree, state_dir)
    head = git(worktree, "rev-parse", "HEAD")
    baseline = snapshot(worktree, head, cursor)
    assert "pkg/alpha.py" in baseline.entries
    assert baseline.payload(baseline.entries["pkg/alpha.py"].content_digest)[0].startswith(b"def alpha")
    original = baseline.entries["pkg/alpha.py"].content_digest
    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 99\n", encoding="utf-8")
    (worktree / "new.py").write_text("new = True\n", encoding="utf-8")
    working = snapshot(worktree, head, cursor, working_tree=True)
    assert working.entries["pkg/alpha.py"].content_digest != original
    assert "new.py" in working.entries
    assert working.payload(working.entries["new.py"].content_digest)[0] == b"new = True\n"
    assert baseline.entries["pkg/alpha.py"].content_digest == original
    git(worktree, "add", "pkg/alpha.py")
    staged = snapshot(worktree, ":index:", cursor)
    assert staged.entries["pkg/alpha.py"] == working.entries["pkg/alpha.py"]
    assert "new.py" not in staged.entries
    git(worktree, "mv", "pkg/beta.py", "pkg/renamed.py")
    (worktree / "docs" / "notes.md").unlink()
    after = snapshot(worktree, head, cursor, working_tree=True)
    assert "pkg/beta.py" not in after.entries
    assert "pkg/renamed.py" in after.entries
    assert "docs/notes.md" not in after.entries
    cursor.save()
    assert DeltaCursor.load(worktree, state_dir).known == cursor.known
    assert cursor.path.stat().st_mode & 0o077 == 0
    git(worktree, "add", "-A")
    git(worktree, "-c", "user.name=Tests", "-c", "user.email=test@example.invalid", "commit", "-qm", "next")
    next_snapshot = snapshot(worktree, git(worktree, "rev-parse", "HEAD"), DeltaCursor.load(worktree, state_dir))
    assert next_snapshot.entries == after.entries


def test_untracked_secrets_and_symlinks_fail_closed(worktree: Path, state_dir: Path, tmp_path: Path) -> None:
    cursor = DeltaCursor.load(worktree, state_dir)
    head = git(worktree, "rev-parse", "HEAD")
    (worktree / ".env").write_text("SECRET=definitely_do_not_upload\n", encoding="utf-8")
    with pytest.raises(ClientError) as denied:
        snapshot(worktree, head, cursor, working_tree=True)
    assert denied.value.code == ErrorCode.PAYLOAD_INVALID
    (worktree / ".env").unlink()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, worktree / "out-link")
    with pytest.raises(ClientError):
        snapshot(worktree, head, cursor, working_tree=True)
    (worktree / "out-link").unlink()
    (worktree / "safe.py").write_bytes(b"-----BEGIN PRIVATE KEY-----\ndanger\n")
    # Sensitive bytes now fail before sending a manifest or any blob metadata.
    with pytest.raises(ClientError) as key_denied:
        snapshot(worktree, head, cursor, working_tree=True)
    assert key_denied.value.code == ErrorCode.PAYLOAD_INVALID


def test_source_mutation_and_oversize_are_refused(worktree: Path, state_dir: Path) -> None:
    cursor = DeltaCursor.load(worktree, state_dir)
    head = git(worktree, "rev-parse", "HEAD")
    target = worktree / "pkg" / "alpha.py"
    target.write_bytes(b"first\n")
    first = snapshot(worktree, head, cursor, working_tree=True)
    digest = first.entries["pkg/alpha.py"].content_digest
    target.write_bytes(b"second\n")
    with pytest.raises(ClientError) as stale:
        first.payload(digest)
    assert stale.value.code == ErrorCode.VIEW_REVISION_STALE
    huge = worktree / "oversize.txt"
    with huge.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ClientError) as too_large:
        snapshot(worktree, head, cursor, working_tree=True)
    assert too_large.value.code == ErrorCode.REQUEST_TOO_LARGE


@pytest.mark.parametrize(
    "relative", [".git-credentials", "config/secrets.json", "deployment/terraform.tfstate", "config/.kube/config"]
)
def test_sensitive_untracked_paths_refused_before_manifest(
    worktree: Path,
    state_dir: Path,
    relative: str,
) -> None:
    path = worktree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("credentials go here\n", encoding="utf-8")
    with pytest.raises(ClientError) as error:
        snapshot(worktree, git(worktree, "rev-parse", "HEAD"), DeltaCursor.load(worktree, state_dir), working_tree=True)
    assert error.value.code == ErrorCode.PAYLOAD_INVALID


@pytest.mark.parametrize(
    "secret",
    [
        b"OPENAI_API_KEY=sk-proj-0123456789abcdefghijklmnopqrstuvwxyz",
        b"AWS_ACCESS_KEY_ID=AKIA0123456789ABCDEF",
        b"token=github_pat_0123456789abcdefghijklmnop",
    ],
)
def test_provider_credentials_refused_before_manifest(worktree: Path, state_dir: Path, secret: bytes) -> None:
    (worktree / "normal.py").write_bytes(secret + b"\n")
    with pytest.raises(ClientError) as error:
        snapshot(worktree, git(worktree, "rev-parse", "HEAD"), DeltaCursor.load(worktree, state_dir), working_tree=True)
    assert error.value.code == ErrorCode.PAYLOAD_INVALID


def test_untracked_exact_allowlist_never_overrides_secret_guards(
    worktree: Path,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    untracked = worktree / "asset.xyz"
    untracked.write_bytes(b"binary source data\x00safe")
    head = git(worktree, "rev-parse", "HEAD")
    cursor = DeltaCursor.load(worktree, state_dir)
    with pytest.raises(ClientError) as denied:
        snapshot(worktree, head, cursor, working_tree=True)
    assert denied.value.code == ErrorCode.PAYLOAD_INVALID
    monkeypatch.setenv("LEMONCROW_REVIEW_UNTRACKED_ALLOW", "elsewhere.xyz")
    with pytest.raises(ClientError):
        snapshot(worktree, head, cursor, working_tree=True)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNTRACKED_ALLOW", "asset.xyz")
    snap = snapshot(worktree, head, cursor, working_tree=True)
    assert snap.payload(snap.entries["asset.xyz"].content_digest)[0] == untracked.read_bytes()
    (worktree / "apikey.xyz").write_bytes(b"-----BEGIN PRIVATE KEY-----\nnever send\n")
    monkeypatch.setenv("LEMONCROW_REVIEW_UNTRACKED_ALLOW", "asset.xyz,apikey.xyz,.env")
    with pytest.raises(ClientError):
        snapshot(worktree, head, cursor, working_tree=True)
    (worktree / "apikey.xyz").unlink()
    (worktree / ".env").write_bytes(b"DANGEROUS=1\n")
    with pytest.raises(ClientError):
        snapshot(worktree, head, cursor, working_tree=True)


def test_git_submodule_pointer_remains_metadata_not_regular_blob(
    worktree: Path,
    state_dir: Path,
    tmp_path: Path,
) -> None:
    from lemoncrow_client.review import _gitlinks_for_range

    nested = tmp_path / "submodule"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "private.py").write_text("NOT_IN_PARENT_SOURCE = True\n")
    git(nested, "add", "private.py")
    git(nested, "-c", "user.name=Tests", "-c", "user.email=t@example.invalid", "commit", "-qm", "nested")
    git(worktree, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(nested), "vendor/library")
    index = snapshot(worktree, ":index:", DeltaCursor.load(worktree, state_dir))
    assert "vendor/library/private.py" not in index.entries
    assert "vendor/library" not in index.entries
    rng = resolve_review_range(worktree, staged=True)
    before, after = _gitlinks_for_range(worktree, rng)
    assert "vendor/library" not in before
    assert "vendor/library" in after
    assert after["vendor/library"] == git(nested, "rev-parse", "HEAD")

    other = tmp_path / "secondary"
    git(worktree, "worktree", "add", "-qb", "review-other", str(other))
    primary = DeltaCursor.load(worktree, state_dir)
    secondary = DeltaCursor.load(other, state_dir)
    assert primary.path != secondary.path
    revision = git(worktree, "rev-parse", "HEAD")
    assert snapshot(worktree, revision, primary).entries == snapshot(other, revision, secondary).entries
    primary.save()
    assert not secondary.path.exists()
    secondary.save()
    assert secondary.path.exists()
    git(worktree, "worktree", "remove", "--force", str(other))


def test_review_delta_real_loopback_http_and_reconnect(config: object, stub: object, worktree: Path) -> None:
    stub.state.review_manifest["capture_options"] = [
        option for option in stub.state.review_manifest["capture_options"] if option != "workspace_branch"
    ]
    cursor = DeltaCursor.load(worktree, config.review_state_dir)
    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    response, url = _capture(
        config,
        resolve_review_range(worktree, working_tree=True),
        with_impact=False,
        restore_archived=False,
        delta_cursor=cursor,
    )
    assert url.startswith(config.url)
    first = response["_delta_upload"]
    assert first["head_uploaded_bytes"] > 0
    assert cursor.path.is_file()
    initial = len(stub.state.blobs)
    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    response2, _ = _capture(
        config,
        resolve_review_range(worktree, working_tree=True),
        with_impact=False,
        restore_archived=False,
        delta_cursor=DeltaCursor.load(worktree, config.review_state_dir),
    )
    second = response2["_delta_upload"]
    assert second["head_uploaded_bytes"] == len(b"def alpha():\n    return 2\n")
    assert second["base_uploaded_bytes"] == 0
    assert len(stub.state.blobs) == initial + 1
    assert len(stub.state.review_captures) == 2


def test_10k_warm_delta_measured_bytes_and_latency(tmp_path: Path, stub: object, state_dir: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    git(root, "init", "-q")
    for number in range(10_000):
        target = root / f"file-{number:05}.py"
        target.write_text(f"VALUE = {number:05}\n" + "#" * 64 + "\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=Tests", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    from conftest import make_config

    stub.state.review_manifest["capture_options"] = [
        option for option in stub.state.review_manifest["capture_options"] if option != "workspace_branch"
    ]
    config = make_config(url=stub.url, worktree=root, state_dir=state_dir)
    cursor = DeltaCursor.load(root, state_dir)
    revision = git(root, "rev-parse", "HEAD")
    cold = snapshot(root, revision, cursor)
    assert len(cold.entries) == 10_000
    baseline_bytes = sum(row.size for row in cold.entries.values())
    first, _url = _capture(
        config,
        resolve_review_range(root, working_tree=True),
        with_impact=False,
        restore_archived=False,
        delta_cursor=cursor,
    )
    assert first["_delta_upload"]["head_uploaded_bytes"] >= baseline_bytes
    times = []
    root.joinpath("file-00500.py").write_text("VALUE = 99999\n", encoding="utf-8")
    for _ in range(5):
        start = time.perf_counter()
        new = snapshot(root, revision, DeltaCursor.load(root, state_dir), working_tree=True)
        times.append(time.perf_counter() - start)
    changed = new.entries["file-00500.py"]
    assert changed.content_digest != cold.entries["file-00500.py"].content_digest
    changed_content = new.payload(changed.content_digest)[0]
    start = time.perf_counter()
    second, _url = _capture(
        config,
        resolve_review_range(root, working_tree=True),
        with_impact=False,
        restore_archived=False,
        delta_cursor=DeltaCursor.load(root, state_dir),
    )
    capture_seconds = time.perf_counter() - start
    sent = sum(second["_delta_upload"].values())
    reduction = 1 - sent / baseline_bytes
    assert sent == len(changed_content)
    assert reduction >= 0.95
    print(
        f"P2 10k HTTP: baseline={baseline_bytes}B second_upload={sent}B "
        f"unchanged_byte_reduction={reduction:.4%} warm_manifest_p95_s={sorted(times)[-1]:.3f} "
        f"second_capture_s={capture_seconds:.3f}"
    )


def test_payload_refuses_symlink_swap_after_manifest(
    worktree: Path,
    state_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow_client import review_delta

    target = worktree / "pkg" / "alpha.py"
    target.write_bytes(b"SAFE=1\n")
    frozen = snapshot(
        worktree, git(worktree, "rev-parse", "HEAD"), DeltaCursor.load(worktree, state_dir), working_tree=True
    )
    digest = frozen.entries["pkg/alpha.py"].content_digest
    external = tmp_path / "outside-secret"
    external.write_bytes(b"NOT_TO_BE_SENT=private\n")
    real_open = os.open
    swapped = False

    def swap_before_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if path == "alpha.py" and "dir_fd" in kwargs and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(external)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(review_delta.os, "open", swap_before_open)
    with pytest.raises(ClientError) as error:
        frozen.payload(digest)
    assert swapped
    assert error.value.code == ErrorCode.VIEW_REVISION_STALE


def test_payload_refuses_ancestor_symlink_and_fifo(
    worktree: Path,
    state_dir: Path,
    tmp_path: Path,
) -> None:
    target = worktree / "pkg" / "alpha.py"
    target.write_bytes(b"BEFORE=1\n")
    frozen = snapshot(
        worktree, git(worktree, "rev-parse", "HEAD"), DeltaCursor.load(worktree, state_dir), working_tree=True
    )
    digest = frozen.entries["pkg/alpha.py"].content_digest
    external = tmp_path / "outsider"
    external.mkdir()
    (external / "alpha.py").write_bytes(b"SECRET=2\n")
    (worktree / "pkg").rename(worktree / "pkg-original")
    (worktree / "pkg").symlink_to(external, target_is_directory=True)
    with pytest.raises(ClientError) as error:
        frozen.payload(digest)
    assert error.value.code == ErrorCode.VIEW_REVISION_STALE
    (worktree / "pkg").unlink()
    (worktree / "pkg-original").rename(worktree / "pkg")
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ClientError) as error:
        frozen.payload(digest)
    assert error.value.code == ErrorCode.VIEW_REVISION_STALE


def test_binary_delta_parity_and_staged_secret_rejection(
    worktree: Path,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = os.urandom(2 * 1024 * 1024)
    binary = worktree / "asset.bin"
    binary.write_bytes(source)
    head = git(worktree, "rev-parse", "HEAD")
    cursor = DeltaCursor.load(worktree, state_dir)
    with pytest.raises(ClientError):
        snapshot(worktree, head, cursor, working_tree=True)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNTRACKED_ALLOW", "asset.bin")
    working = snapshot(worktree, head, cursor, working_tree=True)
    digest = working.entries["asset.bin"].content_digest
    assert working.payload(digest)[0] == source
    git(worktree, "add", "asset.bin")
    staged = snapshot(worktree, ":index:", cursor)
    assert staged.entries["asset.bin"] == working.entries["asset.bin"]
    assert staged.payload(digest)[0] == source
    (worktree / "apikey.txt").write_bytes(b"-----BEGIN PRIVATE KEY-----\nnope\n")
    git(worktree, "add", "apikey.txt")
    with pytest.raises(ClientError) as error:
        snapshot(worktree, ":index:", cursor)
    assert error.value.code == ErrorCode.PAYLOAD_INVALID


def test_partial_http_ack_recovers_via_server_missing_digest_negotiation(
    config: object,
    stub: object,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub.state.review_manifest["capture_options"] = [
        option for option in stub.state.review_manifest["capture_options"] if option != "workspace_branch"
    ]
    for index in range(48):
        (worktree / f"changed-{index:03}.txt").write_bytes(f"revision-{index:03}-different\n".encode())
    cursor = DeltaCursor.load(worktree, config.review_state_dir)
    original = BlobService.upload_payloads
    interrupted = False

    def fail_after_server_ack(self: BlobService, *args: object, **kwargs: object) -> tuple[int, int]:
        nonlocal interrupted
        result = original(self, *args, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("connection lost after acknowledged blob batch")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(BlobService, "upload_payloads", fail_after_server_ack)
        with pytest.raises(OSError):
            _capture(
                config,
                resolve_review_range(worktree, working_tree=True),
                with_impact=False,
                restore_archived=False,
                delta_cursor=cursor,
            )
    assert interrupted and not cursor.path.exists()
    persisted = len(stub.state.blobs)
    assert persisted >= 32
    result, url = _capture(
        config,
        resolve_review_range(worktree, working_tree=True),
        with_impact=False,
        restore_archived=False,
        delta_cursor=DeltaCursor.load(worktree, config.review_state_dir),
    )
    assert url.startswith(config.url)
    assert result["_delta_upload"]["head_uploaded_bytes"] < 48 * len(b"revision-000-different\n")
    assert len(stub.state.blobs) > persisted
    assert DeltaCursor.load(worktree, config.review_state_dir).known


def test_local_source_changes_during_upload_do_not_publish_stale_review(
    config: object,
    stub: object,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow_client.review_session import ReviewSession

    stub.state.review_manifest["capture_options"] = [
        option for option in stub.state.review_manifest["capture_options"] if option != "workspace_branch"
    ]
    target = worktree / "pkg" / "alpha.py"
    target.write_text("def alpha():\n    return 10\n")
    original = ReviewSession.open_delta_snapshot
    calls = 0

    def race(self: ReviewSession, *args: object, **kwargs: object) -> object:
        nonlocal calls
        result = original(self, *args, **kwargs)
        calls += 1
        if calls == 1:
            target.write_text("def alpha():\n    return 20\n")
        return result

    monkeypatch.setattr(ReviewSession, "open_delta_snapshot", race)
    with pytest.raises(ClientError) as denied:
        _capture(
            config,
            resolve_review_range(worktree, working_tree=True),
            with_impact=False,
            restore_archived=False,
            delta_cursor=DeltaCursor.load(worktree, config.review_state_dir),
        )
    assert denied.value.code == ErrorCode.VIEW_REVISION_STALE
    assert not stub.state.review_captures


def test_local_head_moves_during_upload_do_not_publish_old_commit(
    config: object,
    stub: object,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow_client.review_session import ReviewSession

    stub.state.review_manifest["capture_options"] = [
        option for option in stub.state.review_manifest["capture_options"] if option != "workspace_branch"
    ]
    target = worktree / "pkg" / "alpha.py"
    target.write_text("def alpha():\n    return 10\n")
    original = ReviewSession.open_delta_snapshot
    calls = 0

    def race(self: ReviewSession, *args: object, **kwargs: object) -> object:
        nonlocal calls
        result = original(self, *args, **kwargs)
        calls += 1
        if calls == 1:
            git(worktree, "add", "pkg/alpha.py")
            git(worktree, "-c", "user.name=P2", "-c", "user.email=p2@example.invalid", "commit", "-qm", "moving-head")
        return result

    monkeypatch.setattr(ReviewSession, "open_delta_snapshot", race)
    with pytest.raises(ClientError) as denied:
        _capture(
            config,
            resolve_review_range(worktree, working_tree=True),
            with_impact=False,
            restore_archived=False,
            delta_cursor=DeltaCursor.load(worktree, config.review_state_dir),
        )
    assert denied.value.code == ErrorCode.VIEW_REVISION_STALE
    assert not stub.state.review_captures


def test_older_hosted_server_without_capture_intent_endpoint_still_captures(
    config: object,
    stub: object,
    worktree: Path,
) -> None:
    from lemoncrow_client.review_session import ReviewSession

    # The loopback older-client stub has no capture-intent route. The new client
    # may fall back ONLY for endpoint-not-found, never for auth or network denial.
    session = ReviewSession(config)
    try:
        session.handshake()
        session.open_session()
        opened = session.open_delta_snapshot(
            snapshot(
                worktree, git(worktree, "rev-parse", "HEAD"), DeltaCursor.load(worktree, config.review_state_dir)
            ).entries,
            source_revision=git(worktree, "rev-parse", "HEAD"),
            payload=snapshot(
                worktree, git(worktree, "rev-parse", "HEAD"), DeltaCursor.load(worktree, config.review_state_dir)
            ).payload,
        )
        assert session.begin_capture_intent(str(opened["repo_id"]), branch="main", mode="working_tree") == ""
    finally:
        session.close()


def test_rebase_changes_commit_base_without_reusing_stale_blob_or_snapshot(
    worktree: Path,
    state_dir: Path,
) -> None:
    """A rewritten Git history invalidates tree identity despite a warm OID cache."""
    original_branch = git(worktree, "branch", "--show-current")
    initial = git(worktree, "rev-parse", "HEAD")
    git(worktree, "checkout", "-qb", "p2-feature")
    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 'rebased'\n")
    git(worktree, "add", "pkg/alpha.py")
    git(worktree, "-c", "user.name=P2", "-c", "user.email=p2@example.invalid", "commit", "-qm", "feature")
    feature_head = git(worktree, "rev-parse", "HEAD")
    cursor = DeltaCursor.load(worktree, state_dir)
    old_snapshot = snapshot(worktree, feature_head, cursor)
    git(worktree, "checkout", original_branch)
    (worktree / "docs" / "notes.md").write_text("new main-side notes\n")
    git(worktree, "add", "docs/notes.md")
    git(worktree, "-c", "user.name=P2", "-c", "user.email=p2@example.invalid", "commit", "-qm", "main")
    git(worktree, "checkout", "p2-feature")
    git(worktree, "rebase", original_branch)
    rebased_head = git(worktree, "rev-parse", "HEAD")
    assert rebased_head not in (initial, feature_head)
    rebased = snapshot(worktree, rebased_head, cursor)
    stage = snapshot(worktree, ":index:", cursor)
    assert rebased.entries == stage.entries
    assert rebased.entries["docs/notes.md"].content_digest != old_snapshot.entries["docs/notes.md"].content_digest
    assert rebased.payload(rebased.entries["pkg/alpha.py"].content_digest)[0] == b"def alpha():\n    return 'rebased'\n"
    assert rebased.payload(rebased.entries["docs/notes.md"].content_digest)[0] == b"new main-side notes\n"


def test_agent_checkpoint_hook_publishes_real_http_view_without_agent_commands(
    config: object,
    stub: object,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    from lemoncrow_client import review_watch
    from lemoncrow_client.review import review_main

    stub.state.review_manifest["capture_options"] = [
        option for option in stub.state.review_manifest["capture_options"] if option != "workspace_branch"
    ]
    monkeypatch.setattr(review_watch, "load_config", lambda **_: config)
    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 73\n")
    output = io.StringIO()
    assert review_main(["hook", "after-checkpoint"], sink=output) == 0
    assert '"event": "captured"' in output.getvalue()
    assert len(stub.state.review_captures) == 1
    before = len(stub.state.blobs)
    assert review_main(["hook", "on-exit"], sink=io.StringIO()) == 0
    assert len(stub.state.blobs) == before  # identical Snapshot: digest negotiation avoids upload
    assert len(stub.state.review_captures) == 2  # committed View parity, no agent-supplied source


def test_agent_on_exit_hook_captures_clean_committed_head_over_http(
    config: object,
    stub: object,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    from lemoncrow_client import review_watch
    from lemoncrow_client.review import review_main

    stub.state.review_manifest["capture_options"] = [
        option for option in stub.state.review_manifest["capture_options"] if option != "workspace_branch"
    ]
    monkeypatch.setattr(review_watch, "load_config", lambda **_: config)
    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 101\n")
    git(worktree, "add", "pkg/alpha.py")
    git(worktree, "-c", "user.name=P3", "-c", "user.email=p3@example.invalid", "commit", "-qm", "checkpoint")
    assert git(worktree, "status", "--porcelain") == ""
    output = io.StringIO()
    assert review_main(["hook", "on-exit"], sink=output) == 0, output.getvalue()
    assert '"event": "captured"' in output.getvalue()
    assert len(stub.state.review_captures) == 1
    assert stub.state.review_captures[0]["range"]["mode"] == "commit_range"
