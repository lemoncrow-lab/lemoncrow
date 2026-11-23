"""Trust T3 qualification corpus: no unsafe carry (trust plan §30).

Each case is a two-Snapshot Review through the real Review API, test-impact map
and carry path. The runner really executes the selected tests on the Snapshot
side's tree, so it is an oracle: after a plain run on Snapshot 2 (which may carry
Snapshot 1's run), a fresh run of the same Snapshot executes for real. A carried
result that differs from the fresh one is an unsafe carry.

Each case also states whether carrying is correct and, when it is not, the
refusal reason. Where a case can flip the outcome (``flips``), it does, so an
unsafe carry there would show as a carried result that differs from the fresh
one. The run plan's re-verification summary must tell the same story.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pygit2
import pytest

from tests.gateway.test_review_execution_economy import CONFIG, _Runner, _World

Hook = Callable[[_World, pytest.MonkeyPatch], None]

ALPHA = "from pkg.helper import one\n\n\ndef alpha():\n    return one()\n"
TEST_ALPHA = "from pkg.alpha import alpha\n\n\ndef test_alpha():\n    assert alpha() == 1\n"
BASE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/helper.py": "def one():\n    return 1\n",
    "pkg/alpha.py": ALPHA,
    "pkg/beta.py": "def beta():\n    return 2\n",
    "tests/conftest.py": "",
    "tests/test_alpha.py": TEST_ALPHA,
    "tests/test_beta.py": "from pkg.beta import beta\n\n\ndef test_beta():\n    assert beta() == 2\n",
    "run_tests.py": "import sys\n",
    "requirements.txt": "pytest\n",
    "README.md": "# fixture\n",
}
TOUCH_ALPHA: dict[str, str | None] = {"pkg/alpha.py": ALPHA + "# touched\n"}
DOCS: dict[str, str | None] = {"README.md": "# changed\n"}
PLUGINS = {
    "pkg/plugins.py": "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n",
    "tests/test_plugins.py": "from pkg.plugins import load\n\n\ndef test_plugins():\n    assert load('pkg.beta').beta() == 2\n",
}
LIMITS = {
    "pkg/limits.py": (
        "from pathlib import Path\n\n\ndef limit():\n"
        "    return int((Path(__file__).parent.parent / 'data' / 'limit.txt').read_text())\n"
    ),
    "tests/test_limits.py": "from pkg.limits import limit\n\n\ndef test_limit():\n    assert limit() == 5\n",
    "data/limit.txt": "5\n",
}


class _Executing(_Runner):
    """Runs the selected test files for real on the Snapshot side's tree (the oracle)."""

    def __init__(self, repo_root: Path, scratch: Path) -> None:
        super().__init__(self._execute)
        self.repo_root = repo_root
        self.scratch = scratch
        self._side_sha = ""

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> Any:
        revision = context.revision
        self._side_sha = str(revision.base_sha if side == "old" else revision.head_sha)
        return super().run(context, runtime, surface, side=side)

    def _materialize(self, into: Path) -> None:
        repo = pygit2.Repository(str(self.repo_root))

        def walk(tree: Any, prefix: str) -> None:
            for entry in tree:
                path = into / prefix / entry.name
                if entry.type_str == "tree":
                    walk(repo.get(entry.id), f"{prefix}{entry.name}/")
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    blob = repo.get(entry.id)
                    assert isinstance(blob, pygit2.Blob)
                    path.write_bytes(blob.data)

        commit = repo.get(self._side_sha)
        assert isinstance(commit, pygit2.Commit)
        walk(commit.tree, "")

    def _execute(self, _runtime: str, side: str, _count: int, command: tuple[str, ...]) -> dict[str, str]:
        from lemoncrow.pro.capabilities.review.runner_host import command_view

        work = self.scratch / f"run-{len(self.calls)}-{side}"
        self._materialize(work)
        junit = work / "junit.xml"
        targets = [arg for arg in command if arg.startswith("tests/")] or ["tests"]
        env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST")}
        # The recipe's own executable, resolved as the sandbox (and the probe) resolves it.
        found = command_view().lookup(command[0]) if command[0] != "python3" else None
        done = subprocess.run(
            [
                found[1] if found is not None else sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--rootdir={work}",
                # Without a cut, conftest discovery scans every parent directory (all of /tmp).
                f"--confcutdir={work}",
                f"--junitxml={junit}",
                *targets,
            ],
            cwd=work,
            env={
                **env,
                "PYTHONPATH": str(work),
                "PYTHONDONTWRITEBYTECODE": "1",
                # Only pytest itself: the environment's plugins are not part of the fixture.
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        cases: dict[str, str] = {}
        if junit.exists():
            for case in ET.parse(junit).iter("testcase"):
                failed = case.find("failure") is not None or case.find("error") is not None
                stem = str(case.get("classname") or "").split(".")[-1]
                cases[f"{stem}::{case.get('name')}"] = "failed" if failed else "passed"
        if done.returncode not in {0, 1} or not cases:
            # pytest itself failed (nothing collected, usage or internal error): never a pass.
            cases[f"pytest::exit_{done.returncode}"] = "failed"
        return cases


@dataclass(frozen=True)
class Case:
    id: str
    #: Snapshot 1's head change over the base, then Snapshot 2's further change.
    first: dict[str, str | None]
    second: dict[str, str | None]
    #: Whether carrying Snapshot 1's run to Snapshot 2 is correct.
    carry: bool
    #: The refusal reason when it is not (the head attempt's validity code).
    refusal: str = ""
    #: Snapshot 2's real outcome differs from Snapshot 1's, so an unsafe carry would show.
    flips: bool = False
    base: dict[str, str] = field(default_factory=dict)
    config: str = CONFIG
    #: Before Snapshot 1 is created, and between Snapshot 1's run and Snapshot 2.
    before: Hook | None = None
    between: Hook | None = None
    #: Where the run plan places Snapshot 2's ``checks.test`` when it does not run it.
    not_planned: str = ""


def _new_image(world: _World, _monkeypatch: pytest.MonkeyPatch) -> None:
    world.runner.image = "sha256:image-two"


PYRUN_CONFIG = CONFIG.replace("command: python3 run_tests.py", "command: pyrun run_tests.py")


def _pyrun(version: str, extra: str = "") -> str:
    """A recipe executable: a Python launcher that reports its version."""

    return (
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "pyrun {version}"; exit 0; fi\n'
        f'exec "{sys.executable}" "$@"{extra}\n'
    )


def _install_pyrun(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put ``pyrun`` 1.0 on the sandbox's host-runtime PATH (a probed, read-only mount)."""

    from lemoncrow.pro.environment import runners as runtimes

    tools = world.root.parent / "tools"
    tools.mkdir()
    tool = tools / "pyrun"
    tool.write_text(_pyrun("1.0"), encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setattr(runtimes, "_COMMAND_OPTIONAL_MOUNTS", (*runtimes._COMMAND_OPTIONAL_MOUNTS, str(tools)))
    monkeypatch.setattr(runtimes, "_COMMAND_PATH", f"{tools}:{runtimes._COMMAND_PATH}")


def _upgrade_pyrun(world: _World, _monkeypatch: pytest.MonkeyPatch) -> None:
    """Upgrade ``pyrun`` in place under the running server: 2.0 collects no ``test_*`` function."""

    tools = world.root.parent / "tools"
    staged = tools / ".pyrun.new"
    staged.write_text(_pyrun("2.0", ' -o "python_functions=check_*"'), encoding="utf-8")
    staged.chmod(0o755)
    os.replace(staged, tools / "pyrun")


def _new_recipe(_world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    original = RunPlanService.recipe_catalog

    def edited(self: RunPlanService, revision: Any) -> Any:
        catalog = original(self, revision)
        return replace(
            catalog, recipes=tuple(replace(row, command=(*row.command, "--edited")) for row in catalog.recipes)
        )

    monkeypatch.setattr(RunPlanService, "recipe_catalog", edited)


def _new_policy(_world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import runtime_validation

    original = runtime_validation.trusted_policy_digest

    def updated(content: str | bytes | None, *, base_known: bool = True) -> str:
        # The trusted policy changed under the Review (an operator or base update).
        text = content.decode("utf-8") if isinstance(content, bytes) else content or ""
        return original(text + "\n# policy updated\n", base_known=base_known)

    monkeypatch.setattr(runtime_validation, "trusted_policy_digest", updated)


def _truncated_map(_world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    original = RunPlanService._impact_map

    def truncated(self: RunPlanService, revision: Any) -> tuple[Any, str]:
        impact, unavailable = original(self, revision)
        return (None if impact is None else replace(impact, truncated="graph bound reached")), unavailable

    monkeypatch.setattr(RunPlanService, "_impact_map", truncated)


CORPUS: tuple[Case, ...] = (
    # Safe: what the selected tests load is unchanged.
    Case("unrelated-docs", TOUCH_ALPHA, DOCS, carry=True),
    Case("new-unreferenced-module", TOUCH_ALPHA, {"pkg/gamma.py": "GAMMA = 1\n"}, carry=True),
    # Unsafe: the change reaches what the selected tests load.
    Case(
        "direct-dependency",
        TOUCH_ALPHA,
        {"pkg/alpha.py": ALPHA.replace("return one()", "return one() + 1")},
        carry=False,
        refusal="carry_inputs_changed",
        flips=True,
    ),
    Case(
        "indirect-dependency",
        TOUCH_ALPHA,
        {"pkg/helper.py": "def one():\n    return 2\n"},
        carry=False,
        refusal="carry_inputs_changed",
        flips=True,
    ),
    Case(
        "deleted-dependency",
        TOUCH_ALPHA,
        {"pkg/helper.py": None},
        carry=False,
        refusal="carry_inputs_changed",
        flips=True,
    ),
    Case(
        "conftest",
        TOUCH_ALPHA,
        {
            "tests/conftest.py": (
                "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef broken():\n    raise RuntimeError('setup')\n"
            )
        },
        carry=False,
        # A conftest reaches every test under it, so the selection widens.
        refusal="carry_scope_changed",
        flips=True,
    ),
    Case("lockfile", TOUCH_ALPHA, {"uv.lock": "version = 1\n"}, carry=False, refusal="carry_scope_changed"),
    Case(
        "manifest",
        TOUCH_ALPHA,
        {"requirements.txt": "pytest\nrequests\n"},
        carry=False,
        refusal="carry_scope_changed",
    ),
    Case(
        "runner-config",
        TOUCH_ALPHA,
        {"pytest.ini": "[pytest]\npython_functions = check_*\n"},
        carry=False,
        refusal="carry_scope_changed",
        flips=True,
    ),
    Case(
        "runtime-read",
        TOUCH_ALPHA,
        {"data/limit.txt": "6\n"},
        carry=False,
        refusal="carry_scope_changed",
        flips=True,
        base=LIMITS,
    ),
    Case(
        "dynamic-import",
        {"pkg/beta.py": "def beta():\n    return 2  # touched\n"},
        {"pkg/beta.py": "def beta():\n    return 3\n"},
        carry=False,
        refusal="carry_inputs_changed",
        flips=True,
        base=PLUGINS,
    ),
    # Fail-closed: a test reaching a file whose imports are unknowable counts every change.
    Case(
        "dynamic-import-then-docs",
        {"pkg/beta.py": "def beta():\n    return 2  # touched\n"},
        DOCS,
        carry=False,
        refusal="carry_inputs_changed",
        base=PLUGINS,
    ),
    # Fail-closed: when the graph cannot tell, the whole head tree is the input.
    Case(
        "unsupported-language",
        TOUCH_ALPHA,
        DOCS,
        carry=False,
        refusal="carry_inputs_changed",
        base={"tests/calc_test.go": "package calc\n"},
    ),
    Case("truncated-map", TOUCH_ALPHA, DOCS, carry=False, refusal="carry_inputs_changed", before=_truncated_map),
    # The run's identity changed even though no input did.
    Case(
        "environment-toolchain",
        TOUCH_ALPHA,
        DOCS,
        carry=False,
        refusal="carry_environment_changed",
        between=_new_image,
    ),
    # The executable the recipe runs, probed where the sandbox resolves it: unchanged carries,
    # upgraded under the running server (another version and other bytes) runs again.
    Case("environment-executable-unchanged", TOUCH_ALPHA, DOCS, carry=True, config=PYRUN_CONFIG, before=_install_pyrun),
    Case(
        "environment-executable-upgraded",
        TOUCH_ALPHA,
        DOCS,
        carry=False,
        refusal="carry_environment_changed",
        flips=True,
        config=PYRUN_CONFIG,
        before=_install_pyrun,
        between=_upgrade_pyrun,
    ),
    Case("recipe", TOUCH_ALPHA, DOCS, carry=False, refusal="carry_recipe_changed", between=_new_recipe),
    Case(
        "recipe-input",
        TOUCH_ALPHA,
        {"run_tests.py": "import sys  # changed\n"},
        carry=False,
        refusal="carry_scope_changed",
    ),
    Case("policy", TOUCH_ALPHA, DOCS, carry=False, refusal="carry_policy_changed", between=_new_policy),
    # The tests themselves changed.
    Case(
        "test-renamed",
        TOUCH_ALPHA,
        {"tests/test_alpha.py": None, "tests/test_alpha_renamed.py": TEST_ALPHA},
        carry=False,
        refusal="carry_scope_changed",
    ),
    Case(
        "test-removed",
        TOUCH_ALPHA,
        {"tests/test_alpha.py": None},
        carry=False,
        not_planned="skip_not_affected",
    ),
    Case(
        "weakened-assertion",
        {"pkg/alpha.py": ALPHA.replace("return one()", "return one() + 1")},
        {"tests/test_alpha.py": TEST_ALPHA.replace("== 1", "in (1, 2)")},
        carry=False,
        refusal="carry_inputs_changed",
        flips=True,
    ),
)


def _summary(world: _World, review: Any) -> dict[str, list[dict[str, Any]]]:
    shown = world.plan(review)["reverification"]
    return {bucket: [row for row in rows if row["entry_id"] == "checks.test"] for bucket, rows in shown.items()}


def _verdicts(world: _World, review: Any) -> dict[str, str]:
    """The Snapshot's current check verdicts."""

    return {row["stable_key"]: row["status"] for row in world.recipes(review)["verdicts"]}


@pytest.mark.parametrize("case", CORPUS, ids=[case.id for case in CORPUS])
def test_no_unsafe_carry(case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _Executing(tmp_path / "repo", tmp_path / "runs")
    world = _World(tmp_path, config=case.config, runner=runner, base={**BASE, **case.base})
    if case.before is not None:
        case.before(world, monkeypatch)
    review = world.review(case.first)
    first = world.run(review)
    assert first.status_code == 200, first.text
    assert first.json()["executions"] >= 1
    before = first.json()["verdicts"]
    if case.between is not None:
        case.between(world, monkeypatch)
    world.push(review, case.second)
    plain = world.run(review)
    if case.not_planned:
        # Nothing runs on Snapshot 2, so nothing is carried or shown as current there.
        assert plain.status_code == 409, plain.text
        assert [row["entry_id"] for row in _summary(world, review)[case.not_planned]] == ["checks.test"]
        assert "checks.test" not in _verdicts(world, review)
        return
    assert plain.status_code == 200, plain.text
    body = plain.json()
    carried = body["carried"] > 0
    fresh = world.run(review, fresh=True)
    assert fresh.status_code == 200, fresh.text
    # The oracle: a carried result must equal a real run of this Snapshot.
    if carried:
        assert body["verdicts"] == fresh.json()["verdicts"], f"unsafe carry in {case.id}"
    assert carried is case.carry, body["validity"]
    [head] = [row for row in body["validity"] if row["subject_ref"] == "check:unit#1"]
    assert head["reason_code"] == ("carry_exact_key" if case.carry else case.refusal), head
    if case.flips:
        assert fresh.json()["verdicts"] != before, "the case must flip the real outcome"
    else:
        assert fresh.json()["verdicts"] == before


def test_the_summary_tells_the_same_story_as_the_run(tmp_path: Path) -> None:
    """Items 2 and 9 over the corpus: a carried run shows as reused, a refused one with its reason."""

    for case in (CORPUS[0], CORPUS[2]):
        runner = _Executing(tmp_path / case.id / "repo", tmp_path / case.id / "runs")
        world = _World(tmp_path / case.id, runner=runner, base=BASE)
        review = world.review(case.first)
        assert world.run(review).status_code == 200
        world.push(review, case.second)
        assert world.run(review).status_code == 200
        summary = _summary(world, review)
        if case.carry:
            [reused] = summary["reuse"]
            assert (reused["kind"], reused["origin_snapshot"], reused["reason_code"]) == (
                "carried",
                1,
                "carry_exact_key",
            )
        else:
            [ran] = summary["run"]
            assert (ran["state"], ran["reason_code"]) == ("ran", case.refusal)


def test_budget_exhaustion_leaves_the_check_unknown_never_skipped_or_carried(tmp_path: Path) -> None:
    """Item 12 and rule 11.1: budget exhausted is not run and unknown, never skipped or current."""

    config = CONFIG + "review:\n  budget:\n    per_review_minutes: 12\n"
    runner = _Executing(tmp_path / "repo", tmp_path / "runs")
    world = _World(tmp_path, config=config, runner=runner, base=BASE)
    review = world.review(TOUCH_ALPHA)
    assert world.entry(review, "checks.test")["status"] == "run"
    assert world.run(review).status_code == 200
    # Spend the rest of the Review's minutes with fresh runs until the ceiling refuses one.
    for _ in range(20):
        if world.run(review, fresh=True).status_code == 409:
            break
    else:
        pytest.fail("the per_review_minutes ceiling never refused a fresh run")
    world.push(review, DOCS)
    entry = world.entry(review, "checks.test")
    assert entry["status"] == "limited_by_budget", entry
    [unknown] = _summary(world, review)["unknown_not_run"]
    assert unknown["status"] == "limited_by_budget"
    assert world.run(review).status_code == 409
    assert "checks.test" not in _verdicts(world, review)
