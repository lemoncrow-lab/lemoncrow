"""The overview: one cheap model call per review that says what the pull request does, in structure LemonCrow checks and draws.

The pure parts (what the model may return, what is kept, how the diagram is escaped and drawn) are tested on their own;
the end-to-end parts run the whole bot on a real git origin with a fake GitHub and a fake model, and check what the call
costs, when it is not made, what happens when it fails, and that nothing it returned reaches the pull request as markup.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import overview as overview_module
from lemoncrow.pro.capabilities.review.pr_bot import overview_diagram
from lemoncrow.pro.capabilities.review.pr_bot.diagram import SEQUENCE_INIT, THEME_INIT
from lemoncrow.pro.capabilities.review.pr_bot.overview import (
    ACTORS,
    MAX_BULLETS,
    MAX_NODES,
    OVERVIEW_SCHEMA,
    DiagramSpec,
    Edge,
    Node,
    Overview,
    Request,
    Vocabulary,
    build,
    parse,
    prompt,
    trivial,
)
from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state, strip_state
from tests.gateway.pr_bot_noise_support import CONFIG, Recorder, clear_noise_env, review_yaml
from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, FIRST_PUSH, World, build_world

VOCABULARY = Vocabulary(
    files=frozenset({"src/auth.py", "src/cache.py", "web/app/index.ts", "api/app/index.ts"}),
    symbols={"authorize": "src/auth.py", "remember": "src/cache.py"},
)


def proposal(**fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "one_line": "Adds a guard to authorize",
        "intro": "The change touches the auth and cache modules.",
        "bullets": ["authorize rejects a missing user", "remember stores a list"],
        "diagram": None,
    }
    values.update(fields)
    return values


def flow(nodes: list[tuple[str, str]], edges: list[tuple[str, str, str]], kind: str = "flowchart") -> dict[str, Any]:
    return {
        "kind": kind,
        "nodes": [{"id": identifier, "name": name} for identifier, name in nodes],
        "edges": [{"from": a, "to": b, "label": label} for a, b, label in edges],
    }


# --------------------------------------------------------------------------- #
# what is kept of what the model returned
# --------------------------------------------------------------------------- #


def test_the_schema_asks_for_exactly_the_four_fields_and_a_diagram_that_may_be_null() -> None:
    assert set(OVERVIEW_SCHEMA["properties"]) == {"one_line", "intro", "bullets", "diagram"}
    assert set(OVERVIEW_SCHEMA["required"]) == set(OVERVIEW_SCHEMA["properties"])
    assert OVERVIEW_SCHEMA["additionalProperties"] is False
    assert {"type": "null"} in OVERVIEW_SCHEMA["properties"]["diagram"]["anyOf"]


def test_text_is_one_line_within_its_bounds_with_control_characters_gone() -> None:
    found = parse(
        proposal(
            one_line="Adds\na guard\x00 to‮ authorize " + "x" * 400,
            intro="  The   change\ttouches things.  ",
            bullets=[f"bullet {n}" for n in range(9)] + ["", 5, None],
        ),
        VOCABULARY,
    )
    assert found is not None
    assert "\n" not in found.one_line and "\x00" not in found.one_line and "‮" not in found.one_line
    assert len(found.one_line) <= overview_module.MAX_ONE_LINE and found.one_line.endswith("…")
    assert found.intro == "The change touches things."
    assert found.bullets == tuple(f"bullet {n}" for n in range(MAX_BULLETS))


def test_a_sentence_that_is_too_long_ends_on_a_whole_word_and_the_prompt_asks_for_less_than_the_cut() -> None:
    long = "Adds a client call function with authorization and retry loop, extends authorize with a token and logging, and changes cache storage to a list per key"
    found = parse(proposal(one_line=long), VOCABULARY)
    assert found is not None and found.one_line.endswith("…") and len(found.one_line) <= overview_module.MAX_ONE_LINE
    assert long.startswith(found.one_line[:-1]) and long[len(found.one_line) - 1] == " ", "cut between two words"
    assert overview_module.ASK_ONE_LINE < overview_module.MAX_ONE_LINE


@pytest.mark.parametrize(
    "sentence",
    [
        "This change is safe to merge",
        "Looks good to me, LGTM",
        "No issues found in the new retry loop",
        "Ready to merge after review",
        "Confidence score 5/5 for the change",
        "Approved by the team",
        "It is production-ready",
        "Rated 4 / 5 overall",
    ],
)
def test_a_sentence_that_reads_as_a_verdict_is_dropped_because_the_verdict_is_not_the_models(sentence: str) -> None:
    found = parse(proposal(one_line=sentence, intro=sentence, bullets=[sentence, "Adds a retry loop"]), VOCABULARY)
    assert found is not None
    assert (found.one_line, found.intro, found.bullets) == ("", "", ("Adds a retry loop",))


def test_an_answer_with_nothing_usable_is_no_overview() -> None:
    assert parse(proposal(one_line="", intro="", bullets=[], diagram=None), VOCABULARY) is None
    assert parse(proposal(one_line="LGTM", intro="Approved", bullets=["safe to merge"]), VOCABULARY) is None
    assert parse({}, VOCABULARY) is None


# --------------------------------------------------------------------------- #
# the diagram: every node names something real, or it is dropped
# --------------------------------------------------------------------------- #


def diagram_of(raw: dict[str, Any]) -> DiagramSpec | None:
    found = parse(proposal(diagram=raw), VOCABULARY)
    return None if found is None else found.diagram


def test_a_node_may_name_a_changed_file_a_changed_symbol_or_a_generic_actor_and_nothing_else() -> None:
    spec = diagram_of(
        flow(
            [
                ("a", "src/auth.py"),
                ("b", "authorize"),
                ("c", "user"),
                ("d", "Kubernetes"),
                ("e", "src/never_changed.py"),
            ],
            [("a", "b", "defines"), ("c", "a", "logs in"), ("a", "d", "deploys"), ("e", "a", "imports")],
        )
    )
    assert spec is not None
    assert [(node.name, node.kind) for node in spec.nodes] == [
        ("src/auth.py", "file"),
        ("authorize", "symbol"),
        ("User", "actor"),
    ]
    assert [(edge.source, edge.target) for edge in spec.edges] == [
        ("a", "b"),
        ("c", "a"),
    ], "edges to dropped nodes went with them"


def test_a_file_may_be_named_by_its_base_name_only_when_that_is_unambiguous() -> None:
    spec = diagram_of(
        flow([("a", "auth.py"), ("b", "index.ts"), ("c", "cache.py")], [("a", "c", "uses"), ("b", "a", "bad")])
    )
    assert spec is not None
    names = [node.name for node in spec.nodes]
    assert names == ["src/auth.py", "src/cache.py"], "index.ts is two files, so it names neither"


def test_the_actors_are_a_short_fixed_list_with_one_spelling_each() -> None:
    assert set(ACTORS.values()) == {
        "User", "Client", "Browser", "Server", "API", "Backend", "Frontend", "Database", "Cache", "Queue",
        "External API", "CLI", "Worker", "Scheduler",
    }  # fmt: skip
    spec = diagram_of(
        flow([("a", "DB"), ("b", "external api"), ("c", "src/auth.py")], [("c", "a", "reads"), ("c", "b", "calls")])
    )
    assert spec is not None and [node.name for node in spec.nodes] == ["Database", "External API", "src/auth.py"]


def test_two_ids_for_one_thing_are_one_node_and_a_self_loop_is_no_edge() -> None:
    spec = diagram_of(
        flow(
            [("a", "src/auth.py"), ("b", "src/auth.py"), ("c", "authorize")],
            [("a", "c", "has"), ("b", "c", "has"), ("a", "b", "me")],
        )
    )
    assert spec is not None and len(spec.nodes) == 2 and len(spec.edges) == 1


def test_a_diagram_needs_two_nodes_and_an_edge_after_the_dropping_or_there_is_none() -> None:
    assert diagram_of(flow([("a", "src/auth.py"), ("b", "Nonsense")], [("a", "b", "x")])) is None
    assert diagram_of(flow([("a", "src/auth.py"), ("b", "authorize")], [])) is None
    assert diagram_of(flow([("a", "src/auth.py")], [("a", "a", "x")])) is None
    assert diagram_of({"kind": "pie", "nodes": [], "edges": []}) is None
    assert diagram_of({"kind": "flowchart"}) is None
    assert diagram_of("flowchart LR; A-->B") is None  # type: ignore[arg-type]


def test_a_node_with_no_edge_is_not_drawn() -> None:
    spec = diagram_of(flow([("a", "src/auth.py"), ("b", "authorize"), ("c", "src/cache.py")], [("a", "b", "has")]))
    assert spec is not None and [node.name for node in spec.nodes] == ["src/auth.py", "authorize"]


def test_the_diagram_has_at_most_twelve_nodes() -> None:
    files = {f"src/m{n}.py" for n in range(30)}
    vocabulary = Vocabulary(files=frozenset(files), symbols={})
    nodes = [(f"n{n}", f"src/m{n}.py") for n in range(30)]
    edges = [(f"n{n}", f"n{n + 1}", "calls") for n in range(29)]
    found = parse(proposal(diagram=flow(nodes, edges)), vocabulary)
    assert found is not None and found.diagram is not None
    assert len(found.diagram.nodes) <= MAX_NODES == 12 and len(found.diagram.edges) <= overview_module.MAX_EDGES


def test_a_node_id_that_is_not_a_short_word_drops_the_node() -> None:
    spec = diagram_of(
        flow(
            [("a", "src/auth.py"), ("b b", "authorize"), ("c\n", "src/cache.py"), ("d", "src/cache.py")],
            [("a", "d", "x"), ("a", "b b", "y")],
        )
    )
    assert spec is not None and [node.name for node in spec.nodes] == ["src/auth.py", "src/cache.py"]


def test_a_sequence_is_a_list_of_messages_in_order_and_a_message_says_something() -> None:
    spec = diagram_of(
        flow(
            [("u", "User"), ("a", "src/auth.py"), ("d", "Database")],
            [("u", "a", "logs in"), ("a", "d", ""), ("a", "u", "token")],
            kind="sequence",
        )
    )
    assert spec is not None and spec.kind == "sequence"
    assert [edge.label for edge in spec.edges] == ["logs in", "token"], "an empty message is dropped, the order is kept"


def test_an_edge_label_that_reads_as_a_verdict_is_blanked_not_drawn() -> None:
    spec = diagram_of(flow([("a", "src/auth.py"), ("b", "authorize")], [("a", "b", "safe to merge")]))
    assert spec is not None and spec.edges[0].label == ""


# --------------------------------------------------------------------------- #
# drawing it: LemonCrow writes the Mermaid
# --------------------------------------------------------------------------- #

_NODE = re.compile(r'^n\d+(?:\["[^"]*"\]|\("[^"]*"\)|\(\["[^"]*"\]\))$')
_EDGE = re.compile(r'^n\d+ -->(?:\|"[^"]*"\|)? n\d+$')
_PARTICIPANT = re.compile(r"^participant n\d+ as [^\n]*$")
_MESSAGE = re.compile(r"^n\d+->>n\d+: [^\n]*$")
_ENTITY = re.compile(r"#(\d+);")


def mermaid_lines(spec: DiagramSpec) -> list[str]:
    text = overview_diagram.mermaid(spec)
    lines = text.split("\n")
    expected = SEQUENCE_INIT if lines[1] == "sequenceDiagram" else THEME_INIT
    assert lines[0] == expected, "the neutral theme directive comes first"
    return [line.strip() for line in lines[1:]]


def test_a_flowchart_is_drawn_with_counter_ids_and_a_shape_for_what_a_node_names() -> None:
    spec = diagram_of(
        flow(
            [("x", "src/auth.py"), ("y", "authorize"), ("z", "User")],
            [("z", "x", "calls"), ("x", "y", "defines"), ("y", "z", "")],
        )
    )
    assert spec is not None
    lines = mermaid_lines(spec)
    assert lines[0] == "flowchart TD"
    assert lines[1:4] == ['n1["auth.py"]', 'n2("authorize")', 'n3(["User"])']
    for line in lines[1:]:
        assert _NODE.match(line) or _EDGE.match(line), line
    assert 'n3 -->|"calls"| n1' in lines and "n2 --> n3" in lines


def test_a_sequence_diagram_has_participants_then_messages() -> None:
    spec = diagram_of(
        flow([("u", "User"), ("a", "src/auth.py")], [("u", "a", "logs in"), ("a", "u", "token")], kind="sequence")
    )
    assert spec is not None
    lines = mermaid_lines(spec)
    assert lines[0] == "sequenceDiagram"
    assert lines[1:] == ["participant n1 as User", "participant n2 as auth.py", "n1->>n2: logs in", "n2->>n1: token"]
    assert all(_PARTICIPANT.match(row) or _MESSAGE.match(row) for row in lines[1:])


def test_a_sequence_diagram_greys_its_text_so_it_reads_on_the_dark_page_and_a_flowchart_does_not_need_it() -> None:
    """The neutral theme draws message text straight on the page in near-black: unreadable on GitHub's dark page."""

    nodes = [("u", "User"), ("a", "src/auth.py")]
    sequence = diagram_of(flow(nodes, [("u", "a", "x")], kind="sequence"))
    flowchart = diagram_of(flow(nodes, [("u", "a", "x")]))
    assert sequence is not None and flowchart is not None
    drawn_sequence, drawn_flowchart = overview_diagram.mermaid(sequence), overview_diagram.mermaid(flowchart)
    assert drawn_sequence.startswith(SEQUENCE_INIT + "\nsequenceDiagram")
    assert "'signalTextColor': '#6e7781'" in drawn_sequence and "'theme': 'neutral'" in SEQUENCE_INIT
    assert drawn_flowchart.startswith(THEME_INIT + "\nflowchart TD") and "themeVariables" not in drawn_flowchart


def test_a_model_label_that_would_be_markdown_is_drawn_as_text() -> None:
    """Mermaid 11 runs a label through Markdown: a list marker draws an error and ``__init__`` draws a bold "init"."""

    vocabulary = Vocabulary(files=frozenset({"src/__init__.py"}), symbols={})
    nodes = [("a", "src/__init__.py"), ("u", "User")]
    found = parse(proposal(diagram=flow(nodes, [("u", "a", "1. loads"), ("a", "u", "- answers")])), vocabulary)
    assert found is not None and found.diagram is not None
    lines = [line.strip() for line in overview_diagram.mermaid(found.diagram).split("\n")[2:]]
    assert lines[0] == 'n1["#95;#95;init#95;#95;.py"]'
    assert lines[2:] == ['n2 -->|"#49;. loads"| n1', 'n1 -->|"#45; answers"| n2']


def test_two_files_with_the_same_name_are_told_apart_by_their_paths() -> None:
    spec = diagram_of(flow([("a", "web/app/index.ts"), ("b", "api/app/index.ts")], [("a", "b", "fetches")]))
    assert spec is not None
    assert 'n1["web/app/index.ts"]' in mermaid_lines(spec) and 'n2["api/app/index.ts"]' in mermaid_lines(spec)


@pytest.mark.parametrize(
    "label",
    [
        'x"]; click n1 "http://evil.example',
        "a|b",
        "`tick`",
        "<img src=x onerror=1>",
        "@alice",
        "50% #1; done",
        "line\nbreak",
        "end",
    ],
)
def test_nothing_in_an_edge_label_can_leave_its_label_or_start_anything(label: str) -> None:
    spec = DiagramSpec(
        "flowchart",
        (Node("a", "src/auth.py", "file"), Node("b", "authorize", "symbol")),
        (Edge("a", "b", label),),
    )
    lines = mermaid_lines(spec)
    (edge,) = [row for row in lines if "-->" in row]
    inside = edge.split('|"', 1)[1].rsplit('"|', 1)[0]
    assert not re.search(r"[\"\[\]<>|`@\\;%{}\n]", _ENTITY.sub("", inside)), inside
    assert _EDGE.match(edge), edge
    assert len([row for row in lines if row]) == 4, "no extra statement was smuggled in"


def test_a_sequence_message_is_escaped_the_same_way() -> None:
    spec = DiagramSpec(
        "sequence",
        (Node("a", "src/auth.py", "file"), Node("b", "Database", "actor")),
        (Edge("a", "b", "x; deleteAll() # @bob <b>"),),
    )
    (message,) = [row for row in mermaid_lines(spec) if "->>" in row]
    assert _MESSAGE.match(message) and not re.search(r"[<>@;]", _ENTITY.sub("", message.split(": ", 1)[1]))


def test_the_section_is_a_heading_an_open_mermaid_block_and_a_caption_that_says_what_it_is() -> None:
    spec = diagram_of(flow([("a", "src/auth.py"), ("b", "authorize")], [("a", "b", "defines")]))
    assert spec is not None
    drawn = overview_diagram.draw(spec)
    assert drawn.section.startswith("### Diagram\n\n```mermaid\n%%{init: {'theme': 'neutral'}}%%\nflowchart TD")
    assert "<details" not in drawn.section, "open by default, not folded"
    assert "not checked against the code" in drawn.section and drawn.section.endswith("<!-- /lemoncrow:diagram -->")
    assert drawn.state.startswith("2:") and drawn.kind == "flowchart"


# --------------------------------------------------------------------------- #
# the wire form carried in the hidden state
# --------------------------------------------------------------------------- #


def test_an_overview_round_trips_through_its_wire_form_and_a_forged_one_reads_as_none_or_less() -> None:
    spec = diagram_of(flow([("a", "src/auth.py"), ("b", "authorize")], [("a", "b", "defines")]))
    original = Overview("One", "Intro", ("a", "b"), spec)
    assert Overview.from_wire(original.to_wire()) == original
    assert Overview.from_wire("nope") is None and Overview.from_wire({}) is None
    forged = original.to_wire()
    forged["diagram"]["nodes"][0]["kind"] = "script"
    forged["diagram"]["edges"].append({"from": "a", "to": "zzz", "label": "x"})
    kept = Overview.from_wire(forged)
    assert kept is not None and kept.diagram is None, "a node of an unknown kind leaves no diagram"
    assert kept.bullets == ("a", "b")


def test_the_state_carries_the_overview_and_a_shrunk_state_gives_up_the_diagram_before_the_text() -> None:
    from tests.gateway.test_review_pr_bot import review_state

    spec = diagram_of(flow([("a", "src/auth.py"), ("b", "authorize")], [("a", "b", "defines")]))
    state = review_state(overview=Overview("One", "Intro", ("a",), spec), overview_for="a" * 40 + ":" + "b" * 40)
    from lemoncrow.pro.capabilities.review.pr_bot.state import MAX_DETAIL, dump_state, shrink

    back = parse_state(dump_state(state) + "\n")
    assert back is not None and back.overview == state.overview and back.overview_for == state.overview_for
    assert shrink(state, 2).overview == state.overview
    assert shrink(state, 3).overview == Overview("One", "Intro", ("a",), None)
    assert shrink(state, MAX_DETAIL).overview is None


# --------------------------------------------------------------------------- #
# what the model is shown
# --------------------------------------------------------------------------- #


def packet(files: list[dict[str, Any]], symbols: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"files": files, "symbols": symbols or []}


def file_row(path: str, additions: int = 5, deletions: int = 1, **fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "path": path,
        "status": "modified",
        "additions": additions,
        "deletions": deletions,
        "category": "production",
        "hunks": [{"patch": f"+ changed {path}"}],
    }
    values.update(fields)
    return values


def test_a_change_of_two_files_and_twenty_lines_is_trivial_and_a_third_file_or_line_is_not() -> None:
    assert trivial(packet([file_row("a.py", 10, 10)]))
    assert trivial(packet([file_row("a.py", 10, 5), file_row("b.py", 3, 2)]))
    assert not trivial(packet([file_row("a.py", 11, 10)]))
    assert not trivial(packet([file_row("a.py", 1, 1), file_row("b.py", 1, 1), file_row("c.py", 1, 1)]))
    assert trivial(packet([file_row("a.py", 1, 1), file_row("lock.json", 900, 900, category="generated")]))
    assert trivial(packet([]))


def test_the_prompt_never_names_or_shows_a_path_the_content_policy_keeps_from_the_model() -> None:
    rows = [file_row("src/auth.py"), file_row("secrets/prod.env", additions=40), file_row("src/cache.py")]
    shown = prompt(
        packet(rows, [{"file_path": "secrets/prod.env", "symbol_name": "leak", "kind": "function", "change": "added"}]),
        Request(title="Add auth"),
        allowed=lambda path: not path.startswith("secrets/"),
    )
    text = shown.text
    assert "secrets" not in text and "leak" not in text
    assert shown.shown == 2 and shown.withheld == 1
    assert shown.vocabulary.files == {"src/auth.py", "src/cache.py"}


def test_the_prompt_tags_the_authors_words_as_data_and_cuts_them_and_redacts_the_diff() -> None:
    long_description = "Fixes the thing. " * 400
    shown = prompt(
        packet([file_row("src/auth.py", hunks=[{"patch": "+ token = 'ghp_SECRETSECRETSECRET'"}])]),
        Request(title="T" * 500, description=long_description),
        allowed=lambda path: True,
        redact=lambda text: text.replace("ghp_SECRETSECRETSECRET", "<redacted-credential>"),
    )
    blocks = json.loads(shown.text)
    assert blocks["pull_request"]["authority"] == "data:pr_metadata"
    assert len(blocks["pull_request"]["value"]["title"]) == 200
    assert len(blocks["pull_request"]["value"]["description"]) == overview_module.MAX_DESCRIPTION_CHARS
    assert "ghp_SECRET" not in shown.text and "<redacted-credential>" in shown.text
    assert blocks["changed_files"]["authority"] == "data:reviewed_repository_source"
    assert blocks["actors"] == sorted(set(ACTORS.values()))
    assert "never judge" in shown.system.lower() or "do not say whether it is correct" in shown.system


def test_the_prompt_is_bounded_whatever_the_size_of_the_change() -> None:
    rows = [file_row(f"src/m{n}.py", 100, 100, hunks=[{"patch": "+x" * 5000}]) for n in range(200)]
    shown = prompt(packet(rows), Request(), allowed=lambda path: True)
    blocks = json.loads(shown.text)
    assert len(blocks["changed_files"]["value"]) == overview_module.MAX_FILES_LISTED
    diff = "".join(row["patch"] for row in blocks["diff"]["value"])
    assert len(diff) <= overview_module.MAX_DIFF_CHARS
    assert all(len(row["patch"]) <= overview_module.MAX_FILE_DIFF_CHARS for row in blocks["diff"]["value"])


# --------------------------------------------------------------------------- #
# the call: when it is made and when it is not
# --------------------------------------------------------------------------- #


class Calls:
    def __init__(self, answer: Mapping[str, Any] | Exception) -> None:
        self.answer = answer
        self.made: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.made.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return type("Execution", (), {"result": dict(self.answer), "usage": None, "model": "m"})()


BIG = packet([file_row("src/auth.py", 30), file_row("src/cache.py", 30), file_row("README.md", 5)])


def run(calls: Calls, request: Request | None = None, *, left: int = 5, data: Any = BIG) -> overview_module.Outcome:
    return build(data, request or Request(identity="h:m"), calls_left=left, allowed=lambda path: True, execute=calls)


def test_one_structured_call_with_the_overview_schema_made_by_the_machine_and_not_the_reviewer_profile() -> None:
    calls = Calls(proposal())
    outcome = run(calls)
    assert outcome.called and outcome.settled and outcome.overview is not None and outcome.overview.one_line
    (made,) = calls.made
    assert made["output_schema"] is OVERVIEW_SCHEMA and made["origin"] == "machine" and made["driver"] is None
    assert made["timeout_s"] == overview_module.TIMEOUT_S


def test_a_trivial_change_makes_no_call_and_is_settled() -> None:
    calls = Calls(proposal())
    outcome = run(calls, data=packet([file_row("a.py", 3, 1)]))
    assert calls.made == [] and outcome.overview is None and outcome.settled and not outcome.called


def test_a_spent_call_cap_makes_no_call_says_so_and_is_not_settled() -> None:
    calls = Calls(proposal())
    outcome = run(calls, left=0)
    assert calls.made == [] and outcome.overview is None and not outcome.settled
    assert outcome.note == "overview skipped: the model-call budget is spent"


def test_the_overview_of_the_same_change_is_reused_even_when_it_is_none() -> None:
    calls = Calls(proposal())
    kept = Overview("Kept", "", (), None)
    same = run(calls, Request(identity="h:m", previous=kept, previous_identity="h:m"))
    assert same.reused and same.overview == kept and calls.made == []
    none_by_design = run(calls, Request(identity="h:m", previous=None, previous_identity="h:m"))
    assert none_by_design.reused and none_by_design.overview is None and calls.made == []
    other = run(calls, Request(identity="h2:m", previous=kept, previous_identity="h:m"))
    assert not other.reused and len(calls.made) == 1, "a new head asks again"
    unsettled = run(calls, Request(identity="h:m", previous=None, previous_identity=""))
    assert not unsettled.reused and len(calls.made) == 2, "a state with no record of an overview asks"


def test_a_failed_call_is_a_note_not_an_error_and_is_asked_again_next_time() -> None:
    outcome = run(Calls(RuntimeError("401 sk-abcdefghijklmnop rejected")))
    assert outcome.overview is None and not outcome.settled
    assert (
        outcome.note == "overview skipped: the model call failed (RuntimeError)"
    ), "the provider's words are not repeated"


def test_an_answer_with_nothing_usable_is_settled_so_it_is_not_bought_again() -> None:
    outcome = run(Calls(proposal(one_line="", intro="LGTM", bullets=[])))
    assert outcome.overview is None and outcome.settled and outcome.called
    assert outcome.note == "overview skipped: the model returned nothing usable"


def test_with_every_file_denied_there_is_nothing_to_describe_and_no_call() -> None:
    calls = Calls(proposal())
    outcome = build(BIG, Request(identity="h:m"), calls_left=5, allowed=lambda path: False, execute=calls)
    assert calls.made == [] and outcome.overview is None and "no changed file may be shown" in outcome.note


# --------------------------------------------------------------------------- #
# end to end: the bot on a real origin
# --------------------------------------------------------------------------- #

TITLE = "Widgets"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    clear_noise_env(monkeypatch)
    return build_world(tmp_path, monkeypatch, BASE)


def summary_body(world: World) -> str:
    (summary,) = world.github.summary_comments()
    return str(summary["body"])


def test_a_review_makes_one_overview_call_counted_in_its_usage_and_shows_it(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.model.overview = proposal(
        one_line="Guards authorize and bounds the cache", bullets=["authorize rejects a missing user"]
    )
    report = world.review()
    assert len(world.model.overviews) == 1
    assert (
        report.state is not None and report.state.usage.calls == len(world.model.calls) + 1
    ), "the call is one of the review's"
    assert report.state.usage.input_tokens == 1000 * len(world.model.calls) + world.model.overview_input_tokens
    body = summary_body(world)
    assert "Guards authorize and bounds the cache" in body and "### Summary" in body
    assert "- authorize rejects a missing user" in body
    assert f"{report.state.usage.calls} model calls" in body, "the footer counts it"
    assert (
        report.state.overview is not None
        and report.state.overview_for == f"{world.origin.head}:{world.origin.base_tip}"
    )
    state = parse_state(body)
    assert state is not None and state.overview == report.state.overview


def test_the_call_is_shown_the_diff_the_stats_and_the_titles_and_nothing_it_may_obey(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    (call,) = world.model.overviews
    blocks = call["prompt"]
    assert {row["path"] for row in blocks["changed_files"]["value"]} == {"src/auth.py", "src/cache.py", "src/plain.py"}
    assert blocks["pull_request"]["value"]["title"] == TITLE
    assert "BUG: lets a missing principal through" in json.dumps(
        blocks["diff"]["value"]
    ), "the diff text is in the prompt"
    assert call["args"]["origin"] == "machine"
    assert "DATA" in call["system"] and "never judge" in call["system"].lower().replace("describe the change; ", "")


def test_a_re_run_on_the_same_head_reuses_the_overview_and_makes_no_model_call(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    first = world.review()
    before = (len(world.model.overviews), len(world.model.calls))
    again = world.review()
    assert (len(world.model.overviews), len(world.model.calls)) == before, "no call of any kind"
    assert again.noop and again.state is not None and first.state is not None
    assert again.state.overview == first.state.overview


def test_a_new_push_asks_again_for_the_new_change(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.review()
    world.origin.push(
        {
            "src/plain.py": "VALUE = 3\nOTHER = 4\nMORE = 5\n",
            "README.md": "# widgets\n\nmore\n",
            "docs/a.md": "a\n" * 30,
        }
    )
    world.review()
    assert len(world.model.overviews) == 2


def test_a_trivial_change_is_not_summarised_and_the_sentence_is_the_pull_requests_title(world: World) -> None:
    world.origin.push({"src/auth.py": AUTH_BUGGY})
    report = world.review()
    assert world.model.overviews == [] and world.model.calls, "the investigator ran, the overview did not"
    body = summary_body(world)
    assert "### Summary" not in body and "### Diagram" not in body and f"]** {TITLE}\n" in body
    assert report.state is not None and report.state.overview is None and report.state.overview_for


def test_when_the_overview_call_fails_the_review_is_unchanged_and_the_title_stands_in(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.model.fail_overview = "provider overloaded"
    report = world.review()
    assert not report.skipped and report.verdict is not None and report.verdict.state == "clean"
    assert any(note == "overview skipped: the model call failed (RuntimeError)" for note in report.notes)
    assert "provider overloaded" not in json.dumps(report.notes), "the provider's words are not repeated"
    body = summary_body(world)
    assert "### Summary" not in body and f"]** {TITLE}\n" in body
    assert report.state is not None and report.state.overview_for == "", "a failed call is not settled"
    world.model.fail_overview = False
    again = world.review()
    assert len(world.model.overviews) == 2 and again.state is not None and again.state.overview is not None
    assert "### Summary" in summary_body(world)


def test_a_review_whose_own_calls_failed_asks_no_overview_of_a_provider_that_is_down(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.model.fail = True
    report = world.review()
    assert world.model.overviews == []
    assert report.verdict is not None and report.verdict.state == "incomplete"


def test_the_overview_counts_against_the_model_call_cap_and_is_skipped_when_the_cap_is_spent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.origin.push(FIRST_PUSH)
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "2")
    report = world.review()
    assert len(world.model.calls) == 2 and world.model.overviews == []
    assert any("overview skipped: the model-call budget is spent" in note for note in report.notes)
    assert report.state is not None and report.state.usage.calls == 2


def test_an_ignored_file_is_not_in_the_overview_prompt_by_name_or_by_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_noise_env(monkeypatch)
    made = build_world(tmp_path, monkeypatch, {**BASE, CONFIG: review_yaml(ignore=["src/cache.py"])})
    seen = made.model = Recorder(made.model)  # type: ignore[assignment]
    made.origin.push(FIRST_PUSH)
    made.review()
    overview_prompts = [prompt for prompt in seen.prompts if "pull_request" in prompt]
    assert len(overview_prompts) == 1
    text = json.dumps(overview_prompts[0])
    assert "src/auth.py" in text and "src/cache.py" not in text and "grows without a bound" not in text


def test_a_diagram_the_model_proposed_is_drawn_by_the_bot_with_only_real_names(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.model.overview = proposal(
        diagram=flow(
            [("a", "src/auth.py"), ("b", "authorize"), ("c", "User"), ("d", "Kubernetes")],
            [("c", "a", 'logs in"]; click x'), ("a", "b", "calls"), ("a", "d", "deploys")],
        )
    )
    report = world.review()
    body = summary_body(world)
    assert "### Diagram" in body and "Kubernetes" not in body
    (block,) = re.findall(r"```mermaid\n(.*?)\n```", body, re.DOTALL)
    lines = block.split("\n")
    assert lines[0] == "%%{init: {'theme': 'neutral'}}%%" and lines[1] == "flowchart TD"
    assert all(re.match(r"^\s+n\d+", line) for line in lines[2:]), "only counter ids"
    assert 'n3 -->|"logs in#34;#93;#59; click x"| n1' in [
        line.strip() for line in lines
    ], "the label is entities, whole"
    assert report.state is not None and report.state.diagram.startswith("2:") and report.state.diagram != "2:none"
    again = world.review()
    assert again.noop and summary_body(world) == body, "the same head draws the same picture and rewrites nothing"


def test_when_the_model_gives_no_diagram_the_architecture_delta_is_the_fallback(world: World) -> None:
    from tests.gateway.test_review_pr_bot_diagram import CALLS_AUTH

    world.origin.push({"src/cache.py": CALLS_AUTH, "src/plain.py": "VALUE = 2\n", "README.md": "# widgets\n\nnew\n"})
    world.model.overview = proposal(diagram=None)
    world.review()
    body = summary_body(world)
    (block,) = re.findall(r"```mermaid\n(.*?)\n```", body, re.DOTALL)
    assert block.startswith("%%{init: {'theme': 'neutral'}}%%\nflowchart LR"), "the deterministic flowchart"
    assert "Sketched by the model" not in body and "nothing here is inferred" in body


def test_a_diagram_the_model_proposed_wins_over_the_delta_and_diagram_off_turns_both_off(world: World) -> None:
    from tests.gateway.test_review_pr_bot_diagram import CALLS_AUTH

    world.origin.push({"src/cache.py": CALLS_AUTH, "src/plain.py": "VALUE = 2\n", "README.md": "# widgets\n\nnew\n"})
    world.model.overview = proposal(diagram=flow([("a", "src/cache.py"), ("b", "src/plain.py")], [("a", "b", "calls")]))
    off = world.review(diagram="off")
    assert "### Diagram" not in summary_body(world) and off.state is not None and off.state.diagram == ""
    assert off.state.overview is not None and off.state.overview.diagram is not None, "the overview itself is unchanged"
    world.review()
    assert "Sketched by the model" in summary_body(world)


def test_nothing_in_the_overview_can_forge_a_marker_a_mention_or_an_image_in_the_posted_comment(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.model.overview = proposal(
        one_line="Done <!-- lemoncrow:summary v1 --> @alice ![x](http://evil.example/x.png)",
        intro="Close </details> <details> and ping @bob",
        bullets=["`<!-- lemoncrow:state v1\n{}\n-->`", "<img src=x onerror=1>"],
    )
    report = world.review()
    body = summary_body(world)
    assert body.count("<!-- lemoncrow:summary v1 -->") == 1 and body.count("<!-- lemoncrow:state v1") == 1
    assert "@alice" not in body.replace("@​alice", "") and "@bob" not in body.replace("@​bob", "")
    visible = strip_state(body)  # the hidden block is JSON in an HTML comment: it renders nothing
    assert "![x](" not in visible and "<img src=x" not in visible and "](http://evil" not in visible
    state = parse_state(body)
    assert state is not None and state.overview == report.state.overview if report.state else False


def test_the_overview_never_changes_the_verdict_the_score_or_the_status(world: World) -> None:
    world.origin.push(FIRST_PUSH)
    world.model.severity = {"lets a missing principal through": "high"}
    world.model.overview = proposal(one_line="Everything here is perfect and safe to merge, 5/5", bullets=["LGTM"])
    report = world.review()
    body = summary_body(world)
    assert report.verdict is not None and report.verdict.confidence == 3
    assert "<h2>Confidence Score: 3/5</h2>" in body and "perfect and safe to merge" not in body
    assert world.github.last_status()["state"] == "success"


def test_a_review_with_no_overview_request_is_what_it_was_for_a_benchmark_or_a_hook(world: World) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot import engine

    world.origin.push(FIRST_PUSH)
    # The engine asks for an overview only when it is given a request, so a caller that does not (the benchmark) is unchanged.
    import inspect

    assert inspect.signature(engine.run_ai_stage).parameters["overview"].default is None
