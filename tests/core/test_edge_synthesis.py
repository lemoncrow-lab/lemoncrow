"""WS4 N2/N3 -- bounded, provenance-tagged call-edge synthesis (first iteration)."""

from __future__ import annotations

from atelier.core.capabilities.code_context.edge_synthesis import synthesize_edges


def test_flask_route_edge_synthesized() -> None:
    source = '@app.route("/users")\ndef list_users():\n    return []\n'
    edges = synthesize_edges(source, language="python")
    assert len(edges) == 1
    e = edges[0]
    assert e.caller == "route:/users"
    assert e.callee == "list_users"
    assert e.kind == "flask_route"
    assert e.provenance == "heuristic"  # never presented as a static edge


def test_blueprint_route_also_recognized() -> None:
    source = '@bp.route("/items/<id>")\ndef get_item(id):\n    return id\n'
    edges = synthesize_edges(source, language="python")
    assert [e.callee for e in edges] == ["get_item"]
    assert edges[0].caller == "route:/items/<id>"


def test_js_event_handler_edge_synthesized() -> None:
    source = 'emitter.on("data", onData);\nemitter.on("end", onEnd);\n'
    edges = synthesize_edges(source, language="javascript")
    pairs = {(e.caller, e.callee) for e in edges}
    assert pairs == {("on:data", "onData"), ("on:end", "onEnd")}
    assert all(e.kind == "event_handler" and e.provenance == "heuristic" for e in edges)


def test_inline_handlers_and_unknown_languages_yield_nothing() -> None:
    # Anonymous inline handlers have no nameable target -> skipped.
    assert synthesize_edges('emitter.on("x", () => {});', language="javascript") == []
    # Unknown language -> no synthesis.
    assert synthesize_edges("fn main() {}", language="rust") == []


def test_syntactically_broken_python_is_safe() -> None:
    assert synthesize_edges('@app.route("/x")\ndef (:', language="python") == []
