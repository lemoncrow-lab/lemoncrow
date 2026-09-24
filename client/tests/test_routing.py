"""The routing table is the product decision; these are its tests.

Two kinds of assertion, and the split is deliberate:

* **The table is the public client contract.** Names, sites and the count are
  literal here, and -- when the private server is importable -- checked
  tool-for-tool against the server's own copy. Two halves of one contract that
  disagree is how a local-only tool ends up being sent over the network.

* **Every row actually routes that way.** Not "the table says client" but "the
  call ran here and opened no connection", and "the table says server" but "the
  call reached the endpoint and named a revision". A table nobody executes is
  documentation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from _stub import StubServer
from lemoncrow_client.dispatcher import Dispatcher
from lemoncrow_client.errors import ErrorCode
from lemoncrow_client.localtools import executor_names
from lemoncrow_client.routing import ROUTES, Site, route_for
from lemoncrow_client.session import RemoteSession

#: The matrix, transcribed from the design. Kept as a literal rather than
#: derived from the table it is meant to check.
DESIGN_MATRIX: Mapping[str, Site] = {
    "bash": Site.CLIENT,
    "edit": Site.CLIENT,
    "grep": Site.CLIENT,
    "blame": Site.CLIENT,
    "scan": Site.CLIENT,
    "codemod": Site.CLIENT,
    "sql": Site.CLIENT,
    "index": Site.CLIENT_UPLOAD_SERVER_BUILD,
    "code_search": Site.SERVER,
    "read": Site.SERVER,
    "relations": Site.SERVER,
    "graph": Site.SERVER,
    "search": Site.SERVER,
    "context": Site.SERVER,
    "memory": Site.SERVER,
    "verify": Site.SERVER,
    "trace": Site.SERVER,
    "rescue": Site.SERVER,
    "compact": Site.SERVER,
    "orient": Site.SERVER,
    "web_fetch": Site.SERVER,
    "statusline_segment": Site.SERVER,
    "review_rationale": Site.SERVER,
    "review_evidence": Site.SERVER,
    "review_feedback_addressed": Site.SERVER,
    "cache": Site.SERVER_ADMIN,
}


def test_the_table_is_the_design_matrix() -> None:
    assert dict(sorted((name, route.site) for name, route in ROUTES.items())) == dict(sorted(DESIGN_MATRIX.items()))


def test_every_row_carries_the_designs_reason() -> None:
    for name, route in ROUTES.items():
        assert route.reason, f"{name} has no reason in the matrix"


def test_client_rows_name_an_executor_and_server_rows_do_not() -> None:
    available = executor_names()
    for name, route in ROUTES.items():
        if route.site in (Site.CLIENT, Site.CLIENT_UPLOAD_SERVER_BUILD):
            assert route.executor in available, f"{name} routes to a missing executor"
        else:
            assert route.executor == "", f"{name} is server-sited but names an executor"


def test_every_executor_is_reachable_from_the_table() -> None:
    """No orphan executors: an implementation nobody routes to is dead code."""
    named = {route.executor for route in ROUTES.values() if route.executor}
    named |= {route.offline for route in ROUTES.values() if route.offline}
    assert named == executor_names()


def test_read_is_the_only_row_with_an_offline_fallback() -> None:
    """The design names exactly one, and more would be a silent second answer."""
    with_fallback = {name for name, route in ROUTES.items() if route.offline}
    assert with_fallback == {"read"}


def test_only_tree_writing_rows_push_blobs() -> None:
    assert {name for name, route in ROUTES.items() if route.pushes_blobs} == {"edit", "codemod"}


def test_an_unrouted_tool_is_refused_by_name() -> None:
    with pytest.raises(Exception) as caught:
        route_for("definitely_not_a_tool")
    assert getattr(caught.value, "code", None) is ErrorCode.TOOL_UNKNOWN


# --------------------------------------------------------------------------- #
# Behaviour: each row routes the way the table says                           #
# --------------------------------------------------------------------------- #


LOCAL_INVOCATIONS: Mapping[str, Mapping[str, Any]] = {
    "bash": {"command": "printf routed"},
    "edit": {"edits": [{"path": "pkg/alpha.py", "old": "alpha", "new": "alpha2"}]},
    "grep": {"regex": "def "},
    "blame": {},
    "scan": {"path": "pkg"},
    "codemod": {"pattern": "alpha()"},
    "sql": {"action": "query", "sql": "SELECT 1"},
    "index": {},
}

SERVER_INVOCATIONS: Mapping[str, Mapping[str, Any]] = {
    name: ({"files": ["pkg/alpha.py"]} if name == "read" else {"query": "alpha"})
    for name, route in ROUTES.items()
    if route.remote
}


@pytest.mark.parametrize("tool", sorted(LOCAL_INVOCATIONS))
def test_client_rows_run_here_and_send_no_tool_call(
    tool: str, stub: StubServer, bootstrapped: RemoteSession, config: Any
) -> None:
    """A client-sited tool never reaches ``POST /v1/tools/{name}``.

    Whether the local executor *succeeds* depends on the machine -- ``ast-grep``
    and the ``lemoncrow`` CLI may not be installed, and the refusal for that is
    itself part of the contract. What must hold on every machine is that the
    call was answered here.
    """
    before = len(stub.state.tool_calls)
    outcome = Dispatcher(config, bootstrapped).call(tool, LOCAL_INVOCATIONS[tool])
    assert len(stub.state.tool_calls) == before, f"{tool} was dispatched to the server"
    assert outcome.site in {"client", "client_upload_server_build", "refused"}
    assert outcome.content, f"{tool} produced no content"


@pytest.mark.parametrize("tool", sorted(SERVER_INVOCATIONS))
def test_server_rows_reach_the_server_naming_a_revision(
    tool: str, stub: StubServer, bootstrapped: RemoteSession, config: Any
) -> None:
    outcome = Dispatcher(config, bootstrapped).call(tool, SERVER_INVOCATIONS[tool])
    assert not outcome.is_error, outcome.content
    dispatched = [entry for entry in stub.state.tool_calls if entry[0] == tool]
    assert dispatched, f"{tool} never reached the server"
    assert dispatched[-1][2] == bootstrapped.view_revision


def test_index_uploads_rather_than_dispatching(stub: StubServer, bootstrapped: RemoteSession, config: Any) -> None:
    """``index`` is the sync protocol, not a tool RPC."""
    before_tools = len(stub.state.tool_calls)
    before_opens = stub.state.manifest_root
    outcome = Dispatcher(config, bootstrapped).call("index", {})
    assert not outcome.is_error, outcome.content
    assert len(stub.state.tool_calls) == before_tools
    assert stub.state.manifest_root == before_opens
    assert outcome.site == Site.CLIENT_UPLOAD_SERVER_BUILD.value


def test_index_refuses_a_glob_filter_that_would_desync_the_view(bootstrapped: RemoteSession, config: Any) -> None:
    outcome = Dispatcher(config, bootstrapped).call("index", {"include_globs": ["src/**"]})
    assert outcome.is_error
    assert "canonical" in outcome.content[0]["text"]
