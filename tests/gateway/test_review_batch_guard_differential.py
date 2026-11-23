"""Differential proof for the batch guard's block pass: the guard at 892bd488a (the frozen copy in
``review_batch_guard_baseline.py``) against the current one, over seeded random Python and JavaScript/TypeScript
programs that are moved, renamed, wrapped and edited. The programs include one-line compounds, backslash headers,
flush-left multi-line strings, calls without arguments, long runs that split a change into several hunks, and
changes split between two parts that share a hunk.

Every divergence is classified. ``fail-safe``: the new guard holds a part the old one offered for batch confirm.
``fail-open``: the new guard offers a part the old one held. There must be none of the second kind. Separately, the
generator knows which statements ended up in another block (it edits a tree), and the new guard must hold every
such change: ``new_missed`` must be zero.
"""

from __future__ import annotations

import difflib
import itertools
import random
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.review.batch_guard import guard_for_target as new_guard
from tests.gateway.review_batch_guard_baseline import guard_for_target as old_guard
from tests.gateway.test_review_batch_guard import _JS_BLOCKS, _PY_BLOCKS, _hunks, _spans, _target

SEED = 20261109
SAMPLES = 900  # per language; about 10 s
GIT_SAMPLES = 25  # per language, diffs made by git itself instead of difflib
MUTATIONS = (1, 3)  # edits per program

_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_UIDS = itertools.count()


@dataclass
class _Node:
    """A statement (no clauses) or a compound: clauses of ``(header, children)`` (if/elif/else, try/except)."""

    text: str = ""
    clauses: list[tuple[str, list[_Node]]] = field(default_factory=list)
    close: str = ""  # script: what follows the closing brace
    style: str = "block"  # block | line (a one-line body) | braceless (script)
    slash: bool = False  # python: the first header continues on a second row with a backslash
    fmt: str = ""  # a call written wrapped over rows, with a trailing comma, or (script) without its ";"
    inert: bool = False  # a comment: it may move anywhere, nothing runs
    deco: str = ""  # a decorator line above the definition (python def) or the method (script class)
    member: bool = False  # a method of a class
    outer: str = ""  # python: a line above the clauses (``match k:``), which sit one level in
    uid: int = field(default_factory=lambda: next(_UIDS))

    @property
    def leaf(self) -> bool:
        return not self.clauses


_PY_LEAVES = [
    "send(f'{x}')",
    "send('a')",
    'return "a"',
    "limit = 10",
    "use(x)",
    "log(x)",
    "total += x",
    "y = x * 2",
    "step(a, b)",
    "n = len(a)",
    "emit(y)",
    "check(b)",
    "x = a[i]",
    "count = count + 1",
    "save(y)",
    "pass_on(x, y)",
    "value = x - y",
    "flag = not flag",
    "seen.add(x)",
    "print(x)",
    "cleanup()",
    "release()",
    "init()",
    'SQL = """\nSELECT id FROM t\n"""',
]
_PY_INERT = ['"""\nif legacy:\n"""', "# if legacy:"]
_JS_INERT = ["/* disabled for now:\nif (legacy) {\n*/", "// if (legacy) {", "/* } */"]  # fmt: skip
_JS_LEAVES = [
    "send(`a${x}`);",
    "send(`a`);",
    "send('a');",
    'return "a";',
    "limit = 10;",
    "let x = 2;",
    "use(x);",
    "log(x);",
    "total += x;",
    "y = x * 2;",
    "step(a, b);",
    "n = a.length;",
    "emit(y);",
    "check(b);",
    "x = a[i];",
    "count = count + 1;",
    "save(y);",
    "passOn(x, y);",
    "value = x - y;",
    "flag = !flag;",
    "seen.add(x);",
    "console.log(x);",
    "cleanup();",
    "release();",
    "init();",
    "const q = `\nSELECT id FROM t\n`;",
    "const w = `\n{ ${x} }\n`;",
    "const area = width\n    * height;",
    "let list: Array<string>",
    "const index = new Map<string, number>()",
]
_TSX_LEAVES = ["const icon = <Icon />", "label = <p>{ok && <a>see http://example.com</a>}</p>;"]  # fmt: skip
_PY_COMPOUNDS = [
    [("case 1:", 1), ("case 2:", 1)],
    [("def inner(v):", 1)],
    [("for x in xs:", 1)],
    [("for i in range(n):", 1)],
    [("while i < n:", 1)],
    [("if a:", 1), ("elif b:", 1), ("else:", 1)],
    [("if a:", 1), ("else:", 1)],
    [("if flag:", 1)],
    [("try:", 1), ("except OSError:", 1), ("finally:", 1)],
    [("try:", 1), ("except OSError:", 1)],
    [("with open(path) as fh:", 1)],
]
_JS_COMPOUNDS = [
    ([("class K {", 1)], ""),
    ([("class K {", 1)], ""),
    ([("class K {", 1)], ""),
    ([("{", 1)], ""),
    ([("for (const x of xs) {", 1)], ""),
    ([("for (let i = 0; i < n; i++) {", 1)], ""),
    ([("while (i < n) {", 1)], ""),
    ([("if (a) {", 1), ("} else if (b) {", 1), ("} else {", 1)], ""),
    ([("if (a) {", 1), ("} else {", 1)], ""),
    ([("if (flag) {", 1)], ""),
    ([("try {", 1), ("} catch (e) {", 1), ("} finally {", 1)], ""),
    ([("try {", 1), ("} catch (e) {", 1)], ""),
    ([("xs.forEach((x) => {", 1)], ");"),
    ([("items.forEach((type) => {", 1)], ");"),
    ([("import('./mod').then((m) => {", 1)], ");"),
    ([("const inner = function (y) {", 1)], ";"),
    ([("case 1:", 1), ("default:", 1)], ""),
]
_SIMPLE_JS = ("if", "else", "for", "while")


def _body(rng: random.Random, python: bool, depth: int, tsx: bool = False) -> list[_Node]:
    leaves = _PY_LEAVES if python else _JS_LEAVES + (_TSX_LEAVES if tsx else [])
    nodes: list[_Node] = []
    for _ in range(rng.randint(1, 3)):
        roll = rng.random()
        if depth < 2 and roll < 0.45:
            if python:
                shape, close = rng.choice(_PY_COMPOUNDS), ""
            else:
                shape, close = rng.choice(_JS_COMPOUNDS)
            node = _Node(clauses=[(head, _body(rng, python, depth + 1, tsx)) for head, _ in shape], close=close)
            if python and shape[0][0].startswith("def inner") and rng.random() < 0.6:
                node.deco = "@login_required"
            if not python and shape[0][0] == "class K {":
                methods = [
                    _Node(
                        clauses=[
                            (
                                f"{name}(p: P, @Inject(T) x: X, q: Q) {{" if rng.random() < 0.5 else f"{name}() {{",
                                _body(rng, python, depth + 2, tsx),
                            )
                        ],
                        member=True,
                    )
                    for name in ("a", "b")
                ]
                rng.choice(methods).deco = "@Get()"
                node.clauses = [(shape[0][0], methods)]
            node.outer = "switch (a) {" if not python and shape[0][0].startswith("case") else ""
            node.outer = "match k:" if python and shape[0][0].startswith("case") else node.outer
            single = _inline_ok(node)
            if python:
                node.style = "line" if single and rng.random() < 0.3 else "block"
                node.slash = (
                    node.style == "block" and node.clauses[0][0].startswith(("if ", "while ")) and rng.random() < 0.2
                )
            elif single and all(h.removeprefix("} ").startswith(_SIMPLE_JS) for h, _ in node.clauses):
                node.style = rng.choice(["block", "line", "braceless"])
            nodes.append(node)
        elif roll < 0.55:
            nodes.append(_Node(rng.choice(leaves)))
            nodes.extend(_Node(f"pad_{rng.getrandbits(24)}()" + ("" if python else ";")) for _ in range(8))
        elif roll < 0.62:
            nodes.append(_Node(rng.choice(_PY_INERT if python else _JS_INERT), inert=True))
        else:
            node = _Node(rng.choice(leaves))
            if rng.random() < 0.15:
                node.fmt = rng.choice(["wrap", "wrap_comma"] if python else ["wrap", "wrap_comma", "nosemi"])
            nodes.append(node)
    return nodes


def _inline_ok(node: _Node) -> bool:
    return all(
        len(kids) == 1 and kids[0].leaf and "\n" not in kids[0].text and kids[0].fmt in ("", "nosemi")
        for _, kids in node.clauses
    )


_CALL = re.compile(r"^([\w.]+)\((.+)\)(;?)$")


def _one_line(node: _Node) -> str:
    return node.text.removesuffix(";") if node.fmt == "nosemi" else node.text


def _leaf_rows(node: _Node, pad: str, step: str) -> list[str]:
    text = _one_line(node)
    call = _CALL.match(text)
    if node.fmt in ("wrap", "wrap_comma") and call and "\n" not in text:
        name, args, semi = call.groups()
        parts = args.split(", ")
        comma = "," if node.fmt == "wrap_comma" else ""
        rows = [f"{pad}{step}{part}," for part in parts[:-1]] + [f"{pad}{step}{parts[-1]}{comma}"]
        return [f"{pad}{name}(", *rows, f"{pad}){semi}"]
    first, *rest = text.split("\n")
    return [pad + first, *rest]  # a multi-line string or comment keeps its inner rows flush left


def _render(nodes: list[_Node], python: bool, level: int, out: list[str]) -> None:
    step = "    " if python else "  "
    pad = step * level
    for node in nodes:
        if node.leaf:
            out.extend(_leaf_rows(node, pad, step))
            continue
        style = node.style if _inline_ok(node) else "block"
        if node.deco:
            out.append(pad + node.deco)
        if node.outer:
            out.append(pad + node.outer)
            level += 1
            pad = step * level
        for index, (head, children) in enumerate(node.clauses):
            if python:
                if style == "line":
                    out.append(f"{pad}{head} {_one_line(children[0])}")
                    continue
                if index == 0 and node.slash:
                    out.append(pad + head[:-1] + " and \\")
                    out.append(pad + "        flag:")
                else:
                    out.append(pad + head)
                _render(children, python, level + 1, out)
            elif style == "braceless":
                out.append(f"{pad}{head.removeprefix('} ').removesuffix(' {')} {_one_line(children[0])}")
            elif style == "line":
                out.append(f"{pad}{head} {_one_line(children[0])} " + ("}" if index == len(node.clauses) - 1 else ""))
                if index == len(node.clauses) - 1:
                    out[-1] += node.close
                    out[-1] = out[-1].rstrip()
            else:  # K&R chains: a "} else {" head closes the clause before it
                out.append(pad + head)
                _render(children, python, level + 1, out)
                if index == len(node.clauses) - 1 and not node.outer:
                    out.append(pad + "}" + node.close)
        if node.outer:
            level -= 1
            pad = step * level
            if not python:
                out.append(pad + "}")


def _lines(body: list[_Node], python: bool) -> list[str]:
    out: list[str] = ["def f(a, b, xs, n, path, flag):" if python else "function f(a, b, xs, n, path, flag) {"]
    _render(body, python, 1, out)
    if not python:
        out.append("}")
    return out


def _every_node(nodes: list[_Node]) -> list[_Node]:
    found = list(nodes)
    for node in nodes:
        for _, children in node.clauses:
            found.extend(_every_node(children))
    return found


def _blocks(nodes: list[_Node]) -> list[list[_Node]]:
    found = [] if nodes and nodes[0].member else [nodes]  # a class body holds methods, not statements
    for node in nodes:
        for _, children in node.clauses:
            found.extend(_blocks(children))
    return found


def _paths(nodes: list[_Node], prefix: tuple[tuple[int, int], ...] = ()) -> dict[Any, tuple[tuple[int, int], ...]]:
    """Per statement, the blocks around it (compound and clause): what the guard has to notice changing."""

    out: dict[Any, tuple[tuple[int, int], ...]] = {}
    for node in nodes:
        if node.member and "@Inject(T) " in node.clauses[0][0]:
            decorated = node.clauses[0][0].split("@Inject(T) ")[1].split(":")[0]
            out[f"param:{node.uid}"] = (*prefix, (node.uid, sum(map(ord, decorated))))
        if node.deco:
            out["deco:" + node.deco] = (*prefix, (node.uid, -1))
        if node.leaf:
            out[node.uid] = prefix
        else:
            for index, (_, kids) in enumerate(node.clauses):
                out.update(_paths(kids, (*prefix, (node.uid, index))))
    return out


def _mutate(rng: random.Random, body: list[_Node], python: bool, moves: list[str]) -> None:
    leaves = _PY_LEAVES if python else _JS_LEAVES
    blocks = _blocks(body)
    holders = [(block, i) for block in blocks for i, node in enumerate(block) if node.leaf]
    kind = rng.choice(
        [
            "move", "move", "move", "rename", "expr", "delete", "insert", "header", "wrap", "move_edit", "move_fmt",
            "fmt", "move_quote", "move_lit", "deco_move", "deco_move", "param_move",
        ]
    )  # fmt: skip
    filler = "pass" if python else "noop();"
    wraps = ["wrap", "wrap_comma"] if python else ["wrap", "wrap_comma", "nosemi"]
    if kind == "fmt" and holders:  # only the formatting of one statement changes
        block, index = rng.choice(holders)
        block[index].fmt = "" if block[index].fmt else rng.choice(wraps)
        moves.append("fmt")
    elif kind == "param_move":  # the decorator goes to another parameter and the list shifts
        members = [n for n in _every_node(body) if n.member and "@Inject(T) x: X" in n.clauses[0][0]]
        if members:
            member = rng.choice(members)
            member.clauses[0] = (member.clauses[0][0].split("(")[0] + "(x: X, @Inject(T) q: Q) {", member.clauses[0][1])
            moves.append("param_move")
    elif kind == "deco_move":
        defs = [n for n in _every_node(body) if n.member or (n.clauses and n.clauses[0][0].startswith("def "))]
        if any(n.deco for n in defs):
            carrier = next(n for n in defs if n.deco)
            others = [n for n in defs if n is not carrier]
            if not others and holders and python:  # a second definition to move the decorator to
                block, index = rng.choice(holders)
                spare = _Node(clauses=[("def spare(v):", [block[index]])])
                block[index] = spare
                others = [spare]
            if others:
                others[0].deco, carrier.deco = carrier.deco, ""
                moves.append("deco_move")
    elif kind in ("move", "move_edit", "move_fmt", "move_quote", "move_lit") and holders:
        block, index = rng.choice(holders)
        node = block.pop(index)
        if not block:
            block.append(_Node(filler))
        if kind == "move_quote":
            node.text = node.text.translate(str.maketrans("'\"", "\"'"))
            if "`" in node.text and "${" not in node.text:
                node.text = node.text.replace("`", '"')
        if kind == "move_lit":
            node.text = re.sub(r"\b10\b", "11", node.text.replace("'a'", "'b'").replace('"a"', '"b"'))
        if kind == "move_edit":
            node.text = re.sub(r"\bx\b", "item", node.text)
        if kind == "move_fmt":
            node.fmt = "" if node.fmt else rng.choice(wraps)
        target = rng.choice(_blocks(body))
        target.insert(rng.randint(0, len(target)), node)
        moves.append("move")
    elif kind == "rename":
        old, new = rng.choice([("x", "item"), ("y", "result"), ("total", "acc"), ("count", "k")])
        pattern = re.compile(rf"\b{old}\b")
        for block in blocks:
            for node in block:
                node.text = pattern.sub(new, node.text)
                node.clauses = [(pattern.sub(new, head), kids) for head, kids in node.clauses]
        moves.append("rename")
    elif kind == "expr" and holders:
        block, index = rng.choice(holders)
        node = block[index]
        node.text = (
            node.text.replace("* 2", "* 3").replace("+=", "-=").replace(" - ", " + ").replace("count + 1", "count + 2")
        )
        moves.append("expr")
    elif kind == "delete" and holders:
        block, index = rng.choice(holders)
        block.pop(index)
        if not block:
            block.append(_Node(filler))
        moves.append("delete")
    elif kind == "insert":
        target = rng.choice(blocks)
        target.insert(rng.randint(0, len(target)), _Node(rng.choice(leaves)))
        moves.append("insert")
    elif kind == "header":
        for block in blocks:
            for node in block:
                if node.clauses and rng.random() < 0.5:
                    head, kids = node.clauses[0]
                    swapped = head.replace("i < n", "i <= n").replace("range(n)", "range(n - 1)").replace("xs", "ys")
                    node.clauses[0] = (swapped, kids)
        moves.append("header")
    elif kind == "wrap" and holders:
        block, index = rng.choice(holders)
        head = "if a:" if python else "if (a) {"
        block[index] = _Node(clauses=[(head, [block[index]])], close="")
        moves.append("wrap")


def _texts(nodes: list[_Node]) -> dict[Any, str]:
    out: dict[Any, str] = {}
    for node in nodes:
        if node.member and "@Inject(T) " in node.clauses[0][0]:
            out[f"param:{node.uid}"] = f"param:{node.uid}"
        if node.deco:
            out["deco:" + node.deco] = node.deco
        if node.leaf:
            out[node.uid] = node.text
        else:
            for _, kids in node.clauses:
                out.update(_texts(kids))
    return out


def _same_text(text: str) -> str:
    """A statement's text as the guard compares it: quote style does not count."""

    return text.replace("'", '"').replace("`", '"')


def _clone(body: list[_Node]) -> list[_Node]:
    return [
        _Node(
            n.text,
            [(h, _clone(k)) for h, k in n.clauses],
            n.close,
            n.style,
            n.slash,
            n.fmt,
            n.inert,
            n.deco,
            n.member,
            n.outer,
            n.uid,
        )
        for n in body
    ]


def _inert(nodes: list[_Node]) -> set[int]:
    found = {n.uid for n in nodes if n.inert}
    for node in nodes:
        for _, kids in node.clauses:
            found |= _inert(kids)
    return found


def _diff_hunks(old: list[str], new: list[str]) -> list[dict[str, Any]]:
    hunks: list[dict[str, Any]] = []
    for line in difflib.unified_diff(old, new, n=3, lineterm=""):
        match = _HEADER.match(line)
        if match:
            a, b, c, d = match.groups()
            hunks.append(
                {
                    "old_start": int(a), "old_lines": int(b if b is not None else 1),
                    "new_start": int(c), "new_lines": int(d if d is not None else 1),
                    "patch": "",
                }
            )  # fmt: skip
        elif hunks and line[:1] in (" ", "+", "-"):
            hunks[-1]["patch"] += f"{line}\n"
    return hunks


def _verdicts(path: str, hunks: list[dict[str, Any]], spans: Any = None, texts: tuple[str, str] | None = None) -> Any:
    """The earlier guard and the current one (which also reads the whole file when it has the two texts)."""

    target = _target(hunks, path)
    if spans is not None:
        target = replace(target, spans=tuple(spans))
    entry = {"path": path, "status": "modified", "hunks": hunks}
    reader = None if texts is None else (lambda _path, side: texts[0] if side == "old" else texts[1])
    return old_guard(target, entry), new_guard(target, entry, reader, {})


@dataclass
class _Tally:
    samples: int = 0
    split_parts: int = 0
    same_guarded: int = 0
    same_open: int = 0
    fail_safe: Counter[str] = field(default_factory=Counter)
    fail_open: list[str] = field(default_factory=list)
    moves_across: int = 0
    old_missed: int = 0
    new_missed: int = 0
    misses: list[str] = field(default_factory=list)

    def classify(self, before: Any, now: Any, program: str) -> None:
        if before.guarded == now.guarded:
            self.same_guarded += before.guarded
            self.same_open += not before.guarded
        elif now.guarded:
            self.fail_safe[now.code] += 1
        else:
            self.fail_open.append(program)


def _run(python: bool, tmp_path: Path, rng: random.Random, tally: _Tally, *, git: bool) -> None:
    for _ in range(GIT_SAMPLES if git else SAMPLES):
        tsx = not python and rng.random() < 0.5
        path = "app/prog.py" if python else ("app/prog.tsx" if tsx else "app/prog.ts")
        body = _body(rng, python, 0, tsx)
        after = _clone(body)
        for _ in range(rng.randint(*MUTATIONS)):
            _mutate(rng, after, python, [])
        old_text, new_text = _lines(body, python), _lines(after, python)
        if old_text == new_text:
            continue
        program = "\n".join(old_text) + "\n=====\n" + "\n".join(new_text)
        if git:
            hunks = _hunks(tmp_path, path, "\n".join(old_text) + "\n", "\n".join(new_text) + "\n")
        else:
            hunks = _diff_hunks(old_text, new_text)
        if not hunks:
            continue
        texts = ("\n".join(old_text) + "\n", "\n".join(new_text) + "\n")
        before, now = _verdicts(path, hunks, texts=texts)
        tally.samples += 1
        tally.classify(before, now, program)
        before_paths, after_paths = _paths(body), _paths(after)
        old_texts, new_texts = _texts(body), _texts(after)
        inert = _inert(body) | _inert(after)
        copies = Counter(_same_text(t) for t in [*old_texts.values(), *new_texts.values()])
        # Moved statements whose text appears twice cannot be told from "one stayed, a copy was added": left out.
        if any(
            uid in after_paths
            and uid not in inert
            and after_paths[uid] != place
            and copies[_same_text(old_texts[uid])] <= 2
            and copies[_same_text(new_texts[uid])] <= 2
            for uid, place in before_paths.items()
        ):
            tally.moves_across += 1
            tally.old_missed += not before.guarded
            if not now.guarded:
                tally.new_missed += 1
                tally.misses.append(program)
        spans = _spans(hunks)
        if rng.random() < 0.4 and len(spans) > 1:  # two parts that share the hunks: each owns some of the lines
            cut = rng.randint(1, len(spans) - 1)
            for owned in (spans[:cut], spans[cut:]):
                tally.split_parts += 1
                tally.classify(*_verdicts(path, hunks, owned, texts), program)


def test_the_new_guard_never_offers_a_part_the_old_guard_held(tmp_path: Path) -> None:
    results: dict[str, _Tally] = {}
    for name, python in (("python", True), ("script", False)):
        rng = random.Random(f"{SEED}:{name}")
        tally = _Tally()
        _run(python, tmp_path, rng, tally, git=False)
        _run(python, tmp_path, rng, tally, git=True)
        results[name] = tally
    for name, tally in results.items():
        print(
            f"\nbatch guard differential [{name}]: samples={tally.samples} split_parts={tally.split_parts} same_guarded={tally.same_guarded} "
            f"same_unguarded={tally.same_open} fail_safe={sum(tally.fail_safe.values())} {dict(tally.fail_safe)} "
            f"fail_open={len(tally.fail_open)} | moved across a block: {tally.moves_across}, old offered "
            f"{tally.old_missed}, new offers {tally.new_missed}"
        )
    for name, tally in results.items():
        assert not tally.fail_open, f"{name}: the new guard offers a part the old one held:\n{tally.fail_open[0]}"
        assert tally.samples > 300 and tally.split_parts > 100
        assert tally.moves_across > 100
        assert (
            tally.new_missed == 0
        ), f"{name}: a change that moves a statement to another block is offered:\n{tally.misses[0]}"
        assert sum(tally.fail_safe.values()) > 0, f"{name}: the new guard never held anything extra"


def test_the_named_block_moves_are_offered_by_the_old_guard_and_held_by_the_new(tmp_path: Path) -> None:
    for table, name in ((_PY_BLOCKS, "app/blocks.py"), (_JS_BLOCKS, "app/blocks.ts")):
        for label, (base, head, _) in table.items():
            before, now = _verdicts(name, _hunks(tmp_path, name, base, head))
            assert now.guarded, label
            assert (before.guarded, now.guarded) in {(False, True), (True, True)}, label
    offered = [
        label
        for table, name in ((_PY_BLOCKS, "app/blocks.py"), (_JS_BLOCKS, "app/blocks.ts"))
        for label, (base, head, _) in table.items()
        if not _verdicts(name, _hunks(tmp_path, name, base, head))[0].guarded
    ]
    assert len(offered) >= 8, offered


def test_the_generator_is_seeded() -> None:
    first = _lines(_body(random.Random("a"), True, 0), True)
    assert first == _lines(_body(random.Random("a"), True, 0), True)
