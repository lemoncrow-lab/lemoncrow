from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _stub import StubServer
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.review_session import ReviewSession


def test_review_session_uploads_exact_snapshots_and_captures_from_views(
    stub: StubServer,
    config: ClientConfig,
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    head = tmp_path / "head"
    (base / "pkg").mkdir(parents=True)
    (head / "pkg").mkdir(parents=True)
    (base / "pkg" / "alpha.py").write_text("def alpha():\n    return 'before'\n", encoding="utf-8")
    (head / "pkg" / "alpha.py").write_text("def alpha():\n    return 'after'\n", encoding="utf-8")

    session = ReviewSession(config)
    try:
        handshake = session.handshake(timeout_s=5.0)
        assert handshake["protocol_version"]
        session.open_session(timeout_s=5.0)
        base_opened = session.open_snapshot(base, source_revision="base-review", timeout_s=5.0)
        head_opened = session.open_snapshot(head, source_revision="head-review", timeout_s=5.0)
        assert base_opened["repo_id"] == head_opened["repo_id"] == session.state.repo_id

        captured = session.capture_from_views(
            repo_id=session.state.repo_id,
            source_ref="review:test",
            title="Review session test",
            base_view_id=str(base_opened["view_id"]),
            base_view_revision=int(base_opened["view_revision"]),
            with_impact=False,
            range_spec={
                "mode": "working_tree",
                "base_rev": "HEAD",
                "head_rev": "WORKDIR",
                "base_sha": "a" * 40,
                "head_sha": "",
                "merge_base_sha": "",
                "dirty": True,
                "title": "Review session test",
            },
            timeout_s=5.0,
        )
        assert captured["review"]["id"] == "rev_hosted_stub"
        request = stub.state.review_captures[-1]
        assert request["derive_packet"] is True
        assert request["view_id"] == head_opened["view_id"]
        assert request["base_view_id"] == base_opened["view_id"]
    finally:
        session.close()


def test_importing_review_does_not_import_runtime_modules() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(package_root)
    code = r"""
import json
import sys
import lemoncrow_client.review
forbidden = {
    "lemoncrow_client.session",
    "lemoncrow_client.mcpserver",
    "lemoncrow_client.dispatcher",
    "lemoncrow_client.routing",
    "lemoncrow_client.autosync",
    "lemoncrow_client.trace",
    "lemoncrow_client.liveview",
    "lemoncrow_client.localtools",
}
loaded = sorted(name for name in forbidden if name in sys.modules)
print(json.dumps(loaded))
raise SystemExit(1 if loaded else 0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "[]"


def test_review_proves_original_checkout_without_changing_snapshot_bytes(
    stub: StubServer, config: ClientConfig, tmp_path: Path
) -> None:
    from dataclasses import replace

    from lemoncrow_client.workspace_paths import LOCAL_FS_PROOF_PATH

    stub.state.allow_local_fs = True
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "README.md").write_text("frozen source\n")
    session = ReviewSession(replace(config, offer_local_fs=True))
    try:
        session.open_session()
        session.open_snapshot(snapshot, source_revision="frozen")
        assert stub.state.local_fs_offer == {
            "root": str(config.repo_root.resolve()),
            "proof_path": LOCAL_FS_PROOF_PATH,
        }
        assert (config.repo_root / LOCAL_FS_PROOF_PATH).read_text() == session.state.local_fs_proof
        assert not (snapshot / LOCAL_FS_PROOF_PATH).exists()
        assert LOCAL_FS_PROOF_PATH not in stub.state.manifest
        digest = stub.state.manifest["README.md"]["content_digest"]
        assert stub.state.blobs[digest] == b"frozen source\n"
        status = subprocess.run(
            ["git", "-C", str(config.repo_root), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert LOCAL_FS_PROOF_PATH not in status.stdout
    finally:
        session.close()


def test_review_does_not_offer_checkout_access_without_opt_in(
    stub: StubServer, config: ClientConfig, tmp_path: Path
) -> None:
    from dataclasses import replace

    from lemoncrow_client.workspace_paths import LOCAL_FS_PROOF_PATH

    stub.state.allow_local_fs = True
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "README.md").write_text("frozen source\n")
    session = ReviewSession(replace(config, offer_local_fs=False))
    try:
        session.open_session()
        session.open_snapshot(snapshot, source_revision="frozen")
        assert stub.state.local_fs_offer is None
        assert not (config.repo_root / LOCAL_FS_PROOF_PATH).exists()
    finally:
        session.close()
