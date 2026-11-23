"""P10-1: every route on the Reader's open path reads the Snapshot's source-tree manifest once, and each file once."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.gateway.test_review_check_runs_api import (
    CONFIG,
    HEADERS,
    _base_files,
    _head_files,
    _new_snapshot,
    _review,
)

ROUTES = {
    "overview": "/api/reviews/{id}",
    "historical_overview": "/api/reviews/{id}/revisions/0",
    "historical_targets": "/api/reviews/{id}/revisions/0/targets",
    "surfaces": "/api/reviews/{id}/surfaces",
    "targets": "/api/reviews/{id}/targets",
    "check_recipes": "/api/reviews/{id}/check-recipes",
    "map": "/api/reviews/{id}/map",
    "historical_map": "/api/reviews/{id}/revisions/0/map",
    "evidence_qualification": "/api/reviews/{id}/evidence-qualification",
    "projection": "/api/reviews/{id}/projection",
    "readiness": "/api/reviews/{id}/readiness",
    "guidance": "/api/reviews/{id}/guidance",
    "historical_guidance": "/api/reviews/{id}/revisions/0/guidance",
    "run_plan": "/api/reviews/{id}/run-plan",
    "runtime_observation": "/api/reviews/{id}/runtime-observation",
    "interactive_preview": "/api/reviews/{id}/interactive-preview",
    "artifact_analyses": "/api/reviews/{id}/artifact-analyses",
    "automation": "/api/reviews/{id}/automation",
    "verification": "/api/reviews/{id}/verification",
    "behavior": "/api/reviews/{id}/behavior",
    "landing_auto_approval": "/api/reviews/{id}/landing/auto-approval",
    "finish": "/api/reviews/{id}/finish",
    "measures": "/api/reviews/{id}/measures",
    "reviewer_suggestions": "/api/reviews/{id}/reviewer-suggestions",
}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _manifest(files: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    return {path: {"content_digest": _digest(raw), "mode": 0o100644, "size": len(raw)} for path, raw in files.items()}


def _counted(review: SimpleNamespace, base: dict[str, str], head: dict[str, str | None]) -> SimpleNamespace:
    """Write real base and head manifests for the Snapshot, and count every read of them and of their contents."""

    extra = {f"pkg/module_{index:03d}.py": f"VALUE = {index}\n" for index in range(40)}
    head_files = {path: text for path, text in {**base, **extra, **head}.items() if text is not None}
    base_files = {**base, **{path: text for path, text in extra.items()}}
    base_bytes = {path: text.encode() for path, text in base_files.items()}
    head_bytes = {path: text.encode() for path, text in head_files.items()}
    by_digest = {_digest(raw): raw for raw in (*base_bytes.values(), *head_bytes.values())}
    for side, files in (("old", base_bytes), ("new", head_bytes)):
        review.store.write_source_tree_artifact(review.review_id, review.revision_id, _manifest(files), side=side)
    manifest_reads: Counter[str] = Counter()
    content_reads: Counter[str] = Counter()
    original_tree = review.store.read_source_tree_artifact

    def counting_tree(*args: Any, **kwargs: Any) -> Any:
        manifest_reads[str(kwargs.get("side", "new"))] += 1  # the store's own default side
        return original_tree(*args, **kwargs)

    def counting_content(digest: str) -> bytes | None:
        content_reads[digest] += 1
        return by_digest.get(digest)

    review.store.read_source_tree_artifact = counting_tree
    review.store.read_source_content = counting_content
    return SimpleNamespace(
        review=review,
        manifest_reads=manifest_reads,
        content_reads=content_reads,
        head_digests={_digest(raw): path for path, raw in head_bytes.items() if base_bytes.get(path) != raw},
    )


def _fetch(counted: SimpleNamespace, route: str) -> Any:
    review = counted.review
    counted.manifest_reads.clear()
    counted.content_reads.clear()
    response = review.client.get(ROUTES[route].format(id=review.review_id), headers=HEADERS)
    assert response.status_code == 200, response.text
    return response


# A plain change, and one that deletes a file and changes the head's review policy.
CHANGES: dict[str, dict[str, str | None]] = {
    "plain": dict(_head_files()),
    "deleted_file_and_changed_policy": {
        **_head_files(),
        "fmt.py": None,
        ".lemoncrow/review.yaml": CONFIG + "  max_runs: 3\n",
    },
}


# What each route reads, exactly: the sides whose manifest it loads (once each), and which changed head files it reads
# (each once): "all" every changed file, "policy" only the review policy, "none" no head file.
EXPECTED: dict[str, tuple[tuple[str, ...], str]] = {
    "overview": (("new", "old"), "policy"),
    "historical_overview": (("new", "old"), "policy"),
    "historical_targets": (("old",), "none"),
    "surfaces": (("new", "old"), "all"),
    "targets": (("old",), "none"),
    "check_recipes": (("new", "old"), "all"),
    "map": (("old",), "none"),
    "historical_map": (("old",), "none"),
    "evidence_qualification": (("old",), "none"),
    "projection": (("new", "old"), "all"),
    "readiness": (("new", "old"), "all"),
    "guidance": (("old",), "none"),
    "historical_guidance": (("old",), "none"),
    "run_plan": (("new", "old"), "all"),
    "runtime_observation": (("new", "old"), "all"),
    "interactive_preview": (("new", "old"), "all"),
    "artifact_analyses": (("new", "old"), "all"),
    "automation": (("new", "old"), "all"),
    "verification": (("old",), "none"),
    "behavior": (("new", "old"), "all"),
    "landing_auto_approval": (("new", "old"), "all"),
    "finish": (("new", "old"), "policy"),
    "measures": (("old",), "none"),
    "reviewer_suggestions": (("old",), "none"),
}
POLICY = ".lemoncrow/review.yaml"


def test_every_route_has_an_exact_expectation() -> None:
    assert sorted(EXPECTED) == sorted(ROUTES)


@pytest.mark.parametrize("change", sorted(CHANGES))
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_reader_route_reads_the_manifest_once_and_each_changed_file_once(
    tmp_path: Path, route: str, change: str
) -> None:
    """The Reader's open path reloaded the Snapshot's whole source-tree manifest a dozen times per request.

    On a 10,000-file change each reload parsed 10,000 rows, and the head catalog read each changed file twice. The
    Snapshot carries real manifests for both sides here, and the app is new, so no process cache hides a read.
    """

    head = CHANGES[change]
    counted = _counted(_review(tmp_path, head=head), _base_files(), head)
    assert counted.head_digests, "the change must carry changed head files"
    _fetch(counted, route)
    sides, files = EXPECTED[route]
    assert dict(counted.manifest_reads) == {
        side: 1 for side in sides
    }, f"{route}: manifest reads {counted.manifest_reads}"
    reads = {path: counted.content_reads[digest] for digest, path in counted.head_digests.items()}
    wanted = {"all": set(reads), "policy": {POLICY} & set(reads), "none": set()}[files]
    assert reads == {path: int(path in wanted) for path in reads}, f"{route}: reads per changed file {reads}"


def _many_modified_files(tmp_path: Path, count: int) -> SimpleNamespace:
    """A Snapshot where ``count`` routine-language files are modified, so the proofs read each one's old side."""

    modules = {f"pkg/mod_{index:03d}.py": f"def f():\n    return {index}\n" for index in range(count)}
    base = {**_base_files(), **modules}
    head = {**_head_files(), **{path: text.replace("return", "return 1 +") for path, text in modules.items()}}
    return _counted(_review(tmp_path / f"n{count}", base=base, head=head), base, head)


def _mark_one(counted: SimpleNamespace) -> Counter[str]:
    review = counted.review
    unit = review.store.list_units(review.revision_id)[0].unit_key
    counted.manifest_reads.clear()
    response = review.client.post(
        f"/api/reviews/{review.review_id}/marks",
        headers=HEADERS,
        json={"unit_key": unit, "state": "reviewed", "expected_revision_id": review.revision_id},
    )
    assert response.status_code == 200, response.text
    return Counter(counted.manifest_reads)


def test_the_routine_proofs_read_the_old_manifest_once_however_many_files_were_modified(tmp_path: Path) -> None:
    """A mark is outside any request scope; the proofs it computes cold must not reload the manifest per modified file."""

    few = _mark_one(_many_modified_files(tmp_path, 2))
    many = _mark_one(_many_modified_files(tmp_path, 30))
    assert many == few, f"old-side manifest reads grew with the modified files: {few} -> {many}"


@pytest.mark.parametrize("route", ["guidance", "historical_guidance"])
def test_guidance_reads_each_manifest_once_cold_with_many_modified_files(tmp_path: Path, route: str) -> None:
    counted = _many_modified_files(tmp_path, 30)
    _fetch(counted, route)
    assert dict(counted.manifest_reads) == {"old": 1}


# The Checks tab and welcome-back screen's routes: they once read the manifest per modified file.
GROWING = [
    "run_plan",
    "runtime_observation",
    "interactive_preview",
    "artifact_analyses",
    "automation",
    "verification",
    "behavior",
    "landing_auto_approval",
    "finish",
    "measures",
    "reviewer_suggestions",
]


@pytest.mark.parametrize("route", GROWING)
def test_a_route_reads_the_same_however_many_files_were_modified(tmp_path: Path, route: str) -> None:
    """Thirty modified files cost the same manifest reads as two, and no changed file is read twice."""

    seen = []
    for count in (2, 30):
        counted = _many_modified_files(tmp_path, count)
        _fetch(counted, route)
        sides = EXPECTED[route][0]
        assert dict(counted.manifest_reads) == {
            side: 1 for side in sides
        }, f"{route} N={count}: {counted.manifest_reads}"
        reads = {path: counted.content_reads[digest] for digest, path in counted.head_digests.items()}
        assert max(reads.values()) <= 1, f"{route} N={count}: {reads}"
        seen.append(dict(counted.manifest_reads))
    assert seen[0] == seen[1]


@pytest.mark.parametrize("route", ["run_plan", "runtime_observation", "artifact_analyses"])
def test_an_older_snapshot_of_a_route_reads_each_manifest_once(tmp_path: Path, route: str) -> None:
    counted = _counted(_review(tmp_path), _base_files(), _head_files())
    review = counted.review
    first = review.revision_id
    _new_snapshot(review, {"calc.py": "def add(a, b):\n    return a + b + 2\n"})
    counted.manifest_reads.clear()
    response = review.client.get(
        ROUTES[route].format(id=review.review_id), headers=HEADERS, params={"revision_id": first}
    )
    assert response.status_code == 200, response.text
    assert response.json()["historical"] is True
    assert dict(counted.manifest_reads) == {"old": 1, "new": 1}


@pytest.mark.parametrize("route", ["compare", "compare_file"])
def test_comparing_two_snapshots_reads_each_manifest_once(tmp_path: Path, route: str) -> None:
    """Comparing reads both Snapshots' manifests, each once, to find the paths that differ and then their bytes."""

    counted = _counted(_review(tmp_path), _base_files(), _head_files())
    review = counted.review
    first = review.revision_id
    second = _new_snapshot(review, {"calc.py": "def add(a, b):\n    return a + b + 2\n"})
    reads: Counter[tuple[str, str]] = Counter()
    original = review.store.read_source_tree_artifact

    def counting(review_id: str, revision_id: str, *args: Any, **kwargs: Any) -> Any:
        reads[(revision_id, str(kwargs.get("side", "new")))] += 1
        return original(review_id, revision_id, *args, **kwargs)

    review.store.read_source_tree_artifact = counting
    url = f"/api/reviews/{review.review_id}/compare"
    if route == "compare_file":
        url += "/files/calc.py/contents"
    response = review.client.get(url, headers=HEADERS, params={"from_revision": 0, "to_revision": 1})
    assert response.status_code == 200, response.text
    assert dict(reads) == {(first, "new"): 1, (second, "new"): 1}


def _run_plan_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, Any]:
    """The review's own document reader, as the run-plan service holds it (it is built inside the API's closure)."""

    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    services: list[Any] = []
    original = RunPlanService.__init__

    def recording(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        services.append(self)

    monkeypatch.setattr(RunPlanService, "__init__", recording)
    review = _review(tmp_path)
    return review, services[-1]._read_head


def test_one_request_reading_two_revisions_keeps_their_documents_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memo is keyed by revision: a second Snapshot of the same path is its own document, not the first one's."""

    from lemoncrow.pro.capabilities.review import request_reads

    review, read_head = _run_plan_reader(tmp_path, monkeypatch)
    second = _new_snapshot(review, {"calc.py": "def add(a, b):\n    return a + b + 2\n"})
    first_revision = review.store.get_revision(review.revision_id)
    second_revision = review.store.get_revision(second)
    alone = (read_head(first_revision, "calc.py"), read_head(second_revision, "calc.py"))
    assert alone[0] != alone[1]
    with request_reads.reads_once():
        together = (
            read_head(first_revision, "calc.py"),
            read_head(second_revision, "calc.py"),
            read_head(first_revision, "calc.py"),
        )
    assert together == (*alone, alone[0])


def test_documents_past_the_cap_are_read_again_with_identical_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Holding documents is bounded: at the cap they are kept, past it they are read on demand and never dropped."""

    from lemoncrow.pro.capabilities.review import request_reads

    reads: Counter[str] = Counter()

    def read(name: str, text: str | None) -> Any:
        def reader() -> str | None:
            reads[name] += 1
            return text

        return reader

    monkeypatch.setattr(request_reads, "DOCUMENT_CHARS_CAP", 10)
    with request_reads.reads_once():
        first = [request_reads.document_once(("a",), read("a", "x" * 6)) for _ in range(2)]
        at_cap = [request_reads.document_once(("b",), read("b", "y" * 4)) for _ in range(2)]  # 6 + 4 == the cap
        past = [request_reads.document_once(("c",), read("c", "z")) for _ in range(2)]  # 11 > the cap
        missing = [request_reads.document_once(("d",), read("d", None)) for _ in range(2)]
    assert first == ["x" * 6] * 2 and at_cap == ["y" * 4] * 2 and past == ["z"] * 2 and missing == [None, None]
    assert reads == {"a": 1, "b": 1, "c": 2, "d": 1}


def test_a_route_returns_the_same_answer_when_nothing_may_be_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical cold Snapshots, one allowed to hold documents and one not: same answer, only more reads."""

    from lemoncrow.pro.capabilities.review import request_reads

    def cold(name: str) -> tuple[str, dict[str, int]]:
        counted = _counted(_review(tmp_path / name), _base_files(), _head_files())
        text = _fetch(counted, "projection").text
        text = text.replace(counted.review.review_id, "REVIEW").replace(counted.review.revision_id, "REVISION")
        text = re.sub(r"[0-9a-f]{64}", "DIGEST", text)  # plan digests cover the two Snapshots' own ids
        return text, {path: counted.content_reads[digest] for digest, path in counted.head_digests.items()}

    held, held_reads = cold("held")
    monkeypatch.setattr(request_reads, "DOCUMENT_CHARS_CAP", 0)
    unheld, unheld_reads = cold("unheld")
    assert unheld == held
    assert set(held_reads.values()) == {1} and max(unheld_reads.values()) > 1  # the cap is what limits the memo


def test_nothing_read_for_one_request_is_kept_for_the_next(tmp_path: Path) -> None:
    """The memo is request-scoped: a second request reads the manifest again, and none is left set between them."""

    from lemoncrow.pro.capabilities.review import request_reads

    counted = _counted(_review(tmp_path), _base_files(), _head_files())
    seen: list[bool] = []
    original = counted.review.store.read_source_tree_artifact

    def probing(*args: Any, **kwargs: Any) -> Any:
        seen.append(request_reads.active())  # in the handler's own thread, while the request runs
        return original(*args, **kwargs)

    counted.review.store.read_source_tree_artifact = probing
    _fetch(counted, "targets")
    first = dict(counted.manifest_reads)
    _fetch(counted, "targets")
    assert dict(counted.manifest_reads) == first
    assert seen and all(seen), "the handler's reads ran outside a request scope"
    assert not request_reads.active()


@pytest.mark.parametrize("fail", [False, True])
def test_the_scope_is_closed_when_the_request_ends(fail: bool) -> None:
    from lemoncrow.pro.capabilities.review import request_reads

    assert not request_reads.active()
    try:
        with request_reads.reads_once():
            assert request_reads.active()
            with request_reads.reads_once():  # nested: the outer scope stays open
                pass
            assert request_reads.active()
            if fail:
                raise RuntimeError("the request failed")
    except RuntimeError:
        pass
    assert not request_reads.active()
