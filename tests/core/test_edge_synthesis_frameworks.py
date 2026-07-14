"""WS9 N2/N3 -- expanded framework/dynamic-dispatch edge synthesis.

Every test asserts the additive contract: synthesized edges are tagged
``provenance="heuristic"`` and inline anonymous handlers / non-literal targets
produce NO edge (no fabrication).
"""

from __future__ import annotations

from lemoncrow.pro.capabilities.code_context.edge_synthesis import synthesize_edges


def _pairs(source: str, language: str) -> set[tuple[str, str]]:
    return {(e.caller, e.callee) for e in synthesize_edges(source, language=language)}


# --------------------------------------------------------------------------- #
# Python: FastAPI / Flask verb decorators (@app.get etc.)
# --------------------------------------------------------------------------- #
def test_fastapi_verb_route_synthesized() -> None:
    source = '@app.get("/items")\nasync def read_items():\n    return []\n'
    edges = synthesize_edges(source, language="python")
    assert len(edges) == 1
    e = edges[0]
    assert (e.caller, e.callee, e.kind) == ("GET /items", "read_items", "http_route")
    assert e.provenance == "heuristic"


def test_flask_post_verb_route_synthesized() -> None:
    source = '@bp.post("/login")\ndef login():\n    return ""\n'
    assert _pairs(source, "python") == {("POST /login", "login")}


def test_non_http_decorator_not_synthesized() -> None:
    # A plain decorator that is not route/verb must not synthesize an edge.
    source = "@functools.cache\ndef compute():\n    return 1\n"
    assert synthesize_edges(source, language="python") == []


# --------------------------------------------------------------------------- #
# Python: Django URLconf path()/re_path()
# --------------------------------------------------------------------------- #
def test_django_path_route_synthesized() -> None:
    source = 'urlpatterns = [\n    path("home/", home_view),\n    re_path(r"^about$", about_view),\n]\n'
    assert _pairs(source, "python") == {
        ("url:home/", "home_view"),
        ("url:^about$", "about_view"),
    }


def test_django_class_based_view_not_synthesized() -> None:
    # ``.as_view()`` is a Call, not a bare Name -> no single nameable callee.
    source = 'path("users/", UserList.as_view())\n'
    assert synthesize_edges(source, language="python") == []


def test_django_lambda_view_not_synthesized() -> None:
    source = 'path("ping/", lambda r: r)\n'
    assert synthesize_edges(source, language="python") == []


# --------------------------------------------------------------------------- #
# Python: registry/dispatch dict
# --------------------------------------------------------------------------- #
def test_dispatch_dict_synthesized() -> None:
    source = 'HANDLERS = {\n    "start": on_start,\n    "stop": on_stop,\n}\n'
    edges = synthesize_edges(source, language="python")
    assert {(e.caller, e.callee) for e in edges} == {
        ("dispatch:start", "on_start"),
        ("dispatch:stop", "on_stop"),
    }
    assert all(e.kind == "dispatch" and e.provenance == "heuristic" for e in edges)


def test_dispatch_dict_inline_lambda_not_synthesized() -> None:
    # Inline lambda values and non-string keys are skipped -- no fabrication.
    source = 'd = {"a": lambda: 1, 2: handler, "b": obj.method}\n'
    assert synthesize_edges(source, language="python") == []


# --------------------------------------------------------------------------- #
# JS/TS: Express verb routes
# --------------------------------------------------------------------------- #
def test_express_route_synthesized() -> None:
    source = 'app.get("/users", listUsers);\nrouter.post("/users", createUser);\n'
    assert _pairs(source, "javascript") == {
        ("GET /users", "listUsers"),
        ("POST /users", "createUser"),
    }


def test_express_inline_handler_not_synthesized() -> None:
    source = 'app.get("/x", (req, res) => res.end());\n'
    assert synthesize_edges(source, language="javascript") == []


# --------------------------------------------------------------------------- #
# JS/TS: observer / pub-sub (subscribe, addEventListener, addListener)
# --------------------------------------------------------------------------- #
def test_observer_patterns_synthesized() -> None:
    source = 'bus.subscribe("tick", onTick);\nel.addEventListener("click", onClick);\nee.addListener("data", onData);\n'
    assert _pairs(source, "typescript") == {
        ("on:tick", "onTick"),
        ("on:click", "onClick"),
        ("on:data", "onData"),
    }


def test_observer_inline_handler_not_synthesized() -> None:
    source = 'el.addEventListener("click", function () {});\n'
    assert synthesize_edges(source, language="javascript") == []


# --------------------------------------------------------------------------- #
# JS/TS: NestJS controller method decorators
# --------------------------------------------------------------------------- #
def test_nestjs_controller_route_synthesized() -> None:
    source = 'class CatsController {\n  @Get("breeds")\n  findBreeds() {}\n  @Post()\n  async create() {}\n}\n'
    assert _pairs(source, "typescript") == {
        ("GET breeds", "findBreeds"),
        ("POST", "create"),
    }


def test_nestjs_unknown_decorator_not_synthesized() -> None:
    source = "class C {\n  @Injectable()\n  helper() {}\n}\n"
    assert synthesize_edges(source, language="typescript") == []


# --------------------------------------------------------------------------- #
# Cross-cutting: fail-open + provenance contract.
# --------------------------------------------------------------------------- #
def test_all_synthesized_edges_are_heuristic() -> None:
    source = '@app.get("/x")\ndef v():\n    return 1\n\nH = {"k": handler}\n'
    edges = synthesize_edges(source, language="python")
    assert edges  # sanity: something synthesized
    assert all(e.provenance == "heuristic" and e.confidence < 1.0 for e in edges)


def test_broken_source_is_safe() -> None:
    assert synthesize_edges('@app.get("/x")\ndef (:', language="python") == []
    assert synthesize_edges("unterminated string '", language="javascript") == []
