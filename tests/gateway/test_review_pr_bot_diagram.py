"""G17: the ### Diagram in the pull request summary, drawn from the Snapshot-exact Architecture Delta.

The pure parts build a real delta with ``build_architecture_delta`` (the function the Review Map uses) from two small
source graphs; the end-to-end parts run the whole bot on a real git origin, so the delta is the one the Action
computes. The Mermaid is checked by ``block_of`` below, which parses every line the module may emit: no Mermaid
parser ships in this repository's dependencies, so the grammar is asserted here instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.architecture_delta import build_architecture_delta
from lemoncrow.pro.capabilities.review.pr_bot import diagram as diagram_module
from lemoncrow.pro.capabilities.review.pr_bot.diagram import (
    DEFAULT_MAX_SOURCE_FILES,
    MAX_EDGES,
    MAX_NODES,
    draw,
    none_state,
    resolve,
    reuse,
)
from lemoncrow.pro.capabilities.review.pr_bot.render import (
    MAX_COMMENT,
    Receipt,
    RunFacts,
    SummaryView,
    _visible,
    render_summary,
)
from lemoncrow.pro.capabilities.review.pr_bot.runner import PrReviewError
from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState, Usage, dump_state, parse_state
from tests.gateway.pr_bot_support import BASE, FIRST_PUSH, build_world

HEAD = "a" * 40
MERGE_BASE = "b" * 40
MINUS = diagram_module.MINUS
ARROW = "\N{RIGHTWARDS ARROW}"

# --------------------------------------------------------------------------- #
# a Mermaid grammar check, and a real delta to draw
# --------------------------------------------------------------------------- #

_NODE = re.compile(r'^(n\d+)\["([^"]*)"\]:::(added|removed|changed|context)$')
_EDGE = re.compile(r'^(n\d+) (-->|-\.->|==>)\|"([^"]*)"\| (n\d+)$')
_BOX = re.compile(r'^subgraph (g\d+)\["([^"]*)"\]$')
_CLASSDEF = re.compile(
    r"^classDef (added|removed|changed|context) fill:#[0-9a-f]{6},stroke:#[0-9a-f]{6},[a-z0-9:,# -]+$"
)
_STYLE = re.compile(r"^style (g\d+) fill:transparent,stroke:#[0-9a-f]{6}$")
_LINK_STYLE = re.compile(
    r"^linkStyle (\d+(?:,\d+)*) stroke:#[0-9a-f]{6},stroke-width:\dpx(?:,stroke-dasharray:\d+ \d+)?$"
)
_ENTITY = re.compile(r"#(\d+);")
_ARROW_STATUS = {"-->": "added", "-.->": "removed", "==>": "changed"}


@dataclass
class Drawn:
    """A parsed ``flowchart``: nodes by id as ``(label, class)``, edges as ``(source, arrow, label, target)``."""

    text: str
    nodes: dict[str, tuple[str, str]] = field(default_factory=dict)
    edges: list[tuple[str, str, str, str]] = field(default_factory=list)
    boxes: dict[str, str] = field(default_factory=dict)
    classes: set[str] = field(default_factory=set)

    def label(self, wanted: str) -> str:
        """The id of the node drawn for the file *wanted*: by its path, or by its name when it sits in a box."""

        names = {wanted, wanted.rsplit("/", 1)[-1]}
        (found,) = [key for key, (label, _) in self.nodes.items() if label.lstrip(f"+{MINUS} ").strip() in names]
        return found

    def edge(self, source: str, target: str) -> tuple[str, str, str, str]:
        (found,) = [row for row in self.edges if row[0] == self.label(source) and row[3] == self.label(target)]
        return found


def block_of(section: str) -> Drawn:
    """The Mermaid inside *section*, checked line by line against everything the drawing may emit."""

    (text,) = re.findall(r"```mermaid\n(.*?)\n```", section, re.DOTALL)
    lines = text.split("\n")
    assert lines[0] == "%%{init: {'theme': 'neutral'}}%%", lines[0]
    assert lines[1] == "flowchart LR", lines[1]
    drawn = Drawn(text)
    styles: list[str] = []
    ends = 0
    for raw in lines[2:]:
        line = raw.strip()
        if match := _NODE.match(line):
            assert match[1] not in drawn.nodes, f"{match[1]} defined twice"
            drawn.nodes[match[1]] = (match[2], match[3])
        elif match := _EDGE.match(line):
            drawn.edges.append((match[1], match[2], match[3], match[4]))
        elif match := _BOX.match(line):
            drawn.boxes[match[1]] = match[2]
        elif match := _CLASSDEF.match(line):
            drawn.classes.add(match[1])
        elif line == "end":
            ends += 1
        elif _STYLE.match(line):
            assert line.split()[1] in drawn.boxes
        elif _LINK_STYLE.match(line):
            styles.append(line)
        else:
            raise AssertionError(f"not a line the diagram may emit: {line!r}")
    assert ends == len(drawn.boxes), "every subgraph is closed"
    assert {cls for _, cls in drawn.nodes.values()} <= drawn.classes, "a class is used without being defined"
    for source, _, _, target in drawn.edges:
        assert source in drawn.nodes and target in drawn.nodes, "an edge joins two drawn nodes"
    # Every edge is styled exactly once, by an index that exists.
    styled = [int(index) for line in styles for index in line.split()[1].split(",")]
    assert sorted(styled) == list(range(len(drawn.edges)))
    for label in [
        *(label for label, _ in drawn.nodes.values()),
        *(row[2] for row in drawn.edges),
        *drawn.boxes.values(),
    ]:
        # Labels hold no raw quote, bracket, angle bracket, pipe, backtick, @, backslash or semicolon: only entities.
        assert not re.search(r"[\"\[\]<>|`@\\;%{}]", _ENTITY.sub("", label)), label
        assert all(1 <= int(code) <= 0x10FFFF for code in _ENTITY.findall(label))
    # The arrow decides how an edge is drawn: solid added, dotted removed, thick changed.
    for _, arrow, _, _ in drawn.edges:
        assert arrow in _ARROW_STATUS
    return drawn


def _edge(source: str, target: str, *, weight: int = 1, symbol: str = "") -> dict[str, Any]:
    name = symbol or Path(target).stem
    return {
        "source": source,
        "target": target,
        "kind": "references",
        "weight": weight,
        "symbols": [{"name": name, "count": weight}],
        "evidence": [{"kind": "reference", "path": source, "line": 3, "symbol": name, "uncertainty": ""}],
    }


def delta_of(
    *,
    before: list[tuple[str, str]] = (),  # type: ignore[assignment]
    after: list[tuple[str, str]] = (),  # type: ignore[assignment]
    weights: Mapping[tuple[str, str], tuple[int, int]] | None = None,
    modified: tuple[str, ...] = (),
    added: tuple[str, ...] = (),
    deleted: tuple[str, ...] = (),
    context: Mapping[str, Any] | None = None,
    context_graph: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The Architecture Delta between a base with the *before* edges and a head with the *after* ones."""

    weights = weights or {}
    base_edges = [_edge(s, t, weight=weights.get((s, t), (1, 1))[0]) for s, t in before]
    head_edges = [_edge(s, t, weight=weights.get((s, t), (1, 1))[1]) for s, t in after]
    paths = {p for pair in (*before, *after) for p in pair} | set(modified) | set(added) | set(deleted)
    files = (
        [{"path": p, "status": "modified"} for p in modified]
        + [{"path": p, "status": "added"} for p in added]
        + [{"path": p, "status": "deleted"} for p in deleted]
    )
    return build_architecture_delta(
        review_id="review-1",
        revision_id="revision-1",
        packet={"files": files},
        base_architecture={"nodes": sorted(paths - set(added)), "edges": base_edges},
        snapshot_architecture={"nodes": sorted(paths - set(deleted)), "edges": head_edges},
        context_graph=context_graph or {"nodes": [], "edges": []},
        context=context or {},
        base_identity="base",
        snapshot_identity="head",
    )


def section_of(delta: Mapping[str, Any]) -> str:
    drawn = draw(delta)
    assert drawn is not None, "a diagram was expected"
    return drawn.section


# --------------------------------------------------------------------------- #
# what is drawn
# --------------------------------------------------------------------------- #


def test_a_call_edge_added_between_two_modules_is_a_solid_arrow_marked_added() -> None:
    delta = delta_of(after=[("src/cache.py", "src/auth.py")], modified=("src/cache.py",))
    section = section_of(delta)
    drawn = block_of(section)

    source, arrow, label, target = drawn.edge("src/cache.py", "src/auth.py")
    assert arrow == "-->" and label == "+auth"
    assert drawn.nodes[source] == ("src/cache.py", "changed") and drawn.nodes[target] == ("src/auth.py", "context")
    assert section.startswith("### Diagram\n\n"), "a heading and the block, open by default"
    assert "<details" not in section and section.rstrip().endswith("<!-- /lemoncrow:diagram -->")
    # One legend line under the block names every encoding.
    legend = [line for line in section.splitlines() if line.startswith("<sub>**+**")]
    assert len(legend) == 1 and "added (solid)" in legend[0] and "removed (dashed)" in legend[0]
    assert "bold changed" in legend[0] and "grey unchanged" in legend[0]
    # The classes read on a light and a dark page alike: each carries its own fill and its own text colour.
    for line in drawn.text.splitlines():
        if line.strip().startswith("classDef"):
            assert re.search(r"fill:#[0-9a-f]{6}", line) and "color:#1f2328" in line


def test_a_removed_edge_is_dashed_and_so_is_the_file_that_went_away() -> None:
    delta = delta_of(
        before=[("src/billing.py", "src/legacy.py"), ("src/api.py", "src/billing.py")],
        after=[("src/api.py", "src/billing.py")],
        deleted=("src/legacy.py",),
        modified=("src/billing.py",),
    )
    drawn = block_of(section_of(delta))

    _, arrow, label, target = drawn.edge("src/billing.py", "src/legacy.py")
    assert arrow == "-.->" and label == f"{MINUS}legacy"
    assert drawn.nodes[target] == (f"{MINUS}src/legacy.py", "removed")
    assert "removed" in drawn.classes and "stroke-dasharray" in drawn.text
    # The relationship that did not change is not drawn at all.
    assert len(drawn.edges) == 1


def test_a_changed_reference_count_is_bold_and_never_the_reason_for_a_diagram() -> None:
    weighted = {("src/a.py", "src/b.py"): (1, 3)}
    alone = delta_of(before=[("src/a.py", "src/b.py")], after=[("src/a.py", "src/b.py")], weights=weighted)
    assert draw(alone) is None, "a count that moved is not worth a picture on its own"

    delta = delta_of(
        before=[("src/a.py", "src/b.py")],
        after=[("src/a.py", "src/b.py"), ("src/a.py", "src/c.py")],
        weights=weighted,
        modified=("src/a.py",),
    )
    drawn = block_of(section_of(delta))
    assert drawn.edge("src/a.py", "src/b.py")[1:3] == ("==>", f"refs 1 {ARROW} 3")
    assert drawn.edge("src/a.py", "src/c.py")[1] == "-->"
    assert "stroke-width:4px" in drawn.text  # bold


@pytest.mark.parametrize(
    "delta",
    [
        # Nothing about the relationships changed: a body edit.
        delta_of(before=[("src/a.py", "src/b.py")], after=[("src/a.py", "src/b.py")], modified=("src/a.py",)),
        # Nothing at all.
        delta_of(modified=("src/a.py",)),
        # Files came and went with no relationship to or from them.
        delta_of(added=("src/new.py",), deleted=("src/old.py",)),
    ],
    ids=["unchanged-edges", "no-edges", "files-only"],
)
def test_a_trivial_change_draws_no_diagram(delta: Mapping[str, Any]) -> None:
    assert draw(delta) is None


def test_only_what_the_comparison_established_is_drawn() -> None:
    """Current-index context and a Snapshot fact with no base to compare never become an added or removed edge."""

    nodes = [
        {"id": diagram_module_id(path), "path": path, "label": path, "kind": "file", "exactness": exact, "status": "x"}
        for path, exact in (("src/a.py", "snapshot"), ("src/b.py", "current_index"))
    ]
    context_graph = {
        "nodes": nodes,
        "edges": [
            {"source": nodes[0]["id"], "target": nodes[1]["id"], "kind": "calls", "exactness": "current_index"},
            {"source": nodes[1]["id"], "target": nodes[0]["id"], "kind": "calls", "exactness": "snapshot"},
        ],
    }
    delta = delta_of(modified=("src/a.py",), context_graph=context_graph)
    assert any(edge["delta_status"] in {"context_only", "unknown"} for edge in delta["graph"]["edges"])
    assert draw(delta) is None

    unavailable = {"status": "unavailable", "reason": "no comparison", "graph": {"nodes": [], "edges": []}}
    assert draw(unavailable) is None
    # An unavailable comparison is said out loud (and not recorded, so the next run tries again).
    said = resolve(
        "auto",
        previous=None,
        existing_body="",
        head_sha=HEAD,
        merge_base_sha=MERGE_BASE,
        compute=lambda: (unavailable, ""),
    )
    assert not said.section and not said.state and "no comparison" in said.note


def diagram_module_id(path: str) -> str:
    import hashlib

    return f"file:{hashlib.sha256(path.encode()).hexdigest()[:20]}"


def test_a_persistence_change_is_drawn_as_a_flowchart_and_never_as_an_invented_er_diagram() -> None:
    """The delta's nodes are files. A migration is flagged as a persistence boundary but names no table or column, so
    there is no entity to draw: the flowchart is the only diagram it supports. The same goes for inheritance (the
    delta has only reference-style edges) and for call order (a deterministic comparison has none)."""

    path = "db/migrations/0002_orders.sql"
    context_graph = {
        "nodes": [
            {
                "id": diagram_module_id(p),
                "path": p,
                "label": p,
                "kind": "file",
                "exactness": "snapshot",
                "status": "changed",
                "boundaries": ["persistence"],
                "symbols": [{"name": "orders", "kind": "table"}],
            }
            for p in (path, "src/models.py")
        ],
        "edges": [],
    }
    delta = delta_of(
        after=[("src/models.py", path)],
        modified=("src/models.py",),
        added=(path,),
        context_graph=context_graph,
        context={"source": "current_index", "readiness": {"graph": {"state": "ready"}}},
    )
    assert any(node["boundaries"] == ["persistence"] for node in delta["graph"]["nodes"])
    section = section_of(delta)
    block_of(section)
    assert "flowchart LR" in section
    for other in ("erDiagram", "classDiagram", "sequenceDiagram", "stateDiagram"):
        assert other not in section
    assert diagram_module.KIND == "flowchart"


# --------------------------------------------------------------------------- #
# limits and grouping
# --------------------------------------------------------------------------- #


def test_few_files_are_drawn_one_by_one_inside_a_box_per_directory() -> None:
    delta = delta_of(
        after=[("api/orders.py", "core/engine.py"), ("jobs/nightly.py", "core/engine.py")],
        modified=("api/orders.py", "jobs/nightly.py"),
    )
    drawn = block_of(section_of(delta))
    assert sorted(drawn.boxes.values()) == ["api", "core", "jobs"]
    assert sorted(label for label, _ in drawn.nodes.values()) == ["engine.py", "nightly.py", "orders.py"]


def test_many_files_are_grouped_by_directory_under_the_node_cap() -> None:
    callers = [f"svc/s{index:02d}/handler.py" for index in range(30)]
    delta = delta_of(after=[(path, "core/engine.py") for path in callers], modified=tuple(callers))
    drawn_section = draw(delta)
    assert drawn_section is not None
    drawn = block_of(drawn_section.section)

    assert len(drawn.nodes) <= MAX_NODES and len(drawn.nodes) == 2
    assert sorted(label for label, _ in drawn.nodes.values()) == ["core (1 file)", "svc (30 files)"]
    ((_, arrow, label, _),) = drawn.edges
    assert (arrow, label) == ("-->", "30 added")
    assert "Files are grouped by directory." in drawn_section.section
    assert drawn_section.hidden == 0, "every relationship is in the one arrow"


def test_a_directory_deep_enough_to_fit_is_the_grouping_level() -> None:
    """Twenty directories fit under the cap at the second level, so they are not collapsed into one."""

    callers = [f"pkg/m{index:02d}/a{leaf}.py" for index in range(20) for leaf in range(2)]
    delta = delta_of(after=[(path, "lib/base.py") for path in callers], modified=tuple(callers))
    drawn = block_of(section_of(delta))
    assert len(drawn.nodes) == 21
    assert "pkg/m00 (2 files)" in {label for label, _ in drawn.nodes.values()}


def test_past_the_edge_cap_the_rest_is_counted_not_hidden() -> None:
    """Sixteen files in eight directories, each file referring to a file of every other directory: 56 relationships."""

    files = [f"m{index}/f{leaf}.py" for index in range(8) for leaf in range(2)]
    pairs = [(f"m{a}/f0.py", f"m{b}/f1.py") for a in range(8) for b in range(8) if a != b]
    delta = delta_of(after=pairs, modified=tuple(files))
    section = section_of(delta)
    drawn = block_of(section)
    assert len(pairs) == 56 and len(drawn.edges) == MAX_EDGES
    assert "16 more relationships not shown." in section


def test_one_busy_directory_cannot_be_grouped_so_the_busiest_files_are_kept_and_the_rest_counted() -> None:
    files = [f"src/f{index:02d}.py" for index in range(31)]
    chain = [(files[index], files[index + 1]) for index in range(30)]
    delta = delta_of(after=chain, modified=tuple(files))
    section = section_of(delta)
    drawn = block_of(section)

    assert len(drawn.nodes) <= MAX_NODES
    assert not drawn.boxes or len(drawn.boxes) == 1
    hidden = len(chain) - len(drawn.edges)
    assert hidden > 0 and f"{hidden} more relationships not shown." in section
    assert "grouped" not in section, "a single directory is not grouped"


def test_a_removed_edge_outranks_a_changed_one_when_the_cap_cuts() -> None:
    pairs = [(f"s{index:02d}/a.py", f"t{index:02d}/b.py") for index in range(MAX_EDGES + 5)]
    weights = {pair: (1, 2) for pair in pairs}
    removed = ("s99/gone_a.py", "t99/gone_b.py")
    delta = delta_of(before=[*pairs, removed], after=pairs, weights=weights, deleted=removed)
    drawn = block_of(section_of(delta))
    assert drawn.edge(*removed)[1] == "-.->"


# --------------------------------------------------------------------------- #
# escaping, and nothing a model wrote
# --------------------------------------------------------------------------- #

HOSTILE = [
    'src/a"b.py',
    "src/a]b[c.py",
    "src/<img src=x onerror=alert(1)>.py",
    "src/`tick`.py",
    "src/pipe|name.py",
    "src/arrow-->node.py",
    "src/@octocat.py",
    "src/hash#1;semi.py",
    "src/%%comment.py",
    "src/back\\slash.py",
    "src/new\nline.py",
    "src/\N{RIGHT-TO-LEFT OVERRIDE}evil.py",
    "src/\u0000nul.py",
    "src/curly{brace}.py",
    "src/![x](http://evil).py",
    "src/</details>.py",
    "src/" + "long" * 40 + ".py",
    "src/caf\N{LATIN SMALL LETTER E WITH ACUTE}/\N{CJK UNIFIED IDEOGRAPH-65E5}\N{CJK UNIFIED IDEOGRAPH-672C}.py",
]


@pytest.mark.parametrize("name", HOSTILE)
def test_mermaid_breaking_characters_in_a_path_cannot_leave_their_label(name: str) -> None:
    delta = delta_of(after=[(name, "src/target.py")], modified=(name,))
    section = section_of(delta)
    block_of(section)  # the grammar holds: no stray quote, bracket, pipe, tag or fence
    block = re.findall(r"```mermaid\n(.*?)\n```", section, re.DOTALL)[0]
    assert "<" not in block and ">" not in block.replace("-->", "").replace("==>", "").replace("-.->", "")
    assert "@" not in block and "`" not in block
    assert section.count("```") == 2, "the name did not open or close a fence"
    assert section.count("### Diagram") == 1 and section.count("<!-- /lemoncrow:diagram -->") == 1
    # Hostile text is also neutralised the way the rest of the comment is: no mention, image or link can be made.
    assert "![" not in section and "](" not in section


def test_a_label_is_cut_on_the_text_not_in_the_middle_of_an_entity() -> None:
    long_name = "src/" + "<" * 100
    delta = delta_of(after=[(long_name, "src/target.py")], modified=(long_name,))
    drawn = block_of(section_of(delta))
    label = next(label for label, _ in drawn.nodes.values() if "target" not in label)
    # Whole entities after the ellipsis that says the front of the path was cut; never half of one.
    assert re.fullmatch("\N{HORIZONTAL ELLIPSIS}(?:#60;)+", label), label


def test_a_node_id_is_a_counter_and_never_a_name_from_the_pull_request() -> None:
    delta = delta_of(after=[("src/n1.py", "src/g1.py")], modified=("src/n1.py",))
    drawn = block_of(section_of(delta))
    assert set(drawn.nodes) == {"n1", "n2"} and set(drawn.boxes) <= {"g1"}


def test_nothing_in_the_delta_but_paths_and_symbols_reaches_the_drawing() -> None:
    """The delta carries labels, reasons, review notes and a summary. None of them is drawn, whoever wrote it."""

    marker = "MODEL-WRITTEN-ZXQ"
    delta = delta_of(after=[("src/a.py", "src/b.py")], modified=("src/a.py",))
    delta["reason"] = marker
    delta["summary"]["important_concerns"] = [marker]
    delta["flow"]["reason"] = marker
    for node in delta["graph"]["nodes"]:
        node.update(label=marker, reasons=[marker], review={"note": marker}, uncertainty=marker, symbols=[marker])
    for edge in delta["graph"]["edges"]:
        edge["evidence"] = [{"snippet": marker, "path": "src/a.py", "line": 1, "symbol": marker}]
        edge["architecture_provenance"]["note"] = marker
    drawn = draw(delta)
    assert drawn is not None and marker not in drawn.section


# --------------------------------------------------------------------------- #
# the summary: placement and the size limit
# --------------------------------------------------------------------------- #


def finding(identifier: str, severity: str = "p1", **fields: Any) -> Any:
    from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding

    values: dict[str, Any] = {
        "id": identifier,
        "fingerprint": f"fp-{identifier}",
        "severity": severity,
        "category": "security",
        "state": "new",
        "path": f"src/{identifier}.py",
        "line": 12,
        "end_line": 14,
        "target_ids": (f"target:sym:{identifier}",),
        "title": f"Finding {identifier}",
        "explanation": "e" * 300,
        "consequence": "c" * 100,
        "first_seen": "1234567",
        "last_seen": "1234567",
    }
    values.update(fields)
    return BotFinding(**values)


def summary_view(findings: tuple[Any, ...], section: str = "", digest: str = "") -> SummaryView:
    state = ReviewState(
        head_sha=HEAD,
        base_sha=MERGE_BASE,
        merge_base_sha=MERGE_BASE,
        pull_request="acme/widgets#7",
        findings=findings,
        usage=Usage(2, 1000, 200, 0.001),
        usage_total=Usage(2, 1000, 200, 0.001),
        diagram=digest,
    )
    return SummaryView(
        state=state,
        findings=findings,
        facts=RunFacts(True, 38, 3, len(findings), 0, 0, 0),
        receipt=Receipt(("src/a.py",), targets=4, candidates=3, investigated=2, rules=()),
        pull_number=7,
        repository="acme/widgets",
        head_sha=HEAD,
        diagram=section,
    )


def a_section() -> str:
    pairs = [(f"a{index % 5}/x{index}.py", f"b{index % 4}/y{index}.py") for index in range(20)]
    return section_of(delta_of(after=pairs, modified=tuple(p for p, _ in pairs)))


def test_the_diagram_sits_after_the_findings_and_before_the_receipt() -> None:
    section = a_section()
    clean_review, _ = render_summary(summary_view((), section))
    assert clean_review.index("### Diagram") < clean_review.index("What we checked")
    # With a finding open it follows the finding rows and the held-back block, and comes before the agent prompt.
    findings = (finding("one", "p1"), finding("two", "p2"))
    body, _ = render_summary(summary_view(findings, section))
    assert body.index("Finding one") < body.index("held back") < body.index("### Diagram")
    assert body.index("### Diagram") < body.index("Fix with agent prompt") < body.index("lemoncrow:state v1")


def test_a_summary_with_no_diagram_is_exactly_what_it_was_before() -> None:
    body, state = render_summary(summary_view((finding("one"),)))
    assert "### Diagram" not in body and state.diagram == "" and '"diagram"' not in body


def test_under_the_size_limit_the_diagram_goes_first_and_every_finding_keeps_its_text() -> None:
    section = a_section()

    def full_size(findings: tuple[Any, ...]) -> int:
        """The comment's length at the first detail level, before anything is shortened."""

        view = summary_view(findings)
        return len(_visible(view, view.state, 0)) + len(dump_state(view.state)) + 2

    # Findings full of text, until one more would not fit; then the last one's text is tuned so that the comment fits
    # without the diagram and not with it.
    rows = [finding(f"f{index:02d}", "p1", explanation="e" * 900, consequence="c" * 400) for index in range(60)]
    count = next(n for n in range(1, 61) if full_size(tuple(rows[: n + 1])) > MAX_COMMENT)
    kept = tuple(rows[:count])
    pad = next(
        extra
        for extra in range(0, 901)
        if MAX_COMMENT - len(section) - 1
        < full_size((*kept, finding("pad", "p2", explanation="p" * extra)))
        <= MAX_COMMENT
    )
    padded = (*kept, finding("pad", "p2", explanation="p" * pad))

    without, plain = render_summary(summary_view(padded))
    assert len(without) <= MAX_COMMENT and plain.findings == padded, "it fits without the diagram, nothing shortened"

    digest = diagram_module.digest_of(section)
    body, state = render_summary(summary_view(padded, section, digest))
    assert len(body) <= MAX_COMMENT
    assert "### Diagram" not in body, "the diagram is the first thing given up"
    assert state.findings == padded, "no finding lost any text for it"
    assert state.diagram == "", "the state does not claim a diagram that is not in the comment"
    parsed = parse_state(body)
    assert parsed is not None and parsed.diagram == "" and parsed.findings == padded
    assert body == without, "the comment is the one it would have been without a diagram"

    # One finding fewer and the diagram fits beside them all.
    roomy, roomy_state = render_summary(summary_view(padded[:-8], section, digest))
    assert "### Diagram" in roomy and roomy_state.diagram == digest


def test_a_comment_that_fits_keeps_its_diagram_and_records_its_digest() -> None:
    section = a_section()
    digest = diagram_module.digest_of(section)
    body, state = render_summary(summary_view((finding("one"),), section, digest))
    assert "### Diagram" in body and state.diagram == digest
    parsed = parse_state(body)
    assert parsed is not None and parsed.diagram == digest
    assert reuse(body, digest) == section, "what the comment holds is exactly what the digest names"


# --------------------------------------------------------------------------- #
# the hidden state
# --------------------------------------------------------------------------- #


def test_the_digest_round_trips_and_an_old_or_forged_state_reads_as_none() -> None:
    state = summary_view((), "", "1:0123456789abcdef").state
    assert parse_state(dump_state(state)) == state and '"diagram":"1:0123456789abcdef"' in dump_state(state)
    plain = summary_view((), "", "").state
    assert '"diagram"' not in dump_state(plain), "a state written before G17 round-trips unchanged"
    assert parse_state(dump_state(summary_view((), "", none_state()).state)).diagram == none_state()  # type: ignore[union-attr]
    for forged in ("--> <img>", "1:xyz", "x" * 500, "1:0123456789abcde", ""):
        wire = dump_state(plain).replace('"findings_dropped":0', f'"findings_dropped":0,"diagram":"{forged}"', 1)
        parsed = parse_state(wire)
        assert parsed is not None and parsed.diagram == ""


def test_reuse_refuses_a_comment_that_no_longer_holds_the_recorded_drawing() -> None:
    section = a_section()
    digest = diagram_module.digest_of(section)
    assert reuse(f"intro\n\n{section}\n\nmore", digest) == section
    assert reuse(section.replace("added", "ADDED", 1), digest) == ""
    assert reuse("a comment with no diagram", digest) == ""
    assert reuse(section, none_state()) == "" and reuse(section, "") == ""
    assert reuse(section, "9:" + digest.split(":")[1]) == "", "a drawing from another version is drawn again"


# --------------------------------------------------------------------------- #
# the whole bot, on a real git origin
# --------------------------------------------------------------------------- #

CALLS_AUTH = (
    "from auth import authorize\n\nCACHE = {}\n\n\ndef remember(key, value):\n"
    "    if authorize(key):\n        CACHE[key] = value\n"
)
#: Two more files, so that the change is not trivial: a change of at most two files and twenty lines gets no diagram.
PAD = {"src/plain.py": "VALUE = 2\n", "NOTES.md": "# notes\n\nMore words.\n"}


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every time the Architecture Delta is built for a pull request."""

    calls: list[str] = []
    real = diagram_module.delta_for_capture

    def counted(captured: Any, **kwargs: Any) -> Any:
        calls.append(captured.revision.head_sha)
        return real(captured, **kwargs)

    monkeypatch.setattr(diagram_module, "delta_for_capture", counted)
    return calls


def test_a_pull_request_that_adds_a_call_between_modules_gets_a_flowchart_in_its_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    report = world.review()

    assert report.state is not None and report.summary
    drawn = block_of(report.summary)
    source, arrow, label, target = drawn.edge("src/cache.py", "src/auth.py")
    assert (arrow, label) == ("-->", "+authorize"), "the added edge, from the delta, with the symbol it carries"
    assert drawn.nodes[source][1] == "changed" and drawn.nodes[target][1] == "context"
    assert re.fullmatch(r"2:[0-9a-f]{16}", report.state.diagram)
    assert f'"diagram":"{report.state.diagram}"' in report.summary
    assert world.github.summary_comments()[0]["body"] == report.summary
    assert counting and report.noop is False
    # The review itself is untouched: the model was called exactly as without a diagram.
    assert report.verdict is not None and report.verdict.state == "clean"


def test_a_trivial_change_draws_no_diagram_even_when_it_adds_a_relationship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One file and a few lines: a picture of two boxes and an arrow is noise, and the section is open by default."""

    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH})
    report = world.review()

    assert "### Diagram" not in report.summary
    assert report.state is not None and report.state.diagram == none_state(), "decided, and recorded as nothing to draw"
    assert report.verdict is not None and report.verdict.state == "clean"


def test_a_pull_request_that_removes_a_call_between_modules_draws_it_dashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = {**BASE, "src/cache.py": CALLS_AUTH}
    world = build_world(tmp_path, monkeypatch, base)
    world.origin.push({"src/cache.py": "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE[key] = value\n", **PAD})
    report = world.review()

    drawn = block_of(report.summary)
    assert drawn.edge("src/cache.py", "src/auth.py")[1:3] == ("-.->", f"{MINUS}authorize")


def test_a_pull_request_that_only_edits_a_body_gets_no_diagram_and_says_so_in_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/plain.py": "VALUE = 2\n"})
    report = world.review()

    assert report.state is not None
    assert "### Diagram" not in report.summary and "mermaid" not in report.summary
    assert report.state.diagram == none_state()
    assert counting, "the delta was read, and found nothing worth drawing"


def test_a_pull_request_that_changes_no_code_does_not_pay_for_the_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, {**BASE, "README.md": "# widgets\n"})
    world.origin.push({"README.md": "# widgets\n\nMore words.\n", "docs/notes.json": '{"a": 1}\n'})
    seen: list[Any] = []
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.architecture_delta.build_architecture_delta_from_revision",
        lambda **kwargs: seen.append(kwargs),
    )
    report = world.review()

    assert report.state is not None and report.state.diagram == none_state()
    assert seen == [] and "### Diagram" not in report.summary


def test_diagram_off_builds_nothing_and_leaves_no_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    report = world.review(diagram="off")

    assert counting == [], "no delta is computed when the diagram is off"
    assert report.state is not None and report.state.diagram == ""
    assert "### Diagram" not in report.summary and '"diagram"' not in report.summary
    assert not report.notes


def test_an_unknown_diagram_mode_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    with pytest.raises(PrReviewError, match="diagram must be one of auto, off"):
        world.review(diagram="always")


def test_a_re_run_on_an_unchanged_head_reuses_the_drawing_and_does_not_touch_the_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    first = world.review()

    def comment_writes() -> list[tuple[str, str]]:
        return [
            call
            for call in world.github.calls
            if call[0] in {"POST", "PATCH"} and "/issues/" in call[1] and not call[1].endswith("/reactions")
        ]

    writes = comment_writes()
    body = world.github.summary_comments()[0]["body"]
    assert len(counting) == 1

    second = world.review()
    assert second.noop is True, "nothing changed, nothing is written"
    assert len(counting) == 1, "the unchanged head is not parsed again"
    assert world.github.summary_comments()[0]["body"] == body
    assert comment_writes() == writes
    assert second.state is not None and first.state is not None and second.state.diagram == first.state.diagram


def test_a_comment_that_no_longer_holds_the_recorded_drawing_is_drawn_again_to_the_same_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    first = world.review()
    assert first.state is not None and diagram_module.reuse(first.summary, first.state.diagram)

    # Same head, but the comment was changed by hand, so what it holds is not what the state recorded: the delta is
    # read again, and it is deterministic, so the drawing and its digest come out the same.
    comment = world.github.summary_comments()[0]
    comment["body"] = comment["body"].replace("### Diagram", "Changed by hand")
    second = world.review()
    assert len(counting) == 2, "the drawing was not trusted blindly"
    assert second.state is not None and second.state.diagram == first.state.diagram
    assert diagram_module.reuse(second.summary, second.state.diagram) == diagram_module.reuse(
        first.summary, first.state.diagram
    )


def test_turning_the_diagram_off_on_an_unchanged_head_rewrites_the_comment_once_without_a_new_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    first = world.review()
    assert first.state is not None and first.state.diagram

    off = world.review(diagram="off")
    assert off.state is not None and off.state.diagram == "" and off.noop is False
    assert "### Diagram" not in world.github.summary_comments()[0]["body"]
    # It was not another review: the count and the cost of the last one stand.
    assert off.state.reviews == first.state.reviews and off.state.usage == first.state.usage
    again = world.review(diagram="off")
    assert again.noop is True


def test_a_new_push_draws_the_new_head_and_a_budget_stop_keeps_the_drawing_it_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    first = world.review(max_runs_per_pr=1)
    assert first.state is not None and first.state.diagram

    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    stopped = world.review(max_runs_per_pr=1)
    assert stopped.verdict is not None and stopped.verdict.state == "budget"
    assert stopped.state is not None and stopped.state.diagram == first.state.diagram
    assert "### Diagram" in stopped.summary and len(counting) == 1, "nothing was recomputed"

    resumed = world.review()
    assert resumed.state is not None and len(counting) == 2


def test_a_repository_over_the_file_limit_gets_no_diagram_and_a_note_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    monkeypatch.setenv(diagram_module.MAX_FILES_ENV, "2")
    report = world.review()

    assert report.verdict is not None and report.verdict.state == "clean"
    assert "### Diagram" not in report.summary and report.state is not None and report.state.diagram == ""
    assert any("over the 2-file limit" in note and diagram_module.MAX_FILES_ENV in note for note in report.notes)
    assert diagram_module.max_source_files({}) == DEFAULT_MAX_SOURCE_FILES
    assert diagram_module.max_source_files({diagram_module.MAX_FILES_ENV: "12"}) == 12
    assert diagram_module.max_source_files({diagram_module.MAX_FILES_ENV: "many"}) == DEFAULT_MAX_SOURCE_FILES


def test_a_failure_while_reading_the_delta_never_fails_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(FIRST_PUSH)

    def broken(**kwargs: Any) -> Any:
        raise RuntimeError("tree-sitter exploded: /home/runner/private/path")

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.architecture_delta.build_architecture_delta_from_revision", broken
    )
    report = world.review()

    assert report.verdict is not None and report.state is not None and report.state.complete
    assert "### Diagram" not in report.summary and report.state.diagram == ""
    (note,) = [note for note in report.notes if "change diagram" in note]
    assert "RuntimeError" in note and "private" not in note, "the type, never the message, reaches the log"


def test_a_finding_title_a_model_wrote_cannot_reach_the_diagram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Break out </details> @octocat ![x](http://evil.example) flowchart"
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(
        {"src/cache.py": CALLS_AUTH.replace("CACHE[key] = value", f"CACHE[key] = value  # BUG: {title}"), **PAD}
    )
    report = world.review()

    assert any("octocat" in finding.title for finding in report.findings), "the model's finding is in the comment"
    (section,) = re.findall(r"### Diagram\n.*?<!-- /lemoncrow:diagram -->", report.summary, re.DOTALL)
    block_of(section)
    for word in ("octocat", "evil.example", "Break out", "![", "](", "</details> @"):
        assert word not in section


def test_many_disjoint_relationships_still_draw_the_strongest_ones_and_count_the_rest() -> None:
    """Thirty separate pairs: no directory groups them, and the first twenty-five files by name are not all that is
    drawn -- every relationship taken brings both of its files, so the diagram is never empty."""

    pairs = [(f"src/a{index:02d}.py", f"src/b{index:02d}.py") for index in range(30)]
    delta = delta_of(after=pairs, modified=tuple(source for source, _ in pairs))
    section = section_of(delta)
    drawn = block_of(section)
    assert 0 < len(drawn.edges) <= MAX_NODES // 2 and len(drawn.nodes) <= MAX_NODES
    hidden = len(pairs) - len(drawn.edges)
    assert f"{hidden} more relationships not shown." in section


def test_an_ignore_command_that_rewrites_the_summary_keeps_the_same_drawing_and_does_not_parse_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counting: list[str]
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    world.origin.push(
        {"src/cache.py": CALLS_AUTH.replace("CACHE[key] = value", "CACHE[key] = value  # BUG: leaks"), **PAD}
    )
    first = world.review()
    assert first.state is not None and first.state.open_findings() and len(counting) == 1
    section = diagram_module.reuse(first.summary, first.state.diagram)
    assert section

    told = world.chat("@lemoncrow ignore leaks")

    assert told.state is not None and told.state.suppressions, "the instruction was applied"
    body = world.github.summary_comments()[0]["body"]
    assert "dismissed by feedback" in body, "the summary was rewritten"
    assert diagram_module.reuse(body, first.state.diagram) == section, "and it still holds the same drawing"
    assert len(counting) == 1, "an instruction on the same head is not another parse"


# --------------------------------------------------------------------------- #
# Mermaid reads a label as Markdown
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "drawn"),
    [
        ("src/__init__.py", "src/#95;#95;init#95;#95;.py"),
        ("_private", "#95;private"),
        ("trailing_", "trailing#95;"),
        ("snake_case_name", "snake_case_name"),
        ("src/a_b.py", "src/a_b.py"),
    ],
)
def test_an_underscore_at_the_edge_of_a_word_is_an_entity_so_a_file_is_never_drawn_as_a_different_name(
    name: str, drawn: str
) -> None:
    """Mermaid 11 draws ``__init__.py`` as a bold "init.py": the label goes through Markdown before it is drawn."""

    assert diagram_module._esc(name) == drawn


@pytest.mark.parametrize(
    ("label", "drawn"),
    [
        ("- removes the retry", "#45; removes the retry"),
        ("+ adds a check", "#43; adds a check"),
        ("1. first", "#49;. first"),
        ("12) twelfth", "#49;2#41; twelfth"),
        ("1.5 is a version", "1.5 is a version"),
        ("a - b", "a - b"),
        ("-x", "-x"),
    ],
)
def test_a_label_that_starts_like_a_list_item_does_not_become_one(label: str, drawn: str) -> None:
    """Mermaid 11 draws "Unsupported markdown: list" in place of a label that starts with ``- ``, ``+ `` or ``1. ``."""

    assert diagram_module._esc(label) == drawn


def test_a_changed_file_named_like_markdown_is_drawn_with_its_real_name() -> None:
    delta = delta_of(after=[("src/__init__.py", "src/_core.py")], modified=("src/__init__.py",))
    section = section_of(delta)
    block_of(section)  # the block is still made only of lines the diagram may emit
    assert "__init__" not in section and "src/#95;#95;init#95;#95;.py" in section
    assert '"+#95;core"' in section, "the sign goes against the name, so it is not a list item"
