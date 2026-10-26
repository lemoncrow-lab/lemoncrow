"""The change diagram in the summary comment (G17): what the Architecture Delta says changed, drawn as Mermaid.

GitHub draws a ```mermaid block inline, so the diagram needs no image host and no model.

**Source of truth.** The Snapshot-exact Architecture Delta (:mod:`architecture_delta`), which compares the immutable
old and new sides of the pull request. This module reads only the relationships that delta marks ``added``,
``removed`` or ``changed`` with ``exactness: snapshot`` -- the same filter the delta's own narration uses -- and never
the current-index context or the "unknown" facts beside them. It adds no edge, calls no model, and puts no text a
model wrote into the block: every label is a path or a symbol name from the delta, escaped by :func:`_esc`.

**Which diagram.** Only a ``flowchart`` is drawn, because that is all the delta can support:

* ``erDiagram`` needs schema or persistence *entities* (tables, columns, models). The delta's nodes are files; when the
  Review Map's context is passed it flags a path under ``migrations/`` as a persistence boundary, but no table or
  field is ever named, so an ER diagram would be invented.
* ``classDiagram`` needs type or inheritance relationships. The delta's only producer emits ``references`` between
  files (the Review Map's vocabulary adds ``calls``, ``imports`` and ``impacts``); none says "extends" or "implements".
* ``sequenceDiagram`` needs call *order*. A deterministic comparison of two source graphs has none -- an edge says
  "this file uses that file", not "then". It is never drawn.

If the delta ever carries entities or inheritance edges, the place to teach this module is :func:`render_flowchart`'s
caller (:func:`draw`); nothing here guesses ahead of the data.

**Encoding** (readable on GitHub's light and dark themes, because every node carries its own fill and text colour):
added is solid green with a ``+``, removed is dashed red with a minus sign, changed is bold amber, and a file that is only
the other end of a changed relationship is grey.

**Limits.** No diagram unless a relationship was added or removed (a change in how many times a file refers to
another is not worth a picture alone). At most :data:`MAX_NODES` nodes and :data:`MAX_EDGES` edges: past that, files
are grouped by directory (as deep as keeps the count down), the strongest relationships are kept, and the block says
how many more are not shown.

**Cost.** Building the delta parses every source file of both sides of the pull request with tree-sitter (about 10 to
25 ms a file), so :func:`delta_for_capture` first skips pull requests that change no code, and repositories with more
than :data:`DEFAULT_MAX_SOURCE_FILES` source files (``LEMONCROW_REVIEW_DIAGRAM_MAX_FILES`` overrides it). A re-run on
an unchanged head reuses the drawn block from the comment instead of recomputing it (:func:`resolve`).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from lemoncrow.pro.capabilities.review.pr_bot.overview import trivial
from lemoncrow.pro.capabilities.review.pr_bot.render import fence_for, plural
from lemoncrow.pro.capabilities.review.pr_bot.state import DIAGRAM_STATE

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.pr_bot.engine import Captured
    from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState

#: The Action input ``diagram`` and the CLI's ``--diagram``: ``auto`` draws it when it is worth drawing; ``off`` never.
DIAGRAM_MODES: Final[tuple[str, ...]] = ("auto", "off")
#: Bumped when the drawing changes, so a comment drawn by an older version is drawn again, not reused.
DIAGRAM_VERSION: Final[int] = 2
#: The ``### Diagram`` section of the summary, open by default: a heading, the Mermaid block and a caption. A hidden
#: marker ends it, so :func:`reuse` can find the whole section in a comment it wrote.
SECTION_OPEN: Final[str] = "### Diagram"
SECTION_CLOSE: Final[str] = "<!-- /lemoncrow:diagram -->"
#: First line of every Mermaid block this bot writes: the neutral theme reads on GitHub's light and dark pages.
THEME_INIT: Final[str] = "%%{init: {'theme': 'neutral'}}%%"
#: The same for a sequence diagram. Its message text and arrows are drawn straight on the page, and the neutral theme
#: makes them near-black, which cannot be read on GitHub's dark page. A mid grey (about 4.5:1 on white, 4:1 on the dark
#: page) reads on both; a flowchart keeps its text inside boxes and needs nothing.
SEQUENCE_INIT: Final[str] = (
    "%%{init: {'theme': 'neutral', 'themeVariables': "
    "{'signalColor': '#6e7781', 'signalTextColor': '#6e7781', 'actorLineColor': '#6e7781'}}}%%"
)
#: The only diagram type drawn; see the module docstring for why not an ER, class or sequence diagram.
KIND: Final[str] = "flowchart"

#: The most nodes and edges one diagram draws. Past them, files are grouped by directory and the rest is counted.
MAX_NODES: Final[int] = 25
MAX_EDGES: Final[int] = 40
#: Relationships that must be added or removed before a diagram is worth drawing.
MIN_STRUCTURAL: Final[int] = 1
#: Symbol names on one edge, and the characters kept of a label.
MAX_SYMBOLS: Final[int] = 2
MAX_SYMBOL_CHARS: Final[int] = 24
MAX_PATH_CHARS: Final[int] = 44
#: Source files a repository may have before the delta is not built (it parses both sides in full).
DEFAULT_MAX_SOURCE_FILES: Final[int] = 2_000
MAX_FILES_ENV: Final[str] = "LEMONCROW_REVIEW_DIAGRAM_MAX_FILES"

CHANGED: Final[tuple[str, ...]] = ("added", "removed", "changed")
#: Languages the repository map reads no references from: a pull request that changes only these draws nothing.
_NOT_CODE: Final[frozenset[str]] = frozenset({"json", "toml", "yaml", "html", "css", "markdown"})

#: Characters that go into a Mermaid label as they are. Everything else becomes an entity (``#60;``), so a name can
#: neither close the label, start a tag or a comment, mention anyone, nor open a Markdown fence.
_SAFE: Final[frozenset[str]] = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _./-+")

# Fill and text colours are fixed together on every node, so a node reads the same on GitHub's light and dark pages;
# the stroke is a mid-tone that holds against both. Edges are styled by `linkStyle` with the same hues.
_CLASS_DEFS: Final[Mapping[str, str]] = {
    "added": "classDef added fill:#dafbe1,stroke:#2da44e,stroke-width:2px,color:#1f2328",
    "removed": "classDef removed fill:#ffebe9,stroke:#e5534b,stroke-width:2px,stroke-dasharray:6 4,color:#1f2328",
    "changed": "classDef changed fill:#fff8c5,stroke:#bf8700,stroke-width:4px,color:#1f2328",
    "context": "classDef context fill:#eaeef2,stroke:#8c959f,stroke-width:1px,color:#1f2328",
}
_LINK_STYLES: Final[Mapping[str, str]] = {
    "added": "stroke:#2da44e,stroke-width:2px",
    "removed": "stroke:#e5534b,stroke-width:2px,stroke-dasharray:6 4",
    "changed": "stroke:#bf8700,stroke-width:4px",
}
#: solid for added, dotted for removed, thick for changed.
_ARROWS: Final[Mapping[str, str]] = {"added": "-->", "removed": "-.->", "changed": "==>"}
#: U+2212 MINUS SIGN, not a hyphen: it must not read as part of a name.
MINUS: Final[str] = "\N{MINUS SIGN}"
#: A sign goes straight against the name, with no space: Mermaid 11 reads a label that starts with ``+`` and a space as a
#: Markdown list and draws "Unsupported markdown: list" in its place.
_SIGNS: Final[Mapping[str, str]] = {"added": "+", "removed": MINUS}
_PRIORITY: Final[Mapping[str, int]] = {"added": 0, "removed": 1, "changed": 2}

LEGEND: Final[str] = (
    f"<sub>**+** added (solid) \N{MIDDLE DOT} **{MINUS}** removed (dashed) \N{MIDDLE DOT} bold changed \N{MIDDLE DOT} "
    "grey unchanged \N{MIDDLE DOT} "
    "an arrow points from the file that refers to another to the file it refers to</sub>"
)


def none_state() -> str:
    """What the hidden state records when the delta was read and nothing was worth drawing."""

    return f"{DIAGRAM_VERSION}:none"


# --------------------------------------------------------------------------- #
# reading the delta
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Relationship:
    """One relationship the delta says changed between the base and the head."""

    source: str
    target: str
    status: str  # added | removed | changed
    kind: str
    before: int
    after: int
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Facts:
    """What the delta says changed: its relationships, and how each file at their ends changed."""

    relationships: tuple[Relationship, ...]
    file_status: Mapping[str, str]  # path -> added | removed | changed | context
    file_dir: Mapping[str, str]  # path -> its directory ("" for the root)


def _rows(value: object) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def facts_from(delta: Mapping[str, Any]) -> Facts:
    """The exact, changed relationships of *delta* and the status of the files they touch; nothing else.

    A delta that could not compare the two sides (``unavailable``), a relationship that is only current-index
    context and one that is a Snapshot fact with no base to compare against all read as nothing changed.
    """

    graph = delta.get("graph")
    if delta.get("status") == "unavailable" or not isinstance(graph, Mapping):
        return Facts((), {}, {})
    found: list[Relationship] = []
    for edge in _rows(graph.get("edges")):
        status = str(edge.get("delta_status") or "")
        source, target = str(edge.get("source_path") or ""), str(edge.get("target_path") or "")
        if str(edge.get("exactness") or "") != "snapshot" or status not in CHANGED or not source or not target:
            continue
        named = [str(item.get("name") or "") for item in _rows(edge.get("symbols"))]
        found.append(
            Relationship(
                source,
                target,
                status,
                str(edge.get("kind") or "references"),
                _count(edge.get("base_weight")),
                _count(edge.get("snapshot_weight")),
                tuple(name for name in named if name),
            )
        )
    found.sort(key=lambda row: (_PRIORITY[row.status], row.source, row.target, row.kind))
    status_of: dict[str, str] = {}
    dir_of: dict[str, str] = {}
    for node in _rows(graph.get("nodes")):
        path = str(node.get("path") or "")
        if not path or str(node.get("exactness") or "") != "snapshot":
            continue
        change = str(node.get("delta_status") or "")
        status_of[path] = change if change in CHANGED else "context"
        dir_of[path] = str(node.get("component") if node.get("component") is not None else _dir(path))
    return Facts(tuple(found), status_of, dir_of)


def _dir(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "" if parent == "." else parent


# --------------------------------------------------------------------------- #
# escaping
# --------------------------------------------------------------------------- #


#: A label that starts like a Markdown list item: ``-`` or ``+`` or ``1.`` and then a space.
_LIST_START: Final[re.Pattern[str]] = re.compile(r"(?:[-+]|\d+[.)])(?=\s)")


def _esc(text: str) -> str:
    """*text* safe inside a quoted Mermaid label.

    Anything outside :data:`_SAFE` -- a quote, bracket, angle bracket, pipe, backtick, ``@``, ``#``, ``;``, ``%`` --
    becomes a numeric entity, which Mermaid draws as the character. A control or format character (a newline, a
    right-to-left override) becomes ``?``. So a name cannot break the label, forge a comment or an HTML tag, ping a
    person or end the code fence.

    Mermaid 11 also runs a label through Markdown, so two more things are written as entities, which it does not read as
    Markdown: an underscore at the edge of a word (``__init__.py`` is otherwise drawn as a bold "init.py", a different
    file) and the character that makes a label a list item (it is otherwise drawn as "Unsupported markdown: list").
    """

    out: list[str] = []
    last = len(text) - 1
    for index, char in enumerate(text):
        if char == "_":
            inside = 0 < index < last and text[index - 1].isalnum() and text[index + 1].isalnum()
            out.append(char if inside else "#95;")
        elif char in _SAFE or (ord(char) > 127 and char.isalnum()):
            out.append(char)
        elif unicodedata.category(char)[0] == "C":
            out.append("?")
        else:
            out.append(f"#{ord(char)};")
    if out and _LIST_START.match(text):
        out[0] = f"#{ord(text[0])};"
    return "".join(out)


def _label(text: str, limit: int, *, tail: bool = False) -> str:
    """*text* cut to *limit* characters, then escaped (so the cut never splits an entity); a path keeps its tail."""

    if len(text) <= limit:
        return _esc(text)
    return "\u2026" + _esc(text[-(limit - 1) :]) if tail else _esc(text[: limit - 1]) + "\u2026"


# --------------------------------------------------------------------------- #
# laying out
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Node:
    key: str
    status: str
    group: str  # the directory it is drawn inside, "" for none
    title: str  # escaped


@dataclass(frozen=True, slots=True)
class _Link:
    source: str
    target: str
    status: str
    label: str  # escaped
    weight: int


@dataclass(frozen=True, slots=True)
class Layout:
    nodes: tuple[_Node, ...]
    links: tuple[_Link, ...]
    hidden: int
    grouped: bool


def _file_status(facts: Facts, path: str) -> str:
    return facts.file_status.get(path, "context")


def _edge_label(row: Relationship) -> str:
    if row.status == "changed":
        return f"refs {row.before} → {row.after}"
    symbols = ", ".join(_label(name, MAX_SYMBOL_CHARS) for name in row.symbols[:MAX_SYMBOLS])
    return f"{_SIGNS[row.status]}{symbols or _esc(row.kind)}"


def _file_layout(rows: Sequence[Relationship], facts: Facts) -> Layout:
    shown = list(rows[:MAX_EDGES])
    paths = sorted({path for row in shown for path in (row.source, row.target)})
    directories = {facts.file_dir.get(path, _dir(path)) for path in paths}
    drawn_in_groups = len(directories) > 1
    nodes = tuple(
        _Node(
            path,
            _file_status(facts, path),
            facts.file_dir.get(path, _dir(path)) if drawn_in_groups else "",
            _label(PurePosixPath(path).name if drawn_in_groups else path, MAX_PATH_CHARS, tail=True),
        )
        for path in paths
    )
    links = tuple(_Link(row.source, row.target, row.status, _edge_label(row), 1) for row in shown)
    return Layout(nodes, links, len(rows) - len(shown), False)


def _group_name(directory: str, depth: int) -> str:
    parts = [part for part in directory.split("/") if part][:depth]
    return "/".join(parts) or "(root)"


def _group_status(statuses: Sequence[str]) -> str:
    if all(status == "added" for status in statuses):
        return "added"
    if all(status == "removed" for status in statuses):
        return "removed"
    return "changed" if any(status != "context" for status in statuses) else "context"


def _grouped_layout(rows: Sequence[Relationship], facts: Facts, paths: Sequence[str]) -> Layout | None:
    """Files grouped by directory, as deep as keeps the nodes within the cap; ``None`` when no grouping helps.

    Grouping helps when it leaves two or more nodes with a relationship between two of them: a pull request that
    changes thirty files of one directory is not made clearer by one node.
    """

    dirs = {path: facts.file_dir.get(path, _dir(path)) for path in paths}
    deepest = max((len([part for part in directory.split("/") if part]) for directory in dirs.values()), default=0)
    for depth in range(deepest, 0, -1):
        group = {path: _group_name(directory, depth) for path, directory in dirs.items()}
        names = set(group.values())
        across = [row for row in rows if group[row.source] != group[row.target]]
        if not 2 <= len(names) <= MAX_NODES or not across:
            continue
        pairs: dict[tuple[str, str], Counter[str]] = {}
        for row in across:
            pairs.setdefault((group[row.source], group[row.target]), Counter())[row.status] += 1
        ranked = sorted(
            pairs.items(),
            key=lambda item: (min(_PRIORITY[s] for s in item[1]), -sum(item[1].values()), item[0]),
        )[:MAX_EDGES]
        links: list[_Link] = []
        for (source, target), counts in ranked:
            status = next(iter(counts)) if len(counts) == 1 else "changed"
            said = ", ".join(f"{counts[s]} {s}" for s in CHANGED if counts[s])
            links.append(_Link(source, target, status, _esc(said), sum(counts.values())))
        used = {name for link in links for name in (link.source, link.target)}
        members: dict[str, list[str]] = {name: [] for name in used}
        for path, name in group.items():
            if name in used:
                members[name].append(path)
        nodes = tuple(
            _Node(
                name,
                _group_status([_file_status(facts, path) for path in files]),
                "",
                f"{_label(name, MAX_PATH_CHARS, tail=True)} ({plural(len(files), 'file')})",
            )
            for name, files in sorted(members.items())
        )
        return Layout(nodes, tuple(links), len(rows) - sum(link.weight for link in links), True)
    return None


def _busiest_layout(rows: Sequence[Relationship], facts: Facts) -> Layout:
    """Relationships taken strongest first (added, then removed, then changed; hubs before leaves) while they fit.

    A relationship is taken when its two files fit under :data:`MAX_NODES` (a file already taken costs nothing), so
    the diagram is never empty and never over the cap, and the relationships left out are counted.
    """

    degree: Counter[str] = Counter()
    for row in rows:
        degree[row.source] += 1
        degree[row.target] += 1
    ranked = sorted(
        rows,
        key=lambda row: (_PRIORITY[row.status], -(degree[row.source] + degree[row.target]), row.source, row.target),
    )
    taken: list[Relationship] = []
    files: set[str] = set()
    for row in ranked:
        if len(taken) < MAX_EDGES and len(files | {row.source, row.target}) <= MAX_NODES:
            taken.append(row)
            files |= {row.source, row.target}
    layout = _file_layout(
        sorted(taken, key=lambda row: (_PRIORITY[row.status], row.source, row.target, row.kind)), facts
    )
    return Layout(layout.nodes, layout.links, len(rows) - len(layout.links), False)


def layout_for(facts: Facts) -> Layout:
    rows = facts.relationships
    paths = sorted({path for row in rows for path in (row.source, row.target)})
    if len(paths) <= MAX_NODES:
        return _file_layout(rows, facts)
    return _grouped_layout(rows, facts, paths) or _busiest_layout(rows, facts)


# --------------------------------------------------------------------------- #
# writing Mermaid
# --------------------------------------------------------------------------- #


def render_flowchart(layout: Layout) -> str:
    """The Mermaid ``flowchart`` for *layout*: class styles, nodes (inside a subgraph per directory), edges, link styles.

    Node and subgraph ids are ``n1``, ``g1`` ...: counters, never a name from the pull request.
    """

    ids = {node.key: f"n{index}" for index, node in enumerate(layout.nodes, 1)}
    lines = [THEME_INIT, "flowchart LR"]
    lines += [f"    {_CLASS_DEFS[name]}" for name in _CLASS_DEFS if any(node.status == name for node in layout.nodes)]
    subgraphs = sorted({node.group for node in layout.nodes if node.group})
    boxes: dict[str, str] = {}
    for index, directory in enumerate(subgraphs, 1):
        box = f"g{index}"
        boxes[directory] = box
        lines.append(f'    subgraph {box}["{_label(directory, MAX_PATH_CHARS, tail=True)}"]')
        for node in layout.nodes:
            if node.group == directory:
                lines.append(f"        {_node(ids[node.key], node)}")
        lines.append("    end")
    lines += [f"    {_node(ids[node.key], node)}" for node in layout.nodes if not node.group]
    lines += [f"    style {box} fill:transparent,stroke:#8c959f" for box in boxes.values()]
    by_status: dict[str, list[int]] = {}
    for index, link in enumerate(layout.links):
        lines.append(f'    {ids[link.source]} {_ARROWS[link.status]}|"{link.label}"| {ids[link.target]}')
        by_status.setdefault(link.status, []).append(index)
    for status in CHANGED:
        if status in by_status:
            lines.append(f"    linkStyle {','.join(map(str, by_status[status]))} {_LINK_STYLES[status]}")
    return "\n".join(lines)


def _node(identifier: str, node: _Node) -> str:
    sign = _SIGNS.get(node.status)
    title = f"{sign}{node.title}" if sign else node.title
    return f'{identifier}["{title}"]:::{node.status}'


# --------------------------------------------------------------------------- #
# the section
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Diagram:
    """The outcome for one run: the block to put in the comment (``""`` for none) and what the state records."""

    section: str = ""
    state: str = ""
    note: str = ""
    kind: str = ""
    hidden: int = 0
    reused: bool = False


def digest_of(section: str) -> str:
    return f"{DIAGRAM_VERSION}:{hashlib.sha256(section.encode()).hexdigest()[:16]}"


def draw(delta: Mapping[str, Any]) -> Diagram | None:
    """The diagram section for *delta*, or ``None`` when the pull request is trivial: no relationship added or removed."""

    facts = facts_from(delta)
    if sum(row.status != "changed" for row in facts.relationships) < MIN_STRUCTURAL:
        return None
    layout = layout_for(facts)
    block = render_flowchart(layout)
    fence = fence_for(block)
    counts = Counter(row.status for row in facts.relationships)
    touched = len({path for row in facts.relationships for path in (row.source, row.target)})
    said = ", ".join(f"{counts[status]} {status}" for status in CHANGED if counts[status])
    notes: list[str] = []
    if layout.hidden:
        notes.append(f"{plural(layout.hidden, 'more relationship')} not shown")
    if layout.grouped:
        notes.append("files are grouped by directory")
    text = "; ".join(notes)
    footnote = f"<sub>{text[:1].upper()}{text[1:]}.</sub>" if notes else ""
    section = "\n".join(
        [
            SECTION_OPEN,
            "",
            f"Relationships between {plural(touched, 'file')}: {said}. From the Snapshot's architecture delta, "
            "which compares the base and the head; nothing here is inferred.",
            "",
            f"{fence}mermaid",
            block,
            fence,
            "",
            LEGEND,
            *(["", footnote] if footnote else []),
            "",
            SECTION_CLOSE,
        ]
    )
    return Diagram(section=section, state=digest_of(section), kind=KIND, hidden=layout.hidden)


def reuse(body: str, state: str) -> str:
    """The drawn section of the comment *body*, when it is the one *state* recorded; else ``""``.

    The section holds no nested block, and a model's text cannot forge its opening tag (``render.clean`` breaks every
    ``<details``), so the first opening tag followed by the first closing one is the whole section. The digest check
    is what makes it safe to reuse: a comment edited by hand no longer matches.
    """

    if not DIAGRAM_STATE.match(state) or not state.startswith(f"{DIAGRAM_VERSION}:") or state.endswith(":none"):
        return ""
    start = body.find(SECTION_OPEN)
    end = body.find(SECTION_CLOSE, start) if start >= 0 else -1
    if end < 0:
        return ""
    section = body[start : end + len(SECTION_CLOSE)]
    return section if digest_of(section) == state else ""


def resolve(
    mode: str,
    *,
    previous: ReviewState | None,
    existing_body: str,
    head_sha: str,
    merge_base_sha: str,
    compute: Callable[[], tuple[Mapping[str, Any] | None, str]],
) -> Diagram:
    """The diagram for this run.

    ``off`` draws nothing and computes nothing. On the head and merge base the last run drew, the delta cannot have
    changed (it compares two immutable trees), so the block it recorded is reused when the comment still holds it, and
    "nothing worth drawing" is reused too. Otherwise *compute* returns ``(delta, "")`` or ``(None, why)``; a ``why``
    becomes a run note and is not recorded, so the next run tries again.
    """

    if mode == "off":
        return Diagram()
    if (
        previous is not None
        and previous.head_sha == head_sha
        and previous.merge_base_sha == merge_base_sha
        and previous.diagram.startswith(f"{DIAGRAM_VERSION}:")
    ):
        if previous.diagram == none_state():
            return Diagram(state=previous.diagram, reused=True)
        kept = reuse(existing_body, previous.diagram)
        if kept:
            return Diagram(section=kept, state=previous.diagram, kind=KIND, reused=True)
    delta, why = compute()
    if delta is None:
        # An empty reason is "nothing to draw by construction" (the pull request changes no code): deterministic, so recorded.
        return Diagram(note=why) if why else Diagram(state=none_state())
    drawn = draw(delta)
    if drawn is None:
        status = delta.get("status")
        if status == "unavailable":
            return Diagram(
                note=f"change diagram skipped: {str(delta.get('reason') or 'the comparison is unavailable')[:160]}"
            )
        return Diagram(state=none_state())
    return drawn


# --------------------------------------------------------------------------- #
# computing the delta in the headless path
# --------------------------------------------------------------------------- #


def max_source_files(environ: Mapping[str, str] | None = None) -> int:
    raw = (os.environ if environ is None else environ).get(MAX_FILES_ENV, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_SOURCE_FILES


def delta_for_capture(captured: Captured, *, limit: int | None = None) -> tuple[Mapping[str, Any] | None, str]:
    """``(delta, "")`` for the captured pull request, or ``(None, why)`` when it is not worth computing or too costly.

    This is the Review Map's own function (:func:`architecture_delta.build_architecture_delta_from_revision`) on the
    Snapshot already captured for the review, with no current-index context to pass: the headless path has none, and
    the delta never promotes it into a claim anyway. It is skipped when the pull request changes no code (the
    repository map reads no references from data or prose) and when the head tree has more than *limit* source files.
    """

    try:
        from lemoncrow.infra.tree_sitter.tags import detect_language
        from lemoncrow.pro.capabilities.repo_map.graph import should_skip_relative_path
        from lemoncrow.pro.capabilities.review.architecture_delta import build_architecture_delta_from_revision
        from lemoncrow.pro.capabilities.review.sources.local import read_packet_json
    except ModuleNotFoundError:
        return None, "change diagram skipped: this build does not include the repository map"

    packet = read_packet_json(captured.store, captured.revision) or {}
    changed = [str(row.get("path") or "") for row in _rows(packet.get("files"))]
    if not any(_is_code(path, detect_language) for path in changed) or trivial(packet):
        # No code, or a change too small to draw (the same cut as the overview's): deterministic, so recorded.
        return None, ""
    allowed = max_source_files() if limit is None else limit
    size = _source_files(captured, detect_language, should_skip_relative_path)
    if size is not None and size > allowed:
        return None, (
            f"change diagram skipped: the repository has {size} source files, more than the {allowed} a diagram is "
            "drawn for (the GitHub Action guide, under Change diagram, says how to change that limit)"
        )
    delta = build_architecture_delta_from_revision(
        store=captured.store,
        repo_root=captured.repo_root,
        revision=captured.revision,
        packet=packet,
        context_graph={},
        context={},
    )
    return delta, ""


def _is_code(path: str, detect: Callable[[Any], str | None]) -> bool:
    if not path:
        return False
    language = detect(PurePosixPath(path))
    return language is not None and language not in _NOT_CODE


def _source_files(
    captured: Captured, detect: Callable[[Any], str | None], skipped: Callable[[str], bool]
) -> int | None:
    """Source files in the head tree, from git alone (no checkout is read); ``None`` when git cannot say."""

    try:
        done = subprocess.run(
            ["git", "-C", str(captured.repo_root), "ls-tree", "-r", "-z", "--name-only", captured.revision.head_sha],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    names = (raw.decode("utf-8", errors="replace") for raw in done.stdout.split(b"\x00") if raw)
    return sum(1 for name in names if not skipped(name) and _is_code(name, detect))


__all__ = [
    "DEFAULT_MAX_SOURCE_FILES",
    "DIAGRAM_MODES",
    "DIAGRAM_VERSION",
    "Diagram",
    "Facts",
    "Layout",
    "Relationship",
    "delta_for_capture",
    "digest_of",
    "draw",
    "facts_from",
    "none_state",
    "render_flowchart",
    "resolve",
    "reuse",
]
