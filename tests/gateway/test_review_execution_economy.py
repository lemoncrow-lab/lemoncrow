"""Execution economy through the Review API (Phase 9R R6, items 19-23).

A scripted ``command`` runner stands in for the sandbox: it returns per-test
JUnit outcomes and a typed execution result whose subject, cache namespace and
environment fingerprint follow the real runners (``runtimes.py``), and it
records every command it was asked to run.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
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

TOKEN = "economy-token"
PORT = 45991
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

CONFIG = """\
check_recipes:
  - id: unit
    kind: test
    command: python3 run_tests.py {report} {tests}
    report: junit
    inputs: [run_tests.py]
check_policy:
  reruns: 0
"""

BASE_FILES = {
    "pkg/__init__.py": "",
    "pkg/alpha.py": "def alpha():\n    return 1\n",
    "pkg/beta.py": "def beta():\n    return 2\n",
    "tests/test_alpha.py": "from pkg.alpha import alpha\n\n\ndef test_alpha():\n    assert alpha() == 1\n",
    "tests/test_beta.py": "from pkg.beta import beta\n\n\ndef test_beta():\n    assert beta() == 2\n",
    "run_tests.py": "import sys\n",
    "README.md": "# fixture\n",
}
ALPHA_HEAD = {"pkg/alpha.py": "def alpha():\n    return 1  # head\n"}

Script = Callable[[str, str, int, tuple[str, ...]], dict[str, str]]


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _junit(cases: dict[str, str]) -> str:
    rows = "".join(
        f'<testcase classname="tests.{name.split("::")[0]}" name="{name.split("::")[-1]}">'
        + ("<failure message='x'/>" if status == "failed" else "")
        + "</testcase>"
        for name, status in cases.items()
    )
    return f"<testsuite>{rows}</testsuite>"


def _passing(_runtime: str, _side: str, _count: int, command: tuple[str, ...]) -> dict[str, str]:
    return {
        f"{Path(arg).stem}::test_{Path(arg).stem[5:]}": "passed" for arg in command if arg.startswith("tests/")
    } or {
        "test_alpha::test_alpha": "passed",
        "test_beta::test_beta": "passed",
    }


class _Runner:
    """Scripted ``command`` runner with real-shaped typed execution results."""

    runner_id = "command"

    def __init__(self, script: Script = _passing) -> None:
        self.script = script
        self.calls: list[tuple[str, str, str, tuple[str, ...]]] = []
        self.image = "sha256:image-one"
        #: Cache namespace per side; a misbehaving executor could claim anything.
        self.namespace = {"old": "base", "new": "head_untrusted"}
        #: Whether the executor environment is known before dispatch: ``known`` (local
        #: command runner), ``unknown`` (a hosted worker) or ``broken`` (the probe fails).
        self.environment_state = "known"

    def setup_candidates(self, _repo_root: Path) -> tuple[()]:
        return ()

    def environment(self, command: Sequence[str]) -> str:
        """The registry's pre-dispatch probe: the real command probe, on this runner's image."""

        from lemoncrow.pro.capabilities.review.runner_host import command_environment

        if self.environment_state == "broken":
            raise RuntimeError("probe failed")
        return command_environment(command, image_digest=self.image) if self.environment_state == "known" else ""

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> Any:
        from lemoncrow.pro.capabilities.review.runner_host import command_identity, revision_snapshot_identity
        from lemoncrow.pro.environment.execution import ExecutionRecipe, ExecutionSubject, StageOutcome
        from lemoncrow.pro.environment.runners import SurfaceRunResult
        from tests.gateway.test_review_execution_contract import _result

        command = tuple(str(part) for part in runtime.values["command"])
        count = 1 + sum(1 for call in self.calls if call[1:3] == (runtime.id, side))
        self.calls.append((context.revision.review_id, runtime.id, side, command))
        cases = self.script(runtime.id, side, count, command)
        failed = "failed" in cases.values()
        execution = _result(
            subject=ExecutionSubject(
                review_id=context.revision.review_id,
                revision_id=context.revision.id,
                side=side,
                snapshot_identity=revision_snapshot_identity(context.revision, side),
            ),
            recipe=ExecutionRecipe(command=command),
            stages=(StageOutcome("run", "failed" if failed else "passed", exit_code=int(failed), duration_ms=90_000),),
            namespace=self.namespace[side],
            # What this run used, through the same probe as the real command runner.
            environment=command_identity(command, image_digest=self.image),
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
            duration_ms=90_000,
            data={"execution": execution, "report": _junit(cases), "report_truncated": False},
        )

    def sides(self, review_id: str) -> list[str]:
        return [call[2] for call in self.calls if call[0] == review_id]


def _write(root: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        target = root / rel
        if text is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


class _World:
    """One repository and one Review store; Reviews are branches off one base commit."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        config: str = CONFIG,
        runner: _Runner | None = None,
        base: dict[str, str] | None = None,
    ) -> None:
        from lemoncrow.pro.environment.runners import RunnerRegistry

        self.root = tmp_path / "repo"
        self.root.mkdir(parents=True)
        self.repo = pygit2.init_repository(str(self.root), initial_head="main")
        _write(self.root, {**(BASE_FILES if base is None else base), ".lemoncrow/review.yaml": config})
        self.base_sha = _commit(self.repo, "base", 0)
        self.store_root = tmp_path / "store"
        self.store = ReviewStore(self.store_root)
        self.runner = runner or _Runner()
        app = FastAPI()
        register_review_api(
            app,
            self.store,
            auth_dependency=make_token_dependency(TOKEN),
            repo_root=self.root,
            port=PORT,
            runner_registry=RunnerRegistry((self.runner,), environment_probe=self.runner.environment),
        )
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
        self._offset = 1

    def _snapshot(self, session: Any) -> str:
        rng = resolve_rev_range(self.root, base=self.base_sha, head="HEAD")
        build = build_review_packet_with_blobs(self.root, rng, store_root=self.store_root, with_patch_text=True)
        revision = snapshot_revision(self.store, session, self.root, rng, store_root=self.store_root, build=build)
        self.store.publish_snapshot(session.id, revision.id)
        return str(revision.id)

    def review(self, head: dict[str, str | None]) -> SimpleNamespace:
        """A new Review whose head branches off the base commit."""

        branch = f"change-{self._offset}"
        base_commit = self.repo.get(self.base_sha)
        self.repo.branches.local.create(branch, base_commit, force=True)
        self.repo.checkout(f"refs/heads/{branch}", strategy=pygit2.GIT_CHECKOUT_FORCE)
        _write(self.root, head)
        self._offset += 1
        _commit(self.repo, branch, self._offset)
        rng = resolve_rev_range(self.root, base=self.base_sha, head="HEAD")
        build = build_review_packet_with_blobs(self.root, rng, store_root=self.store_root, with_patch_text=True)
        session = open_or_create_session(self.store, self.root, rng, title=branch, force_new=True)
        revision = snapshot_revision(self.store, session, self.root, rng, store_root=self.store_root, build=build)
        self.store.publish_snapshot(session.id, revision.id)
        return SimpleNamespace(review_id=session.id, revision_id=revision.id, branch=branch)

    def push(self, review: SimpleNamespace, files: dict[str, str | None]) -> str:
        """A new Snapshot of an existing Review (its branch moves)."""

        self.repo.checkout(f"refs/heads/{review.branch}", strategy=pygit2.GIT_CHECKOUT_FORCE)
        _write(self.root, files)
        self._offset += 1
        _commit(self.repo, "follow-up", self._offset)
        session = self.store.get_session(review.review_id)
        review.revision_id = self._snapshot(session)
        return str(review.revision_id)

    def plan(self, review: SimpleNamespace) -> dict[str, Any]:
        response = self.client.get(f"/api/reviews/{review.review_id}/run-plan", headers=HEADERS)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def entry(self, review: SimpleNamespace, entry_id: str) -> dict[str, Any]:
        return next(row for row in self.plan(review)["plan"]["entries"] if row["id"] == entry_id)

    def run(self, review: SimpleNamespace, entry_id: str = "checks.test", *, fresh: bool = False) -> Any:
        return self.client.post(
            f"/api/reviews/{review.review_id}/check-recipes/run",
            headers=HEADERS,
            json={"revision_id": review.revision_id, "entry": entry_id, **({"fresh": True} if fresh else {})},
        )

    def recipes(self, review: SimpleNamespace) -> dict[str, Any]:
        response = self.client.get(f"/api/reviews/{review.review_id}/check-recipes", headers=HEADERS)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def unit(self, review: SimpleNamespace) -> dict[str, Any]:
        return next(row for row in self.recipes(review)["results"] if row["recipe"]["id"] == "unit")


def _failing_on(side_outcomes: dict[str, str]) -> Script:
    """``test_alpha`` fails or passes per side; everything else passes."""

    def script(_runtime: str, side: str, _count: int, command: tuple[str, ...]) -> dict[str, str]:
        cases = _passing(_runtime, side, _count, command)
        if "test_alpha::test_alpha" in cases:
            cases["test_alpha::test_alpha"] = side_outcomes[side]
        return cases

    return script


# --------------------------------------------------------------------------- #
# item 19: select before executing
# --------------------------------------------------------------------------- #


def test_a_change_to_alpha_runs_alpha_tests_only_and_the_reason_is_visible(tmp_path: Path) -> None:
    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    entry = world.entry(review, "checks.test")
    assert entry["status"] == "run" and "test impact: 1 of 2 test files reach a changed file" in entry["reason"]
    unit = entry["selection"]["recipes"]["unit"]
    assert (unit["mode"], unit["tests"], unit["total"]) == ("impact", ["tests/test_alpha.py"], 2)
    shown = next(row for row in world.recipes(review)["entries"] if row["id"] == "checks.test")
    assert shown["selection"]["recipes"]["unit"]["reason"] == "test impact: 1 of 2 test files reach a changed file"
    response = world.run(review)
    assert response.status_code == 200, response.text
    assert response.json()["verdicts"] == {"checks.recipe:unit": "PASS", "checks.test": "PASS"}
    [(_review, _runtime, side, command)] = world.runner.calls
    assert side == "new" and command[-1] == "tests/test_alpha.py" and "tests/test_beta.py" not in command
    result = world.unit(review)
    assert result["economy"]["selection"]["selected"] == 1
    assert "ran 1 of 2 test files selected by test impact" in result["reasons"]
    # A narrowed PASS never carries the full recipe digest a CI-equivalence row pins.
    digest = world.recipes(review)["entry_digests"]["checks.test"]
    verdict = next(row for row in world.recipes(review)["verdicts"] if row["stable_key"] == "checks.test")
    row = world.store.get_evidence(verdict["evidence_id"])
    assert row is not None and "recipe_digest=selected%3Asha256%3A" in row.attestation_scope
    assert digest.replace(":", "%3A") not in row.attestation_scope


def test_without_a_map_the_full_suite_runs_with_the_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    monkeypatch.setattr(
        RunPlanService, "_impact_map", lambda self, revision: (None, "the trusted base tree is not readable")
    )
    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    entry = world.entry(review, "checks.test")
    unit = entry["selection"]["recipes"]["unit"]
    assert entry["status"] == "run" and unit["mode"] == "all" and "tests" not in unit
    assert unit["reason"].startswith("full suite: no test-impact map for base")
    assert "(the trusted base tree is not readable)" in entry["reason"]
    assert world.run(review).status_code == 200
    [(_review, _runtime, _side, command)] = world.runner.calls
    assert not any(arg.startswith("tests/") for arg in command) and "{tests}" not in command


def test_building_the_impact_map_is_metered_compute_once_per_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RC12-B item 1: the import-graph build is spent compute, visible and counted, never invisible."""

    import time

    from lemoncrow.pro.capabilities.review import test_impact
    from lemoncrow.pro.capabilities.review.compute_ledger import ComputeLedger

    real_build = test_impact.build_impact_map
    builds: list[str] = []

    def slow_build(base_sha: str, paths: Any, read: Any) -> Any:
        builds.append(base_sha)
        time.sleep(0.05)
        return real_build(base_sha, paths, read)

    monkeypatch.setattr(test_impact, "build_impact_map", slow_build)
    world = _World(tmp_path)
    first = world.review(ALPHA_HEAD)
    plan = world.plan(first)
    assert builds == [world.base_sha]
    [row] = [row for row in plan["compute"]["rows"] if row["entry_id"] == test_impact.IMPACT_MAP_ENTRY]
    assert (row["executor"], row["stage"], row["runs"]) == ("review_host", "impact_map_build", 1)
    [metered] = ComputeLedger(world.store).rows([first.revision_id])
    assert metered.entry_id == test_impact.IMPACT_MAP_ENTRY and metered.seconds >= 0.05
    # It counts against the Review's own minutes (the budget sees it).
    assert plan["plan"]["budget"]["usage"]["review_minutes"] >= round(0.05 / 60.0, 3)
    # Reads and later Reviews on the same base reuse the map: nothing is built or metered again.
    world.plan(first)
    second = world.review({"pkg/beta.py": "def beta():\n    return 2  # head\n"})
    assert world.entry(second, "checks.test")["selection"]["recipes"]["unit"]["mode"] == "impact"
    assert builds == [world.base_sha]
    assert ComputeLedger(world.store).rows([second.revision_id]) == ()
    assert ComputeLedger(world.store).rows([first.revision_id])[0].runs == 1


def test_a_policy_required_check_runs_in_full_when_no_test_reaches_the_change(tmp_path: Path) -> None:
    docs_only = {"README.md": "# changed\n"}
    optional = _World(tmp_path / "optional")
    skipped = optional.entry(optional.review(docs_only), "checks.test")
    assert skipped["status"] == "skipped"
    assert skipped["reason"].startswith("test impact: only documentation no test reads changed (README.md)")
    required = _World(tmp_path / "required", config=CONFIG + "review:\n  required: [checks.test]\n")
    review = required.review(docs_only)
    entry = required.entry(review, "checks.test")
    assert entry["status"] == "run" and entry["required"] is True
    assert entry["selection"]["recipes"]["unit"]["mode"] == "all"
    assert "selection never narrows policy-required work" in entry["reason"]
    assert required.run(review).status_code == 200
    [(_review, _runtime, _side, command)] = required.runner.calls
    assert not any(arg.startswith("tests/") for arg in command)


# --------------------------------------------------------------------------- #
# item 20: head first, base on demand
# --------------------------------------------------------------------------- #


def test_head_first_a_passing_head_never_runs_the_base(tmp_path: Path) -> None:
    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    entry = world.entry(review, "checks.test")
    assert entry["sides"] == "head" and entry["base_on_demand"] is True
    assert world.run(review).status_code == 200
    assert world.runner.sides(review.review_id) == ["new"]
    result = world.unit(review)
    assert result["sides"] == ["new"] and result["base_verdict"] == ""
    assert result["economy"]["base"] == {
        "mode": "on_demand",
        "ran": False,
        "reason": "head passed: the base runs only to classify a head failure",
    }


@pytest.mark.parametrize(
    ("base_outcome", "klass"), [("passed", "introduced_failure"), ("failed", "pre_existing_failure")]
)
def test_a_failing_head_runs_the_base_once_and_classifies_it(tmp_path: Path, base_outcome: str, klass: str) -> None:
    world = _World(tmp_path, runner=_Runner(_failing_on({"new": "failed", "old": base_outcome})))
    review = world.review(ALPHA_HEAD)
    response = world.run(review)
    assert response.status_code == 200, response.text
    assert world.runner.sides(review.review_id) == ["new", "old"]
    result = world.unit(review)
    assert result["sides"] == ["old", "new"] and result["comparable"] is True
    assert {row["subject"].rpartition("::")[2]: row["class"] for row in result["tests"]} == {"test_alpha": klass}
    assert result["economy"]["base"]["ran"] is True
    # The base ran the same selected tests as the head.
    assert {call[3][-1] for call in world.runner.calls} == {"tests/test_alpha.py"}


# --------------------------------------------------------------------------- #
# item 21: shared base results
# --------------------------------------------------------------------------- #


def _economy_rows(world: _World, review: SimpleNamespace) -> dict[str, float]:
    economy = world.plan(review)["economy"]
    return {row["reason"]: row["minutes"] for row in economy["rows"]}


def test_two_reviews_on_one_base_fingerprint_and_recipe_execute_the_base_once(tmp_path: Path) -> None:
    world = _World(tmp_path, runner=_Runner(_failing_on({"new": "failed", "old": "passed"})))
    first = world.review(ALPHA_HEAD)
    assert world.run(first).status_code == 200
    assert world.runner.sides(first.review_id) == ["new", "old"]
    second = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # other head\n"})
    response = world.run(second)
    assert response.status_code == 200, response.text
    body = response.json()
    # Only the head executed; the base came from the first Review, with its provenance.
    assert world.runner.sides(second.review_id) == ["new"]
    assert body["shared_base"] == 1 and body["executions"] == 1 and body["saved_seconds"] == 90.0
    result = world.unit(second)
    base = next(row for row in result["attempts"] if row["side"] == "old")
    assert base["reused"] is True and base["reused_from"]["kind"] == "shared_base"
    assert base["reused_from"]["review_id"] == first.review_id
    assert {row["subject"].rpartition("::")[2]: row["class"] for row in result["tests"]} == {
        "test_alpha": "introduced_failure"
    }
    # Compute saved is visible next to compute spent.
    economy = world.plan(second)["economy"]
    assert economy["saved_measured_minutes"] == 1.5 and economy["spent_minutes"] > 0
    assert _economy_rows(world, second)["base_shared"] == 1.5


def test_a_different_fingerprint_or_recipe_digest_is_never_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    world = _World(tmp_path, runner=_Runner(_failing_on({"new": "failed", "old": "passed"})))
    assert world.run(world.review(ALPHA_HEAD)).status_code == 200
    # Another image: another environment fingerprint, so the base executes again.
    world.runner.image = "sha256:image-two"
    other_image = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # two\n"})
    assert world.run(other_image).json()["shared_base"] == 0
    assert world.runner.sides(other_image.review_id) == ["new", "old"]
    # Another recipe digest (the trusted base recipe changed): never reused either.
    original = RunPlanService.recipe_catalog

    def edited(self: RunPlanService, revision: Any) -> Any:
        catalog = original(self, revision)
        recipes = tuple(replace(recipe, command=(*recipe.command, "--edited")) for recipe in catalog.recipes)
        return replace(catalog, recipes=recipes)

    monkeypatch.setattr(RunPlanService, "recipe_catalog", edited)
    other_recipe = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # three\n"})
    assert world.run(other_recipe).json()["shared_base"] == 0
    assert world.runner.sides(other_recipe.review_id) == ["new", "old"]


def test_the_shared_base_kill_switch_runs_the_base_and_turning_it_back_on_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate T-J item 2: ``LEMONCROW_REVIEW_SHARED_BASE=off`` executes every base side again."""

    world = _World(tmp_path, runner=_Runner(_failing_on({"new": "failed", "old": "passed"})))
    first = world.review(ALPHA_HEAD)
    assert world.run(first).status_code == 200
    monkeypatch.setenv("LEMONCROW_REVIEW_SHARED_BASE", "off")
    second = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # other head\n"})
    body = world.run(second).json()
    assert (body["shared_base"], body["saved_seconds"]) == (0, 0.0)
    assert world.runner.sides(second.review_id) == ["new", "old"]
    assert "base_shared" not in _economy_rows(world, second)
    # Back on: the next Review reuses a base run again, with no migration.
    monkeypatch.setenv("LEMONCROW_REVIEW_SHARED_BASE", "on")
    third = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # third head\n"})
    body = world.run(third).json()
    assert body["shared_base"] == 1 and world.runner.sides(third.review_id) == ["new"]
    base = next(row for row in world.unit(third)["attempts"] if row["side"] == "old")
    assert base["reused_from"]["review_id"] in {first.review_id, second.review_id}


def test_a_disabled_shared_base_lookup_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.base_results import (
        SHARED_BASE_FLAG,
        SharedBaseKey,
        find_shared_base,
        shared_base_enabled,
    )

    def unread(*_args: Any) -> Any:
        raise AssertionError("read a shared base row")

    store = SimpleNamespace(derived_rows_by_scope=unread, get_evidence=unread)
    key = SharedBaseKey("b" * 40, "sha256:recipe", "sha256:env", "author_machine", "policy", "scope", 1)
    for value in ("off", "OFF", " off ", "0", "false", "enabled"):
        monkeypatch.setenv(SHARED_BASE_FLAG, value)
        assert not shared_base_enabled() and find_shared_base(store, key) is None
    for value in ("on", " On ", ""):
        monkeypatch.setenv(SHARED_BASE_FLAG, value)
        assert shared_base_enabled()
        with pytest.raises(AssertionError, match="read a shared base row"):
            find_shared_base(store, key)  # on, the same usable key is looked up


def test_a_head_run_can_never_write_a_shared_base_result(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.base_results import (
        BASE_RESULT_KIND,
        SharedBaseKey,
        find_shared_base,
        publish_refusal,
        publish_shared_base,
    )
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    # An executor that labels its base run with a head cache namespace shares nothing.
    runner = _Runner(_failing_on({"new": "failed", "old": "passed"}))
    runner.namespace = {"old": "head_untrusted", "new": "head_untrusted"}
    world = _World(tmp_path, runner=runner)
    first = world.review(ALPHA_HEAD)
    body = world.run(first).json()
    assert any(
        "base result not shared (the execution wrote the head_untrusted cache namespace" in n for n in body["notes"]
    )
    second = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # other\n"})
    assert world.run(second).json()["shared_base"] == 0
    assert world.runner.sides(second.review_id) == ["new", "old"]

    # Every head-side record is refused at publication, whatever it claims.
    result = world.unit(second)
    head = next(row for row in result["attempts"] if row["side"] == "new")
    base = next(row for row in result["attempts"] if row["side"] == "old")
    head_item = world.store.get_evidence(head["evidence_id"])
    head_record = json.loads(world.store.read_evidence_artifact(head_item))
    environment = head["environment"]
    revision = world.store.get_revision(second.revision_id)
    key = SharedBaseKey(world.base_sha, result["recipe"]["digest"], environment, "author_machine", "p", "s", 1)
    forged = {**head_record, "side": "old"}
    assert publish_refusal(head_record, key, revision_base_sha=world.base_sha, side="new", variant="run", overlay=False)
    assert publish_refusal(forged, key, revision_base_sha=world.base_sha, side="old", variant="run", overlay=False) == (
        "the execution did not run the base commit unmodified"
    )
    refused = publish_shared_base(
        world.store,
        review_id=second.review_id,
        revision_id=second.revision_id,
        revision_base_sha=revision.base_sha,
        key=key,
        evidence=head_item,
        record=head_record,
        side="new",
        variant="run",
        overlay=False,
    )
    assert refused == "only a base-side run is shared"
    assert world.store.derived_rows_by_scope(BASE_RESULT_KIND, key.scope_key) == []
    # A row planted directly in the store that points at head evidence is re-verified and ignored.
    world.store.put_derived_results(
        [
            DerivedResult(
                review_id=second.review_id,
                revision_id=second.revision_id,
                kind=BASE_RESULT_KIND,
                scope_key=key.scope_key,
                stamp="v1",
                payload={
                    "evidence_id": head_item.id,
                    "content_hash": head_item.content_hash,
                    "base_sha": key.base_sha,
                    "recipe_digest": key.recipe_digest,
                    "environment": key.environment,
                    "executor": key.executor_class,
                    "attempt": 1,
                    "duration_ms": 1,
                },
            )
        ]
    )
    assert find_shared_base(world.store, key) is None
    assert "reused_from" not in base


def test_shared_base_results_are_evicted_least_recently_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import base_results

    monkeypatch.setattr(base_results, "MAX_SHARED_BASE_RESULTS", 1)
    world = _World(tmp_path, runner=_Runner(_failing_on({"new": "failed", "old": "passed"})))

    def beta_fails(_runtime: str, side: str, _count: int, command: tuple[str, ...]) -> dict[str, str]:
        cases = _failing_on({"new": "failed", "old": "passed"})(_runtime, side, _count, command)
        if "test_beta::test_beta" in cases and side == "new":
            cases["test_beta::test_beta"] = "failed"
        return cases

    world.runner.script = beta_fails
    assert world.run(world.review(ALPHA_HEAD)).status_code == 200  # shares the alpha-selection base
    assert world.run(world.review({"pkg/beta.py": "def beta():\n    return 2  # b\n"})).status_code == 200
    # The beta-selection base evicted the alpha one (cap 1): an alpha Review executes its base again.
    third = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # c\n"})
    assert world.run(third).json()["shared_base"] == 0
    assert world.runner.sides(third.review_id) == ["new", "old"]
    # ...and its fresh base is now the one kept; a fourth alpha Review reuses it.
    fourth = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # d\n"})
    assert world.run(fourth).json()["shared_base"] == 1


# --------------------------------------------------------------------------- #
# item 22: incremental per Snapshot
# --------------------------------------------------------------------------- #


def test_unchanged_inputs_reuse_the_earlier_snapshot_and_changed_inputs_rerun(tmp_path: Path) -> None:
    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    assert world.run(review).json()["executions"] == 1
    # Snapshot 2 adds a module no selected test loads: test_alpha's inputs are unchanged.
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    entry = world.entry(review, "checks.test")
    assert entry["selection"]["recipes"]["unit"]["tests"] == ["tests/test_alpha.py"]
    body = world.run(review).json()
    assert body["executions"] == 0 and body["carried"] == 1 and body["verdicts"]["checks.test"] == "PASS"
    attempt = world.unit(review)["attempts"][0]
    assert attempt["reused_from"]["kind"] == "snapshot" and attempt["reused_from"]["snapshot"] == 1
    assert _economy_rows(world, review)["carried_forward"] == 1.5
    # Snapshot 3 changes what test_alpha loads: it runs again.
    world.push(review, {"pkg/alpha.py": "def alpha():\n    return 1  # changed again\n"})
    body = world.run(review).json()
    assert body["executions"] == 1 and body["carried"] == 0
    assert len(world.runner.calls) == 2


def test_a_changed_executor_environment_runs_again_and_says_why(tmp_path: Path) -> None:
    """Trust T3 item 1: carry needs the same executor environment, not only the same inputs."""

    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    assert world.run(review).json()["executions"] == 1
    # Snapshot 2 leaves test_alpha's inputs unchanged, but the executor image moved.
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    world.runner.image = "sha256:image-two"
    body = world.run(review).json()
    assert (body["executions"], body["carried"]) == (1, 0)
    [row] = body["validity"]
    assert (row["reason_code"], row["state"]) == ("carry_environment_changed", "reverify")
    # Snapshot 3 runs in that same new environment, so it carries from Snapshot 2.
    snapshot_two = review.revision_id
    world.push(review, {"pkg/gamma.py": "GAMMA = 2\n"})
    body = world.run(review).json()
    assert (body["executions"], body["carried"]) == (0, 1)
    assert body["validity"][0]["source_revision"] == snapshot_two
    assert len(world.runner.calls) == 2


@pytest.mark.parametrize("state", ["unknown", "broken"])
def test_without_a_pre_dispatch_environment_nothing_is_carried(tmp_path: Path, state: str) -> None:
    """A hosted worker reports its environment only with its result, so its head runs execute again."""

    world = _World(tmp_path)
    world.runner.environment_state = state
    review = world.review(ALPHA_HEAD)
    assert world.run(review).json()["executions"] == 1
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    body = world.run(review).json()
    assert (body["executions"], body["carried"], body["saved_seconds"]) == (1, 0, 0.0)
    [row] = body["validity"]
    assert (row["reason_code"], row["state"]) == ("carry_environment_unknown", "reverify")
    assert "carried_forward" not in _economy_rows(world, review)


def test_the_carry_kill_switch_runs_again_and_turning_it_back_on_carries_without_a_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate T-J item 2: ``LEMONCROW_REVIEW_CARRY=off`` restores executing every plain head run."""

    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    assert world.run(review).json()["executions"] == 1
    monkeypatch.setenv("LEMONCROW_REVIEW_CARRY", "off")
    # Snapshot 2 leaves test_alpha's inputs unchanged, but carry is off: it runs again.
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    body = world.run(review).json()
    assert (body["executions"], body["carried"], body["saved_seconds"]) == (1, 0, 0.0)
    [row] = body["validity"]
    assert (row["reason_code"], row["state"], row["source_revision"]) == ("carry_disabled", "reverify", "")
    assert "carried_forward" not in _economy_rows(world, review)
    [ran] = _summary(world, review)["run"]
    assert (ran["state"], ran["reason_code"]) == ("ran", "carry_disabled") and ran["detail"]
    assert _summary(world, review)["reuse"] == []
    # The same Snapshot still replays its own result: that is not a carry.
    assert world.run(review).json()["executions"] == 0
    # Back on: Snapshot 3 carries from the run Snapshot 2 executed while carry was off.
    snapshot_two = review.revision_id
    monkeypatch.delenv("LEMONCROW_REVIEW_CARRY")
    world.push(review, {"pkg/gamma.py": "GAMMA = 2\n"})
    body = world.run(review).json()
    assert (body["executions"], body["carried"]) == (0, 1)
    assert (body["validity"][0]["reason_code"], body["validity"][0]["source_revision"]) == (
        "carry_exact_key",
        snapshot_two,
    )
    assert len(world.runner.calls) == 2


def test_with_carry_off_a_snapshot_holding_a_carried_copy_runs_fresh_instead_of_replaying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate T-J item 2 fix: turning carry off also stops replaying a copy already carried onto a Snapshot."""

    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    first = review.revision_id
    assert world.run(review).json()["executions"] == 1
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    snapshot_two = review.revision_id
    assert world.run(review).json()["carried"] == 1
    # Carry on: the carried copy replays, and the replay names the Snapshot that actually ran it.
    body = world.run(review).json()
    assert (body["executions"], body["reused"]) == (0, 1)
    [row] = body["validity"]
    assert (row["reason_code"], row["state"], row["source_revision"]) == ("replayed_on_snapshot", "reused", first)
    assert row["evidence_refs"] and row["current_revision"] == snapshot_two
    # Carry off: the copy is not replayed; the check runs fresh on this Snapshot and says why.
    monkeypatch.setenv("LEMONCROW_REVIEW_CARRY", "off")
    body = world.run(review).json()
    assert (body["executions"], body["carried"], body["reused"]) == (1, 0, 0)
    [row] = body["validity"]
    assert (row["reason_code"], row["state"]) == ("carry_disabled", "reverify")
    summary = _summary(world, review)
    assert summary["reuse"] == [] and [(r["state"], r["reason_code"]) for r in summary["run"]] == [
        ("ran", "carry_disabled")
    ]
    # That run is now this Snapshot's own result: it replays, named as this Snapshot's.
    body = world.run(review).json()
    assert (body["executions"], body["reused"]) == (0, 1)
    [row] = body["validity"]
    assert (row["reason_code"], row["source_revision"]) == ("replayed_on_snapshot", snapshot_two)
    # Back on: the Snapshot's own run still wins over its carried copy, and Snapshot 3 carries from it.
    monkeypatch.delenv("LEMONCROW_REVIEW_CARRY")
    body = world.run(review).json()
    assert body["executions"] == 0 and body["validity"][0]["source_revision"] == snapshot_two
    world.push(review, {"pkg/gamma.py": "GAMMA = 2\n"})
    body = world.run(review).json()
    assert (body["executions"], body["carried"]) == (0, 1)
    assert (body["validity"][0]["reason_code"], body["validity"][0]["source_revision"]) == (
        "carry_exact_key",
        snapshot_two,
    )
    assert len(world.runner.calls) == 2


@pytest.mark.parametrize(
    ("state", "code"), [("changed", "carry_environment_changed"), ("unknown", "carry_environment_unknown")]
)
def test_a_carried_copy_is_not_replayed_once_the_environment_changed_or_is_unknown(
    tmp_path: Path, state: str, code: str
) -> None:
    """Trust T3 item 1 fix: a carried copy replays only while it still could carry."""

    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    assert world.run(review).json()["executions"] == 1
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    assert world.run(review).json()["carried"] == 1
    if state == "changed":
        world.runner.image = "sha256:image-two"
    else:
        world.runner.environment_state = "unknown"
    body = world.run(review).json()
    assert (body["executions"], body["reused"]) == (1, 0)
    [row] = body["validity"]
    assert (row["reason_code"], row["state"]) == (code, "reverify")
    assert len(world.runner.calls) == 2


def test_every_plain_head_attempt_says_why_it_was_reused_or_runs_again(tmp_path: Path) -> None:
    """Trust T1: the check-run report explains each plain head attempt in the validity vocabulary."""

    def explained(body: Any) -> tuple[str, str, str, str]:
        [row] = body["validity"]
        assert row["subject_ref"] == "check:unit#1" and row["current_revision"] == review.revision_id
        return row["reason_code"], row["state"], row["source_revision"], ",".join(row["chain"])

    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    first = review.revision_id
    assert explained(world.run(review).json()) == (
        "carry_no_earlier_snapshot",
        "reverify",
        "",
        "carry_no_earlier_snapshot",
    )
    assert explained(world.run(review).json())[:2] == ("replayed_on_snapshot", "reused")
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    assert explained(world.run(review).json())[:3] == ("carry_exact_key", "reused", first)
    world.push(review, {"pkg/alpha.py": "def alpha():\n    return 1  # changed again\n"})
    body = world.run(review).json()
    assert body["executions"] == 1 and explained(body)[:2] == ("carry_inputs_changed", "reverify")
    body = world.run(review, fresh=True).json()
    assert body["executions"] == 1 and explained(body)[:2] == ("fresh_run_requested", "reverify")


def test_a_fresh_run_executes_instead_of_replaying_or_carrying_and_becomes_the_carry_source(
    tmp_path: Path,
) -> None:
    """RC12-B item 4: ``fresh: true`` reuses nothing; a plain run still reuses."""

    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    assert world.run(review).json()["executions"] == 1
    # Same Snapshot: a plain run replays, a fresh run executes again.
    assert world.run(review).json()["executions"] == 0
    fresh = world.run(review, fresh=True).json()
    assert (fresh["executions"], fresh["reused"], fresh["carried"]) == (1, 0, 0) and fresh["fresh_run_id"]
    assert world.run(review, fresh=True).json()["executions"] == 1  # each fresh run is its own
    assert len(world.runner.calls) == 3
    # Snapshot 2 with unchanged test inputs: a plain run carries, a fresh run executes.
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    assert world.run(review).json()["carried"] == 1
    body = world.run(review, fresh=True).json()
    assert (body["executions"], body["carried"], body["saved_seconds"]) == (1, 0, 0.0)
    assert body["verdicts"]["checks.test"] == "PASS"
    assert len(world.runner.calls) == 4
    snapshot_two = world.store.latest_revision(review.review_id)
    # Snapshot 3: the carried result now comes from the fresh run on Snapshot 2.
    world.push(review, {"pkg/gamma.py": "GAMMA = 2\n"})
    assert world.run(review).json()["carried"] == 1
    attempt = world.unit(review)["attempts"][0]
    assert attempt["reused_from"]["revision_id"] == snapshot_two.id
    assert attempt["reused_from"]["snapshot"] == snapshot_two.revision_number


def test_repeated_fresh_runs_stop_at_the_per_review_minutes_ceiling(tmp_path: Path) -> None:
    """RC12-B fix round: a fresh run is admitted against the live ledger, not the plan's frozen usage."""

    from lemoncrow.pro.capabilities.review.compute_ledger import ComputeLedger

    world = _World(tmp_path, config=CONFIG + "review:\n  budget:\n    per_review_minutes: 12\n")
    review = world.review(ALPHA_HEAD)
    estimate = world.entry(review, "checks.test")["estimated_minutes"]
    assert world.run(review).status_code == 200
    responses = [world.run(review, fresh=True) for _ in range(12)]
    admitted = [row for row in responses if row.status_code == 200]
    refused = [row for row in responses if row.status_code != 200]
    assert admitted and refused
    # Once refused, every later fresh run is refused too, each with the reason.
    assert all(row.status_code == 409 for row in responses[len(admitted) :])
    detail = refused[0].json()["detail"]
    assert "does not fit per_review_minutes" in detail and "of 12 min already spent on this Review" in detail
    spent = ComputeLedger(world.store).minutes([review.revision_id])
    assert spent <= 12, (spent, estimate)
    assert len(world.runner.calls) == 1 + len(admitted)
    # A plain run still replays (no new compute) after the ceiling.
    plain = world.run(review)
    assert plain.status_code == 200 and plain.json()["executions"] == 0


def test_a_fresh_run_is_refused_at_the_metered_monthly_minutes_ceiling() -> None:
    from lemoncrow.pro.capabilities.review.check_api import fresh_budget_refusal
    from lemoncrow.pro.capabilities.review.run_plan import BudgetUsage

    plan = SimpleNamespace(
        ceilings={"monthly_minutes": 100},
        entry=lambda _id: SimpleNamespace(estimated_minutes=3.0),
    )
    # Not metered here: no refusal on an unknown spend.
    assert fresh_budget_refusal(plan, "checks.test", BudgetUsage(review_minutes=500)) == ""
    assert fresh_budget_refusal(plan, "checks.test", BudgetUsage(monthly_minutes=97.0)) == ""
    refusal = fresh_budget_refusal(plan, "checks.test", BudgetUsage(monthly_minutes=97.5))
    assert refusal.startswith("a fresh run (about 3 min) does not fit monthly_minutes")
    assert fresh_budget_refusal(plan, "checks.test", BudgetUsage(monthly_minutes=100.0))


def _head_by_call(outcomes: dict[int, str]) -> Script:
    """The head's ``test_alpha`` outcome per head call number (default: passed)."""

    def script(_runtime: str, side: str, count: int, command: tuple[str, ...]) -> dict[str, str]:
        cases = _passing(_runtime, side, count, command)
        if side == "new" and "test_alpha::test_alpha" in cases:
            cases["test_alpha::test_alpha"] = outcomes.get(count, "passed")
        return cases

    return script


def _shown_verdict(world: _World, review: SimpleNamespace) -> str:
    row = next(row for row in world.recipes(review)["verdicts"] if row["stable_key"] == "checks.test")
    return str(row["status"])


@pytest.mark.parametrize(
    ("outcomes", "plain", "fresh"),
    [({2: "failed"}, "PASS", "FAIL"), ({1: "failed"}, "FAIL", "PASS")],
    ids=["pass-then-fresh-fail", "fail-then-fresh-pass"],
)
def test_a_plain_run_after_a_fresh_run_resolves_to_the_fresh_result(
    tmp_path: Path, outcomes: dict[int, str], plain: str, fresh: str
) -> None:
    """RC12-B fix round: the newest executed result supersedes the Snapshot's older plain one."""

    world = _World(tmp_path, runner=_Runner(_head_by_call(outcomes)))
    review = world.review(ALPHA_HEAD)
    assert world.run(review).json()["verdicts"]["checks.test"] == plain
    assert world.run(review, fresh=True).json()["verdicts"]["checks.test"] == fresh
    calls = len(world.runner.calls)
    again = world.run(review).json()
    assert again["verdicts"]["checks.test"] == fresh and again["executions"] == 0
    assert _shown_verdict(world, review) == fresh
    assert len(world.runner.calls) == calls
    # The next Snapshot carries the fresh result, consistent with this Snapshot's verdict.
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    carried = world.run(review).json()
    assert carried["carried"] == 1 and carried["verdicts"]["checks.test"] == fresh


def test_a_plain_run_after_a_fresh_only_snapshot_replays_the_fresh_result(tmp_path: Path) -> None:
    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    assert world.run(review, fresh=True).json()["executions"] == 1
    body = world.run(review).json()
    assert (body["executions"], body["reused"], body["verdicts"]["checks.test"]) == (0, 1, "PASS")
    assert len(world.runner.calls) == 1


def test_a_fresh_run_executes_the_base_instead_of_using_the_shared_one(tmp_path: Path) -> None:
    world = _World(tmp_path, runner=_Runner(_failing_on({"new": "failed", "old": "passed"})))
    assert world.run(world.review(ALPHA_HEAD)).status_code == 200
    second = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # other head\n"})
    body = world.run(second, fresh=True).json()
    assert body["shared_base"] == 0 and body["executions"] == 2
    assert world.runner.sides(second.review_id) == ["new", "old"]
    base = next(row for row in world.unit(second)["attempts"] if row["side"] == "old")
    assert "reused_from" not in base and "shared_from" not in base


def test_a_fresh_base_run_replaces_its_snapshots_shared_base_row(tmp_path: Path) -> None:
    """RC12-B fix round: another Review shares the newest executed base, not the superseded one."""

    def script(_runtime: str, side: str, count: int, command: tuple[str, ...]) -> dict[str, str]:
        cases = _passing(_runtime, side, count, command)
        if "test_alpha::test_alpha" in cases:
            cases["test_alpha::test_alpha"] = "failed" if side == "new" or count >= 2 else "passed"
        return cases

    world = _World(tmp_path, runner=_Runner(script))
    first = world.review(ALPHA_HEAD)
    assert world.run(first).status_code == 200
    assert world.run(first, fresh=True).json()["executions"] == 2  # the base now fails
    fresh_base = next(row for row in world.unit(first)["attempts"] if row["side"] == "old")
    assert fresh_base["status"] == "failed"
    second = world.review({"pkg/alpha.py": "def alpha():\n    return 1  # other head\n"})
    assert world.run(second).json()["shared_base"] == 1
    base = next(row for row in world.unit(second)["attempts"] if row["side"] == "old")
    assert base["reused_from"]["evidence_id"] == fresh_base["evidence_id"] and base["status"] == "failed"


# --------------------------------------------------------------------------- #
# item 23: reuse CI through P11-FED equivalence
# --------------------------------------------------------------------------- #

_PROVIDER = '  - {recipe: "checks.recipe:unit", check: unit-tests, provider: github}\n'


def _ingest(world: _World, review: SimpleNamespace, *, head_sha: str, stable_key: str = "unit-tests") -> Any:
    from lemoncrow.pro.capabilities.review.check_ingest import CheckIngest, IngestAuthority, record_check_ingest

    github = IngestAuthority(attestation_class="provider_ci", source="ci", provider="github", integration_id="actions")
    revision = world.store.latest_revision(review.review_id)
    row, _created = record_check_ingest(
        world.store,
        revision,
        CheckIngest(
            tool="pytest",
            category="tests",
            status="PASS",
            head_sha=head_sha,
            title="CI / unit tests",
            stable_key=stable_key,
            remote_identity=f"run-{stable_key}-{head_sha[:6]}",
            url="https://ci.example.invalid/runs/1",
        ),
        github,
    )
    return row


def test_an_equivalent_current_ci_result_satisfies_the_entry_and_nothing_executes(tmp_path: Path) -> None:
    world = _World(tmp_path, config=CONFIG + "ci_equivalents:\n" + _PROVIDER)
    review = world.review(ALPHA_HEAD)
    head_sha = str(world.repo.head.target)
    assert world.entry(review, "checks.recipe:unit")["status"] == "run"
    # An inequivalent result (a check no ci_equivalents row maps) satisfies nothing.
    _ingest(world, review, head_sha=head_sha, stable_key="lint")
    assert world.entry(review, "checks.recipe:unit")["status"] == "run"
    provider = _ingest(world, review, head_sha=head_sha)
    entry = world.entry(review, "checks.recipe:unit")
    assert entry["status"] == "satisfied" and entry["reason"].endswith("not re-run")
    # Trust T3 item 9: the re-verification summary shows it as reused CI evidence.
    [reused] = _summary(world, review, "checks.recipe:unit")["reuse"]
    assert (reused["kind"], reused["satisfied_by"]["evidence_id"]) == ("ci_equivalent", provider.id)
    # Gate T-J fix: the structured equivalence reason, on the plan entry and the summary row.
    assert (entry["equivalence"]["reason_code"], entry["equivalence"]["evidence_refs"]) == (
        "equivalent_result",
        [provider.id],
    )
    assert entry["equivalence"]["subject_ref"] == "entry:checks.recipe:unit"
    assert reused["reason_code"] == "equivalent_result" and reused["detail"].endswith("not re-run")
    assert entry["satisfied_by"]["evidence_id"] == provider.id
    assert entry["satisfied_by"]["url"] == "https://ci.example.invalid/runs/1"
    shown = next(row for row in world.recipes(review)["entries"] if row["id"] == "checks.recipe:unit")
    assert shown["satisfied_by"]["attestation_class"] == "provider_ci"
    assert shown["satisfied_by"]["url"] == "https://ci.example.invalid/runs/1"
    refused = world.run(review, "checks.recipe:unit")
    assert refused.status_code == 409 and "not re-run" in refused.json()["detail"]
    assert world.runner.calls == []
    assert "ci_reuse" in _economy_rows(world, review)
    # RC12-B item 4: an explicit fresh run executes anyway, and the plan says why.
    forced = world.run(review, "checks.recipe:unit", fresh=True)
    assert forced.status_code == 200, forced.text
    assert forced.json()["executions"] == 1 and len(world.runner.calls) == 1
    assert "fresh_run_requested" in {
        row["reason_code"] for row in forced.json()["validity"] if row["subject_ref"] == "entry:checks.recipe:unit"
    }
    entry = world.entry(review, "checks.recipe:unit")
    assert entry["status"] == "run" and entry["satisfied_by"] is None
    assert entry["reason"].startswith("fresh run requested; an equivalent result exists (provider CI")
    assert entry["equivalence"]["reason_code"] == "fresh_run_requested"
    picks = world.plan(review)["plan"]["manual"]
    assert picks == [{"id": "checks.recipe:unit", "depth": picks[0]["depth"], "fresh": True}]


def test_the_ci_equivalence_kill_switch_runs_the_recipe_says_why_and_turns_back_on_without_a_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate T-J item 2: ``LEMONCROW_REVIEW_CI_EQUIVALENCE=off`` lets no CI result stand in."""

    world = _World(tmp_path, config=CONFIG + "ci_equivalents:\n" + _PROVIDER)
    review = world.review(ALPHA_HEAD)
    # With nothing to stand in, the switch changes no plan identity.
    plain_digest = world.plan(review)["plan"]["inputs_digest"]
    monkeypatch.setenv("LEMONCROW_REVIEW_CI_EQUIVALENCE", "off")
    assert world.plan(review)["plan"]["inputs_digest"] == plain_digest
    monkeypatch.delenv("LEMONCROW_REVIEW_CI_EQUIVALENCE")
    provider = _ingest(world, review, head_sha=str(world.repo.head.target))
    assert world.entry(review, "checks.recipe:unit")["status"] == "satisfied"
    satisfied_digest = world.plan(review)["plan"]["inputs_digest"]
    monkeypatch.setenv("LEMONCROW_REVIEW_CI_EQUIVALENCE", "off")
    entry = world.entry(review, "checks.recipe:unit")
    assert entry["status"] == "run" and entry["satisfied_by"] is None
    assert "an equivalent result exists (provider CI" in entry["reason"]
    assert "but CI equivalence is turned off on this server" in entry["reason"]
    assert world.plan(review)["plan"]["inputs_digest"] != satisfied_digest
    assert _summary(world, review, "checks.recipe:unit")["reuse"] == []
    # Gate T-J fix: the structured ``equivalence_disabled`` reason on the real federation path:
    # the plan entry, the re-verification summary, the run report and operator telemetry.
    assert (entry["equivalence"]["reason_code"], entry["equivalence"]["state"]) == (
        "equivalence_disabled",
        "reverify",
    )
    assert entry["equivalence"]["evidence_refs"] == [provider.id]
    [planned] = _summary(world, review, "checks.recipe:unit")["run"]
    assert (planned["state"], planned["reason_code"]) == ("planned", "equivalence_disabled")
    ran_now = world.run(review, "checks.recipe:unit")
    assert ran_now.status_code == 200
    assert [call[2] for call in world.runner.calls] == ["new"]
    [reported] = [row for row in ran_now.json()["validity"] if row["subject_ref"] == "entry:checks.recipe:unit"]
    assert (reported["reason_code"], reported["evidence_refs"]) == ("equivalence_disabled", [provider.id])
    [ran] = _summary(world, review, "checks.recipe:unit")["run"]
    assert (ran["state"], ran["reason_code"], ran["causes"]) == (
        "ran",
        "equivalence_disabled",
        ["carry_no_earlier_snapshot"],
    )
    from lemoncrow.pro.capabilities.review.trust_telemetry import run_plan_observation

    checks = run_plan_observation(world.plan(review)["reverification"])["checks"]
    assert {"disposition": "ran", "reason": "equivalence_disabled", "count": 1} in checks
    # Back on: the same CI result satisfies the entry again, under the earlier plan identity.
    monkeypatch.delenv("LEMONCROW_REVIEW_CI_EQUIVALENCE")
    entry = world.entry(review, "checks.recipe:unit")
    assert entry["status"] == "satisfied" and entry["satisfied_by"]["evidence_id"] == provider.id
    assert world.plan(review)["plan"]["inputs_digest"] == satisfied_digest


def test_a_stale_ci_result_runs_the_recipe(tmp_path: Path) -> None:
    world = _World(tmp_path, config=CONFIG + "ci_equivalents:\n" + _PROVIDER)
    review = world.review(ALPHA_HEAD)
    _ingest(world, review, head_sha=str(world.repo.head.target))
    assert world.entry(review, "checks.recipe:unit")["status"] == "satisfied"
    # A new Snapshot (another head): the old CI result is stale, so the recipe runs.
    world.push(review, {"pkg/alpha.py": "def alpha():\n    return 1  # pushed\n"})
    entry = world.entry(review, "checks.recipe:unit")
    assert entry["status"] == "run" and entry["satisfied_by"] is None
    assert world.run(review, "checks.recipe:unit").status_code == 200
    assert [call[2] for call in world.runner.calls] == ["new"]


# --------------------------------------------------------------------------- #
# Trust T3 items 2, 8, 9 and 10: the run plan's re-verification summary
# --------------------------------------------------------------------------- #


def _summary(world: _World, review: SimpleNamespace, entry_id: str = "checks.test") -> dict[str, list[dict[str, Any]]]:
    """The run plan's re-verification buckets, narrowed to one entry."""

    shown = world.plan(review)["reverification"]
    return {bucket: [row for row in rows if row["entry_id"] == entry_id] for bucket, rows in shown.items()}


def test_the_run_plan_says_what_runs_is_carried_or_is_not_affected_and_why(tmp_path: Path) -> None:
    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    first = review.revision_id
    summary = _summary(world, review)
    [planned] = summary["run"]
    assert (planned["recipe_id"], planned["state"]) == ("unit", "planned")
    assert planned["reason"] == "test impact: 1 of 2 test files reach a changed file"
    [left_out] = summary["skip_not_affected"]
    assert (left_out["recipe_id"], left_out["tests_skipped"]) == ("unit", 1)
    assert summary["reuse"] == summary["unknown_broadened"] == summary["unknown_not_run"] == []
    assert world.run(review).status_code == 200
    [ran] = _summary(world, review)["run"]
    assert (ran["state"], ran["reason_code"]) == ("ran", "carry_no_earlier_snapshot")
    # Snapshot 2 leaves test_alpha's inputs unchanged: carried, with its origin and why.
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    assert _summary(world, review)["run"][0]["state"] == "planned"
    assert world.run(review).json()["carried"] == 1
    summary = _summary(world, review)
    [carried] = summary["reuse"]
    assert summary["run"] == [] and [row["tests_skipped"] for row in summary["skip_not_affected"]] == [1]
    assert (carried["kind"], carried["recipe_id"], carried["origin_snapshot"], carried["origin_revision_id"]) == (
        "carried",
        "unit",
        1,
        first,
    )
    assert carried["reason_code"] == "carry_exact_key" and "same carry key" in carried["detail"]
    assert carried["origin_evidence_id"]
    # Snapshot 3 changes what test_alpha loads: it ran again, and the refusal says why.
    world.push(review, {"pkg/alpha.py": "def alpha():\n    return 1  # changed again\n"})
    assert world.run(review).json()["executions"] == 1
    [again] = _summary(world, review)["run"]
    assert (again["state"], again["reason_code"]) == ("ran", "carry_inputs_changed") and again["detail"]


def test_unknown_coverage_broadens_to_the_full_suite_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    monkeypatch.setattr(
        RunPlanService, "_impact_map", lambda self, revision: (None, "the trusted base tree is not readable")
    )
    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    assert world.entry(review, "checks.test")["selection"]["recipes"]["unit"]["coverage"] == "unknown"
    summary = _summary(world, review)
    [broad] = summary["unknown_broadened"]
    assert (broad["recipe_id"], broad["coverage"], broad["state"]) == ("unit", "coverage_unknown", "planned")
    assert broad["reason"].startswith("full suite: no test-impact map for base")
    assert summary["run"] == summary["skip_not_affected"] == []
    assert world.run(review).status_code == 200
    assert _summary(world, review)["unknown_broadened"][0]["state"] == "ran"


def test_a_full_run_the_change_is_known_to_need_is_not_unknown_coverage(tmp_path: Path) -> None:
    world = _World(tmp_path / "input")
    review = world.review({"run_tests.py": "import sys  # changed\n"})
    unit = world.entry(review, "checks.test")["selection"]["recipes"]["unit"]
    assert unit["mode"] == "all" and "coverage" not in unit
    summary = _summary(world, review)
    assert [row["reason"] for row in summary["run"]] == ["full suite: run_tests.py is an input of this recipe"]
    assert summary["unknown_broadened"] == []
    required = _World(tmp_path / "required", config=CONFIG + "review:\n  required: [checks.test]\n")
    summary = _summary(required, required.review({"README.md": "# changed\n"}))
    [forced] = summary["run"]
    assert forced["required"] is True and forced["reason"] == "full suite: selection never narrows policy-required work"
    assert summary["unknown_broadened"] == summary["skip_not_affected"] == []


def test_a_check_entry_without_a_selection_still_reports_each_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary finds an entry's recipes in the trusted-base catalog, not only in its selection."""

    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    monkeypatch.setattr(RunPlanService, "impact", lambda self, revision: {})
    world = _World(tmp_path)
    review = world.review(ALPHA_HEAD)
    assert world.entry(review, "checks.test")["selection"] is None
    assert [(row["recipe_id"], row["state"]) for row in _summary(world, review)["run"]] == [("unit", "planned")]
    assert world.run(review).status_code == 200
    [ran] = _summary(world, review)["run"]
    # Without a selection the run's inputs are unknown, so it could never be carried.
    assert (ran["recipe_id"], ran["state"], ran["reason_code"]) == ("unit", "ran", "carry_no_input_digest")


def test_a_change_no_test_reaches_is_not_affected(tmp_path: Path) -> None:
    world = _World(tmp_path)
    summary = _summary(world, world.review({"README.md": "# changed\n"}))
    [skipped] = summary["skip_not_affected"]
    assert skipped["reason"].startswith("test impact: only documentation no test reads changed (README.md)")
    assert summary["run"] == summary["not_run"] == summary["unknown_broadened"] == []
