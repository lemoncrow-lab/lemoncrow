"""P5-5 terminal parity: each ``lc review`` verb makes the call its button makes, with the same guards.

The verbs run against the real Review API (a ``TestClient`` over the real app and store), so every payload is
the server's own. A recording wrapper proves the CLI sends what the Reader's API functions send.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Imported before any patch: review.py binds config, session and browser names at first import.
import lemoncrow_client.browser
import lemoncrow_client.config
import lemoncrow_client.review
import lemoncrow_client.review_session
import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lemoncrow_client import review_verbs
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.review_terminal import TerminalError

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "p5-5-terminal-token-0123456789abcdef"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class Review:
    """One real review on a real store, with a request callable shaped like the client's."""

    def __init__(self, client: TestClient, review_id: str, revision_id: str, root: Path, store: ReviewStore) -> None:
        self.client = client
        self.id = review_id
        self.revision = revision_id
        self.root = root
        self.store = store
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []
        #: ``hook(method, path)`` runs before a call reaches the server (to model a race).
        self.hook: Callable[[str, str], None] | None = None

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        self.calls.append((method, path, body))
        if self.hook is not None:
            self.hook(method, path)
        reply = self.client.request(method, path, headers=HEADERS, json=body if body is not None else None)
        if reply.status_code >= 400:
            detail = (
                reply.json().get("detail", reply.text)
                if reply.headers.get("content-type", "").startswith("application/json")
                else reply.text
            )
            raise ClientError.from_wire(
                {"error": {"code": "review_state_conflict", "message": str(detail)}}, status=reply.status_code
            )
        return reply.json()

    def get(self, suffix: str = "") -> dict[str, Any]:
        reply = self.client.get(f"/api/reviews/{self.id}{suffix}", headers=HEADERS)
        assert reply.status_code == 200, reply.text
        return reply.json()

    def targets(self) -> dict[str, dict[str, Any]]:
        return {item["label"]: item for item in self.get("/targets?order=recommended")["targets"]}

    def dispatch_due(self) -> None:
        asyncio.run(self.client.app.state.review_flow_dispatch_due())

    def pin_agent_destination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.direct_delivery_supported", lambda host: True)
        pinned = self.client.put(
            f"/api/reviews/{self.id}/destination",
            headers=HEADERS,
            json={"kind": "session", "host": "claude", "session_id": "session-author"},
        )
        assert pinned.status_code == 200, pinned.text


@pytest.fixture
def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Review]:
    for name, _host in review_verbs.agent_session_host.__globals__["AGENT_SESSION_ENVS"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    root = tmp_path / "repo"
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Terminal Test"
    repo.config["user.email"] = "terminal@example.test"
    for name in ("charge.py", "refund.py", "run.sh"):
        (root / name).write_text("def f():\n    return 1\n" if name.endswith(".py") else "echo hi\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Terminal Test", "terminal@example.test", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "baseline", tree, [])
    for name in ("charge.py", "refund.py"):
        (root / name).write_text("def f():\n    return 2\n", encoding="utf-8")
    os.chmod(root / "run.sh", 0o755)  # an added executable bit is a decision, never a quick check
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(root, working_tree=True)
    built = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, root, rng, title=built.packet.title)
    first = snapshot_revision(store, session, root, rng, store_root=store_root, build=built)
    store.publish_snapshot(session.id, first.id)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=root, port=48080)
    client = TestClient(app, base_url="http://127.0.0.1:48080")
    try:
        yield Review(client, session.id, first.id, root, store)
    finally:
        client.close()


def _decision(review: Review) -> str:
    found = [label for label, item in review.targets().items() if item["answer"]["category"] == "decision"]
    assert len(found) == 1, found
    return found[0]


def _quick(review: Review) -> list[str]:
    return [label for label, item in review.targets().items() if item["answer"]["category"] == "quick_check"]


def _new_revision(review: Review) -> str:
    """Another revision of the same review, as an agent's next edit would capture."""

    (review.root / "charge.py").write_text("def f():\n    return 3\n", encoding="utf-8")
    rng = resolve_rev_range(review.root, working_tree=True)
    built = build_review_packet_with_blobs(review.root, rng, store_root=review.store.root, with_patch_text=True)
    session = open_or_create_session(review.store, review.root, rng, title=built.packet.title)
    second = snapshot_revision(review.store, session, review.root, rng, store_root=review.store.root, build=built)
    review.store.publish_snapshot(session.id, second.id)
    assert second.id != review.revision
    return second.id


# --------------------------------------------------------------------------- #
# open: addresses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("written", "slug", "tail"),
    [
        ("/r/58c148b6", "58c148b6", ""),
        ("r/58c148b6", "58c148b6", ""),
        ("58c148b6", "58c148b6", ""),
        ("http://127.0.0.1:48080/r/58c148b6", "58c148b6", ""),
        ("http://127.0.0.1:48080/r/58c148b6/", "58c148b6", ""),
        ("https://reader.example/r/58c148b6/one/charge.py%3A%3Af", "58c148b6", "/one/charge.py%3A%3Af"),
        ("/r/58c148b6/map?lens=dependencies&view=after", "58c148b6", "/map?lens=dependencies&view=after"),
        ("/r/58c148b6/all#charge", "58c148b6", "/all#charge"),
        ("#/r/58c148b6/wrap", "58c148b6", "/wrap"),
    ],
)
def test_open_reads_every_address_the_cli_and_github_print(written: str, slug: str, tail: str) -> None:
    assert review_verbs.parse_review_link(written) == (slug, tail)


@pytest.mark.parametrize("written", ["", "/settings/reading", "ftp://host/r/x", "/r/", "mailto:a@b.c"])
def test_open_refuses_what_is_not_a_review_address(written: str) -> None:
    with pytest.raises(TerminalError):
        review_verbs.parse_review_link(written)


@pytest.fixture
def cli(review: Review, monkeypatch: pytest.MonkeyPatch) -> Callable[..., tuple[int, str]]:
    """``cli(verb, *args)`` runs ``verbs_main`` against the real app; the browser launch is recorded, not run."""

    class Session:
        def __init__(self, config: object) -> None:
            pass

        def review_url(self, reference: str) -> str:
            return f"http://127.0.0.1:48080/r/{reference.removeprefix('r/')}"

        def list_reviews(self, *, limit: int = 100, timeout_s: float | None = None) -> list[dict[str, Any]]:
            return []

        def close(self) -> None:
            pass

    config = SimpleNamespace(hosted=False, authenticated=True, repo_root=review.root, request_timeout_s=5.0)
    monkeypatch.setattr("lemoncrow_client.config.load_config", lambda **_kw: config)
    monkeypatch.setattr("lemoncrow_client.review_session.ReviewSession", Session)
    opened: list[str] = []
    monkeypatch.setattr("lemoncrow_client.browser.launch_browser_url", lambda url: opened.append(url) or True)
    monkeypatch.setattr("lemoncrow_client.review._arm_local_browser", lambda _config, _url: None)
    review.opened = opened  # type: ignore[attr-defined]

    def run(verb: str, *args: str, confirm: Callable[[str], bool] | None = None) -> tuple[int, str]:
        sink = io.StringIO()
        code = review_verbs.verbs_main(verb, list(args), sink=sink, request=review.request, confirm=confirm)
        return code, sink.getvalue()

    return run


def test_open_prints_and_opens_the_same_reader_address(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    code, out = cli("open", "http://old-host:9/r/58c148b6/one/charge.py")
    assert code == 0
    assert out == "Review  http://127.0.0.1:48080/r/58c148b6/one/charge.py\n"
    assert review.opened == ["http://127.0.0.1:48080/r/58c148b6/one/charge.py"]  # type: ignore[attr-defined]
    code, out = cli("open", "/r/58c148b6/wrap", "--no-open", "--json")
    assert json.loads(out) == {
        "schema": 1,
        "verb": "open",
        "review": "58c148b6",
        "url": "http://127.0.0.1:48080/r/58c148b6/wrap",
    }
    assert len(review.opened) == 1  # type: ignore[attr-defined]
    code, out = cli("open", "/settings/reading")
    assert code == 1
    assert "not a Review address" in out
    assert len(review.opened) == 1  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# answer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("word", "state", "printed"),
    [
        ("good", "reviewed", "Looks good"),
        ("change", "needs_changes", "Needs a change"),
        ("later", "later", "Decide later"),
    ],
)
def test_answer_records_what_the_answer_button_records(
    review: Review, cli: Callable[..., tuple[int, str]], word: str, state: str, printed: str
) -> None:
    part = _decision(review)
    code, out = cli("answer", part, word, "--review", review.id)
    assert code == 0, out
    assert out == f"{printed} · {part}\n"
    # The Reader's markUnit sends exactly these three fields.
    posts = [call for call in review.calls if call[0] == "POST"]
    assert [(path.rsplit("/", 1)[-1], body) for _m, path, body in posts] == [
        (
            "marks",
            {"unit_key": review.targets()[part]["unit_key"], "state": state, "expected_revision_id": review.revision},
        )
    ]
    assert review.targets()[part]["state"] == state
    # The same state the button's own call leaves behind on another part.
    other = _quick(review)[0]
    direct = review.client.post(
        f"/api/reviews/{review.id}/marks",
        headers=HEADERS,
        json={"unit_key": review.targets()[other]["unit_key"], "state": state, "expected_revision_id": review.revision},
    )
    assert direct.status_code == 200, direct.text
    assert review.targets()[other]["state"] == review.targets()[part]["state"]


def test_a_later_answer_is_not_a_judgment(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    part = _decision(review)
    assert cli("answer", part, "later", "--review", review.id)[0] == 0
    progress = review.get("/targets?order=recommended")["progress"]
    assert progress["later"] == 1
    code, out = cli("send", "--verdict", "approve", "--review", review.id, "--yes")
    assert code == 1
    assert "1 decision still need your answer" in out
    assert review.get("/outcome")["current"] is None


def test_answer_is_refused_when_the_revision_moved(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    part = _decision(review)
    before = review.targets()[part]["state"]
    # The review reads its overview, then another revision lands before the answer is written.
    stale = {"overview": True}

    def race(method: str, path: str) -> None:
        if method == "POST" and path.endswith("/marks") and stale.pop("overview", None):
            _new_revision(review)

    review.hook = race
    code, out = cli("answer", part, "good", "--review", review.id)
    assert code == 1
    assert "revision" in out.lower()
    review.hook = None
    assert review.targets()[part]["state"] == before  # nothing was recorded


def test_answer_names_only_the_revision_the_person_means(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    part = _decision(review)
    code, out = cli("answer", part, "good", "--review", review.id, "--revision", "3")
    assert code == 1
    assert "Revision 3 is not the one open now: this review is on Revision 1" in out
    assert review.targets()[part]["state"] == "unreviewed"
    assert cli("answer", part, "good", "--review", review.id, "--revision", "1")[0] == 0


def test_answer_never_guesses_which_part(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    code, out = cli("answer", "nothing.py", "good", "--review", review.id)
    assert code == 1
    assert "no part matches 'nothing.py'" in out
    code, out = cli("answer", _decision(review), "fine", "--review", review.id)
    assert code == 1
    assert "answer must be one of good, change, later" in out
    assert all(item["state"] == "unreviewed" for item in review.targets().values())


# --------------------------------------------------------------------------- #
# note
# --------------------------------------------------------------------------- #


def test_note_saves_the_note_the_save_button_saves(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    part = _quick(review)[0]
    unit_key = review.targets()[part]["unit_key"]
    code, out = cli("note", part, "Keep the guard", "--review", review.id)
    assert code == 0, out
    assert out.startswith(f"Note saved · {part}.")
    assert "Nothing is sent until you send" in out
    body = next(body for method, path, body in review.calls if method == "POST" and path.endswith("/annotations"))
    assert body is not None
    assert {key: body[key] for key in ("kind", "target_unit_key", "mark_target", "expected_revision_id", "body")} == {
        "kind": "request_change",
        "target_unit_key": unit_key,
        "mark_target": True,
        "expected_revision_id": review.revision,
        "body": "Keep the guard",
    }
    notes = review.get("/annotations")
    assert [(note["kind"], note["body"], note["path"]) for note in notes["annotations"]] == [
        ("request_change", "Keep the guard", review.targets()[part]["path"])
    ]
    assert notes["feedback"]["unpublished"] == 1
    assert review.targets()[part]["state"] == "needs_changes"  # the Reader's note also marks the part
    assert review.get("/flow-send")["state"] == "none"  # a note sends nothing


def test_note_is_refused_when_the_revision_moved(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    part = _quick(review)[0]
    pending = {"race": True}

    def race(method: str, path: str) -> None:
        if method == "POST" and path.endswith("/annotations") and pending.pop("race", None):
            _new_revision(review)

    review.hook = race
    code, out = cli("note", part, "Keep the guard", "--review", review.id)
    assert code == 1
    assert "revision" in out.lower()
    review.hook = None
    assert review.get("/annotations")["annotations"] == []
    assert cli("note", part, "  ", "--review", review.id)[0] == 1


# --------------------------------------------------------------------------- #
# send / approve / undo
# --------------------------------------------------------------------------- #


def _with_note(review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch) -> str:
    review.pin_agent_destination(monkeypatch)
    part = _quick(review)[0]
    assert cli("note", part, "Keep the guard", "--review", review.id)[0] == 0
    return str(review.get("/annotations")["annotations"][0]["id"])


def _button_hash(review: Review, verdict: str, kind: str, summary: str = "") -> str:
    """The hash Wrap up's preview shows for one note and these choices: what a person checks before --yes."""

    note_id = review.get("/annotations")["annotations"][0]["id"]
    presentation = {"verdict": verdict, "importance": {note_id: kind}, "test_with_fix": False, "summary": summary}
    return str(
        review.client.post(
            f"/api/reviews/{review.id}/feedback/export",
            headers=HEADERS,
            json={"expected_revision_id": review.revision, "presentation": presentation},
        ).json()["feedback_hash"]
    )


def test_send_shows_wrap_up_and_sends_nothing_without_a_yes(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = _with_note(review, cli, monkeypatch)
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    code, out = cli("send", "--verdict", "request-changes", "--importance", "must-fix", "--review", review.id)
    assert code == 1
    assert "Wrap up · Revision 1" in out
    assert f"Note {note_id} · Must fix" in out
    assert "Verdict: Request changes" in out
    assert "Nothing sent. Run again with --preview-hash " in out
    assert " --yes to send exactly this." in out
    assert review.get("/flow-send")["state"] == "none"
    # An interactive no changes nothing either.
    assert (
        cli(
            "send",
            "--verdict",
            "request-changes",
            "--importance",
            "must-fix",
            "--review",
            review.id,
            confirm=lambda _q: False,
        )[0]
        == 1
    )
    assert review.get("/flow-send")["state"] == "none"


def test_send_previews_the_exact_message_and_queues_the_hash_it_showed(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = _with_note(review, cli, monkeypatch)
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    presentation = {
        "verdict": "changes_requested",
        "importance": {note_id: "must_fix"},
        "test_with_fix": True,
        "summary": "Cover it",
    }
    button = review.client.post(
        f"/api/reviews/{review.id}/feedback/export",
        headers=HEADERS,
        json={"expected_revision_id": review.revision, "presentation": presentation},
    ).json()
    code, out = cli(
        "send", "--verdict", "request-changes", "--importance", f"{note_id}=must-fix", "--summary", "Cover it",
        "--test-with-fix", "--review", review.id, "--preview-hash", button["feedback_hash"], "--yes",
    )  # fmt: skip
    assert code == 0, out
    assert button["markdown"].rstrip("\n") in out  # the message Wrap up shows, byte for byte
    assert f"Preview hash {button['feedback_hash']}" in out
    assert "Sending to " in out
    assert "will be recorded and 1 note sent in" in out
    assert "Undo with: lc review undo" in out
    queued = review.get("/flow-send")
    assert queued["state"] == "pending"
    assert (queued["outcome"], queued["has_feedback"], queued["note_count"]) == ("changes_requested", True, 1)
    posted = next(body for method, path, body in review.calls if method == "POST" and path.endswith("/flow-send"))
    assert posted is not None
    assert posted["feedback"] == {
        key: button[key] for key in ("revision_id", "operation_id", "feedback_hash", "presentation")
    }
    assert posted["expected_revision_id"] == review.revision
    # The server holds the delivery: nothing is recorded until the window closes.
    assert review.get("/outcome")["current"] is None
    time.sleep(0.15)
    review.dispatch_due()
    assert review.get("/flow-send")["state"] in {"sent", "dispatching", "uncertain", "failed"}
    assert review.get("/outcome")["current"]["outcome"] == "changes_requested"


def test_send_is_refused_when_the_preview_changed_after_it_was_shown(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_note(review, cli, monkeypatch)
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    exports = {"count": 0}

    def edit_note_after_preview(method: str, path: str) -> None:
        if method == "POST" and path.endswith("/feedback/export"):
            exports["count"] += 1
            if exports["count"] == 3:  # probe, preview, then the re-check just before sending
                note = review.get("/annotations")["annotations"][0]
                patched = review.client.patch(
                    f"/api/annotations/{note['id']}",
                    headers=HEADERS,
                    json={"body": "A different request", "expected_revision_id": review.revision},
                )
                assert patched.status_code == 200, patched.text

    review.hook = edit_note_after_preview
    shown = _button_hash(review, "changes_requested", "must_fix")
    exports["count"] = 0
    code, out = cli(
        "send", "--verdict", "request-changes", "--importance", "must-fix", "--review", review.id,
        "--preview-hash", shown, "--yes",
    )  # fmt: skip
    assert code == 1
    assert "Feedback changed after the preview" in out
    review.hook = None
    assert review.get("/flow-send")["state"] == "none"


def test_send_refuses_a_hash_the_person_did_not_check(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_note(review, cli, monkeypatch)
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    code, out = cli(
        "send", "--verdict", "request-changes", "--importance", "must-fix", "--review", review.id, "--yes",
        "--preview-hash", "0" * 64,
    )  # fmt: skip
    assert code == 1
    assert "The preview changed since the one you checked" in out
    assert review.get("/flow-send")["state"] == "none"


def test_send_names_what_blocks_it_and_never_sends_around_it(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_note(review, cli, monkeypatch)
    code, out = cli("send", "--verdict", "request-changes", "--review", review.id, "--yes")
    assert code == 1
    assert "Can't send yet: 1 decision still need your answer" in out
    assert "Can't send yet: Set the importance of 1 note" in out
    assert review.get("/flow-send")["state"] == "none"
    code, out = cli("send", "--review", review.id, "--yes")
    assert code == 2
    assert "Usage: lc review send --verdict" in out
    assert review.get("/flow-send")["state"] == "none"


def test_a_mismatched_verdict_needs_its_own_confirmation(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_note(review, cli, monkeypatch)
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    shown = _button_hash(review, "lgtm", "must_fix")
    args = ("approve", "--importance", "must-fix", "--review", review.id, "--preview-hash", shown, "--yes")
    code, out = cli(*args)
    assert code == 1
    assert "You have 1 must-fix note. Approve anyway?" in out
    assert review.get("/flow-send")["state"] == "none"
    code, out = cli(*args, "--confirm-mismatch")
    assert code == 0, out
    assert review.get("/flow-send")["outcome"] == "lgtm"


def test_approve_records_the_verdict_and_never_merges(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    code, out = cli("approve", "--review", review.id, "--yes")
    assert code == 0, out
    assert "No notes to send." in out
    assert "Recording your approval…" in out
    assert "(Approve) will be recorded in" in out
    assert "LemonCrow never commits or merges." in out
    queued = review.get("/flow-send")
    assert (queued["state"], queued["outcome"], queued["has_feedback"]) == ("pending", "lgtm", False)
    assert review.get("/finish")["status"] == "open"
    time.sleep(0.15)
    review.dispatch_due()
    assert review.get("/outcome")["current"]["outcome"] == "lgtm"
    assert review.get("/finish")["status"] == "finished"


def test_send_json_is_one_document(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    code, out = cli("approve", "--review", review.id, "--yes", "--json")
    assert code == 0, out
    document = json.loads(out)
    assert document["verb"] == "send"
    assert document["send"]["state"] == "pending"
    assert document["plan"]["outcome"] == "lgtm"


def test_undo_inside_the_window_cancels_the_send(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "20000")  # the window stays open for the whole test
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    assert cli("approve", "--review", review.id, "--yes")[0] == 0
    operation = review.get("/flow-send")["operation_id"]
    code, out = cli("undo", "--review", review.id)
    assert code == 0, out
    assert out.startswith("Undone. Nothing was sent.")
    assert review.get("/flow-send")["state"] == "undone"
    undo_calls = [path for method, path, _b in review.calls if method == "POST" and path.endswith("/undo")]
    assert undo_calls == [f"/api/reviews/{review.id}/flow-send/{operation}/undo"]
    assert review.get("/outcome")["current"] is None
    assert cli("undo", "--review", review.id)[1] == "Already undone. Nothing was sent.\n"


def test_undo_after_the_window_says_it_cannot_be_undone_now(
    review: Review, cli: Callable[..., tuple[int, str]]
) -> None:
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    assert cli("approve", "--review", review.id, "--yes")[0] == 0
    time.sleep(0.15)
    review.dispatch_due()
    code, out = cli("undo", "--review", review.id)
    assert code == 1
    assert out == "Sent · can't be undone now\n"
    assert review.get("/outcome")["current"]["outcome"] == "lgtm"  # undo did not touch what was sent


def test_undo_that_loses_the_race_with_the_window_reports_the_servers_answer(
    review: Review, cli: Callable[..., tuple[int, str]]
) -> None:
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    assert cli("approve", "--review", review.id, "--yes")[0] == 0
    # The read still sees the send pending; by the time the undo lands, the server's window has closed.
    seen = {"read": False}

    def close_window(method: str, path: str) -> None:
        if method == "GET" and path.endswith("/flow-send"):
            seen["read"] = True
        if method == "POST" and path.endswith("/undo") and seen["read"]:
            time.sleep(0.15)

    review.hook = close_window
    code, out = cli("undo", "--review", review.id)
    assert code == 1
    assert out == "Sent · can't be undone now\n"
    review.hook = None
    assert (
        review.get("/flow-send")["state"] == "pending"
    )  # the worker has not sent yet; the window is shut all the same


@pytest.mark.parametrize(
    "argv",
    [
        ["lc", "review", "retract", "--review", "r"],
        ["lemoncrow", "review", "retract", "--review", "r"],
        ["lcr", "retract", "--review", "r"],
        ["lc", "review", "next", "--review", "r"],
        ["lcr", "approve", "--review", "r"],
    ],
)
def test_the_full_install_routes_terminal_verbs_to_the_client(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    from lemoncrow.gateway.cli.app import main

    seen: list[list[str]] = []
    monkeypatch.setattr(lemoncrow_client.review, "review_main", lambda args, sink: seen.append(list(args)) or 7)
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 7 and seen == [argv[argv.index("review") + 1 if argv[1] == "review" else 1 :]]


def test_nothing_to_undo(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    code, out = cli("undo", "--review", review.id)
    assert code == 1
    assert out == "Nothing to undo. No send is waiting.\n"


# --------------------------------------------------------------------------- #
# agent guard and retract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "verb_args",
    [
        ("answer", "x", "good"),
        ("note", "x", "text"),
        ("send", "--verdict", "approve", "--yes"),
        ("approve", "--yes"),
        ("undo",),
        ("retract",),
    ],
)
def test_an_agent_session_cannot_judge_through_the_terminal(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch, verb_args: tuple[str, ...]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    code, out = cli(*verb_args, "--review", review.id)
    assert code == 2
    assert (
        f"lc review {verb_args[0]} records a human review judgment and cannot run inside an agent session (claude)"
        in out
    )
    assert review.calls == []


def test_next_and_open_stay_available_inside_an_agent_session(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli("open", "/r/abc", "--no-open")[0] == 0
    assert cli("next", "--no-open")[0] == 0


def _signed_verdict(
    review: Review, cli: Callable[..., tuple[int, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Approve through the terminal with a signing key set up; returns the record the server signed."""

    from lemoncrow.core.settings import write_setting
    from lemoncrow.pro.capabilities.review.record_signing import USER_KEY_SETTING, generate_user_key

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    key = root / "review-signing" / "user-key.pem"
    generate_user_key(key)
    write_setting(root, USER_KEY_SETTING, str(key))
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    assert cli("approve", "--review", review.id, "--yes")[0] == 0
    time.sleep(0.15)
    review.dispatch_due()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        records = review.get("/record")["records"]
        if records:
            record: dict[str, Any] = records[0]
            return record
        time.sleep(0.05)
    raise AssertionError("the verdict was not signed")


def test_retract_withdraws_the_record_the_retract_button_withdraws(
    review: Review, cli: Callable[..., tuple[int, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _signed_verdict(review, cli, tmp_path, monkeypatch)
    code, out = cli("retract", "--review", review.id)
    assert code == 0, out
    assert out.startswith("You retracted the earlier record \u00b7 Revision 1 \u00b7 ")
    assert out.endswith(" UTC. Sending again signs a new one: lc review send\n")
    posts = [path for method, path, _b in review.calls if method == "POST" and path.endswith("/retract")]
    assert posts == [f"/api/reviews/{review.id}/records/{record['record_id']}/retract"]
    assert review.get("/record")["records"][0]["retracted"] is not None
    assert review.get("/finish")["status"] == "open"
    again_code, again = cli("retract", "--review", review.id)
    assert again_code == 0 and again.startswith("Already retracted \u00b7 Revision 1")


def test_retract_json_is_one_document(
    review: Review, cli: Callable[..., tuple[int, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_verdict(review, cli, tmp_path, monkeypatch)
    code, out = cli("retract", "--review", review.id, "--json")
    document = json.loads(out)
    assert code == 0 and (document["verb"], document["retracted"], document["state"]) == ("retract", True, "retracted")


def test_retract_after_the_window_is_refused_by_the_server(
    review: Review, cli: Callable[..., tuple[int, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_RETRACT_MS", "1500")
    _signed_verdict(review, cli, tmp_path, monkeypatch)
    time.sleep(1.6)
    code, out = cli("retract", "--review", review.id)
    assert code == 1
    assert out.startswith("Can't retract \u00b7 the 24 hours from signing ended at ") and out.endswith(
        "Nothing was changed.\n"
    )
    assert review.get("/record")["records"][0]["retracted"] is None


def test_retract_with_nothing_sent_says_so(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    code, out = cli("retract", "--review", review.id)
    assert code == 1
    assert (
        out == "Nothing to retract. No signed record of your verdict exists for this revision. Nothing was changed.\n"
    )
    assert [call for call in review.calls if call[0] == "POST"] == []


# --------------------------------------------------------------------------- #
# next
# --------------------------------------------------------------------------- #


def test_next_offers_the_review_and_reason_done_offers(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    code, out = cli("next", "--no-open", "--json")
    assert code == 0, out
    chosen = json.loads(out)["next"]
    listed = review.client.get("/api/reviews?status_filter=open", headers=HEADERS).json()["reviews"]
    assert chosen["id"] == listed[0]["id"] == review.id
    assert chosen["ref"] == listed[0]["ref"]
    assert chosen["reason"] == "your turn (1 decision)"
    assert chosen["your_turn"] is True
    assert chosen["url"].endswith(f"/{listed[0]['ref']}")
    code, out = cli("next", "--no-open")
    assert out.splitlines()[0] == "Next up · your turn (1 decision)"
    assert out.splitlines()[-1] == f"Review  http://127.0.0.1:48080/{listed[0]['ref']}"
    # The decision is answered and the quick checks too: nothing is left to judge, said as Done says it.
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    for part in _quick(review):
        assert cli("answer", part, "good", "--review", review.id)[0] == 0
    assert json.loads(cli("next", "--no-open", "--json")[1])["next"]["reason"] == "nothing left to judge"


def test_next_leaves_out_the_review_just_finished_and_opens_the_reader(
    review: Review, cli: Callable[..., tuple[int, str]]
) -> None:
    code, out = cli("next", "--after", f"r/{review.id}", "--no-open")
    assert (code, out) == (0, "Queue clear. Nothing waits on you.\n")
    code, out = cli("next")
    assert code == 0
    assert review.opened == [out.splitlines()[-1].removeprefix("Review  ")]  # type: ignore[attr-defined]


def _candidate(
    review_id: str, states: list[str], *, feedback: Mapping[str, int] | None = None, snoozed: bool = False
) -> dict[str, Any]:
    return {
        "review": {
            "id": review_id,
            "ref": f"r/{review_id}",
            "title": review_id,
            **({"queue": {"snoozed_until": "2999-01-01T00:00:00Z", "resume": None}} if snoozed else {}),
        },
        "targets": [
            {
                "target_id": f"{review_id}{index}",
                "unit_key": f"{review_id}{index}",
                "path": "a.py",
                "state": state,
                "answer": {"category": "decision"},
                "priority": {"outstanding": state == "unreviewed", "position": index, "reasons": []},
            }
            for index, state in enumerate(states)
        ],
        "feedback": {"unpublished": 0, "published": 0, "in_flight": 0, **(feedback or {})},
    }


def _next_for(candidates: list[dict[str, Any]], listed: int | None = None) -> dict[str, Any] | None:
    by_id = {item["review"]["id"]: item for item in candidates}
    rows = [item["review"] for item in candidates]
    rows += [
        {"id": f"extra{index}", "ref": f"r/extra{index}", "title": "extra"}
        for index in range((listed or 0) - len(rows))
    ]

    def request(method: str, path: str, body: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if path.startswith("/api/reviews?"):
            return {"reviews": rows}
        review_id = path.split("/")[3]
        if review_id not in by_id:
            raise ClientError(ErrorCode.REVIEW_UNKNOWN, "gone")
        if "/targets" in path:
            return {"targets": by_id[review_id]["targets"]}
        return {"feedback": by_id[review_id]["feedback"]}

    return review_verbs.next_up(request)["next"]


# The cases shared with `frontend/src/review/done/nextUp.test.ts`: one JSON file, two implementations.
_SHARED = json.loads(
    (Path(__file__).resolve().parents[2] / "frontend/src/review/done/nextUpCases.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", _SHARED, ids=[case["name"] for case in _SHARED])
def test_next_up_rules_match_the_readers(case: dict[str, Any]) -> None:
    candidates = [
        _candidate(item["id"], item["states"], feedback=item.get("feedback"), snoozed=item.get("snoozed", False))
        for item in case["candidates"]
    ]
    chosen = _next_for(candidates, listed=case.get("listed"))
    if case["expect"] is None:
        assert chosen is None
        return
    assert chosen is not None
    assert {"id": chosen["id"], "reason": chosen["reason"], "your_turn": chosen["your_turn"]} == case["expect"]


# --------------------------------------------------------------------------- #
# round 2: what the person must see, and what the Reader blocks
# --------------------------------------------------------------------------- #


def test_request_changes_with_nothing_to_say_is_blocked_as_wrap_up_blocks_it(
    review: Review, cli: Callable[..., tuple[int, str]]
) -> None:
    assert cli("answer", _decision(review), "change", "--review", review.id)[0] == 0
    args = ("send", "--verdict", "request-changes", "--review", review.id, "--yes", "--confirm-mismatch")
    code, out = cli(*args)
    assert code == 1
    assert "Can't send yet: Add a note or a summary explaining what needs to change" in out
    assert review.get("/flow-send")["state"] == "none"
    code, out = cli(*args, "--summary", "The executable bit is not wanted")
    assert code == 0, out
    assert review.get("/flow-send")["outcome"] == "changes_requested"


def test_yes_alone_never_sends_a_message_the_person_did_not_check(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = _with_note(review, cli, monkeypatch)
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    code, out = cli("send", "--verdict", "request-changes", "--importance", "must-fix", "--review", review.id, "--yes")
    assert code == 1
    shown = _button_hash(review, "changes_requested", "must_fix")
    assert f"Preview hash {shown}" in out
    assert "Keep the guard" in out  # the note's text is in the plan, not only its id
    assert "Not sent. --yes needs the preview hash" in out
    assert review.get("/flow-send")["state"] == "none"
    command = out.split("Run:\n", 1)[1].strip()
    assert f"--importance {note_id}=must-fix" in command
    assert f"--preview-hash {shown} --yes" in command
    # The printed command is the one that sends.
    import shlex

    words = shlex.split(command)
    assert words[:3] == ["lc", "review", "send"]
    code, out = cli(*words[2:])
    assert code == 0, out
    assert review.get("/flow-send")["state"] == "pending"


def test_approve_with_suggestions_is_its_own_verdict_with_the_readers_summary(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_note(review, cli, monkeypatch)
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    shown = _button_hash(review, "lgtm_with_suggestions", "suggestion", "Looks right · Approved with suggestions")
    args = ("send", "--verdict", "approve-with-suggestions", "--importance", "suggestion", "--summary", "Looks right")
    code, out = cli(*args, "--review", review.id, "--preview-hash", shown, "--yes")
    assert code == 0, out
    assert "Approve with suggestions" in out
    queued = review.get("/flow-send")
    assert queued["outcome"] == "lgtm"
    posted = next(body for method, path, body in review.calls if method == "POST" and path.endswith("/flow-send"))
    assert posted is not None
    assert posted["summary"] == "Looks right · Approved with suggestions"
    assert posted["feedback"]["presentation"]["verdict"] == "lgtm_with_suggestions"
    # With no note it is blocked, as the Reader blocks it.
    other = cli("send", "--verdict", "approve-with-suggestions", "--review", review.id)
    assert other[0] == 1


def test_approve_with_suggestions_needs_a_note(review: Review, cli: Callable[..., tuple[int, str]]) -> None:
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    code, out = cli("send", "--verdict", "approve-with-suggestions", "--review", review.id, "--yes")
    assert code == 1
    assert "Can't send yet: Approval with suggestions requires a note" in out
    assert review.get("/flow-send")["state"] == "none"


def test_a_note_the_server_would_send_but_the_list_lacks_blocks_send(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_note(review, cli, monkeypatch)
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    real = review.request

    def without_notes(method: str, path: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        reply = real(method, path, body)
        return {**reply, "annotations": []} if method == "GET" and path.endswith("/annotations") else reply

    plan = review_verbs.plan_send(without_notes, review.id, verdict="request-changes", importance=["must-fix"])
    assert "Some unsent comments were not loaded; review the feedback before sending" in plan["blockers"]
    assert plan["notes"] == []


def test_a_comparison_row_note_leaves_out_the_part_key_as_the_reader_does() -> None:
    sent: list[Mapping[str, Any]] = []
    target = {
        "target_id": "t",
        "unit_key": "cmp:1",
        "label": "a.py",
        "path": "a.py",
        "kind": "file",
        "start_line": 0,
        "end_line": 0,
        "spans": [],
        "fingerprint_method": "revision_compare",
    }

    def request(method: str, path: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if method == "POST":
            assert body is not None
            sent.append(body)
            return {"annotation": {"id": "n"}}
        if path.endswith("/targets?order=recommended"):
            return {"targets": [target]}
        return {"revision": {"id": "rev", "revision_number": 0}, "session": {}}

    review_verbs.note_part(request, "r", "a.py", "text")
    assert "target_unit_key" not in sent[0]
    assert "mark_target" not in sent[0]
    assert sent[0]["file_level"] is True


def test_a_verb_name_that_is_also_a_branch_says_how_to_review_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lemoncrow_client import review as review_module

    repo = tmp_path / "branchy"
    repo.mkdir()
    pygit2.init_repository(str(repo), initial_head="next")
    sig = pygit2.Signature("T", "t@example.test", 1700000000, 0)
    repo_obj = pygit2.Repository(str(repo))
    (repo / "a.txt").write_text("x", encoding="utf-8")
    repo_obj.index.add_all()
    repo_obj.index.write()
    repo_obj.create_commit("HEAD", sig, sig, "c", repo_obj.index.write_tree(), [])
    review_module._warn_if_git_ref(["next", "--repo-root", str(repo)])
    assert "To review the revision, run: lc review -- next" in capsys.readouterr().err
    review_module._warn_if_git_ref(["open", "--repo-root", str(repo)])
    assert capsys.readouterr().err == ""
    # `--` reaches the old behaviour: the word is the revision.
    seen: list[object] = []

    def stop(_root: object, rev: object, **_kw: object) -> None:
        seen.append(rev)
        raise ClientError(ErrorCode.PAYLOAD_INVALID, "stop here")

    config = SimpleNamespace(hosted=False, authenticated=True, repo_root=repo)
    monkeypatch.setattr(review_module, "load_config", lambda **_kw: config)
    monkeypatch.setattr(review_module, "resolve_review_range", stop)
    assert review_module.review_main(["--no-open", "--", "next"], sink=io.StringIO()) == 1
    assert seen == ["next"]


def test_an_undo_that_fails_for_another_reason_does_not_claim_the_send_went(
    review: Review, cli: Callable[..., tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "20000")
    assert cli("answer", _decision(review), "good", "--review", review.id)[0] == 0
    assert cli("approve", "--review", review.id, "--yes")[0] == 0
    real = review.request

    def flaky(method: str, path: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if method == "POST" and path.endswith("/undo"):
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "connection reset")
        return real(method, path, body)

    review.request = flaky  # type: ignore[method-assign]
    sink = io.StringIO()
    code = review_verbs.verbs_main("undo", ["--review", review.id], sink=sink, request=flaky)
    assert code == 1
    assert "Couldn't undo: connection reset" in sink.getvalue()
    assert "can't be undone now" not in sink.getvalue()
    assert review.get("/flow-send")["state"] == "pending"  # still waiting, still undoable
    review.request = real  # type: ignore[method-assign]
    assert cli("undo", "--review", review.id)[0] == 0
