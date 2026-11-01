"""Test-impact selection and head-first planning (Phase 9R R6, items 19 and 20)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import test_impact as ti
from lemoncrow.pro.capabilities.review.capability_catalog import DETECTION_FACTS, DetectedCapabilities
from lemoncrow.pro.capabilities.review.recipes import MAX_SELECTED_TEST_ARGS, CheckRecipe
from lemoncrow.pro.capabilities.review.run_plan import (
    PlanInputs,
    RunPlan,
    SettingOrigin,
    build_run_plan,
    parse_run_settings,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.test_impact import (
    EntryImpact,
    HeadChange,
    ImpactMap,
    build_impact_map,
    select_recipe_tests,
)

BASE = "b" * 40
RECIPE = CheckRecipe(id="unit", kind="test", command=("pytest", "{tests}"))
WHOLE = CheckRecipe(id="whole", kind="test", command=("pytest",))

FILES: dict[str, str] = {
    "src/pkg/__init__.py": "",
    "src/pkg/a.py": "def a():\n    return 1\n",
    "src/pkg/b.py": "def b():\n    return 2\n",
    "src/pkg/shared.py": "X = 1\n",
    "src/pkg/uses_shared.py": "from .shared import X\n",
    "tests/conftest.py": "",
    "tests/test_a.py": "from pkg.a import a\n\ndef test_a():\n    assert a() == 1\n",
    "tests/test_b.py": "from pkg import b\n\ndef test_b():\n    assert b.b() == 2\n",
    "tests/test_shared.py": "import pkg.uses_shared\n",
    "tests/test_docs.py": "from pathlib import Path\n\ndef test_readme():\n    Path('USAGE.md').read_text()\n",
    "README.md": "# readme\n",
    "USAGE.md": "usage\n",
    "pyproject.toml": "[project]\nname = 'pkg'\n",
    "web/util.ts": "export const one = 1\n",
    "web/util.test.ts": "import { one } from './util'\n",
    "web/other.test.ts": "import x from 'react'\n",
}


def _map(files: dict[str, str] | None = None, **bounds: Any) -> ImpactMap:
    source = FILES if files is None else files
    return build_impact_map(BASE, list(source), source.get, **bounds)


def _select(changes: dict[str, str | None], impact: ImpactMap | None = None, **kwargs: Any) -> ti.RecipeSelection:
    rows = [HeadChange.of(path, text, deleted=text is None) for path, text in changes.items()]
    return select_recipe_tests(
        _map() if impact is None else impact, kwargs.pop("recipe", RECIPE), rows, base_sha=BASE, **kwargs
    )


# --------------------------------------------------------------------------- #
# selection (item 19)
# --------------------------------------------------------------------------- #


def test_a_change_to_module_a_runs_a_tests_and_not_b_tests() -> None:
    chosen = _select({"src/pkg/a.py": "def a():\n    return 3\n"})
    assert chosen.mode == "impact" and chosen.tests == ("tests/test_a.py",)
    assert chosen.total == 6 and chosen.reason == "test impact: 1 of 6 test files reach a changed file"
    assert RECIPE.argv(chosen.cwd_relative(RECIPE.cwd)) == ("pytest", "tests/test_a.py")
    other = _select({"src/pkg/b.py": "def b():\n    return 4\n"})
    assert other.tests == ("tests/test_b.py",)
    # Relative imports, package re-exports and JS relative specifiers are followed too.
    assert _select({"src/pkg/shared.py": "X = 2\n"}).tests == ("tests/test_shared.py",)
    assert _select({"web/util.ts": "export const one = 2\n"}).tests == ("web/util.test.ts",)
    # A changed test file selects itself.
    assert _select({"tests/test_b.py": "from pkg import b\n"}).tests == ("tests/test_b.py",)


def test_without_a_current_map_the_recipe_runs_in_full_with_the_reason_never_nothing() -> None:
    change = {"src/pkg/a.py": "def a():\n    return 3\n"}
    rows = [HeadChange.of(path, text) for path, text in change.items()]
    missing = select_recipe_tests(
        None, RECIPE, rows, base_sha=BASE, unavailable="the trusted base tree is not readable"
    )
    assert missing.mode == "all" and missing.tests == ()
    assert (
        missing.reason == "full suite: no test-impact map for base bbbbbbbbbbbb (the trusted base tree is not readable)"
    )
    stale = select_recipe_tests(_map(), RECIPE, rows, base_sha="c" * 40)
    assert stale.mode == "all" and "the test-impact map is stale (built for base bbbbbbbbbbbb" in stale.reason
    old_builder = select_recipe_tests(replace(_map(), version=0), RECIPE, rows, base_sha=BASE)
    assert old_builder.mode == "all" and "builder v0" in old_builder.reason
    truncated = select_recipe_tests(_map(max_files=2), RECIPE, rows, base_sha=BASE)
    assert truncated.mode == "all" and "test-impact map incomplete" in truncated.reason
    incomplete = select_recipe_tests(_map(), RECIPE, rows, base_sha=BASE, complete=False)
    assert incomplete.mode == "all" and incomplete.input_digest == ""
    # Even documentation-only changes never skip without a usable map.
    docs = [HeadChange.of("README.md", "x")]
    assert {select_recipe_tests(None, RECIPE, docs, base_sha=BASE).mode, stale.mode} == {"all"}


def test_inputs_the_graph_cannot_see_run_everything() -> None:
    assert _select({"pyproject.toml": "[project]\n"}).reason == (
        "full suite: pyproject.toml is a dependency or test-runner configuration input"
    )
    assert "outside the import graph" in _select({"data/fixture.json": "{}"}).reason
    with_input = replace(RECIPE, inputs=("tools/run.sh",))
    assert (
        _select({"tools/run.sh": "x"}, recipe=with_input).reason
        == "full suite: tools/run.sh is an input of this recipe"
    )
    # A file whose imports are unknowable may load any changed module: tests reaching it run.
    dynamic = dict(FILES, **{"src/pkg/plugins.py": "import importlib\nimportlib.import_module(name)\n"})
    dynamic["tests/test_plugins.py"] = "import pkg.plugins\n"
    chosen = _select({"src/pkg/b.py": "def b():\n    return 5\n"}, impact=_map(dynamic))
    assert chosen.tests == ("tests/test_b.py", "tests/test_plugins.py")
    # An added module that may satisfy an import the base could not resolve.
    unresolved = dict(FILES, **{"tests/test_new.py": "import pkg.missing\n"})
    assert "may satisfy an import" in _select({"src/pkg/missing.py": ""}, impact=_map(unresolved)).reason
    # Tests in a language the graph does not parse.
    go = dict(FILES, **{"tests/calc_test.go": "package calc\n"})
    assert _select({"src/pkg/a.py": ""}, impact=_map(go)).reason == (
        "full suite: tests in .go are outside the import graph"
    )


def test_a_full_suite_the_graph_cannot_justify_is_unknown_coverage_not_an_ordinary_full_run() -> None:
    """Trust T3 item 8: absent, stale or truncated maps and unknowable reads say ``unknown``."""

    change = [HeadChange.of("src/pkg/a.py", "def a():\n    return 3\n")]
    past = {f"src/pkg/gen_{index}.py": "" for index in range(ti.MAX_CHANGED + 1)}
    unresolved = dict(FILES, **{"tests/test_new.py": "import pkg.missing\n"})
    go = dict(FILES, **{"tests/calc_test.go": "package calc\n"})
    unknown = {
        "no map": select_recipe_tests(None, RECIPE, change, base_sha=BASE),
        "stale map": select_recipe_tests(_map(), RECIPE, change, base_sha="c" * 40),
        "old builder": select_recipe_tests(replace(_map(), version=0), RECIPE, change, base_sha=BASE),
        "truncated map": select_recipe_tests(_map(max_files=2), RECIPE, change, base_sha=BASE),
        "incomplete change list": select_recipe_tests(_map(), RECIPE, change, base_sha=BASE, complete=False),
        "too many changes": _select(past),
        "runtime read": _select({"data/fixture.json": "{}"}),
        "doctest": _select({"README.md": "x"}, impact=replace(_map(), doc_tests=True)),
        "shadowing import": _select({"src/pkg/missing.py": ""}, impact=_map(unresolved)),
        "foreign tests": _select({"src/pkg/a.py": ""}, impact=_map(go)),
        "no recognized tests": _select({"src/lib.py": "V = 2\n"}, impact=_map({"src/lib.py": "V = 1\n"})),
    }
    known = {
        "recipe input": _select({"tools/run.sh": "x"}, recipe=replace(RECIPE, inputs=("tools/run.sh",))),
        "dependency config": _select({"pyproject.toml": "[project]\n"}),
        "not a test recipe": _select({"src/pkg/a.py": ""}, recipe=replace(RECIPE, kind="lint")),
        "no {tests} argument": _select({"src/pkg/a.py": ""}, recipe=WHOLE),
        "every test reaches": _select(
            {"src/lib.py": "V = 2\n"},
            impact=_map(
                {"src/lib.py": "V = 1\n", "tests/test_one.py": "import lib\n", "tests/test_two.py": "import lib\n"}
            ),
        ),
        "forced": _select({"src/pkg/a.py": ""}).as_full("full suite: Deep runs every test"),
    }
    for name, chosen in unknown.items():
        assert (chosen.mode, chosen.coverage) == ("all", "unknown"), (name, chosen.reason)
        assert chosen.to_wire()["coverage"] == "unknown"
    for name, chosen in known.items():
        assert (chosen.mode, chosen.coverage) == ("all", "known"), (name, chosen.reason)
        assert "coverage" not in chosen.to_wire()
    # Display only: coverage round-trips but never enters the digested execution document.
    flipped = replace(known["dependency config"], coverage="unknown")
    assert ti.RecipeSelection.from_wire("unit", flipped.to_wire()).coverage == "unknown"
    assert ti.RecipeSelection.from_wire("unit", known["dependency config"].to_wire()).coverage == "known"
    assert flipped.execution_document() == known["dependency config"].execution_document()


def test_conftest_init_deletion_and_docs_follow_what_tests_load() -> None:
    python_tests = ("tests/test_a.py", "tests/test_b.py", "tests/test_docs.py", "tests/test_shared.py")
    assert _select({"tests/conftest.py": "import os\n"}).tests == python_tests
    package = ("tests/test_a.py", "tests/test_b.py", "tests/test_shared.py")
    assert _select({"src/pkg/__init__.py": "Y = 1\n"}).tests == package
    deleted = _select({"src/pkg/a.py": None})
    assert deleted.tests == ("tests/test_a.py",)
    unread = _select({"README.md": "# changed\n"})
    assert unread.mode == "none" and unread.reason.startswith("test impact: only documentation no test reads")
    assert _select({"USAGE.md": "new usage\n"}).tests == ("tests/test_docs.py",)
    doctests = dict(FILES, **{"pytest.ini": "[pytest]\naddopts = --doctest-glob=*.md\n"})
    assert "may be collected as a doctest" in _select({"README.md": "x"}, impact=_map(doctests)).reason
    # A change no test reaches is a positive finding: nothing of this recipe runs.
    nothing = _select({"src/pkg/new_module.py": "Z = 1\n"})
    assert nothing.mode == "none" and "no test under the repository reaches a changed file" in nothing.reason


def test_new_nested_conftest_reaches_only_tests_in_its_pytest_scope() -> None:
    """A new fixture provider must not silently yield a 'no affected tests' selection."""
    files = dict(FILES)
    files["tests/unit/test_unit.py"] = "from pkg.a import a\n"
    files["tests/other/test_other.py"] = "from pkg.b import b\n"
    chosen = _select({"tests/unit/conftest.py": "import pytest\n"}, impact=_map(files))
    assert chosen.mode == "impact"
    assert chosen.tests == ("tests/unit/test_unit.py",)
    assert chosen.input_digest
    assert (
        chosen.input_digest
        != _select({"tests/unit/conftest.py": "import pytest\n# fixture changed\n"}, impact=_map(files)).input_digest
    )


def test_new_package_initializer_reaches_importers_of_existing_namespace_modules() -> None:
    """New __init__.py executes even when the previously-imported .py file has not changed."""
    files = {path: content for path, content in FILES.items() if path != "src/pkg/__init__.py"}
    chosen = _select({"src/pkg/__init__.py": "SIDE_EFFECT = True\n"}, impact=_map(files))
    assert chosen.mode == "impact"
    assert chosen.tests == ("tests/test_a.py", "tests/test_b.py", "tests/test_shared.py")
    assert chosen.input_digest
    assert (
        chosen.input_digest
        != _select({"src/pkg/__init__.py": "SIDE_EFFECT = False\n"}, impact=_map(files)).input_digest
    )


@pytest.mark.parametrize("language", ["python", "js"])
def test_documentation_references_past_the_bound_count_as_reading_every_document(language: str) -> None:
    """RC12-B item 5: a cut doc-reference list is coverage unknown, never "reads none of these"."""

    def test_file(count: int) -> tuple[str, str]:
        names = [f"doc{index:03d}.md" for index in range(count)]
        if language == "python":
            return "tests/test_many_docs.py", "".join(f"P{index} = '{name}'\n" for index, name in enumerate(names))
        return "web/many_docs.test.ts", "".join(f"const p{index} = '{name}'\n" for index, name in enumerate(names))

    def select(count: int, changed: str) -> ti.RecipeSelection:
        path, text = test_file(count)
        return _select({changed: "changed\n"}, impact=_map(dict(FILES, **{path: text})))

    bound = ti._MAX_DOC_REFS
    path, _text = test_file(bound)
    # At the bound every name is recorded: a named doc selects the file, an unnamed one nothing.
    assert select(bound, "doc000.md").tests == (path,)
    assert select(bound, "zzz.md").mode == "none"
    # Past it the list is not cut silently: which documents the file reads is unknown,
    # so any changed document selects it -- including one past the old cut.
    assert select(bound + 1, f"doc{bound:03d}.md").tests == (path,)
    assert select(bound + 1, "zzz.md").tests == (path,)
    impact = _map(dict(FILES, **{test_file(bound + 1)[0]: test_file(bound + 1)[1]}))
    assert impact.doc_refs[path] == (ti.DOC_REFS_UNKNOWN,)
    # The changed document is part of that test's run inputs (no carry over a doc change).
    first = select(bound + 1, "zzz.md")
    second = _select({"zzz.md": "other\n"}, impact=impact)
    assert first.input_digest and first.input_digest != second.input_digest


def test_bounds_fall_back_to_running_more_at_and_past_each_bound() -> None:
    # Changed paths: exactly MAX_CHANGED still selects; one more runs the full suite.
    at = {f"src/pkg/gen_{index}.py": "" for index in range(ti.MAX_CHANGED - 1)}
    at["src/pkg/a.py"] = "def a():\n    return 9\n"
    assert len(at) == ti.MAX_CHANGED and _select(at).mode == "impact"
    past = dict(at, **{"src/pkg/b.py": ""})
    assert _select(past).reason == f"full suite: more than {ti.MAX_CHANGED} changed paths"
    # Graph size: at the cap the map is usable; past it the map is truncated (full suite).
    sources = [path for path in FILES if ti.is_source(path)]
    assert _map(max_files=len(sources)).usable
    assert not _map(max_files=len(sources) - 1).usable
    # Per-file size: at the cap a file is parsed; past it, it counts as unknowable.
    a_size = len(FILES["src/pkg/a.py"].encode())
    assert "src/pkg/a.py" not in _map(max_file_bytes=a_size).dynamic
    assert "src/pkg/a.py" in _map(max_file_bytes=a_size - 1).dynamic
    total = sum(len(FILES[path].encode()) for path in sources)
    assert _map(max_total_bytes=total).usable and not _map(max_total_bytes=total - 1).usable
    # {tests} arguments: up to MAX_SELECTED_TEST_ARGS are passed; more run the whole suite.
    many = {"src/lib.py": "V = 1\n"}
    for index in range(MAX_SELECTED_TEST_ARGS + 1):
        many[f"tests/test_gen_{index:04d}.py"] = "import lib\n" if index < MAX_SELECTED_TEST_ARGS else ""
    impact = _map(many)
    rows = [HeadChange.of("src/lib.py", "V = 2\n")]
    selected = select_recipe_tests(impact, RECIPE, rows, base_sha=BASE)
    assert selected.mode == "impact" and len(selected.tests) == MAX_SELECTED_TEST_ARGS
    many[f"tests/test_gen_{MAX_SELECTED_TEST_ARGS:04d}.py"] = "import lib\n"
    many["tests/test_unrelated.py"] = ""
    over = select_recipe_tests(_map(many), RECIPE, rows, base_sha=BASE)
    assert over.mode == "all" and f"more than {MAX_SELECTED_TEST_ARGS} files" in over.reason
    assert len(RECIPE.argv(tuple(f"t{i}" for i in range(MAX_SELECTED_TEST_ARGS)))) == MAX_SELECTED_TEST_ARGS + 1
    assert RECIPE.argv(tuple(f"t{i}" for i in range(MAX_SELECTED_TEST_ARGS + 1))) == ("pytest",)
    # A recipe without {tests} cannot be narrowed: it runs whole, with the reason.
    whole = _select({"src/pkg/a.py": ""}, recipe=WHOLE)
    assert whole.mode == "all" and "takes no {tests} argument" in whole.reason


def test_input_digest_covers_only_what_the_selected_tests_load() -> None:
    first = _select({"src/pkg/a.py": "def a():\n    return 3\n", "src/pkg/b.py": "one\n"}, recipe=WHOLE)
    second = _select({"src/pkg/a.py": "def a():\n    return 3\n", "src/pkg/b.py": "two\n"}, recipe=WHOLE)
    # A full run depends on every changed module the suite imports.
    assert first.input_digest and first.input_digest != second.input_digest
    narrow_one = _select({"src/pkg/a.py": "def a():\n    return 3\n", "src/pkg/new.py": "one\n"})
    narrow_two = _select({"src/pkg/a.py": "def a():\n    return 3\n", "src/pkg/new.py": "two\n"})
    assert narrow_one.tests == narrow_two.tests == ("tests/test_a.py",)
    assert narrow_one.input_digest == narrow_two.input_digest != ""
    changed = _select({"src/pkg/a.py": "def a():\n    return 4\n", "src/pkg/new.py": "one\n"})
    assert changed.input_digest != narrow_one.input_digest
    unreadable = select_recipe_tests(_map(), RECIPE, [HeadChange.of("src/pkg/a.py", None)], base_sha=BASE)
    assert unreadable.input_digest == ""


def test_the_map_round_trips_and_aliases_are_unknowable() -> None:
    impact = _map(dict(FILES, **{"web/alias.ts": "import x from '@/lib/x'\n"}))
    assert ImpactMap.from_wire(impact.to_wire()) == impact and impact.digest.startswith("impact:sha256:")
    assert "web/alias.ts" in impact.dynamic
    assert impact.edges["tests/test_a.py"] == ("src/pkg/a.py", "src/pkg/__init__.py", "tests/conftest.py")
    with pytest.raises(ValueError):
        ImpactMap.from_wire({**impact.to_wire(), "edges": [[99], *impact.to_wire()["edges"][1:]]})
    assert build_impact_map(BASE, None, FILES.get).truncated == "the trusted base tree is not readable"


# --------------------------------------------------------------------------- #
# the planner: selection never narrows required work; head first (item 20)
# --------------------------------------------------------------------------- #

REPO = SettingOrigin("trusted_base_repository", "base Snapshot .lemoncrow/review.yaml")


def _plan(repo: dict[str, Any] | None, impact: dict[str, EntryImpact], **kwargs: Any) -> RunPlan:
    settings = resolve_run_settings([parse_run_settings(repo, origin=REPO)] if repo is not None else [])
    return build_run_plan(
        PlanInputs(
            review_id="review-1",
            revision_id="revision-1",
            trigger="open",
            settings=settings,
            detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS)),
            changed_paths=("src/pkg/a.py",),
            impact=impact,
            **kwargs,
        )
    )


def _impact(entry_id: str, mode: str) -> dict[str, EntryImpact]:
    selection = ti.RecipeSelection(
        "unit",
        mode,  # type: ignore[arg-type]
        f"reason {mode}",
        ("tests/test_a.py",) if mode == "impact" else (),
        total=6,
        input_digest="inputs:sha256:narrow" if mode != "none" else "",
        full_input_digest="inputs:sha256:full",
    )
    return {entry_id: EntryImpact(entry_id, (selection,), "impact:sha256:map")}


def test_the_plan_shows_the_selection_and_a_required_check_always_runs_in_full() -> None:
    narrowed = _plan(None, _impact("checks.test", "impact")).entry("checks.test")
    assert narrowed is not None and narrowed.status == "run"
    assert narrowed.selection is not None and narrowed.selection["recipes"]["unit"]["tests"] == ["tests/test_a.py"]
    assert "reason impact" in narrowed.reason
    skipped = _plan(None, _impact("checks.test", "none")).entry("checks.test")
    assert skipped is not None and skipped.status == "skipped" and skipped.reason == "reason none"
    # Policy-required: never narrowed or skipped by selection.
    required = _plan({"required": ["checks.test"]}, _impact("checks.test", "none")).entry("checks.test")
    assert required is not None and required.required and required.status == "run"
    assert required.selection is not None
    row = required.selection["recipes"]["unit"]
    assert (row["mode"], row["input"]) == ("all", "inputs:sha256:full") and "tests" not in row
    assert "selection never narrows policy-required work" in required.reason
    # Deep runs the full suite.
    deep = _plan({"effort": "deep"}, _impact("checks.test", "impact")).entry("checks.test")
    assert deep is not None and deep.selection is not None and deep.selection["recipes"]["unit"]["mode"] == "all"
    # Selection is execution identity; its reason text is not.
    a = _plan(None, _impact("checks.test", "impact"))
    other = _impact("checks.test", "impact")
    relabeled = {
        key: EntryImpact(key, tuple(replace(r, reason="x") for r in v.recipes), v.map_digest)
        for key, v in other.items()
    }
    assert _plan(None, relabeled).digest == a.digest
    assert _plan(None, {}).digest != a.digest


def test_auto_runs_the_head_first_and_the_base_only_on_demand() -> None:
    auto = _plan(None, {}).entry("checks.test")
    assert auto is not None and auto.sides == "head" and auto.base_on_demand
    assert "head first: the base runs only if the head fails" in auto.reason
    quick = _plan({"effort": "quick"}, {}).entry("checks.test")
    assert quick is not None and quick.sides == "head" and not quick.base_on_demand
    thorough = _plan({"effort": "thorough"}, {}).entry("checks.test")
    assert thorough is not None and thorough.sides == "base+head" and not thorough.base_on_demand
    # Escalated Auto runs the behavior diff, which needs the base: planned base+head.
    risky = _plan(None, {}, risk_signal=True).entry("checks.test")
    assert risky is not None and risky.sides == "base+head" and not risky.base_on_demand
    # On-demand halves the planned estimate of the entry.
    assert auto.estimated_minutes < risky.estimated_minutes
