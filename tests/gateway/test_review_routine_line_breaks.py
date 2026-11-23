"""A line terminator can carry meaning, and a comment can hide one (spec §4.4, P11-2 review round 1, B1).

tree-sitter skips comments, so ``return /* c */ x`` and ``return /*\\n */ x`` parse to one shape. In ECMAScript (and Go) a
block comment holding a newline is a line terminator: the second returns ``undefined``. ``formatting_only`` must never be
granted over such a change, by the base-diff proof (``prove_file``) or by the since-review proof (``prove_region``).
Real reflow at positions where a line break means nothing still has to pass.

The last test is a differential against the TypeScript compiler's own parse: every token gap of a corpus gets a real
newline, a newline inside a block comment, and a plain block comment, and no edit the compiler reads differently may be
proved routine (fail-open == 0).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.routine import _parse, prove_file, prove_region, routine_language
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan


def _target(old: str, new: str, path: str) -> ReviewTarget:
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


def _rules(path: str, old: str, new: str) -> tuple[str | None, str | None]:
    """The rule each proof grants (``None`` for no proof): the base-diff proof, then the since-review proof."""

    file_proof = prove_file(path, old, new, [_target(old, new, path)]).get("t")
    region = prove_region(path, old, new, (1, len(old.split("\n"))), (1, len(new.split("\n"))))
    return (file_proof.rule if file_proof else None, region.rule if region else None)


def _fn(body: str) -> str:
    return f"function f(x, l) {{\n  {body}\n}}\n"


# Each pair changes behavior (or breaks the file) in ECMAScript although the parsed shape is equal.
_ASI_CASES = [
    pytest.param("return /* c */ x;", "return /*\n  */ x;", id="return-comment-newline"),
    pytest.param("return x;", "return\n  x;", id="return-real-newline"),
    pytest.param("throw /* c */ x;", "throw /*\n*/ x;", id="throw"),
    pytest.param("l: for (;;) { break /* c */ l; }", "l: for (;;) { break /*\n*/ l; }", id="break"),
    pytest.param("l: for (;;) { continue /* c */ l; }", "l: for (;;) { continue /*\n*/ l; }", id="continue"),
    pytest.param("const g = (a) => a;", "const g = (a)\n  => a;", id="newline-before-arrow"),
    pytest.param("const g = async (a) => a;", "const g = async\n  (a) => a;", id="async-newline"),
    pytest.param("const g = async (a) => a;", "const g = async /*\n*/ (a) => a;", id="async-comment-newline"),
    pytest.param("x++;", "x /*\n*/ ++;", id="postfix-comment-newline"),
    pytest.param("x = x + 1;", "x = x /*\n*/ + 1;", id="infix-comment-newline"),
    pytest.param("x = f(l);", "x = f /*\n*/ (l);", id="call-comment-newline"),
]


@pytest.mark.parametrize("path", ["a.js", "a.ts", "a.tsx", "a.jsx"])
@pytest.mark.parametrize(("old", "new"), _ASI_CASES)
def test_a_line_terminator_a_comment_adds_or_moves_is_never_formatting_only(path: str, old: str, new: str) -> None:
    assert _rules(path, _fn(old), _fn(new)) == (None, None)


def test_a_yield_across_a_comment_newline_is_not_formatting_only() -> None:
    old = "function* g(x) {\n  yield /* c */ x;\n}\n"
    new = "function* g(x) {\n  yield /*\n*/ x;\n}\n"
    assert _rules("a.ts", old, new) == (None, None)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("type A = number;", "type /*\n*/ A = number;", id="type-comment-newline"),
        pytest.param("declare const a: number;", "declare\nconst a: number;", id="declare-newline"),
        pytest.param("abstract class A {}", "abstract\nclass A {}", id="abstract-newline"),
    ],
)
def test_typescript_contextual_keywords_do_not_survive_a_line_break(old: str, new: str) -> None:
    assert _rules("a.ts", old + "\n", new + "\n") == (None, None)


def _go(body: str) -> str:
    return f"package a\n\nfunc F(a, b int) int {{\n\t{body}\n}}\n"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("return a + b", "return a /*\n*/ + b", id="infix"),
        pytest.param("return a", "return /*\n*/ a", id="return"),
        pytest.param("return F(a, b)", "return F(a, b /*\n*/)", id="call-close"),
    ],
)
def test_a_go_comment_holding_a_newline_acts_like_a_newline(old: str, new: str) -> None:
    assert _rules("a.go", _go(old), _go(new)) == (None, None)


@pytest.mark.parametrize("path", ["a.rs", "A.java"])
def test_a_line_break_in_a_comment_means_nothing_to_rust_and_java(path: str) -> None:
    if path == "a.rs":
        old, new = "fn f() -> i32 {\n    1 + 2\n}\n", "fn f() -> i32 {\n    1 /*\n    */ + 2\n}\n"
    else:
        old = "class A {\n    int f() {\n        return 1 + 2;\n    }\n}\n"
        new = "class A {\n    int f() {\n        return 1 /*\n        */ + 2;\n    }\n}\n"
    assert _rules(path, old, new)[0] is not None
    assert _rules(path, old, new)[1] is not None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("const y = a + b;", "const y = a\n    + b;", id="infix-operator-on-the-next-line"),
        pytest.param("const y = f(a, b);", "const y = f(\n    a,\n    b\n  );", id="arguments-one-per-line"),
        pytest.param("const y = o.a().b();", "const y = o\n    .a()\n    .b();", id="method-chain"),
        pytest.param("const y = { a: 1, b: 2 };", "const y = {\n    a: 1,\n    b: 2\n  };", id="object-literal"),
        pytest.param("const h = (a) => a + 1;", "const h = (a) =>\n    a + 1;", id="arrow-body-on-the-next-line"),
        pytest.param("return a + b;", "return a +\n    b;", id="return-operator-at-the-line-end"),
        pytest.param("const y = c ? a : b;", "const y = c\n    ? a\n    : b;", id="ternary"),
    ],
)
def test_prettier_style_reflow_with_real_newlines_still_passes(old: str, new: str) -> None:
    body_old = f"function f(a, b, c, o) {{\n  {old}\n}}\n"
    body_new = f"function f(a, b, c, o) {{\n  {new}\n}}\n"
    assert _rules("a.js", body_old, body_new) == ("formatting_only", "formatting_only")
    assert _rules("a.ts", body_old, body_new) == ("formatting_only", "formatting_only")


def test_a_block_comment_that_replaces_a_real_line_break_stays_formatting_only() -> None:
    # The statement end is the same token on both sides, and a real line terminator stood there before.
    old = "function f(a) {\n  const y = a + 1;\n  return y;\n}\n"
    new = "function f(a) {\n  const y = a + 1; /* done\n  */\n  return y;\n}\n"
    assert _rules("a.js", old, new)[1] in {"comment_only", "formatting_only"}


# ------------------------------------------------------------------ the compiler as the oracle

_ROOT = Path(__file__).resolve().parents[2]
_TYPESCRIPT = _ROOT / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js"
_ORACLE = r"""
const ts = require(process.argv[1]);
const pairs = JSON.parse(require("fs").readFileSync(0, "utf8"));
function parse(text, ext) {
  const kind = ext === ".tsx" ? ts.ScriptKind.TSX : ext === ".ts" ? ts.ScriptKind.TS : ts.ScriptKind.JS;
  const file = ts.createSourceFile("x" + ext, text, ts.ScriptTarget.Latest, true, kind);
  const out = [];
  (function walk(node) {
    const kids = node.getChildren(file);
    out.push(node.kind + (kids.length === 0 ? ":" + node.getText(file) : ""));
    kids.forEach(walk);
  })(file);
  return { tree: out.join("|"), bad: file.parseDiagnostics.length > 0 };
}
console.log(JSON.stringify(pairs.map(([a, b, ext]) => {
  const x = parse(a, ext), y = parse(b, ext);
  return !x.bad && !y.bad && x.tree === y.tree;
})));
"""
_CORPUS = [
    "function f(x, y) {\n  return x + y;\n}\n",
    "function f(x, y) {\n  if (x) {\n    return y;\n  }\n  throw y;\n}\n",
    "function f(x, y) {\n  const g = (a) => a + x;\n  return g(y);\n}\n",
    "function f(x, y) {\n  let i = 0;\n  i++;\n  --y;\n  return i;\n}\n",
    "function f(x, y) {\n  l: for (;;) {\n    if (x) break l;\n    continue l;\n  }\n}\n",
    "async function f(x, y) {\n  const a = await x;\n  return a + y;\n}\n",
    "function* f(x, y) {\n  yield x;\n  yield* y;\n}\n",
    "function f(x, y) {\n  const o = { a: x, b: [y, 1] };\n  return o.a.toString();\n}\n",
    "function f(x, y) {\n  const g = async (a) => a;\n  return g(x) ? y : x;\n}\n",
    "function f(x, y) {\n  return x\n    ? y\n    : x;\n}\n",
    "class A {\n  static x = 1;\n  get g() {\n    return 1;\n  }\n  set g(v) {}\n  async m() {}\n  *gen() {}\n}\n",
    "export default function f() {}\nexport const c = 1;\n",
    "for (const x of [1]) {}\nconst r = /a b/g;\nconst t = `a ${1 + 2} b`;\n",
    "const o = { get a() { return 1; }, set a(v) {}, async b() {}, c: 1 };\n",
    "const s = 'a b';\nconst u = \"x\";\nconst n = s?.y ?? u;\n",
    "import a, { b as c } from 'm';\nexport { c as d };\n",
    "let x = 1\nlet y = x\n++x\nconst z = y\n(function () {})()\n",
    "const a = b\n[1, 2].forEach(f)\nconst t = c\n`tpl`\n",
    "function f(a, b) {\n  a = b\n  /re/g.test(a)\n  return a\n}\n",
    "class A {\n  static m() {\n    return 1;\n  }\n  x = 1\n  y = 2\n  static\n  z = 3\n}\n",
    "class B extends A {\n  #p = 1;\n  static {\n    init();\n  }\n  constructor() {\n    super();\n  }\n}\n",
]
# Syntax only TypeScript has: every contextual modifier a line break can detach (`canFollowModifier`), the binary
# `as` and `satisfies`, the non-null `!`, and the declarations that start with a contextual keyword.
_TS_CORPUS = [
    "let m: Map<string, number[]>[] = [];\ntype I<T> = T extends Array<infer U> ? U : never;\nconst g = f<number>(1);\n",
    "@dec\nclass C {\n  @prop() p = 1;\n  m(@inject() a: number) {\n    return a;\n  }\n}\n",
    "type A = number;\ndeclare const a: A;\nabstract class B {}\nnamespace N {}\n",
    "function f(x: number, y: string): string {\n  return `${x}` + y;\n}\n",
    "class A {\n  accessor q = 1;\n}\n",
    "class A {\n  public p = 1;\n  private r = 2;\n  protected s = 3;\n  readonly t = 4;\n  static readonly u = 5;\n}\n",
    "class A extends B {\n  override m(): void {}\n  declare v: number;\n  static override w = 1;\n}\n",
    "abstract class A {\n  abstract w(): void;\n  protected abstract x: number;\n}\n",
    "class A {\n  public static async g() {}\n  static get h() {\n    return 1;\n  }\n}\n",
    "const a = x as number;\nconst b = x satisfies number;\nconst c = x!;\nconst d = <number>x;\n",
    "function f(x: unknown, b: unknown, a: number, c: number) {\n  const d = (x as any)!.y;\n  const e = (x satisfies object).z;\n  return a + (b as number) + c;\n}\n",
    "interface I<T> {\n  x: T;\n}\ntype J<T> = () => T;\n",
    "enum E {\n  A = 1,\n}\nconst enum F {\n  B,\n}\ndeclare module 'm' {}\ndeclare global {}\n",
    "export type { A };\nimport type { B } from 'm';\nexport default class {}\nexport abstract class C {}\n",
    "export declare const z: number;\nexport enum G {\n  A,\n}\nexport namespace M {}\nexport interface K {}\n",
    "function g(x: unknown): asserts x is string {}\ndeclare const s: unique symbol;\ntype V<T> = T extends (infer U)[] ? U : never;\n",
    "type T = `a${string}b`;\nlet z: keyof T;\nlet w: readonly string[];\nconst q = f<number>(1);\n",
    "{\n  using x = f();\n}\nasync function g() {\n  await using y = f();\n}\n",
    "const x = {\n  async *a() {},\n  get b() {\n    return 1;\n  },\n};\nlet v = import.meta;\nfor await (const e of g()) {}\n",
]


def _node_oracle(pairs: list[tuple[str, str, str]]) -> list[bool] | None:
    node = shutil.which("node")
    if node is None or not _TYPESCRIPT.exists():
        return None
    done = subprocess.run(
        [node, "-e", _ORACLE, str(_TYPESCRIPT)],
        input=json.dumps(pairs),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return [bool(item) for item in json.loads(done.stdout)]


def _variants(text: str, path: str) -> list[str]:
    tree = _parse(routine_language(path), text.encode())
    assert tree is not None, text
    starts: list[int] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.child_count == 0 and node.start_byte > 0:
            starts.append(node.start_byte)
        stack.extend(node.children)
    raw = text.encode()
    out = []
    for at in sorted(set(starts)):
        for glue in ("\n", "/*\n*/", "/* c */"):
            out.append((raw[:at] + glue.encode() + raw[at:]).decode())
    # and the other direction: join each existing line break, which is what a reflow does to one-line-per-token code
    out.extend(text[: m.start()] + " " + text[m.end() :] for m in re.finditer(r"\n[ \t]*", text))
    return out


def _differential() -> tuple[Counter[str], list[str]]:
    pairs: list[tuple[str, str, str]] = []
    for ext in (".js", ".ts"):
        for text in _CORPUS + (_TS_CORPUS if ext == ".ts" else []):
            for variant in _variants(text, "a" + ext):
                pairs.append((text, variant, ext))
    verdicts = _node_oracle(pairs)
    if verdicts is None:
        pytest.skip("node and the frontend's typescript package are needed for the oracle")
    counts: Counter[str] = Counter()
    fail_open: list[str] = []
    for (old, new, ext), same in zip(pairs, verdicts, strict=True):
        rules = _rules("a" + ext, old, new)
        accepted = rules[0] is not None or rules[1] is not None
        key = {
            (True, True): "correct_accept",
            (False, False): "correct_refuse",
            (True, False): "fail_safe",
            (False, True): "fail_open",
        }[(same, accepted)]
        counts[key] += 1
        if key == "fail_open":
            fail_open.append(f"{ext} {rules}\n--- old\n{old}\n--- new\n{new}")
    return counts, fail_open


def test_no_line_break_edit_the_typescript_compiler_reads_differently_is_proved_routine() -> None:
    counts, fail_open = _differential()
    assert not fail_open, "\n\n".join(fail_open[:3])
    assert counts["fail_open"] == 0
    assert counts["correct_accept"] > 100 and counts["correct_refuse"] > 50, counts
    print("line-break differential against the TypeScript parser:", dict(counts))


def test_the_oracle_does_see_fail_open_when_line_terminators_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import routine

    monkeypatch.setattr(routine, "_same_structure", lambda old, new, language: old.shape == new.shape)
    counts, fail_open = _differential()
    assert counts["fail_open"] > 20 and fail_open, counts


# ------------------------------------------- every modifier TypeScript can follow with a word (`canFollowModifier`)
# For each one a line break right after it is put to the compiler. Whichever the compiler reads differently must not
# be proved routine; the others may be (a break after `static`, `get` or `set` is plain reflow).

_MODIFIER_SNIPPETS = {
    "get": "class A {\n  get q() {\n    return 1;\n  }\n}\n",
    "set": "class A {\n  set q(v) {}\n}\n",
    "static": "class A {\n  static q = 1;\n}\n",
    "public": "class A {\n  public q = 1;\n}\n",
    "private": "class A {\n  private q = 1;\n}\n",
    "protected": "class A {\n  protected q = 1;\n}\n",
    "override": "class A extends B {\n  override q() {}\n}\n",
    "readonly": "class A {\n  readonly q = 1;\n}\n",
    "abstract": "abstract class A {\n  abstract q(): void;\n}\n",
    "declare": "class A {\n  declare q: number;\n}\n",
    "accessor": "class A {\n  accessor q = 1;\n}\n",
    "async": "class A {\n  async q() {}\n}\n",
    "export": "export const q = 1;\n",
    "default": "export default class {}\n",
    "const": "const enum E {\n  A,\n}\n",
    "in": "type T<in U> = U;\n",
    "out": "type T<out U> = U;\n",
}


@pytest.mark.parametrize("modifier", sorted(_MODIFIER_SNIPPETS))
def test_a_line_break_after_a_modifier_the_compiler_reads_differently_is_never_routine(modifier: str) -> None:
    old = _MODIFIER_SNIPPETS[modifier]
    new = re.sub(rf"\b{modifier} ", f"{modifier}\n  ", old, count=1)
    assert new != old
    verdict = _node_oracle([(old, new, ".ts")])
    if verdict is None:
        pytest.skip("node and the frontend's typescript package are needed for the oracle")
    if verdict[0]:  # the compiler reads both the same: no requirement either way
        return
    assert _rules("a.ts", old, new) == (None, None)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("const y = x as T;", "const y = x\n  as T;", id="as"),
        pytest.param("const y = x satisfies T;", "const y = x\n  satisfies T;", id="satisfies"),
        pytest.param("const y = (x as T).z;", "const y = (x\n  as T).z;", id="as-in-parentheses"),
        pytest.param("const y = (x satisfies T).z;", "const y = (x\n  satisfies T).z;", id="satisfies-in-parentheses"),
        pytest.param(
            "const y = a + (b as T) + c;", "const y = a + (b /* c */\n  as T) + c;", id="as-after-a-comment-line"
        ),
        pytest.param(
            "type V<T> = T extends (infer U)[] ? U : never;",
            "type V<T> = T extends (infer U)\n[] ? U : never;",
            id="array-type",
        ),
        pytest.param(
            "function g(x: unknown): asserts x is string {}",
            "function g(x: unknown): asserts\n x is string {}",
            id="asserts",
        ),
        pytest.param(
            "let m: Map<string, number[]>[] = [];", "let m: Map\n<string, number[]>[] = [];", id="type-arguments"
        ),
        pytest.param(
            "type U<T> = T extends Array<infer V> ? V : never;",
            "type U<T> = T extends Array\n<infer V> ? V : never;",
            id="infer-type-arguments",
        ),
        pytest.param("const y = f<string>(x);", "const y = f\n<string>(x);", id="call-type-arguments"),
        pytest.param(
            "type P = Promise<void>;", "type P = Promise /* c */\n<void>;", id="type-arguments-after-a-comment-line"
        ),
    ],
)
def test_typescript_postfix_and_type_keywords_do_not_survive_a_line_break(old: str, new: str) -> None:
    assert _rules("a.ts", old + "\n", new + "\n") == (None, None)


# ---------------------------------------------- content of a string, raw string, template, regex or character
# An edit inside such a literal changes the program by construction, whatever the grammar does with the whitespace or
# the comment-looking text in it. Ground truth is that construction: every offset strictly inside a literal gets a
# space, a line break and a comment, and none of the files that still parse may be proved routine.

_LITERAL_SOURCES = {
    "a.py": 'x = "ab cd"\ny = f"{x} b"\nz = """multi\nline"""\nw = b\'q\'\n',
    "a.go": "package a\n\nvar s = \"ab cd\"\nvar r = `raw\nstr`\nvar c = 'x'\n",
    "a.js": "const a = 'ab cd';\nconst t = `x ${a} z`;\nconst r = /a b+/g;\nconst u = \"q\";\n",
    "a.ts": "const a: string = 'ab cd';\nconst t = `x ${a} z`;\nconst r = /a b+/g;\ntype T = `a${string}b`;\n",
    "a.tsx": "const a = 'ab cd';\nconst t = `x ${a} z`;\nconst r = /a b+/g;\nconst e = <p title=\"a b\">x</p>;\n",
    "a.rs": (
        'fn pat() -> &\'static str {\n    r"^ab$"\n}\nfn sql() -> &\'static str {\n    r#"SELECT 1"#\n}\n'
        "fn c() -> char {\n    'x'\n}\nfn s() -> &'static str {\n    \"a b\"\n}\nfn b() -> &'static [u8] {\n    b\"by\"\n}\n"
        "fn m() -> &'static str {\n    stringify!(a+b, c)\n}\n#[derive(Debug, Clone)]\nstruct D;\n"
    ),
    "A.java": (
        'class A {\n    String s = "ab cd";\n    char c = \'x\';\n    String t = """\n        hi there\n        """;\n}\n'
    ),
}
_LITERAL_NODES = {
    "string",
    "template_string",
    "template_literal_type",
    "regex",
    "interpreted_string_literal",
    "raw_string_literal",
    "rune_literal",
    "string_literal",
    "char_literal",
    "character_literal",
    "token_tree",
}
_CODE_INSIDE = {"template_substitution", "string_interpolation", "interpolation", "template_type"}


def _literal_offsets(path: str, text: str) -> list[int]:
    tree = _parse(routine_language(path), text.encode())
    assert tree is not None, path
    offsets: set[int] = set()

    def inside_code(node_start: int, at: int, node: object) -> bool:
        return any(
            child.type in _CODE_INSIDE and child.start_byte < at < child.end_byte
            for child in node.children  # type: ignore[attr-defined]
        )

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type in _LITERAL_NODES and node.end_byte - node.start_byte > 2:
            for at in range(node.start_byte + 1, node.end_byte):
                if not inside_code(node.start_byte, at, node):
                    offsets.add(at)
    return sorted(offsets)


def _literal_mutations(path: str) -> list[tuple[str, str]]:
    old = _LITERAL_SOURCES[path]
    raw = old.encode()
    return [
        (old, (raw[:at] + glue.encode() + raw[at:]).decode())
        for at in _literal_offsets(path, old)
        for glue in (" ", "\n", "/* c */")
    ]


def _literal_differential() -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in _LITERAL_SOURCES:
        for old, new in _literal_mutations(path):
            if _parse(routine_language(path), new.encode()) is None:
                counts["no_parse"] += 1
                continue
            rules = _rules(path, old, new)
            counts["parsed"] += 1
            counts["fail_open"] += any(rule is not None for rule in rules)
    return counts


def test_nothing_changed_inside_a_literal_is_proved_routine_in_any_language() -> None:
    counts = _literal_differential()
    assert counts["fail_open"] == 0, counts
    assert counts["parsed"] > 200, counts
    print("literal-content differential:", dict(counts))


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "a.rs",
            'fn pat() -> &\'static str {\n    r"^ab$"\n}\n',
            'fn pat() -> &\'static str {\n    r" ^ab$"\n}\n',
            id="rust-raw-leading-space",
        ),
        pytest.param(
            "a.rs",
            'fn q() -> &\'static str {\n    r#"SELECT 1"#\n}\n',
            'fn q() -> &\'static str {\n    r#"\nSELECT 1"#\n}\n',
            id="rust-raw-leading-newline",
        ),
        pytest.param("a.js", "const r = /a b/;\n", "const r = /a  b/;\n", id="js-regex-space"),
        pytest.param("a.js", "const t = `a b`;\n", "const t = `a\n b`;\n", id="js-template-newline"),
        pytest.param("a.go", 'package a\n\nvar s = "a b"\n', 'package a\n\nvar s = "a  b"\n', id="go-string-space"),
        pytest.param("a.go", "package a\n\nvar s = `a\nb`\n", "package a\n\nvar s = `a\n b`\n", id="go-raw-indent"),
        pytest.param(
            "A.java",
            'class A {\n    String t = """\n        hi\n        """;\n}\n',
            'class A {\n    String t = """\n         hi\n        """;\n}\n',
            id="java-text-block-indent",
        ),
    ],
)
def test_content_of_a_literal_is_never_formatting(path: str, old: str, new: str) -> None:
    assert _rules(path, old, new) == (None, None)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("stringify!(a+b)", "stringify!(a + b)", id="spacing"),
        pytest.param("stringify!(a + b)", "stringify!(a +\n b)", id="line-break"),
        pytest.param("stringify!(a + b)", "stringify!(a + /* c */ b)", id="comment"),
        pytest.param('concat!("a", "b")', 'concat!("a","b")', id="comma-spacing"),
        pytest.param('format!("{}", x)', 'format!(\n    "{}", x\n)', id="format-reflow"),
        pytest.param("vec![1,2]", "vec![1, 2]", id="bracket-macro"),
    ],
)
def test_the_tokens_of_a_rust_macro_call_are_content_not_formatting(old: str, new: str) -> None:
    template = "fn f() -> String {{\n    {}.to_string()\n}}\n"
    assert _rules("a.rs", template.format(old), template.format(new)) == (None, None)
    # The same spacing outside a macro stays formatting.
    assert _rules("a.rs", "fn f() -> i32 {\n    a+b\n}\n", "fn f() -> i32 {\n    a + b\n}\n") != (None, None)


def test_the_tokens_of_an_attribute_are_content_too() -> None:
    old, new = "#[derive(Debug,Clone)]\nstruct D;\n", "#[derive(Debug, Clone)]\nstruct D;\n"
    assert _rules("a.rs", old, new) == (None, None)


def test_the_literal_differential_does_see_fail_open_when_literals_are_walked(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import routine

    monkeypatch.setattr(routine, "_LITERAL_TYPES", frozenset())
    counts = _literal_differential()
    assert counts["fail_open"] > 5, counts
