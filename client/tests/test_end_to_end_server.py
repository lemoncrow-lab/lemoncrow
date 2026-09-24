"""The thin client against the real enterprise server, over a real socket.

Everything else in this suite runs against a stub that can be pushed into
failure modes the real server will not produce on demand. This module is the
opposite: the actual ``enterprise/server`` process, bound to loopback, driven
by the actual client, with nothing mocked between them. If the two halves of
the protocol disagree, this is where it shows.

The scenario is the one the design cares about, in order: **open a view, upload,
search, read, edit, re-read** -- and the assertion that matters is that the
re-read returns the edited bytes *because* it named the revision the edit
committed, not because enough time passed.

It runs twice, with the same-host ``local_fs`` capability off and on, and
asserts the answers are identical: an optimization that changed a result would
not be an optimization. The only differences allowed are how many bytes the
client uploaded.

Run it:

    cd enterprise/server && PYTHONPATH=../../client/src .venv/bin/python -m pytest \\
        ../../client/tests/test_end_to_end_server.py -q
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _serverpkg import REASON, server_available

pytestmark = [
    pytest.mark.enterprise_server,
    pytest.mark.skipif(not server_available(), reason=REASON),
]

TOKEN = "e2e-token-0123456789abcdef0123"
ORG = "org_e2e"


# --------------------------------------------------------------------------- #
# The 11D join, written here so the real index answers the real tools          #
# --------------------------------------------------------------------------- #


class IndexBackedDispatcher:
    """Answers ``read`` and ``code_search`` out of the real three-layer index.

    The server routes tool calls into the public ``lemoncrow`` registry, which
    this checkout's server venv deliberately does not carry, and joining the
    four index-answerable tools to ``/v1/views/{id}/query`` is 11D's transition
    rather than this increment's. So the join is written here, against the same
    contracts 11D will use, which makes this a real end-to-end test of the
    client over a real index -- not a canned answer.
    """

    def __init__(self, backend: Any, *, content_size_cap: int) -> None:
        from lemoncrow_server_core.index.query import IndexQuery

        self._backend = backend
        self._query = IndexQuery(backend, content_size_cap=content_size_cap)

    @property
    def available(self) -> bool:
        return True

    @property
    def tools(self) -> frozenset[str]:
        from lemoncrow_server_core.matrix import SERVER_ADMIN_TOOLS, SERVER_TOOLS

        return SERVER_TOOLS | SERVER_ADMIN_TOOLS

    def dispatch(self, invocation: Any) -> Any:
        from lemoncrow_server_core.dispatch import ToolResult

        org_id = invocation.tenant.org_id
        view_id = invocation.tenant.view_id or ""
        if invocation.tool == "read":
            return ToolResult(tool="read", content=tuple(self._read(org_id, view_id, invocation.arguments)))
        if invocation.tool in {"code_search", "search", "relations"}:
            kind = {"code_search": "lexical", "search": "semantic", "relations": "relations"}[invocation.tool]
            answer = self._query.run(
                org_id=org_id,
                view_id=view_id,
                kind=kind,
                query=str(invocation.arguments.get("query") or ""),
                path=str(invocation.arguments.get("path") or ""),
                symbol=str(invocation.arguments.get("symbol") or ""),
                limit=20,
            )
            text = "\n".join(f"{hit.path}\t{hit.score:.4f}" for hit in answer.hits) or "[no hits]"
            return ToolResult(
                tool=invocation.tool,
                content=({"type": "text", "text": text},),
                structured={"hits": len(answer.hits), "kind": answer.kind},
                degraded=answer.degraded,
                degraded_reason=answer.degraded_reason,
            )
        return ToolResult(tool=invocation.tool, content=({"type": "text", "text": "ok"},))

    def _read(self, org_id: str, view_id: str, arguments: Mapping[str, Any]) -> list[dict[str, str]]:
        raw = arguments.get("files") or []
        entries = [raw] if isinstance(raw, str) else list(raw)
        blocks: list[dict[str, str]] = []
        for item in entries:
            spelled = item.get("path") if isinstance(item, Mapping) else str(item)
            path = str(spelled).split(":", 1)[0]
            entry = self._backend.views.entry(org_id, view_id, path)
            if entry is None:
                blocks.append({"type": "text", "text": f"## {path}\n[not in view]"})
                continue
            data = self._backend.content.get(org_id, entry.content_digest)
            body = "[content not held]" if data is None else data.decode("utf-8", errors="replace")
            blocks.append({"type": "text", "text": f"## {path}\n{body}"})
        return blocks


# --------------------------------------------------------------------------- #
# A real server on a real loopback socket                                     #
# --------------------------------------------------------------------------- #


@dataclass
class RunningServer:
    url: str
    backend: Any
    state: Any


class _Loop:
    """An event loop on its own thread, so the client can stay synchronous."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="e2e-loop")
        self.thread.start()

    def run(self, coroutine: Any, timeout: float = 30.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10.0)
        self.loop.close()


@pytest.fixture(params=[False, True], ids=["local_fs_off", "local_fs_on"])
def running_server(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RunningServer]:
    from lemoncrow_server.app import build_app
    from lemoncrow_server.auth import (
        RoleAuthorizer,
        StaticTokenAuthenticator,
        TokenRecord,
        token_digest,
    )
    from lemoncrow_server.config import ServerConfig
    from lemoncrow_server.identity import Role
    from lemoncrow_server.limits import Limits
    from lemoncrow_server.reviews import HostedReviewRepository
    from lemoncrow_server.server import EnterpriseServer
    from lemoncrow_server_core.index.lifecycle import Maintenance
    from lemoncrow_server_core.index.sqlite import build_sqlite_backend

    allow_local_fs = bool(request.param)
    config = ServerConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        limits=Limits(),
        allow_local_fs=allow_local_fs,
        maintenance_interval_s=0.0,
    )
    backend = build_sqlite_backend(
        tmp_path / "index.sqlite",
        content_size_cap=config.limits.max_indexed_content_bytes,
        inline_rebuild_budget=config.limits.inline_rebuild_budget,
    )
    dispatcher = IndexBackedDispatcher(backend, content_size_cap=config.limits.max_indexed_content_bytes)
    authenticator = StaticTokenAuthenticator(
        (
            TokenRecord(
                token_id="tok_e2e",
                token_sha256=token_digest(TOKEN),
                org_id=ORG,
                subject="user_e2e",
                roles=frozenset({Role.DEVELOPER}),
            ),
        )
    )
    state, app = build_app(
        config=config,
        dispatcher=dispatcher,
        backend=backend,
        authenticator=authenticator,
        authorizer=RoleAuthorizer(),
        maintenance=Maintenance(backend, retention_s=config.blob_retention_s),
        reviews=HostedReviewRepository(tmp_path / "reviews", backend.content),
    )
    server = EnterpriseServer(state, app, config)
    loop = _Loop()
    url = loop.run(server.start())
    try:
        yield RunningServer(url=url, backend=backend, state=state)
    finally:
        loop.run(server.stop())
        loop.close()


@pytest.fixture
def e2e_worktree(worktree: Path, running_server: RunningServer) -> Path:
    """The worktree, with the same-host proof file ignored.

    The proof is written into the tree before the walk, so a repository that did
    not ignore it would produce a different manifest root with the capability on
    than with it off -- which would make the two runs incomparable for a reason
    that has nothing to do with the capability.
    """
    from lemoncrow_client.config import LOCAL_FS_PROOF_PATH

    ignore = worktree / ".gitignore"
    ignore.write_text(ignore.read_text(encoding="utf-8") + f"{LOCAL_FS_PROOF_PATH}\n", encoding="utf-8")
    return worktree


def _client_config(server: RunningServer, worktree: Path, state_dir: Path) -> Any:
    from lemoncrow_client.config import load_config

    return load_config(
        {
            "LEMONCROW_URL": server.url,
            "LEMONCROW_TOKEN": TOKEN,
            "LEMONCROW_HOME": str(state_dir),
            "HOME": str(state_dir.parent),
            "LEMONCROW_LOCAL_FS": "1",
            "LEMONCROW_REQUEST_TIMEOUT_S": "30",
            "LEMONCROW_TOOL_TIMEOUT_S": "60",
        },
        cwd=worktree,
    )


# --------------------------------------------------------------------------- #
# The scenario                                                                #
# --------------------------------------------------------------------------- #


def test_open_upload_search_read_edit_reread(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    """The whole contract, against the real server, in one test."""
    from lemoncrow_client.dispatcher import Dispatcher
    from lemoncrow_client.session import RemoteSession

    config = _client_config(running_server, e2e_worktree, state_dir)
    session = RemoteSession(config)

    # -- open a view, and upload what the server says it lacks ------------ #
    report = session.bootstrap(deadline_s=30.0)
    assert report.ok, report.reason
    assert report.session_id and report.view_id
    assert report.files == len(list(e2e_worktree.rglob("*.py"))) + 3  # md, md, .gitignore
    dispatcher = Dispatcher(config, session)

    # -- search: the index really indexed the uploaded content ------------ #
    found = dispatcher.call("code_search", {"query": "alpha"})
    assert not found.is_error, found.content
    assert "pkg/alpha.py" in found.content[0]["text"]
    assert found.structured is not None
    assert found.structured["kind"] == "lexical"

    # -- read: answered from the server's copy ---------------------------- #
    first = dispatcher.call("read", {"files": ["pkg/alpha.py"]})
    assert not first.is_error, first.content
    assert "return 'alpha'" in first.content[0]["text"]
    revision_before = first.view_revision

    # -- edit: writes locally, pushes, commits a revision ----------------- #
    edited = dispatcher.call("edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'EDITED-BY-E2E'"}]})
    assert not edited.is_error, edited.content
    assert edited.view_revision is not None
    assert edited.view_revision > (revision_before or 0)

    # -- re-read: the edit is visible, at the revision it committed ------- #
    second = dispatcher.call("read", {"files": ["pkg/alpha.py"]})
    assert not second.is_error, second.content
    assert "'EDITED-BY-E2E'" in second.content[0]["text"]
    assert second.view_revision == edited.view_revision

    # -- and the index moved with it -------------------------------------- #
    after = dispatcher.call("code_search", {"query": "EDITED"})
    assert "pkg/alpha.py" in after.content[0]["text"]

    session.close()


def test_hosted_review_snapshot_is_captured_by_the_real_server(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    """Hosted Review uses the exact packet snapshot, not the live checkout."""

    from lemoncrow_client.session import RemoteSession

    from lemoncrow.pro.capabilities.review.models import (
        SCHEMA_VERSION,
        ChangedFile,
        DiffHunk,
        ReviewOrderEntry,
        ReviewPacket,
    )

    config = _client_config(running_server, e2e_worktree, state_dir)
    session = RemoteSession(config)
    session.handshake(timeout_s=30.0)
    session.open_session(timeout_s=30.0)

    path = "pkg/alpha.py"
    snapshot_text = "def alpha():\n    return 'HISTORICAL-REVIEW-SNAPSHOT'\n"
    packet = ReviewPacket(
        schema_version=SCHEMA_VERSION,
        generated_at="2026-09-18T08:00:00+00:00",
        repo_root=str(e2e_worktree),
        range_mode="commit_range",
        base_rev="base",
        head_rev="head",
        base_sha="a" * 40,
        head_sha="b" * 40,
        dirty=False,
        title="Historical hosted review",
        files=(
            ChangedFile(
                path=path,
                old_path=None,
                status="modified",
                additions=1,
                deletions=1,
                language="python",
                hunks=(
                    DiffHunk(
                        old_start=1,
                        old_lines=2,
                        new_start=1,
                        new_lines=2,
                        header="@@ -1,2 +1,2 @@",
                        added=1,
                        removed=1,
                        new_ranges=((2, 2),),
                        old_ranges=((2, 2),),
                        patch=" def alpha():\n-    return 'alpha'\n+    return 'HISTORICAL-REVIEW-SNAPSHOT'\n",
                    ),
                ),
            ),
        ),
        order=(
            ReviewOrderEntry(
                path=path,
                rank=1,
                score=1.0,
                reasons=("changed production code",),
                group="production",
            ),
        ),
        stats={"files": 1, "additions": 1, "deletions": 1, "hunks": 1, "symbols": 0, "impact_sites": 0},
    )

    snapshot_root = state_dir / "exact-review-snapshot"
    snapshot_file = snapshot_root / path
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(snapshot_text, encoding="utf-8")
    opened = session.open_review_snapshot(
        snapshot_root,
        source_revision=packet.head_sha,
        timeout_s=30.0,
    )
    assert opened["repo_id"] == session.state.repo_id
    local_fs_granted = bool((opened.get("local_fs") or {}).get("granted"))
    assert local_fs_granted is bool(running_server.state.config.allow_local_fs)

    entry = running_server.backend.views.entry(ORG, session.state.view_id, path)
    assert entry is not None
    assert running_server.backend.content.get(ORG, entry.content_digest) == snapshot_text.encode()
    assert (e2e_worktree / path).read_text(encoding="utf-8") != snapshot_text

    captured = session.capture_review(
        packet=packet.to_dict(),
        new_blobs={path: snapshot_text},
        source_ref=f"{packet.base_sha}..{packet.head_sha}",
        title=packet.title,
    )
    review = captured.get("review")
    assert isinstance(review, Mapping)
    review_id = str(review.get("id") or "")
    assert re.fullmatch(r"[0-9a-f]{32}", review_id)
    assert running_server.state.reviews is not None
    store, stored_session = running_server.state.reviews.require_review(ORG, session.state.repo_id, review_id)
    assert stored_session.id == review_id
    latest = store.latest_revision(review_id)
    assert latest is not None
    assert latest.revision_number == 1
    if running_server.state.config.allow_local_fs:
        assert running_server.state._review_runtime_roots[(ORG, session.state.repo_id)] == e2e_worktree.resolve()
        binding = running_server.state._review_runtime_binding_path(ORG, session.state.repo_id)
        assert binding.is_file()
        running_server.state._review_runtime_roots.clear()
        assert running_server.state._review_runtime_root(ORG, session.state.repo_id) == e2e_worktree.resolve()
    else:
        assert (ORG, session.state.repo_id) not in running_server.state._review_runtime_roots
    session.close()


def test_thin_hosted_review_derives_packet_on_the_real_server(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    """A client-only Review publishes two Views; the server owns packet derivation."""

    from lemoncrow_client.session import RemoteSession

    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    config = _client_config(running_server, e2e_worktree, state_dir)
    session = RemoteSession(config)
    session.handshake(timeout_s=30.0)
    session.open_session(timeout_s=30.0)

    path = "pkg/alpha.py"
    base_root = state_dir / "thin-review-base"
    head_root = state_dir / "thin-review-head"
    base_file = base_root / path
    head_file = head_root / path
    base_file.parent.mkdir(parents=True, exist_ok=True)
    head_file.parent.mkdir(parents=True, exist_ok=True)
    base_file.write_text("def alpha():\n    return 'before'\n", encoding="utf-8")
    head_file.write_text("def alpha():\n    return 'after'\n", encoding="utf-8")

    base_opened = session.open_review_snapshot(base_root, source_revision="base-thin", timeout_s=30.0)
    head_opened = session.open_review_snapshot(head_root, source_revision="head-thin", timeout_s=30.0)
    assert base_opened["repo_id"] == head_opened["repo_id"] == session.state.repo_id

    captured = session.capture_review_from_views(
        repo_id=session.state.repo_id,
        base_view_id=str(base_opened["view_id"]),
        base_view_revision=int(base_opened["view_revision"]),
        source_ref="thin:e2e",
        title="Thin server-derived Review",
        with_impact=False,
        range_spec={
            "mode": "working_tree",
            "base_rev": "HEAD",
            "head_rev": "WORKDIR",
            "base_sha": "a" * 40,
            "head_sha": "",
            "merge_base_sha": "",
            "dirty": True,
            "title": "Thin server-derived Review",
        },
        timeout_s=30.0,
    )
    review = captured.get("review")
    assert isinstance(review, Mapping)
    review_id = str(review.get("id") or "")
    assert re.fullmatch(r"[0-9a-f]{32}", review_id)

    assert running_server.state.reviews is not None
    store, _stored_session = running_server.state.reviews.require_review(ORG, session.state.repo_id, review_id)
    latest = store.latest_revision(review_id)
    assert latest is not None
    packet = read_packet_json(store, latest)
    assert packet["range_mode"] == "working_tree"
    assert packet["base_sha"] == "a" * 40
    assert packet["title"] == "Thin server-derived Review"
    changed = packet["files"][0]
    assert changed["path"] == path
    patch = changed["hunks"][0]["patch"]
    assert "-    return 'before'" in patch
    assert "+    return 'after'" in patch
    assert "provenance_disabled" in packet["degraded"]
    session.close()


def test_a_read_at_a_revision_the_server_has_not_committed_is_refused(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    """Read-after-edit is a guarantee because the wrong revision is refused."""
    from lemoncrow_client.errors import ClientError, ErrorCode
    from lemoncrow_client.session import RemoteSession

    config = _client_config(running_server, e2e_worktree, state_dir)
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=30.0).ok

    session.state.view_revision += 3
    with pytest.raises(ClientError) as caught:
        session.call_tool("read", {"files": ["pkg/alpha.py"]})
    assert caught.value.code is ErrorCode.VIEW_REVISION_UNACKNOWLEDGED
    assert caught.value.action.value == "refresh_view_revision"
    session.close()


def test_a_blob_miss_is_filled_in_the_same_turn(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    """A file created after the view opened is pulled by the server and filled.

    The server answers ``{need: [...]}`` on its own -- nothing here arranges it
    -- because the path is referenced, the overlay declares it, and the content
    is not in the store yet.
    """
    from lemoncrow_client.dispatcher import Dispatcher
    from lemoncrow_client.manifest import ManifestEntry, profile_for_path, sha256_hex
    from lemoncrow_client.session import RemoteSession

    config = _client_config(running_server, e2e_worktree, state_dir)
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=30.0).ok

    # Declare a path in the view without uploading its content, then reference
    # it. This is the state a lost upload leaves behind.
    data = b"def late():\n    return 'late'\n"
    (e2e_worktree / "pkg" / "late.py").write_bytes(data)
    entry = ManifestEntry(
        path="pkg/late.py",
        content_digest=sha256_hex(data),
        parser_profile=profile_for_path("pkg/late.py"),
        mode=0o100644,
        size=len(data),
    )
    session.blobs._commit((entry,), ())  # declare the row, upload nothing

    outcome = Dispatcher(config, session).call("read", {"files": ["pkg/late.py"]})
    assert not outcome.is_error, outcome.content
    assert "return 'late'" in outcome.content[0]["text"]
    session.close()


def test_a_second_view_on_the_same_root_is_warm_and_transfers_nothing(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    """The canonical root is what makes a second worktree or developer free.

    The second session sends no manifest rows and uploads no content, which is
    the storage claim stated from the client's side: the cost of another view on
    an identical tree is one row, not another copy.
    """
    from lemoncrow_client.session import RemoteSession

    config = _client_config(running_server, e2e_worktree, state_dir)
    first = RemoteSession(config)
    cold = first.bootstrap(deadline_s=30.0)
    assert cold.ok, cold.reason
    assert cold.warm is False
    assert cold.manifest_chunks_sent == 1
    if not cold.local_fs:
        assert cold.uploaded_blobs > 0
    else:
        # The same-host grant means the server read the bytes off disk. Same
        # answers, fewer bytes on the wire -- which is the whole point of it.
        assert cold.uploaded_blobs == 0

    second = RemoteSession(config)
    warm = second.bootstrap(deadline_s=30.0)
    assert warm.ok, warm.reason
    assert warm.warm is True
    assert warm.manifest_chunks_sent == 0
    assert warm.uploaded_blobs == 0
    assert warm.files == cold.files

    first.close()
    second.close()


def test_reopening_after_the_last_view_closed_is_warm(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    """The property every real session depends on.

    The ``SessionStart`` hook opens a view, synchronizes and closes it again --
    every session, by design, because the hook is a short-lived process. So the
    interesting question is not what two concurrent views share; it is what
    survives a close. A manifest root is org-scoped, content-addressed and
    immutable, so it does: the next session announces the same root, the server
    recognizes it, and neither rows nor content move. A root reclaimed with its
    last view would make every session cold and re-send thousands of rows for
    an unchanged tree, which is the plan's warm target lost for structural
    reasons rather than slow ones.
    """
    from lemoncrow_client.session import RemoteSession

    config = _client_config(running_server, e2e_worktree, state_dir)
    first = RemoteSession(config)
    cold = first.bootstrap(deadline_s=30.0)
    assert cold.ok, cold.reason
    assert cold.warm is False
    first.close()

    again = RemoteSession(config)
    reopened = again.bootstrap(deadline_s=30.0)
    assert reopened.ok, reopened.reason
    assert reopened.warm is True, "a closed view must not take its manifest root with it"
    assert reopened.manifest_chunks_sent == 0, "an unchanged tree re-sends no manifest rows"
    assert reopened.uploaded_blobs == 0, "content is org-scoped and survives the view"
    assert reopened.files == cold.files
    again.close()


def test_an_over_cap_file_is_manifested_and_never_demanded(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path, tmp_path: Path
) -> None:
    """The cap is server policy; the client honours it without being told twice."""
    from lemoncrow_client.dispatcher import Dispatcher
    from lemoncrow_client.session import RemoteSession

    cap = running_server.state.config.limits.max_indexed_content_bytes
    (e2e_worktree / "huge.txt").write_bytes(b"z" * (cap + 4096))

    config = _client_config(running_server, e2e_worktree, state_dir)
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=60.0).ok

    entry = running_server.backend.views.entry(ORG, session.state.view_id, "huge.txt")
    assert entry is not None, "an over-cap file must still have a manifest row"
    assert running_server.backend.content.get(ORG, entry.content_digest) is None

    # And referencing it does not produce an unfillable demand for bytes.
    outcome = Dispatcher(config, session).call("read", {"files": ["huge.txt"]})
    assert not outcome.is_error, outcome.content
    session.close()


def test_the_index_query_route_is_bound_to_the_same_revision(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    """Both ways to reach the server name the revision they observed."""
    from lemoncrow_client.session import RemoteSession

    config = _client_config(running_server, e2e_worktree, state_dir)
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=30.0).ok

    answer = session.query_index("lexical", query="alpha")
    assert answer["view_revision"] == session.view_revision
    assert any(hit["path"] == "pkg/alpha.py" for hit in answer["hits"])

    (e2e_worktree / "pkg" / "alpha.py").write_text("def gamma():\n    return 'gamma'\n", encoding="utf-8")
    session.push_paths(("pkg/alpha.py",))
    refreshed = session.query_index("lexical", query="gamma")
    assert refreshed["view_revision"] == session.view_revision
    assert any(hit["path"] == "pkg/alpha.py" for hit in refreshed["hits"])
    session.close()


def test_the_client_is_refused_when_it_offers_an_unsupported_protocol(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused loudly, before authentication, with an upgrade action."""
    from lemoncrow_client import protocol as client_protocol
    from lemoncrow_client import session as session_module
    from lemoncrow_client.errors import ClientError, ErrorCode

    monkeypatch.setattr(client_protocol, "PROTOCOL_VERSION", "1999-01-01")
    monkeypatch.setattr(session_module, "PROTOCOL_VERSION", "1999-01-01")
    config = _client_config(running_server, e2e_worktree, state_dir)
    with pytest.raises(ClientError) as caught:
        session_module.RemoteSession(config).handshake()
    assert caught.value.code is ErrorCode.PROTOCOL_VERSION_MISMATCH
    assert caught.value.action.value == "upgrade_client"


def test_a_credential_the_server_does_not_know_is_refused_with_reauthenticate(
    running_server: RunningServer, e2e_worktree: Path, state_dir: Path
) -> None:
    from lemoncrow_client.config import load_config
    from lemoncrow_client.session import RemoteSession

    config = load_config(
        {
            "LEMONCROW_URL": running_server.url,
            "LEMONCROW_TOKEN": "wrong-token-0123456789abcdef",
            "LEMONCROW_HOME": str(state_dir),
            "HOME": str(state_dir.parent),
        },
        cwd=e2e_worktree,
    )
    report = RemoteSession(config).bootstrap(deadline_s=30.0)
    assert not report.ok
    assert "unauthenticated" in report.reason
