"""Server half of the version-skew contract (G6-SKEW).

The protocol support window is two dates, N and N-1. Inside it a client is
served; outside it the refusal says which side to upgrade. The Review
capability manifest names every Review feature and the evidence, policy and
record schema versions this server understands, and it is the same document
at discovery, at the handshake and on the Review API.

The client half is ``client/tests/test_client_protocol_window.py``; the cross-version
matrix is ``test_protocol_skew_matrix.py``.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession
from lemoncrow_client import protocol as client_protocol
from lemoncrow_client.errors import ClientError
from lemoncrow_server_core import local_app, protocol
from lemoncrow_server_core.bootstrap import build_local_server, read_local_token
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.errors import AgentAction, ErrorCode, ServerError
from lemoncrow_server_core.protocol import (
    CAPTURE_OPTIONS,
    PREVIOUS_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    PROTOCOL_WINDOW,
    REQUIRED_CLIENT_CAPABILITIES,
    REVIEW_FEATURES,
    REVIEW_MANIFEST_SCHEMA,
    ProtocolWindow,
    negotiate,
    require_capture_options,
    require_packet_schema,
    review_capabilities,
)

from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION as PACKET_SCHEMA_VERSION
from lemoncrow.pro.capabilities.review.readiness import READINESS_RULES_VERSION
from lemoncrow.pro.capabilities.review.review_record import RECORD_SCHEMA_VERSION
from lemoncrow.pro.capabilities.review.session_models import REVIEW_EVIDENCE_KINDS, SESSION_SCHEMA_VERSION


def _offer(version: str) -> dict[str, Any]:
    return {"protocol_version": version, "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES)}


def _day(version: str, delta: int) -> str:
    return (date.fromisoformat(version) + timedelta(days=delta)).isoformat()


# --------------------------------------------------------------------------- #
# The window                                                                  #
# --------------------------------------------------------------------------- #


def test_the_window_is_exactly_n_and_n_minus_one_as_dates() -> None:
    assert PROTOCOL_WINDOW == ProtocolWindow(current=PROTOCOL_VERSION, previous=PREVIOUS_PROTOCOL_VERSION)
    assert protocol.SUPPORTED_PROTOCOL_VERSIONS == frozenset({PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION})
    assert date.fromisoformat(PREVIOUS_PROTOCOL_VERSION) < date.fromisoformat(PROTOCOL_VERSION)


@pytest.mark.parametrize("version", [PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION])
def test_both_ends_of_the_window_negotiate_to_the_offered_version(version: str) -> None:
    assert negotiate(_offer(version)).protocol_version == version


def test_one_day_before_the_window_is_refused_with_upgrade_the_client() -> None:
    stale = _day(PREVIOUS_PROTOCOL_VERSION, -1)
    with pytest.raises(ServerError) as caught:
        negotiate(_offer(stale))
    error = caught.value
    assert error.code is ErrorCode.PROTOCOL_VERSION_MISMATCH
    assert error.status == 426
    assert error.action is AgentAction.UPGRADE_CLIENT
    assert "upgrade the client" in error.message
    assert error.details["direction"] == "client_older"
    assert error.details["client_protocol"] == stale
    assert error.details["server_protocol"] == PROTOCOL_VERSION
    assert error.details["supported"] == sorted({PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION})


def test_one_day_after_the_window_is_refused_with_upgrade_the_server() -> None:
    newer = _day(PROTOCOL_VERSION, 1)
    with pytest.raises(ServerError) as caught:
        negotiate(_offer(newer))
    error = caught.value
    assert error.code is ErrorCode.PROTOCOL_VERSION_MISMATCH
    assert error.status == 426
    assert error.action is AgentAction.UPGRADE_SERVER
    assert "the server is older; upgrade the server" in error.message
    assert error.details["direction"] == "client_newer"


def test_a_date_inside_the_window_that_names_no_version_is_refused() -> None:
    between = _day(PREVIOUS_PROTOCOL_VERSION, 1)
    assert between < PROTOCOL_VERSION
    with pytest.raises(ServerError) as caught:
        negotiate(_offer(between))
    assert caught.value.code is ErrorCode.PROTOCOL_VERSION_MISMATCH
    assert caught.value.action is AgentAction.UPGRADE_CLIENT


@pytest.mark.parametrize("version", ["v2", "2026-13-01", "2026-13-40", "20261005"])
def test_a_version_that_is_not_a_date_is_refused_loudly(version: str) -> None:
    with pytest.raises(ServerError) as caught:
        negotiate(_offer(version))
    assert caught.value.code is ErrorCode.PROTOCOL_VERSION_MISMATCH
    assert caught.value.action is AgentAction.UPGRADE_CLIENT
    assert "upgrade the client" in caught.value.message
    # Not orderable, so neither "older" nor "newer" is claimed for it.
    assert caught.value.details["direction"] == "unrecognized"


@pytest.mark.parametrize(
    ("current", "previous"),
    [("2026-10-05", "2026-10-05"), ("2026-09-14", "2026-10-05"), ("next", "2026-10-05"), ("2026-10-05", "")],
)
def test_a_window_is_two_strictly_ordered_dates(current: str, previous: str) -> None:
    with pytest.raises(ValueError):
        ProtocolWindow(current=current, previous=previous)


def test_negotiation_and_discovery_read_the_window_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server one version behind is this code with an older window."""

    older = ProtocolWindow(current=PREVIOUS_PROTOCOL_VERSION, previous=_day(PREVIOUS_PROTOCOL_VERSION, -30))
    monkeypatch.setattr(protocol, "PROTOCOL_WINDOW", older)
    assert negotiate(_offer(PREVIOUS_PROTOCOL_VERSION)).protocol_version == PREVIOUS_PROTOCOL_VERSION
    with pytest.raises(ServerError) as caught:
        negotiate(_offer(PROTOCOL_VERSION))
    assert caught.value.action is AgentAction.UPGRADE_SERVER
    identity = protocol.server_identity(allow_local_fs=False)
    assert identity["protocol_version"] == PREVIOUS_PROTOCOL_VERSION
    assert identity["supported_protocol_versions"] == sorted(older.versions)


def test_client_and_server_declare_the_same_window() -> None:
    """Cutting a version moves both halves in one change, or this fails."""

    assert client_protocol.protocol_window() == (PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION)


def test_client_and_server_name_the_same_review_features_and_capture_options() -> None:
    assert client_protocol.REVIEW_FEATURES == REVIEW_FEATURES
    assert client_protocol.CAPTURE_OPTIONS == CAPTURE_OPTIONS


# --------------------------------------------------------------------------- #
# Server-side fail-closed writes                                              #
# --------------------------------------------------------------------------- #


def test_the_current_packet_schema_passes_and_a_malformed_one_is_left_to_the_decoder() -> None:
    require_packet_schema({"schema_version": PACKET_SCHEMA_VERSION})
    require_packet_schema({})
    require_packet_schema({"schema_version": "2"})


@pytest.mark.parametrize(
    ("delta", "action"),
    [(-1, AgentAction.UPGRADE_CLIENT), (1, AgentAction.UPGRADE_SERVER)],
)
def test_a_packet_schema_this_server_does_not_understand_is_refused_typed(delta: int, action: AgentAction) -> None:
    version = PACKET_SCHEMA_VERSION + delta
    with pytest.raises(ServerError) as caught:
        require_packet_schema({"schema_version": version})
    error = caught.value
    assert error.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert error.action is action
    assert error.details == {"schema": "packet", "version": version, "supported": [PACKET_SCHEMA_VERSION]}


def test_an_unhonored_capture_option_is_refused_instead_of_dropped() -> None:
    with pytest.raises(ServerError) as caught:
        require_capture_options(
            {"review_scope": "agent-1", "force_new_review": True, "title": "kept"},
            honored=frozenset({"title", "restore_archived"}),
        )
    error = caught.value
    assert error.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert error.details["unsupported_options"] == ["force_new_review", "review_scope"]
    assert "does not honor" in error.message


#: One requested (non-default) value per capture option.
REQUESTED_OPTIONS: dict[str, object] = {
    "title": "a title",
    "restore_archived": True,
    "force_new_review": True,
    "continue_review_id": "r/1",
    "reuse_closed_review": True,
    "review_scope": "agent-1",
    "workspace_branch": "main",
}

#: What counts as requesting an option. Client and server must agree on every row.
OPTION_VALUES: list[tuple[object, bool]] = [
    ("", False),
    ("   ", False),
    ("\t\n", False),
    ("x", True),
    (" x ", True),
    (True, True),
    (False, False),
    (None, False),
    (0, False),
    (1, True),
    ([], False),
    (["a"], True),
    ({}, False),
    ({"a": 1}, True),
]


def test_every_capture_option_has_a_requested_example() -> None:
    assert set(REQUESTED_OPTIONS) == CAPTURE_OPTIONS


def test_a_server_honoring_every_option_refuses_none() -> None:
    require_capture_options(dict(REQUESTED_OPTIONS), honored=CAPTURE_OPTIONS)


@pytest.mark.parametrize("option", sorted(CAPTURE_OPTIONS))
def test_each_capture_option_a_server_does_not_honor_is_refused(option: str) -> None:
    """Every option, one at a time: dropping any single one from the guard fails here."""

    honored = CAPTURE_OPTIONS - {option}
    require_capture_options({name: REQUESTED_OPTIONS[name] for name in honored}, honored=honored)
    with pytest.raises(ServerError) as caught:
        require_capture_options({option: REQUESTED_OPTIONS[option]}, honored=honored)
    assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert caught.value.action is AgentAction.UPGRADE_SERVER
    assert caught.value.details == {"unsupported_options": [option], "honored": sorted(honored)}


def test_an_option_left_at_its_default_is_not_a_request_for_it() -> None:
    require_capture_options(
        {"review_scope": "", "force_new_review": False, "continue_review_id": "", "title": "kept"},
        honored=frozenset({"title"}),
    )


@pytest.mark.parametrize(("value", "requested"), OPTION_VALUES, ids=[repr(value) for value, _ in OPTION_VALUES])
def test_server_and_client_agree_on_what_requests_an_option(value: object, requested: bool) -> None:
    try:
        require_capture_options({"review_scope": value}, honored=frozenset())
        server_refused = False
    except ServerError:
        server_refused = True
    manifest = client_protocol.parse_review_manifest(
        PROTOCOL_VERSION,
        {"product_capabilities": {"review": {"manifest": 1, "capture": True, "capture_modes": ["packet"]}}},
    )
    try:
        manifest.require_capture("packet", {"review_scope": value})
        client_refused = False
    except ClientError:
        client_refused = True
    assert server_refused is requested
    assert client_refused is requested


# --------------------------------------------------------------------------- #
# The manifest                                                                #
# --------------------------------------------------------------------------- #


def _complete_features(value: bool = True) -> dict[str, bool]:
    return dict.fromkeys(REVIEW_FEATURES, value)


def test_the_manifest_refuses_an_incomplete_or_unknown_feature_set() -> None:
    features = _complete_features()
    del features["ask"]
    with pytest.raises(ValueError, match="ask"):
        review_capabilities(features, capture_modes=("packet",), capture_options=CAPTURE_OPTIONS)
    with pytest.raises(ValueError, match="telepathy"):
        review_capabilities(
            {**_complete_features(), "telepathy": True}, capture_modes=("packet",), capture_options=CAPTURE_OPTIONS
        )
    with pytest.raises(ValueError):
        review_capabilities(_complete_features(), capture_modes=("fax",), capture_options=())
    with pytest.raises(ValueError):
        review_capabilities(_complete_features(), capture_modes=("packet",), capture_options=("teleport",))


def _serve(tmp_path: Path, scenario: Any) -> Any:
    async def run() -> Any:
        token_file = tmp_path / "token"
        server, _state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", token_file=token_file, allow_local_fs=False)
        )
        url = await server.start()
        try:
            async with ClientSession() as client:
                return await scenario(client, url, read_local_token(token_file))
        finally:
            await server.stop()

    return asyncio.run(run())


def test_the_local_manifest_names_every_feature_and_schema(tmp_path: Path) -> None:
    async def scenario(client: ClientSession, url: str, token: str) -> dict[str, Any]:
        response = await client.get(f"{url}/.well-known/lemoncrow-server.json")
        assert response.status == 200
        return dict(await response.json())

    discovery = _serve(tmp_path, scenario)
    review = discovery["product_capabilities"]["review"]
    assert review["manifest"] == REVIEW_MANIFEST_SCHEMA
    assert {name: type(review[name]) for name in REVIEW_FEATURES} == dict.fromkeys(REVIEW_FEATURES, bool)
    assert review["capture"] is True
    assert review["evidence_read"] is True
    assert review["evidence_upload"] is True
    assert review["schemas"] == {
        "packet": [PACKET_SCHEMA_VERSION],
        "evidence": [SESSION_SCHEMA_VERSION],
        "policy": [READINESS_RULES_VERSION],
        "record": [RECORD_SCHEMA_VERSION],
    }
    assert review["evidence_kinds"] == sorted(REVIEW_EVIDENCE_KINDS)
    # Read from the constant: the loopback capture modes may grow (server-side derivation).
    assert review["capture_modes"] == sorted(local_app.LOCAL_CAPTURE_MODES)
    assert review["capture_options"] == sorted(CAPTURE_OPTIONS)


def test_handshake_discovery_and_the_review_api_return_one_manifest(tmp_path: Path) -> None:
    async def scenario(client: ClientSession, url: str, token: str) -> tuple[Any, ...]:
        discovery = await (await client.get(f"{url}/.well-known/lemoncrow-server.json")).json()
        handshake = await client.post(
            f"{url}/v1/handshake",
            json={**_offer(PROTOCOL_VERSION), "client_name": "test", "client_version": "1"},
        )
        assert handshake.status == 200
        unauthenticated = await client.get(f"{url}/v1/reviews/manifest")
        api = await client.get(f"{url}/v1/reviews/manifest", headers={"Authorization": f"Bearer {token}"})
        assert api.status == 200
        return discovery, await handshake.json(), unauthenticated.status, await api.json()

    discovery, handshake, unauthenticated, api = _serve(tmp_path, scenario)
    manifest = discovery["product_capabilities"]["review"]
    assert handshake["server"]["product_capabilities"]["review"] == manifest
    assert unauthenticated == 401
    assert api == {
        "protocol_version": PROTOCOL_VERSION,
        "supported_protocol_versions": sorted({PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION}),
        "deployment": {"mode": "local"},
        "product_capabilities": {"review": manifest},
    }


def test_the_local_server_keeps_everything_the_previous_protocol_served(tmp_path: Path) -> None:
    """Client N-1 against server N: no Review feature it relied on disappears."""

    async def scenario(client: ClientSession, url: str, token: str) -> dict[str, Any]:
        return dict(await (await client.get(f"{url}/.well-known/lemoncrow-server.json")).json())

    current = client_protocol.parse_review_manifest(PROTOCOL_VERSION, _serve(tmp_path, scenario))
    legacy = client_protocol.parse_review_manifest(
        PREVIOUS_PROTOCOL_VERSION, {"deployment": {"mode": "local"}, "product_capabilities": {"review": {}}}
    )
    assert current.declared and not legacy.declared
    assert {name for name, on in legacy.features.items() if on} <= {name for name, on in current.features.items() if on}
    assert legacy.capture_modes <= current.capture_modes
    assert legacy.capture_options <= current.capture_options
    assert legacy.evidence_kinds <= current.evidence_kinds
    for name, versions in legacy.schemas.items():
        assert versions <= current.schemas[name], name


def test_a_derived_capture_is_refused_typed_by_a_server_that_derives_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_app, "LOCAL_CAPTURE_MODES", frozenset({"packet"}))

    async def scenario(client: ClientSession, url: str, token: str) -> tuple[int, dict[str, Any]]:
        response = await client.post(
            f"{url}/v1/repos/repo_fixture/reviews",
            json={"view_id": "view_fixture", "view_revision": 1, "source_ref": "x", "derive_packet": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        return response.status, dict(await response.json())

    status, body = _serve(tmp_path, scenario)
    assert status == 400
    assert body["error"]["code"] == ErrorCode.CAPABILITY_UNSUPPORTED.value
    assert body["error"]["details"]["capture_mode"] == "derived"
    assert "does not support server-derived Review capture" in body["error"]["message"]


def test_an_enrichment_in_a_packet_schema_this_server_does_not_read_is_refused_typed(tmp_path: Path) -> None:
    async def scenario(client: ClientSession, url: str, token: str) -> tuple[int, dict[str, Any]]:
        response = await client.post(
            f"{url}/v1/repos/repo_fixture/reviews/review_fixture/revisions/revision_fixture/projection",
            json={"packet": {"schema_version": PACKET_SCHEMA_VERSION + 1, "files": []}},
            headers={"Authorization": f"Bearer {token}"},
        )
        return response.status, dict(await response.json())

    status, body = _serve(tmp_path, scenario)
    assert status == 400
    assert body["error"]["code"] == ErrorCode.CAPABILITY_UNSUPPORTED.value
    assert body["error"]["action"] == AgentAction.UPGRADE_SERVER.value
    assert body["error"]["details"]["schema"] == "packet"
