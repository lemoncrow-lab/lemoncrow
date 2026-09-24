"""Where the two halves of a contract must agree, asserted against each other.

The client and the server each keep their own copy of the protocol version,
the capability names, the error vocabulary, the execution matrix and the tool
surface. That duplication is deliberate -- neither package may depend on the
other -- but duplication without a comparison is drift waiting to happen, and
every one of these has a failure mode that only shows up in production:

* a capability name typo -> refused at handshake;
* an error code the client does not know -> an agent that cannot branch;
* a matrix disagreement -> a local-only tool sent over the network;
* a tool surface drift -> a name the model calls and nothing answers.

All of it is conditional, because the public client must test green without
either private or public sibling installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _serverpkg import REASON, public_registry_available, server_available
from lemoncrow_client.errors import AgentAction, ErrorCode
from lemoncrow_client.protocol import (
    CLIENT_CAPABILITIES,
    PROTOCOL_VERSION,
    REQUIRED_CLIENT_CAPABILITIES,
)
from lemoncrow_client.routing import ROUTES, SECURITY_EXCLUDED_TOOLS, Site
from lemoncrow_client.surface import SURFACE_PATH, load_surface, tool_list

PUBLIC_REASON = "the public lemoncrow distribution is not importable here"


# --------------------------------------------------------------------------- #
# Against the private server                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not server_available(), reason=REASON)
def test_the_protocol_version_matches() -> None:
    from lemoncrow_server_core import protocol as server_protocol

    assert PROTOCOL_VERSION == server_protocol.PROTOCOL_VERSION
    assert PROTOCOL_VERSION in server_protocol.SUPPORTED_PROTOCOL_VERSIONS


@pytest.mark.skipif(not server_available(), reason=REASON)
def test_the_client_offers_everything_the_server_requires() -> None:
    from lemoncrow_server_core import protocol as server_protocol

    assert REQUIRED_CLIENT_CAPABILITIES == server_protocol.REQUIRED_CLIENT_CAPABILITIES
    assert server_protocol.REQUIRED_CLIENT_CAPABILITIES <= CLIENT_CAPABILITIES


@pytest.mark.skipif(not server_available(), reason=REASON)
def test_every_capability_the_client_offers_is_one_the_server_knows() -> None:
    """Offering a name the server has never heard of is a silent no-op."""
    from lemoncrow_server_core import protocol as server_protocol

    unknown = CLIENT_CAPABILITIES - server_protocol.SERVER_CAPABILITIES
    assert unknown == set()


@pytest.mark.skipif(not server_available(), reason=REASON)
def test_the_error_vocabulary_is_a_superset_of_the_servers() -> None:
    """A code the client cannot name is a refusal an agent cannot branch on."""
    from lemoncrow_server_core import errors as server_errors

    server_codes = {member.value for member in server_errors.ErrorCode}
    client_codes = {member.value for member in ErrorCode}
    assert server_codes <= client_codes

    server_actions = {member.value for member in server_errors.AgentAction}
    client_actions = {member.value for member in AgentAction}
    assert server_actions == client_actions


@pytest.mark.skipif(not server_available(), reason=REASON)
def test_the_execution_matrix_agrees_tool_for_tool() -> None:
    from lemoncrow_server_core import matrix as server_matrix

    mine = {name: route.site.value for name, route in ROUTES.items()}
    theirs = {
        name: site.value for name, site in ((name, server_matrix.site_of(name)) for name in ROUTES) if site is not None
    }
    assert mine == theirs

    server_classified = (
        server_matrix.SERVER_TOOLS
        | server_matrix.SERVER_ADMIN_TOOLS
        | server_matrix.CLIENT_TOOLS
        | server_matrix.SYNC_TOOLS
    )
    assert set(ROUTES) == server_classified
    assert SECURITY_EXCLUDED_TOOLS == server_matrix.SECURITY_EXCLUDED_TOOLS


@pytest.mark.skipif(not server_available(), reason=REASON)
def test_the_client_never_routes_a_local_capture_tool_anywhere() -> None:
    """Review capture drives a local browser and is excluded by construction."""
    from lemoncrow_server_core import matrix as server_matrix

    assert set(ROUTES) & server_matrix.LOCAL_CAPTURE_TOOLS == set()


@pytest.mark.skipif(not server_available(), reason=REASON)
def test_every_tool_the_client_sends_to_the_server_is_one_the_server_accepts() -> None:
    from lemoncrow_server_core import matrix as server_matrix
    from lemoncrow_server_core.errors import ServerError

    for name, route in ROUTES.items():
        if not route.remote:
            continue
        assert server_matrix.require_server_side(name) is not None

    for name, route in ROUTES.items():
        if route.site is not Site.CLIENT:
            continue
        with pytest.raises(ServerError):
            server_matrix.require_server_side(name)


# --------------------------------------------------------------------------- #
# Against the public registry                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not public_registry_available(), reason=PUBLIC_REASON)
def test_the_bundled_tool_surface_is_the_public_registrys() -> None:
    """Regenerate it and diff. Drift here is a name the model calls in vain."""
    from lemoncrow.gateway.adapters import mcp_server as public
    from lemoncrow.gateway.tools.surface import bundled_tool_record

    expected = {name: bundled_tool_record(name, spec) for name, spec in public.TOOLS.items()}
    bundled = load_surface()
    assert set(bundled) == set(expected), "the bundled surface names a different tool set"
    for name in sorted(expected):
        assert dict(bundled[name]) == expected[name], f"{name} drifted; regenerate {SURFACE_PATH.name}"


@pytest.mark.skipif(not public_registry_available(), reason=PUBLIC_REASON)
def test_the_routing_table_covers_the_public_registry_except_security_exclusions() -> None:
    """Every upstream tool is routed or explicitly denied by the one-process policy."""
    from lemoncrow.gateway.adapters import mcp_server as public

    assert set(ROUTES).isdisjoint(SECURITY_EXCLUDED_TOOLS)
    assert set(ROUTES) | set(SECURITY_EXCLUDED_TOOLS) == set(public.TOOLS)


@pytest.mark.skipif(not public_registry_available(), reason=PUBLIC_REASON)
def test_generated_visibility_matches_the_canonical_environment_policy() -> None:
    """The dependency-free client consumes policy data generated from one owner."""
    from lemoncrow.core.environment import CORE_MCP_TOOLS, LLM_VISIBLE_TOOLS

    surface = load_surface()
    for name in ROUTES:
        assert bool(surface[name].get("visibleToLlm")) is (name in LLM_VISIBLE_TOOLS)
        assert bool(surface[name].get("core")) is (name in CORE_MCP_TOOLS)


# --------------------------------------------------------------------------- #
# Unconditional: the descriptor itself                                        #
# --------------------------------------------------------------------------- #


def test_the_bundled_surface_is_well_formed_and_covers_the_table() -> None:
    surface = load_surface()
    assert set(ROUTES) <= set(surface)
    assert SECURITY_EXCLUDED_TOOLS <= set(surface)
    assert not (SECURITY_EXCLUDED_TOOLS & {entry["name"] for entry in tool_list()})
    for name, spec in surface.items():
        assert isinstance(spec.get("description"), str) and spec["description"], name
        assert isinstance(spec.get("inputSchema"), dict), name


def test_the_surface_is_a_plain_data_file_with_no_machine_specific_content() -> None:
    """A bundled data file must be identical on every machine.

    Tool descriptions legitimately talk about "hardcoded secrets" and "token
    budgets", so the scan looks for the two things that would actually make the
    file machine-specific: an absolute filesystem path, and anything shaped like
    a credential.
    """
    import re

    raw = SURFACE_PATH.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert set(parsed) == {"generated_from", "tools"}
    assert parsed["generated_from"] == "lemoncrow.gateway.adapters.mcp_server.TOOLS"

    for needle in (str(Path.home()), "/Users/", "/home/", "/root/", "/var/folders/"):
        assert needle not in raw, f"the bundled surface contains the absolute path {needle!r}"
    credential = re.compile(r"\b(?:[A-Fa-f0-9]{32,}|sk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]{16,})\b")
    assert credential.search(raw) is None, "the bundled surface contains something credential-shaped"


def test_tool_list_is_stable_across_calls() -> None:
    assert tool_list() == tool_list()


def test_tool_list_defaults_to_the_canonical_visible_surface() -> None:
    """``full`` means every canonically visible routed tool, never hidden tools."""
    surface = load_surface()
    expected = {name for name in ROUTES if bool(surface[name].get("visibleToLlm"))}
    for env in ({}, {"LEMONCROW_MCP_TOOL_PROFILE": "full"}, {"LEMONCROW_MCP_TOOL_PROFILE": "bogus"}):
        names = {entry["name"] for entry in tool_list(env=env)}
        assert names == expected


def test_tool_list_narrows_to_the_generated_core_surface_when_core() -> None:
    surface = load_surface()
    expected = {name for name in ROUTES if bool(surface[name].get("visibleToLlm")) and bool(surface[name].get("core"))}
    names = {entry["name"] for entry in tool_list(env={"LEMONCROW_MCP_TOOL_PROFILE": "core"})}
    assert names == expected


def test_tool_list_profile_is_case_and_whitespace_insensitive() -> None:
    surface = load_surface()
    expected = {name for name in ROUTES if bool(surface[name].get("visibleToLlm")) and bool(surface[name].get("core"))}
    names = {entry["name"] for entry in tool_list(env={"LEMONCROW_MCP_TOOL_PROFILE": "  CORE  "})}
    assert names == expected


def test_tool_list_has_no_runtime_subtractive_visibility_override() -> None:
    surface = load_surface()
    expected = {name for name in ROUTES if bool(surface[name].get("visibleToLlm"))}
    names = {entry["name"] for entry in tool_list(env={"UNRELATED_SETTING": "bash"})}
    assert names == expected
