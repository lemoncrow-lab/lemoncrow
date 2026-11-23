"""Reviewer-requested split into stacked Changes (Phase 12 item 13, P12-AGENT).

The reviewer asks; a fake authoring agent follows the handoff and builds one
stacked branch per Phase 8 cohort; collecting the branches makes each layer a
normal Review linked to the original. Each acceptance property is asserted with
its refusal beside it: a failed or cancelled split leaves the original intact,
and judgments carry only onto exactly matching targets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import delivery as delivery_module
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.development import DevelopmentStore
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.review_actions import DATA_LINE_PREFIX, HANDOFF_HEADER
from lemoncrow.pro.capabilities.review.session_models import ReviewAttribution, ReviewMark
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.split_request import (
    MAX_LAYER_PATHS,
    MAX_SPLIT_LAYERS,
    REMAINING_COHORT,
    SplitPlan,
    SplitRefused,
    plan_from_cohorts,
    render_split_handoff,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore
from tests.gateway.test_review_actions import _MARKERS, HEADERS, INJECTIONS, TOKEN, Deliveries, _data_lines

PORT = 45996
BASE = f"http://127.0.0.1:{PORT}"
SHA_A = "a" * 40
SHA_B = "b" * 40

_BASE_FILES = {
    "src/auth/login.py": "def login(user):\n    return user\n",
    "src/billing/invoice.py": "def total(items):\n    return sum(items)\n",
    "docs/guide.md": "# Guide\n\nHello.\n",
}
_LOGIN = "def login(user):\n    if not user:\n        raise ValueError('user')\n    return user\n"
_HEAD_FILES = {
    "src/auth/login.py": _LOGIN,
    "src/billing/invoice.py": "def total(items):\n    return round(sum(items), 2)\n\n\ndef tax(amount):\n    return amount * 0.2\n",
    "docs/guide.md": "# Guide\n\nHello.\n\nBilling now rounds totals.\n",
    "tests/test_login.py": "from src.auth.login import login\n\n\ndef test_login():\n    assert login('a') == 'a'\n",
}
_SIGNATURE = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
_BLOB_MODE = pygit2.enums.FileMode.BLOB


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_PREWARM", "0")
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "lemoncrow-root"))


def _write(root: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    return str(repo.create_commit("HEAD", _SIGNATURE, _SIGNATURE, message, tree, parents))


@dataclass
class Split:
    client: TestClient
    store: ReviewStore
    repo_root: Path
    review_id: str
    base: str
    head: str

    def latest(self) -> Any:
        revision = self.store.latest_revision(self.review_id)
        assert revision is not None
        return revision

    def get(self, path: str, review_id: str = "") -> Any:
        return self.client.get(f"/api/reviews/{review_id or self.review_id}/{path}", headers=HEADERS)

    def post(self, path: str, body: Mapping[str, Any] | None = None, *, expected: bool = True) -> Any:
        payload = dict(body or {})
        if expected:
            payload.setdefault("expected_revision_id", self.latest().id)
        return self.client.post(f"/api/reviews/{self.review_id}/{path}", headers=HEADERS, json=payload)

    def preview(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self.post("split-requests/preview", body)
        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()
        return payload

    def start(
        self, body: Mapping[str, Any] | None = None, preview: Mapping[str, Any] | None = None, **extra: Any
    ) -> Any:
        shown = preview or self.preview(body)
        return self.post(
            "split-requests",
            {
                **dict(body or {}),
                "operation_id": shown["operation_id"],
                "expected_handoff_digest": shown["handoff_digest"],
                **extra,
            },
        )

    def collect(self, split_id: str, body: Mapping[str, Any] | None = None) -> Any:
        return self.client.post(
            f"/api/reviews/{self.review_id}/split-requests/{split_id}/collect", headers=HEADERS, json=dict(body or {})
        )

    def splits(self) -> dict[str, Any]:
        response = self.get("split-requests")
        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()
        return payload

    def review_ids(self) -> set[str]:
        return {item.id for item in self.store.list_sessions(status="", limit=None)}


def _split(tmp_path: Path, *, trusted: bool = True, head_files: Mapping[str, str] | None = None) -> Split:
    repo_root = tmp_path / "repo"
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    _write(repo_root, _BASE_FILES)
    base = _commit(repo, "base")
    _write(repo_root, head_files or _HEAD_FILES)
    head = _commit(repo, "mixed change")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, base=base, head=head)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title="Mixed change")
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    extra: dict[str, Any] = {"source_repo_root": repo_root, "source_store_root": store_root}
    if not trusted:
        extra["source_refresh_enabled"] = False  # a server without the developer's checkout (hosted shape)
    register_review_api(
        app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=repo_root, port=PORT, **extra
    )
    return Split(TestClient(app, base_url=BASE), store, repo_root, session.id, base, head)


# --------------------------------------------------------------------------- #
# A fake authoring agent that follows the handoff                             #
# --------------------------------------------------------------------------- #

_LAYER_LINE = re.compile(r"^(\d+)\. branch (\S+) \(from ([^)]+)\) holds the files of data layer (\d+)$")
_HEAD_LINE = re.compile(r"taking them as they are in commit ([0-9a-f]{40})")
_DATA_LAYER = re.compile(r"^### Data layer (\d+) of \d+$")


class FakeAuthor:
    """Reads only the handoff text, then commits one stacked layer per branch."""

    def __init__(self, repo_root: Path) -> None:
        self.repo = pygit2.Repository(str(repo_root))

    @staticmethod
    def read(handoff: str) -> tuple[str, list[tuple[str, str, list[str]]]]:
        head_match = _HEAD_LINE.search(handoff)
        assert head_match is not None, "the handoff names the commit to reproduce"
        directive: list[tuple[str, str]] = []
        files: dict[int, list[str]] = {}
        current = 0
        for line in handoff.splitlines():
            layer = _LAYER_LINE.match(line)
            if layer:
                directive.append((layer.group(2), layer.group(3)))
            data = _DATA_LAYER.match(line)
            if data:
                current = int(data.group(1))
            elif current and line.startswith(DATA_LINE_PREFIX + "- "):
                files.setdefault(current, []).append(line[len(DATA_LINE_PREFIX) + 2 :])
        return head_match.group(1), [
            (branch, start, files.get(index, [])) for index, (branch, start) in enumerate(directive, start=1)
        ]

    def blob(self, commit: str, path: str) -> bytes | None:
        try:
            obj = self.repo.revparse_single(f"{commit}:{path}")
        except KeyError:
            return None
        return bytes(obj.data)

    def commit_layer(self, parent: str, files: Mapping[str, bytes | None], branch: str) -> str:
        index = pygit2.Index()
        index.read_tree(self.repo[parent].peel(pygit2.Commit).tree)
        mode = _BLOB_MODE
        for path, data in files.items():
            if data is None:
                if path in index:
                    index.remove(path)
            else:
                index.add(pygit2.IndexEntry(path, self.repo.create_blob(data), mode))
        tree = index.write_tree(self.repo)
        oid = self.repo.create_commit(None, _SIGNATURE, _SIGNATURE, f"split: {branch}", tree, [pygit2.Oid(hex=parent)])
        self.repo.branches.local.create(branch, self.repo[oid].peel(pygit2.Commit), force=True)
        return str(oid)

    def follow(
        self,
        handoff: str,
        *,
        extra: Mapping[int, Mapping[str, bytes | None]] | None = None,
        only: Sequence[int] | None = None,
        restart: Sequence[int] = (),
    ) -> list[str]:
        """Build the branches as told; *extra* adds files to a layer, *restart* bases a layer on the original base."""

        head, layers = self.read(handoff)
        heads: list[str] = []
        base = layers[0][1]
        previous = base
        for index, (branch, _start, paths) in enumerate(layers, start=1):
            if only is not None and index not in only:
                continue
            files: dict[str, bytes | None] = {path: self.blob(head, path) for path in paths}
            files.update((extra or {}).get(index, {}))
            previous = self.commit_layer(base if index in restart else previous, files, branch)
            heads.append(previous)
        return heads


def _judge(split: Split) -> dict[str, Any]:
    """A human judges every unit of the original; one unit was marked by an agent instead."""

    revision = split.latest()
    session = split.store.get_session(split.review_id)
    assert session is not None
    units = {unit.unit_key: unit for unit in split.store.list_units(revision.id)}
    agent_key = next(key for key, unit in units.items() if unit.path == "docs/guide.md" and unit.kind == "hunk")
    needs_key = next(
        key for key, unit in units.items() if unit.path == "src/billing/invoice.py" and unit.kind == "file"
    )
    for key, unit in units.items():
        split.store.set_mark(
            ReviewMark(
                review_id=split.review_id,
                unit_key=key,
                state="needs_changes" if key == needs_key else "reviewed",
                reviewed_revision_id=revision.id,
                content_fingerprint=unit.content_fingerprint,
                reviewer_id=session.reviewer_id,
                actor_type="agent" if key == agent_key else "human",
            )
        )
    return {"units": units, "agent_key": agent_key, "needs_key": needs_key, "reviewer": session.reviewer_id}


# --------------------------------------------------------------------------- #
# Pure plan                                                                    #
# --------------------------------------------------------------------------- #


def test_the_plan_follows_cohorts_in_canonical_order_and_never_drops_a_path() -> None:
    cohorts = [
        {"id": "area:auth", "title": "Auth", "primary_paths": ["src/auth.py"], "related_paths": ["src/shared.py"]},
        {"id": "area:billing", "title": "Billing", "primary_paths": ["src/billing.py", "src/auth.py"]},
        {"id": "area:empty", "title": "Nothing changed here", "primary_paths": ["src/gone.py"]},
    ]
    changed = ["src/auth.py", "src/billing.py", "src/shared.py", "README.md", "src/auth.py"]
    plan = plan_from_cohorts(cohorts, changed, review_short="abc123", base_sha=SHA_A, head_sha=SHA_B)
    assert [(layer.cohort_id, layer.paths) for layer in plan.layers] == [
        ("area:auth", ("src/auth.py", "src/shared.py")),
        ("area:billing", ("src/billing.py",)),
        (REMAINING_COHORT, ("README.md",)),
    ]
    assert [layer.branch for layer in plan.layers] == [f"lemoncrow/split/abc123/{n:02d}" for n in (1, 2, 3)]
    assert sorted(path for layer in plan.layers for path in layer.paths) == sorted(set(changed))
    assert SplitPlan.from_wire(plan.to_wire()) == plan
    assert plan.to_wire()["digest"] == plan.digest


def _cohorts(count: int) -> tuple[list[dict[str, Any]], list[str]]:
    cohorts = [{"id": f"c{n}", "title": f"C{n}", "primary_paths": [f"src/c{n}.py"]} for n in range(count)]
    return cohorts, [f"src/c{n}.py" for n in range(count)]


def test_plan_bounds_refuse_visibly() -> None:
    with pytest.raises(SplitRefused, match="nothing to split"):
        plan_from_cohorts(*_cohorts(1), review_short="abcd", base_sha=SHA_A, head_sha=SHA_B)
    assert len(plan_from_cohorts(*_cohorts(2), review_short="abcd", base_sha=SHA_A, head_sha=SHA_B).layers) == 2
    at_bound = plan_from_cohorts(*_cohorts(MAX_SPLIT_LAYERS), review_short="abcd", base_sha=SHA_A, head_sha=SHA_B)
    assert len(at_bound.layers) == MAX_SPLIT_LAYERS
    with pytest.raises(SplitRefused, match=f"at most {MAX_SPLIT_LAYERS}"):
        plan_from_cohorts(*_cohorts(MAX_SPLIT_LAYERS + 1), review_short="abcd", base_sha=SHA_A, head_sha=SHA_B)

    def crowded(size: int) -> SplitPlan:
        big = [f"src/big/{n}.py" for n in range(size)]
        cohorts = [{"id": "big", "title": "Big", "primary_paths": big}, {"id": "one", "primary_paths": ["x.py"]}]
        return plan_from_cohorts(cohorts, [*big, "x.py"], review_short="abcd", base_sha=SHA_A, head_sha=SHA_B)

    assert len(crowded(MAX_LAYER_PATHS).layers[0].paths) == MAX_LAYER_PATHS
    with pytest.raises(SplitRefused, match=f"at most {MAX_LAYER_PATHS}"):
        crowded(MAX_LAYER_PATHS + 1)
    with pytest.raises(SplitRefused, match="committed Change"):
        plan_from_cohorts(*_cohorts(2), review_short="abcd", base_sha=SHA_A, head_sha="WORKTREE")


def test_the_split_handoff_delimits_cohort_text_as_data() -> None:
    """Cohort titles, file paths and the Change description are data; they cannot redefine the directive."""

    titles = list(INJECTIONS.values())
    cohorts = [
        {"id": f"c{n}", "title": title, "primary_paths": [f"src/{name}.py"]}
        for n, (name, title) in enumerate(zip(INJECTIONS, titles, strict=True))
    ]
    changed = [f"src/{name}.py" for name in INJECTIONS]
    plan = plan_from_cohorts(cohorts, changed, review_short="abcd", base_sha=SHA_A, head_sha=SHA_B)
    hostile = render_split_handoff(
        plan,
        split_id="spl_test",
        reviewer_id="alice",
        snapshot_label="Revision 2",
        change_description=INJECTIONS["pr_text"],
    )
    benign_cohorts = [{**cohort, "title": f"Cohort {n}"} for n, cohort in enumerate(cohorts)]
    benign = render_split_handoff(
        plan_from_cohorts(benign_cohorts, changed, review_short="abcd", base_sha=SHA_A, head_sha=SHA_B),
        split_id="spl_test",
        reviewer_id="alice",
        snapshot_label="Revision 2",
        change_description="A normal description",
    )
    assert hostile.startswith(f"{HANDOFF_HEADER} spl_test · split request · Revision 2")
    inside, outside = _data_lines(hostile)
    for marker in _MARKERS:
        assert not any(marker in line for line in outside), marker
    joined = "\n".join(inside)
    assert "PUSH-FORCE-TO-MAIN" in joined and "DISABLE-THE-SANDBOX" in joined  # kept as visible data
    assert all(line.startswith(DATA_LINE_PREFIX.rstrip()) for line in inside)
    # The trusted part (everything outside the fences) is identical to a benign plan's.
    assert outside == _data_lines(benign)[1]
    # The directive names only LemonCrow's branches, in plan order, and forbids pushing.
    _head, layers = FakeAuthor.read(hostile)
    assert [branch for branch, _start, _paths in layers] == [layer.branch for layer in plan.layers]
    assert "do not push" in hostile


# --------------------------------------------------------------------------- #
# End to end                                                                   #
# --------------------------------------------------------------------------- #


def test_a_split_produces_linked_stacked_reviews_and_carries_exact_judgments(tmp_path: Path) -> None:
    split = _split(tmp_path)
    judged = _judge(split)
    original_revision = split.latest()
    before = split.review_ids()

    preview = split.preview({"directive": "Keep each layer green."})
    assert preview["ready"] is True and preview["collect_available"] is True
    plan = preview["plan"]
    # Layers are named as the Brief names its areas: the real folder, never an invented area name.
    assert [layer["title"] for layer in plan["layers"]] == ["src/billing/", "src/auth/", "Tests", "Docs"]
    assert preview["route"]["tier"] == "prompt_fallback"
    assert "Keep each layer green." in preview["handoff"]

    # The copy route needs the reviewer to confirm the handoff, like Fix.
    unconfirmed = split.start({"directive": "Keep each layer green."}, preview)
    assert unconfirmed.status_code == 409 and "copied: true" in unconfirmed.text
    started = split.start({"directive": "Keep each layer green."}, preview, copied=True)
    assert started.status_code == 200, started.text
    record = started.json()["split"]
    assert record["status"] == "awaiting_branches" and record["plan"]["digest"] == plan["digest"]
    replay = split.start({"directive": "Keep each layer green."}, preview, copied=True)
    assert replay.json()["replayed"] is True and replay.json()["split"]["id"] == record["id"]
    # One split at a time per Review.
    busy = split.preview()
    assert busy["ready"] is False and "still awaiting_branches" in busy["reason"]

    # Collecting before the agent made its branches changes nothing.
    early = split.collect(record["id"])
    assert early.status_code == 409 and "has not created" in early.text
    assert split.review_ids() == before
    assert split.splits()["splits"][0]["status"] == "awaiting_branches"

    FakeAuthor(split.repo_root).follow(preview["handoff"])
    wrong_plan = split.collect(record["id"], {"expected_plan_digest": "0" * 64})
    assert wrong_plan.status_code == 409
    collected = split.collect(record["id"], {"expected_plan_digest": plan["digest"]})
    assert collected.status_code == 200, collected.text
    done = collected.json()["split"]
    assert done["status"] == "completed"
    children = done["children"]
    assert [child["layer"] for child in children] == [1, 2, 3, 4]
    assert all(child["status"] == "open" and child["extra_paths"] == [] for child in children)
    assert split.review_ids() == before | {child["review_id"] for child in children}

    # The original is linked, never closed, and its Snapshot did not move.
    original = split.store.get_session(split.review_id)
    assert original is not None and original.status == "open"
    assert split.latest().id == original_revision.id
    into = [rel for rel in split.store.list_relationships(split.review_id) if rel.relation == "split_into"]
    assert [rel.target_review_id for rel in into] == [child["review_id"] for child in children]

    previous = ""
    for child, layer in zip(children, plan["layers"], strict=True):
        session = split.store.get_session(child["review_id"])
        assert session is not None and session.subject_type == "commit_range"
        assert session.title.startswith(f"Split {layer['index']}/4 · {layer['title']}")
        revision = split.store.latest_revision(child["review_id"])
        assert revision is not None and revision.head_sha == child["head_sha"]
        assert sorted({unit.path for unit in split.store.list_units(revision.id)}) == layer["paths"]
        links = {(rel.relation, rel.target_review_id) for rel in split.store.list_relationships(child["review_id"])}
        assert ("split_from", split.review_id) in links
        if previous:
            assert ("depends_on", previous) in links
        previous = child["review_id"]
        shown = split.get("split-requests", child["review_id"]).json()["split_from"]
        assert shown == [
            {
                "review_id": split.review_id,
                "split_id": record["id"],
                "layer": layer["index"],
                "layer_count": 4,
                "branch": layer["branch"],
            }
        ]

    # Every human judgment on an identical target carried; the agent's mark did not.
    migration = done["migration"]
    human = len(judged["units"]) - 1
    assert migration["carried"] == human and migration["not_carried_total"] == 0
    carried: dict[str, str] = {}
    for child in children:
        for mark in split.store.list_marks(child["review_id"], reviewer_id=judged["reviewer"]):
            assert mark.actor_type == "human" and mark.reviewed_revision_id == child["revision_id"]
            carried[mark.unit_key] = mark.state
    assert judged["agent_key"] not in carried
    assert carried[judged["needs_key"]] == "needs_changes"
    assert len(carried) == human
    events = split.store.list_mark_events(children[0]["review_id"], reviewer_id=judged["reviewer"])
    assert events and all(event.event_kind == "migrated" for event in events)
    assert all(f"carried by split from {split.review_id}" in event.reason for event in events)

    # A completed split frees the Review for another request.
    assert split.preview()["ready"] is True


def test_a_target_the_agent_changed_does_not_carry_its_judgment(tmp_path: Path) -> None:
    """The agent puts a partial login.py in layer 1; only identical targets keep their verdict."""

    split = _split(tmp_path)
    judged = _judge(split)
    preview = split.preview()
    record = split.start({}, preview, copied=True).json()["split"]
    auth_layer = next(layer["index"] for layer in preview["plan"]["layers"] if layer["title"] == "src/auth/")
    assert auth_layer == 2
    # Judged while the split runs (after the plan, which orders cohorts by attention):
    # needs_changes is the state reconcile keeps across a content change on the same
    # Review; a produced Review still receives it only for identical content.
    revision = split.latest()
    for key, unit in judged["units"].items():
        if unit.path == "src/auth/login.py":
            split.store.set_mark(
                ReviewMark(
                    review_id=split.review_id,
                    unit_key=key,
                    state="needs_changes",
                    reviewed_revision_id=revision.id,
                    content_fingerprint=unit.content_fingerprint,
                    reviewer_id=judged["reviewer"],
                    actor_type="human",
                )
            )
    partial = b"def login(user):\n    if not user:\n        return None\n    return user\n"
    FakeAuthor(split.repo_root).follow(preview["handoff"], extra={1: {"src/auth/login.py": partial}})
    done = split.collect(record["id"]).json()["split"]
    assert done["status"] == "completed"
    first, second = done["children"][0], done["children"][1]
    assert first["extra_paths"] == ["src/auth/login.py"]  # reported, not refused

    login_units = {key: unit for key, unit in judged["units"].items() if unit.path == "src/auth/login.py"}
    in_first = {mark.unit_key for mark in split.store.list_marks(first["review_id"], reviewer_id=judged["reviewer"])}
    assert not in_first & set(login_units), "a judgment never lands on a partial version of its target"
    second_marks = split.store.list_marks(second["review_id"], reviewer_id=judged["reviewer"])
    in_second = {mark.unit_key: mark.content_fingerprint for mark in second_marks}
    assert {mark.state for mark in second_marks if mark.unit_key in login_units} == {"needs_changes"}
    second_units = {
        unit.unit_key: unit
        for unit in split.store.list_units(second["revision_id"])
        if unit.path == "src/auth/login.py"
    }
    for key, unit in login_units.items():
        same = key in second_units and second_units[key].content_fingerprint == unit.content_fingerprint
        assert (key in in_second) is same, (unit.kind, key)
        if same:
            assert in_second[key] == unit.content_fingerprint
    changed = {key for key in login_units if key not in in_second}
    assert changed, "the layer-2 diff against the partial file differs somewhere"
    reasons = {item["unit_key"]: item["reason"] for item in done["migration"]["not_carried"]}
    assert {key: reasons.get(key) for key in changed} == dict.fromkeys(changed, "the target changed")
    assert done["migration"]["not_carried_total"] == len(changed)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda author, handoff: author.follow(handoff, restart=(2,)), "is not stacked on layer 1"),
        (
            lambda author, handoff: author.follow(handoff, extra={4: {"docs/guide.md": b"# Guide\n\nOther.\n"}}),
            "does not reproduce the reviewed commit",
        ),
        (lambda author, handoff: author.follow(handoff, only=(1, 2, 3)), "has not created"),
    ],
    ids=["not-stacked", "wrong-final-tree", "missing-layer"],
)
def test_a_failed_collect_leaves_the_original_intact(tmp_path: Path, build: Any, message: str) -> None:
    split = _split(tmp_path)
    _judge(split)
    marks_before = split.store.list_marks(split.review_id, reviewer_id="local")
    links_before = split.store.list_relationships(split.review_id)
    before = split.review_ids()
    preview = split.preview()
    record = split.start({}, preview, copied=True).json()["split"]
    build(FakeAuthor(split.repo_root), preview["handoff"])
    failed = split.collect(record["id"])
    assert failed.status_code == 409 and message in failed.text
    assert split.review_ids() == before
    assert split.store.list_relationships(split.review_id) == links_before
    assert split.store.list_marks(split.review_id, reviewer_id="local") == marks_before
    state = split.splits()["splits"][0]
    assert state["status"] == "awaiting_branches" and message in state["message"]


def test_a_collect_that_fails_midway_rolls_back_every_review_it_created(tmp_path: Path) -> None:
    split = _split(tmp_path)
    preview = split.preview()
    record = split.start({}, preview, copied=True).json()["split"]
    heads = FakeAuthor(split.repo_root).follow(preview["handoff"])
    # Someone already reviews layer 3's exact range: collecting must not adopt it.
    rng = resolve_rev_range(split.repo_root, None, base=heads[1], head=heads[2])
    squatter = open_or_create_session(split.store, split.repo_root, rng, title="already here")
    links_before = split.store.list_relationships(split.review_id)
    squatter_links = split.store.list_relationships(squatter.id)
    before = split.review_ids()
    failed = split.collect(record["id"])
    assert failed.status_code == 409 and "already an open Review" in failed.text
    assert split.review_ids() == before and squatter.id in before
    assert split.store.list_relationships(split.review_id) == links_before
    assert split.store.list_relationships(squatter.id) == squatter_links
    assert split.splits()["splits"][0]["status"] == "awaiting_branches"


def test_cancel_leaves_the_original_intact_and_stops_collection(tmp_path: Path) -> None:
    split = _split(tmp_path)
    preview = split.preview()
    record = split.start({}, preview, copied=True).json()["split"]
    FakeAuthor(split.repo_root).follow(preview["handoff"])
    before = split.review_ids()
    cancelled = split.client.post(
        f"/api/reviews/{split.review_id}/split-requests/{record['id']}/cancel",
        headers=HEADERS,
        json={"reason": "too early"},
    )
    assert cancelled.status_code == 200 and cancelled.json()["split"]["status"] == "cancelled"
    assert split.collect(record["id"]).status_code == 409
    again = split.client.post(
        f"/api/reviews/{split.review_id}/split-requests/{record['id']}/cancel", headers=HEADERS, json={}
    )
    assert again.status_code == 409
    assert split.review_ids() == before
    original = split.store.get_session(split.review_id)
    assert original is not None and original.status == "open"
    assert split.preview()["ready"] is True


def test_a_newer_snapshot_refuses_collection(tmp_path: Path) -> None:
    split = _split(tmp_path)
    preview = split.preview()
    record = split.start({}, preview, copied=True).json()["split"]
    FakeAuthor(split.repo_root).follow(preview["handoff"])
    session = split.store.get_session(split.review_id)
    assert session is not None
    _write(split.repo_root, {"docs/guide.md": "# Guide\n\nHello again.\n"})
    repo = pygit2.Repository(str(split.repo_root))
    newer = _commit(repo, "later")
    rng = resolve_rev_range(split.repo_root, None, base=split.base, head=newer)
    revision = snapshot_revision(split.store, session, split.repo_root, rng, store_root=tmp_path / "store")
    split.store.publish_snapshot(split.review_id, revision.id)
    refused = split.collect(record["id"])
    assert refused.status_code == 409 and "newer revision" in refused.text


# --------------------------------------------------------------------------- #
# Same gates and routes as Fix                                                 #
# --------------------------------------------------------------------------- #


def test_split_requests_use_the_fix_gates(tmp_path: Path) -> None:
    split = _split(tmp_path)
    missing = split.post("split-requests/preview", {}, expected=False)
    assert missing.status_code == 400 and "expected_revision_id" in missing.text
    stale = split.post("split-requests/preview", {"expected_revision_id": "rev-old"})
    assert stale.status_code == 409
    unknown = split.post("split-requests/preview", {"paths": ["src/auth/login.py"]})
    assert unknown.status_code == 422 and "unsupported split field" in unknown.text
    reviewer_apply = split.post("split-requests/preview", {"route": "reviewer_apply"})
    assert reviewer_apply.status_code == 422
    no_operation = split.post("split-requests", {})
    assert no_operation.status_code == 400
    preview = split.preview()
    tampered = split.post(
        "split-requests",
        {"operation_id": preview["operation_id"], "expected_handoff_digest": "0" * 64, "copied": True},
    )
    assert tampered.status_code == 409 and "changed since its preview" in tampered.text
    split.store.update_session(split.review_id, status="archived")
    archived = split.post("split-requests/preview", {})
    fix = split.post("actions/preview", {"subjects": []})
    assert archived.status_code == fix.status_code == 409


def test_nothing_to_split_is_said_plainly(tmp_path: Path) -> None:
    split = _split(tmp_path, head_files={**_BASE_FILES, "src/auth/login.py": _LOGIN})
    preview = split.preview()
    assert preview["ready"] is False and "nothing to split" in preview["reason"] and preview["plan"] is None
    refused = split.start({}, {**preview, "operation_id": "spl_x", "handoff_digest": ""})
    assert refused.status_code == 409 and "nothing to split" in refused.text


def test_collect_needs_the_trusted_checkout(tmp_path: Path) -> None:
    split = _split(tmp_path, trusted=False)
    listing = split.splits()
    assert listing["collect_available"] is False and "trusted local checkout" in listing["collect_reason"]
    refused = split.collect("spl_missing")
    assert refused.status_code == 409 and "trusted local checkout" in refused.text


def test_the_exact_authoring_session_receives_the_split_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    split = _split(tmp_path)
    revision = split.latest()
    split.store.add_revision_attributions(
        revision.id,
        (
            ReviewAttribution(
                revision_id=revision.id,
                scope="revision",
                scope_key="",
                host="claude",
                session_id="s-author",
                role="primary",
                certainty="exact",
                method="recorded_edit",
            ),
        ),
    )
    deliveries = Deliveries()
    monkeypatch.setattr(delivery_module, "deliver_prompt_to_agent_session", deliveries)
    preview = split.preview()
    assert preview["route"]["tier"] == "exact_session", preview["candidates"]
    started = split.start({}, preview)
    assert started.status_code == 200, started.text
    record = started.json()["split"]
    assert record["status"] == "awaiting_branches"
    ((host, session_id, repo, prompt),) = deliveries.calls
    assert (host, session_id, repo) == ("claude", "s-author", split.repo_root)
    assert prompt == preview["handoff"]
    # The handoff is linked in Development like a Fix handoff.
    development = DevelopmentStore(split.store)
    rows = development.list_events(split.review_id, limit=50)
    assert any(row.causation_id == f"split:{record['id']}" for row in rows)

    # The agent follows it from the delivered prompt alone.
    FakeAuthor(split.repo_root).follow(prompt)
    assert split.collect(record["id"]).json()["split"]["status"] == "completed"


def test_a_failed_delivery_leaves_the_original_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    split = _split(tmp_path)
    revision = split.latest()
    split.store.add_revision_attributions(
        revision.id,
        (
            ReviewAttribution(
                revision_id=revision.id,
                scope="revision",
                scope_key="",
                host="claude",
                session_id="s-author",
                role="primary",
                certainty="exact",
                method="recorded_edit",
            ),
        ),
    )
    monkeypatch.setattr(delivery_module, "deliver_prompt_to_agent_session", Deliveries("failed"))
    before = split.review_ids()
    record = split.start({}).json()["split"]
    assert record["status"] == "failed"
    assert split.review_ids() == before
    assert split.collect(record["id"]).status_code == 409
    assert split.preview()["ready"] is True  # a failed split never blocks a new one
    assert json.dumps(split.splits()["splits"][0]["events"]).count("delivery_failed") == 1
