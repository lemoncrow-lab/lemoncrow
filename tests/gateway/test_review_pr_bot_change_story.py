"""The Change Story in the pull request summary (C04d) and the exactness of the diagram's edges (P3.4).

The Python port of the Review's ordering (``frontend/src/review/changeStory/changeStoryModel.ts``) is held to the same
shared fixture as the TypeScript model (``tests/fixtures/change_story/ordering.json``, read by
``frontend/src/review/changeStory/changeStoryParity.test.ts``), so the two cannot drift apart.
"""

from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import change_story, change_story_svg
from lemoncrow.pro.capabilities.review.pr_bot import diagram as diagram_module
from lemoncrow.pro.capabilities.review.pr_bot.change_story import (
    ChangeStory,
    FindingRef,
    SourceLinks,
    build_change_story,
    findings_from_annotations,
    links_from_env,
    walkthrough,
)
from lemoncrow.pro.capabilities.review.pr_bot.diagram import digest_of, draw, resolve, reuse, with_findings
from tests.gateway.pr_bot_support import BASE, build_world
from tests.gateway.test_review_pr_bot_diagram import CALLS_AUTH, PAD, block_of, delta_of

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/change_story/ordering.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
HEAD = "a" * 40
BASE_SHA = "b" * 40
REPO = "https://github.com/acme/widgets"
LINKS = SourceLinks(REPO, HEAD, BASE_SHA)


def story_of_case(case: Mapping[str, Any]) -> ChangeStory:
    given = case["input"]
    findings = None if given["findings"] is None else findings_from_annotations(given["findings"])
    return build_change_story(
        given["graph"],
        flow=given["flow"],
        findings=findings,
        max_changed=given.get("maxChanged", change_story.DEFAULT_MAX_CHANGED),
        max_affected=given.get("maxAffected", change_story.DEFAULT_MAX_AFFECTED),
    )


def as_fixture(story: ChangeStory) -> dict[str, Any]:
    """*story* in the fixture's own shape, the one the TypeScript test compares too."""

    return {
        "ordering": story.ordering,
        "changedTotal": story.changed_total,
        "omittedChanged": story.omitted_changed,
        "affectedTotal": story.affected_total,
        "omittedAffected": story.omitted_affected,
        "findingsKnown": story.findings_known,
        "steps": [
            {
                "nodeId": step.node_id,
                "role": step.role,
                "change": step.change,
                "exactness": step.exactness,
                "side": step.side,
                "line": step.line,
                "via": (
                    None
                    if step.via is None
                    else {
                        "edgeId": step.via.edge_id,
                        "fromId": step.via.from_id,
                        "toId": step.via.to_id,
                        "exactness": step.via.exactness,
                        "change": step.via.change,
                    }
                ),
                "findings": [{"id": row.id, "on": row.on} for row in step.findings],
            }
            for step in story.steps
        ],
    }


# --------------------------------------------------------------------------- #
# the ordering: one fixture, two implementations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_python_ordering_is_the_reviews_ordering_on_the_shared_fixture(case: Mapping[str, Any]) -> None:
    assert as_fixture(story_of_case(case)) == case["expected"]


def test_the_fixture_covers_every_ordering_rule_it_claims_to() -> None:
    """A fixture that silently lost a case would let the two models drift in exactly that case."""

    stories = [story_of_case(case) for case in CASES]
    assert {story.ordering for story in stories} == {"flow", "dependencies"}
    roles = {step.role for story in stories for step in story.steps}
    assert roles == {"entry", "changed", "dependency", "affected"}
    exactness = {step.via.exactness for story in stories for step in story.steps if step.via is not None}
    assert {"snapshot", "observed", "current_index"} <= exactness
    assert any(story.omitted_changed and story.omitted_affected for story in stories), "both limits are exercised"
    assert any(not story.findings_known for story in stories), "a run with no findings to say"


# --------------------------------------------------------------------------- #
# the walkthrough
# --------------------------------------------------------------------------- #


def test_the_walkthrough_walks_the_change_in_the_reviews_order_and_counts_the_findings_on_each_step() -> None:
    text = walkthrough(story_of_case(CASES[0]))
    assert text.split("\n") == [
        "**Change Story**",
        "",
        "<sub>Ordered by recorded dependencies between changed files, dependents first. This is not a runtime order."
        "</sub>",
        "",
        "1. `api/auth.py` · entry point · changed · 1 P0 finding · 1 P1 finding",
        "2. → `session.py` (imports, in this commit) · changed · 2 P1 findings",
        "3. → `store.py` (references, in this commit) · relied on, unchanged",
        "4. → `legacy.py` (session.py references it, in this commit) · removed",
        "5. `core/config.py` · entry point · added",
        "6. `tests/test_auth.py` · entry point · changed",
        "7. ← `views.py` (imports auth.py, in this commit) · affected caller · unchanged",
        "8. ← `main.py` (calls session.py, from the current index, not this commit) · affected caller · context only",
    ]


def test_a_run_that_does_not_know_its_findings_says_nothing_about_them() -> None:
    """No count is not "0 findings": an absence would prove nothing."""

    given = CASES[0]["input"]
    story = build_change_story(given["graph"], flow=given["flow"], findings=None)
    assert "finding" not in walkthrough(story)


def test_an_observed_step_says_it_was_seen_in_a_recorded_run_and_the_flow_order_says_so() -> None:
    text = walkthrough(story_of_case(CASES[1]))
    assert "2. → `c.py` (references, observed in a recorded run) · changed" in text
    assert "1 step matched a recorded run; the rest are not a runtime order." in text


def test_the_walkthrough_stops_at_eight_steps_and_counts_the_rest_in_the_review() -> None:
    nodes = [
        {
            "id": f"file:src/m{index:02}.py",
            "kind": "file",
            "path": f"src/m{index:02}.py",
            "status": "changed",
            "exactness": "snapshot",
        }
        for index in range(11)
    ]
    story = build_change_story({"nodes": nodes, "edges": []}, findings=None)
    lines = walkthrough(story).split("\n")
    numbered = [line for line in lines if re.match(r"\d+\. ", line)]
    assert [line.split(" ", 1)[0] for line in numbered] == [f"{n}." for n in range(1, 9)]
    assert lines[-1] == "<sub>3 more in the Review.</sub>"


def test_the_files_the_walk_did_not_reach_are_counted_with_the_rest() -> None:
    text = walkthrough(story_of_case(CASES[2]))
    assert text.endswith("<sub>4 more in the Review.</sub>")


def test_a_story_of_one_step_is_not_a_walkthrough() -> None:
    nodes = [{"id": "file:a.py", "kind": "file", "path": "a.py", "status": "changed", "exactness": "snapshot"}]
    assert walkthrough(build_change_story({"nodes": nodes, "edges": []})) == ""


# --------------------------------------------------------------------------- #
# source links
# --------------------------------------------------------------------------- #


def test_every_snapshot_step_links_to_its_line_at_the_head_and_a_removed_file_at_the_merge_base() -> None:
    text = walkthrough(story_of_case(CASES[0]), links=LINKS)
    assert f"1. [`api/auth.py`]({REPO}/blob/{HEAD}/api/auth.py#L12) · entry point" in text
    assert f"[`store.py`]({REPO}/blob/{HEAD}/db/store.py) (references" in text, "no line, no anchor"
    assert f"[`legacy.py`]({REPO}/blob/{BASE_SHA}/auth/legacy.py#L7)" in text, "gone from the head"


def test_current_index_context_never_links_because_it_is_not_the_commit_under_review() -> None:
    text = walkthrough(story_of_case(CASES[0]), links=LINKS)
    assert "8. ← `main.py` (calls session.py" in text
    assert "cli/main.py" not in text


def test_a_path_is_percent_encoded_so_it_cannot_end_its_own_link() -> None:
    step = story_of_case(CASES[0]).steps[0]
    hostile = type(step)(**{**{f: getattr(step, f) for f in step.__slots__}, "path": "a b/x)](javascript:1)#?.py"})
    url = LINKS.url(hostile)
    assert url == f"{REPO}/blob/{HEAD}/a%20b/x%29%5D%28javascript%3A1%29%23%3F.py#L12"


@pytest.mark.parametrize(
    "links",
    [
        SourceLinks("javascript:alert(1)", HEAD),
        SourceLinks("http://github.com/acme/widgets", HEAD),
        SourceLinks("https://github.com/acme/widgets/../x", HEAD),
        SourceLinks("https://github.com/acme/widgets)", HEAD),
        SourceLinks(REPO, "not-a-sha"),
        SourceLinks(REPO, HEAD, ""),  # a removed file with no merge base to point at
    ],
)
def test_a_link_that_cannot_be_proven_safe_is_left_out(links: SourceLinks) -> None:
    story = story_of_case(CASES[0])
    removed = next(step for step in story.steps if step.change == "removed")
    assert links.url(removed) == ""
    if links.repo_url != REPO:
        assert all(links.url(step) == "" for step in story.steps)


def test_a_github_actions_run_links_to_its_own_repository() -> None:
    env = {"GITHUB_SERVER_URL": "https://ghe.example.com/", "GITHUB_REPOSITORY": "acme/widgets"}
    assert links_from_env(env, head_sha=HEAD, base_sha=BASE_SHA) == SourceLinks(
        "https://ghe.example.com/acme/widgets", HEAD, BASE_SHA
    )
    assert links_from_env({"GITHUB_REPOSITORY": "acme/widgets"}, head_sha=HEAD) == SourceLinks(REPO, HEAD)
    assert links_from_env({}, head_sha=HEAD) is None
    assert links_from_env({"GITHUB_REPOSITORY": "acme/wid gets"}, head_sha=HEAD) is None


@pytest.mark.parametrize(
    "name",
    [
        "x](https://evil.example)",
        "![img](https://evil.example/a.png)",
        "@octocat",
        "`code`",
        "```",
        "<details>",
        "<!-- /lemoncrow:diagram -->",
    ],
)
def test_a_hostile_file_name_cannot_leave_its_code_span(name: str) -> None:
    nodes = [
        {"id": "file:a.py", "kind": "file", "path": "src/a.py", "status": "changed", "exactness": "snapshot"},
        {"id": "file:b", "kind": "file", "path": f"src/{name}.py", "status": "changed", "exactness": "snapshot"},
    ]
    text = walkthrough(build_change_story({"nodes": nodes, "edges": []}), links=LINKS)
    (line,) = [line for line in text.split("\n") if line.startswith("2. ")]
    for raw in ("](https://evil", "![", "@octocat", "```", "<!-- /lemoncrow"):
        assert raw not in line, line
    assert line.count("`") == 2, "one code span, opened and closed"
    assert "/blob/" + HEAD + "/src/" in line and ")](" not in line.split("](", 1)[1]


# --------------------------------------------------------------------------- #
# P3.4: the exactness of every edge in the delta diagram
# --------------------------------------------------------------------------- #


def _with_edge(delta: Mapping[str, Any], source: str, target: str, *, exactness: str, status: str) -> dict[str, Any]:
    out = copy.deepcopy(dict(delta))
    ids = {node["path"]: node["id"] for node in out["graph"]["nodes"] if node.get("path")}
    out["graph"]["edges"].append(
        {
            "id": f"extra:{source}->{target}",
            "source": ids[source],
            "target": ids[target],
            "source_path": source,
            "target_path": target,
            "kind": "calls",
            "exactness": exactness,
            "delta_status": status,
            "weight": 1,
            "evidence": [],
        }
    )
    return out


def _two_added() -> dict[str, Any]:
    return delta_of(
        after=[("src/a.py", "src/b.py"), ("src/c.py", "src/b.py")],
        modified=("src/a.py", "src/b.py", "src/c.py"),
    )


def _section(delta: Mapping[str, Any]) -> str:
    drawn = draw(delta)
    assert drawn is not None
    return drawn.section


def test_a_current_index_relationship_between_drawn_files_is_dotted_dashed_and_labelled() -> None:
    section = _section(_with_edge(_two_added(), "src/a.py", "src/c.py", exactness="current_index", status="unchanged"))
    drawn = block_of(section)  # checks dotted == labelled == dashed for every edge
    assert drawn.edge("src/a.py", "src/c.py")[1:3] == ("-.->", "calls #183; current index")
    assert "dashed ones are context it did not compare" in section
    assert "dashed: inferred or from the current index, not this commit" in section


def test_an_inferred_relationship_the_delta_calls_changed_is_still_only_context() -> None:
    """Only the comparison says a relationship changed: an inferred "added" is drawn dotted and never green."""

    section = _section(_with_edge(_two_added(), "src/a.py", "src/c.py", exactness="inferred", status="added"))
    drawn = block_of(section)
    assert drawn.edge("src/a.py", "src/c.py")[1:3] == ("-.->", "calls #183; change unknown #183; inferred")
    assert "2 added" in section, "the count is of the comparison's own relationships"


def test_an_observed_relationship_is_solid_and_says_it_was_observed() -> None:
    delta = _two_added()
    edge = next(row for row in delta["graph"]["edges"] if row["source_path"] == "src/a.py")
    delta = {
        **delta,
        "flow": {"applicable": True, "steps": [{"edge_id": edge["id"], "grounding": "observed", "trace": "t1"}]},
    }
    drawn = block_of(_section(delta))
    assert drawn.edge("src/a.py", "src/b.py")[1:3] == ("-->", "+b #183; observed")
    assert drawn.edge("src/c.py", "src/b.py")[1:3] == ("-->", "+b")


def test_an_unchanged_snapshot_relationship_between_drawn_files_is_a_thin_grey_solid_line() -> None:
    section = _section(_with_edge(_two_added(), "src/a.py", "src/c.py", exactness="snapshot", status="unchanged"))
    drawn = block_of(section)
    index = drawn.edges.index(drawn.edge("src/a.py", "src/c.py"))
    assert drawn.edges[index][1:3] == ("-->", "calls")
    (style,) = [line for line in drawn.text.split("\n") if line.strip().startswith("linkStyle") and "#8c959f" in line]
    assert style.strip() == f"linkStyle {index} stroke:#8c959f,stroke-width:1px"
    assert "nothing here is inferred" in section


def test_context_never_draws_a_new_file_or_doubles_a_changed_arrow() -> None:
    base = _two_added()
    outside = _with_edge(base, "src/a.py", "src/b.py", exactness="current_index", status="unchanged")
    assert block_of(_section(outside)).edges == block_of(_section(base)).edges


# --------------------------------------------------------------------------- #
# the section: findings, digest, reuse, resolve
# --------------------------------------------------------------------------- #


def _story_delta() -> dict[str, Any]:
    given = CASES[0]["input"]
    return {"status": "ready", "graph": given["graph"], "flow": given["flow"]}


def test_the_section_is_the_diagram_its_legend_then_the_walkthrough() -> None:
    drawn = draw(_story_delta(), links=LINKS)
    assert drawn is not None and drawn.story is not None
    section = drawn.section
    order = [section.index(mark) for mark in ("```mermaid", "solid: in this commit", "**Change Story**")]
    assert order == sorted(order)
    assert section.endswith(diagram_module.SECTION_CLOSE)
    assert drawn.state == digest_of(section)


def test_findings_known_later_redo_the_walkthrough_and_its_digest_and_the_section_still_reuses(
    tmp_path: Path,
) -> None:
    drawn = draw(_story_delta(), links=LINKS)
    assert drawn is not None
    finding = FindingRef("f9", "p1", "Session leak", "auth/session.py", 3, 3, "head")
    target = tmp_path / "story.svg"
    later = with_findings(drawn, [finding], environ={change_story_svg.ARTIFACT_ENV: str(target)})
    assert "2. → [`session.py`]" in later.section and "· changed · 1 P1 finding" in later.section
    assert later.state != drawn.state and later.state == digest_of(later.section)
    body = f"## Review\n\n{later.section}\n\n<sub>receipt</sub>"
    assert reuse(body, later.state) == later.section
    assert target.read_text(encoding="utf-8") == change_story_svg.render_svg(later.story)  # type: ignore[arg-type]


def test_a_reused_section_is_kept_as_it_is() -> None:
    kept = diagram_module.Diagram(section="### Diagram\n\n<!-- /lemoncrow:diagram -->", state="3:none", reused=True)
    assert with_findings(kept, [FindingRef("f", "p0", "t", "a.py", 1, 1, "head")]) is kept


def test_resolve_links_a_github_actions_run_to_its_repository_and_writes_the_picture(tmp_path: Path) -> None:
    target = tmp_path / "out" / "story.svg"
    env = {"GITHUB_REPOSITORY": "acme/widgets", change_story_svg.ARTIFACT_ENV: str(target)}
    drawn = resolve(
        "auto",
        previous=None,
        existing_body="",
        head_sha=HEAD,
        merge_base_sha=BASE_SHA,
        compute=lambda: (_story_delta(), ""),
        environ=env,
    )
    assert f"[`api/auth.py`]({REPO}/blob/{HEAD}/api/auth.py#L12)" in drawn.section
    assert f"/blob/{BASE_SHA}/auth/legacy.py#L7" in drawn.section
    assert target.is_file()
    plain = resolve(
        "auto",
        previous=None,
        existing_body="",
        head_sha=HEAD,
        merge_base_sha=BASE_SHA,
        compute=lambda: (_story_delta(), ""),
        environ={},
    )
    assert "/blob/" not in plain.section and "1. `api/auth.py` · entry point" in plain.section


# --------------------------------------------------------------------------- #
# the SVG
# --------------------------------------------------------------------------- #

_SVG_NS = "{http://www.w3.org/2000/svg}"


def test_the_svg_is_byte_identical_for_the_same_story_and_well_formed() -> None:
    first = change_story_svg.render_svg(story_of_case(CASES[0]))
    assert first == change_story_svg.render_svg(story_of_case(CASES[0]))
    root = ET.fromstring(first)
    assert root.tag == f"{_SVG_NS}svg" and root.get("role") == "img"
    assert root.find(f"{_SVG_NS}title") is not None and root.find(f"{_SVG_NS}desc") is not None


def test_the_svg_loads_nothing_and_runs_nothing() -> None:
    svg = change_story_svg.render_svg(story_of_case(CASES[0]))
    assert re.findall(r"https?://[^\"\s]+", svg) == ["http://www.w3.org/2000/svg"]
    for banned in ("<script", "href", "@import", "@font-face", "url(http", "<image", "<foreignObject", "on" + "load="):
        assert banned not in svg
    assert "prefers-color-scheme:dark" in svg and 'class="bg"' in svg, "its own background in both themes"


def test_an_arc_that_is_not_a_fact_of_this_commit_is_dashed_and_every_recorded_one_is_solid() -> None:
    story = story_of_case(CASES[0])
    root = ET.fromstring(change_story_svg.render_svg(story))
    arcs = [node for node in root.iter(f"{_SVG_NS}path") if "e" in (node.get("class") or "").split()]
    drawn = {
        (step.via.from_id, step.via.to_id): step.via.recorded
        for step in story.steps
        if step.via is not None and {step.via.from_id, step.via.to_id} <= {other.node_id for other in story.steps}
    }
    assert len(arcs) == len(drawn) == 5
    dashed = ["dash" in (node.get("class") or "").split() for node in arcs]
    assert sorted(dashed) == sorted(not recorded for recorded in drawn.values())
    assert any(dashed), "main.py is reached by a current-index relationship"


def test_the_svg_draws_at_most_twelve_rows_and_counts_the_rest() -> None:
    nodes = [
        {
            "id": f"file:src/m{index:02}.py",
            "kind": "file",
            "path": f"src/m{index:02}.py",
            "status": "changed",
            "exactness": "snapshot",
        }
        for index in range(15)
    ]
    svg = change_story_svg.render_svg(build_change_story({"nodes": nodes, "edges": []}))
    root = ET.fromstring(svg)
    rows = [node for node in root.iter(f"{_SVG_NS}g") if (node.get("id") or "").startswith("step-")]
    assert len(rows) == change_story_svg.MAX_NODES == 12
    assert ">3 more in the Review.</text>" in svg


def test_hostile_text_in_the_svg_is_character_data() -> None:
    name = "<script>alert(1)</script>&\"'‮\u0007.py"
    nodes = [
        {"id": "file:a", "kind": "file", "path": "src/a.py", "status": "changed", "exactness": "snapshot"},
        {"id": "file:b", "kind": "file", "path": f"src/{name}", "status": "changed", "exactness": "snapshot"},
    ]
    svg = change_story_svg.render_svg(build_change_story({"nodes": nodes, "edges": []}))
    root = ET.fromstring(svg)
    assert "<script" not in svg and "‮" not in svg and "\u0007" not in svg
    assert root.find(f".//{_SVG_NS}script") is None
    texts = ["".join(node.itertext()) for node in root.iter(f"{_SVG_NS}text")]
    assert any("<script>alert(1)</script>&\"'??.py" in text for text in texts)


def test_the_picture_is_written_only_where_the_workflow_asks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    story = story_of_case(CASES[0])
    monkeypatch.delenv(change_story_svg.ARTIFACT_ENV, raising=False)
    assert change_story_svg.write_artifact(story) is None
    assert change_story_svg.write_artifact(story, {change_story_svg.ARTIFACT_ENV: "  "}) is None
    target = tmp_path / "deep" / "story.svg"
    assert change_story_svg.write_artifact(story, {change_story_svg.ARTIFACT_ENV: str(target)}) == target
    assert target.read_text(encoding="utf-8") == change_story_svg.render_svg(story)
    blocked = tmp_path / "file"
    blocked.write_text("x")
    assert change_story_svg.write_artifact(story, {change_story_svg.ARTIFACT_ENV: str(blocked / "x.svg")}) is None


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #


def test_a_pull_request_summary_walks_its_change_under_the_diagram_with_links_and_an_svg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    target = tmp_path / "artifact" / "change-story.svg"
    monkeypatch.setenv(change_story_svg.ARTIFACT_ENV, str(target))
    world.origin.push({"src/cache.py": CALLS_AUTH, **PAD})
    report = world.review()

    assert report.state is not None and "**Change Story**" in report.summary
    head = report.state.head_sha
    assert f". [`src/cache.py`]({REPO}/blob/{head}/src/cache.py) · entry point · changed" in report.summary
    assert re.search(r"\n\d\. → \[`auth\.py`\]\(" + re.escape(f"{REPO}/blob/{head}/src/auth.py"), report.summary)
    assert report.summary.index("**Change Story**") < report.summary.index(diagram_module.SECTION_CLOSE)
    assert ET.fromstring(target.read_text(encoding="utf-8")).tag == f"{_SVG_NS}svg"


def test_the_walkthrough_counts_the_runs_findings_and_follows_a_model_diagram_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, BASE)
    target = tmp_path / "artifact" / "change-story.svg"
    monkeypatch.setenv(change_story_svg.ARTIFACT_ENV, str(target))
    world.model.severity["leaks the cache"] = "high"
    world.model.overview = {
        "one_line": "Guards the cache with authorize",
        "intro": "The cache now asks auth first.",
        "bullets": ["remember calls authorize"],
        "diagram": {
            "kind": "flowchart",
            "nodes": [{"id": "a", "name": "src/cache.py"}, {"id": "b", "name": "src/plain.py"}],
            "edges": [{"from": "a", "to": "b", "label": "calls"}],
        },
    }
    buggy = CALLS_AUTH + "    return CACHE  # BUG: leaks the cache\n"
    world.origin.push({"src/cache.py": buggy, **PAD})
    report = world.review()

    head = world.origin.head
    assert "Sketched by the model from the diff" in report.summary, "the model's diagram is the one drawn"
    assert "**Change Story**" in report.summary
    step = next(line for line in report.summary.splitlines() if f"/blob/{head}/src/cache.py)" in line)
    assert "· 1 P1 finding" in step, step
    assert "P1 finding" in target.read_text(encoding="utf-8"), "the artifact is written with the run's findings"
