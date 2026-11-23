"""P12-4 and P13-1 (SPEC 7.1 OWNER-19, OWNER-22): a rename of a function that a config, manifest or script file names,
or that a source file of its own language names by its qualified name, is read, never batched.

Three layers: the search over the head's files (which files, the match rule and every bound, at the bound and past it),
the head reader (a commit range's tree, a working-tree or staged capture's base tree under the frozen changed files),
and the product path (``/targets`` on a real local review, where the head is the Snapshot and never the checkout).
"""

from __future__ import annotations

import asyncio
import codecs
import random
import re
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review import config_names
from lemoncrow.pro.capabilities.review.config_names import (
    ConfigBudget,
    ConfigFile,
    SearchBound,
    head_config_files,
    is_searched_path,
    module_stem,
    module_stems,
    search_config_names,
    source_family,
)
from tests.gateway.test_review_since_review import _ALPHA_PY, _BASE_PY, _GAMMA_PY, _Repo, _since, _write


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# ------------------------------------------------------------------ which files count


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "package.json",
        "serverless.yml",
        "template.yaml",
        "Procfile",
        "Dockerfile",
        ".github/workflows/ci.yml",
        "infra/main.tf",
        "infra/prod.tfvars",
        "infra/stack.hcl",
        ".flaskenv",
        ".env",
        "deploy.sh",
        "deploy.ps1",
        "deploy.bat",
        "Makefile",
        "justfile",
        "Jenkinsfile",
        "Pipfile",
        "crontab",
        "Caddyfile",
        "BUILD",
        ".bazelrc",
        "deploy/app.service",
        "app.properties",
        "pom.xml",
        "nginx.conf",
        "gunicorn.conf.py",
        "templates/unit.j2",
        "templates/app.tmpl",
        "config.in",
        "notes.txt",
        "src/app.ts",
        "src/lib.rs",
        "LICENSE",
    ],
)
def test_these_files_are_searched(path: str) -> None:
    assert is_searched_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/guide.MD",
        "docs/intro.markdown",
        "docs/api.rst",
        "docs/page.adoc",
        "docs/page.asciidoc",
        "package-lock.json",
        "web/npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
        "uv.lock",
        "Pipfile.lock",
        "assets/logo.png",
        "assets/photo.JPG",
        "dist/app.zip",
        "lib/native.so",
        "fonts/a.woff2",
        "data/blob.pkl",
        "build/mod.pyc",
    ],
)
def test_prose_docs_generated_lockfiles_and_known_binaries_are_not_searched(path: str) -> None:
    assert not is_searched_path(path)


@pytest.mark.parametrize(
    ("path", "family"),
    [
        ("a.py", "python"),
        ("pkg/a.pyi", "python"),
        ("a.go", "go"),
        ("a.rs", "rust"),
        ("A.java", "java"),
        ("a.js", "javascript"),
        ("a.tsx", "javascript"),
        ("a.mts", "javascript"),
        ("setup.py", ""),  # Python that configures a tool: searched whatever the renamed function's language
        ("sub/gunicorn.conf.py", ""),
        ("deploy.sh", ""),
        ("main.tf", ""),
        ("Makefile", ""),
    ],
)
def test_the_language_family_of_a_source_file(path: str, family: str) -> None:
    assert source_family(path) == family


# ------------------------------------------------------------------ the match rule


def _files(**texts: str) -> config_names.HeadConfigFiles:
    """The head's files, keyed by path with ``__`` standing for ``/`` and ``_dot_`` for ``.``."""

    items = [(key.replace("__", "/").replace("_dot_", "."), text.encode("utf-8")) for key, text in texts.items()]
    return lambda _budget: [ConfigFile(path, len(payload), lambda payload=payload: payload) for path, payload in items]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        pytest.param("serverless.yml", "functions:\n  hello:\n    handler: app.old_name\n", id="lambda-handler"),
        pytest.param(
            "template.yaml",
            "Resources:\n  Fn:\n    Type: AWS::Serverless::Function\n    Properties:\n      Handler: app.old_name\n",
            id="sam-template",
        ),
        pytest.param("pyproject.toml", '[project.scripts]\nx = "pkg.mod:old_name"\n', id="pyproject-entry-point"),
        pytest.param(
            "setup.cfg", "[options.entry_points]\nconsole_scripts =\n    x = pkg.mod:old_name\n", id="setup-cfg"
        ),
        pytest.param(
            "setup.py", "setup(entry_points={'console_scripts': ['x=pkg.mod:old_name']})\n", id="setup-py-entry-points"
        ),
        pytest.param(
            "package.json",
            '{"scripts": {"start": "node -e \\"require(\'./app\').old_name()\\""}}',
            id="package-json-script",
        ),
        pytest.param("Procfile", "worker: python -c 'from app import old_name; old_name()'\n", id="procfile"),
        pytest.param("Dockerfile", 'CMD ["python", "-m", "app.old_name"]\n', id="dockerfile"),
        pytest.param(
            ".github/workflows/ci.yml",
            "jobs:\n  t:\n    steps:\n      - run: python -c 'import app; app.old_name()'\n",
            id="workflow",
        ),
        pytest.param("conf/app.ini", "[app]\nentry = old_name\n", id="bare-in-ini"),
        pytest.param(
            "deploy/conf.yaml", "# old_name is called by the nightly cron\nkey: 1\n", id="prose-in-a-yaml-comment"
        ),
        pytest.param("deploy/conf.json", '{"description": "calls old_name twice"}', id="prose-in-a-json-string"),
        pytest.param("infra/main.tf", 'resource "f" "x" {\n  handler = "b.old_name"\n}\n', id="terraform"),
        pytest.param("infra/prod.tfvars", 'handler = "b.old_name"\n', id="tfvars"),
        pytest.param(".flaskenv", "FLASK_APP=b:old_name\n", id="flaskenv"),
        pytest.param("deploy.sh", "gcloud functions deploy f --entry-point=old_name\n", id="shell-script"),
        pytest.param("Makefile", "run:\n\tpython -c 'from b import old_name; old_name()'\n", id="makefile"),
        pytest.param("deploy/app.service", "[Service]\nExecStart=/usr/bin/python -c 'b.old_name()'\n", id="systemd"),
        pytest.param("app.properties", "main.handler=b.old_name\n", id="properties"),
        pytest.param("conf/app.xml", "<handler>b.old_name</handler>\n", id="xml"),
        pytest.param("run.ps1", "python -c 'b.old_name()'\n", id="powershell"),
        pytest.param("justfile", "run:\n  python -c 'b.old_name()'\n", id="justfile"),
        pytest.param("Jenkinsfile", "sh 'python -c \"b.old_name()\"'\n", id="jenkinsfile"),
        pytest.param("templates/unit.j2", "ExecStart=b.old_name\n", id="jinja-template"),
        pytest.param("gunicorn.conf.py", "wsgi_app = 'b:old_name'\n", id="gunicorn-conf-py"),
        pytest.param(
            "stacks/api.ts", "new Function(this, 'F', { handler: 'b.old_name' })\n", id="cdk-stack-in-typescript"
        ),
    ],
)
def test_a_config_file_that_names_the_function_is_found(path: str, text: str) -> None:
    found = search_config_names({"old_name"}, lambda _b: [ConfigFile(path, len(text), lambda: text.encode())])
    assert found.named == {"old_name": path} and not found.incomplete


@pytest.mark.parametrize(
    "text",
    [
        "handler: app.other_name\n",
        "handler: app.old_name_v2\n",  # a longer word is another name
        "handler: app.my_old_name\n",
        "handler: app.old_names\n",
        "handler: app.OLD_NAME\n",  # names are case-sensitive
        "",
    ],
)
def test_a_config_file_that_does_not_name_the_function_is_not_a_hit(text: str) -> None:
    found = search_config_names({"old_name"}, lambda _b: [ConfigFile("a.yml", len(text), lambda: text.encode())])
    assert found.named == {} and not found.incomplete


def test_one_pass_answers_for_every_name_and_names_the_first_file_that_holds_each() -> None:
    head = _files(
        a_dot_yml="x: first.alpha\n",
        b_dot_toml='y = "pkg:beta"\n',
        c_dot_json='{"z": "alpha beta"}',
    )
    found = search_config_names({"alpha", "beta", "gamma"}, head)
    assert found.named == {"alpha": "a.yml", "beta": "b.toml"} and not found.incomplete


def test_no_names_means_nothing_is_read() -> None:
    def boom(_budget: ConfigBudget) -> Iterable[ConfigFile]:
        raise AssertionError("the head must not be read for no names")

    assert search_config_names(set(), boom).named == {}


def test_the_search_stops_once_every_name_is_found() -> None:
    read: list[str] = []

    def head(_budget: ConfigBudget) -> Iterable[ConfigFile]:
        for path, text in ("a.yml", "k: alpha"), ("b.yml", "k: alpha"):
            yield ConfigFile(path, len(text), lambda path=path, text=text: (read.append(path), text.encode())[1])

    assert search_config_names({"alpha"}, head).named == {"alpha": "a.yml"}
    assert read == ["a.yml"]


# ------------------------------------------------------------------ the bounds: at the bound passes, past it refuses


def _many(count: int, text: str = "k: v\n") -> config_names.HeadConfigFiles:
    return lambda _b: [ConfigFile(f"c{i}.yml", len(text), lambda: text.encode()) for i in range(count)]


def test_the_file_count_bound_passes_at_the_bound_and_is_incomplete_past_it() -> None:
    at = search_config_names({"alpha"}, _many(3), ConfigBudget(max_files=3))
    assert at.named == {} and not at.incomplete
    past = search_config_names({"alpha"}, _many(4), ConfigBudget(max_files=3))
    assert "more than 3 files to search" in past.incomplete


def test_the_per_file_bound_passes_at_the_bound_and_is_incomplete_past_it() -> None:
    def one(size: int) -> config_names.HeadConfigFiles:
        text = ("k: " + "x" * (size - 3)).encode()
        assert len(text) == size
        return lambda _b: [ConfigFile("big.yml", size, lambda: text)]

    at = search_config_names({"alpha"}, one(100), ConfigBudget(max_file_bytes=100))
    assert at.named == {} and not at.incomplete
    past = search_config_names({"alpha"}, one(101), ConfigBudget(max_file_bytes=100))
    assert "big.yml is larger than 100 bytes" in past.incomplete


def test_a_name_in_a_file_past_the_per_file_bound_is_never_silently_skipped() -> None:
    text = ("x: 1\n" * 30 + "handler: app.alpha\n").encode()
    found = search_config_names(
        {"alpha"}, lambda _b: [ConfigFile("f.yml", len(text), lambda: text)], ConfigBudget(max_file_bytes=10)
    )
    assert found.incomplete and found.named == {}


def test_the_total_bound_passes_at_the_bound_and_is_incomplete_past_it() -> None:
    text = "k: v\n"  # five bytes
    at = search_config_names({"alpha"}, _many(4, text), ConfigBudget(max_bytes=4 * len(text)))
    assert at.named == {} and not at.incomplete
    past = search_config_names({"alpha"}, _many(4, text), ConfigBudget(max_bytes=4 * len(text) - 1))
    assert "hold more than" in past.incomplete


def test_the_time_bound_passes_at_the_bound_and_is_incomplete_past_it() -> None:
    def run(step: float) -> config_names.ConfigSearch:
        now = [0.0]

        def clock() -> float:
            now[0] += step
            return now[0]

        # The budget reads the clock once to start, then once per file: three files take 3 * step.
        return search_config_names({"alpha"}, _many(3), ConfigBudget(seconds=3.0, clock=clock))

    assert not run(1.0).incomplete  # the last tick is exactly 3.0 s after the start
    past = run(1.5)
    assert "longer than 3 seconds" in past.incomplete and past.transient


def test_a_time_bound_met_while_walking_a_tree_with_no_config_files_is_incomplete() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 1.0
        return now[0]

    def walk(budget: ConfigBudget) -> Iterable[ConfigFile]:
        for _ in range(100):  # entries that are not config files: nothing is yielded, the walk alone ticks
            budget.tick()
        return ()

    found = search_config_names({"alpha"}, walk, ConfigBudget(seconds=5.0, clock=clock))
    assert "longer than 5 seconds" in found.incomplete


def test_files_that_are_not_searched_do_not_spend_the_budget_or_get_read() -> None:
    def head(_budget: ConfigBudget) -> Iterable[ConfigFile]:
        for path in "README.md", "yarn.lock", "logo.png":
            yield ConfigFile(path, 10**9, lambda: pytest.fail("a file that is not searched was read"))
        yield ConfigFile("a.yml", 5, lambda: b"k: v\n")

    found = search_config_names({"alpha"}, head, ConfigBudget(max_files=1, max_file_bytes=5, max_bytes=5))
    assert found.named == {} and not found.incomplete and found.searched == 1


# ------------------------------------------------------------------ same-language source, binaries, encodings


def _one(path: str, payload: bytes) -> config_names.HeadConfigFiles:
    return lambda _b: [ConfigFile(path, len(payload), lambda: payload)]


def test_a_source_file_of_the_functions_own_language_is_not_read_when_there_is_no_stem_to_qualify_the_name_by() -> None:
    """A function with no module stem (an ``__init__.py`` at the top) has no qualified form: its importers are not read."""

    python_only = {"alpha": frozenset({"python"})}
    importer = _one("pkg/use.py", b"from b import alpha\nb.alpha()\n")
    for modules in (None, {}, {"alpha": frozenset()}, {"beta": frozenset({"b"})}):
        found = search_config_names({"alpha"}, importer, families=python_only, modules=modules)
        assert found.named == {} and not found.incomplete and found.searched == 0
    # Every other kind of file, and a source file of another language, is searched for the bare name.
    for path in ("run.sh", "stack.ts", "setup.py", "Makefile", "main.go"):
        assert search_config_names({"alpha"}, _one(path, b"b.alpha()\n"), families=python_only).named == {"alpha": path}
    for path in ("run.sh", "stack.ts", "setup.py", "Makefile", "main.go"):  # and the stems change nothing there
        bare = _one(path, b"alpha()\n")
        assert search_config_names({"alpha"}, bare, families=python_only, modules=_B).named == {"alpha": path}


_PY = {"alpha": frozenset({"python"})}
_B = {"alpha": frozenset({"b"})}


@pytest.mark.parametrize(
    ("path", "stem"),
    [
        ("src/views.py", "views"),
        ("views.py", "views"),
        ("app/views.pyi", "views"),
        ("pkg/__init__.py", "pkg"),
        ("a/b/__init__.pyi", "b"),
        ("__init__.py", ""),
        ("lambda/index.ts", "index"),
        ("cmd/handlers.go", "handlers"),
        ("src/my.mod.py", "my.mod"),
        ("Makefile", "Makefile"),
        ("src/b.rs", "b"),
        ("src/b/mod.rs", "b"),  # a Rust module directory is named by its directory
        ("src/mod.rs", "src"),
        ("mod.rs", ""),
        ("pkg/mod.py", "mod"),  # only in Rust is ``mod`` a package file
        ("src/lib.rs", "lib"),  # a crate root's directory is ``src``, which nobody writes
        ("src/main.rs", "main"),
    ],
)
def test_the_module_stem_is_the_file_name_without_its_extension_or_the_package_directory(path: str, stem: str) -> None:
    assert module_stem(path) == stem


@pytest.mark.parametrize(
    ("path", "stems"),
    [
        ("util/strings.go", {"strings", "util"}),  # a Go caller qualifies by package, the directory
        ("a/b/c.go", {"c", "b"}),
        ("util/util.go", {"util"}),
        ("main.go", {"main"}),
        ("src/b.py", {"b"}),
        ("pkg/__init__.py", {"pkg"}),
        ("__init__.py", set()),
        ("src/b/mod.rs", {"b"}),
        ("mod.rs", set()),
        ("lambda/index.ts", {"index"}),
        ("app/Service.java", {"Service"}),
    ],
)
def test_the_stems_a_same_language_file_may_qualify_a_function_by(path: str, stems: set[str]) -> None:
    assert module_stems(path) == stems


@pytest.mark.parametrize(
    "text",
    [
        'LOGIN_URL = "app.views.alpha"',  # a Django settings string: a dotted path that ends in the stem
        'LOGIN_URL = "views.alpha"',
        'handler="views.alpha"',  # a CDK handler
        '"task": "proj.views.alpha", "schedule": 60',  # a Celery beat entry
        'ENTRY = "pkg.views:alpha"',
        'ENTRY = "views:alpha"',
        'path = "src/views.alpha"',  # a slash path
        "views.alpha()",  # a call through the module
        "mock.patch('app.views.alpha.delay')",  # more after the name, past a dot
        "x = views.alpha",
        "(views.alpha)",
        "views.alpha",  # nothing around it
        "first line\nviews.alpha\nlast line",
        "crate::views::alpha()",  # Rust
        "use b::views::alpha;",
        "views::alpha",
        # Python spells a dotted name with a gap around the dot, and only around the dot.
        "views .alpha",
        "views. alpha",
        "views .  alpha",
        "views\t.\talpha",
        "views.\nalpha",
        "x = (views\n        .alpha)",
        "x = views.\\\n    alpha",  # a backslash, then the line break
        "x = views\\\n    .alpha",
        "x = views \\\n . \\\n alpha",
        "x = views.\r\nalpha",
        "x = views\\\r\n.alpha",
    ],
)
def test_a_same_language_file_that_holds_the_qualified_name_is_found(text: str) -> None:
    found = search_config_names(
        {"alpha"}, _one("app/settings.py", text.encode()), families=_PY, modules={"alpha": frozenset({"views"})}
    )
    assert found.named == {"alpha": "app/settings.py"} and not found.incomplete and found.searched == 1


@pytest.mark.parametrize(
    "text",
    [
        "from views import alpha\nalpha()",  # the bare name: the documented limit
        'getattr(views, "alpha")',
        "alpha = 1",
        "alpha",
        'LOGIN_URL = "app.other.alpha"',  # another module's function of the same name
        'ENTRY = "pkg.other:alpha"',
        "other.alpha()",
        "myviews.alpha",  # the stem is a whole word
        "views2.alpha",
        "x_views.alpha",
        "$views.alpha",
        "views.alpha_two",  # and so is the name
        "views.alphabet",
        "views.alpha2",
        "views.Alpha",
        "Views.alpha",
        "views_alpha",
        "views alpha",
        "views.py:alpha",  # the extension is not the stem
        "alpha.views",
        "views..alpha",
        "views. .alpha",
        "views.\n.alpha",
        # No gap around a colon: ``views: alpha`` is an annotation, and ``::`` takes none either.
        "views: alpha",
        "views :alpha",
        "views : alpha",
        "views:\nalpha",
        "views:: alpha",
        "views ::alpha",
        "views : :alpha",
        "views:::alpha",
        # A backslash joins lines only when a line break follows it.
        "views\\ .alpha",
        "views.\\\ralpha",
        "views.\\ alpha",
        "",
    ],
)
def test_a_same_language_file_without_the_qualified_name_is_not_a_hit(text: str) -> None:
    found = search_config_names(
        {"alpha"}, _one("app/settings.py", text.encode()), families=_PY, modules={"alpha": frozenset({"views"})}
    )
    assert found.named == {} and not found.incomplete and found.searched == 1


def test_the_stems_of_a_renamed_file_are_alternatives_and_belong_to_their_own_name() -> None:
    head = _files(a_dot_py='X = "handlers.alpha"', b_dot_py='Y = "views.beta"', c_dot_py='Z = "views.alpha"')
    py = {"alpha": frozenset({"python"}), "beta": frozenset({"python"})}
    found = search_config_names(
        {"alpha", "beta", "gamma"},
        head,
        families=py,
        modules={"alpha": frozenset({"views", "handlers"}), "beta": frozenset({"handlers"})},
    )
    assert found.named == {"alpha": "a.py"}  # beta is not named by *views* and gamma has no stem
    found = search_config_names(
        {"alpha", "beta"}, head, families=py, modules={"alpha": frozenset({"views"}), "beta": frozenset({"views"})}
    )
    assert found.named == {"alpha": "c.py", "beta": "b.py"}


def test_a_same_language_file_is_searched_by_the_qualified_name_in_javascript_too() -> None:
    js = {"oldName": frozenset({"javascript"})}
    index = {"oldName": frozenset({"index"})}
    cdk = _one("stacks/api.ts", b"new lambda.Function(this, 'F', { handler: 'index.oldName' });\n")
    assert search_config_names({"oldName"}, cdk, families=js, modules=index).named == {"oldName": "stacks/api.ts"}
    jsx = _one("web/app.jsx", b"import { oldName } from './index';\noldName();\nwindow.other.oldName();\n")
    assert search_config_names({"oldName"}, jsx, families=js, modules=index).named == {}
    py = _one("deploy.py", b"handler='index.oldName'")  # another language: the bare name
    assert search_config_names({"oldName"}, py, families=js, modules=index).named == {"oldName": "deploy.py"}


def test_a_name_defined_in_two_languages_is_searched_bare_in_both_whatever_its_stems() -> None:
    both = {"alpha": frozenset({"python", "go"})}
    b = {"alpha": frozenset({"b"})}
    assert search_config_names({"alpha"}, _one("a.py", b"alpha()"), families=both, modules=b).named == {"alpha": "a.py"}
    assert search_config_names({"alpha"}, _one("a.go", b"alpha()"), families=both, modules=b).named == {"alpha": "a.go"}


def _oracle(text: str, stems: Iterable[str], name: str) -> bool:
    """The qualified rule by pairing every stem occurrence with every name occurrence after it, with no regular expression.

    A stem and a name that are each a whole word count when what lies between them is ``:``, ``::``, or a single dot
    with only white space and backslash-then-line-break continuations on either side of it.
    """

    def word(char: str) -> bool:
        return char.isalnum() or char in "_$"

    def gap(chars: str) -> bool:
        i = 0
        while i < len(chars):
            if chars[i].isspace():
                i += 1
            elif chars[i] == "\\" and chars[i + 1 : i + 2] == "\n":
                i += 2
            elif chars[i] == "\\" and chars[i + 1 : i + 3] == "\r\n":
                i += 3
            else:
                return False
        return True

    def separator(between: str) -> bool:
        left, dot, right = between.partition(".")
        return between in (":", "::") or (dot == "." and gap(left) and gap(right))

    for stem in stems:
        start = text.find(stem)
        while start != -1:
            after_stem = start + len(stem)
            if start == 0 or not word(text[start - 1]):
                at = text.find(name, after_stem)
                while at != -1:
                    end = at + len(name)
                    if (end == len(text) or not word(text[end])) and separator(text[after_stem:at]):
                        return True
                    at = text.find(name, at + 1)
            start = text.find(stem, start + 1)
    return False


def test_randomized_texts_match_the_qualified_rule_exactly_when_an_independent_scan_says_so() -> None:
    pieces = (
        *("views", "app", "alpha", "alphabet", ".", ":", "/", " ", '"', "_", "x", "9", "$", "\n", "py", "Alpha"),
        *("\t", "\\", "\\\n", "\r\n", "(", "::"),
    )
    stem_sets = (
        frozenset({"views"}),
        frozenset({"views", "app"}),
        frozenset({"app"}),
        frozenset({"views", "my.views"}),
    )
    # Noise, then a qualified name that is right or wrong in one small way, then noise.
    stems_seen = ("views", "views", "app", "app", "my.views", "myviews", "views2", "other")
    joins = (
        *(".", ".", ":", ":", "::", "::", "/", "..", " ", ""),
        *(" .", ". ", " . ", "\n.", ".\n", "\\\n.", ".\\\n", " \\\n . \\\n ", "\\.", ".\\"),
        *(" :", ": ", " ::", ":: ", " : ", ":\n", ". .", ".\r\n", "\t.\t"),
    )
    names_seen = ("alpha", "alpha", "alpha", "alpha", "alpha", "alphabet", "Alpha", "alpha_x", "alpha2")

    def noise(rng: random.Random) -> str:
        return "".join(rng.choice(pieces) for _ in range(rng.randrange(0, 5) if rng.random() < 0.5 else 0))

    hits = 0
    for seed in range(4000):
        rng = random.Random(seed)
        text = noise(rng) + rng.choice(stems_seen) + rng.choice(joins) + rng.choice(names_seen) + noise(rng)
        stems = rng.choice(stem_sets)
        found = search_config_names(
            {"alpha"}, _one("s.py", text.encode()), families=_PY, modules={"alpha": stems}
        ).named
        expected = _oracle(text, stems, "alpha")
        assert bool(found) == expected, (seed, text, sorted(stems))
        hits += expected
    assert 200 < hits < 3000, hits  # both outcomes are exercised


def test_same_language_files_spend_the_budget_and_an_incomplete_search_refuses() -> None:
    def head(count: int, size: int = 6) -> config_names.HeadConfigFiles:
        text = ("x = 1\n" * size)[:size].encode()
        return lambda _b: [ConfigFile(f"m{i}.py", len(text), lambda: text) for i in range(count)]

    kwargs: dict[str, Any] = {"families": _PY, "modules": _B}
    at = search_config_names({"alpha"}, head(3), ConfigBudget(max_files=3), **kwargs)
    assert at.named == {} and not at.incomplete and at.searched == 3
    past = search_config_names({"alpha"}, head(4), ConfigBudget(max_files=3), **kwargs)
    assert "more than 3 files to search" in past.incomplete
    assert not search_config_names({"alpha"}, head(2), ConfigBudget(max_bytes=12), **kwargs).incomplete
    assert "hold more than" in search_config_names({"alpha"}, head(2), ConfigBudget(max_bytes=11), **kwargs).incomplete
    # A source file of text past the per-file bound refuses, as any text file does; a bundle is not skipped by its name.
    big = b"var x = 1;\n" * 20 + b"b.alpha()\n"
    found = search_config_names({"alpha"}, _one("dist/app.py", big), ConfigBudget(max_file_bytes=10), **kwargs)
    assert found.incomplete and found.named == {} and "dist/app.py is larger than 10 bytes" in found.incomplete
    # A binary one is skipped as before, even with a source extension.
    blob = bytes(range(256)) * 8
    skipped = search_config_names(
        {"alpha"}, _one("gen.py", blob), ConfigBudget(max_file_bytes=10), families=_PY, modules=_B
    )
    assert not skipped.incomplete and skipped.searched == 0
    # Under the time bound a same-language file ticks like any other.
    now = [0.0]

    def clock() -> float:
        now[0] += 1.0
        return now[0]

    late = search_config_names({"alpha"}, head(5), ConfigBudget(seconds=2.0, clock=clock), **kwargs)
    assert "longer than 2 seconds" in late.incomplete and late.transient


def test_javascript_and_typescript_are_one_family() -> None:
    js = {"alpha": frozenset({"javascript"})}
    assert search_config_names({"alpha"}, _one("a.ts", b"alpha()"), families=js).searched == 0
    assert search_config_names({"alpha"}, _one("a.jsx", b"alpha()"), families=js).searched == 0
    assert search_config_names({"alpha"}, _one("a.py", b"alpha()"), families=js).named == {"alpha": "a.py"}


def test_a_name_defined_in_two_languages_is_searched_in_both() -> None:
    both = {"alpha": frozenset({"python", "go"})}
    assert search_config_names({"alpha"}, _one("a.py", b"alpha()"), families=both).named == {"alpha": "a.py"}
    assert search_config_names({"alpha"}, _one("a.go", b"alpha()"), families=both).named == {"alpha": "a.go"}


def test_a_file_with_nul_bytes_and_no_byte_order_mark_is_searched_as_utf16_in_both_byte_orders() -> None:
    text = "handler: b.alpha\n"
    for codec in ("utf-16-le", "utf-16-be"):
        found = search_config_names({"alpha"}, _one("deploy.conf", text.encode(codec)))
        assert found.named == {"alpha": "deploy.conf"} and found.searched == 1, codec
    # Real binary content that does not hold the name is searched and clears nothing.
    junk = search_config_names({"alpha"}, _one("blob.dat", b"\0\x01\x02 some bytes \0\xff"))
    assert junk.named == {} and not junk.incomplete and junk.searched == 1


def test_utf8_text_with_an_early_nul_is_still_searched_as_utf8() -> None:
    """A stray NUL must not turn a config file into something the search cannot read."""

    found = search_config_names({"old_name"}, _one("serverless.yml", b"x: \0\nhandler: app.old_name\n"))
    assert found.named == {"old_name": "serverless.yml"} and found.searched == 1


def test_a_text_file_with_a_late_nul_is_utf8_text() -> None:
    late = b"alpha " + b"x" * 9000 + b"\0"  # the NUL is past the first 8 KiB: text
    assert search_config_names({"alpha"}, _one("blob.dat", late)).named == {"alpha": "blob.dat"}


@pytest.mark.parametrize(
    "mark", [codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, codecs.BOM_UTF8]
)
def test_a_file_with_a_byte_order_mark_is_decoded_by_it_not_skipped_as_binary(mark: bytes) -> None:
    codec = {
        codecs.BOM_UTF16_LE: "utf-16-le",
        codecs.BOM_UTF16_BE: "utf-16-be",
        codecs.BOM_UTF32_LE: "utf-32-le",
        codecs.BOM_UTF32_BE: "utf-32-be",
        codecs.BOM_UTF8: "utf-8",
    }[mark]
    payload = mark + "handler: b.alpha\n".encode(codec)
    found = search_config_names({"alpha"}, _one("deploy.yml", payload))
    assert found.named == {"alpha": "deploy.yml"} and found.searched == 1


def test_a_text_file_past_the_per_file_bound_refuses_and_a_binary_one_is_skipped() -> None:
    """A model checkpoint or a data blob is not a reason to read every rename; a bundle of text still is."""

    def one(payload: bytes, **extra: Any) -> config_names.HeadConfigFiles:
        return lambda _b: [ConfigFile("data/fixture.dat", 101, lambda: payload, **extra)]

    budget = lambda: ConfigBudget(max_file_bytes=100)  # noqa: E731
    text = search_config_names({"alpha"}, one(b"k: v " * 20), budget())
    assert "data/fixture.dat is larger than 100 bytes" in text.incomplete and not text.transient
    binary = search_config_names({"alpha"}, one(b"GGUF\0\0\0\x03" + b"\x01" * 90), budget())
    assert binary.named == {} and not binary.incomplete and binary.searched == 0
    # An oversized file with a byte-order mark is text, however many NULs it holds.
    utf16 = search_config_names({"alpha"}, one(codecs.BOM_UTF16_LE + "k: v".encode("utf-16-le") * 20), budget())
    assert "larger than 100 bytes" in utf16.incomplete


@pytest.mark.parametrize(
    ("payload", "refuses"),
    [
        pytest.param(b"x: \0\nhandler: app.alpha\n" + b"k: v\n" * 40, True, id="yaml-with-an-early-nul-that-names-it"),
        pytest.param(
            b"x: \0\n" + b"k: v\n" * 3000 + b"handler: app.alpha\n", True, id="yaml-with-a-stray-nul-naming-it-late"
        ),
        pytest.param(
            ('{"handler": "b.alpha", "pad": "' + "x" * 30000 + '"}').encode("utf-16-le"),
            True,
            id="utf16-le-json-no-mark",
        ),
        pytest.param(
            ('{"pad": "' + "x" * 30000 + '", "handler": "b.alpha"}').encode("utf-16-be"),
            True,
            id="utf16-be-json-name-late",
        ),
        pytest.param(b"alpha\0" + b"\x7fELF\0\x01\x02\x03" * 2000, True, id="binary-that-holds-the-name-in-its-start"),
        pytest.param(bytes(range(256)) * 80, False, id="binary-data"),
        pytest.param(b"GGUF\0\0\0\x03" + b"\0\x01\x02\0" * 5000, False, id="binary-weights-with-nul-runs"),
    ],
)
def test_an_oversized_file_is_skipped_only_when_it_is_binary_and_does_not_name_the_function(
    payload: bytes, refuses: bool
) -> None:
    found = search_config_names(
        {"alpha"},
        lambda _b: [ConfigFile("data/blob.dat", len(payload), lambda: payload)],
        ConfigBudget(max_file_bytes=100),
    )
    assert bool(found.incomplete) is refuses, found
    if refuses:
        assert "data/blob.dat is larger than 100 bytes" in found.incomplete
    else:
        assert found.searched == 0


def test_an_oversized_file_is_sniffed_not_read_and_a_skipped_one_still_counts_as_a_file() -> None:
    sniffed: list[int] = []

    def head(_budget: ConfigBudget) -> Iterable[ConfigFile]:
        yield ConfigFile(
            "weights.pt0",
            10**9,
            lambda: pytest.fail("an oversized file was read whole"),
            lambda: (sniffed.append(1), b"\0" * 8192)[1],
        )
        yield ConfigFile("a.yml", 5, lambda: b"k: v\n")

    assert search_config_names({"alpha"}, head, ConfigBudget(max_files=2)).searched == 1
    assert sniffed == [1]
    assert "more than 1 files" in search_config_names({"alpha"}, head, ConfigBudget(max_files=1)).incomplete


@pytest.mark.parametrize(
    "path",
    [
        "m/model.safetensors", "m/llama.gguf", "m/net.ckpt", "m/net.pt", "m/net.pth", "m/net.onnx", "m/frozen.pb",
        "m/net.tflite", "m/a.npy", "m/a.npz", "d/rows.parquet", "d/model.h5", "d/arrays.hdf5", "d/features.arrow",
        "d/scores.joblib", "release/app.deb", "release/app.dmg", "app/app.apk",
    ],
)  # fmt: skip
def test_model_weights_and_other_binary_data_are_not_searched_by_name(path: str) -> None:
    assert not is_searched_path(path)


def test_a_head_that_cannot_be_read_is_incomplete() -> None:
    def unreadable(_budget: ConfigBudget) -> Iterable[ConfigFile]:
        raise OSError("no such object")

    found = search_config_names({"alpha"}, unreadable)
    assert "could not be read (OSError)" in found.incomplete and not found.transient


def test_a_file_that_cannot_be_read_is_incomplete_not_skipped() -> None:
    def broken() -> bytes:
        raise KeyError("gone")

    found = search_config_names({"alpha"}, lambda _b: [ConfigFile("a.yml", 1, broken)])
    assert "could not be read (KeyError)" in found.incomplete


def test_names_found_before_the_search_stopped_are_still_reported() -> None:
    head = lambda _b: [  # noqa: E731
        ConfigFile("a.yml", 12, lambda: b"k: x.alpha  "),
        ConfigFile("b.yml", 10**9, lambda: b""),
    ]
    found = search_config_names({"alpha", "beta"}, head, ConfigBudget(max_file_bytes=100))
    assert found.named == {"alpha": "a.yml"} and found.incomplete


# ------------------------------------------------------------------ a randomized check of the match rule

_PLACEMENTS = (
    ("serverless.yml", "handler: {mod}.{name}"),
    ("serverless.yml", "handler: {mod}:{name}"),
    ("pyproject.toml", 'x = "{mod}:{name}"'),
    ("package.json", '{{"main": "{name}"}}'),
    ("conf.ini", "# {name} is run nightly"),
    ("Procfile", "web: python -c '{name}()'"),
)
_DECOYS = ("{name}_x", "x_{name}", "{name}2", "{upper}", "prefix.{name}suffix")


def test_randomized_configs_are_refused_exactly_when_the_name_is_in_them() -> None:
    """Ground truth is how each config was built: a placement of the name, or only decoys that contain it."""

    for seed in range(300):
        rng = random.Random(seed)
        names = [f"fn_{rng.randrange(10**6)}" for _ in range(rng.randrange(1, 6))]
        placed: dict[str, str] = {}
        texts: dict[str, list[str]] = {}
        for name in names:
            fields = {"name": name, "upper": name.upper(), "mod": f"m{rng.randrange(9)}"}
            if rng.random() < 0.5:
                path, template = rng.choice(_PLACEMENTS)
                placed.setdefault(name, path)
                texts.setdefault(path, []).append(template.format(**fields))
            for decoy in rng.sample(_DECOYS, rng.randrange(0, 3)):
                path = rng.choice(_PLACEMENTS)[0]
                texts.setdefault(path, []).append(f"d: {decoy.format(**fields)}")
        texts.setdefault("unrelated/other.yaml", []).append("k: v")
        head = _files(
            **{path.replace("/", "__").replace(".", "_dot_"): "\n".join(lines) for path, lines in texts.items()}
        )
        found = search_config_names(set(names), head)
        assert set(found.named) == set(placed), (seed, names, found.named, placed)
        assert not found.incomplete


# ------------------------------------------------------------------ the head reader


def _commit(git: pygit2.Repository, files: dict[str, str], parents: list[Any]) -> str:
    root = Path(git.workdir)
    for rel, text in files.items():
        _write(root, rel, text)
        git.index.add(rel)
    git.index.write()
    sig = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000, 0)
    return str(git.create_commit(None, sig, sig, "c", git.index.write_tree(), parents))


def _paths(files: Iterable[ConfigFile]) -> dict[str, str]:
    return {file.path: file.read().decode() for file in files}


def test_a_commit_ranges_head_is_its_head_commits_whole_tree(tmp_path: Path) -> None:
    git = pygit2.init_repository(str(tmp_path / "r"), initial_head="main")
    base = _commit(git, {"serverless.yml": "base: 1", "src/a.py": "x = 1"}, [])
    head = _commit(
        git,
        {"pyproject.toml": "[project]\nname='p'", "deep/er/app.json": "{}", "serverless.yml": "handler: a.new"},
        [git.get(base).id],
    )
    # Working-tree text must never be read: change the files on disk after the commit.
    _write(Path(git.workdir), "serverless.yml", "handler: a.EDITED_ON_DISK")
    revision = SimpleNamespace(range_mode="commit_range", head_sha=head, base_sha=base, review_id="r", id="v")
    found = _paths(head_config_files(SimpleNamespace(), revision, Path(git.workdir), ConfigBudget()))  # type: ignore[arg-type]
    assert found == {
        "serverless.yml": "handler: a.new",
        "pyproject.toml": "[project]\nname='p'",
        "deep/er/app.json": "{}",
        "src/a.py": "x = 1",  # a source file is yielded too: the search decides by language whether to read it
    }


def test_symlinks_and_submodules_are_not_walked(tmp_path: Path) -> None:
    git = pygit2.init_repository(str(tmp_path / "r"), initial_head="main")
    root = Path(git.workdir)
    _write(root, "real.yml", "handler: a.old")
    (root / "link.yml").symlink_to("real.yml")
    git.index.add("real.yml")
    git.index.add("link.yml")
    git.index.add(pygit2.IndexEntry("vendor/sub", pygit2.Oid(hex="1" * 40), pygit2.GIT_FILEMODE_COMMIT))
    git.index.write()
    sig = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000, 0)
    head = str(git.create_commit(None, sig, sig, "c", git.index.write_tree(), []))
    revision = SimpleNamespace(range_mode="commit_range", head_sha=head, base_sha="", review_id="r", id="v")
    found = _paths(head_config_files(SimpleNamespace(), revision, root, ConfigBudget()))  # type: ignore[arg-type]
    assert found == {"real.yml": "handler: a.old"}


def test_a_revision_with_no_head_commit_or_an_unknown_mode_cannot_be_searched(tmp_path: Path) -> None:
    for revision in (
        SimpleNamespace(range_mode="commit_range", head_sha="", base_sha="", review_id="r", id="v"),
        SimpleNamespace(range_mode="something_else", head_sha="a", base_sha="b", review_id="r", id="v"),
    ):
        with pytest.raises(SearchBound):
            list(head_config_files(SimpleNamespace(), revision, tmp_path, ConfigBudget()))  # type: ignore[arg-type]


def _frozen_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: list[dict[str, Any]],
    blobs: dict[str, str] | None,
    media: dict[str, bytes] | None = None,
) -> Callable[[], list[ConfigFile]]:
    git = pygit2.init_repository(str(tmp_path / "r"), initial_head="main")
    base = _commit(
        git,
        {
            "serverless.yml": "handler: a.old_name",
            "gone.toml": "x = old_name",
            "was.yaml": "k: old_name",
            "keep.json": "{}",
        },
        [],
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.sources.local.read_packet_json",
        lambda _store, _revision: None if files is None else {"files": files},
    )
    store = SimpleNamespace(
        read_blob_artifact=lambda _review, _revision: blobs, read_media_artifact=lambda _review, _revision: media
    )
    revision = SimpleNamespace(range_mode="working_tree", head_sha="", base_sha=base, review_id="r", id="v")
    return lambda: list(head_config_files(store, revision, Path(git.workdir), ConfigBudget()))  # type: ignore[arg-type]


def _frozen_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: list[dict[str, Any]],
    blobs: dict[str, str] | None,
    media: dict[str, bytes] | None = None,
) -> Callable[[], dict[str, str]]:
    read = _frozen_files(tmp_path, monkeypatch, files, blobs, media)
    return lambda: _paths(read())


def test_a_working_trees_head_is_the_base_tree_under_the_frozen_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = _frozen_head(
        tmp_path,
        monkeypatch,
        [
            {"path": "serverless.yml", "status": "modified"},  # the frozen text replaces the base text
            {"path": "gone.toml", "status": "deleted"},  # no longer at the head
            {
                "path": "now.yaml",
                "old_path": "was.yaml",
                "status": "renamed",
            },  # the old path is gone, the new one is there
            {"path": "src/new.py", "status": "added"},
            {"path": "added.json", "status": "added"},
            {"path": "docs/new.md", "status": "added"},  # prose: never searched, so no text is needed
            {"path": "img/logo.png", "status": "added", "is_binary": True},  # a known binary type: never searched
            {"path": "vendor/sub", "status": "modified", "submodule_pointer": {"sha": "1" * 40}},
        ],
        {"serverless.yml": "handler: a.new_name", "now.yaml": "k: x", "added.json": '{"a": 1}', "src/new.py": "y = 2"},
    )
    assert read() == {
        "serverless.yml": "handler: a.new_name",
        "now.yaml": "k: x",
        "added.json": '{"a": 1}',
        "src/new.py": "y = 2",
        "keep.json": "{}",
    }


def test_only_a_rename_removes_the_old_path_not_any_status_that_carries_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = _frozen_head(
        tmp_path,
        monkeypatch,
        [
            {"path": "now.yaml", "old_path": "was.yaml", "status": "copied"},  # a copy leaves the old path in place
            {"path": "serverless.yml", "old_path": "gone.toml", "status": "modified"},
        ],
        {"now.yaml": "k: copy", "serverless.yml": "handler: a.new_name"},
    )
    assert read() == {
        "now.yaml": "k: copy",
        "serverless.yml": "handler: a.new_name",
        "was.yaml": "k: old_name",
        "gone.toml": "x = old_name",
        "keep.json": "{}",
    }


def test_a_changed_file_git_calls_binary_is_read_from_the_media_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UTF-16 text is binary to Git: its bytes are in the media artifact, and the search must see them."""

    utf16 = "handler: b.alpha\n".encode("utf-16-le")
    entry = [{"path": "deploy.conf", "status": "added", "is_binary": True}]
    read = _frozen_files(tmp_path, monkeypatch, entry, {}, {"deploy.conf": utf16})
    by_path = {file.path: file for file in read()}
    assert by_path["deploy.conf"].read() == utf16 and by_path["deploy.conf"].size == len(utf16)
    assert search_config_names({"alpha"}, lambda _b: read()).named == {"alpha": "deploy.conf"}


def test_a_changed_binary_file_with_no_frozen_bytes_refuses_when_it_is_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = [{"path": "deploy.conf", "status": "added", "is_binary": True}]
    for media in (None, {}):
        with pytest.raises(SearchBound):
            _frozen_head(tmp_path / str(media), monkeypatch, entry, {}, media)()
    # The same missing bytes of a file that is never searched do not matter, and the media artifact is not even read.
    quiet = _frozen_head(tmp_path / "q", monkeypatch, [{"path": "logo.png", "status": "added", "is_binary": True}], {})
    assert "logo.png" not in quiet()


def test_a_changed_file_that_is_never_searched_needs_no_frozen_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = _frozen_head(tmp_path, monkeypatch, [{"path": "docs/new.md", "status": "added"}], {})
    assert "docs/new.md" not in read()


@pytest.mark.parametrize(
    ("files", "blobs"),
    [
        pytest.param(
            [{"path": "serverless.yml", "status": "modified"}], {}, id="a changed config file has no frozen text"
        ),
        pytest.param([{"path": "serverless.yml", "status": "modified"}], None, id="no frozen text at all"),
        pytest.param(None, {"serverless.yml": "x"}, id="no packet"),
        pytest.param(["not a mapping"], {}, id="a malformed packet entry"),
    ],
)
def test_a_working_trees_head_that_cannot_be_read_exactly_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: Any, blobs: Any
) -> None:
    read = _frozen_head(tmp_path, monkeypatch, files, blobs)
    with pytest.raises(SearchBound):
        read()


# ------------------------------------------------------------------ the product path: /targets on a local review


def _commit_on_head(repo: _Repo, files: dict[str, str]) -> None:
    git = pygit2.Repository(str(repo.root))
    oid = _commit(git, files, [git.head.target])
    git.references[git.head.name].set_target(git.get(oid).id)


def _renamed(
    tmp_path: Path,
    *,
    committed: dict[str, str] | None = None,
    untracked: dict[str, str] | None = None,
    module: str = "src/b.py",
) -> tuple[_Repo, dict[str, dict[str, Any]]]:
    """A reviewed ``alpha`` in *module* (``src/b.py``) that is renamed to ``alpha_two``, with config files at the head."""

    repo = _Repo(tmp_path)
    if committed:
        _commit_on_head(repo, committed)
    _write(repo.root, "src/a.py", _BASE_PY)
    _write(repo.root, module, _ALPHA_PY)
    for rel, text in (untracked or {}).items():
        _write(repo.root, rel, text)
    repo.review_everything()
    _write(repo.root, module, _ALPHA_PY.replace("alpha", "alpha_two"))
    repo.advance()
    return repo, repo.targets()


_RENAMED = "src/b.py::alpha_two"


def test_a_rename_with_no_config_file_naming_it_is_still_batched(tmp_path: Path) -> None:
    _, rows = _renamed(
        tmp_path,
        committed={"serverless.yml": "functions:\n  hello:\n    handler: app.other\n", "pyproject.toml": "[project]\n"},
    )
    proof = _since(rows, _RENAMED)
    assert proof["routine"] is True and proof["rule"] == "rename_only"
    assert re.search(
        r"No config, manifest or script file at the head names alpha \(\d+ files searched\)\.$", proof["detail"]
    )


@pytest.mark.parametrize(
    ("path", "text"),
    [
        pytest.param("serverless.yml", "functions:\n  hello:\n    handler: b.alpha\n", id="lambda-handler"),
        pytest.param(
            "template.yaml", "Resources:\n  Fn:\n    Properties:\n      Handler: b.alpha\n", id="sam-template"
        ),
        pytest.param("pyproject.toml", '[project.scripts]\nx = "pkg.b:alpha"\n', id="pyproject-entry-point"),
        pytest.param(
            "package.json", '{"scripts": {"start": "node -e \\"require(\'./b\').alpha()\\""}}', id="package-json-script"
        ),
        pytest.param(
            "conf/cron.yaml", "# alpha runs from cron, see runbook\nschedule: nightly\n", id="prose-in-a-yaml-comment"
        ),
    ],
)
def test_a_rename_of_a_function_a_committed_config_file_names_is_read(tmp_path: Path, path: str, text: str) -> None:
    _, rows = _renamed(tmp_path, committed={path: text})
    proof = _since(rows, _RENAMED)
    assert proof["routine"] is False and proof["rule"] == ""
    assert proof["detail"] == (
        f"A config, manifest or script file names this function, so a rename is read. Found in {path}."
    )


def test_a_changed_config_file_is_read_from_the_frozen_changed_files(tmp_path: Path) -> None:
    # Not committed: the revision itself adds it, so it is in the Snapshot's changed files, not in the base tree.
    _, rows = _renamed(tmp_path, untracked={"template.yaml": "Handler: b.alpha\n"})
    assert _since(rows, _RENAMED)["routine"] is False
    assert "template.yaml" in _since(rows, _RENAMED)["detail"]


def test_the_snapshots_head_is_searched_never_the_checkout(tmp_path: Path) -> None:
    """The reader is called after the checkout moved on: it must still return the Snapshot's own text."""

    repo, _ = _renamed(
        tmp_path, committed={"serverless.yml": "handler: b.alpha\n"}, untracked={"template.yaml": "Handler: b.alpha\n"}
    )
    revision = repo.store.latest_revision(repo.session.id)
    assert revision is not None and revision.range_mode == "working_tree"
    _write(repo.root, "serverless.yml", "handler: b.EDITED_AFTER_THE_SNAPSHOT\n")  # a committed file, edited on disk
    _write(repo.root, "template.yaml", "Handler: b.EDITED_AFTER_THE_SNAPSHOT\n")  # a changed file, edited on disk
    _write(repo.root, "late.yml", "handler: b.alpha\n")  # a file the Snapshot never had
    head = _paths(head_config_files(repo.store, revision, repo.root, ConfigBudget()))
    assert head["serverless.yml"] == "handler: b.alpha\n"
    assert head["template.yaml"] == "Handler: b.alpha\n"
    assert "late.yml" not in head


def test_a_config_file_deleted_in_the_revision_no_longer_names_the_function(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _commit_on_head(repo, {"serverless.yml": "handler: b.alpha\n"})
    _write(repo.root, "src/b.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    (repo.root / "serverless.yml").unlink()  # deleted together with the rename
    repo.advance()
    assert _since(repo.targets(), _RENAMED)["routine"] is True


def test_one_search_covers_every_renamed_part_of_a_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(tmp_path)
    _commit_on_head(repo, {"serverless.yml": "handler: c.gamma\n"})
    _write(repo.root, "src/b.py", _ALPHA_PY)
    _write(repo.root, "src/c.py", _GAMMA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    _write(repo.root, "src/c.py", _GAMMA_PY.replace("gamma", "gamma_two"))
    repo.advance()
    calls: list[set[str]] = []
    real = config_names.search_config_names

    def spy(names: Any, head: Any, budget: Any = None, **kwargs: Any) -> Any:
        calls.append(set(names))
        return real(names, head, budget, **kwargs)

    monkeypatch.setattr(config_names, "search_config_names", spy)
    rows = repo.targets()
    assert calls == [{"alpha", "gamma"}]
    assert _since(rows, "src/b.py::alpha_two")["routine"] is True
    assert _since(rows, "src/c.py::gamma_two")["routine"] is False


def test_no_search_runs_when_nothing_was_renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/b.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("v + 1", "v + 1  # why"))
    repo.advance()
    monkeypatch.setattr(config_names, "search_config_names", lambda *a, **k: pytest.fail("searched"))
    assert _since(repo.targets(), "src/b.py::alpha")["rule"] == "comment_only"


# The head holds four searched files: the two config files and, now that same-language source is read for the
# qualified name, ``src/a.py`` and the frozen ``src/b.py``.
@pytest.mark.parametrize(("max_files", "routine"), [(4, True), (3, False)], ids=["at the bound", "past the bound"])
def test_a_search_that_reaches_its_bound_refuses_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_files: int, routine: bool
) -> None:
    repo = _Repo(tmp_path)
    _commit_on_head(repo, {"serverless.yml": "handler: b.other\n", "pyproject.toml": "[project]\n"})
    _write(repo.root, "src/b.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    repo.advance()
    real = config_names.search_config_names
    monkeypatch.setattr(
        config_names,
        "search_config_names",
        lambda names, head, budget=None, **kwargs: real(names, head, ConfigBudget(max_files=max_files), **kwargs),
    )
    proof = _since(repo.targets(), _RENAMED)
    assert proof["routine"] is routine
    if not routine:
        assert proof["detail"] == (
            "The config, manifest and script files could not all be searched for this function's name "
            "(there are more than 3 files to search), so a rename is read."
        )


def test_a_head_that_cannot_be_read_refuses_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/b.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    repo.advance()

    def unreadable(*_args: Any, **_kwargs: Any) -> Iterable[ConfigFile]:
        raise OSError("the object database is unreadable")

    monkeypatch.setattr(config_names, "head_config_files", unreadable)
    proof = _since(repo.targets(), _RENAMED)
    assert proof["routine"] is False
    assert "could not all be searched" in proof["detail"] and "OSError" in proof["detail"]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        pytest.param("infra/main.tf", 'resource "f" "x" {\n  handler = "b.alpha"\n}\n', id="terraform"),
        pytest.param(".flaskenv", "FLASK_APP=b:alpha\n", id="flaskenv"),
        pytest.param("deploy.sh", "gcloud functions deploy f --entry-point=alpha\n", id="shell-script"),
        pytest.param("Makefile", "run:\n\tpython -c 'from b import alpha; alpha()'\n", id="makefile"),
        pytest.param("deploy/app.service", "[Service]\nExecStart=/usr/bin/python -c 'b.alpha()'\n", id="systemd"),
        pytest.param(
            "stacks/api.ts", "new lambda.Function(this, 'F', { handler: 'b.alpha' });\n", id="cdk-stack-in-typescript"
        ),
    ],
)
def test_a_rename_that_a_script_or_a_file_of_another_language_names_is_read(
    tmp_path: Path, path: str, text: str
) -> None:
    _, rows = _renamed(tmp_path, committed={path: text})
    proof = _since(rows, _RENAMED)
    assert proof["routine"] is False and proof["rule"] == ""
    assert proof["detail"] == (
        f"A config, manifest or script file names this function, so a rename is read. Found in {path}."
    )


def test_a_prose_doc_or_a_lockfile_that_names_the_function_does_not_refuse(tmp_path: Path) -> None:
    _, rows = _renamed(
        tmp_path, committed={"README.md": "Call `alpha()`.\n", "docs/api.rst": "alpha\n", "yarn.lock": "alpha@1\n"}
    )
    assert _since(rows, _RENAMED)["routine"] is True


def test_a_python_importer_of_a_python_function_is_still_batched(tmp_path: Path) -> None:
    """The documented lc-debt boundary: same-language callers by the bare name are not checked (SPEC 7.1, P11-2)."""

    _, rows = _renamed(tmp_path, committed={"src/use.py": "from b import alpha\n\nalpha()\n"})
    proof = _since(rows, _RENAMED)
    assert proof["routine"] is True and proof["rule"] == "rename_only"
    assert "Callers in other files are not checked." in proof["detail"]


def test_a_python_function_named_by_a_python_tool_config_is_read(tmp_path: Path) -> None:
    _, rows = _renamed(tmp_path, committed={"gunicorn.conf.py": "wsgi_app = 'b:alpha'\n"})
    assert _since(rows, _RENAMED)["routine"] is False


@pytest.mark.parametrize(
    ("module", "path", "text"),
    [
        pytest.param(
            "app/views.py", "app/settings.py", 'LOGIN_REDIRECT_URL = "app.views.alpha"\n', id="django-settings-string"
        ),
        pytest.param(
            "lambda_src/index.py",
            "infra/stack.py",
            'fn = lambda_.Function(self, "F", runtime=Runtime.PYTHON_3_12, handler="index.alpha")\n',
            id="same-language-cdk-handler",
        ),
        pytest.param(
            "proj/tasks.py",
            "proj/celery.py",
            'app.conf.beat_schedule = {"nightly": {"task": "proj.tasks.alpha", "schedule": 60.0}}\n',
            id="celery-beat-entry",
        ),
        pytest.param("pkg/mod.py", "console.py", 'ENTRY = "pkg.mod:alpha"\n', id="pkg-mod-colon-name"),
        pytest.param(
            "pkg/__init__.py", "plugins.py", 'PLUGIN = "pkg.alpha"\n', id="init-file-takes-the-directory-name"
        ),
        pytest.param("src/handlers.py", "wiring.py", 'HANDLER = "src/handlers.alpha"\n', id="slash-path"),
        pytest.param("b.py", "wiring.py", "import b\nb.alpha()\n", id="call-through-the-module"),
    ],
)
def test_a_rename_that_a_same_language_source_file_names_by_its_qualified_name_is_read(
    tmp_path: Path, module: str, path: str, text: str
) -> None:
    _, rows = _renamed(tmp_path, committed={path: text}, module=module)
    proof = _since(rows, f"{module}::alpha_two")
    assert proof["routine"] is False and proof["rule"] == ""
    assert proof["detail"] == (
        f"A config, manifest or script file names this function, so a rename is read. Found in {path}."
    )


def test_a_same_language_file_that_the_revision_changes_is_read_from_the_frozen_text(tmp_path: Path) -> None:
    _, rows = _renamed(tmp_path, untracked={"app/settings.py": 'HOOK = "b.alpha"\n'})
    proof = _since(rows, _RENAMED)
    assert proof["routine"] is False and "Found in app/settings.py." in proof["detail"]


@pytest.mark.parametrize(
    "text",
    [
        'import b\nvalue = getattr(b, "alpha")\n',  # the bare name in a string: the documented limit
        "from b import alpha\n\nalpha()\n",
        "# alpha is called nightly\n",
        'HOOK = "other.alpha"\n',  # a qualified name with another stem
        'HOOK = "app.other.alpha"\n',
        'HOOK = "pkg.other:alpha"\n',
        'HOOK = "myb.alpha"\n',
        'HOOK = "b.alpha_two"\n',
        'HOOK = "b.alphabet"\n',
        'HOOK = "b.py:alpha"\n',
    ],
)
def test_a_same_language_file_without_the_qualified_name_still_batches(tmp_path: Path, text: str) -> None:
    _, rows = _renamed(tmp_path, committed={"app/settings.py": text})
    proof = _since(rows, _RENAMED)
    assert proof["routine"] is True and proof["rule"] == "rename_only"
    assert re.search(
        r"No config, manifest or script file at the head names alpha \(\d+ files searched\)\.$", proof["detail"]
    )


_ALPHA_RS = "fn alpha(v: i32) -> i32 {\n    let total = v + 1;\n    total\n}\n"
_ALPHA_UTIL_GO = "package util\n\nfunc Alpha(v int) int {\n\ttotal := v + 1\n\treturn total\n}\n"


def _renamed_source(
    tmp_path: Path, module: str, source: str, old: str, new: str, committed: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """A reviewed *source* in *module* whose function *old* is renamed to *new*, and the head's other files."""

    repo = _Repo(tmp_path)
    _commit_on_head(repo, committed)
    _write(repo.root, module, source)
    repo.review_everything()
    _write(repo.root, module, source.replace(old, new))
    repo.advance()
    return repo.targets()


_MAIN_RS = 'mod b;\n\nfn main() {\n    println!("{}", %s);\n}\n'


@pytest.mark.parametrize(
    ("module", "source", "old", "new", "path", "text"),
    [
        pytest.param(
            "src/b.rs", _ALPHA_RS, "alpha", "alpha_two", "src/main.rs", _MAIN_RS % "b::alpha(1)", id="rust-path"
        ),
        pytest.param(
            "src/b.rs",
            _ALPHA_RS,
            "alpha",
            "alpha_two",
            "src/main.rs",
            _MAIN_RS % "crate::b::alpha(1)",
            id="rust-crate-path",
        ),
        pytest.param(
            "src/b/mod.rs",
            _ALPHA_RS,
            "alpha",
            "alpha_two",
            "src/main.rs",
            _MAIN_RS % "b::alpha(1)",
            id="rust-mod-rs-takes-the-directory-name",
        ),
        pytest.param(
            "util/strings.go",
            _ALPHA_UTIL_GO,
            "Alpha",
            "AlphaTwo",
            "cmd/main.go",
            "package main\n\nfunc main() {\n\tutil.Alpha(3)\n}\n",
            id="go-package-is-the-directory",
        ),
        pytest.param(
            "util/strings.go",
            _ALPHA_UTIL_GO,
            "Alpha",
            "AlphaTwo",
            "cmd/main.go",
            "package main\n\nfunc main() {\n\tstrings.Alpha(3)\n}\n",
            id="go-the-file-name-still-counts",
        ),
        pytest.param(
            "src/b.py", _ALPHA_PY, "alpha", "alpha_two", "wiring.py", "HOOK = b .alpha\n", id="python-space-before-dot"
        ),
        pytest.param(
            "src/b.py", _ALPHA_PY, "alpha", "alpha_two", "wiring.py", "HOOK = b. alpha\n", id="python-space-after-dot"
        ),
        pytest.param(
            "src/b.py",
            _ALPHA_PY,
            "alpha",
            "alpha_two",
            "wiring.py",
            "HOOK = (b\n    .alpha)\n",
            id="python-line-break-inside-brackets",
        ),
        pytest.param(
            "src/b.py",
            _ALPHA_PY,
            "alpha",
            "alpha_two",
            "wiring.py",
            "HOOK = b.\\\n    alpha\n",
            id="python-backslash-continuation",
        ),
    ],
)
def test_a_rename_that_a_rust_go_or_spaced_python_source_file_names_is_read(
    tmp_path: Path, module: str, source: str, old: str, new: str, path: str, text: str
) -> None:
    rows = _renamed_source(tmp_path, module, source, old, new, {path: text})
    proof = _since(rows, f"{module}::{new}")
    assert proof["routine"] is False and proof["rule"] == ""
    assert f"Found in {path}." in proof["detail"]


@pytest.mark.parametrize(
    ("module", "source", "old", "new", "path", "text"),
    [
        pytest.param(
            "src/b.rs", _ALPHA_RS, "alpha", "alpha_two", "src/main.rs", _MAIN_RS % "c::alpha(1)", id="rust-other-module"
        ),
        pytest.param(
            "src/b.rs", _ALPHA_RS, "alpha", "alpha_two", "src/main.rs", _MAIN_RS % "alpha(1)", id="rust-bare-name"
        ),
        pytest.param(
            "src/b.rs",
            _ALPHA_RS,
            "alpha",
            "alpha_two",
            "src/main.rs",
            _MAIN_RS % "b:: alpha(1)",
            id="rust-gap-in-a-path",
        ),
        pytest.param(
            "src/lib.rs",
            _ALPHA_RS,
            "alpha",
            "alpha_two",
            "src/other.rs",
            "fn run() -> i32 {\n    crate::alpha(1)\n}\n",
            id="limit-rust-crate-root-is-reached-as-crate",
        ),
        pytest.param(
            "util/strings.go",
            _ALPHA_UTIL_GO,
            "Alpha",
            "AlphaTwo",
            "cmd/main.go",
            "package main\n\nfunc main() {\n\tother.Alpha(3)\n}\n",
            id="go-another-package",
        ),
        pytest.param(
            "util/strings.go",
            _ALPHA_UTIL_GO,
            "Alpha",
            "AlphaTwo",
            "cmd/main.go",
            "package main\n\nfunc main() {\n\tAlpha(3)\n}\n",
            id="go-bare-name",
        ),
        pytest.param(
            "src/b.py", _ALPHA_PY, "alpha", "alpha_two", "wiring.py", "def f(b: alpha) -> None: ...\n", id="annotation"
        ),
        pytest.param(
            "src/b.py",
            _ALPHA_PY,
            "alpha",
            "alpha_two",
            "wiring.py",
            "x = {b: alpha}\nb :alpha\n",
            id="colon-with-a-gap",
        ),
        pytest.param("src/b.py", _ALPHA_PY, "alpha", "alpha_two", "wiring.py", "x = b. .alpha\n", id="python-two-dots"),
        # The documented limits: a qualified name that is built, aliased or spelled another way.
        pytest.param(
            "src/b.py",
            _ALPHA_PY,
            "alpha",
            "alpha_two",
            "wiring.py",
            'T = "app.b." "alpha"\n',
            id="limit-implicit-concat",
        ),
        pytest.param(
            "src/b.py", _ALPHA_PY, "alpha", "alpha_two", "wiring.py", 'T = "app.b." + "alpha"\n', id="limit-plus-concat"
        ),
        pytest.param(
            "src/b.py", _ALPHA_PY, "alpha", "alpha_two", "wiring.py", 'T = f"app.{mod}.alpha"\n', id="limit-f-string"
        ),
        pytest.param(
            "src/b.py",
            _ALPHA_PY,
            "alpha",
            "alpha_two",
            "wiring.py",
            "import src.b as bb\nbb.alpha()\n",
            id="limit-import-alias",
        ),
        pytest.param(
            "src/b.py",
            _ALPHA_PY,
            "alpha",
            "alpha_two",
            "wiring.py",
            'T = "src/b.py::alpha"\n',
            id="limit-pytest-node-id",
        ),
        pytest.param(
            "pkg/__init__.py",
            _ALPHA_PY,
            "alpha",
            "alpha_two",
            "wiring.py",
            'T = "pkg.__init__.alpha"\n',
            id="limit-init-spelled-out",
        ),
    ],
)
def test_a_rust_go_or_python_source_file_without_the_qualified_name_still_batches(
    tmp_path: Path, module: str, source: str, old: str, new: str, path: str, text: str
) -> None:
    rows = _renamed_source(tmp_path, module, source, old, new, {path: text})
    proof = _since(rows, f"{module}::{new}")
    assert proof["routine"] is True and proof["rule"] == "rename_only"


def _two_modules_renaming_alpha(tmp_path: Path) -> _Repo:
    """One head whose ``use.py`` names ``c.alpha``; ``src/b.py`` and ``src/c.py`` both rename their ``alpha``."""

    repo = _Repo(tmp_path)
    _commit_on_head(repo, {"use.py": 'HOOK = "c.alpha"\n'})
    c_py = _ALPHA_PY.replace("v + 1", "v + 2")  # another body, so the two renames are not one pairing
    _write(repo.root, "src/b.py", _ALPHA_PY)
    _write(repo.root, "src/c.py", c_py)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    _write(repo.root, "src/c.py", c_py.replace("alpha", "alpha_two"))
    repo.advance()
    return repo


@pytest.mark.parametrize("order", [("b", "c"), ("c", "b")], ids=["b then c", "c then b"])
def test_two_requests_that_rename_the_same_name_in_two_modules_are_answered_each_by_its_own_stem(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    """The product path: one head, one memo, ``b.alpha`` and ``c.alpha`` asked in turn; only ``c.alpha`` is named."""

    repo = _two_modules_renaming_alpha(tmp_path)
    seen = {mod: _since(repo.targets(f"src/{mod}.py"), f"src/{mod}.py::alpha_two") for mod in order}
    assert seen["b"]["routine"] is True and seen["b"]["rule"] == "rename_only"
    assert seen["c"]["routine"] is False and "Found in use.py." in seen["c"]["detail"]


def test_two_renames_of_one_name_in_one_request_are_read_when_either_is_named(tmp_path: Path) -> None:
    """One search per request asks for the old name with every stem of the request, so the pair is refused together."""

    rows = _two_modules_renaming_alpha(tmp_path).targets()
    for mod in ("b", "c"):
        proof = _since(rows, f"src/{mod}.py::alpha_two")
        assert proof["routine"] is False and "Found in use.py." in proof["detail"]


def test_the_search_is_asked_for_the_stems_of_the_old_and_the_new_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    walks = _Walks(monkeypatch)
    _renamed(tmp_path, module="app/views.py")[0].targets()
    assert walks.kwargs == [{"families": {"alpha": frozenset({"python"})}, "modules": {"alpha": frozenset({"views"})}}]


def test_a_rename_that_also_moves_the_file_is_searched_by_both_stems(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _commit_on_head(repo, {"settings.py": 'HOOK = "old_home.alpha"\n'})
    _write(repo.root, "src/old_home.py", _ALPHA_PY)
    repo.review_everything()
    (repo.root / "src/old_home.py").unlink()
    _write(repo.root, "src/new_home.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    repo.advance()
    proof = _since(repo.targets(), "src/new_home.py::alpha_two")
    assert proof["routine"] is False and "Found in settings.py." in proof["detail"]


def _staged(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.gateway.test_review_since_review as module

    real = module.resolve_rev_range
    monkeypatch.setattr(module, "resolve_rev_range", lambda root, *a, **k: real(root, *a, staged=True, **k))


def _stage_all(repo: _Repo) -> None:
    git = pygit2.Repository(str(repo.root))
    git.index.add_all()
    git.index.write()


@pytest.mark.parametrize(("config", "routine"), [("handler: b.alpha\n", False), ("handler: b.other\n", True)])
def test_a_staged_revision_searches_its_base_tree_and_its_staged_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, routine: bool
) -> None:
    _staged(monkeypatch)
    repo = _Repo(tmp_path)
    _commit_on_head(repo, {"serverless.yml": "handler: b.other\n"})
    _write(repo.root, "src/b.py", _ALPHA_PY)
    _stage_all(repo)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    _write(repo.root, "template.yaml", config)  # staged with the rename, so it is in the Snapshot's changed files
    _stage_all(repo)
    repo.advance()
    revision = repo.store.latest_revision(repo.session.id)
    assert revision is not None and revision.range_mode == "staged"
    proof = _since(repo.targets(), _RENAMED)
    assert proof["routine"] is routine
    if not routine:
        assert "Found in template.yaml." in proof["detail"]


# ------------------------------------------------------------------ one walk per head, answers kept (the memo)


def _renamed_repo(tmp_path: Path, *, committed: dict[str, str] | None = None) -> _Repo:
    repo = _Repo(tmp_path)
    if committed:
        _commit_on_head(repo, committed)
    _write(repo.root, "src/b.py", _ALPHA_PY)
    _write(repo.root, "src/c.py", _GAMMA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    _write(repo.root, "src/c.py", _GAMMA_PY.replace("gamma", "gamma_two"))
    repo.advance()
    return repo


class _Walks:
    """Counts the walks of the product's search, and says how each one goes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, seconds: float | None = None, pause: float = 0.0) -> None:
        self.names: list[set[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.seconds = seconds
        self.pause = pause
        self.real = config_names.search_config_names
        monkeypatch.setattr(config_names, "search_config_names", self)

    def __call__(self, names: Any, head: Any, budget: Any = None, **kwargs: Any) -> Any:
        self.names.append(set(names))
        self.kwargs.append(kwargs)
        time.sleep(self.pause)
        if self.seconds is not None:
            budget = ConfigBudget(seconds=self.seconds)
        return self.real(names, head, budget, **kwargs)


def test_a_search_that_always_runs_out_of_time_walks_twice_and_then_keeps_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's case: five requests for a head that never finishes in time must not walk five times."""

    repo = _renamed_repo(tmp_path)
    walks = _Walks(monkeypatch, seconds=-1.0)
    for _ in range(5):
        proof = _since(repo.targets(), _RENAMED)
        assert proof["routine"] is False and "longer than -1 seconds" in proof["detail"]
    assert len(walks.names) == 2  # the retry, then the refusal is the Snapshot's answer


def test_a_search_that_ran_out_of_time_once_is_asked_again_and_a_later_answer_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _renamed_repo(tmp_path)
    walks = _Walks(monkeypatch, seconds=-1.0)
    assert _since(repo.targets(), _RENAMED)["routine"] is False
    walks.seconds = None  # the load is gone: the next request walks again, finishes, and the rename is routine
    assert _since(repo.targets(), _RENAMED)["routine"] is True
    assert len(walks.names) == 2
    for _ in range(3):  # a finished answer is the Snapshot's: no further walk
        assert _since(repo.targets(), _RENAMED)["routine"] is True
    assert len(walks.names) == 2


def test_a_search_that_hit_a_size_bound_is_kept_for_the_snapshot_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _renamed_repo(tmp_path, committed={"serverless.yml": "handler: b.other\n"})
    real = config_names.search_config_names
    walks: list[Any] = []

    def search(names: Any, head: Any, budget: Any = None, **kwargs: Any) -> Any:
        walks.append(set(names))
        return real(names, head, ConfigBudget(max_files=0), **kwargs)

    monkeypatch.setattr(config_names, "search_config_names", search)
    for _ in range(3):
        assert _since(repo.targets(), _RENAMED)["routine"] is False
    assert len(walks) == 1


def test_a_later_request_walks_only_for_the_names_not_yet_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _renamed_repo(tmp_path, committed={"serverless.yml": "handler: c.gamma\n"})
    walks = _Walks(monkeypatch)
    first = repo.targets("src/b.py")
    assert _since(first, _RENAMED)["routine"] is True and walks.names == [{"alpha"}]
    rows = repo.targets()  # the whole change: gamma is new to the head's memo, alpha is answered
    assert walks.names == [{"alpha"}, {"gamma"}]
    assert _since(rows, "src/c.py::gamma_two")["routine"] is False and _since(rows, _RENAMED)["routine"] is True
    repo.targets()
    repo.targets("src/c.py")
    assert len(walks.names) == 2


def test_requests_that_arrive_together_share_one_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Reader asks for the startup fetch and a path slice at the same time; the head is walked once."""

    repo = _renamed_repo(tmp_path)
    repo.targets()  # build the app and its client outside the race
    walks = _Walks(monkeypatch, pause=0.3)
    # A second Snapshot head would not share; a fresh revision resets the per-Snapshot caches and the memo for it.
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_three"))
    repo.advance()
    results: list[dict[str, dict[str, Any]]] = []
    threads = [
        threading.Thread(target=lambda path=path: results.append(repo.targets(path))) for path in ("", "src/b.py", "")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 3
    assert len(walks.names) == 1, walks.names


def test_requests_that_arrive_together_share_a_timeout_instead_of_walking_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _renamed_repo(tmp_path)
    repo.targets()
    walks = _Walks(monkeypatch, seconds=-1.0, pause=0.3)
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_three"))
    repo.advance()
    threads = [threading.Thread(target=lambda: repo.targets()) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert (
        len(walks.names) == 1
    ), walks.names  # the waiting requests took the walk that timed out, they did not repeat it


class _Memo:
    """The memo on its own, with a search that records each walk."""

    def __init__(self, results: list[config_names.ConfigSearch], pause: float = 0.0) -> None:
        self.results = results
        self.pause = pause
        self.calls: list[tuple[list[str], dict[str, frozenset[str]]]] = []
        self.modules: list[dict[str, frozenset[str]]] = []
        self.memo = config_names.HeadSearchMemo(self.search)

    def search(
        self, names: Any, head: Any, budget: Any = None, *, families: Any = None, modules: Any = None
    ) -> config_names.ConfigSearch:
        time.sleep(self.pause)
        self.calls.append((sorted(names), dict(families or {})))
        self.modules.append(dict(modules or {}))
        return self.results[min(len(self.calls), len(self.results)) - 1]


def _head(_budget: ConfigBudget) -> Iterable[ConfigFile]:
    return ()


def test_the_memo_answers_per_name_and_language_family() -> None:
    cleared = config_names.ConfigSearch({}, searched=7)
    fake = _Memo([cleared, config_names.ConfigSearch({"alpha": "run.sh"}, searched=3)])
    search = fake.memo.searcher("head-1")
    py = {"alpha": frozenset({"python"})}
    assert search({"alpha"}, _head, families=py) == config_names.ConfigSearch({}, "", 7, False, None)
    assert search({"alpha"}, _head, families=py).searched == 7 and len(fake.calls) == 1
    # The same name from a Go file is another question: Python files must be searched for it.
    found = search({"alpha"}, _head, families={"alpha": frozenset({"go"})})
    assert found.named == {"alpha": "run.sh"} and len(fake.calls) == 2
    # Another head has its own memory.
    fake.memo.searcher("head-2")({"alpha"}, _head, families=py)
    assert len(fake.calls) == 3


def test_the_memo_does_not_share_an_answer_between_the_same_name_in_two_modules() -> None:
    """``a.f`` and ``b.f`` are two questions: what was searched for (the stems) is part of the key."""

    fake = _Memo([config_names.ConfigSearch({}, searched=7), config_names.ConfigSearch({"f": "use.py"}, searched=7)])
    search = fake.memo.searcher("head-1")
    py = {"f": frozenset({"python"})}
    a, b = {"f": frozenset({"a"})}, {"f": frozenset({"b"})}
    assert search({"f"}, _head, families=py, modules=a).named == {}
    assert search({"f"}, _head, families=py, modules=b).named == {"f": "use.py"}
    assert len(fake.calls) == 2 and fake.modules == [a, b]
    # Each answer is kept for its own stems: asking either again walks nothing.
    assert search({"f"}, _head, families=py, modules=a).named == {}
    assert search({"f"}, _head, families=py, modules=b).named == {"f": "use.py"}
    assert len(fake.calls) == 2
    # The stems of a rename that moves the file are one question, in any order.
    ab = {"f": frozenset({"a", "b"})}
    search({"f"}, _head, families=py, modules=ab)
    search({"f"}, _head, families=py, modules={"f": frozenset({"b", "a"})})
    assert len(fake.calls) == 3


def test_the_memo_key_would_fail_this_test_if_it_left_the_stems_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation check: with the P12-4 key (name and families only) the second module is answered by the first."""

    monkeypatch.setattr(
        config_names,
        "_answer_key",
        lambda name, families, modules: (name, tuple(sorted(families.get(name, frozenset()))), ()),
    )
    fake = _Memo([config_names.ConfigSearch({}, searched=7), config_names.ConfigSearch({"f": "use.py"}, searched=7)])
    search = fake.memo.searcher("head-1")
    py = {"f": frozenset({"python"})}
    search({"f"}, _head, families=py, modules={"f": frozenset({"a"})})
    reused = search({"f"}, _head, families=py, modules={"f": frozenset({"b"})})
    assert reused.named == {} and len(fake.calls) == 1  # the wrong answer the real key prevents


def test_the_memo_ignores_the_stems_of_a_name_that_is_searched_bare_everywhere() -> None:
    """A name defined in two languages skips no file, so its stems change nothing and its answer is shared."""

    fake = _Memo([config_names.ConfigSearch({}, searched=7)])
    search = fake.memo.searcher("head-1")
    both = {"f": frozenset({"python", "go"})}
    search({"f"}, _head, families=both, modules={"f": frozenset({"a"})})
    search({"f"}, _head, families=both, modules={"f": frozenset({"b"})})
    search({"f"}, _head, families=both)
    assert len(fake.calls) == 1
    # A name of one family with no stems is another question from the same name with stems.
    one = {"f": frozenset({"python"})}
    search({"f"}, _head, families=one)
    search({"f"}, _head, families=one, modules={"f": frozenset({"a"})})
    assert len(fake.calls) == 3


def test_the_memo_walks_for_the_missing_names_only_and_keeps_what_it_found() -> None:
    fake = _Memo([config_names.ConfigSearch({"alpha": "a.yml"}, searched=1), config_names.ConfigSearch({}, searched=4)])
    search = fake.memo.searcher("h")
    assert search({"alpha"}, _head).named == {"alpha": "a.yml"}
    both = search({"alpha", "beta"}, _head)
    assert fake.calls == [(["alpha"], {}), (["beta"], {})]
    assert both.named == {"alpha": "a.yml"} and not both.incomplete


def test_the_memo_retries_a_timeout_once_and_then_keeps_it_for_the_head() -> None:
    timed_out = config_names.ConfigSearch({}, "the search took longer than 10 seconds", 5, True)
    fake = _Memo([timed_out])
    search = fake.memo.searcher("h")
    first = search({"alpha"}, _head)
    assert first.incomplete and first.transient and first.unsearched == {"alpha"}
    second = search({"alpha"}, _head)
    assert second.incomplete and not second.transient  # the second timeout is final
    assert len(fake.calls) == 2
    for _ in range(3):
        assert search({"alpha"}, _head).incomplete
    assert len(fake.calls) == 2
    # A name found before the walk timed out is true and kept at once.
    fake2 = _Memo([config_names.ConfigSearch({"alpha": "a.yml"}, "the search took longer than 10 seconds", 2, True)])
    got = fake2.memo.searcher("h")({"alpha", "beta"}, _head)
    assert got.named == {"alpha": "a.yml"} and got.unsearched == {"beta"}
    assert fake2.memo.searcher("h")({"alpha"}, _head).named == {"alpha": "a.yml"} and len(fake2.calls) == 1


def test_a_name_that_was_cleared_is_not_refused_because_another_name_is_unsearched() -> None:
    fake = _Memo(
        [config_names.ConfigSearch({}, searched=2), config_names.ConfigSearch({}, "there are more than 3 files", 0)]
    )
    search = fake.memo.searcher("h")
    search({"alpha"}, _head)
    mixed = search({"alpha", "beta"}, _head)
    assert mixed.unsearched == {"beta"} and mixed.incomplete


def test_the_memo_is_bounded_by_heads_and_by_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_names, "MAX_MEMO_HEADS", 2)
    monkeypatch.setattr(config_names, "MAX_MEMO_ANSWERS", 2)
    fake = _Memo([config_names.ConfigSearch({}, searched=1)])
    for head in ("a", "b", "c"):
        fake.memo.searcher(head)({"alpha"}, _head)
    assert len(fake.calls) == 3
    fake.memo.searcher("c")({"alpha"}, _head)  # still held
    assert len(fake.calls) == 3
    fake.memo.searcher("a")({"alpha"}, _head)  # evicted, asked again
    assert len(fake.calls) == 4
    fake.memo.searcher("a")({"n1", "n2", "n3"}, _head)  # past the answer cap: answered, not all kept
    assert len(fake.memo._heads["a"].answers) <= 2
    fake.memo.forget("a")
    assert "a" not in fake.memo._heads


def test_the_memo_serves_concurrent_requests_with_one_walk_and_lets_other_heads_pass() -> None:
    fake = _Memo([config_names.ConfigSearch({}, searched=1)], pause=0.3)
    started = time.monotonic()
    threads = [threading.Thread(target=lambda: fake.memo.searcher("same")({"alpha"}, _head)) for _ in range(4)]
    other = threading.Thread(target=lambda: fake.memo.searcher("other")({"alpha"}, _head))
    for thread in (*threads, other):
        thread.start()
    for thread in (*threads, other):
        thread.join()
    assert len(fake.calls) == 2  # one walk for "same", one for "other"
    assert time.monotonic() - started < 0.55  # the two heads walked side by side, not one after the other


# ------------------------------------------------------------------ UTF-16 without a mark, at the head and in the capture


def test_a_committed_utf16_config_without_a_byte_order_mark_is_searched(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    git = pygit2.Repository(str(repo.root))
    (repo.root / "deploy.conf").write_bytes("[svc]\nhandler = b.alpha\n".encode("utf-16-le"))
    git.index.add("deploy.conf")
    git.index.write()
    sig = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000, 0)
    oid = git.create_commit(None, sig, sig, "c", git.index.write_tree(), [git.head.target])
    git.references[git.head.name].set_target(oid)
    _write(repo.root, "src/b.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    repo.advance()
    proof = _since(repo.targets(), _RENAMED)
    assert proof["routine"] is False and "Found in deploy.conf." in proof["detail"]


def test_a_changed_utf16_config_in_the_capture_is_searched_through_the_media_artifact(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/b.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    (repo.root / "deploy.conf").write_bytes("[svc]\nhandler = b.alpha\n".encode("utf-16-le"))
    repo.advance()
    proof = _since(repo.targets(), _RENAMED)
    assert proof["routine"] is False and "Found in deploy.conf." in proof["detail"]


# ------------------------------------------------------------------ the search never runs on the event loop


def test_the_seen_coverage_endpoint_reads_the_head_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every Focus answer posts ``focus_answer`` there, and that handler is ``async``: the walk must not block the loop."""

    repo = _renamed_repo(tmp_path)
    repo.targets()  # builds the app
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_three"))
    repo.advance()  # a new Snapshot: nothing is cached for it
    seen: list[tuple[str, bool]] = []
    real = config_names.search_config_names

    def search(names: Any, head: Any, budget: Any = None, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append((threading.current_thread().name, on_loop))
        return real(names, head, budget, **kwargs)

    monkeypatch.setattr(config_names, "search_config_names", search)
    response = repo.client.post(
        f"/api/reviews/{repo.session.id}/seen-coverage", json={"source": "focus_answer"}, headers=repo.headers
    )
    assert (
        response.status_code == 422
    )  # the body is not a real focus answer: the targets were read before it was refused
    assert seen, "the endpoint did not reach the rename search"
    assert all(not on_loop for _, on_loop in seen), seen
