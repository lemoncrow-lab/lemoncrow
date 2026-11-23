"""P11-3: randomized move programs over real source files, per language, through ``derive_units`` and ``reconcile``.

``test_review_symbol_moves`` proves the rule on synthetic units. This file proves it on what the product really
derives: every program writes real files in one language, commits a base, refreshes Review three or four times
(``derive_units`` and ``reconcile`` run for real, with a real store, real marks and real baselines), and then checks
two things:

* **fail-open == 0**, from invariants that do not trust the implementation: a ``reviewed`` mark always sits on exactly the
  content its reviewer attested, content a reviewer did not judge is never ``reviewed`` because of a move unless it is
  the only copy of attested content, and a reviewer's own later decision on a destination key is never overwritten;
* for programs of one hop, an exact oracle written from the product rule (one source, one destination, nothing else of
  that content; same body under another name reopens), so a *missed* carry shows up as a divergence too.

Categories exercised per language: plain moves, copies, ambiguous twins, move+edit, renames, same-path class-to-class
moves, a file rename combined with a symbol move in the same revision, ``unknown`` fingerprint methods, baseline drift
across three or more revisions and existing marks on destination keys. ``LEMONCROW_MOVE_PROGRAMS`` raises the number of
programs per language for a larger local run (40 per language, 13 languages, ran clean).

Two product limits the oracle pins on purpose (both fail-safe, a part reopens): a top-level definition cannot move out of
a *deleted* file, because the file's placeholder unit reuses its key; and a method moved to the last position of a class
or file hashes differently there, which is why the generator always renders a fixed tail member.
"""

from __future__ import annotations

import os
import random
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.revisions import reconcile
from lemoncrow.pro.capabilities.review.session_models import ReviewUnit
from lemoncrow.pro.capabilities.review.sources import local
from lemoncrow.pro.capabilities.review.sources.local import mark_unit, open_or_create_session, refresh
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.units import bare_fingerprint, owns_deleted_text

PROGRAMS = int(os.environ.get("LEMONCROW_MOVE_PROGRAMS", "10"))
WHY: list[str] = []


@dataclass(frozen=True)
class Lang:
    name: str
    ext: str
    comment: str
    prelude: tuple[str, ...]
    define: tuple[str, ...]  # lines of one definition with {n} the name and {k} the shape; no indentation
    hosted: str  # "never" | "sometimes" | "always"
    open_host: str = ""  # {h} is the class name
    close_host: str = ""
    indent: str = "    "
    method: tuple[str, ...] = ()  # the definition when it lives in a class; defaults to ``define``
    decorator: str = (
        ""  # a line that decorates a definition (decorator, annotation, attribute), "" when the language has none
    )
    multiline: str = ""  # the same kind of attribute written over several lines (as a formatter does), where legal


_C_LIKE = ("int {n}(int v) {{", "    int total = v + {k};", "    return total;", "}}")
LANGS: tuple[Lang, ...] = (
    Lang(
        "python",
        "py",
        "#",
        (),
        ("def {n}(v):", "    total = v + {k}", "    return total"),
        "sometimes",
        "class {h}:",
        "",
        method=("def {n}(self, v):", "    total = v + {k}", "    return total"),
        decorator="@login_required",
    ),
    Lang(
        "typescript",
        "ts",
        "//",
        (),
        ("function {n}(v: number): number {{", "  const total = v + {k};", "  return total;", "}}"),
        "sometimes",
        "class {h} {{",
        "}}",
        "  ",
        method=("{n}(v: number): number {{", "  const total = v + {k};", "  return total;", "}}"),
        decorator="@sealed",
    ),
    Lang(
        "javascript",
        "js",
        "//",
        (),
        ("function {n}(v) {{", "  const total = v + {k};", "  return total;", "}}"),
        "sometimes",
        "class {h} {{",
        "}}",
        "  ",
        method=("{n}(v) {{", "  const total = v + {k};", "  return total;", "}}"),
    ),
    Lang(
        "go",
        "go",
        "//",
        ("package a",),
        ("func {n}(v int) int {{", "\ttotal := v + {k}", "\treturn total", "}}"),
        "never",
    ),
    Lang("java", "java", "//", (), _C_LIKE, "always", "class {h} {{", "}}", decorator="@Deprecated"),
    Lang(
        "rust",
        "rs",
        "//",
        (),
        ("fn {n}(v: i32) -> i32 {{", "    let total = v + {k};", "    total", "}}"),
        "never",
        decorator="#[inline]",
        multiline="#[derive(\n    Debug,\n    Clone,\n)]",
    ),
    Lang("ruby", "rb", "#", (), ("def {n}(v)", "  total = v + {k}", "  total", "end"), "never"),
    Lang("c", "c", "//", (), _C_LIKE, "never"),
    Lang("cpp", "cpp", "//", (), _C_LIKE, "never"),
    Lang(
        "csharp",
        "cs",
        "//",
        (),
        _C_LIKE,
        "always",
        "class {h} {{",
        "}}",
        decorator="[Obsolete]",
        multiline='[Route(\n    "x",\n)]',
    ),
    Lang(
        "php",
        "php",
        "//",
        ("<?php",),
        ("function {n}($v) {{", "    $total = $v + {k};", "    return $total;", "}}"),
        "never",
        decorator="#[Pure]",
        multiline="#[Route(\n    '/x',\n)]",
    ),
    Lang(
        "kotlin",
        "kt",
        "//",
        (),
        ("fun {n}(v: Int): Int {{", "    val total = v + {k}", "    return total", "}}"),
        "never",
        # No decorator here on purpose: a top-level annotation makes the symbol deriver name the unit after the annotation
        # (``@JvmStatic fun fn_a`` derives a unit called ``JvmStatic`` and none for ``fn_a``). Pre-existing, fail-safe
        # for moves, and reported separately; the decorator case for Kotlin would only test that derivation.
    ),
    Lang(
        "swift",
        "swift",
        "//",
        (),
        ("func {n}(_ v: Int) -> Int {{", "    let total = v + {k}", "    return total", "}}"),
        "never",
        decorator="@discardableResult",
    ),
)

# A symbol of the model: (path, host class or "", leaf name, shape). ``shape`` is the body; the name is not part of it.
Sym = tuple[str, str, str, int]
_NAMES = [f"fn_{c}" for c in "abcdefghij"]
_RENAMES = [f"ren_{i}" for i in range(5)]
_HOSTS = ("HostA", "HostB")
_SENTINEL = "zz_tail"
_STATES = ("reviewed", "needs_changes", "later")
_STATE_WEIGHTS = (70, 15, 15)
_OPS = ("keep", "move", "rename", "edit", "delete", "copy", "move_two", "move_edit", "class_move")
_WEIGHTS = (22, 25, 8, 8, 5, 8, 4, 8, 12)


def _qname(sym: Sym) -> str:
    return f"{sym[1]}.{sym[2]}" if sym[1] else sym[2]


def _render(
    lang: Lang,
    path: str,
    symbols: list[Sym],
    filler: int = 0,
    decorated: frozenset[str] = frozenset(),
    gap: bool = False,
) -> str:
    """``gap`` puts a blank line between each decorator and its definition (legal in every language here that has one)."""

    lines = [*lang.prelude, f"{lang.comment} {path}", *(f"{lang.comment} filler {i}" for i in range(filler)), ""]
    hosts: list[str] = []
    for sym in symbols:
        if sym[1] and sym[1] not in hosts:
            hosts.append(sym[1])
    for host in hosts:
        lines.append(lang.open_host.format(h=host).replace("{{", "{"))
        for sym in (s for s in symbols if s[1] == host):
            template = lang.method or lang.define
            if _qname(sym) in decorated:
                lines.extend([lang.indent + lang.decorator, *([""] if gap else [])])
            lines.extend(lang.indent + line for line in _fill(template, sym))
        # The window of the *last* definition in a file or class reaches the closing brace or the end of the file, so
        # the same method would hash differently there. A fixed tail member keeps every modelled method in the middle.
        lines.extend(lang.indent + line for line in _fill(template, (path, host, _SENTINEL, 0)))
        if lang.close_host:
            lines.append(lang.close_host.replace("}}", "}"))
        lines.append("")
    top_level = [s for s in symbols if not s[1]]
    for sym in [*top_level, (path, "", _SENTINEL, 0)] if top_level else []:
        if _qname(sym) in decorated:
            lines.extend([lang.decorator, *([""] if gap else [])])
        lines.extend(_fill(lang.define, sym))
        lines.append("")
    return "\n".join(lines) + "\n"


def _fill(template: tuple[str, ...], sym: Sym) -> list[str]:
    return [line.format(n=sym[2], k=sym[3]) for line in template]


def _write_tree(
    root: Path,
    lang: Lang,
    model: list[Sym],
    *,
    keep: frozenset[str] = frozenset(),
    filler: int = 0,
    decorated: frozenset[str] = frozenset(),
    gap: bool = False,
) -> None:
    for old in root.glob("src/*"):
        old.unlink()
    (root / "src").mkdir(exist_ok=True)
    by_path: dict[str, list[Sym]] = {}
    for sym in model:
        by_path.setdefault(sym[0], []).append(sym)
    for path in keep - by_path.keys():
        by_path[path] = []  # emptied but still there: the file is modified, not deleted
    for path, symbols in by_path.items():
        (root / path).write_text(_render(lang, path, symbols, filler, decorated, gap), encoding="utf-8")


def _kept(lang: Lang, previous: list[Sym]) -> frozenset[str]:
    """The files a program empties but leaves in place (the even ones); the odd ones are deleted when emptied."""

    return frozenset(
        path for index, path in enumerate(_paths(lang)) if index % 2 == 0 and any(s[0] == path for s in previous)
    )


def _paths(lang: Lang) -> list[str]:
    return [f"src/m{i}.{lang.ext}" for i in range(4)]


def _initial(rng: random.Random, lang: Lang, hosted: bool) -> list[Sym]:
    paths, used, out = _paths(lang)[:3], set(), []
    for _ in range(rng.randint(5, 9)):
        path, name = rng.choice(paths), rng.choice(_NAMES)
        host = rng.choice(_HOSTS) if hosted else ""
        if (path, host, name) not in used:
            used.add((path, host, name))
            out.append((path, host, name, rng.randrange(1, 6)))
    return out


def _step(rng: random.Random, lang: Lang, hosted: bool, current: list[Sym], *, fresh: Any) -> list[Sym]:
    """One revision of edits on *current* (the model), drawn from the operations the rule distinguishes."""

    paths = _paths(lang)
    out: list[Sym] = []
    taken: set[tuple[str, str, str]] = set()

    def put(path: str, host: str, name: str, shape: int) -> None:
        if (path, host, name) not in taken:
            taken.add((path, host, name))
            out.append((path, host, name, shape))

    for path, host, name, shape in current:
        others = [p for p in paths if p != path]
        op = rng.choices(_OPS, _WEIGHTS)[0]
        if op == "class_move" and not host:
            op = "move"
        match op:
            case "keep":
                put(path, host, name, shape)
            case "move":
                put(rng.choice(others), host, name, shape)
            case "rename":
                put(rng.choice(paths), host, rng.choice(_RENAMES), shape)
            case "edit":
                put(path, host, name, next(fresh))
            case "copy":
                put(path, host, name, shape)
                put(rng.choice(others), host, name, shape)
            case "move_two":
                for destination in rng.sample(others, 2):
                    put(destination, host, name, shape)
            case "move_edit":
                put(rng.choice(others), host, name, next(fresh))
            case "class_move":
                put(path, next(h for h in _HOSTS if h != host), name, shape)
    return out


def _expected(
    previous: list[tuple[Sym, str]], following: list[Sym], deleted_files: frozenset[str]
) -> tuple[dict[tuple[str, str], str], dict[str, int]]:
    """What the product rule says each *reviewed/requested/later* mark becomes, from the model alone.

    Returns ``{(path, qualified name): state}`` of the marks expected to exist and per-category counts of the
    reviewed ones.
    """

    before = {(s[0], _qname(s)): s[3] for s, _state in previous}
    after = {(s[0], _qname(s)): s[3] for s in following}
    prev = [s for s, _state in previous]
    content_before = Counter((_qname(s), s[3]) for s in prev)
    content_after = Counter((_qname(s), s[3]) for s in following)
    # A deleted file keeps a placeholder unit under each top-level definition's own key, so that key never counts as
    # removed and a top-level definition cannot move out of a deleted file (fail-safe: it reopens). A method's placeholder
    # is keyed without its class, so the method's key does go away and it can move. Pinned here so a change shows.
    stuck = {(s[0], _qname(s)) for s in prev if s[0] in deleted_files and not s[1]}
    removed = [s for s in prev if (s[0], _qname(s)) not in after and (s[0], _qname(s)) not in stuck]
    added = [s for s in following if (s[0], _qname(s)) not in before]
    moved: dict[tuple[str, str], tuple[str, str]] = {}
    for src in removed:
        twins = [t for t in added if (_qname(t), t[3]) == (_qname(src), src[3])]
        if len(twins) == 1 and content_before[(_qname(src), src[3])] == 1 and content_after[(_qname(src), src[3])] == 1:
            moved[(src[0], _qname(src))] = (twins[0][0], _qname(twins[0]))
    moved_to = set(moved.values())
    renamed: dict[tuple[str, str], tuple[str, str]] = {}
    for src in removed:
        if (src[0], _qname(src)) in moved:
            continue
        olds = [r for r in removed if (r[0], _qname(r)) not in moved and r[3] == src[3]]
        news = [a for a in added if (a[0], _qname(a)) not in moved_to and a[3] == src[3]]
        if len(olds) == 1 and len(news) == 1 and _qname(news[0]) != _qname(src):
            renamed[(src[0], _qname(src))] = (news[0][0], _qname(news[0]))
    expected: dict[tuple[str, str], str] = {}
    category: Counter[str] = Counter()
    for sym, state in previous:
        key = (sym[0], _qname(sym))
        name = "reviewed" if state == "reviewed" else "other"
        if key in after:
            if after[key] == sym[3]:
                expected[key] = state
                category[f"carried_{name}"] += 1
            else:
                category[f"changed_{name}"] += 1  # reopened: no valid mark remains on the new content
        elif key in moved:
            expected[moved[key]] = state
            category[f"moved_{name}"] += 1
        elif key in renamed and state == "reviewed":
            category["renamed_reviewed"] += 1  # reopened and labelled, never carried
        elif key in stuck:
            category[f"deleted_file_{name}"] += 1
        elif any((_qname(t), t[3]) == (_qname(sym), sym[3]) for t in added):
            category[f"ambiguous_{name}"] += 1
        else:
            category[f"gone_{name}"] += 1
    return expected, dict(category)


class _Repo:
    """One program's repository: a base commit, the working tree the model renders to, and a Review session."""

    def __init__(self, root: Path, store_root: Path, lang: Lang, base: list[Sym], *, filler: int = 0) -> None:
        # ``filler`` identical comment lines in every file: git only reports a rename when the files are similar enough,
        # and these fixtures are a handful of lines, so a file rename needs a body of shared text to be seen as one.
        self.root, self.store_root, self.lang, self.filler = root, store_root, lang, filler
        self.decorated: frozenset[str] = frozenset()  # qualified names rendered with ``lang.decorator`` above them
        self.decorator_gap = False  # a blank line between the decorator and the definition
        root.mkdir(parents=True, exist_ok=True)
        self.repo = pygit2.init_repository(str(root), initial_head="main")
        self.repo.config["user.name"], self.repo.config["user.email"] = "Fixture Tester", "tester@example.invalid"
        _write_tree(root, lang, [(p, h, n, 0) for p, h, n, _k in base], filler=filler)
        self.repo.index.add_all()
        self.repo.index.write()
        signature = pygit2.Signature("Fixture Tester", "tester@example.invalid", 1700000000, 0)
        self.repo.create_commit("HEAD", signature, signature, "baseline", self.repo.index.write_tree(), [])
        self.store = ReviewStore(store_root)
        self.session = open_or_create_session(self.store, root, self._range())

    def _range(self) -> Any:
        # Staged mode is the one that similarity-matches a new file against a deleted one: libgit2 reports every new
        # file of a tree-to-working-tree diff as untracked, and never as a rename.
        return resolve_rev_range(self.root, staged=bool(self.filler))

    def advance(self, model: list[Sym], *, keep: frozenset[str] = frozenset()) -> Any:
        _write_tree(
            self.root, self.lang, model, keep=keep, filler=self.filler, decorated=self.decorated, gap=self.decorator_gap
        )
        if self.filler:
            # What ``git mv`` leaves behind: the new path is in the index.
            index = self.repo.index
            index.add_all()
            for entry in list(index):
                if not (self.root / entry.path).exists():
                    index.remove(entry.path)
            index.write()
        return refresh(
            self.store,
            self.session,
            self.root,
            store_root=self.store_root,
            rng=self._range(),
            limit=500,
        )

    def symbols(self, revision_id: str) -> dict[tuple[str, str], ReviewUnit]:
        return {(u.path, u.symbol): u for u in self.store.list_units(revision_id) if u.kind == "symbol"}


def _hosted(lang: Lang, seed: int) -> bool:
    return lang.hosted == "always" or (lang.hosted == "sometimes" and seed % 2 == 1)


_FORCED = (
    "move",
    "dest_mark",
    "file_rename",
    "class_move",
    "decorator_lost",
    "decorator_kept",
    "decorator_multi_lost",  # the same two, with the attribute over several lines (Rust, PHP, C#)
    "decorator_multi_kept",
)


def _run_program(
    tmp: Path, lang: Lang, index: int, counts: Counter[str], seen_renames: list[tuple[tuple[str, str], ...]]
) -> None:
    seed = index * 7919 + sum(map(ord, lang.name))
    rng = random.Random(seed)
    hosted = _hosted(lang, index)
    forced = _FORCED[index] if index < len(_FORCED) else ""
    if forced == "class_move" and not hosted:
        forced = "move"
    if forced.startswith("decorator"):
        if not (lang.multiline if "multi" in forced else lang.decorator):
            forced = "move"  # nothing to decorate with in this language
        else:
            hosted = lang.hosted != "never"  # a TypeScript decorator is only valid on a method
            if "multi" in forced:
                lang = replace(lang, decorator=lang.multiline)
    hops = (1, 3, 3, 1, 1, 1, 1, 1)[index] if index < len(_FORCED) else (1 if index % 2 == 0 else 3)
    fresh = iter(range(100, 10**6))
    model1 = _initial(rng, lang, hosted)
    if forced:
        paths = _paths(lang)
        model1 = [
            (paths[0], "HostA" if hosted else "", "fn_a", 1),
            (paths[0], "HostA" if hosted else "", "fn_b", 2),
            (paths[1], "HostB" if hosted else "", "fn_c", 3),
            (paths[2], "HostA" if hosted else "", "fn_d", 4),
        ]
    repo = _Repo(
        tmp / f"{lang.name}-{index}",
        tmp / f"store-{lang.name}-{index}",
        lang,
        model1,
        filler=24 if forced == "file_rename" else 0,
    )
    if forced.startswith("decorator"):
        repo.decorated = frozenset({_qname(model1[0])})
        repo.decorator_gap = forced.endswith("lost")  # the loss must be seen through a blank line
    first = repo.advance(model1)
    states: dict[tuple[str, str], str] = {}
    attested: dict[str, set[str]] = {}  # unit key -> fingerprints a reviewer judged there
    directly: dict[str, str] = {}  # unit key -> state a reviewer set by hand (never a move's to overwrite)
    marked: list[tuple[Sym, str]] = []
    units1 = repo.store.list_units(first.revision.id)
    for unit in units1:
        state = "reviewed" if forced.startswith("decorator") else rng.choices(_STATES, _STATE_WEIGHTS)[0]
        mark_unit(repo.store, repo.session, first.revision, unit, state=state)  # type: ignore[arg-type]
        attested.setdefault(unit.unit_key, set()).add(unit.content_fingerprint)
        states[(unit.path, unit.symbol)] = state
    symbols1 = repo.symbols(first.revision.id)
    for sym in model1:
        unit = symbols1.get((sym[0], _qname(sym)))
        assert unit is not None, (lang.name, "the model symbol was not derived as a unit", sym, sorted(symbols1))
        marked.append((sym, states[(sym[0], _qname(sym))]))
    counts["programs"] += 1
    counts["revisions"] += 1

    model = model1
    for hop in range(1, hops + 1):
        previous_model = model
        renames: dict[str, str] = {}
        if forced == "move" and hop == 1:
            src = model[0]
            model = [(rng.choice(_paths(lang)[2:]), src[1], src[2], src[3]) if s == src else s for s in model]
        elif forced == "dest_mark":
            src = model[0]
            if hop == 1:
                model = [*model, (_paths(lang)[3], src[1], src[2], src[3])]  # a copy
            else:
                model = [s for s in model if s != src]  # the original goes: the move now looks unique
        elif forced.startswith("decorator"):
            src = model[0]  # leaves for the file that already holds ``fn_d``; the decorator stays or goes with it
            model = [(_paths(lang)[2], src[1], src[2], src[3]) if s == src else s for s in model]
            repo.decorated = repo.decorated if forced.endswith("kept") else frozenset()
        elif forced == "class_move" and hop == 1:
            src = model[0]
            model = [
                (src[0], "HostB" if src[1] == "HostA" else "HostA", src[2], src[3]) if s == src else s for s in model
            ]
        else:
            model = _step(rng, lang, hosted, model, fresh=fresh)
        if forced == "file_rename" and hop == 1:
            # The file keeps its content under a new path while one symbol leaves it in the same revision.
            old_path, new_path = _paths(lang)[0], f"src/renamed.{lang.ext}"
            renames[old_path] = new_path
            model = list(previous_model)
            leaving = model[1]
            model = [(_paths(lang)[2], s[1], s[2], s[3]) if s == leaving else s for s in model]
            model = [(new_path if s[0] == old_path else s[0], s[1], s[2], s[3]) for s in model]
        if forced == "class_move" and hop == 1:
            counts["class_moves"] += 1
        before_marks = {m.unit_key: m for m in repo.store.list_marks(repo.session.id)}
        seen_renames.clear()
        keep = _kept(lang, previous_model) - renames.keys()  # a renamed file is gone from its old path, not emptied
        result = repo.advance(model, keep=keep)
        counts["revisions"] += 1
        symbols = repo.symbols(result.revision.id)
        after_marks = {m.unit_key: m for m in repo.store.list_marks(repo.session.id)}
        current = {u.unit_key: u for u in repo.store.list_units(result.revision.id)}
        if renames:
            # The category only counts when git really reported the rename, and Review really reconciled it as one.
            assert any(pair in seen for seen in seen_renames for pair in renames.items()), (
                lang.name,
                "git did not report the file as renamed",
                renames,
                seen_renames,
            )
            counts["file_rename_with_move"] += 1
            # Both halves land: the symbol that stayed follows the file by alias, the one that left follows the move, and
            # each decision sits on exactly one key.
            for before_sym, where in ((previous_model[0], renames[previous_model[0][0]]), (leaving, _paths(lang)[2])):
                landed = symbols.get((where, _qname(before_sym)))
                assert landed is not None, (lang.name, "symbol missing after the rename", before_sym)
                carried = after_marks.get(landed.unit_key)
                before_state = states[(before_sym[0], _qname(before_sym))]
                # P11-6: the symbol that stayed sits right above the one that left, so git aligns the leaver's deleted
                # lines with the stayer's own changed lines and the stayer's fingerprint covers them. Those lines are
                # text its verdict never saw, so a ``reviewed`` stayer reopens; the leaver, which shows no deleted text
                # at its destination, follows the move unchanged.
                reopens = (
                    before_sym is not leaving
                    and before_state == "reviewed"
                    and owns_deleted_text(landed.content_fingerprint)
                )
                want = "changed_since_review" if reopens else before_state
                assert carried is not None and carried.state == want, (
                    lang.name,
                    "decision lost or changed across a file rename with a move",
                    before_sym,
                    carried,
                )
                if not reopens:
                    # A verdict that never attested content (``later``) keeps its stamp; it now differs from the
                    # stayer's, which covers the leaver's deleted lines, and the part reads as changed since.
                    if before_sym is leaving or not owns_deleted_text(landed.content_fingerprint):
                        assert carried.content_fingerprint == landed.content_fingerprint
                    counts["file_rename_decisions_followed"] += 1

        if forced == "dest_mark" and hop == 1:
            dest = symbols.get((_paths(lang)[3], _qname(model[-1])))
            assert dest is not None
            mark_unit(repo.store, repo.session, result.revision, dest, state="later")
            directly[dest.unit_key] = "later"
            attested.setdefault(dest.unit_key, set()).add(dest.content_fingerprint)
            counts["dest_marks"] += 1
            after_marks = {m.unit_key: m for m in repo.store.list_marks(repo.session.id)}

        # ---- fail-open invariants, whatever the program did -------------------------------------------------
        for key, mark in after_marks.items():
            unit = current.get(key)
            if mark.state == "reviewed":
                if unit is None or unit.content_fingerprint != mark.content_fingerprint:
                    counts["fail_open"] += 1  # reviewed on content it did not attest
                elif mark.content_fingerprint not in {
                    part for fps in attested.values() for fp in fps for part in (fp, bare_fingerprint(fp))
                }:
                    counts["fail_open"] += 1  # reviewed content nobody judged
                elif key not in before_marks and key not in directly:
                    copies = [
                        u
                        for u in current.values()
                        if u.kind == "symbol" and u.content_fingerprint == mark.content_fingerprint
                    ]
                    if len(copies) != 1:
                        counts["fail_open"] += 1  # a carried verdict on a copy of the attested content
        for key, state in directly.items():
            kept = after_marks.get(key)
            if kept is None or kept.state != state:
                counts[
                    "fail_open"
                ] += 1  # a move changed or dropped the reviewer's own decision, whatever it turned into
            else:
                counts["dest_marks_kept"] += 1

        # ---- the exact oracle, for a single hop without a file rename ---------------------------------------
        if forced.startswith("decorator"):
            landed = symbols.get((_paths(lang)[2], _qname(src)))
            assert landed is not None, (lang.name, "the moved symbol was not derived at its destination")
            carried_mark = after_marks.get(landed.unit_key)
            if forced.endswith("kept"):
                # The control: the same move with the decorator intact is carried, so the next case is not a blanket refusal.
                assert carried_mark is not None and carried_mark.state == "reviewed", (lang.name, forced, carried_mark)
            else:
                assert carried_mark is None, (
                    lang.name,
                    "a judgment followed a symbol that lost its decorator",
                    carried_mark,
                )
            counts[forced] += 1
        if hops == 1 and not renames and not forced == "dest_mark" and not forced.startswith("decorator"):
            gone = frozenset(
                p for p in {s[0] for s in previous_model} - {s[0] for s in model} - _kept(lang, previous_model)
            )
            expected, category = _expected(marked, model, gone)
            have = {
                (u.path, u.symbol): after_marks[u.unit_key].state
                for u in symbols.values()
                if u.unit_key in after_marks
                and after_marks[u.unit_key].content_fingerprint == u.content_fingerprint  # a valid mark only
                and u.symbol not in _HOSTS  # the host's own class unit and the tail member are not modelled
                and not u.symbol.endswith(_SENTINEL)
            }
            for pair in expected.keys() | have.keys():
                if have.get(pair) == expected.get(pair):
                    continue
                here = symbols.get(pair)
                if have.get(pair) is None and here is not None and owns_deleted_text(here.content_fingerprint):
                    # P11-6: the part shows base lines a neighbour's removal or move put in its block. Its fingerprint
                    # covers them, the old verdict never saw them, so it reopens (the model has no notion of that text).
                    counts["deleted_text_reopens"] += 1
                    continue
                counts["divergence"] += 1
                if have.get(pair) == "reviewed" or (pair in have and pair not in expected):
                    counts["fail_open"] += 1
                    WHY.append(
                        f"{lang.name}#{index} oracle {pair}: have={have.get(pair)} expected={expected.get(pair)} model_before={marked} after={model}"
                    )
                else:
                    counts["fail_safe"] += 1
                    WHY.append(
                        f"SAFE {lang.name}#{index} {pair}: have={have.get(pair)} expected={expected.get(pair)} model_before={marked} after={model}"
                    )
            for name, number in category.items():
                counts[name] += number
            counts["oracle_programs"] += 1
            # ``unknown`` fingerprints are never matched: flip each side of the program's own moves and run it again.
            for old_path, old_name in [(k, v) for k, v in _moved_keys(marked, model)]:
                for flip in ("source", "destination", "both"):
                    prev_units = list(repo.store.list_units(first.revision.id))
                    next_units = list(repo.store.list_units(result.revision.id))
                    prev_units = [
                        (
                            replace(u, fingerprint_method="unknown")
                            if flip != "destination" and (u.path, u.symbol) == (old_path, old_name[0])
                            else u
                        )
                        for u in prev_units
                    ]
                    next_units = [
                        (
                            replace(u, fingerprint_method="unknown")
                            if flip != "source" and (u.path, u.symbol) == old_name[1]
                            else u
                        )
                        for u in next_units
                    ]
                    outcome = reconcile(prev_units, next_units, list(before_marks.values()), body_digests=None)
                    destination = next((u for u in next_units if (u.path, u.symbol) == old_name[1]), None)
                    assert destination is not None
                    counts["unknown_cases"] += 1
                    if any(m.unit_key == destination.unit_key and m.state == "reviewed" for m in outcome.marks):
                        counts["fail_open"] += 1


def _moved_keys(marked: list[tuple[Sym, str]], following: list[Sym]) -> list[tuple[str, tuple[str, tuple[str, str]]]]:
    """``(old path, (old qualified name, (new path, new qualified name)))`` for each unambiguous move of the model."""

    after = {(s[0], _qname(s)) for s in following}
    prev = [s for s, state in marked if state == "reviewed"]
    out = []
    for src in prev:
        if (src[0], _qname(src)) in after:
            continue
        twins = [t for t in following if (_qname(t), t[3]) == (_qname(src), src[3]) and t[0] != src[0]]
        if len(twins) == 1 and (twins[0][0], _qname(twins[0])) not in {(s[0], _qname(s)) for s, _ in marked}:
            out.append((src[0], (_qname(src), (twins[0][0], _qname(twins[0])))))
    return out


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def test_every_language_move_program_is_fail_closed_through_the_real_pipeline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_renames: list[tuple[tuple[str, str], ...]] = []
    real_reconcile = local.reconcile

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen_renames.append(tuple(kwargs.get("renames") or ()))
        return real_reconcile(*args, **kwargs)

    monkeypatch.setattr(local, "reconcile", spy)
    totals: dict[str, Counter[str]] = {}
    for lang in LANGS:
        counts: Counter[str] = Counter()
        for index in range(PROGRAMS):
            _run_program(tmp_path, lang, index, counts, seen_renames)
        totals[lang.name] = counts
    with capsys.disabled():
        print("\nP11-3 per-language differential (programs / revisions / fail_open / divergence):")
        for name, counts in totals.items():
            print(f"  {name:11s} {dict(sorted(counts.items()))}")
        for line in WHY[:4]:
            print("  WHY", line)
    for name, counts in totals.items():
        assert counts["programs"] == PROGRAMS, name
        assert counts["fail_open"] == 0, (name, dict(counts), WHY[:3])
        assert counts["divergence"] == 0, (name, dict(counts))
        # Not vacuous: the moves, copies, renames and drift this language exercised actually happened.
        assert counts["moved_reviewed"] + counts["moved_other"] >= 1, (name, dict(counts))
        assert counts["unknown_cases"] >= 1, (name, dict(counts))
        assert counts["dest_marks_kept"] >= 1, (name, dict(counts))
        assert counts["file_rename_with_move"] >= 1, (name, dict(counts))
        assert counts["revisions"] >= 2 * PROGRAMS, (name, dict(counts))  # at least a base and a hop per program
    for lang in LANGS:
        if lang.decorator:
            assert totals[lang.name]["decorator_lost"] >= 1 and totals[lang.name]["decorator_kept"] >= 1, lang.name
        if lang.multiline:
            assert totals[lang.name]["decorator_multi_lost"] >= 1, lang.name
            assert totals[lang.name]["decorator_multi_kept"] >= 1, lang.name
        if _hosted(lang, 3):
            assert totals[lang.name]["class_moves"] >= 1, lang.name  # a same-path class-to-class move happened
    everything: Counter[str] = Counter()
    for counts in totals.values():
        everything.update(counts)
    for category in ("renamed_reviewed", "ambiguous_reviewed", "changed_reviewed", "gone_reviewed", "carried_reviewed"):
        assert everything[category] >= 1, (category, dict(everything))
