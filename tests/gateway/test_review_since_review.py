"""P11-2: what changed since the reviewer's own review decides what the Quick check batch may offer in one step.

Three layers:

* the pure rule (:func:`prove_symbol_since_review`): a formatting or comment change, or a rename that is the only
  difference, is routine; everything else -- and everything the rule cannot read -- is not;
* a seeded randomized differential whose ground truth comes from how each edit was *built* (a reformat, a comment or a
  lone rename is routine; a changed literal, operator, line, name collision or half-renamed recursion is behavior) and
  whose headline number is **fail-open == 0**: no edit that changed behavior is ever proved routine;
* the store side: a real two-revision refresh, the reviewer's reviewed text found again by fingerprint, and the
  ``since_review`` field on the targets payload.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import routine, since_review
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.impact import symbol_windows
from lemoncrow.pro.capabilities.review.since_review import SinceReview, prove_symbol_since_review
from lemoncrow.pro.capabilities.review.sources.local import mark_unit, open_or_create_session, refresh
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan
from lemoncrow.pro.capabilities.review.units import symbol_fingerprint


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# ------------------------------------------------------------------ the pure rule

_PY = "def alpha(v):\n    total = v + 1\n    return total\n"
_GO = "package a\n\nfunc Alpha(v int) int {\n\ttotal := v + 1\n\treturn total\n}\n"
_JS = "function alpha(v) {\n  const total = v + 1;\n  return total;\n}\n"
_TS = "function alpha(v: number): number {\n  const total = v + 1;\n  return total;\n}\n"
_RS = "fn alpha(v: i32) -> i32 {\n    let total = v + 1;\n    total\n}\n"
_JAVA = "class C {\n    int alpha(int v) {\n        int total = v + 1;\n        return total;\n    }\n}\n"
_SAMPLES = {
    "py": ("a.py", _PY, "alpha"),
    "go": ("a.go", _GO, "Alpha"),
    "js": ("a.js", _JS, "alpha"),
    "ts": ("a.ts", _TS, "alpha"),
    "rs": ("a.rs", _RS, "alpha"),
    "java": ("A.java", _JAVA, "alpha"),
}


def _span(text: str, path: str, leaf: str) -> tuple[int, int]:
    # The first window of that name: a renamed definition can share its new name with a later one.
    window = next(w for w in symbol_windows(text, path) if re.split(r"[.:]+", w.name)[-1] == leaf)
    return window.start_line, window.end_line


def _prove(
    old: str, new: str, *, path: str, old_name: str, new_name: str | None = None, new_path: str | None = None
) -> SinceReview:
    new_name = new_name or old_name
    return prove_symbol_since_review(
        old_path=path,
        old_text=old,
        old_span=_span(old, path, old_name),
        old_symbol=old_name,
        new_path=new_path or path,
        new_text=new,
        new_span=_span(new, new_path or path, new_name),
        new_symbol=new_name,
    )


def _name_in(text: str, old: str, new: str) -> str:
    return text.replace(old, new)


@pytest.mark.parametrize("language", sorted(_SAMPLES))
def test_the_same_code_and_a_lone_rename_are_routine_in_every_language(language: str) -> None:
    path, text, name = _SAMPLES[language]
    same = _prove(text, text, path=path, old_name=name)
    assert same.routine and same.rule == "formatting_only"
    renamed = _prove(text, _name_in(text, name, name + "Two"), path=path, old_name=name, new_name=name + "Two")
    if language == "java":  # every Java definition is a member of a class: never a rename proof
        assert not renamed.routine and "module-level function" in renamed.detail
        return
    assert renamed.routine and renamed.rule == "rename_only", renamed
    assert renamed.detail.startswith(f"Renamed {name} to {name}Two.")


@pytest.mark.parametrize("language", sorted(_SAMPLES))
def test_a_changed_literal_is_not_routine_in_any_language(language: str) -> None:
    path, text, name = _SAMPLES[language]
    changed = _prove(text, text.replace("v + 1", "v + 2"), path=path, old_name=name)
    assert not changed.routine and changed.rule == ""
    both = _prove(
        text,
        _name_in(text, name, name + "Two").replace("v + 1", "v + 2"),
        path=path,
        old_name=name,
        new_name=name + "Two",
    )
    assert not both.routine, "a rename must not carry a behavior change along with it"


@pytest.mark.parametrize("language", sorted(_SAMPLES))
def test_a_comment_is_routine_in_every_language(language: str) -> None:
    path, text, name = _SAMPLES[language]
    marker = "#" if language == "py" else "//"
    first_body_line = text.splitlines()[
        text.splitlines().index(next(line for line in text.splitlines() if "total" in line))
    ]
    commented = text.replace(first_body_line, f"{first_body_line}  {marker} the next one", 1)
    proof = _prove(text, commented, path=path, old_name=name)
    assert proof.routine and proof.rule == "comment_only", proof


@pytest.mark.parametrize("language", ["go", "js", "ts", "rs", "java"])
def test_a_reformat_is_routine_where_layout_does_not_carry_meaning(language: str) -> None:
    path, text, name = _SAMPLES[language]
    reformatted = text.replace("v + 1", "v+1").replace("\n  ", "\n      ").replace("\n    ", "\n        ")
    assert reformatted != text
    proof = _prove(text, reformatted, path=path, old_name=name)
    assert proof.routine and proof.rule == "formatting_only", proof


def test_a_python_reflow_is_never_a_formatting_proof() -> None:
    reflowed = _PY.replace("v + 1", "v+1")
    assert not _prove(_PY, reflowed, path="a.py", old_name="alpha").routine
    indented = _PY.replace("    total", "      total").replace("    return", "      return")
    assert not _prove(_PY, indented, path="a.py", old_name="alpha").routine


_FIB = "def fib(n):\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\n"


@pytest.mark.parametrize(
    ("case", "old", "new", "old_name", "new_name"),
    [
        pytest.param(
            "old name used again", _FIB, _FIB.replace("def fib", "def fibonacci"), "fib", "fibonacci", id="half"
        ),
        pytest.param(
            "every use renamed",
            _FIB,
            _FIB.replace("fib", "fibonacci"),
            "fib",
            "fibonacci",
            id="all-uses-renamed",
        ),
        pytest.param(
            "new name already in the body",
            "def alpha(v):\n    total = v + 1\n    return total\n",
            "def total(v):\n    total = v + 1\n    return total\n",
            "alpha",
            "total",
            id="collision",
        ),
        pytest.param(
            "old name inside a string",
            'def alpha(v):\n    return "alpha:" + str(v)\n',
            'def beta(v):\n    return "alpha:" + str(v)\n',
            "alpha",
            "beta",
            id="in-a-string",
        ),
        pytest.param(
            "old name as an attribute",
            "def alpha(v):\n    return v.alpha\n",
            "def beta(v):\n    return v.alpha\n",
            "alpha",
            "beta",
            id="attribute",
        ),
        pytest.param(
            "name as a substring of another word",
            "def alpha(v):\n    alphabet = v\n    return alphabet\n",
            "def beta(v):\n    alphabet = v\n    return alphabet\n",
            "alpha",
            "beta",
            id="substring-is-not-a-use",
        ),
    ],
)
def test_a_rename_alone_is_not_proof(case: str, old: str, new: str, old_name: str, new_name: str) -> None:
    proof = _prove(old, new, path="a.py", old_name=old_name, new_name=new_name)
    if case == "name as a substring of another word":
        # ``alphabet`` is a different word: this one is a lone rename and may be routine.
        assert proof.routine and proof.rule == "rename_only"
    else:
        assert not proof.routine, case


def test_a_qualified_rename_must_keep_its_enclosing_class() -> None:
    old = "class A:\n    def alpha(self):\n        return 1\n"
    assert not _prove(old, old.replace("alpha", "beta"), path="a.py", old_name="alpha", new_name="beta").routine
    # The enclosing name is part of the symbol: a different parent is a different part.
    proof = prove_symbol_since_review(
        old_path="a.py",
        old_text=old,
        old_span=_span(old, "a.py", "alpha"),
        old_symbol="A.alpha",
        new_path="a.py",
        new_text=old.replace("alpha", "beta").replace("class A", "class B"),
        new_span=_span(old.replace("alpha", "beta"), "a.py", "beta"),
        new_symbol="B.beta",
    )
    assert not proof.routine


_USES = "\n\ndef use(xs):\n    return len(xs)\n"


@pytest.mark.parametrize(
    ("case", "old", "new", "new_name"),
    [
        pytest.param(
            "a builtin the module's other code reads",
            _PY + _USES,
            (_PY + _USES).replace("def alpha", "def len"),
            "len",
            id="python-builtin",
        ),
        pytest.param(
            "another definition of the new name in the file",
            _PY + "\n\ndef beta():\n    return 1\n",
            (_PY + "\n\ndef beta():\n    return 1\n").replace("def alpha", "def beta", 1),
            "beta",
            id="new-name-defined-elsewhere",
        ),
        pytest.param(
            "a caller in the file still uses the old name",
            _PY + "\n\ndef use():\n    return alpha(1)\n",
            (_PY + "\n\ndef use():\n    return alpha(1)\n").replace("def alpha", "def beta", 1),
            "beta",
            id="stale-caller",
        ),
        pytest.param(
            "a caller in the file uses the new name",
            _PY + "\n\ndef use():\n    return alpha(1)\n",
            (_PY + "\n\ndef use():\n    return alpha(1)\n").replace("alpha", "beta"),
            "beta",
            id="caller-renamed-with-it",
        ),
        pytest.param(
            "a dunder name",
            "class A:\n    def alpha(self):\n        return 1\n",
            "class A:\n    def __call__(self):\n        return 1\n",
            "__call__",
            id="dunder",
        ),
        pytest.param(
            "a test the runner stops collecting",
            "def test_alpha():\n    assert 1\n",
            "def alpha():\n    assert 1\n",
            "alpha",
            id="test-prefix-removed",
        ),
        pytest.param(
            "a function the runner starts collecting",
            "def alpha():\n    assert 1\n",
            "def test_alpha():\n    assert 1\n",
            "test_alpha",
            id="test-prefix-added",
        ),
    ],
)
def test_a_rename_that_can_change_what_other_code_reads_is_not_batched(
    case: str, old: str, new: str, new_name: str
) -> None:
    old_name = "test_alpha" if "def test_alpha" in old else "alpha"
    proof = _prove(old, new, path="a.py", old_name=old_name, new_name=new_name)
    assert not proof.routine, case
    assert proof.rule == ""


def test_a_rename_to_a_name_the_language_defines_is_refused_in_other_languages_too() -> None:
    go = _GO + "\nfunc Use(xs []int) int {\n\treturn len(xs)\n}\n"
    assert not _prove(go, go.replace("Alpha", "len"), path="a.go", old_name="Alpha", new_name="len").routine
    js = _JS + "\nfunction use(a) {\n  return a;\n}\n"
    assert not _prove(js, js.replace("alpha", "Object"), path="a.js", old_name="alpha", new_name="Object").routine
    rust = _RS + "\nfn use_it() -> Option<i32> {\n    None\n}\n"
    assert not _prove(rust, rust.replace("alpha", "Some"), path="a.rs", old_name="alpha", new_name="Some").routine
    assert not _prove(
        _GO, _GO.replace("Alpha", "TestAlpha"), path="a.go", old_name="Alpha", new_name="TestAlpha"
    ).routine


def _two(old: str, new: str, path: str, old_name: str, new_name: str) -> SinceReview:
    return _prove(old, new, path=path, old_name=old_name, new_name=new_name)


# Each case keeps the body and renames a definition that something other than a caller in this file reads by name.
_PROTOCOL_RENAMES = [
    pytest.param(
        "a.go",
        'package a\n\ntype T struct{}\n\nfunc (t T) String() string {\n\treturn "t"\n}\n',
        "String",
        "Str",
        id="go-stringer",
    ),
    pytest.param(
        "A.java",
        'class A {\n    public String toString() {\n        return "a";\n    }\n}\n',
        "toString",
        "asText",
        id="java-toString",
    ),
    pytest.param(
        "a.js", "class A {\n  toString() {\n    return 'a';\n  }\n}\n", "toString", "asText", id="js-toString"
    ),
    pytest.param("a.js", "class A {\n  then(r) {\n    r(1);\n  }\n}\n", "then", "next", id="js-thenable"),
    pytest.param(
        "a.py",
        "class T(unittest.TestCase):\n    def setUp(self):\n        self.x = 1\n",
        "setUp",
        "set_up",
        id="python-setUp",
    ),
    pytest.param(
        "a.jsx",
        "class C extends React.Component {\n  render() {\n    return <div/>;\n  }\n}\n",
        "render",
        "draw",
        id="react-render",
    ),
    pytest.param("a.go", "package a\n\nfunc Helper() int {\n\treturn 1\n}\n", "Helper", "helper", id="go-unexported"),
    pytest.param("a.go", "package a\n\nfunc helper() int {\n\treturn 1\n}\n", "helper", "Helper", id="go-exported"),
    # A free function of a protocol name: no longer a method, but the name is still looked up by something else.
    pytest.param("a.js", "function then(r) {\n  r(1);\n}\n", "then", "next", id="js-free-then"),
    pytest.param("a.js", "function render() {\n  return 1;\n}\n", "render", "draw", id="js-free-render"),
    pytest.param("a.py", "def setUp():\n    return 1\n", "setUp", "set_up", id="python-free-setUp"),
    pytest.param(
        "a.py", "def tearDownClass():\n    return 1\n", "tearDownClass", "tear_down", id="python-free-tearDownClass"
    ),
    pytest.param(
        "a.go", 'package a\n\nfunc String() string {\n\treturn "t"\n}\n', "String", "Text", id="go-free-String"
    ),
    pytest.param("a.js", "function toJSON() {\n  return 1;\n}\n", "toJSON", "asJson", id="js-free-toJSON"),
    pytest.param("a.js", "function getIt(u) {\n  return u;\n}\n", "getIt", "fetch", id="js-onto-the-global-fetch"),
    pytest.param(
        "A.java",
        "class A {\n    boolean equals(Object o) {\n        return true;\n    }\n}\n",
        "equals",
        "same",
        id="java-equals",
    ),
    pytest.param(
        "a.js", "function constructor() {\n  return 1;\n}\n", "constructor", "build", id="js-free-constructor"
    ),
    pytest.param("a.py", "def main():\n    return 1\n", "main", "run", id="python-free-main"),
    pytest.param("a.py", "def init():\n    return 1\n", "init", "setup", id="python-free-init"),
]


@pytest.mark.parametrize(("path", "old", "old_name", "new_name"), _PROTOCOL_RENAMES)
def test_a_rename_away_from_a_name_something_else_looks_up_is_read(
    path: str, old: str, old_name: str, new_name: str
) -> None:
    proof = _two(old, old.replace(old_name, new_name), path, old_name, new_name)
    assert not proof.routine and proof.rule == "", proof
    # The same rename of an ordinary module-level function is still a rename (the refusal is about the name and the kind).
    plain = _prove(_PY, _PY.replace("alpha", "gamma"), path="a.py", old_name="alpha", new_name="gamma")
    assert plain.routine and plain.rule == "rename_only"


@pytest.mark.parametrize(
    ("path", "old", "old_name", "old_symbol", "new_name"),
    [
        pytest.param(
            "a.py",
            "class A:\n    def alpha(self):\n        return 1\n",
            "alpha",
            "A.alpha",
            "gamma",
            id="python-method",
        ),
        pytest.param(
            "a.go",
            "package a\n\ntype T struct{}\n\nfunc (t T) Alpha() int {\n\treturn 1\n}\n",
            "Alpha",
            "Alpha",
            "Gamma",
            id="go-receiver",
        ),
        pytest.param(
            "a.rs",
            "struct S;\nimpl S {\n    fn alpha(&self) -> i32 {\n        1\n    }\n}\n",
            "alpha",
            "S.alpha",
            "gamma",
            id="rust-impl",
        ),
        pytest.param(
            "A.java",
            "class A {\n    int alpha() {\n        return 1;\n    }\n}\n",
            "alpha",
            "A.alpha",
            "gamma",
            id="java-method",
        ),
        pytest.param(
            "a.js", "class A {\n  alpha() {\n    return 1;\n  }\n}\n", "alpha", "A.alpha", "gamma", id="js-method"
        ),
        pytest.param(
            "a.ts",
            "class A {\n  alpha() {\n    return 1;\n  }\n}\n",
            "alpha",
            "alpha",
            "gamma",
            id="ts-method-bare-name",
        ),
        pytest.param(
            "a.py",
            "def outer():\n    def alpha():\n        return 1\n    return alpha\n",
            "alpha",
            "outer.alpha",
            "gamma",
            id="python-nested",
        ),
        pytest.param("a.py", "class Alpha:\n    x = 1\n", "Alpha", "Alpha", "Gamma", id="python-class"),
        pytest.param("a.js", "class Alpha {\n  x = 1;\n}\n", "Alpha", "Alpha", "Gamma", id="js-class"),
        pytest.param(
            "a.js", "const alpha = { x: 1 };\n", "alpha", "alpha", "gamma", id="js-const-that-is-not-a-function"
        ),
    ],
)
def test_a_rename_of_anything_but_a_module_level_function_is_read(
    path: str, old: str, old_name: str, old_symbol: str, new_name: str
) -> None:
    new = old.replace(old_name, new_name)
    new_symbol = old_symbol.replace(old_name, new_name)
    proof = prove_symbol_since_review(
        old_path=path,
        old_text=old,
        old_span=_span(old, path, old_name),
        old_symbol=old_symbol,
        new_path=path,
        new_text=new,
        new_span=_span(new, path, new_name),
        new_symbol=new_symbol,
    )
    assert not proof.routine and proof.rule == "", proof


@pytest.mark.parametrize(
    ("path", "old", "old_name", "new_name"),
    [
        pytest.param("a.go", "package a\n\nfunc alpha() int {\n\treturn 1\n}\n", "alpha", "gamma", id="go"),
        pytest.param("a.rs", "fn alpha() -> i32 {\n    1\n}\n", "alpha", "gamma", id="rust"),
        pytest.param("a.js", "function alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="js-function"),
        pytest.param("a.js", "const alpha = () => 1;\n", "alpha", "gamma", id="js-arrow"),
        pytest.param("a.ts", "function alpha(): number {\n  return 1;\n}\n", "alpha", "gamma", id="ts-function"),
        pytest.param(
            "a.rs",
            'pub extern "C" fn alpha() -> i32 {\n    1\n}\n',
            "alpha",
            "gamma",
            id="rust-extern-without-attribute",
        ),
        pytest.param("conftestish.py", "def alpha():\n    return 1\n", "alpha", "gamma", id="python-not-conftest"),
        pytest.param("a.py", "def prepare():\n    return 1\n", "prepare", "gamma", id="python-ordinary-name"),
        pytest.param("a.py", "async def alpha():\n    return 1\n", "alpha", "gamma", id="python-async"),
    ],
)
def test_a_module_level_function_is_still_a_rename(path: str, old: str, old_name: str, new_name: str) -> None:
    proof = _prove(old, old.replace(old_name, new_name), path=path, old_name=old_name, new_name=new_name)
    assert proof.routine and proof.rule == "rename_only", proof


# A module-level function that a framework or a tool finds by its name. Every case renames it with the body untouched.
_FOUND_BY_NAME = [
    # (a) a decorator or an attribute above the definition registers it
    pytest.param(
        "db_test.py",
        "import pytest\n\n@pytest.fixture\ndef db():\n    return 1\n",
        "db",
        "database",
        id="a-pytest-fixture",
    ),
    pytest.param("app.py", "@app.route('/x')\ndef alpha():\n    return 1\n", "alpha", "gamma", id="a-flask-route"),
    pytest.param(
        "a.rs",
        '#[no_mangle]\npub extern "C" fn add(a: i32) -> i32 {\n    a\n}\n',
        "add",
        "plus",
        id="a-rust-no-mangle",
    ),
    pytest.param(
        "a.rs", '#[export_name = "x"]\npub fn alpha() -> i32 {\n    1\n}\n', "alpha", "gamma", id="a-rust-export-name"
    ),
    # (b) a JS or TS export, and a member of a TS namespace
    pytest.param(
        "route.ts",
        "export async function GET(req: Request) {\n  return new Response('x');\n}\n",
        "GET",
        "get",
        id="b-nextjs-route-handler",
    ),
    pytest.param(
        "page.tsx",
        "export async function getServerSideProps() {\n  return { props: {} };\n}\n",
        "getServerSideProps",
        "loadProps",
        id="b-nextjs-getServerSideProps",
    ),
    pytest.param("a.ts", "export function alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="b-export-function"),
    pytest.param(
        "a.js", "export default function alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="b-export-default"
    ),
    pytest.param("a.ts", "export const alpha = () => 1;\n", "alpha", "gamma", id="b-export-arrow"),
    # (b) ``export`` on a line of its own, or behind a comment: the definition's window starts at ``function``
    pytest.param(
        "a.ts", "export\nfunction alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="b-export-on-its-own-line"
    ),
    pytest.param(
        "a.js",
        "export /* api */\nfunction alpha() {\n  return 1;\n}\n",
        "alpha",
        "gamma",
        id="b-export-then-comment-line",
    ),
    pytest.param(
        "a.ts", "export default\nfunction alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="b-export-default-split"
    ),
    pytest.param("a.ts", "export\nconst alpha = () => 1;\n", "alpha", "gamma", id="b-export-arrow-split"),
    pytest.param(
        "a.ts", "export /* c */\nfunction alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="b-export-block-comment-ts"
    ),
    pytest.param(
        "a.ts", "export // why\nfunction alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="b-export-line-comment-ts"
    ),
    pytest.param(
        "a.tsx",
        "export /* c */\nfunction alpha() {\n  return 1;\n}\n",
        "alpha",
        "gamma",
        id="b-export-block-comment-tsx",
    ),
    pytest.param(
        "a.tsx", "export // why\nfunction alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="b-export-line-comment-tsx"
    ),
    pytest.param(
        "a.js", "export // why\nfunction alpha() {\n  return 1;\n}\n", "alpha", "gamma", id="b-export-line-comment-js"
    ),
    # (d) a host reads ``onmessage`` / ``onload`` by name in a classic script or a worker
    pytest.param("w.js", "function handle(e) {\n  return e.data;\n}\n", "handle", "onmessage", id="d-worker-onmessage"),
    pytest.param("a.js", "function boot() {\n  return 1;\n}\n", "boot", "onload", id="d-window-onload"),
    pytest.param("a.ts", "function onerror(e) {\n  return e;\n}\n", "onerror", "report", id="d-away-from-onerror"),
    pytest.param(
        "n.ts",
        "export namespace N {\n  export function alpha(v: number): number {\n    return v + 1;\n  }\n}\n",
        "alpha",
        "gamma",
        id="b-ts-namespace-member",
    ),
    pytest.param(
        "n.ts",
        "namespace N {\n  function alpha(v: number): number {\n    return v + 1;\n  }\n}\n",
        "alpha",
        "gamma",
        id="b-ts-namespace-private-member",
    ),
    # (c) a Python name a runner looks up in the module
    pytest.param(
        "conftest.py",
        "def pytest_configure(config):\n    config.x = 1\n",
        "pytest_configure",
        "configure",
        id="c-pytest-hook-old",
    ),
    pytest.param(
        "helpers.py",
        "def configure(config):\n    config.x = 1\n",
        "configure",
        "pytest_configure",
        id="c-pytest-hook-new",
    ),
    pytest.param(
        "test_a.py", "def setup_module():\n    return 1\n", "setup_module", "prepare", id="c-setup-module-old"
    ),
    pytest.param(
        "test_a.py", "def prepare():\n    return 1\n", "prepare", "teardown_module", id="c-teardown-module-new"
    ),
    pytest.param("test_a.py", "def setup():\n    return 1\n", "setup", "prepare", id="c-nose-setup"),
    pytest.param("test_a.py", "def prepare():\n    return 1\n", "prepare", "teardown", id="c-nose-teardown"),
    pytest.param(
        "test_a.py",
        "def load_tests(loader, tests, pattern):\n    return tests\n",
        "load_tests",
        "build_tests",
        id="c-load-tests",
    ),
    pytest.param("test_a.py", "def setUpModule():\n    return 1\n", "setUpModule", "prepare", id="c-setUpModule"),
    pytest.param("conftest.py", "def alpha():\n    return 1\n", "alpha", "gamma", id="c-anything-in-conftest"),
    pytest.param(
        "tests/conftest.py", "def alpha():\n    return 1\n", "alpha", "gamma", id="c-anything-in-a-nested-conftest"
    ),
    # Test runners that find a Go function by its name, and the entry points every language reserves.
    pytest.param("a_test.go", "package a\n\nfunc TestAlpha() {\n}\n", "TestAlpha", "Check", id="go-test"),
    pytest.param(
        "a_test.go", "package a\n\nfunc BenchmarkAlpha() {\n}\n", "BenchmarkAlpha", "Bench", id="go-benchmark"
    ),
    pytest.param("a_test.go", "package a\n\nfunc ExampleAlpha() {\n}\n", "ExampleAlpha", "Sample", id="go-example"),
    pytest.param("a_test.go", "package a\n\nfunc FuzzAlpha() {\n}\n", "FuzzAlpha", "Probe", id="go-fuzz"),
    pytest.param("a.go", "package a\n\nfunc init() {\n}\n", "init", "setup", id="go-init"),
    pytest.param("a.go", "package main\n\nfunc main() {\n}\n", "main", "run", id="go-main"),
    pytest.param("a.go", "package a\n\nfunc setup() {\n}\n", "setup", "init", id="go-to-init"),
]


@pytest.mark.parametrize(("path", "old", "old_name", "new_name"), _FOUND_BY_NAME)
def test_a_module_level_function_something_finds_by_name_is_read_after_a_rename(
    path: str, old: str, old_name: str, new_name: str
) -> None:
    proof = _two(old, old.replace(old_name, new_name), path, old_name, new_name)
    assert not proof.routine and proof.rule == "", proof


def test_a_rename_that_only_a_configuration_file_can_see_is_still_offered_with_its_names() -> None:
    # The pure rule sees one file. A Lambda handler string or a pyproject entry point is refused one level up, per request,
    # by searching the config files at the head (tests/gateway/test_review_config_names.py).
    old = "def handler(event, context):\n    return 1\n"
    proof = _two(old, old.replace("handler", "handle"), "app.py", "handler", "handle")
    assert proof.routine and proof.rule == "rename_only"
    assert proof.detail.startswith("Renamed handler to handle.")


def test_a_clean_rename_says_that_callers_in_other_files_were_not_checked() -> None:
    proof = _prove(_PY, _PY.replace("alpha", "gamma"), path="a.py", old_name="alpha", new_name="gamma")
    assert proof.routine and proof.rule == "rename_only"
    assert "Callers in other files are not checked." in proof.detail
    commented = _PY.replace("alpha", "gamma").replace("v + 1", "v + 1  # why")
    both = _prove(_PY, commented, path="a.py", old_name="alpha", new_name="gamma")
    assert both.routine and both.rule == "comment_only"
    assert "Callers in other files are not checked." in both.detail


_DOC = "def alpha():\n    return '''a \nb'''\n"


def test_a_trailing_space_inside_a_multi_line_string_is_content_not_formatting() -> None:
    assert not _prove(_DOC, _DOC.replace("a \nb", "a\nb"), path="a.py", old_name="alpha").routine
    renamed = _DOC.replace("alpha", "gamma").replace("a \nb", "a\nb")
    assert not _prove(_DOC, renamed, path="a.py", old_name="alpha", new_name="gamma").routine
    # The same body, renamed and nothing else, is still a rename.
    assert _prove(_DOC, _DOC.replace("alpha", "gamma"), path="a.py", old_name="alpha", new_name="gamma").routine
    js = "function alpha() {\n  return `a \nb`;\n}\n"
    assert not _prove(js, js.replace("a \nb", "a\nb"), path="a.js", old_name="alpha").routine


def test_a_decorator_or_attribute_above_the_definition_is_part_of_the_comparison() -> None:
    assert not _prove(_PY, "@cache\n" + _PY, path="a.py", old_name="alpha").routine
    assert not _prove("@cache\n" + _PY, _PY, path="a.py", old_name="alpha").routine
    assert _prove("@cache\n" + _PY, "@cache\n" + _PY, path="a.py", old_name="alpha").routine
    rust = "#[inline]\n" + _RS
    assert not _prove(rust, _RS, path="a.rs", old_name="alpha").routine
    assert not _prove(_RS, "#[test]\n" + _RS, path="a.rs", old_name="alpha").routine


def _direct(path: str, old: str, new: str, *, new_path: str | None = None) -> SinceReview:
    """The rule with explicit whole-body spans, for files no symbol parser reads."""

    return prove_symbol_since_review(
        old_path=path,
        old_text=old,
        old_span=(1, len(old.splitlines())),
        old_symbol="alpha",
        new_path=new_path or path,
        new_text=new,
        new_span=(1, len(new.splitlines())),
        new_symbol="alpha",
    )


def test_a_part_that_changed_language_is_not_compared() -> None:
    assert not _direct("a.py", _PY, _PY, new_path="a.go").routine


def test_a_file_with_no_parser_proves_only_the_very_same_code() -> None:
    assert _direct("a.rb", _PY, _PY).routine
    assert not _direct("a.rb", _PY, _PY.replace("v + 1", "v+1")).routine
    assert not _direct("a.rb", _PY, _PY.replace("v + 1", "v + 1  # why")).routine


@pytest.mark.parametrize("exotic", ["\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029", "\r"])
def test_a_line_break_the_two_line_counters_would_disagree_on_is_never_proved(exotic: str) -> None:
    old = _GO
    new = _GO.replace("\treturn total", f"\t// note{exotic}\treturn total")
    proof = prove_symbol_since_review(
        old_path="a.go",
        old_text=old,
        old_span=(3, 6),
        old_symbol="Alpha",
        new_path="a.go",
        new_text=new,
        new_span=(3, 6),
        new_symbol="Alpha",
    )
    assert not proof.routine


def test_a_crlf_file_is_read_like_an_lf_file() -> None:
    old, new = _GO.replace("\n", "\r\n"), _GO.replace("v + 1", "v+1").replace("\n", "\r\n")
    proof = prove_symbol_since_review(
        old_path="a.go",
        old_text=old,
        old_span=(3, 6),
        old_symbol="Alpha",
        new_path="a.go",
        new_text=new,
        new_span=(3, 6),
        new_symbol="Alpha",
    )
    assert proof.routine and proof.rule == "formatting_only"
    behavior = _GO.replace("v + 1", "v + 2").replace("\n", "\r\n")
    assert not prove_symbol_since_review(
        old_path="a.go",
        old_text=old,
        old_span=(3, 6),
        old_symbol="Alpha",
        new_path="a.go",
        new_text=behavior,
        new_span=(3, 6),
        new_symbol="Alpha",
    ).routine


@pytest.mark.parametrize("span", [(0, 3), (3, 2), (1, 99), (-1, 2)])
def test_spans_outside_the_file_are_refused_not_raised(span: tuple[int, int]) -> None:
    for old_span, new_span in ((span, (1, 3)), ((1, 3), span)):
        proof = prove_symbol_since_review(
            old_path="a.py",
            old_text=_PY,
            old_span=old_span,
            old_symbol="alpha",
            new_path="a.py",
            new_text=_PY,
            new_span=new_span,
            new_symbol="alpha",
        )
        assert not proof.routine


def test_a_change_outside_the_window_cannot_ride_in_with_a_formatting_proof() -> None:
    """The proof is for the whole file with only this window swapped back: another difference fails it."""

    old = _GO + "\nfunc Other() int {\n\treturn 1\n}\n"
    new = _GO.replace("v + 1", "v+1") + "\nfunc Other() int {\n\treturn 2\n}\n"
    proof = prove_symbol_since_review(
        old_path="a.go",
        old_text=old,
        old_span=(3, 6),
        old_symbol="Alpha",
        new_path="a.go",
        new_text=new,
        new_span=(3, 6),
        new_symbol="Alpha",
    )
    # Alpha's own window differs only by spacing, and the splice puts the reviewed Alpha into the *new* file, so the
    # unrelated edit to Other is the same on both sides and cannot make this symbol's proof wrong either way.
    assert proof.routine and proof.rule == "formatting_only"


# ------------------------------------------------------------------ the randomized differential

_COMMENT = {"py": "#", "go": "//", "js": "//", "ts": "//", "rs": "//", "java": "//"}
_RECURSIVE = {
    "py": ("a.py", "def alpha(n):\n    if n < 2:\n        return n\n    return alpha(n - 1) + alpha(n - 2)\n"),
    "go": (
        "a.go",
        "package a\n\nfunc Alpha(n int) int {\n\tif n < 2 {\n\t\treturn n\n\t}\n\treturn Alpha(n-1) + Alpha(n-2)\n}\n",
    ),
    "js": (
        "a.js",
        "function alpha(n) {\n  if (n < 2) {\n    return n;\n  }\n  return alpha(n - 1) + alpha(n - 2);\n}\n",
    ),
}
_STRINGY = {
    "py": 'def alpha(v):\n    label = "a b"\n    total = v + 1\n    return label, total\n',
    "go": 'package a\n\nfunc Alpha(v int) string {\n\tlabel := "a b"\n\ttotal := v + 1\n\treturn label + string(total)\n}\n',
    "js": 'function alpha(v) {\n  const label = "a b";\n  const total = v + 1;\n  return label + total;\n}\n',
    "ts": 'function alpha(v: number): string {\n  const label = "a b";\n  const total = v + 1;\n  return label + total;\n}\n',
    "rs": 'fn alpha(v: i32) -> String {\n    let label = "a b";\n    let total = v + 1;\n    format!("{}{}", label, total)\n}\n',
    "java": 'class C {\n    String alpha(int v) {\n        String label = "a b";\n        int total = v + 1;\n        return label + total;\n    }\n}\n',
}
_PATH = {"py": "a.py", "go": "a.go", "js": "a.js", "ts": "a.ts", "rs": "a.rs", "java": "A.java"}
_STYLED = ("py", "go", "js", "ts", "rs", "java")
_HEADERS = {"py": "@cache", "rs": "#[inline]", "java": "@Override"}
_NAMES = ["beta", "gamma", "delta", "omega"]
_ROUTINE_OPS = (
    "comment_line",
    "trailing_comment",
    "reindent",
    "respace",
    "rename",
    "rename_and_comment",
    "same",
    # Behavior-preserving edits the rule is allowed to be too careful about (fail-safe, never fail-open):
    "recursive_all_renamed",
    "python_reflow",
    # Block comments, also holding a newline where that is harmless (the next token already follows a line end).
    "block_comment",
    "block_comment_multiline",
    "block_inline",
)
_BEHAVIOR_OPS = (
    "literal",
    "operator",
    "delete_line",
    "add_statement",
    "comment_out",
    "string_content",
    "rename_and_literal",
    "rename_collision",
    "rename_half_recursive",
    "header",
    # A line terminator, real or inside a block comment, after ``return`` ends the statement: ASI.
    "asi_comment_newline",
    "asi_newline",
)


def _leaf(name: str, language: str) -> str:
    return name.capitalize() if language == "go" else name


def _body_lines(text: str, path: str, name: str) -> tuple[list[str], int, int]:
    start, end = _span(text, path, name)
    return text.split("\n"), start, end


def _code_lines(lines: list[str], start: int, end: int, language: str) -> list[int]:
    """Indexes of the lines strictly inside the window that carry code and no string or comment text."""

    marker = _COMMENT[language]
    return [
        i
        for i in range(start, end - 1)  # 0-based: start is the line after the def line; end-1 excludes the closer
        if lines[i].strip()
        and '"' not in lines[i]
        and marker not in lines[i]
        and lines[i].strip() not in ("}", "{", "end")
    ]


def _tokens(text: str, language: str, rename: tuple[str, str] | None) -> list[str]:
    """An independent oracle for routine edits: the code's tokens with comments dropped and the one name mapped."""

    marker = re.escape(_COMMENT[language])
    stripped = re.sub(rf"{marker}[^\n]*", "", text)
    if language != "py":
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    tokens = re.findall(r"[\w$]+|\"[^\"]*\"|==|<=|>=|->|=>|:=|[^\s\w]", stripped)
    if rename:
        tokens = [rename[1] if t == rename[0] else t for t in tokens]
    return tokens


def _edit(language: str, op: str, rng: random.Random) -> tuple[str, str, str, str, str, str]:
    """``(path, old_text, new_text, old_name, new_name, truth)`` for one edit built from *op*."""

    path = _PATH[language]
    if op in {"rename_half_recursive", "recursive_all_renamed"} and language not in _RECURSIVE:
        op = "rename"  # no recursive sample for this language: a lone rename, which is routine
    if op == "python_reflow" and language != "py":
        op = "respace"
    if op in {"asi_comment_newline", "asi_newline"} and language not in {"js", "ts", "go"}:
        op = "block_comment_multiline"
    if op in {"block_comment", "block_comment_multiline", "block_inline"} and language == "py":
        op = "trailing_comment"
    if op in {"rename_half_recursive", "recursive_all_renamed"}:
        path, old = _RECURSIVE[language]
    elif op in {"string_content"} or (
        rng.random() < 0.3 and op not in {"literal", "operator", "asi_comment_newline", "asi_newline"}
    ):
        old = _STRINGY[language]
    else:
        old = _SAMPLES[language][1]
    name = _leaf("alpha", language)
    lines, start, end = _body_lines(old, path, name)
    code = _code_lines(lines, start, end, language) or [start]
    marker = _COMMENT[language]
    new, new_name, truth = old, name, "routine"
    if op == "same":
        pass
    elif op == "recursive_all_renamed":
        new_name = _leaf(rng.choice(_NAMES), language)
        new = re.sub(rf"(?<![\w$]){name}(?![\w$])", new_name, old)
    elif op == "python_reflow":
        new = old.replace("v + 1", "v+1").replace("n - 1", "n-1")
    elif op in {"block_comment", "block_comment_multiline"}:
        i = rng.choice(code)
        comment = "/* note */" if op == "block_comment" else "/*\n  note\n  */"
        new = "\n".join([*lines[:i], f"{lines[i]} {comment}", *lines[i + 1 :]])
    elif op == "block_inline":
        new = old.replace(" + ", " /* inline */ + ", 1)
    elif op == "asi_comment_newline":
        new, truth = old.replace("return total", "return /*\n*/ total", 1), "behavior"
    elif op == "asi_newline":
        new, truth = old.replace("return total", "return\ntotal", 1), "behavior"
    elif op == "comment_line":
        i = rng.choice(code)
        indent = re.match(r"\s*", lines[i]).group(0)  # type: ignore[union-attr]
        new = "\n".join([*lines[:i], f"{indent}{marker} {rng.choice(['why', 'note', 'todo later'])}", *lines[i:]])
    elif op in {"trailing_comment", "rename_and_comment"}:
        i = rng.choice(code)
        edited = [*lines[:i], f"{lines[i]}  {marker} {rng.choice(['why', 'note'])}", *lines[i + 1 :]]
        new = "\n".join(edited)
    elif op == "reindent":
        if language == "py":
            return _edit(language, "comment_line", rng)
        unit = rng.choice(["  ", "        ", "\t"])
        edited = [
            (
                lines[i]
                if not (start - 1 < i < end)
                else re.sub(r"^\s+", lambda m: unit * (len(m.group(0)) // 2 or 1), lines[i])
            )
            for i in range(len(lines))
        ]
        new = "\n".join(edited)
    elif op == "respace":
        if language == "py":
            return _edit(language, "trailing_comment", rng)
        new = old.replace("v + 1", "v+1").replace("n - 1", "n-1").replace("n - 2", "n-2")
    if op in {"rename", "rename_and_comment", "rename_and_literal", "rename_collision", "rename_half_recursive"}:
        new_name = _leaf(rng.choice(_NAMES), language)
        if op == "rename_collision":
            new_name = "total"
        if op == "rename_half_recursive":
            # the definition takes the new name, the recursive calls keep pointing at the old one
            new = (
                new.replace(f"def {name}(", f"def {new_name}(")
                .replace(f"func {name}(", f"func {new_name}(")
                .replace(f"function {name}(", f"function {new_name}(")
            )
        else:
            new = re.sub(rf"(?<![\w$]){name}(?![\w$])", new_name, new, count=1)
        truth = "routine" if op in {"rename", "rename_and_comment"} else "behavior"
    if op == "literal":
        new, truth = re.sub(r"\b1\b", "2", old, count=1), "behavior"
    elif op == "rename_and_literal":
        new = re.sub(r"\b1\b", "2", new, count=1)
    elif op == "operator":
        new, truth = old.replace(" + ", " - ", 1), "behavior"
    elif op == "delete_line":
        i = rng.choice(code)
        new, truth = "\n".join(lines[:i] + lines[i + 1 :]), "behavior"
    elif op == "add_statement":
        i = rng.choice(code)
        indent = re.match(r"\s*", lines[i]).group(0)  # type: ignore[union-attr]
        tail = "" if language in ("py", "go") else ";"
        new, truth = "\n".join([*lines[:i], f"{indent}audit(v){tail}", *lines[i:]]), "behavior"
    elif op == "comment_out":
        i = rng.choice(code)
        indent = re.match(r"\s*", lines[i]).group(0)  # type: ignore[union-attr]
        new, truth = "\n".join([*lines[:i], f"{indent}{marker} {lines[i].strip()}", *lines[i + 1 :]]), "behavior"
    elif op == "string_content":
        new = old.replace('"a b"', rng.choice(['"a  b"', f'"a b {marker} x"', '"a b "']))
        truth = "behavior"
    elif op == "header" and language in _HEADERS:
        new, truth = (
            (
                _HEADERS[language] + "\n" + old
                if language != "java"
                else old.replace("    String alpha", "    @Override\n    String alpha").replace(
                    "    int alpha", "    @Override\n    int alpha"
                )
            ),
            "behavior",
        )
    elif op == "header":
        new, truth = old.replace("v + 1", "v + 2"), "behavior"
    return path, old, new, name, new_name, truth  # type: ignore[return-value]


def _guard(path: str, old: str, new: str, name: str, new_name: str) -> SinceReview:
    try:
        return _prove(old, new, path=path, old_name=name, new_name=new_name)
    except ValueError:
        # No window for the edited definition (a deleted or unparsable header): nothing to compare, nothing proved.
        return SinceReview(False, "", "unreadable")


def _classify(
    seeds: range, counts: Counter[str], *, guard: Callable[..., SinceReview] = _guard, skip_oracle: bool = False
) -> list[str]:
    """Run each seed's edit through *guard*; classify against the ground truth the edit was built from."""

    fail_open: list[str] = []
    for seed in seeds:
        rng = random.Random(seed)
        language = rng.choice(_STYLED)
        op = rng.choice(_ROUTINE_OPS + _BEHAVIOR_OPS)
        built = _edit(language, op, rng)
        path, old, new, name, new_name, truth = built  # type: ignore[misc]
        if old == new and op != "same":
            counts["skipped_noop"] += 1
            continue
        if truth == "routine" and not skip_oracle:
            rename = (name, new_name) if name != new_name else None
            assert _tokens(old, language, rename) == _tokens(new, language, None), (seed, op, language)
        proof = guard(path, old, new, name, new_name)
        verdict = "accepted" if proof.routine else "refused"
        key = {
            ("routine", "accepted"): "correct_accept",
            ("behavior", "refused"): "correct_refuse",
            ("routine", "refused"): "fail_safe",
            ("behavior", "accepted"): "fail_open",
        }[(truth, verdict)]
        counts[key] += 1
        counts[f"{language}:{key}"] += 1
        if key == "fail_open":
            fail_open.append(f"seed={seed} lang={language} op={op}\n--- old\n{old}\n--- new\n{new}")
    return fail_open


def test_randomized_edits_never_fail_open() -> None:
    counts: Counter[str] = Counter()
    fail_open = _classify(range(1500), counts)
    assert not fail_open, "\n\n".join(fail_open[:3])
    assert counts["fail_open"] == 0
    total = sum(counts[k] for k in ("correct_accept", "correct_refuse", "fail_safe"))
    # The harness has to exercise both sides, or "fail-open == 0" would be a refusal of everything.
    assert counts["correct_accept"] > total * 0.25, counts
    assert counts["correct_refuse"] > total * 0.25, counts
    assert counts["fail_safe"] > 0, "the harness has to contain edits the rule is too careful about"
    for language in _STYLED:
        assert counts[f"{language}:correct_accept"] > 5, (language, counts)
        assert counts[f"{language}:correct_refuse"] > 5, (language, counts)
    print("since-review differential:", dict(sorted(counts.items())))


def test_the_harness_does_see_fail_open_in_a_guard_that_trusts_the_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard that takes ``renamed_from`` at its word -- the rename is the whole difference -- must be caught."""

    def trusting(old_body: str, old_symbol: str, new_symbol: str, new_body: str) -> tuple[str | None, str]:
        return new_body, ""

    monkeypatch.setattr(since_review, "_normalised_old_body", trusting)
    counts: Counter[str] = Counter()
    fail_open = _classify(range(600), counts)
    assert fail_open and counts["fail_open"] > 0, counts


def test_the_harness_does_see_fail_open_in_a_guard_that_ignores_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(since_review, "symbol_header", lambda lines, start: "")
    counts: Counter[str] = Counter()
    fail_open = _classify(range(600), counts)
    assert fail_open and counts["fail_open"] > 0, counts


def _file_target(old: str, new: str, path: str) -> ReviewTarget:
    """One whole-file target, the way the base-diff proof (``prove_file``) is asked about a changed file."""

    return ReviewTarget(
        target_id="t",
        unit_key=f"file:{path}",
        kind="file",
        path=path,
        label=path,
        symbol="",
        start_line=1,
        end_line=len(new.split("\n")),
        hunk_ordinals=(0,),
        spans=(TargetSpan("old", 1, len(old.split("\n")), 0), TargetSpan("new", 1, len(new.split("\n")), 0)),
        state="unreviewed",
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=0,
        attention_level="normal",
        reasons=(),
        additions=1,
        deletions=0,
        fingerprint_method="exact",
    )


def _file_guard(path: str, old: str, new: str, name: str, new_name: str) -> SinceReview:
    """The base-diff proof on the same edits: it must hold the same line (it never sees a rename)."""

    proofs = routine.prove_file(path, old, new, [_file_target(old, new, path)])
    return SinceReview(bool(proofs), "", "")


def test_the_file_proof_never_fails_open_on_the_same_edits() -> None:
    counts: Counter[str] = Counter()
    fail_open = _classify(range(1500), counts, guard=_file_guard)
    assert not fail_open, "\n\n".join(fail_open[:3])
    assert counts["fail_open"] == 0
    assert counts["correct_accept"] > 200 and counts["correct_refuse"] > 200, counts
    print("file-proof differential:", dict(sorted(counts.items())))


def _shape_only(old: Any, new: Any, language: str) -> bool:
    return bool(old.shape == new.shape)


def test_both_differentials_see_fail_open_when_line_terminators_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the line-terminator rule, a block comment holding a newline after ``return`` is proved routine."""

    monkeypatch.setattr(routine, "_same_structure", _shape_only)
    for guard in (_guard, _file_guard):
        counts: Counter[str] = Counter()
        fail_open = _classify(range(600), counts, guard=guard)
        assert fail_open and counts["fail_open"] > 0, (guard.__name__, counts)
        assert all("/*" in item or "return\n" in item for item in fail_open), fail_open[:1]


# ------------------------------------------------------------------ the store side

_BASE_PY = "def keep(x):\n    return x\n"
_ALPHA_PY = "def alpha(v):\n    total = v + 1\n    return total\n"
_GAMMA_PY = "def gamma(v):\n    total = v * 3\n    return total\n"
_ALPHA_GO = "package a\n\nfunc Alpha(v int) int {\n\ttotal := v + 1\n\treturn total\n}\n"


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class _Repo:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.root.mkdir()
        repo = pygit2.init_repository(str(self.root), initial_head="main")
        repo.config["user.name"], repo.config["user.email"] = "Fixture Tester", "tester@example.com"
        _write(self.root, "src/a.py", _BASE_PY)
        repo.index.add_all()
        repo.index.write()
        signature = pygit2.Signature("Fixture Tester", "tester@example.com", 1700000000, 0)
        repo.create_commit("HEAD", signature, signature, "baseline", repo.index.write_tree(), [])
        self.store = ReviewStore(tmp_path / "store")
        self.tmp = tmp_path
        self.session: Any = None
        self.client: Any = None
        self.headers: dict[str, str] = {}

    def advance(self) -> Any:
        if self.session is None:
            self.session = open_or_create_session(self.store, self.root, resolve_rev_range(self.root))
        return refresh(
            self.store,
            self.session,
            self.root,
            store_root=self.tmp / "store",
            rng=resolve_rev_range(self.root),
            limit=500,
        )

    def review_everything(self, state: str = "reviewed") -> Any:
        first = self.advance()
        for unit in self.store.list_units(first.revision.id):
            mark_unit(self.store, self.session, first.revision, unit, state=state)
        return first

    def targets(self, path: str = "") -> dict[str, dict[str, Any]]:
        if self.client is None:
            app = FastAPI()
            token = "since-review-token"
            register_review_api(
                app, self.store, auth_dependency=make_token_dependency(token), repo_root=self.root, port=45997
            )
            self.client = TestClient(app, base_url="http://127.0.0.1:45997")
            self.headers = {"Authorization": f"Bearer {token}"}
        query = f"?path={path}" if path else ""
        payload = self.client.get(f"/api/reviews/{self.session.id}/targets{query}", headers=self.headers)
        return {row["label"]: row for row in payload.json()["targets"]}


def _since(rows: dict[str, dict[str, Any]], label: str) -> dict[str, Any]:
    return rows[label]["since_review"]


def test_a_comment_added_after_the_review_is_routine_and_a_behavior_change_is_not(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/a.py", _BASE_PY + "\n" + _ALPHA_PY)
    _write(repo.root, "src/g.go", _ALPHA_GO)
    repo.review_everything()
    _write(repo.root, "src/a.py", _BASE_PY + "\n" + _ALPHA_PY.replace("v + 1", "v + 1  # why"))
    _write(repo.root, "src/g.go", _ALPHA_GO.replace("v + 1", "v + 2"))
    repo.advance()
    rows = repo.targets()
    assert _since(rows, "src/a.py::alpha")["routine"] is True
    assert _since(rows, "src/a.py::alpha")["rule"] == "comment_only"
    assert _since(rows, "src/g.go::Alpha")["routine"] is False


def test_the_comparison_is_with_the_reviewed_content_not_with_the_diff_base(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/g.go", _ALPHA_GO)
    repo.review_everything()
    # The reviewer approved the arithmetic as it was. A formatter then runs: routine since the review, although the
    # base diff of the file still shows arithmetic.
    _write(repo.root, "src/g.go", _ALPHA_GO.replace("v + 1", "v+1"))
    repo.advance()
    row = repo.targets()["src/g.go::Alpha"]
    assert row["since_review"]["routine"] is True and row["since_review"]["rule"] == "formatting_only"
    assert row["batch_guard"]["guarded"] is True, "the base diff alone would still have kept this part out"


def test_a_part_edited_twice_is_not_routine_when_only_the_second_edit_looks_small(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/g.go", _ALPHA_GO)
    repo.review_everything()
    _write(repo.root, "src/g.go", _ALPHA_GO.replace("v + 1", "v + 2"))
    repo.advance()
    _write(repo.root, "src/g.go", _ALPHA_GO.replace("v + 1", "v+2"))  # only a reformat since the *second* revision
    repo.advance()
    assert _since(repo.targets(), "src/g.go::Alpha")["routine"] is False


def test_a_rename_alone_is_routine_but_a_rename_with_a_changed_body_is_not(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/a.py", _BASE_PY)
    _write(repo.root, "src/b.py", _ALPHA_PY)
    _write(repo.root, "src/c.py", _GAMMA_PY)
    repo.review_everything()
    _write(repo.root, "src/b.py", _ALPHA_PY.replace("alpha", "alpha_two"))
    _write(repo.root, "src/c.py", _GAMMA_PY.replace("gamma", "gamma_two").replace("v * 3", "v * 4"))
    repo.advance()
    rows = repo.targets()
    assert _since(rows, "src/b.py::alpha_two")["rule"] == "rename_only"
    assert rows["src/b.py::alpha_two"]["renamed_from"] == {"path": "src/b.py", "symbol": "alpha"}
    # Renamed *and* changed: the old judgment does not follow it at all, so it is a new part with nothing to compare.
    assert rows["src/c.py::gamma_two"]["state"] != "changed_since_review"
    assert "since_review" not in rows["src/c.py::gamma_two"]


def test_an_earlier_request_for_changes_is_never_closed_by_a_reformat(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/g.go", _ALPHA_GO)
    first = repo.advance()
    for unit in repo.store.list_units(first.revision.id):
        mark_unit(repo.store, repo.session, first.revision, unit, state="needs_changes")
    _write(repo.root, "src/g.go", _ALPHA_GO.replace("v + 1", "v+1"))
    repo.advance()
    row = repo.targets().get("src/g.go::Alpha")
    assert row is not None
    assert row.get("since_review", {}).get("routine") is not True


_OWNS_BASE = "import os\n\nLIMIT = 3\n\ndef keep(x):\n    return x\n"
_OWNS_R1 = "import os\n\ndef alpha(v):\n    return v + 1\n\ndef keep(x):\n    return x\n"
_OWNS_GO_BASE = "package a\n\nconst Limit = 3\n\nfunc Keep(x int) int {\n\treturn x\n}\n"
_OWNS_GO_R1 = "package a\n\nfunc Alpha(v int) int {\n\treturn v + 1\n}\n\nfunc Keep(x int) int {\n\treturn x\n}\n"


def _repo_with_a_definition_that_owns_deleted_text(
    tmp_path: Path,
    path: str = "src/o.py",
    base: str = _OWNS_BASE,
    r1: str = _OWNS_R1,
    symbol: str = "alpha",
    *,
    owns: bool = True,
) -> _Repo:
    """``alpha`` replaces the base line ``LIMIT = 3``, so its fingerprint covers that deleted line; all is reviewed."""

    repo = _Repo(tmp_path)
    git = pygit2.Repository(str(repo.root))
    _write(repo.root, path, base)
    git.index.add(path)
    git.index.write()
    signature = pygit2.Signature("Fixture Tester", "tester@example.com", 1700000050, 0)
    git.create_commit("HEAD", signature, signature, "base", git.index.write_tree(), [git.head.target])
    _write(repo.root, path, r1)
    first = repo.review_everything()
    alpha = next(u for u in repo.store.list_units(first.revision.id) if u.symbol == symbol)
    assert ("~" in alpha.content_fingerprint) is owns, "the fixture's definition owns deleted text (or not)"
    return repo


def test_a_reformat_of_a_part_that_owns_deleted_text_is_still_formatting_only(tmp_path: Path) -> None:
    """The body changed only in layout and the deleted lines it owns are the ones the reviewer saw."""

    repo = _repo_with_a_definition_that_owns_deleted_text(tmp_path, "src/o.go", _OWNS_GO_BASE, _OWNS_GO_R1, "Alpha")
    _write(repo.root, "src/o.go", _OWNS_GO_R1.replace("v + 1", "v+1"))
    repo.advance()
    row = repo.targets()["src/o.go::Alpha"]
    assert row["state"] == "changed_since_review"
    assert row["since_review"]["routine"] is True and row["since_review"]["rule"] == "formatting_only"


def test_a_comment_on_a_part_that_owns_deleted_text_is_still_routine(tmp_path: Path) -> None:
    """S1 control: a comment since the review proves routine for a definition owning deleted text."""

    repo = _repo_with_a_definition_that_owns_deleted_text(tmp_path)
    _write(repo.root, "src/o.py", _OWNS_R1.replace("v + 1", "v + 1  # why"))
    repo.advance()
    row = repo.targets()["src/o.py::alpha"]
    assert row["since_review"]["routine"] is True and row["since_review"]["rule"] == "comment_only"


@pytest.mark.parametrize("reformat", [True, False], ids=["and reformatted", "body untouched"])
def test_new_deleted_text_beside_a_part_is_never_batched_as_routine(tmp_path: Path, reformat: bool) -> None:
    """More base lines are now replaced beside the same body: that is not formatting, whatever the body did."""

    repo = _repo_with_a_definition_that_owns_deleted_text(tmp_path, "src/o.go", _OWNS_GO_BASE, _OWNS_GO_R1, "Alpha")
    changed = _OWNS_GO_R1.replace("package a\n\n", "")  # the deleted `package a` lines join the block Alpha replaces
    _write(repo.root, "src/o.go", changed.replace("v + 1", "v+1") if reformat else changed)
    repo.advance()
    row = repo.targets()["src/o.go::Alpha"]
    assert row["state"] == "changed_since_review"
    assert row.get("since_review", {}).get("routine") is not True


def test_a_part_reviewed_as_a_pure_addition_is_not_routine_once_it_replaces_base_lines_and_is_reformatted(
    tmp_path: Path,
) -> None:
    """Q1: the mark owns no deleted text; the part now does. The reformat must not carry the new deleted line."""

    r1 = "package a\n\nconst Limit = 3\n" + _OWNS_GO_R1.split("package a\n", 1)[1]
    repo = _repo_with_a_definition_that_owns_deleted_text(tmp_path, "src/o.go", _OWNS_GO_BASE, r1, "Alpha", owns=False)
    _write(repo.root, "src/o.go", _OWNS_GO_R1.replace("v + 1", "v+1"))  # `const Limit = 3` is replaced by Alpha
    repo.advance()
    row = repo.targets()["src/o.go::Alpha"]
    assert row["state"] == "changed_since_review"
    assert row.get("since_review", {}).get("routine") is not True


_TWO_COPIES_GO = (
    "package a\n\nfunc K() int {\n\treturn 1\n}\n\nvar r = reg()\n\nfunc T() int {\n\treturn 1\n}\n\nvar r = reg()\n"
)
_ALPHA_BLOCK_GO = "func Alpha(v int) int {\n\treturn v + 1\n}\n"


def test_the_same_deleted_text_at_a_second_place_is_not_batched_as_formatting_only(tmp_path: Path) -> None:
    """Q2: the reviewed part replaced the first copy of a run; now it replaces an identical second copy, reformatted."""

    r1 = _TWO_COPIES_GO.replace("var r = reg()\n", _ALPHA_BLOCK_GO, 1)
    head, _, tail = _TWO_COPIES_GO.rpartition("var r = reg()\n")
    r2 = head + _ALPHA_BLOCK_GO.replace("v + 1", "v+1") + tail
    repo = _repo_with_a_definition_that_owns_deleted_text(tmp_path, "src/o.go", _TWO_COPIES_GO, r1, "Alpha")
    _write(repo.root, "src/o.go", r2)
    repo.advance()
    row = repo.targets()["src/o.go::Alpha"]
    assert row["state"] == "changed_since_review"
    assert row.get("since_review", {}).get("routine") is not True


def test_a_part_the_reviewer_never_answered_has_no_since_review_answer(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/g.go", _ALPHA_GO)
    repo.advance()
    _write(repo.root, "src/g.go", _ALPHA_GO.replace("v + 1", "v+1"))
    repo.advance()
    assert not any("since_review" in row for row in repo.targets().values())


def test_a_unchanged_part_has_no_since_review_answer(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/g.go", _ALPHA_GO)
    _write(repo.root, "src/a.py", _BASE_PY + "\n" + _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/a.py", _BASE_PY + "\n" + _ALPHA_PY.replace("v + 1", "v + 1  # why"))
    repo.advance()
    rows = repo.targets()
    assert "since_review" in rows["src/a.py::alpha"]
    assert "since_review" not in rows["src/g.go::Alpha"]


def test_a_missing_reviewed_blob_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/g.go", _ALPHA_GO)
    repo.review_everything()
    _write(repo.root, "src/g.go", _ALPHA_GO.replace("v + 1", "v+1"))
    repo.advance()
    monkeypatch.setattr(repo.store, "read_blob_artifact", lambda review_id, revision_id: None)
    row = repo.targets()["src/g.go::Alpha"]
    assert row["since_review"]["routine"] is False


def test_a_rename_that_shadows_a_builtin_or_uncollects_a_test_reaches_the_read_queue(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/b.py", _ALPHA_PY + "\n\ndef use(xs):\n    return len(xs)\n")
    _write(repo.root, "tests/test_b.py", "def test_alpha():\n    assert 1 + 1 == 2\n")
    repo.review_everything()
    _write(repo.root, "src/b.py", (_ALPHA_PY + "\n\ndef use(xs):\n    return len(xs)\n").replace("alpha", "len"))
    _write(repo.root, "tests/test_b.py", "def alpha():\n    assert 1 + 1 == 2\n")
    repo.advance()
    rows = repo.targets()
    assert _since(rows, "src/b.py::len")["routine"] is False
    assert _since(rows, "tests/test_b.py::alpha")["routine"] is False


# ---------------------------------------------------------------- bounded cost and the proof's own gates


def _many_functions(count: int, tail: str = "") -> str:
    return "\n\n".join(f"def f{i}(v):\n    total = v + {i}\n{tail}    return total\n" for i in range(count))


def test_the_parts_compared_in_one_request_are_capped_and_the_rest_say_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(since_review, "MAX_PARTS_PROVED", 3)
    repo = _Repo(tmp_path)
    for count in (3, 4):  # at the cap, then one past it
        case = tmp_path / f"case{count}"
        case.mkdir()
        repo = _Repo(case)
        for i in range(count):
            _write(repo.root, f"src/m{i}.py", f"def f{i}(v):\n    total = v + {i}\n    return total\n")
        repo.review_everything()
        for i in range(count):
            _write(repo.root, f"src/m{i}.py", f"def f{i}(v):\n    total = v + {i}  # why\n    return total\n")
        repo.advance()
        proofs = [row["since_review"] for row in repo.targets().values()]
        assert len(proofs) == count
        routine = [proof for proof in proofs if proof["routine"]]
        refused = [proof for proof in proofs if not proof["routine"]]
        assert len(routine) == min(count, 3)
        assert len(refused) == count - len(routine)
        for proof in refused:
            assert proof["rule"] == "" and "3 reopened parts" in proof["detail"]


def test_a_second_request_compares_only_the_parts_the_first_did_not_and_keeps_its_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/a.py", _ALPHA_PY)
    _write(repo.root, "src/b.py", _GAMMA_PY)
    repo.review_everything()
    _write(repo.root, "src/a.py", _ALPHA_PY.replace("v + 1", "v + 1  # why"))
    _write(repo.root, "src/b.py", _GAMMA_PY.replace("v * 3", "v * 3  # why"))
    repo.advance()
    asked: list[list[str]] = []
    real = since_review.since_review_proofs

    def spy(
        store: Any, review_id: str, reviewer_id: str, revision_id: str, targets: Any, read: Any, **kwargs: Any
    ) -> Any:
        targets = list(targets)
        asked.append(sorted({target.path for target in targets}))
        return real(store, review_id, reviewer_id, revision_id, targets, read, **kwargs)

    monkeypatch.setattr(since_review, "since_review_proofs", spy)
    assert repo.targets(path="src/a.py")["src/a.py::alpha"]["since_review"]["routine"] is True
    rows = repo.targets()
    assert asked == [["src/a.py"], ["src/b.py"]]
    assert rows["src/a.py::alpha"]["since_review"]["routine"] is True
    assert rows["src/b.py::gamma"]["since_review"]["routine"] is True
    repo.targets(path="src/a.py")
    repo.targets()
    assert len(asked) == 2, "nothing left to compare"


def test_parts_of_one_file_share_one_parse_of_the_new_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/many.py", _many_functions(6))
    repo.review_everything()
    _write(repo.root, "src/many.py", _many_functions(6, tail="    # why\n"))
    repo.advance()
    parsed: list[str] = []
    real = routine._side

    def counting(language: str, text: str) -> Any:
        parsed.append(text)
        return real(language, text)

    monkeypatch.setattr(routine, "_side", counting)
    rows = repo.targets()
    functions = [row for label, row in rows.items() if "::" in label]
    assert len(functions) == 6 and all(row["since_review"]["routine"] for row in functions)
    new_text = _many_functions(6, tail="    # why\n")
    assert parsed.count(new_text) == 1, "the new file is parsed once, not once per part"
    assert len(parsed) <= 6 + 1, "plus one spliced reviewed side per part"


class _CountingUnit:
    """A stored unit that counts how often its fingerprint is read."""

    reads = 0

    def __init__(self, fingerprint: str) -> None:
        self.kind = "symbol"
        self._fingerprint = fingerprint

    @property
    def content_fingerprint(self) -> str:
        type(self).reads += 1
        return self._fingerprint


def test_the_reviewed_revision_is_indexed_by_fingerprint_once_not_scanned_per_part() -> None:
    units = [_CountingUnit(f"fp{i}") for i in range(50)]

    class Store:
        listed = 0

        def list_units(self, revision: str) -> list[_CountingUnit]:
            Store.listed += 1
            return units

        def read_blob_artifact(self, review_id: str, revision: str) -> dict[str, str]:
            return {}

    request = since_review._Request(
        {},
        {},
        {},
        Store(),
        "r",
        lambda path: None,
        lambda text, path: [],
        routine.SideMemo(),
        {},
        {},  # type: ignore[arg-type]
    )
    for i in range(50):
        found = since_review._reviewed_candidates(request, "rev1", f"fp{i}")
        assert found is not None and len(found) == 1
    assert Store.listed == 1
    assert _CountingUnit.reads == 50, "each unit's fingerprint is read once, when the index is built"


def _tamper(monkeypatch: pytest.MonkeyPatch, repo: _Repo, **changes: Any) -> None:
    import dataclasses

    real = repo.store.list_mark_events

    def events(*args: Any, **kwargs: Any) -> Any:
        return tuple(dataclasses.replace(event, **changes) for event in real(*args, **kwargs))

    monkeypatch.setattr(repo.store, "list_mark_events", events)


def test_a_judgment_made_at_other_content_than_the_mark_holds_is_no_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/a.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/a.py", _ALPHA_PY.replace("v + 1", "v + 1  # why"))
    repo.advance()
    assert repo.targets()["src/a.py::alpha"]["since_review"]["routine"] is True
    # Same history, but the Looks good on record was made at different content than the mark's fingerprint.
    (tmp_path / "other").mkdir()
    other = _Repo(tmp_path / "other")
    _write(other.root, "src/a.py", _ALPHA_PY)
    other.review_everything()
    _write(other.root, "src/a.py", _ALPHA_PY.replace("v + 1", "v + 1  # why"))
    other.advance()
    _tamper(monkeypatch, other, content_fingerprint="not-the-marks-fingerprint")
    row = other.targets()["src/a.py::alpha"]["since_review"]
    assert row["routine"] is False and "not Looks good" in row["detail"]


def test_a_reviewed_body_found_in_two_places_is_not_a_single_reviewed_copy(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/a.py", _ALPHA_PY)
    _write(repo.root, "src/b.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/a.py", _ALPHA_PY.replace("v + 1", "v + 1  # why"))
    repo.advance()
    row = repo.targets()["src/a.py::alpha"]["since_review"]
    assert row["routine"] is False and "exactly once" in row["detail"]


def test_a_window_whose_text_no_longer_has_the_units_fingerprint_is_not_used() -> None:
    text = _ALPHA_PY
    [window] = symbol_windows(text, "a.py")
    unit = type(
        "Unit",
        (),
        {
            "kind": "symbol",
            "fingerprint_method": "body",
            "start_line": window.start_line,
            "end_line": window.end_line,
            "symbol": "alpha",
            "content_fingerprint": symbol_fingerprint(window.kind, "alpha", window.body),
        },
    )()
    windows_of = symbol_windows
    assert since_review._window_of(text, "a.py", unit, windows_of) == (window.start_line, window.end_line)  # type: ignore[arg-type]
    swapped = text.replace("v + 1", "v + 2")  # same lines, different code
    assert since_review._window_of(swapped, "a.py", unit, windows_of) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("a.py", "def alpha(v):\n    return v + 1\n", "def alpha(v):\n    return v + 1  # type: ignore\n"),
        ("a.py", "def alpha(v):\n    return v + 1  # noqa\n", "def alpha(v):\n    return v + 1\n"),
        (
            "a.py",
            "def alpha(v):\n    x = v\n    return x\n",
            "def alpha(v):\n    x = v\n    # pylint: disable=all\n    return x\n",
        ),
        (
            "a.py",
            "def alpha(v):\n    x = v\n    # fmt: off\n    return x\n",
            "def alpha(v):\n    x = v\n    return x\n",
        ),
        (
            "a.py",
            "def alpha(v):\n    x = v  # type: ignore\n    y = x\n    return y\n",
            "def alpha(v):\n    x = v\n    y = x  # type: ignore\n    # a note\n    return y\n",
        ),
        (
            "a.js",
            "function alpha(v) {\n  return v + 1;\n}\n",
            "function alpha(v) {\n  // @ts-ignore\n  return v + 1;\n}\n",
        ),
        (
            "a.js",
            "function alpha(v) {\n  return v + 1;\n}\n",
            "function alpha(v) {\n  return v + 1; // eslint-disable-line\n}\n",
        ),
        (
            "a.go",
            "package a\n\nfunc Alpha(v int) int {\n\treturn v + 1\n}\n",
            "package a\n\nfunc Alpha(v int) int {\n\treturn v + 1 //nolint\n}\n",
        ),
    ],
)
def test_adding_or_removing_a_directive_comment_is_never_routine(path: str, old: str, new: str) -> None:
    name = "Alpha" if path.endswith(".go") else "alpha"
    assert routine.prove_region(path, old, new, _span(old, path, name), _span(new, path, name)) is None
    assert not _prove(old, new, path=path, old_name=name).routine


def test_a_part_that_is_reopened_on_a_later_revision_after_a_behavior_change_is_not_batched(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, "src/a.py", _ALPHA_PY)
    repo.review_everything()
    _write(repo.root, "src/a.py", _ALPHA_PY.replace("v + 1", "v + 1  # why"))
    repo.advance()
    assert repo.targets()["src/a.py::alpha"]["since_review"]["routine"] is True
    _write(repo.root, "src/a.py", _ALPHA_PY.replace("v + 1", "v + 2  # why"))
    repo.advance()
    assert repo.targets()["src/a.py::alpha"]["since_review"]["routine"] is False


@pytest.mark.parametrize(
    ("rel", "old", "old_name", "new_name"),
    [
        pytest.param(
            "conftest.py",
            "def pytest_configure(config):\n    config.flag = 1\n    return None\n",
            "pytest_configure",
            "configure",
            id="pytest-hook",
        ),
        pytest.param(
            "src/n.ts",
            "export namespace N {\n  export function alpha(v: number): number {\n    const total = v + 1;\n    return total;\n  }\n}\n",
            "alpha",
            "gamma",
            id="ts-namespace-member",
        ),
        pytest.param(
            "app/route.ts",
            "export async function GET(req: Request) {\n  const total = 1;\n  return new Response(String(total));\n}\n",
            "GET",
            "get",
            id="nextjs-route",
        ),
    ],
)
def test_a_registered_function_renamed_in_a_real_review_is_not_offered_in_the_batch(
    tmp_path: Path, rel: str, old: str, old_name: str, new_name: str
) -> None:
    repo = _Repo(tmp_path)
    _write(repo.root, rel, old)
    repo.review_everything()
    _write(repo.root, rel, old.replace(old_name, new_name))
    repo.advance()
    rows = [row for row in repo.targets().values() if row["path"] == rel and row["kind"] == "symbol"]
    assert rows, "the renamed function is a reopened part"
    assert not any((row.get("since_review") or {}).get("routine") for row in rows), rows


def test_a_file_is_parsed_once_however_many_of_its_parts_are_checked_for_an_export(monkeypatch):
    from lemoncrow.pro.capabilities.review import since_review as module

    calls: list[str] = []
    real = module._parse_source

    def counting(language, source):
        calls.append(language)
        return real(language, source)

    monkeypatch.setattr(module, "_parse_source", counting)
    trees: dict = {}
    text = "function alpha() {\n  return 1;\n}\n\nfunction beta() {\n  return 2;\n}\n"
    assert module._under_export("javascript", text, 1, trees) is False
    assert module._under_export("javascript", text, 5, trees) is False
    assert module._under_export("javascript", text, 1, trees) is False
    assert calls == ["javascript"]
    # the dict stays small whatever the request walks
    for index in range(6):
        module._under_export("javascript", f"function a{index}() {{}}\n", 1, trees)
    assert len(trees) <= module._EXPORT_TREES_KEPT


def test_a_file_too_large_to_parse_is_treated_as_exported_and_not_parsed(monkeypatch):
    from lemoncrow.pro.capabilities.review import since_review as module

    monkeypatch.setattr(module, "_parse_source", lambda *_: pytest.fail("parsed a file over the size cap"))
    big = "function alpha() {\n  return 1;\n}\n" + "// " + "x" * module._MAX_BYTES + "\n"
    assert module._under_export("javascript", big, 1, {}) is True


def test_no_parse_outlives_the_call_that_made_it():
    from lemoncrow.pro.capabilities.review import since_review as module

    assert not hasattr(module._export_tree, "cache_info"), "a module-level cache would keep parsed trees alive"
    old = "function alpha() {\n  return 1;\n}\n"
    new = "function gamma() {\n  return 1;\n}\n"
    kept: dict = {}
    proof = module.prove_symbol_since_review(
        old_path="a.js",
        old_text=old,
        old_span=(1, 3),
        old_symbol="alpha",
        new_path="a.js",
        new_text=new,
        new_span=(1, 3),
        new_symbol="gamma",
        export_trees=kept,
    )
    assert proof.routine and proof.rule == "rename_only"
    assert len(kept) == 2  # the caller's dict is what holds the parses
    # without a dict handed in, nothing is left behind anywhere
    module.prove_symbol_since_review(
        old_path="a.js",
        old_text=old,
        old_span=(1, 3),
        old_symbol="alpha",
        new_path="a.js",
        new_text=new,
        new_span=(1, 3),
        new_symbol="gamma",
    )
    assert len(kept) == 2


def test_one_request_parses_a_file_once_for_all_of_its_renamed_parts(tmp_path: Path, monkeypatch) -> None:
    from lemoncrow.pro.capabilities.review import since_review as module

    old = "function alpha(v) {\n  return v + 1;\n}\n\nfunction beta(v) {\n  return v + 2;\n}\n"
    repo = _Repo(tmp_path)
    _write(repo.root, "src/a.js", old)
    repo.review_everything()
    _write(repo.root, "src/a.js", old.replace("alpha", "gamma").replace("beta", "delta"))
    repo.advance()
    calls: list[str] = []
    real = module._parse_source

    def counting(language, source):
        calls.append(language)
        return real(language, source)

    monkeypatch.setattr(module, "_parse_source", counting)
    rows = [row for row in repo.targets().values() if row["path"] == "src/a.js" and row["kind"] == "symbol"]
    assert len(rows) == 2 and all((row.get("since_review") or {}).get("routine") for row in rows), rows
    assert len(calls) == 2, calls  # the reviewed file and the new file, once each, not once per part
