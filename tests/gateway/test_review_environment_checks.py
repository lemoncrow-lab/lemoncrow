"""Review takes its checks from the trusted base's ``.lemoncrow/environment.toml`` (plan M2, first slice)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import recipes as recipes_module
from lemoncrow.pro.capabilities.review.recipes import (
    CHANGED_PLACEHOLDER,
    ENVIRONMENT_FILE,
    MAX_CHANGED_BYTES,
    CheckRecipe,
    build_recipe_catalog,
    changed_listing,
    changed_listing_refusal,
    environment_recipes,
    touched_checks,
)
from lemoncrow.pro.environment.runners import RunnerRegistry
from tests.gateway.test_review_check_runs_api import (
    CALC_BASE,
    _base_files,
    _get,
    _plan_entry,
    _review,
    _run,
    _Scripted,
)

ENVIRONMENT = """\
version = 1

[[checks]]
id = "py"
paths = ["*.py", ":!tests"]
run = "python3 -c 'print(1)'"

[[checks]]
id = "docs"
paths = ["docs"]
run = "make docs"

[[checks]]
id = "web"
paths = ["web", ":!web/README.md"]
run = "npm test"
"""
REVIEW_CONFIG = (
    "check_recipes:\n  - id: unit\n    kind: test\n    command: python3 run_tests.py\ncheck_policy:\n  reruns: 3\n"
)


def _reader(files: dict[str, str]) -> Any:
    return lambda path: files.get(path)


def _recipe(paths: tuple[str, ...], recipe_id: str = "c") -> CheckRecipe:
    return CheckRecipe(id=recipe_id, kind="build", command=("true",), paths=paths)


def test_environment_checks_are_the_recipes_and_review_yaml_only_fills_in_without_them() -> None:
    defined, errors = environment_recipes(ENVIRONMENT)
    assert errors == ()
    py, docs, web = defined
    assert py.command[:2] == ("sh", "-c") and py.command[2].endswith("\npython3 -c 'print(1)'")
    assert py.command[3:] == ("sh", CHANGED_PLACEHOLDER) and py.paths == ("*.py", ":!tests")
    # No kind: addressable only as checks.recipe:<id>, never folded into checks.test and the like.
    assert {recipe.kind_entry for recipe in defined} == {""} and {recipe.origin for recipe in defined} == {"config"}
    assert docs.definition == "Makefile" and docs.inputs == (ENVIRONMENT_FILE, "Makefile")
    assert web.definition == "package.json" and py.inputs == (ENVIRONMENT_FILE,)

    files = {
        ENVIRONMENT_FILE: ENVIRONMENT,
        ".lemoncrow/review.yaml": REVIEW_CONFIG,
        "pyproject.toml": "[tool.pytest.ini_options]\n[tool.ruff]\n",
        "tests/test_x.py": "",
    }
    read = _reader(files)
    catalog = build_recipe_catalog(REVIEW_CONFIG, list(files), read, environment=ENVIRONMENT)
    # Neither check_recipes nor discovery adds a recipe; check_policy still applies.
    assert [recipe.id for recipe in catalog.recipes] == ["py", "docs", "web"]
    assert catalog.errors == () and catalog.policy.reruns == 3
    assert dict(catalog.recipes[0].input_digests)[ENVIRONMENT_FILE] is not None

    without = build_recipe_catalog(REVIEW_CONFIG, list(files), read).to_wire()
    assert {row["id"] for row in without["recipes"]} == {"unit", "ruff-lint"}
    for absent in (None, "", "version = 1\n"):
        assert build_recipe_catalog(REVIEW_CONFIG, list(files), read, environment=absent).to_wire() == without


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('version = 1\n[[checks]]\nid = "x"\npaths = ["."]\nrun = "true"\ncolour = "red"\n', "unknown key 'colour'"),
        ("version = 2\n", "version must be 1"),
        ('version = 1\n[[checks]]\nid = "x"\n', "paths must be a non-empty array"),
        (f'version = 1\n[[checks]]\nid = "{"a" * 65}"\npaths = ["."]\nrun = "true"\n', "at most 64 characters"),
        (f'version = 1\n[[checks]]\nid = "x"\npaths = ["."]\nrun = "{"t" * 4000}"\n', "run command of at most"),
        (
            "version = 1\n" + "".join(f'[[checks]]\nid = "c{i}"\npaths = ["."]\nrun = "true"\n' for i in range(65)),
            "Review runs at most 64",
        ),
    ],
)
def test_an_invalid_environment_fails_closed_without_falling_back(text: str, message: str) -> None:
    defined, errors = environment_recipes(text)
    assert defined == () and len(errors) == 1 and message in errors[0]
    files = {".lemoncrow/review.yaml": REVIEW_CONFIG, "tests/test_x.py": ""}
    catalog = build_recipe_catalog(REVIEW_CONFIG, list(files), _reader(files), environment=text)
    assert catalog.recipes == () and message in catalog.errors[0]


def test_paths_are_matched_by_git_as_lc_verify_matches_them() -> None:
    recipes = (
        _recipe(("src", ":!src/gen"), "src"),
        _recipe(("*.md",), "md"),
        _recipe((":!docs",), "not-docs"),
        _recipe(("tests",), "tests"),
    )
    touched, problem = touched_checks(recipes, ["src/gen/x.py", "docs/a/b.md"])
    # `*` crosses directories in a Git pathspec; `:!` excludes; an exclusion alone means "everything else".
    assert problem == "" and touched == {"src": False, "md": True, "not-docs": True, "tests": False}
    touched, _ = touched_checks(recipes, ["src/gen/x.py", "src/a.py", "docs/x.txt"])
    assert touched == {"src": True, "md": False, "not-docs": True, "tests": False}
    assert touched_checks(recipes, []) == ({"src": False, "md": False, "not-docs": False, "tests": False}, "")
    # A file replaced by a directory lists both paths; one index cannot hold both.
    file_only = _recipe(("tool", ":!tool/*"), "tool-file")
    assert touched_checks((file_only,), ["tool", "tool/x.py"]) == ({"tool-file": True}, "")
    # A path Git will not index is unknown, never a silent "not touched".
    for unindexable in ("../outside", "a/.git/config"):
        touched, problem = touched_checks(recipes, ["src/a.py", unindexable])
        assert touched == {} and problem.startswith("Git could not match the paths"), problem


def test_lc_changed_lists_the_changed_paths_that_exist_like_lc_verify(tmp_path: Path) -> None:
    text = (
        'version = 1\n[[checks]]\nid = "show"\npaths = ["."]\n' 'run = \'echo "args=$#"; cat "$LC_CHANGED"; exit 3\'\n'
    )
    (recipe,), _ = environment_recipes(text)
    for name in ("a.py", " lead.py", "trail.py ", "dir/b.txt", "-n"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x", encoding="utf-8")
    changed = changed_listing(["dir/b.txt", "gone.py", " lead.py", "trail.py ", "a.py", "-n", "a.py", ""])
    # The command runner strips each argument; the bracketed paths survive it.
    argv = [part.strip() for part in recipe.argv(None, changed)]
    done = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert done.returncode == 3, done.stderr
    assert done.stdout == "args=0\n lead.py\n-n\na.py\ndir/b.txt\ntrail.py \n"
    # Recipes without paths ignore the argument; the bound holds at and past its edge.
    assert CheckRecipe(id="x", kind="lint", command=("lint", CHANGED_PLACEHOLDER)).argv(None, changed) == (
        "lint",
        CHANGED_PLACEHOLDER,
    )
    exact = ["p" * (MAX_CHANGED_BYTES - 3)]
    assert changed_listing_refusal(exact) == ""
    assert "cannot be passed for $LC_CHANGED" in changed_listing_refusal(["p" * (MAX_CHANGED_BYTES - 2)])


class _Capturing(_Scripted):
    """A scripted ``command`` runner that records the argv of every run."""

    def __init__(self) -> None:
        super().__init__(lambda _runtime_id, _side, _count: ({}, 50))
        self.commands: dict[tuple[str, str], list[str]] = {}

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> Any:
        self.commands[(runtime.id, side)] = list(runtime.values["command"])
        return super().run(context, runtime, surface, side=side)


def _environment_review(tmp_path: Path, environment: str | None, head: dict[str, str | None]) -> Any:
    runner = _Capturing()
    base = {**_base_files(), "docs/old.md": "old\n", "web/README.md": "web\n"}
    if environment is not None:
        base[ENVIRONMENT_FILE] = environment
    review = _review(tmp_path, base=base, head=head, runner_registry=RunnerRegistry((runner,)))
    return review, runner


def test_review_runs_the_trusted_base_checks_the_change_touches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "pwned-by-head"
    head_environment = (
        ENVIRONMENT.replace("print(1)", marker) + '\n[[checks]]\nid = "head-only"\npaths = ["."]\nrun = "true"\n'
    )
    head: dict[str, str | None] = {
        "calc.py": CALC_BASE + "\n",
        "docs/old.md": None,
        "web/README.md": "edited\n",
        ENVIRONMENT_FILE: head_environment,
    }
    review, runner = _environment_review(tmp_path, ENVIRONMENT, head)
    payload = _get(review)
    # The trusted base's checks, not the head's copy and not review.yaml's check_recipes.
    assert [row["id"] for row in payload["recipes"]] == ["py", "docs", "web"] and payload["errors"] == []
    assert marker not in str(payload["recipes"])
    assert [row["id"] for row in payload["future_policy"]["added"]] == ["head-only"]
    assert _run(review, "checks.recipe:unit").status_code == 409
    # `web` is touched only through its excluded README: not applicable, refused.
    untouched = _run(review, "checks.recipe:web")
    assert untouched.status_code == 409 and "touches none of its paths (web, :!web/README.md)" in untouched.text
    assert _plan_entry(review, "checks.recipe:docs")["status"] == "run"  # a deletion counts as a touch

    # Past the $LC_CHANGED bound neither side runs, and the check does not pass.
    monkeypatch.setattr(recipes_module, "MAX_CHANGED_BYTES", 8)
    refused = _run(review, "checks.recipe:py")
    assert refused.status_code == 200, refused.text
    assert refused.json()["verdicts"] == {"checks.recipe:py": "NOT_RUN"} and runner.commands == {}
    monkeypatch.undo()

    ran = _run(review, "checks.recipe:py")
    assert ran.status_code == 200, ran.text
    assert ran.json()["verdicts"] == {"checks.recipe:py": "PASS"}
    command = runner.commands[("recipe:py", "new")]
    assert command[:2] == ["sh", "-c"] and command[2].endswith("\npython3 -c 'print(1)'") and command[3] == "sh"
    # Every changed path, deleted ones too; the command keeps those that exist.
    assert command[4:] == [
        f"[{path}]" for path in sorted(["calc.py", "docs/old.md", "web/README.md", ENVIRONMENT_FILE])
    ]


def test_an_invalid_trusted_base_environment_is_reported_and_runs_nothing(tmp_path: Path) -> None:
    invalid = 'version = 1\n[[checks]]\nid = "x"\npaths = ["."]\nrun = "true"\ncolour = "red"\n'
    review, runner = _environment_review(tmp_path, invalid, {"calc.py": CALC_BASE + "\n"})
    payload = _get(review)
    assert payload["recipes"] == [] and any("unknown key 'colour'" in error for error in payload["errors"])
    # The invalid file stands as an entry that cannot run; check_recipes do not stand in for it.
    planned = _plan_entry(review, "checks.recipe:environment.toml")
    # The plan entry carries the parse error itself, not a generic reason.
    assert planned["status"] == "not_available" and "unknown key 'colour'" in planned["reason"], planned["reason"]
    assert planned["reason"].startswith("invalid in the trusted base: .lemoncrow/environment.toml: ")
    for entry in ("checks.recipe:environment.toml", "checks.recipe:unit"):
        assert _run(review, entry).status_code == 409
    assert runner.commands == {}


def test_without_environment_checks_review_yaml_recipes_are_unchanged(tmp_path: Path) -> None:
    head: dict[str, str | None] = {"calc.py": CALC_BASE + "\n"}
    absent, _ = _environment_review(tmp_path / "absent", None, head)
    empty, _ = _environment_review(tmp_path / "empty", "version = 1\n", head)
    # A head-side environment.toml is proposed policy only; it never defines this Review's checks.
    added, _ = _environment_review(tmp_path / "added", None, {**head, ENVIRONMENT_FILE: ENVIRONMENT})
    expected = _get(absent)
    assert [row["id"] for row in expected["recipes"]] == ["unit", "fmt"]
    for other in (empty, added):
        payload = _get(other)
        assert payload["recipes"] == expected["recipes"] and payload["errors"] == expected["errors"]
    assert sorted(row["id"] for row in _get(added)["future_policy"]["added"]) == ["docs", "py", "web"]


# --------------------------------------------------------------------------- #
# M2: kind, timeout, attr: pathspecs, needs, services, dependencies, fingerprint
# --------------------------------------------------------------------------- #

KINDS = (
    """\
version = 1

[needs]
network = ["pypi.org"]
secrets = ["API_TOKEN"]

[[dependencies]]
lockfile = "uv.lock"
install = "uv sync --frozen"

[services.db]
image = "postgres@sha256:"""
    + "a" * 64
    + """"
ready = "pg_isready"

[[checks]]
id = "unit"
paths = ["*.py"]
run = "python3 run_tests.py"
kind = "test"
timeout = 60
needs = ["db"]

[[checks]]
id = "docs-test"
paths = ["docs"]
run = "make docs"
kind = "test"

[[checks]]
id = "build"
paths = ["."]
run = "make"
"""
)


def test_a_check_kind_files_it_under_that_kind_and_its_timeout_bounds_it() -> None:
    from lemoncrow.pro.capabilities.review.check_runs import recipe_runtime
    from lemoncrow.pro.capabilities.review.hermetic import DeclaredDependencies
    from lemoncrow.pro.environment.services import ServiceSet

    (unit, docs, build), errors = environment_recipes(KINDS)
    assert errors == ()
    assert (unit.kind, unit.kind_entry, unit.timeout) == ("test", "checks.test", 60)
    # Without a timeout, 900 s; without a kind, a build recipe reachable only as checks.recipe:<id>.
    assert (docs.kind, docs.timeout) == ("test", 900) and (build.kind, build.kind_entry) == ("build", "")
    catalog = build_recipe_catalog("", [], _reader({}), environment=KINDS)
    assert [recipe.id for recipe in catalog.for_entry("checks.test")] == ["unit", "docs-test"]
    runtime, _surface = recipe_runtime(unit, changed=("a.py",))
    assert runtime.values["timeout"] == 60
    # The services it needs and exactly the declared lockfiles reach the runner as trusted-only values.
    assert runtime.values["services"] == ServiceSet((unit.environment.service("db"),))  # type: ignore[union-attr]
    assert runtime.values["dependencies"] == DeclaredDependencies(("uv.lock",))
    build_runtime, _ = recipe_runtime(build)
    assert "services" not in build_runtime.values
    # The longest timeout a check may ask for is accepted; one past it is refused by the parser.
    edge = 'version = 1\n[[checks]]\nid = "x"\npaths = ["."]\nrun = "true"\ntimeout = %d\n'
    (longest,), _ = environment_recipes(edge % 3600)
    assert longest.timeout == 3600
    assert environment_recipes(edge % 3601)[0] == () and "timeout" in environment_recipes(edge % 3601)[1][0]


def test_attr_pathspecs_match_with_the_trusted_base_attributes_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.recipes import attribute_files

    # Host attributes never steer the match.
    (tmp_path / "git").mkdir()
    (tmp_path / "git" / "attributes").write_text("*.py generated\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    generated = _recipe((":(attr:generated)",), "generated")
    plain = _recipe(("*.py",), "plain")
    attributes = {".gitattributes": "*.gen generated\n", "sub/.gitattributes": "*.txt generated\n"}
    assert touched_checks((generated, plain), ["a.gen"], attributes) == ({"generated": True, "plain": False}, "")
    assert touched_checks((generated, plain), ["a.py"], attributes) == ({"generated": False, "plain": True}, "")
    assert touched_checks((generated,), ["sub/x.txt"], attributes) == ({"generated": True}, "")
    assert touched_checks((generated,), ["x.txt"], attributes) == ({"generated": False}, "")
    # Unknown attributes, or a change to them: the attr: check is unknown (it runs); the others still match.
    touched, why = touched_checks((generated, plain), ["a.py"], None)
    assert touched == {"plain": True} and "could not be read" in why
    touched, why = touched_checks((generated, plain), ["a.py", "docs/.gitattributes"], attributes)
    assert touched == {"plain": True} and "edits .gitattributes" in why
    # An attributes path that leaves the scratch tree is a match Git cannot make, never a silent skip.
    touched, why = touched_checks((generated,), ["a.gen"], {"../.gitattributes": "*"})
    assert touched == {} and why.startswith("Git could not match the paths")
    # Reading them is bounded; past a bound they are unknown.
    files = {".gitattributes": "*.gen generated\n", "a/.gitattributes": "", "a/b.py": ""}
    assert attribute_files(files, _reader(files)) == {".gitattributes": "*.gen generated\n", "a/.gitattributes": ""}
    assert attribute_files(None, _reader(files)) is None
    huge = {".gitattributes": "x" * (recipes_module.MAX_ATTRIBUTE_BYTES + 1)}
    assert attribute_files(huge, _reader(huge)) is None
    many = [f"d{i}/.gitattributes" for i in range(recipes_module.MAX_ATTRIBUTE_FILES + 1)]
    assert attribute_files(many, lambda _path: "") is None
    assert attribute_files(many[:-1], lambda _path: "") is not None


def test_review_selects_attr_checks_from_the_base_gitattributes(tmp_path: Path) -> None:
    text = (
        'version = 1\n[[checks]]\nid = "gen"\npaths = [":(attr:generated)"]\nrun = "true"\n'
        '[[checks]]\nid = "py"\npaths = ["*.py"]\nrun = "true"\n'
    )
    runner = _Capturing()
    base = {**_base_files(), ENVIRONMENT_FILE: text, ".gitattributes": "*.gen generated\n"}
    review = _review(tmp_path, base=base, head={"calc.py": CALC_BASE + "\n"}, runner_registry=RunnerRegistry((runner,)))
    assert _plan_entry(review, "checks.recipe:py")["status"] == "run"
    gen = _plan_entry(review, "checks.recipe:gen")
    assert gen["status"] == "inapplicable" and "touches none of its paths" in gen["reason"]


def test_a_kind_entry_runs_only_the_checks_the_change_touches(tmp_path: Path) -> None:
    text = (
        'version = 1\n[[checks]]\nid = "unit"\npaths = ["*.py"]\nrun = "python3 run_tests.py"\nkind = "test"\n'
        '[[checks]]\nid = "docs-test"\npaths = ["docs"]\nrun = "make docs"\nkind = "test"\n'
        '[needs]\nnetwork = ["pypi.org"]\nsecrets = ["API_TOKEN"]\n'
    )
    review, runner = _environment_review(tmp_path, text, {"calc.py": CALC_BASE + "\n"})
    assert _plan_entry(review, "checks.test")["status"] == "run"
    ran = _run(review, "checks.test")
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["verdicts"] == {"checks.recipe:unit": "PASS", "checks.test": "PASS"}
    assert set(runner.commands) == {("recipe:unit", "new")}
    assert any(note.startswith("docs-test: not run (the change touches none of its paths") for note in body["notes"])
    # Review grants none of the definition's needs, and says so.
    assert any("Review grants none" in note and "pypi.org" in note for note in body["notes"]), body["notes"]


def test_review_compares_its_fingerprint_with_the_lc_verify_record(tmp_path: Path) -> None:
    from lemoncrow_client.environment import parse_environment, read_verify_record, write_verify_record

    text = (
        'version = 1\n[[dependencies]]\nlockfile = "uv.lock"\ninstall = "uv sync"\n'
        '[[checks]]\nid = "py"\npaths = ["*.py"]\nrun = "true"\n'
    )
    head: dict[str, str | None] = {"calc.py": CALC_BASE + "\n", "uv.lock": "version = 1\n"}
    review, _runner = _environment_review(tmp_path, text, head)
    definition = parse_environment(text)
    (check,) = definition.checks
    common = review.repo_root / ".git"
    head_sha = str(review.repo.head.target)

    def record(lockfile: bytes, *, dirty: bool) -> None:
        fingerprint = definition.fingerprint(check, lambda path: lockfile if path == "uv.lock" else None)
        write_verify_record(common, head_sha, {"py": (fingerprint, "passed")}, dirty=dirty)

    assert _run(review, "checks.recipe:py").json()["verify"] == []  # no record: nothing to compare
    record(b"version = 1\n", dirty=False)
    assert read_verify_record(common, head_sha) is not None
    (same,) = _run(review, "checks.recipe:py").json()["verify"]
    assert same["same"] is True and same["differences"] == [] and same["status"] == "passed"
    record(b"version = 2\n", dirty=True)
    body = _run(review, "checks.recipe:py").json()
    (other,) = body["verify"]
    assert other["same"] is False and other["differences"] == ["lockfile_digest"] and other["dirty"] is True
    assert any("under a different environment (lockfile_digest)" in note for note in body["notes"])
