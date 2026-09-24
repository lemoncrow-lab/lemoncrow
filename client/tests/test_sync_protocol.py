"""Bootstrap, the manifest negotiation, and the two contracts that follow it.

The revision binding and the blob miss are what make remote execution safe, so
they are asserted as behaviour rather than as the presence of a header: an edit
is written, a read is issued, and the *content that comes back* is the content
the edit produced -- because the read named the revision the write committed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import lemoncrow_client.session as session_mod
import pytest
from _stub import StubServer, sha256_hex
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.dispatcher import Dispatcher
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.manifest import walk_worktree
from lemoncrow_client.session import RemoteSession, _host_runtime_headers, _json_size
from lemoncrow_client.transport import HttpTransport

# --------------------------------------------------------------------------- #
# Handshake and registration                                                  #
# --------------------------------------------------------------------------- #


def test_the_handshake_happens_before_authentication(stub: StubServer, worktree: Path, state_dir: Path) -> None:
    """An out-of-date client must learn *that*, not see a confusing 401."""
    from conftest import make_config

    stub.state.protocol_version = "2099-01-01"
    config = make_config(url=stub.url, worktree=worktree, state_dir=state_dir, token="")
    session = RemoteSession(config)
    with pytest.raises(ClientError) as caught:
        session.handshake()
    assert caught.value.code is ErrorCode.PROTOCOL_VERSION_MISMATCH


def test_a_version_mismatch_refuses_loudly_and_never_degrades(stub: StubServer, config: ClientConfig) -> None:
    stub.state.protocol_version = "2099-01-01"
    report = RemoteSession(config).bootstrap(deadline_s=5.0)
    assert not report.ok
    assert "protocol_version_mismatch" in report.reason
    assert stub.state.session_id == "", "a mismatched client must not open a session"


def test_the_client_offers_the_capability_the_server_requires(stub: StubServer, bootstrapped: RemoteSession) -> None:
    assert "blob_miss.v1" in bootstrapped.state.capabilities
    assert "result_reuse.v1" in bootstrapped.state.capabilities


def test_a_missing_credential_is_reported_rather_than_raised(stub: StubServer, worktree: Path, state_dir: Path) -> None:
    from conftest import make_config

    config = make_config(url=stub.url, worktree=worktree, state_dir=state_dir, token="")
    report = RemoteSession(config).bootstrap(deadline_s=5.0)
    assert not report.ok
    assert "unauthenticated" in report.reason
    assert "LEMONCROW_TOKEN" in report.reason


def test_a_provisioned_token_file_is_used_and_a_readable_one_is_refused(
    stub: StubServer, worktree: Path, state_dir: Path
) -> None:
    import os

    from _stub import TOKEN
    from conftest import make_config

    target = state_dir / "token"
    target.write_text(TOKEN, encoding="utf-8")
    os.chmod(target, 0o600)
    config = make_config(url=stub.url, worktree=worktree, state_dir=state_dir, token="")
    assert config.token == TOKEN
    assert config.token_source == str(target)

    os.chmod(target, 0o644)
    with pytest.raises(ClientError) as caught:
        make_config(url=stub.url, worktree=worktree, state_dir=state_dir, token="")
    assert "chmod 600" in caught.value.message


def test_repository_identity_never_carries_a_git_remote(config: ClientConfig) -> None:
    from lemoncrow_client.config import RepoIdentity, repo_identity

    claim = repo_identity(config.repo_root, {})
    assert isinstance(claim, RepoIdentity)
    assert set(claim.to_wire()) <= {"scm_provider", "scm_repo_id", "fingerprint"}
    assert claim.fingerprint


# --------------------------------------------------------------------------- #
# host runtime attribution metadata                                           #
# --------------------------------------------------------------------------- #


def test_non_claude_workspace_bridge_becomes_runtime_headers(
    config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "LEMONCROW_AGENT",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SESSION_ID",
        "OPENCODE_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    bridge = config.repo_root / ".lemoncrow" / "workspace" / "session_state.json"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text(
        '{"session_id":"codex-native-1","host":"codex","model":"gpt-5.6-sol"}',
        encoding="utf-8",
    )

    assert _host_runtime_headers(config) == {
        "X-LemonCrow-Host-Session": "codex-native-1",
        "X-LemonCrow-Host": "codex",
        "X-LemonCrow-Model": "gpt-5.6-sol",
    }


def test_claude_workspace_bridge_is_not_adopted_without_window_session_id(
    config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("LEMONCROW_AGENT", raising=False)
    bridge = config.repo_root / ".lemoncrow" / "workspace" / "session_state.json"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text(
        '{"session_id":"claude-last-writer","host":"claude","model":"claude-sonnet-5"}',
        encoding="utf-8",
    )

    assert _host_runtime_headers(config) == {}


# --------------------------------------------------------------------------- #
# same-host proof metadata                                                    #
# --------------------------------------------------------------------------- #


def test_local_fs_proof_never_enters_the_source_manifest(stub: StubServer, worktree: Path, state_dir: Path) -> None:
    from conftest import make_config
    from lemoncrow_client.config import LOCAL_FS_PROOF_PATH

    config = make_config(
        url=stub.url,
        worktree=worktree,
        state_dir=state_dir,
        LEMONCROW_LOCAL_FS="1",
    )
    session = RemoteSession(config)
    assert session._write_proof("proof-nonce-0123456789") is True

    walked = walk_worktree(worktree, size_cap=stub.state.content_size_cap)
    assert LOCAL_FS_PROOF_PATH not in {item.path for item in walked.files}


def test_local_fs_proof_is_excluded_from_git_status_without_editing_gitignore(
    stub: StubServer, worktree: Path, state_dir: Path
) -> None:
    from conftest import make_config
    from lemoncrow_client.config import LOCAL_FS_PROOF_PATH

    config = make_config(
        url=stub.url,
        worktree=worktree,
        state_dir=state_dir,
        LEMONCROW_LOCAL_FS="1",
    )
    session = RemoteSession(config)
    assert session._write_proof("proof-nonce-0123456789") is True

    exclude = worktree / ".git" / "info" / "exclude"
    assert f"/{LOCAL_FS_PROOF_PATH}" in exclude.read_text(encoding="utf-8")
    assert LOCAL_FS_PROOF_PATH not in (worktree / ".gitignore").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# views/open and the manifest                                                 #
# --------------------------------------------------------------------------- #


def test_a_cold_open_sends_the_manifest_and_exactly_the_missing_content(
    stub: StubServer, session: RemoteSession, worktree: Path
) -> None:
    report = session.bootstrap(deadline_s=10.0)
    assert report.ok, report.reason
    expected = walk_worktree(worktree, size_cap=stub.state.content_size_cap)
    assert stub.state.manifest_root == expected.root()
    assert set(stub.state.manifest) == {file.path for file in expected.files}
    assert set(stub.state.blobs) == {file.content_digest for file in expected.uploadable()}
    assert report.manifest_chunks_sent == 1
    assert report.uploaded_blobs == len(stub.state.blobs)
    assert session.state.repo_id == "repo_stub"


def test_hosted_review_capture_uses_an_exact_virtual_snapshot(
    stub: StubServer, session: RemoteSession, tmp_path: Path
) -> None:
    historical = {"src/app.py": "print('historical snapshot')\n"}
    binary = {"assets/logo.bin": b"\x00\x01\xff\xfe"}
    snapshot = tmp_path / "review-snapshot"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "assets").mkdir(parents=True)
    (snapshot / "src/app.py").write_text(historical["src/app.py"], encoding="utf-8")
    (snapshot / "assets/logo.bin").write_bytes(binary["assets/logo.bin"])
    (snapshot / "README.md").write_text("unchanged but runtime-required\n", encoding="utf-8")

    session.handshake(timeout_s=10.0)
    session.open_session(timeout_s=10.0)
    opened = session.open_review_snapshot(
        snapshot,
        source_revision="historical-deadbeef",
        timeout_s=10.0,
    )

    assert opened["repo_id"] == "repo_stub"
    assert set(stub.state.manifest) == {"src/app.py", "assets/logo.bin", "README.md"}
    row = stub.state.manifest["src/app.py"]
    assert row["content_digest"] == sha256_hex(historical["src/app.py"].encode())
    assert stub.state.content_for("src/app.py") == historical["src/app.py"].encode()
    binary_row = stub.state.manifest["assets/logo.bin"]
    assert binary_row["content_digest"] == sha256_hex(binary["assets/logo.bin"])
    assert stub.state.content_for("assets/logo.bin") == binary["assets/logo.bin"]

    answer = session.capture_review(
        packet={"title": "Change", "files": []},
        new_blobs=historical,
        source_ref="workspace:change-one",
        title="Change",
    )

    assert answer["review"]["id"] == "rev_hosted_stub"
    assert len(stub.state.review_captures) == 1
    body = stub.state.review_captures[0]
    assert body["view_id"] == session.state.view_id
    assert body["view_revision"] == session.state.view_revision
    assert body["source_ref"] == "workspace:change-one"
    assert body["packet"] == {"title": "Change", "files": []}
    assert body["new_blobs"] == historical


def test_review_snapshot_propagates_explicit_timeout_to_manifest_and_blob_uploads(
    stub: StubServer,
    session: RemoteSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "review-snapshot"
    snapshot.mkdir()
    (snapshot / "a.txt").write_text("a\n", encoding="utf-8")
    seen: list[tuple[str, float | None]] = []
    original_post = HttpTransport.post

    def recording_post(self: HttpTransport, path: str, **kwargs: Any):
        if path.endswith("/manifest-chunks") or path == "/v1/blobs":
            seen.append((path, kwargs.get("timeout_s")))
        return original_post(self, path, **kwargs)

    monkeypatch.setattr(HttpTransport, "post", recording_post)
    session.handshake(timeout_s=10.0)
    session.open_session(timeout_s=10.0)
    session.open_review_snapshot(snapshot, source_revision="rev", timeout_s=37.0)

    assert seen
    assert all(timeout == 37.0 for _, timeout in seen)


def test_review_snapshot_uploads_files_above_index_cap_up_to_blob_limit(
    stub: StubServer, session: RemoteSession, tmp_path: Path
) -> None:
    snapshot = tmp_path / "review-snapshot"
    snapshot.mkdir()
    (snapshot / "small.txt").write_text("small\n", encoding="utf-8")
    big = b"x" * (session.state.content_size_cap + 1)
    (snapshot / "big.bin").write_bytes(big)

    session.handshake(timeout_s=10.0)
    session.open_session(timeout_s=10.0)
    session.open_review_snapshot(snapshot, source_revision="rev", timeout_s=10.0)

    assert set(stub.state.manifest) == {"small.txt", "big.bin"}
    assert stub.state.content_for("small.txt") == b"small\n"
    # Review proactively stores bytes above the ordinary indexing cap because
    # runtime reconstruction needs them, up to the actual blob transport limit.
    assert stub.state.content_for("big.bin") == big


def test_review_snapshot_refuses_symlinks_instead_of_silently_changing_runtime_tree(
    stub: StubServer, session: RemoteSession, tmp_path: Path
) -> None:
    snapshot = tmp_path / "review-snapshot"
    snapshot.mkdir()
    (snapshot / "target.txt").write_text("target\n", encoding="utf-8")
    (snapshot / "alias.txt").symlink_to("target.txt")

    session.handshake(timeout_s=10.0)
    session.open_session(timeout_s=10.0)
    with pytest.raises(ClientError) as caught:
        session.open_review_snapshot(snapshot, source_revision="rev", timeout_s=10.0)

    assert caught.value.code is ErrorCode.PAYLOAD_INVALID
    assert caught.value.details["symlinks"] == 1


def test_review_snapshot_refuses_files_above_blob_transport_limit(
    stub: StubServer, session: RemoteSession, tmp_path: Path
) -> None:
    snapshot = tmp_path / "review-snapshot"
    snapshot.mkdir()
    (snapshot / "too-big.bin").write_bytes(b"x" * (session.state.max_blob_bytes + 1))

    session.handshake(timeout_s=10.0)
    session.open_session(timeout_s=10.0)
    with pytest.raises(ClientError) as caught:
        session.open_review_snapshot(snapshot, source_revision="rev", timeout_s=10.0)

    assert caught.value.code is ErrorCode.PAYLOAD_INVALID
    assert "cannot be uploaded completely" in caught.value.message
    assert caught.value.details["max_blob_bytes"] == session.state.max_blob_bytes


def _review_packet(count: int) -> dict[str, Any]:
    return {
        "title": "Big change",
        "files": [{"path": f"src/module_{index:03}.py", "hunks": ["x" * 120]} for index in range(count)],
    }


def _open_capture_snapshot(session: RemoteSession, tmp_path: Path) -> None:
    snapshot = tmp_path / "review-snapshot"
    snapshot.mkdir()
    (snapshot / "a.txt").write_text("a\n", encoding="utf-8")
    session.handshake(timeout_s=10.0)
    session.open_session(timeout_s=10.0)
    session.open_review_snapshot(snapshot, source_revision="rev", timeout_s=10.0)


def test_review_capture_that_fits_is_one_request(stub: StubServer, session: RemoteSession, tmp_path: Path) -> None:
    _open_capture_snapshot(session, tmp_path)
    session.capture_review(packet=_review_packet(3), new_blobs={}, source_ref="ref")

    assert len(stub.state.review_requests) == 1
    assert "draft" not in stub.state.review_requests[0][1]


def test_review_capture_over_the_limit_is_draft_appends_and_a_final_append(
    stub: StubServer, session: RemoteSession, tmp_path: Path
) -> None:
    _open_capture_snapshot(session, tmp_path)
    session.state.max_request_bytes = 4096
    packet = _review_packet(40)
    answer = session.capture_review(packet=packet, new_blobs={}, source_ref="ref")

    requests = stub.state.review_requests
    assert len(requests) >= 3
    assert answer["review"]["id"] == "rev_hosted_stub"
    first_path, first = requests[0]
    assert first_path.endswith("/reviews") and first["draft"] is True
    appends = requests[1:]
    assert all(path.endswith("/reviews/drafts/rdraft_stub") for path, _ in appends)
    assert [body["seq"] for _, body in appends] == list(range(1, len(requests)))
    assert [body["final"] for _, body in appends] == [False] * (len(appends) - 1) + [True]
    sent = [*first["packet"]["files"], *(item for _, body in appends for item in body["files"])]
    assert sent == packet["files"]
    for _, body in requests:
        assert _json_size(body) <= 4096


def test_review_capture_refuses_one_entry_larger_than_a_request(
    stub: StubServer, session: RemoteSession, tmp_path: Path
) -> None:
    _open_capture_snapshot(session, tmp_path)
    session.state.max_request_bytes = 4096
    packet = {"title": "t", "files": [{"path": "huge.py", "hunks": ["x" * 9000]}]}

    with pytest.raises(ClientError) as caught:
        session.capture_review(packet=packet, new_blobs={}, source_ref="ref")

    assert caught.value.code is ErrorCode.REQUEST_TOO_LARGE
    assert caught.value.details["path"] == "huge.py"
    assert stub.state.review_requests == []


def test_a_manifest_only_open_sends_no_repository_content(
    stub: StubServer, session: RemoteSession, worktree: Path
) -> None:
    session.handshake(timeout_s=10.0)
    session.open_session(timeout_s=10.0)
    opened = session.open_view(timeout_s=10.0, upload_missing_content=False)

    expected = walk_worktree(worktree, size_cap=stub.state.content_size_cap)
    assert set(stub.state.manifest) == {file.path for file in expected.files}
    assert stub.state.blobs == {}
    assert opened["uploaded_blobs"] == 0
    assert opened["uploaded_bytes"] == 0
    assert opened["content_sync_skipped"] is True


def test_a_warm_open_resends_no_manifest_rows_and_no_content(
    stub: StubServer, session: RemoteSession, config: ClientConfig
) -> None:
    """ "Large unchanged repositories must not resend thousands of rows.\" """
    assert session.bootstrap(deadline_s=10.0).ok
    uploads_after_cold = stub.state.blob_requests
    chunks_after_cold = len(stub.state.chunks_received)

    warm = RemoteSession(config).bootstrap(deadline_s=10.0)
    assert warm.ok, warm.reason
    assert warm.warm is True
    assert warm.manifest_chunks_sent == 0
    assert len(stub.state.chunks_received) == chunks_after_cold
    assert stub.state.blob_requests == uploads_after_cold


def test_a_file_over_the_servers_cap_is_manifested_but_never_uploaded(
    stub: StubServer, worktree: Path, state_dir: Path
) -> None:
    from conftest import make_config

    stub.state.content_size_cap = 64
    (worktree / "big.txt").write_bytes(b"y" * 512)
    config = make_config(url=stub.url, worktree=worktree, state_dir=state_dir)
    assert RemoteSession(config).bootstrap(deadline_s=10.0).ok
    assert "big.txt" in stub.state.manifest
    assert sha256_hex(b"y" * 512) not in stub.state.blobs


def test_uploads_are_batched_within_the_servers_declared_ceiling(
    stub: StubServer, worktree: Path, state_dir: Path
) -> None:
    """The client obeys the server's declared limits rather than its own."""
    from conftest import make_config

    stub.state.max_blobs_per_request = 4
    for index in range(12):
        (worktree / f"file{index}.txt").write_bytes(f"content {index}\n".encode())
    config = make_config(url=stub.url, worktree=worktree, state_dir=state_dir)
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=10.0).ok
    assert session.state.max_blobs_per_request == 4
    uploaded = len(stub.state.blobs)
    assert uploaded > 4
    assert stub.state.blob_requests >= (uploaded + 3) // 4


# --------------------------------------------------------------------------- #
# The revision contract                                                       #
# --------------------------------------------------------------------------- #


def test_every_tool_call_names_the_revision_it_observed(stub: StubServer, bootstrapped: RemoteSession) -> None:
    bootstrapped.call_tool("code_search", {"query": "alpha"})
    assert stub.state.tool_calls[-1][2] == bootstrapped.view_revision


def test_repeat_resolution_is_server_validated_without_redispatch(
    stub: StubServer, bootstrapped: RemoteSession
) -> None:
    first = bootstrapped.call_tool("code_search", {"query": "alpha"})
    before = len(stub.state.tool_calls)
    second = bootstrapped.call_tool("code_search", {"query": "alpha"})
    assert second.content == first.content
    assert len(stub.state.tool_calls) == before
    assert stub.state.reuse_hits == 1
    assert bootstrapped.cache_stats.hits >= 1


def test_digest_ack_cache_skips_missing_round_trip_on_revert(
    stub: StubServer, bootstrapped: RemoteSession, worktree: Path
) -> None:
    target = worktree / "pkg" / "alpha.py"
    original = target.read_bytes()
    target.write_text("def alpha():\n    return 'changed'\n", encoding="utf-8")
    bootstrapped.push_paths(("pkg/alpha.py",))
    after_change = stub.state.blob_requests
    target.write_bytes(original)
    bootstrapped.push_paths(("pkg/alpha.py",))
    assert stub.state.blob_requests == after_change


def test_an_edit_commits_a_revision_before_the_result_returns(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig
) -> None:
    before = bootstrapped.view_revision
    outcome = Dispatcher(config, bootstrapped).call(
        "edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'EDITED'"}]}
    )
    assert not outcome.is_error, outcome.content
    assert outcome.view_revision == before + 1
    assert bootstrapped.view_revision == before + 1


def test_a_read_after_an_edit_returns_the_edited_content(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig
) -> None:
    """The whole point of the revision contract, asserted end to end."""
    dispatcher = Dispatcher(config, bootstrapped)
    first = dispatcher.call("read", {"files": ["pkg/alpha.py"]})
    assert "'alpha'" in first.content[0]["text"]

    dispatcher.call("edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'EDITED'"}]})
    second = dispatcher.call("read", {"files": ["pkg/alpha.py"]})
    assert "'EDITED'" in second.content[0]["text"]
    assert second.view_revision > first.view_revision


def test_workspace_fingerprint_disables_optional_git_index_writes(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"# branch.oid 0123456789abcdef\0")

    monkeypatch.setattr(session_mod.subprocess, "run", fake_run)

    assert session_mod._workspace_state_fingerprint(worktree)
    env = seen.get("env")
    assert isinstance(env, dict)
    assert env.get("GIT_OPTIONAL_LOCKS") == "0"


def test_server_read_refreshes_after_an_out_of_band_edit(
    stub: StubServer, bootstrapped: RemoteSession, worktree: Path
) -> None:
    """A long-lived MCP session must not keep serving its bootstrap View forever."""
    first = bootstrapped.call_tool("read", {"files": ["pkg/alpha.py"]})
    assert "'alpha'" in first.content[0]["text"]

    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 'OUT_OF_BAND'\n", encoding="utf-8")
    second = bootstrapped.call_tool("read", {"files": ["pkg/alpha.py"]})

    assert "'OUT_OF_BAND'" in second.content[0]["text"]
    assert stub.state.content_for("pkg/alpha.py") == b"def alpha():\n    return 'OUT_OF_BAND'\n"


def test_server_view_drops_a_file_that_becomes_ignored_out_of_band(
    stub: StubServer, bootstrapped: RemoteSession, worktree: Path
) -> None:
    target = worktree / "pkg" / "external.tmp"
    target.write_text("visible first\n", encoding="utf-8")
    bootstrapped.call_tool("read", {"files": ["pkg/external.tmp"]})
    assert "pkg/external.tmp" in stub.state.manifest

    with (worktree / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("*.tmp\n")
    bootstrapped.call_tool("code_search", {"query": "visible"})
    assert "pkg/external.tmp" not in stub.state.manifest


def test_a_deleted_file_is_tombstoned_in_the_view(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, worktree: Path
) -> None:
    (worktree / "docs" / "notes.md").unlink()
    bootstrapped.push_paths(("docs/notes.md",))
    assert "docs/notes.md" not in stub.state.manifest


def test_client_seq_increases_monotonically_across_pushes(
    stub: StubServer, bootstrapped: RemoteSession, worktree: Path
) -> None:
    seqs = []
    for index in range(3):
        (worktree / "pkg" / "alpha.py").write_text(f"x = {index}\n", encoding="utf-8")
        bootstrapped.push_paths(("pkg/alpha.py",))
        seqs.append(bootstrapped.state.client_seq)
    assert seqs == sorted(set(seqs)) and len(seqs) == 3


def test_lost_overlay_ack_replays_same_sequence_without_duplicate_mutation(
    stub: StubServer,
    bootstrapped: RemoteSession,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed write whose ACK is lost must be replayed, not re-applied."""

    from lemoncrow_client.errors import AgentAction

    transport = bootstrapped._transport
    transport_type = type(transport)
    original_post = transport_type.post
    overlay_bodies: list[dict[str, Any]] = []
    lost_once = False

    def lose_first_overlay_ack(self, path: str, **kwargs: Any):
        nonlocal lost_once
        if self is transport and path.endswith("/overlay"):
            body = dict(kwargs.get("body") or {})
            overlay_bodies.append(body)
            response = original_post(self, path, **kwargs)
            if not lost_once:
                lost_once = True
                # The server has already committed; only the response disappears.
                raise ClientError(
                    ErrorCode.SERVER_UNREACHABLE,
                    "simulated lost acknowledgement",
                    retryable=True,
                    action=AgentAction.RETRY_LATER,
                )
            return response
        return original_post(self, path, **kwargs)

    monkeypatch.setattr(transport_type, "post", lose_first_overlay_ack)
    before_revision = stub.state.view_revision
    before_seq = bootstrapped.state.client_seq
    (worktree / "pkg" / "alpha.py").write_text("x = 'LOST_ACK'\\n", encoding="utf-8")

    revision = bootstrapped.push_paths(("pkg/alpha.py",))

    assert revision == before_revision + 1
    assert stub.state.view_revision == before_revision + 1
    assert bootstrapped.view_revision == before_revision + 1
    assert bootstrapped.state.client_seq == before_seq + 1
    assert len(overlay_bodies) == 2
    assert overlay_bodies[0] == overlay_bodies[1]
    assert overlay_bodies[0]["client_seq"] == before_seq + 1
    assert stub.state.content_for("pkg/alpha.py") == b"x = 'LOST_ACK'\\n"


def test_overlay_retry_after_precommit_disconnect_commits_exactly_once(
    stub: StubServer,
    bootstrapped: RemoteSession,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry is also safe when the first request never reached the server."""

    from lemoncrow_client.errors import AgentAction

    transport = bootstrapped._transport
    transport_type = type(transport)
    original_post = transport_type.post
    overlay_bodies: list[dict[str, Any]] = []
    failed_once = False

    def fail_before_first_overlay(self, path: str, **kwargs: Any):
        nonlocal failed_once
        if self is transport and path.endswith("/overlay"):
            body = dict(kwargs.get("body") or {})
            overlay_bodies.append(body)
            if not failed_once:
                failed_once = True
                raise ClientError(
                    ErrorCode.SERVER_UNREACHABLE,
                    "simulated disconnect before commit",
                    retryable=True,
                    action=AgentAction.RETRY_LATER,
                )
        return original_post(self, path, **kwargs)

    monkeypatch.setattr(transport_type, "post", fail_before_first_overlay)
    before_revision = stub.state.view_revision
    before_seq = bootstrapped.state.client_seq
    (worktree / "pkg" / "alpha.py").write_text("x = 'PRECOMMIT_RETRY'\n", encoding="utf-8")

    revision = bootstrapped.push_paths(("pkg/alpha.py",))

    assert revision == before_revision + 1
    assert stub.state.view_revision == before_revision + 1
    assert bootstrapped.state.client_seq == before_seq + 1
    assert len(overlay_bodies) == 2
    assert overlay_bodies[0] == overlay_bodies[1]
    assert overlay_bodies[0]["client_seq"] == before_seq + 1
    assert stub.state.content_for("pkg/alpha.py") == b"x = 'PRECOMMIT_RETRY'\n"
    events = [event for event in bootstrapped.state.recovery_events if event.kind == "write_replay"]
    assert [event.outcome for event in events[-2:]] == ["retrying", "recovered"]


def test_a_stale_revision_is_refreshed_once_rather_than_failing_the_call(
    stub: StubServer, bootstrapped: RemoteSession
) -> None:
    """A lost ACK leaves the client behind; one refresh recovers it."""
    stub.state.view_revision += 1  # a commit the client never saw the ACK for
    answer = bootstrapped.call_tool("code_search", {"query": "alpha"})
    assert answer.view_revision == stub.state.view_revision
    assert bootstrapped.view_revision == stub.state.view_revision
    events = [event for event in bootstrapped.state.recovery_events if event.kind == "revision_retry"]
    assert [event.outcome for event in events[-2:]] == ["retrying", "recovered"]
    assert events[-1].to_revision == stub.state.view_revision


def test_a_revision_the_server_has_not_committed_is_refused(stub: StubServer, bootstrapped: RemoteSession) -> None:
    bootstrapped.state.view_revision = stub.state.view_revision + 5
    with pytest.raises(ClientError) as caught:
        bootstrapped.call_tool("code_search", {"query": "alpha"})
    assert caught.value.code is ErrorCode.VIEW_REVISION_UNACKNOWLEDGED


# --------------------------------------------------------------------------- #
# The blob miss                                                               #
# --------------------------------------------------------------------------- #


def test_a_blob_miss_is_filled_in_the_same_turn_and_retried_once(
    stub: StubServer,
    bootstrapped: RemoteSession,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (worktree / "pkg" / "late.py").write_text("def late():\n    return 1\n", encoding="utf-8")
    stub.state.miss_once["read"] = ("pkg/late.py",)
    # Isolate the server-pull recovery path from the independent proactive
    # workspace-fingerprint refresh, which can synchronize this file first.
    monkeypatch.setattr(RemoteSession, "_refresh_view_if_source_changed", lambda self, fingerprint: False)

    answer = bootstrapped.call_tool("read", {"files": ["pkg/late.py"]})
    assert "def late" in answer.content[0]["text"]
    assert "pkg/late.py" in stub.state.manifest
    assert len([call for call in stub.state.tool_calls if call[0] == "read"]) == 1
    events = [event for event in bootstrapped.state.recovery_events if event.kind == "blob_fill"]
    assert [event.outcome for event in events[-2:]] == ["retrying", "recovered"]
    assert events[-1].tool == "read"
    assert events[-1].count == 1


def test_a_second_unfilled_miss_is_a_typed_refusal_not_a_loop(stub: StubServer, bootstrapped: RemoteSession) -> None:
    """The server asks once; a client that loops turns an answer into a hang."""
    stub.state.miss_always["read"] = ("pkg/never.py",)
    with pytest.raises(ClientError) as caught:
        bootstrapped.call_tool("read", {"files": ["pkg/never.py"]})
    assert caught.value.code is ErrorCode.BLOB_MISSING
    assert caught.value.action.value == "upload_blobs"
    assert not caught.value.retryable


def test_a_path_the_client_cannot_address_is_refused_rather_than_joined(
    bootstrapped: RemoteSession,
) -> None:
    with pytest.raises(ClientError) as caught:
        bootstrapped.blobs.fill(["/etc/passwd", "../../escape"])
    assert caught.value.code is ErrorCode.BLOB_MISSING


# --------------------------------------------------------------------------- #
# Close                                                                       #
# --------------------------------------------------------------------------- #


def test_close_is_best_effort_and_never_raises(stub: StubServer, bootstrapped: RemoteSession) -> None:
    stub.state.dead = True
    bootstrapped.close()  # a lease sweeps what this misses, by design


def test_recovery_event_history_is_bounded_and_content_free(bootstrapped: RemoteSession) -> None:
    for index in range(160):
        bootstrapped.state.record_recovery(
            "revision_retry",
            "recovered",
            error_code=ErrorCode.VIEW_REVISION_STALE.value,
            tool="read",
            from_revision=index,
            to_revision=index + 1,
        )

    events = bootstrapped.state.recovery_events
    assert len(events) == 128
    assert events[0].from_revision == 32
    assert events[-1].to_revision == 160
    assert all(not hasattr(event, "arguments") and not hasattr(event, "path") for event in events)
