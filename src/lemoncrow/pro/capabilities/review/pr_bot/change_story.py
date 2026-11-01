"""The Change Story for GitHub: the Review's step-by-step walk over a change, as a numbered list in the summary.

This is a port of the *ordering* of the Reader's Change Story (``frontend/src/review/changeStory/changeStoryModel.ts``):
the changed entry point first, then the changed files it reaches through recorded relationships (dependents before what
they depend on, following the architecture flow when the comparison gives one), then the unchanged callers the change
reaches. The tie-breaks are the same: rank, then graph order; Snapshot-exact links before others, then heavier links. A
shared fixture (``tests/fixtures/change_story/ordering.json``) is checked by both test suites, so the two walks cannot
drift apart.

What it reads is the Architecture Delta's own graph (the nodes and edges the Review Map draws), its flow, and the open
P0/P1 findings when the caller has them. It calls no model and adds no relationship. Every step keeps its exactness
(``snapshot``, ``observed``, ``current_index``, ``inferred``) and its change kind, and a finding is attached to a step
only where its anchor proves the link: the step's own file, or the recorded site of the relationship it arrived by.

The checks, receipts and human-review state of the Reader's steps are not ported: the summary already reports CI and
human review once, for the whole pull request, and a step must not repeat a claim it cannot prove for its own file.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Final
from urllib.parse import quote

from lemoncrow.pro.capabilities.review.pr_bot.render import code, plural

MOVED: Final[frozenset[str]] = frozenset({"added", "removed", "changed"})
_CLOSED_DISPOSITIONS: Final[frozenset[str]] = frozenset({"dismissed", "fixed", "superseded_stale"})
_EXACTNESS: Final[frozenset[str]] = frozenset({"snapshot", "observed", "current_index", "inferred"})
_CHANGES: Final[frozenset[str]] = frozenset({"added", "removed", "changed", "unchanged", "context_only"})

#: The Reader's defaults (``DEFAULT_MAX_CHANGED``, ``DEFAULT_MAX_AFFECTED``), so the story is the one the Review shows.
DEFAULT_MAX_CHANGED: Final[int] = 20
DEFAULT_MAX_AFFECTED: Final[int] = 6
#: Steps the summary lists before it says how many more the Review has.
MAX_LISTED: Final[int] = 8

EDGE_KIND_LABEL: Final[Mapping[str, str]] = {
    "impacts": "affects",
    "calls": "calls",
    "imports": "imports",
    "references": "references",
    "other": "relates to",
}
#: How a relationship's exactness reads in a sentence. Only ``snapshot`` and ``observed`` are facts of this commit.
EXACTNESS_WORDS: Final[Mapping[str, str]] = {
    "snapshot": "in this commit",
    "observed": "observed in a recorded run",
    "current_index": "from the current index, not this commit",
    "inferred": "inferred",
    "unknown": "exactness unavailable",
}
CHANGE_WORDS: Final[Mapping[str, str]] = {
    "added": "added",
    "removed": "removed",
    "changed": "changed",
    "unchanged": "unchanged",
    "context_only": "context only",
    "unknown": "change unknown",
}


# --------------------------------------------------------------------------- #
# the graph's vocabulary (mapModel.ts / changeStoryModel.ts)
# --------------------------------------------------------------------------- #


def edge_kind(kind: object) -> str:
    value = str(kind or "")
    if value == "impacts":
        return "impacts"
    if value in {"calls", "call"}:
        return "calls"
    if value in {"imports", "import"}:
        return "imports"
    if value in {"references", "reference"}:
        return "references"
    return "other"


def visual_ends(edge: Mapping[str, Any]) -> tuple[str, str]:
    """``(from, to)``: from the dependent to what it depends on. An impact points from the changed code to the site it
    reaches; the site *uses* the changed code, so it is read the other way round."""

    source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
    return (target, source) if edge_kind(edge.get("kind")) == "impacts" else (source, target)


def story_exactness(value: object) -> str:
    return value if isinstance(value, str) and value in _EXACTNESS else "unknown"


def node_change(node: Mapping[str, Any]) -> str:
    delta = str(node.get("delta_status") or "")
    if delta in _CHANGES:
        return delta
    if delta == "unknown":
        return "unknown"
    status = node.get("status")
    if status == "new":
        return "added"
    if status == "changed":
        return "removed" if re.fullmatch(r"d|deleted|removed", str(node.get("change") or ""), re.I) else "changed"
    return "context_only" if status == "context" else "unchanged"


def edge_change(edge: Mapping[str, Any]) -> str:
    delta = str(edge.get("delta_status") or "")
    return delta if delta in _CHANGES else "unknown"


def is_changed_node(node: Mapping[str, Any]) -> bool:
    return str(node.get("delta_status") or "") in MOVED or node.get("status") in {"changed", "new"}


def node_name(node: Mapping[str, Any]) -> str:
    path = str(node.get("path") or "")
    if node.get("kind") == "file":
        return PurePosixPath(path).name if path and not path.endswith("/") else str(node.get("label") or path)
    return str(node.get("label") or "")


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _rank(node: Mapping[str, Any]) -> float:
    rank = _number(node.get("rank"))
    return rank if rank > 0 else math.inf


def _rows(value: object) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FindingRef:
    """An open P0 or P1 finding, as far as the story needs it: where its anchor is."""

    id: str
    severity: str  # p0 | p1
    title: str
    path: str
    start_line: int
    end_line: int
    side: str = "head"  # base | head


@dataclass(frozen=True, slots=True)
class StoryFinding:
    id: str
    severity: str
    title: str
    path: str
    line: int
    side: str
    #: Where the anchor proves the link: the step's file, or the relationship's recorded site.
    on: str  # node | relationship


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def findings_from_annotations(rows: Iterable[Mapping[str, Any]]) -> tuple[FindingRef, ...]:
    """The Review's annotations (the Reader's ``Annotation``) that the story may attach: open, anchored P0/P1."""

    out: list[FindingRef] = []
    for row in rows:
        severity = str(row.get("severity") or "")
        if (
            severity not in {"p0", "p1"}
            or row.get("state") != "open"
            or not row.get("anchored")
            or str(row.get("finding_disposition") or "") in _CLOSED_DISPOSITIONS
            or not row.get("path")
        ):
            continue
        body = str(row.get("body") or "")
        out.append(
            FindingRef(
                id=str(row.get("id") or ""),
                severity=severity,
                title=str(row.get("title") or "") or body.split("\n")[0] or "Finding",
                path=str(row["path"]),
                start_line=_int(row.get("start_line")),
                end_line=_int(row.get("end_line")),
                side="base" if row.get("side") == "old" else "head",
            )
        )
    return tuple(out)


def findings_from_bot(findings: Iterable[Any]) -> tuple[FindingRef, ...]:
    """The pull request bot's findings (:class:`findings.BotFinding`) that the story may attach: open P0/P1 with a path."""

    out: list[FindingRef] = []
    for finding in findings:
        severity = str(getattr(finding, "severity", "") or "")
        if severity not in {"p0", "p1"} or not getattr(finding, "is_open", True) or not getattr(finding, "path", ""):
            continue
        start = _int(getattr(finding, "line", 0))
        out.append(
            FindingRef(
                id=str(getattr(finding, "id", "") or ""),
                severity=severity,
                title=str(getattr(finding, "title", "") or "Finding"),
                path=str(finding.path),
                start_line=start,
                end_line=max(start, _int(getattr(finding, "end_line", 0))),
                side="base" if getattr(finding, "side", "new") == "old" else "head",
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# the story
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StoryVia:
    """The relationship a step was reached by, in visual ends (dependent -> what it depends on)."""

    edge_id: str
    from_id: str
    to_id: str
    from_name: str
    to_name: str
    relation: str
    kind: str  # edge_kind(relation)
    change: str
    exactness: str
    #: Only a Snapshot-exact or observed relationship is a fact of this commit; the rest are drawn dashed.
    recorded: bool
    site: tuple[str, int] | None


@dataclass(frozen=True, slots=True)
class StoryStep:
    index: int
    node_id: str
    role: str  # entry | changed | dependency | affected
    title: str
    path: str
    line: int
    side: str  # base | head
    change: str
    exactness: str
    via: StoryVia | None
    findings: tuple[StoryFinding, ...]
    #: Every path the step's node stands for (its old path too), which a finding's anchor is matched against.
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChangeStory:
    steps: tuple[StoryStep, ...]
    ordering: str  # flow | dependencies
    order_note: str
    changed_total: int
    omitted_changed: int
    affected_total: int
    omitted_affected: int
    #: False when no findings were supplied: the absence of a finding then proves nothing, and none is mentioned.
    findings_known: bool


@dataclass(frozen=True, slots=True)
class _Link:
    edge: Mapping[str, Any]
    other: str


def build_change_story(
    graph: Mapping[str, Any],
    *,
    flow: Mapping[str, Any] | None = None,
    findings: Sequence[FindingRef] | None = None,
    max_changed: int = DEFAULT_MAX_CHANGED,
    max_affected: int = DEFAULT_MAX_AFFECTED,
) -> ChangeStory:
    """The walk of ``buildChangeStory`` over *graph* (the delta's or the Review Map's ``nodes`` and ``edges``)."""

    nodes = [node for node in _rows(graph.get("nodes")) if str(node.get("id") or "")]
    # Like the Reader's ``new Map(...)``: a repeated id keeps its last node.
    by_id: dict[str, Mapping[str, Any]] = {str(node["id"]): node for node in nodes}
    by_path: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        path = str(node.get("path") or "")
        if path and path not in by_path:
            by_path[path] = node
    position = {str(node["id"]): index for index, node in enumerate(nodes)}

    def order(node: Mapping[str, Any]) -> tuple[float, int]:
        return (_rank(node), position.get(str(node["id"]), 0))

    changed = sorted((node for node in nodes if is_changed_node(node)), key=order)
    changed_ids = {str(node["id"]) for node in changed}
    dependencies: dict[str, list[_Link]] = {}
    dependents: dict[str, list[_Link]] = {}
    edges = _rows(graph.get("edges"))
    for edge in edges:
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        if source not in by_id or target not in by_id or source == target:
            continue
        start, end = visual_ends(edge)
        dependencies.setdefault(start, []).append(_Link(edge, end))
        dependents.setdefault(end, []).append(_Link(edge, start))

    def link_order(link: _Link) -> tuple[int, float, float, int]:
        return (
            -int(link.edge.get("exactness") == "snapshot"),
            -_number(link.edge.get("weight")),
            *order(by_id[link.other]),
        )

    for links in (*dependencies.values(), *dependents.values()):
        links.sort(key=link_order)

    flow_steps = _rows(flow.get("steps")) if isinstance(flow, Mapping) and flow.get("applicable") else []
    flow_by_edge = {str(step.get("edge_id") or ""): step for step in flow_steps}
    visited: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = []
    seen: set[str] = set()

    def emit(node: Mapping[str, Any], via: Mapping[str, Any] | None) -> None:
        if str(node["id"]) not in seen:
            seen.add(str(node["id"]))
            visited.append((node, via))

    cursor: dict[str, int] = {}

    # An unchanged file is walked to only through a relationship the comparison says moved, and never past it.
    def walkable(link: _Link) -> bool:
        return link.other not in seen and (
            link.other in changed_ids or str(link.edge.get("delta_status") or "") in MOVED
        )

    # Iterative depth-first walk (preorder), so a long chain of changed files cannot overflow the stack.
    def walk(start: Mapping[str, Any], via: Mapping[str, Any] | None) -> None:
        emit(start, via)
        path = [start] if str(start["id"]) in changed_ids else []
        while path:
            here = str(path[-1]["id"])
            links = dependencies.get(here, [])
            at = cursor.get(here, 0)
            while at < len(links) and not walkable(links[at]):
                at += 1
            cursor[here] = at + 1
            if at >= len(links):
                path.pop()
                continue
            child = by_id[links[at].other]
            emit(child, links[at].edge)
            if str(child["id"]) in changed_ids:
                path.append(child)

    ordering = "dependencies"
    if flow_steps:
        edge_by_id = {str(edge.get("id") or ""): edge for edge in edges}
        for step in flow_steps:
            found = edge_by_id.get(str(step.get("edge_id") or ""))
            source_node = (
                by_id.get(str(found.get("source") or "")) if found else by_path.get(str(step.get("from") or ""))
            )
            target_node = by_id.get(str(found.get("target") or "")) if found else by_path.get(str(step.get("to") or ""))
            if source_node is None or target_node is None:
                continue
            ordering = "flow"
            start_id, end_id = visual_ends(found) if found else (str(source_node["id"]), str(target_node["id"]))
            emit(by_id[start_id], None)
            emit(by_id[end_id], found)
        # Continue each flow file's own changed dependencies before the rest.
        for node, _ in list(visited):
            walk(node, None)

    # Tests depend on the code they cover, but they are not where a change starts: they neither make an entry point
    # nor stop one, and are walked last.
    def is_test(node_id: str) -> bool:
        return by_id.get(node_id, {}).get("category") == "test"

    entries = [
        node
        for node in changed
        if not is_test(str(node["id"]))
        and not any(
            link.other in changed_ids and not is_test(link.other) for link in dependents.get(str(node["id"]), [])
        )
    ]
    for node in entries:
        if str(node["id"]) not in seen:
            walk(node, None)
    # Cycles and files without a recorded relationship to the rest, then tests.
    for node in changed:
        if str(node["id"]) not in seen and not is_test(str(node["id"])):
            walk(node, None)
    for node in changed:
        if str(node["id"]) not in seen:
            walk(node, None)

    shown_path = visited[: max(0, max_changed)]
    shown_ids = {str(node["id"]) for node, _ in shown_path}
    shown_changed = [item for item in shown_path if str(item[0]["id"]) in changed_ids]
    # A step whose arriving edge starts at a file left out of the walk keeps no arrow.
    chained: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = []
    for node, via in shown_path:
        if via is not None:
            start_id, end_id = visual_ends(via)
            other = end_id if start_id == str(node["id"]) else start_id
            if other not in shown_ids:
                via = None
        chained.append((node, via))

    # Unchanged code that depends on a walked file: the callers the change reaches.
    affected_links: dict[str, tuple[Mapping[str, Any], str, float]] = {}
    for node, _ in shown_changed:
        for link in dependents.get(str(node["id"]), []):
            if link.other in changed_ids or link.other in shown_ids:
                continue
            score = (1000 if link.edge.get("exactness") == "snapshot" else 0) + (_number(link.edge.get("weight")) or 1)
            prior = affected_links.get(link.other)
            if prior is None or score > prior[2]:
                affected_links[link.other] = (link.edge, str(node["id"]), score)
    affected_all = sorted(
        ((by_id[node_id], edge) for node_id, (edge, _, _) in affected_links.items()),
        key=lambda item: (-affected_links[str(item[0]["id"])][2], *order(item[0])),
    )
    affected = affected_all[: max(0, max_affected)]

    def via_for(edge: Mapping[str, Any]) -> StoryVia:
        start_id, end_id = visual_ends(edge)
        start, end = by_id.get(start_id), by_id.get(end_id)
        step = flow_by_edge.get(str(edge.get("id") or ""))
        observed = step is not None and step.get("grounding") == "observed" and bool(step.get("trace"))
        exactness = "observed" if observed else story_exactness(edge.get("exactness"))
        site = next(
            (
                (str(item["path"]), _int(item.get("line")))
                for item in _rows(edge.get("evidence"))
                if item.get("path") and _int(item.get("line")) > 0
            ),
            None,
        )
        return StoryVia(
            edge_id=str(edge.get("id") or ""),
            from_id=start_id,
            to_id=end_id,
            from_name=node_name(start) if start else start_id,
            to_name=node_name(end) if end else end_id,
            relation=str(edge.get("kind") or ""),
            kind=edge_kind(edge.get("kind")),
            change=edge_change(edge),
            exactness=exactness,
            recorded=exactness in {"snapshot", "observed"},
            site=site,
        )

    def paths_of(node: Mapping[str, Any]) -> tuple[str, ...]:
        raw = node.get("paths")
        listed = [str(path) for path in raw if path] if isinstance(raw, list) else []
        paths = listed or ([str(node["path"])] if node.get("path") else [])
        old = str(node.get("old_path") or "")
        return tuple([*paths, old] if old and old not in paths else paths)

    def step_of(node: Mapping[str, Any], via: Mapping[str, Any] | None, role: str, index: int) -> StoryStep:
        raw = node.get("selection")
        selection: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        change = node_change(node)
        side = "base" if selection.get("side") == "old" or change == "removed" else "head"
        raw_line = selection.get("line") if selection.get("line") is not None else node.get("line")
        via_step = via_for(via) if via is not None else None
        paths = paths_of(node)
        return StoryStep(
            index=index,
            node_id=str(node["id"]),
            role=role,
            title=node_name(node),
            path=str(node.get("path") or ""),
            line=_int(raw_line),
            side=side,
            change=change,
            exactness=story_exactness(node.get("exactness")),
            via=via_step,
            findings=findings_on(paths, via_step, findings or ()),
            paths=paths,
        )

    steps: list[StoryStep] = []
    for index, (node, via) in enumerate(chained):
        if str(node["id"]) not in changed_ids:
            role = "dependency"
        else:
            role = "entry" if index == 0 or via is None else "changed"
        steps.append(step_of(node, via, role, len(steps)))
    for node, edge in affected:
        steps.append(step_of(node, edge, "affected", len(steps)))

    observed_steps = sum(1 for step in steps if step.via is not None and step.via.exactness == "observed")
    if ordering == "flow":
        tail = (
            f"{plural(observed_steps, 'step')} matched a recorded run; the rest are not a runtime order."
            if observed_steps
            else "This is not a runtime order."
        )
        note = f"Ordered by the changed-relationship flow between the base and this Snapshot. {tail}"
    else:
        note = "Ordered by recorded dependencies between changed files, dependents first. This is not a runtime order."
    return ChangeStory(
        steps=tuple(steps),
        ordering=ordering,
        order_note=note,
        changed_total=len(changed),
        omitted_changed=max(0, len(changed) - len(shown_changed)),
        affected_total=len(affected_all),
        omitted_affected=max(0, len(affected_all) - len(affected)),
        findings_known=findings is not None,
    )


def findings_on(paths: Sequence[str], via: StoryVia | None, findings: Iterable[FindingRef]) -> tuple[StoryFinding, ...]:
    """The findings a step may show: anchored in one of its *paths*, or on the recorded site of the relationship it
    arrived by. P0 first, then by line."""

    own = set(paths)
    out: list[StoryFinding] = []
    for finding in findings:
        on = ""
        if finding.path in own:
            on = "node"
        elif (
            via is not None
            and via.site is not None
            and finding.path == via.site[0]
            and finding.start_line <= via.site[1] <= max(finding.start_line, finding.end_line)
        ):
            on = "relationship"
        if on:
            out.append(
                StoryFinding(
                    finding.id, finding.severity, finding.title, finding.path, finding.start_line, finding.side, on
                )
            )
    return tuple(sorted(out, key=lambda row: (row.severity, row.line)))


def attach_findings(story: ChangeStory, findings: Sequence[FindingRef]) -> ChangeStory:
    """*story* with *findings* attached where their anchors prove it (and so ``findings_known``); the walk is unchanged."""

    return replace(
        story,
        steps=tuple(replace(step, findings=findings_on(step.paths, step.via, findings)) for step in story.steps),
        findings_known=True,
    )


# --------------------------------------------------------------------------- #
# words
# --------------------------------------------------------------------------- #


def role_words(step: StoryStep) -> str:
    """What the step is, then how it changed: ``entry point · changed``, ``changed``, ``affected caller · unchanged``."""

    change = CHANGE_WORDS.get(step.change, CHANGE_WORDS["unknown"])
    if step.role == "entry":
        return f"entry point \N{MIDDLE DOT} {change}"
    if step.role == "dependency":
        return "relied on, unchanged"
    if step.role == "affected":
        site = step.via is not None and step.via.kind == "impacts"
        return f"{'affected site' if site else 'affected caller'} \N{MIDDLE DOT} {change}"
    return change


def via_words(step: StoryStep, previous: StoryStep | None) -> str:
    """How the step was reached, from its own side: ``imports, in this commit``; ``auth.py calls it, inferred``;
    ``imports auth.py, in this commit`` for a caller. The other file is named unless it is the step just before."""

    via = step.via
    if via is None:
        return ""
    label = EDGE_KIND_LABEL[via.kind]
    exact = EXACTNESS_WORDS[via.exactness]
    if step.node_id == via.from_id:  # a caller: this file depends on the other
        phrase = f"affected by {via.to_name}" if via.kind == "impacts" else f"{label} {via.to_name}"
    elif via.kind == "impacts":  # an impact walked backwards: this changed file affects the other
        phrase = f"affects {via.from_name}"
    elif previous is not None and previous.node_id == via.from_id:
        phrase = label
    else:
        phrase = f"{via.from_name} {label} it"
    return f"{phrase}, {exact}"


def findings_words(step: StoryStep) -> str:
    counts = {severity: sum(1 for row in step.findings if row.severity == severity) for severity in ("p0", "p1")}
    return " \N{MIDDLE DOT} ".join(
        plural(count, f"{severity.upper()} finding") for severity, count in counts.items() if count
    )


def more_count(story: ChangeStory, listed: int) -> int:
    """What the Review has beyond the first *listed* steps: the rest of the walk and the files it did not reach."""

    return max(0, len(story.steps) - listed) + story.omitted_changed + story.omitted_affected


# --------------------------------------------------------------------------- #
# source links
# --------------------------------------------------------------------------- #

#: A repository's web root: ``https://host/owner/repo``, or a GitLab project in nested groups, with its ``/-`` (``https://host
#: /group/sub/project/-``), under which GitLab serves ``/blob/<sha>/<path>``. No segment is ``.`` or ``..``.
_REPO_URL: Final[re.Pattern[str]] = re.compile(
    r"https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+){2,20}"
)
_SHA: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{7,64}")


@dataclass(frozen=True, slots=True)
class SourceLinks:
    """Where a step's file is on the host: ``<repo_url>/blob/<sha>/<path>#L<line>`` at the head, or at the merge base
    for a file the pull request removes."""

    repo_url: str
    head_sha: str
    base_sha: str = ""

    def url(self, step: StoryStep) -> str:
        """The link for *step*, or ``""``: only a Snapshot-exact file links, since current-index context is not the
        commit under review. The path is percent-encoded, so no character of it can end the Markdown link."""

        sha = self.base_sha if step.side == "base" else self.head_sha
        if step.exactness != "snapshot" or not step.path or not _REPO_URL.fullmatch(self.repo_url):
            return ""
        if not _SHA.fullmatch(sha):
            return ""
        anchor = f"#L{step.line}" if step.line > 0 else ""
        return f"{self.repo_url}/blob/{sha}/{quote(step.path, safe='/')}{anchor}"


def links_from_env(environ: Mapping[str, str], *, head_sha: str, base_sha: str = "") -> SourceLinks | None:
    """The links of a GitHub Actions run (``GITHUB_SERVER_URL`` and ``GITHUB_REPOSITORY``); ``None`` outside one."""

    server = str(environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = str(environ.get("GITHUB_REPOSITORY") or "").strip("/")
    if not repository:
        return None
    links = SourceLinks(f"{server}/{repository}", head_sha, base_sha)
    return links if _REPO_URL.fullmatch(links.repo_url) else None


# --------------------------------------------------------------------------- #
# the summary's walkthrough
# --------------------------------------------------------------------------- #

HEADING: Final[str] = "**Change Story**"


def _shared_names(steps: Sequence[StoryStep]) -> set[str]:
    names = [step.title for step in steps]
    return {name for name in names if names.count(name) > 1}


def walkthrough(story: ChangeStory, *, links: SourceLinks | None = None, limit: int = MAX_LISTED) -> str:
    """The numbered walkthrough under the diagram; ``""`` for a story of fewer than two steps.

    ``1. api/auth.py · entry point · changed`` then ``2. → session.py (imports, in this commit) · changed · 1 P1
    finding``. A caller the change reaches reads ``← views.py (imports auth.py, in this commit) · affected caller``.
    Every name is a path or file name from the delta, in a code span (:func:`render.code`), and every link is built
    here from a validated repository URL and a percent-encoded path.
    """

    if len(story.steps) < 2:
        return ""
    listed = story.steps[: max(1, limit)]
    shared = _shared_names(listed)
    lines = [HEADING, "", f"<sub>{story.order_note}</sub>", ""]
    for number, step in enumerate(listed, 1):
        previous = story.steps[step.index - 1] if step.index > 0 else None
        name = step.path if step.via is None or step.title in shared or not step.title else step.title
        shown = f"`{code(name)}`" if name else "(no path)"
        url = links.url(step) if links is not None else ""
        target = f"[{shown}]({url})" if url else shown
        if step.via is None:
            head = target
        else:
            arrow = "\N{LEFTWARDS ARROW}" if step.node_id == step.via.from_id else "\N{RIGHTWARDS ARROW}"
            head = f"{arrow} {target} ({via_words(step, previous)})"
        parts = [head, role_words(step)]
        if step.exactness != "snapshot" and (step.via is None or step.via.exactness != step.exactness):
            parts.append(f"file {EXACTNESS_WORDS[step.exactness]}")
        if story.findings_known and (said := findings_words(step)):
            parts.append(said)
        lines.append(f"{number}. " + " \N{MIDDLE DOT} ".join(parts))
    if more := more_count(story, len(listed)):
        lines += ["", f"<sub>{more} more in the Review.</sub>"]
    return "\n".join(lines)


__all__ = [
    "CHANGE_WORDS",
    "DEFAULT_MAX_AFFECTED",
    "DEFAULT_MAX_CHANGED",
    "EXACTNESS_WORDS",
    "MAX_LISTED",
    "ChangeStory",
    "FindingRef",
    "SourceLinks",
    "StoryFinding",
    "StoryStep",
    "StoryVia",
    "attach_findings",
    "build_change_story",
    "findings_from_annotations",
    "findings_from_bot",
    "findings_on",
    "findings_words",
    "links_from_env",
    "more_count",
    "role_words",
    "via_words",
    "walkthrough",
]
