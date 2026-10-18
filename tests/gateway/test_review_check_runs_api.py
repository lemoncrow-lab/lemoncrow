"""Check recipes run sandboxed, planned and differential through the Review API (Phase 9R R5; Phase 14 item 33)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from tests.gateway.test_review_hermetic import _Server, needs_bwrap

TOKEN = "check-runs-token"
PORT = 45997
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def npm_registry() -> Iterator[_Server]:
    server = _Server()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# --------------------------------------------------------------------------- #
# fixture repository (Phase 14 item 33): a stdlib test runner writing JUnit
# --------------------------------------------------------------------------- #

RUN_TESTS = """
import importlib.util, pathlib, sys, traceback
from xml.sax.saxutils import quoteattr

sys.path.insert(0, ".")
rows, failed = [], 0
for path in sorted(pathlib.Path("tests").glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in sorted(vars(module)):
        if not name.startswith("test_"):
            continue
        try:
            getattr(module, name)()
            rows.append(f"<testcase classname={quoteattr('tests.' + path.stem)} name={quoteattr(name)}/>")
        except Exception as exc:
            failed += 1
            rows.append(
                f"<testcase classname={quoteattr('tests.' + path.stem)} name={quoteattr(name)}>"
                f"<failure message={quoteattr(repr(exc))}/></testcase>"
            )
pathlib.Path(sys.argv[1]).write_text("<testsuite>" + "".join(rows) + "</testsuite>")
print(f"{len(rows)} tests, {failed} failed")
sys.exit(1 if failed else 0)
"""

FMT = """
import pathlib, sys
check = "--check" in sys.argv
bad = []
for path in sorted(pathlib.Path(".").rglob("*.py")):
    text = path.read_text()
    fixed = "\\n".join(line.rstrip() for line in text.splitlines()) + ("\\n" if text else "")
    if fixed != text:
        bad.append(str(path))
        if not check:
            path.write_text(fixed)
if check and bad:
    print("would reformat", *bad)
    sys.exit(1)
"""

CONFIG = """\
check_recipes:
  - id: unit
    kind: test
    command: python3 run_tests.py {report}
    report: junit
    inputs: [run_tests.py]
  - id: fmt
    kind: format
    command: [python3, fmt.py, --check]
    fix_command: [python3, fmt.py]
    inputs: [fmt.py]
check_policy:
  reruns: 1
  quarantine:
    - {test: "*::test_net_fetch", reason: needs network}
"""

CALC_BASE = (
    "def add(a, b):\n    return a + b\n\n\n"
    "def sub(a, b):\n    return a + b\n\n\n"
    "def mul(a, b):\n    return a * b\n\n\n"
    "def div(a, b):\n    return a / b\n"
)
CALC_HEAD = CALC_BASE.replace("    return a + b\n\n\ndef mul", "    return a - b\n\n\ndef mul").replace(
    "return a * b", "return a * b + 1"
)
TESTS_BASE = (
    "from calc import add, div, mul, sub\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_sub():\n    assert sub(3, 1) == 2\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n\n\n"
    "def test_div():\n    assert div(7, 2) == 3\n\n\n"
    "def test_net_fetch():\n    raise AssertionError('no network in the sandbox')\n\n\n"
    "def test_legacy():\n    assert True\n"
)
TESTS_HEAD = TESTS_BASE.replace(
    "    assert sub(3, 1) == 2\n", "    assert sub(3, 1) == 2\n    assert sub(1, 3) == -2\n"
).replace(
    "def test_legacy():\n    assert True\n",
    "def test_neg():\n    assert sub(0, 5) == -5\n\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n",
)


def _write(root: Path, rel: str, text: str | None) -> None:
    target = root / rel
    if text is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _base_files(config: str = CONFIG) -> dict[str, str]:
    return {
        "calc.py": CALC_BASE,
        "tests/test_calc.py": TESTS_BASE,
        "run_tests.py": RUN_TESTS,
        "fmt.py": FMT,
        ".lemoncrow/review.yaml": config,
    }


def _head_files() -> dict[str, str | None]:
    # Head fixes sub, breaks mul, edits/adds tests, removes test_legacy, and leaves trailing whitespace.
    return {"calc.py": CALC_HEAD.replace("def div(a, b):\n", "def div(a, b):   \n"), "tests/test_calc.py": TESTS_HEAD}


def _review(
    tmp_path: Path,
    *,
    base: dict[str, str] | None = None,
    head: dict[str, str | None] | None = None,
    shared_store: tuple[ReviewStore, Path] | None = None,
    **register: Any,
) -> SimpleNamespace:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    for rel, text in (base if base is not None else _base_files()).items():
        _write(repo_root, rel, text)
    base_sha = _commit(repo, "base", 0)
    for rel, change in (head if head is not None else _head_files()).items():
        _write(repo_root, rel, change)
    _commit(repo, "change", 1)
    if shared_store is None:
        store_root = tmp_path / "store"
        store = ReviewStore(store_root)
    else:
        store, store_root = shared_store
    rng = resolve_rev_range(repo_root, base=base_sha, head="HEAD")
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    register_review_api(
        app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=repo_root, port=PORT, **register
    )
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
    return SimpleNamespace(
        client=client,
        store=store,
        review_id=session.id,
        revision_id=revision.id,
        repo_root=repo_root,
        repo=repo,
        base_sha=base_sha,
        store_root=store_root,
    )


def _new_snapshot(review: SimpleNamespace, files: dict[str, str | None], offset: int = 2) -> str:
    for rel, text in files.items():
        _write(review.repo_root, rel, text)
    _commit(review.repo, "follow-up", offset)
    rng = resolve_rev_range(review.repo_root, base=review.base_sha, head="HEAD")
    build = build_review_packet_with_blobs(review.repo_root, rng, store_root=review.store_root, with_patch_text=True)
    session = review.store.get_session(review.review_id)
    revision = snapshot_revision(
        review.store, session, review.repo_root, rng, store_root=review.store_root, build=build
    )
    review.store.publish_snapshot(session.id, revision.id)
    return str(revision.id)


def _get(review: SimpleNamespace, revision_id: str = "") -> dict[str, Any]:
    response = review.client.get(
        f"/api/reviews/{review.review_id}/check-recipes",
        headers=HEADERS,
        params={"revision_id": revision_id} if revision_id else {},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _run(review: SimpleNamespace, entry: str, *, revision_id: str = "", pick: bool = False) -> Any:
    return review.client.post(
        f"/api/reviews/{review.review_id}/check-recipes/run",
        headers=HEADERS,
        json={"revision_id": revision_id or review.revision_id, "entry": entry, "pick": pick},
    )


def _result(payload: dict[str, Any], recipe_id: str) -> dict[str, Any]:
    return next(row for row in payload["results"] if row["recipe"]["id"] == recipe_id)


def _classes(result: dict[str, Any]) -> dict[str, str]:
    return {row["subject"].rpartition("::")[2]: row["class"] for row in result["tests"]}


def _evidence(review: SimpleNamespace, evidence_id: str) -> Any:
    item = review.store.get_evidence(evidence_id)
    assert item is not None
    return item


def _record(review: SimpleNamespace, evidence_id: str) -> dict[str, Any]:
    payload = review.store.read_evidence_artifact(_evidence(review, evidence_id))
    assert payload is not None
    return dict(json.loads(payload))


def _compute_runs(review: SimpleNamespace, stage: str = "run") -> dict[str, int]:
    """Recorded run-stage executions per (entry, executor); setup time is a separate stage row."""

    response = review.client.get(f"/api/reviews/{review.review_id}/run-plan", headers=HEADERS)
    assert response.status_code == 200, response.text
    out: dict[str, int] = {}
    for row in response.json()["compute"]["rows"]:
        if row["stage"] == stage:
            key = f"{row['entry_id']}@{row['executor']}"
            out[key] = out.get(key, 0) + int(row["runs"])
    return out


@needs_bwrap
def test_sandboxed_differential_classifies_flags_reconciles_and_reproduces(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.check_ingest import EquivalenceKey, equivalence_key_of
    from lemoncrow.pro.capabilities.review.execution import ExecutorClass, executor_satisfies
    from lemoncrow.pro.capabilities.review.hermetic import REPRODUCE_COMMAND
    from lemoncrow.pro.capabilities.review.reproduce import load_execution_record, reproduce_execution
    from lemoncrow.pro.capabilities.review.session_models import CheckAttestation

    review = _review(tmp_path)
    # What `lc review --check --run` posts: a caller-attested result, executed on the author's machine.
    claimed = review.client.post(
        f"/api/reviews/{review.review_id}/verification",
        headers=HEADERS,
        json={
            "revision_id": review.revision_id,
            "name": "unit tests",
            "status": "PASS",
            "source": "test",
            "source_ref": "python3 run_tests.py",
        },
    )
    assert claimed.status_code == 201, claimed.text
    attested = _evidence(review, claimed.json()["evidence"]["id"])
    assert (attested.executor_class, attested.attestation_class) == ("author_machine", "")
    # It is no typed attestation at all, and its class could never meet a trusted reservation.
    assert CheckAttestation.from_evidence(attested) is None
    assert not executor_satisfies(attested.executor_class, ("customer_worker", "managed_sandbox"))

    response = _run(review, "checks.test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdicts"] == {"checks.recipe:unit": "FAIL", "checks.test": "FAIL"}
    assert body["executor_class"] == "author_machine" and body["run_on"] == "local"

    payload = _get(review)
    unit = _result(payload, "unit")
    assert unit["granularity"] == "test" and unit["comparable"] is True
    assert unit["head_verdict"] == "FAIL" and unit["base_verdict"] == "FAIL"
    assert _classes(unit) == {
        "test_add": "unchanged",
        "test_sub": "fixed",
        "test_mul": "introduced_failure",
        "test_div": "pre_existing_failure",
        "test_net_fetch": "quarantined",
        "test_legacy": "removed",
        "test_neg": "new",
        "test_add_zero": "new",
    }
    assert {row["subject"].rpartition("::")[2] for row in unit["attention"]} == {
        "test_sub",
        "test_mul",
        "test_legacy",
        "test_neg",
        "test_add_zero",
    }
    # Failures were rerun once on each side (trusted-base reruns: 1); the adequacy probe ran once.
    runs = [(row["side"], row["variant"]) for row in unit["attempts"]]
    assert runs.count(("old", "run")) == 2 and runs.count(("new", "run")) == 2 and runs.count(("old", "probe")) == 1
    adequacy = {row["test"].rpartition("::")[2]: row["verdict"] for row in unit["adequacy"] if row["test"]}
    assert adequacy["test_sub"] == "demonstrates" and adequacy["test_neg"] == "demonstrates"
    assert adequacy["test_add_zero"] == "not_demonstrating"
    assert "test_add" not in adequacy  # untouched by the Change
    # The agent's claimed PASS is contradicted by the sandboxed head run.
    claim = next(row for row in payload["claims"] if row["claim"]["command"] == "python3 run_tests.py")
    assert (claim["recipe_id"], claim["verdict"], claim["sandbox"]) == ("unit", "contradicted", "FAIL")
    assert unit["claims"][0]["verdict"] == "contradicted"

    # Typed lemoncrow_verification results, stamped for CI equivalence; attempts are observations only.
    head_sha = str(review.repo.head.target)
    verdicts = {row["stable_key"]: row for row in payload["verdicts"]}
    assert verdicts["checks.test"]["status"] == "FAIL" and verdicts["checks.test"]["executor_class"] == "author_machine"
    kind_row = _evidence(review, verdicts["checks.test"]["evidence_id"])
    assert kind_row.attestation_class == "lemoncrow_verification" and kind_row.provider_revision == head_sha
    expected = EquivalenceKey(
        tool="::checks.test",
        recipe="checks.test",
        head_sha=head_sha,
        recipe_digest=payload["entry_digests"]["checks.test"],
    )
    assert kind_row.attestation_scope == expected.canonical()
    # The stamp is exactly the key the federation matcher derives from the row (same tool identity).
    assert equivalence_key_of(kind_row) == expected
    recipe_row = _evidence(review, verdicts["checks.recipe:unit"]["evidence_id"])
    recipe_digest = next(row["digest"] for row in payload["recipes"] if row["id"] == "unit")
    assert recipe_row.attestation_scope.endswith("recipe_digest=" + recipe_digest.replace(":", "%3A"))
    verdict_record = _record(review, recipe_row.id)
    assert verdict_record["data"]["execution"]["executor_class"] == "author_machine"
    assert verdict_record["data"]["execution"]["environment_fingerprint"] == unit["environment"]["head"] != ""
    attempt_rows = [_evidence(review, row["evidence_id"]) for row in unit["attempts"]]
    assert all(row.verification_status == "" for row in attempt_rows)
    # A local sandboxed result is author_machine too: it can never satisfy a trusted reservation.
    projected = CheckAttestation.from_evidence(kind_row)
    assert projected is not None and not executor_satisfies(projected.executor_class, ("customer_worker",))
    assert ExecutorClass(projected.executor_class) is ExecutorClass.AUTHOR_MACHINE

    # Only deltas demand attention: they become test_outcome behavior targets.
    behavior = review.client.get(f"/api/reviews/{review.review_id}/behavior", headers=HEADERS)
    assert behavior.status_code == 200, behavior.text
    targets = behavior.json()["behavior"]["targets"]
    titles = {row["title"] for row in targets if row["producer"] == "lemoncrow.check_recipes"}
    assert {
        "Introduced failure · tests.test_calc::test_mul",
        "Fixed · tests.test_calc::test_sub",
        "Test removed · tests.test_calc::test_legacy",
        "New test · tests.test_calc::test_neg",
        "Test does not demonstrate the change · tests.test_calc::test_add_zero",
    } <= titles
    assert any(title.startswith("Claim contradicted") for title in titles)
    # Pre-existing and quarantined failures are counted, not raised.
    assert not any("test_div" in title or "test_net_fetch" in title for title in titles)
    assert all(row["kind"] == "test_outcome" for row in targets if row["producer"] == "lemoncrow.check_recipes")

    # Compute is recorded per planned entry: 2 base + 2 head + 1 probe executions.
    assert _compute_runs(review) == {"checks.test@local": 5}

    # Every result reproduces locally with one command (R4 primitive): a head run and the overlay probe.
    head_attempt = next(row for row in unit["attempts"] if row["side"] == "new" and row["variant"] == "run")
    probe_attempt = next(row for row in unit["attempts"] if row["variant"] == "probe")
    for attempt in (head_attempt, probe_attempt):
        record = _record(review, attempt["evidence_id"])
        assert record["data"]["execution"]["reproduce"] == REPRODUCE_COMMAND
        outcome = reproduce_execution(
            load_execution_record(record), repo=review.repo_root, store_root=tmp_path / "reproduce"
        )
        assert outcome.reproduced, outcome.to_wire()
    probe_subject = load_execution_record(_record(review, probe_attempt["evidence_id"])).subject
    assert probe_subject.overlay_source == head_sha and [path for path, _digest in probe_subject.overlay] == [
        "tests/test_calc.py"
    ]

    # A second request reuses the exact-Snapshot attempts instead of executing again.
    again = _run(review, "checks.test")
    assert again.status_code == 200 and again.json()["executions"] == 0 and again.json()["reused"] >= 5


@needs_bwrap
def test_formatter_autofix_is_a_proposal_then_a_new_snapshot_never_a_source_mutation(tmp_path: Path) -> None:
    review = _review(tmp_path)
    calc = review.repo_root / "calc.py"
    before = calc.read_text(encoding="utf-8")
    assert "def div(a, b):   \n" in before

    response = _run(review, "checks.format")
    assert response.status_code == 200, response.text
    assert response.json()["verdicts"]["checks.format"] == "FAIL"
    payload = _get(review)
    fmt = _result(payload, "fmt")
    assert fmt["head_verdict"] == "FAIL" and fmt["sides"] == ["new"]
    assert fmt["autofix"]["status"] == "proposed" and len(fmt["autofix"]["proposal_ids"]) == 1
    proposal = review.store.get_change_proposal(fmt["autofix"]["proposal_ids"][0])
    assert proposal is not None and proposal.path == "calc.py" and proposal.intent == "autofix:fmt"
    assert proposal.created_by == "lemoncrow:recipe:fmt" and proposal.base_revision_id == review.revision_id
    assert "def div(a, b):\n" in proposal.replacement_text and "-def div(a, b):   " in proposal.patch_text
    assert [row["id"] for row in payload["proposals"]] == [proposal.id]
    # LemonCrow never touched the source: the checkout and the Snapshot keep the original text.
    assert calc.read_text(encoding="utf-8") == before
    assert review.store.latest_revision(review.review_id).id == review.revision_id
    fix_attempt = next(row for row in fmt["attempts"] if row["variant"] == "fix")
    assert fix_attempt["changed_paths"] == ["calc.py"]

    # The proposal is routed to the authoring agent as reviewer feedback through the normal handoff.
    routed = review.client.post(
        f"/api/reviews/{review.review_id}/check-recipes/requests",
        headers=HEADERS,
        json={"kind": "autofix", "proposal_id": proposal.id, "expected_revision_id": review.revision_id},
    )
    assert routed.status_code == 201, routed.text
    annotation = routed.json()["annotation"]
    assert annotation["source"] == "human" and annotation["path"] == "calc.py"
    assert annotation["title"] == "Autofix proposed · fmt" and f"proposal:{proposal.id}" in json.dumps(annotation)

    # The agent applies it: a new Snapshot, on which the planned check runs fresh and passes.
    second = _new_snapshot(review, {"calc.py": proposal.replacement_text})
    rerun = _run(review, "checks.format", revision_id=second)
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["verdicts"]["checks.format"] == "PASS" and rerun.json()["executions"] == 1
    assert _result(_get(review, second), "fmt")["autofix"] == {}
    # The first Snapshot's results stay bound to it and are historical now.
    first = _get(review, review.revision_id)
    assert first["historical"] is True and _result(first, "fmt")["head_verdict"] == "FAIL"


@needs_bwrap
def test_head_side_recipe_edits_are_never_executed(tmp_path: Path) -> None:
    config = CONFIG + "  - id: build\n    kind: build\n    command: make build\n"
    config = config.replace("check_policy:\n", "").replace(
        '  reruns: 1\n  quarantine:\n    - {test: "*::test_net_fetch", reason: needs network}\n', ""
    )
    config += "check_policy:\n  reruns: 0\n"
    base = {
        **_base_files(config),
        "Makefile": "build:\n\tpython3 -c \"print('base build')\"\n",
        "tests/test_calc.py": "def test_ok():\n    assert True\n",
    }
    marker = "pwned-by-head"
    head_config = config.replace(
        "command: python3 run_tests.py {report}", f"command: python3 -c \"open('{marker}','w')\" {{report}}"
    )
    head: dict[str, str | None] = {
        ".lemoncrow/review.yaml": head_config,
        "Makefile": f"build:\n\tpython3 -c \"print('{marker}')\"\n",
        "calc.py": CALC_BASE + "\n",
    }
    review = _review(tmp_path, base=base, head=head)
    payload = _get(review)
    # The head's edits are proposed future policy, shown for later Reviews.
    assert payload["future_policy"]["changed"] is True
    assert [row["id"] for row in payload["future_policy"]["changed_recipes"]] == ["unit"]
    unit_recipe = next(row for row in payload["recipes"] if row["id"] == "unit")
    assert "run_tests.py" in " ".join(unit_recipe["command"])

    test_run = _run(review, "checks.recipe:unit")
    assert test_run.status_code == 200, test_run.text
    unit = _result(_get(review), "unit")
    head_output = _record(review, next(r for r in unit["attempts"] if r["side"] == "new")["evidence_id"])
    assert marker not in json.dumps(head_output) and unit["head_verdict"] == "PASS"
    # The head side ran the trusted base's command, not the head's edited one.
    assert head_output["data"]["execution"]["recipe"]["command"][:2] == ["python3", "run_tests.py"]
    # The Makefile defines `make build`; the head edited it, so the head side is not executed.
    build_run = _run(review, "checks.recipe:build", pick=True)
    assert build_run.status_code == 200, build_run.text
    build = _result(_get(review), "build")
    assert build["head_verdict"] == "NOT_RUN" and "edits Makefile" in build["head_guard"]
    assert [row["status"] for row in build["attempts"] if row["side"] == "new"] == ["not_run"]
    assert build_run.json()["verdicts"]["checks.recipe:build"] == "NOT_RUN"


@needs_bwrap
def test_a_proof_request_is_handoff_feedback_and_the_next_snapshot_runs_fresh(tmp_path: Path) -> None:
    review = _review(tmp_path)
    assert _run(review, "checks.recipe:unit").status_code == 200
    request = review.client.post(
        f"/api/reviews/{review.review_id}/check-recipes/requests",
        headers=HEADERS,
        json={
            "kind": "proof",
            "recipe_id": "unit",
            "subject": "tests.test_calc::test_mul",
            "path": "calc.py",
            "expected_revision_id": review.revision_id,
        },
    )
    assert request.status_code == 201, request.text
    annotation = request.json()["annotation"]
    assert annotation["source"] == "human" and annotation["kind"] == "comment"
    assert annotation["title"] == "Proof requested · tests.test_calc::test_mul"
    assert "fails on the base and passes on your change" in annotation["body"]
    # It rides the existing feedback handoff to the authoring agent.
    handoff = review.client.post(f"/api/reviews/{review.review_id}/feedback/export", headers=HEADERS)
    assert handoff.status_code == 200, handoff.text
    assert annotation["id"] in handoff.json()["annotation_versions"] and handoff.json()["open"] == 1
    assert "Proof requested" in handoff.json()["markdown"] or "fails on the base" in handoff.json()["markdown"]
    stale = review.client.post(
        f"/api/reviews/{review.review_id}/check-recipes/requests",
        headers=HEADERS,
        json={"kind": "proof", "recipe_id": "unit", "expected_revision_id": "rr-elsewhere"},
    )
    assert stale.status_code == 409

    second = _new_snapshot(
        review, {"calc.py": CALC_BASE.replace("return a + b\n\n\ndef mul", "return a - b\n\n\ndef mul")}
    )
    rerun = _run(review, "checks.recipe:unit", revision_id=second)
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["executions"] >= 1 and rerun.json()["reused"] == 0
    assert _result(_get(review, second), "unit")["head_verdict"] == "FAIL"  # test_div and test_net remain


# --------------------------------------------------------------------------- #
# planned execution, reruns and Snapshot races with a scripted runner
# --------------------------------------------------------------------------- #


def _junit(cases: dict[str, str]) -> str:
    rows = "".join(
        f'<testcase classname="tests.test_calc" name="{name}">'
        + ("<failure message='x'/>" if status == "failed" else "")
        + "</testcase>"
        for name, status in cases.items()
    )
    return f"<testsuite>{rows}</testsuite>"


class _Scripted:
    """A ``command`` runner returning scripted per-test outcomes with a typed execution."""

    runner_id = "command"

    def __init__(self, script: Any) -> None:
        self.script = script
        self.calls: list[tuple[str, str, int]] = []

    def setup_candidates(self, _repo_root: Path) -> tuple[()]:
        return ()

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> Any:
        from lemoncrow.pro.capabilities.review.execution import ExecutionSubject, StageOutcome
        from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
        from tests.gateway.test_review_execution_contract import _result

        count = 1 + sum(1 for call in self.calls if call[:2] == (runtime.id, side))
        self.calls.append((runtime.id, side, count))
        cases, duration_ms = self.script(runtime.id, side, count)
        failed = "failed" in cases.values()
        execution = _result(
            subject=ExecutionSubject(
                review_id=context.revision.review_id, revision_id=context.revision.id, side=side, snapshot_identity="t"
            ),
            stages=(StageOutcome("run", "failed" if failed else "passed", exit_code=int(failed), duration_ms=1000),),
        ).to_wire()
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            "failed" if failed else "passed",
            "scripted",
            self.runner_id,
            runtime.id,
            exit_code=int(failed),
            duration_ms=duration_ms,
            data={"execution": execution, "report": _junit(cases), "report_truncated": False},
        )


def _scripted_review(
    tmp_path: Path,
    script: Any,
    *,
    config: str = CONFIG,
    shared_store: tuple[ReviewStore, Path] | None = None,
) -> tuple[SimpleNamespace, _Scripted]:
    from lemoncrow.pro.capabilities.review.runtimes import RunnerRegistry

    runner = _Scripted(script)
    review = _review(
        tmp_path,
        base=_base_files(config),
        shared_store=shared_store,
        runner_registry=RunnerRegistry((runner,)),
    )
    return review, runner


def _all_pass(_runtime_id: str, _side: str, _count: int) -> tuple[dict[str, str], int]:
    return {"test_add": "passed", "test_sub": "passed"}, 100


def test_recipes_run_only_as_planned_and_compute_is_recorded(tmp_path: Path) -> None:
    store_root = tmp_path / "shared-store"
    shared_store = (ReviewStore(store_root), store_root)
    review, runner = _scripted_review(tmp_path / "auto", _all_pass, shared_store=shared_store)
    assert _run(review, "checks.unknown").status_code == 422
    inapplicable = _run(review, "checks.lint")
    assert inapplicable.status_code == 409 and "no linter configured" in inapplicable.json()["detail"]
    unplanned = _run(review, "checks.precommit")
    assert unplanned.status_code == 409 and "not in the effective run plan" in unplanned.json()["detail"]
    # Auto skips the base-vs-head diff without a risk signal: refused until a reviewer picks it.
    skipped = _run(review, "behavior.test_diff")
    assert skipped.status_code == 409 and "Auto escalates only on a risk signal" in skipped.json()["detail"]
    assert runner.calls == []
    picked = _run(review, "behavior.test_diff", pick=True)
    assert picked.status_code == 200, picked.text
    assert picked.json()["plan_entry"]["status"] == "run" and picked.json()["plan_entry"]["origin"] == "manual"
    assert picked.json()["verdicts"] == {"checks.recipe:unit": "PASS", "checks.test": "PASS"}
    sides = {(runtime_id, side) for runtime_id, side, _count in runner.calls}
    assert {("recipe:unit", "old"), ("recipe:unit", "new"), ("recipe:unit:probe", "old")} == sides
    assert _compute_runs(review) == {"behavior.test_diff@local": 3}
    entry = next(row for row in _get(review)["entries"] if row["id"] == "behavior.test_diff")
    assert entry["status"] == "run"

    off, off_runner = _scripted_review(
        tmp_path / "off",
        _all_pass,
        config=CONFIG + "review:\n  capabilities:\n    checks: off\n",
        shared_store=shared_store,
    )
    refused = _run(off, "checks.test")
    assert refused.status_code == 409 and "family turned off" in refused.json()["detail"]
    assert off_runner.calls == [] and _compute_runs(off) == {}

    limited, limited_runner = _scripted_review(
        tmp_path / "limited",
        _all_pass,
        config=CONFIG + "review:\n  budget:\n    per_review_minutes: 1\n",
        shared_store=shared_store,
    )
    capped = _run(limited, "checks.test")
    assert capped.status_code == 409 and "Limited by budget" in capped.json()["detail"]
    assert limited_runner.calls == [] and _compute_runs(limited) == {}


def test_flaky_failures_are_rerun_reported_flaky_and_never_pass(tmp_path: Path) -> None:
    def flaky(runtime_id: str, side: str, count: int) -> tuple[dict[str, str], int]:
        if runtime_id == "recipe:unit" and side == "new" and count == 1:
            return {"test_add": "failed", "test_sub": "passed"}, 100
        return {"test_add": "passed", "test_sub": "passed"}, 100

    review, runner = _scripted_review(tmp_path, flaky)
    response = _run(review, "checks.test")
    assert response.status_code == 200, response.text
    assert response.json()["verdicts"] == {"checks.recipe:unit": "UNKNOWN", "checks.test": "UNKNOWN"}
    unit = _result(_get(review), "unit")
    flaky_unit = next(row for row in unit["tests"] if row["subject"].endswith("::test_add"))
    assert flaky_unit["class"] == "flaky" and flaky_unit["head_attempts"] == ["failed", "passed"]
    assert unit["head_verdict"] == "UNKNOWN" and unit["reasons"][0].startswith("flaky")
    # Head first (R6): Auto plans the base on demand, and a head that passed on its
    # rerun is flaky, which no base run can reclassify, so the base never runs.
    assert unit["reruns"]["executed"] == {"new": 1}
    assert unit["economy"]["base"] == {
        "mode": "on_demand",
        "ran": False,
        "reason": "head passed on a rerun (flaky): a base run cannot classify a flaky head",
    }
    assert [call for call in runner.calls if call[0] == "recipe:unit"] == [
        ("recipe:unit", "new", 1),
        ("recipe:unit", "new", 2),
    ]


def test_reruns_stop_at_the_plan_budget(tmp_path: Path) -> None:
    def failing(_runtime_id: str, _side: str, _count: int) -> tuple[dict[str, str], int]:
        return {"test_add": "failed"}, 5000

    probe, _runner = _scripted_review(tmp_path / "probe", failing)
    planned = probe.client.get(f"/api/reviews/{probe.review_id}/run-plan", headers=HEADERS).json()["plan"]
    minutes = float(planned["budget"]["planned"]["minutes"])
    # A ceiling just above the plan: the entries run, but about 0.6 s remain for reruns.
    config = CONFIG + f"review:\n  budget:\n    per_review_minutes: {minutes + 0.01}\n"
    review, runner = _scripted_review(tmp_path / "tight", failing, config=config)
    response = _run(review, "checks.recipe:unit")
    assert response.status_code == 200, response.text
    unit = _result(_get(review), "unit")
    assert unit["reruns"]["limited_by_budget"] is True and unit["reruns"]["executed"] == {"new": 0}
    assert unit["head_verdict"] == "FAIL" and runner.calls == [("recipe:unit", "new", 1)]


def test_a_stale_snapshot_run_cannot_publish_current_results(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}

    def advancing(runtime_id: str, side: str, count: int) -> tuple[dict[str, str], int]:
        if side == "new" and "advanced" not in holder:
            holder["advanced"] = _new_snapshot(holder["review"], {"calc.py": CALC_BASE + "# follow-up\n"})
        return {"test_add": "passed"}, 100

    review, _runner = _scripted_review(tmp_path, advancing)
    holder["review"] = review
    response = _run(review, "checks.test")
    assert response.status_code == 409, response.text
    assert "results were not published as current" in response.json()["detail"]
    first = _get(review, review.revision_id)
    assert first["historical"] is True and first["verdicts"] == [] and first["results"] == []
    current = _get(review)
    assert current["revision_id"] == holder["advanced"] and current["verdicts"] == []
    statuses = [
        item.verification_status
        for item in review.store.list_evidence(review.review_id)
        if item.attestation_class == "lemoncrow_verification"
    ]
    assert all(status == "" for status in statuses)


# --------------------------------------------------------------------------- #
# CI equivalence (P11-FED): a verdict stands in for a rerun on the exact head and recipe digest
# --------------------------------------------------------------------------- #

_LEMONCROW_EQUIVALENT = (
    '  - {recipe: "checks.recipe:unit", check: "checks.recipe:unit", attestation_class: lemoncrow_verification}\n'
)
_PROVIDER_EQUIVALENT = '  - {recipe: "checks.recipe:unit", check: unit-tests, provider: github}\n'


def _plan_entry(review: SimpleNamespace, entry_id: str, revision_id: str = "") -> dict[str, Any]:
    response = review.client.get(
        f"/api/reviews/{review.review_id}/run-plan",
        headers=HEADERS,
        params={"revision_id": revision_id} if revision_id else {},
    )
    assert response.status_code == 200, response.text
    return dict(next(row for row in response.json()["plan"]["entries"] if row["id"] == entry_id))


@needs_bwrap
def test_a_sandboxed_verdict_satisfies_its_entry_only_on_the_exact_head_and_recipe_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from lemoncrow.pro.capabilities.review.check_ingest import ingested_satisfactions, parse_ci_equivalents
    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    config = CONFIG + "ci_equivalents:\n" + _LEMONCROW_EQUIVALENT
    review = _review(tmp_path, base=_base_files(config))
    assert _plan_entry(review, "checks.recipe:unit")["status"] == "run"
    first = _run(review, "checks.recipe:unit")
    assert first.status_code == 200, first.text
    verdict_id = first.json()["verdict_evidence"]["checks.recipe:unit"]
    executed = _compute_runs(review)
    assert executed and first.json()["executions"] >= 1
    head_sha = str(review.repo.head.target)
    digest = _get(review)["entry_digests"]["checks.recipe:unit"]

    # Head H + recipe digest D: the entry is satisfied by LemonCrow's own sandboxed verdict.
    entry = _plan_entry(review, "checks.recipe:unit")
    assert entry["status"] == "satisfied"
    satisfied = entry["satisfied_by"]
    assert (satisfied["evidence_id"], satisfied["attestation_class"], satisfied["head_sha"]) == (
        verdict_id,
        "lemoncrow_verification",
        head_sha,
    )
    assert f";head={head_sha};" in satisfied["equivalence"]
    assert satisfied["equivalence"].endswith("recipe_digest=" + digest.replace(":", "%3A"))
    assert entry["reason"].startswith("satisfied by LemonCrow verification") and entry["reason"].endswith("not re-run")
    assert next(row for row in _get(review)["entries"] if row["id"] == "checks.recipe:unit")["status"] == "satisfied"
    # Nothing re-executes: the plan refuses the run and no compute is added.
    again = _run(review, "checks.recipe:unit")
    assert again.status_code == 409 and "not re-run" in again.json()["detail"]
    assert _compute_runs(review) == executed

    # The trusted base edits the recipe (digest D'): the D verdict no longer satisfies.
    original = RunPlanService.recipe_catalog

    def edited(self: RunPlanService, revision: Any) -> Any:
        catalog = original(self, revision)
        recipes = tuple(
            replace(recipe, command=(*recipe.command, "--edited")) if recipe.id == "unit" else recipe
            for recipe in catalog.recipes
        )
        return replace(catalog, recipes=recipes)

    monkeypatch.setattr(RunPlanService, "recipe_catalog", edited)
    changed = _plan_entry(review, "checks.recipe:unit")
    assert changed["status"] == "run" and changed["satisfied_by"] is None
    monkeypatch.setattr(RunPlanService, "recipe_catalog", original)
    assert _plan_entry(review, "checks.recipe:unit")["satisfied_by"]["evidence_id"] == verdict_id

    # A different head never satisfies: the pushed Snapshot plans the run again.
    second = _new_snapshot(review, {"calc.py": CALC_HEAD + "\n# follow-up\n"})
    pushed = _plan_entry(review, "checks.recipe:unit", second)
    assert pushed["status"] == "run" and pushed["satisfied_by"] is None
    # The same rows against another head or another digest never match either.
    rows = review.store.list_evidence(review.review_id)
    old = review.store.get_revision(review.revision_id)
    assert old is not None
    equivalents, errors = parse_ci_equivalents(config)
    assert errors == ()
    pinned = {"checks.recipe:unit": digest}
    assert [row.entry_id for row in ingested_satisfactions(rows, old, equivalents, recipe_digests=pinned)] == [
        "checks.recipe:unit"
    ]
    assert ingested_satisfactions(rows, replace(old, head_sha="c" * 40), equivalents, recipe_digests=pinned) == ()
    moved = {"checks.recipe:unit": "recipe:sha256:" + "0" * 64}
    assert ingested_satisfactions(rows, old, equivalents, recipe_digests=moved) == ()


def test_provider_ci_and_lemoncrow_verdicts_for_one_recipe_resolve_by_trusted_base_order(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.check_ingest import CheckIngest, IngestAuthority, record_check_ingest

    github = IngestAuthority(attestation_class="provider_ci", source="ci", provider="github", integration_id="actions")
    winners: dict[str, tuple[str, str]] = {}
    for name, rows in (
        ("provider_first", _PROVIDER_EQUIVALENT + _LEMONCROW_EQUIVALENT),
        ("lemoncrow_first", _LEMONCROW_EQUIVALENT + _PROVIDER_EQUIVALENT),
    ):
        review, runner = _scripted_review(tmp_path / name, _all_pass, config=CONFIG + "ci_equivalents:\n" + rows)
        ran = _run(review, "checks.recipe:unit")
        assert ran.status_code == 200, ran.text
        verdict_id = ran.json()["verdict_evidence"]["checks.recipe:unit"]
        assert _plan_entry(review, "checks.recipe:unit")["satisfied_by"]["evidence_id"] == verdict_id
        calls = list(runner.calls)
        revision = review.store.latest_revision(review.review_id)
        provider, _created = record_check_ingest(
            review.store,
            revision,
            CheckIngest(
                tool="pytest",
                category="tests",
                status="PASS",
                head_sha=revision.head_sha,
                title="CI / unit tests",
                stable_key="unit-tests",
                remote_identity="run-1",
            ),
            github,
        )
        entry = _plan_entry(review, "checks.recipe:unit")
        assert entry["status"] == "satisfied"
        winner = (str(entry["satisfied_by"]["attestation_class"]), str(entry["satisfied_by"]["evidence_id"]))
        winners[name] = winner
        assert winner[1] == (provider.id if winner[0] == "provider_ci" else verdict_id)
        # Recomputing never flips it, and neither result re-executes the recipe.
        assert _plan_entry(review, "checks.recipe:unit")["satisfied_by"] == entry["satisfied_by"]
        assert _run(review, "checks.recipe:unit").status_code == 409 and runner.calls == calls
    # The first trusted-base ``ci_equivalents`` row with a matching result wins.
    assert winners["provider_first"][0] == "provider_ci"
    assert winners["lemoncrow_first"][0] == "lemoncrow_verification"


# --------------------------------------------------------------------------- #
# a dependency-changing PR runs hermetically (Phase 14 item 33)
# --------------------------------------------------------------------------- #


@needs_bwrap
def test_a_dependency_changing_change_runs_hermetically_and_is_not_comparable(
    tmp_path: Path, npm_registry: _Server
) -> None:
    registry = npm_registry
    from tests.gateway.test_review_hermetic import _project, _tarball

    old = _tarball("left-pad", "1.0.0", files={"index.js": "module.exports = 1"})
    new = _tarball("left-pad", "1.1.0", files={"index.js": "module.exports = 2"})
    stage = tmp_path / "stage"
    _project(stage / "base", registry, [("left-pad", "1.0.0", old)])
    _project(stage / "head", registry, [("left-pad", "1.1.0", new)])
    execution = (
        "execution:\n  registries:\n"
        f"    - {{host: localhost, scheme: http, port: {registry.port}, private_network: true}}\n"
    )
    test_dep = (
        "import json\n\n\ndef test_left_pad_is_the_audited_version():\n"
        "    assert json.load(open('node_modules/left-pad/package.json'))['version'] == '1.0.0'\n\n\n"
        "def test_ok():\n    assert True\n"
    )
    base = {
        "run_tests.py": RUN_TESTS,
        "fmt.py": FMT,
        "calc.py": CALC_BASE,
        "tests/test_dep.py": test_dep,
        ".lemoncrow/review.yaml": CONFIG + execution,
        "package.json": (stage / "base" / "package.json").read_text(encoding="utf-8"),
        "package-lock.json": (stage / "base" / "package-lock.json").read_text(encoding="utf-8"),
    }
    head: dict[str, str | None] = {
        "package.json": (stage / "head" / "package.json").read_text(encoding="utf-8"),
        "package-lock.json": (stage / "head" / "package-lock.json").read_text(encoding="utf-8"),
    }
    review = _review(tmp_path, base=base, head=head)
    response = _run(review, "checks.test")
    assert response.status_code == 200, response.text
    unit = _result(_get(review), "unit")
    # Executed on both sides (never refused), each with its own hermetically fetched dependencies.
    assert {row["side"] for row in unit["attempts"] if row["status"] != "not_run"} == {"old", "new"}
    head_attempt = next(row for row in unit["attempts"] if row["side"] == "new" and row["variant"] == "run")
    stages = [
        (row["stage"], row["status"])
        for row in _record(review, head_attempt["evidence_id"])["data"]["execution"]["stages"]
    ]
    assert ("fetch", "passed") in stages and stages[-1] == ("run", "failed")
    assert any(request.endswith("left-pad-1.1.0.tgz") for request in registry.requests)
    # The lockfile changed the environment: nothing is called introduced across it.
    assert unit["comparable"] is False and "lockfile_digest" in unit["environment"]["differences"]
    assert _classes(unit) == {"test_left_pad_is_the_audited_version": "not_comparable", "test_ok": "unchanged"}
    assert unit["head_verdict"] == "FAIL"
