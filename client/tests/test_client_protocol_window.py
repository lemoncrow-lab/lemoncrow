"""Client half of the version-skew contract (G6-SKEW).

The client speaks protocol N and still speaks N-1. Against a server one
version behind it negotiates down once; outside the window it refuses loudly
and says which side to upgrade. Every Review feature it uses is gated on the
server's capability manifest: an unsupported read degrades with "this server
does not support X", and a write the server would apply partially is refused
before it is sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from _stub import StubServer
from lemoncrow_client import protocol
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode
from lemoncrow_client.protocol import (
    CAPTURE_OPTIONS,
    PREVIOUS_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    REVIEW_FEATURES,
    negotiate_protocol,
    parse_review_manifest,
    protocol_window,
    review_route_requirement,
)
from lemoncrow_client.session import RemoteSession


def _day(version: str, delta: int) -> str:
    return (date.fromisoformat(version) + timedelta(days=delta)).isoformat()


class _Server:
    """A handshake endpoint with a window, in either the current or the legacy refusal shape."""

    def __init__(self, *versions: str, legacy_refusal: bool = False) -> None:
        self.versions = versions
        self.legacy_refusal = legacy_refusal
        self.offers: list[str] = []

    def post(self, version: str) -> Mapping[str, Any]:
        self.offers.append(version)
        if version in self.versions:
            return {"protocol_version": version, "capabilities": ["blob_miss.v1"]}
        details: dict[str, Any] = {"client_protocol": version, "server_protocol": max(self.versions)}
        if not self.legacy_refusal:
            details["supported"] = sorted(self.versions)
        raise ClientError(
            ErrorCode.PROTOCOL_VERSION_MISMATCH,
            "client protocol is not supported by this server",
            details=details,
            action=AgentAction.UPGRADE_CLIENT,
        )


# --------------------------------------------------------------------------- #
# The window                                                                  #
# --------------------------------------------------------------------------- #


def test_the_window_is_n_and_n_minus_one_as_ordered_dates() -> None:
    assert protocol_window() == (PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION)
    assert date.fromisoformat(PREVIOUS_PROTOCOL_VERSION) < date.fromisoformat(PROTOCOL_VERSION)


def test_a_current_server_negotiates_n_in_one_round_trip() -> None:
    server = _Server(PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION)
    payload, version = negotiate_protocol(server.post)
    assert version == PROTOCOL_VERSION == payload["protocol_version"]
    assert server.offers == [PROTOCOL_VERSION]


@pytest.mark.parametrize("legacy_refusal", [False, True])
def test_a_server_one_version_behind_is_negotiated_down_once(legacy_refusal: bool) -> None:
    server = _Server(PREVIOUS_PROTOCOL_VERSION, _day(PREVIOUS_PROTOCOL_VERSION, -30), legacy_refusal=legacy_refusal)
    payload, version = negotiate_protocol(server.post)
    assert version == PREVIOUS_PROTOCOL_VERSION == payload["protocol_version"]
    assert server.offers == [PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION]


def test_a_server_older_than_the_window_is_refused_with_upgrade_the_server() -> None:
    older = _day(PREVIOUS_PROTOCOL_VERSION, -1)
    server = _Server(older, _day(older, -30))
    with pytest.raises(ClientError) as caught:
        negotiate_protocol(server.post)
    error = caught.value
    assert error.code is ErrorCode.PROTOCOL_VERSION_MISMATCH
    assert error.action is AgentAction.UPGRADE_SERVER
    assert "the server is older; upgrade the server" in error.message
    assert server.offers == [PROTOCOL_VERSION], "no futile retry outside the window"


def test_a_server_newer_than_the_window_is_refused_with_upgrade_the_client() -> None:
    newer = _day(PROTOCOL_VERSION, 1)
    server = _Server(_day(newer, 30), newer)
    with pytest.raises(ClientError) as caught:
        negotiate_protocol(server.post)
    assert caught.value.action is AgentAction.UPGRADE_CLIENT
    assert "upgrade the client" in caught.value.message
    assert server.offers == [PROTOCOL_VERSION]


def test_a_server_that_answers_a_version_it_was_not_offered_is_refused() -> None:
    def post(version: str) -> Mapping[str, Any]:
        return {"protocol_version": PREVIOUS_PROTOCOL_VERSION}

    with pytest.raises(ClientError) as caught:
        negotiate_protocol(post)
    assert caught.value.code is ErrorCode.PROTOCOL_VERSION_MISMATCH


def test_any_other_refusal_is_not_retried() -> None:
    calls: list[str] = []

    def post(version: str) -> Mapping[str, Any]:
        calls.append(version)
        raise ClientError(ErrorCode.CAPABILITY_UNSUPPORTED, "missing blob_miss.v1")

    with pytest.raises(ClientError) as caught:
        negotiate_protocol(post)
    assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert calls == [PROTOCOL_VERSION]


# --------------------------------------------------------------------------- #
# The manifest                                                                #
# --------------------------------------------------------------------------- #


def _manifest(**overrides: Any) -> dict[str, Any]:
    review: dict[str, Any] = {
        "manifest": 1,
        **dict.fromkeys(REVIEW_FEATURES, True),
        "schemas": {"packet": [2], "evidence": [1], "policy": [1], "record": [1]},
        "evidence_kinds": ["document", "screenshot"],
        "capture_modes": ["derived", "packet"],
        "capture_options": sorted(CAPTURE_OPTIONS),
    }
    review.update(overrides)
    return {"deployment": {"mode": "customer_hosted"}, "product_capabilities": {"review": review}}


def test_a_declared_manifest_is_read_as_published() -> None:
    manifest = parse_review_manifest(PROTOCOL_VERSION, _manifest(ask=False, capture_modes=["packet"]))
    assert manifest.declared
    assert manifest.protocol_version == PROTOCOL_VERSION
    assert manifest.supports("capture") and not manifest.supports("ask")
    assert manifest.capture_modes == frozenset({"packet"})
    assert manifest.schemas["packet"] == frozenset({2})
    assert manifest.evidence_kinds == frozenset({"document", "screenshot"})


def test_an_unsupported_feature_degrades_with_this_server_does_not_support() -> None:
    manifest = parse_review_manifest(PROTOCOL_VERSION, _manifest(ask=False))
    with pytest.raises(ClientError) as caught:
        manifest.require("ask")
    assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert caught.value.message.startswith("this server does not support Ask")
    assert caught.value.details == {"feature": "ask", "protocol_version": PROTOCOL_VERSION}


def test_a_feature_the_manifest_does_not_name_means_the_server_is_older() -> None:
    identity = _manifest()
    del identity["product_capabilities"]["review"]["evidence_upload"]
    with pytest.raises(ClientError) as caught:
        parse_review_manifest(PROTOCOL_VERSION, identity).require("evidence_upload")
    assert caught.value.action is AgentAction.UPGRADE_SERVER
    assert "upgrade the server" in caught.value.message


def test_a_current_server_without_a_manifest_does_not_serve_review() -> None:
    manifest = parse_review_manifest(PROTOCOL_VERSION, {"deployment": {"mode": "local"}, "product_capabilities": {}})
    assert manifest.declared
    with pytest.raises(ClientError, match="this server does not support Review capture"):
        manifest.require("capture")


def test_a_previous_protocol_server_gets_the_baseline_of_its_deployment() -> None:
    local = parse_review_manifest(
        PREVIOUS_PROTOCOL_VERSION,
        {"deployment": {"mode": "local"}, "product_capabilities": {"review": {"ask": False}}},
    )
    assert not local.declared
    assert local.supports("capture") and not local.supports("ask"), "what the old server published still wins"
    assert local.capture_modes == frozenset({"packet"})
    assert "review_scope" in local.capture_options
    hosted = parse_review_manifest(PREVIOUS_PROTOCOL_VERSION, {"deployment": {"mode": "managed_shared"}})
    assert hosted.supports("ask")
    assert hosted.capture_modes == frozenset({"derived", "packet"})
    assert hosted.capture_options == frozenset({"title", "restore_archived"})
    dashboard_only = parse_review_manifest(
        PREVIOUS_PROTOCOL_VERSION,
        {"deployment": {"mode": "local"}, "products": {"review": {"available": False}}},
    )
    assert not dashboard_only.supports("capture")


@pytest.mark.parametrize(
    ("version", "action"),
    [(1, AgentAction.UPGRADE_CLIENT), (3, AgentAction.UPGRADE_SERVER)],
)
def test_a_schema_the_server_does_not_understand_fails_closed(version: int, action: AgentAction) -> None:
    manifest = parse_review_manifest(PROTOCOL_VERSION, _manifest())
    manifest.require_schema("packet", 2)
    with pytest.raises(ClientError) as caught:
        manifest.require_schema("packet", version)
    assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert caught.value.action is action
    assert caught.value.details == {"schema": "packet", "version": version, "supported": [2]}


def test_an_empty_schema_set_means_the_server_does_not_serve_it() -> None:
    manifest = parse_review_manifest(
        PROTOCOL_VERSION, _manifest(schemas={"packet": [2], "evidence": [1], "policy": [1], "record": []})
    )
    with pytest.raises(ClientError, match="this server does not support Review records"):
        manifest.require_schema("record", 1)


def test_a_capture_the_server_would_apply_partially_is_refused() -> None:
    manifest = parse_review_manifest(PROTOCOL_VERSION, _manifest(capture_options=["title", "restore_archived"]))
    manifest.require_capture("packet", {"title": "t", "review_scope": "", "force_new_review": False})
    with pytest.raises(ClientError) as caught:
        manifest.require_capture("packet", {"review_scope": "agent-1", "continue_review_id": "r/1"})
    assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert caught.value.details["unsupported_options"] == ["continue_review_id", "review_scope"]
    assert "does not honor" in caught.value.message


def test_a_capture_mode_the_server_does_not_take_is_refused() -> None:
    manifest = parse_review_manifest(PROTOCOL_VERSION, _manifest(capture_modes=["packet"]))
    with pytest.raises(ClientError, match="this server does not support server-derived Review capture"):
        manifest.require_capture("derived", {})


#: One requested (non-default) value per capture option.
_REQUESTED_OPTIONS: dict[str, object] = {
    "title": "a title",
    "restore_archived": True,
    "force_new_review": True,
    "continue_review_id": "r/1",
    "reuse_closed_review": True,
    "review_scope": "agent-1",
    "workspace_branch": "main",
    "capture_intent": "intent-1",
    "with_workspace": True,
    "workspace_root": "/projects",
}

#: What counts as requesting an option (the server's table in
#: ``server/tests/test_server_protocol_window.py`` must agree row for row).
_OPTION_VALUES: list[tuple[object, bool]] = [
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
    assert set(_REQUESTED_OPTIONS) == CAPTURE_OPTIONS


@pytest.mark.parametrize("option", sorted(CAPTURE_OPTIONS))
def test_each_capture_option_the_server_does_not_honor_is_refused(option: str) -> None:
    """Every option, one at a time: dropping any single one from the gate fails here."""

    honored = sorted(CAPTURE_OPTIONS - {option})
    manifest = parse_review_manifest(PROTOCOL_VERSION, _manifest(capture_options=honored))
    manifest.require_capture("packet", {name: _REQUESTED_OPTIONS[name] for name in honored})
    with pytest.raises(ClientError) as caught:
        manifest.require_capture("packet", {option: _REQUESTED_OPTIONS[option]})
    assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert caught.value.action is AgentAction.UPGRADE_SERVER
    assert caught.value.details == {"unsupported_options": [option], "honored": honored}


@pytest.mark.parametrize(("value", "requested"), _OPTION_VALUES, ids=[repr(value) for value, _ in _OPTION_VALUES])
def test_only_a_non_default_value_requests_an_option(value: object, requested: bool) -> None:
    manifest = parse_review_manifest(PROTOCOL_VERSION, _manifest(capture_options=[]))
    if requested:
        with pytest.raises(ClientError, match="does not honor"):
            manifest.require_capture("packet", {"review_scope": value})
    else:
        manifest.require_capture("packet", {"review_scope": value})


@pytest.mark.parametrize(
    ("served", "readable", "action"),
    [
        pytest.param([2], frozenset({1}), AgentAction.UPGRADE_CLIENT, id="server-newer"),
        pytest.param([1], frozenset({2}), AgentAction.UPGRADE_SERVER, id="server-older"),
    ],
)
def test_a_schema_the_client_and_server_share_no_version_of_is_refused(
    monkeypatch: pytest.MonkeyPatch, served: list[int], readable: frozenset[int], action: AgentAction
) -> None:
    monkeypatch.setattr(protocol, "CLIENT_REVIEW_SCHEMAS", {**protocol.CLIENT_REVIEW_SCHEMAS, "evidence": readable})
    manifest = parse_review_manifest(
        PROTOCOL_VERSION, _manifest(schemas={"packet": [2], "evidence": served, "policy": [1], "record": [1]})
    )
    requirement = review_route_requirement("GET", "/api/evidence/ev1/content")
    assert requirement is not None
    with pytest.raises(ClientError) as caught:
        manifest.require_route(requirement)
    assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert caught.value.action is action
    assert caught.value.details == {"schema": "evidence", "served": served, "readable": sorted(readable)}


def test_a_schema_with_one_shared_version_is_read() -> None:
    manifest = parse_review_manifest(
        PROTOCOL_VERSION, _manifest(schemas={"packet": [2], "evidence": [1, 2], "policy": [1], "record": [1]})
    )
    requirement = review_route_requirement("GET", "/api/evidence/ev1/content")
    assert requirement is not None
    manifest.require_route(requirement)


def test_an_evidence_kind_the_server_does_not_store_is_refused_and_a_listed_one_is_allowed() -> None:
    manifest = parse_review_manifest(PROTOCOL_VERSION, _manifest(evidence_kinds=["document", "screenshot"]))
    listed = review_route_requirement("POST", "/api/reviews/r1/evidence/upload?kind=document&title=x")
    unlisted = review_route_requirement("POST", "/api/reviews/r1/evidence/upload?kind=sbom&title=x")
    assert listed is not None and listed.evidence_kind == "document"
    assert unlisted is not None and unlisted.evidence_kind == "sbom"
    manifest.require_route(listed)
    with pytest.raises(ClientError) as caught:
        manifest.require_route(unlisted)
    assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert caught.value.details == {"evidence_kind": "sbom", "supported": ["document", "screenshot"]}
    assert "does not support 'sbom' evidence" in caught.value.message


def test_an_upload_of_an_unlisted_evidence_kind_never_reaches_the_server(
    stub: StubServer, config: ClientConfig
) -> None:
    session = RemoteSession(config)
    try:
        with pytest.raises(ClientError, match="does not support 'sbom' evidence"):
            session.review_reader_request("POST", "/api/reviews/r1/evidence/upload?kind=sbom", body={"a": 1})
        assert stub.state.reader_requests == []
        session.review_reader_request("POST", "/api/reviews/r1/evidence/upload?kind=document", body={"a": 1})
        assert stub.state.reader_requests == [("POST", "/api/reviews/r1/evidence/upload?kind=document")]
    finally:
        session.close()


@pytest.mark.parametrize(
    ("method", "path", "feature", "schema"),
    [
        ("POST", "/api/reviews/r1/ask", "ask", None),
        ("GET", "/api/reviews/r1/ask", "ask", None),
        ("GET", "/api/evidence/ev1/content", "evidence_read", "evidence"),
        ("GET", "/api/reviews/r1/evidence?revision_id=x", "evidence_read", "evidence"),
        ("POST", "/api/reviews/r1/evidence/upload?kind=document", "evidence_upload", "evidence"),
        ("POST", "/api/reviews/r1/evidence/link", "evidence_upload", "evidence"),
        ("GET", "/api/reviews/r1/record", None, "record"),
        ("GET", "/api/reviews/r1/records/rec1/verify", None, "record"),
        ("GET", "/api/reviews/r1/readiness", None, "policy"),
    ],
)
def test_review_api_routes_name_what_they_need(method: str, path: str, feature: str | None, schema: str | None) -> None:
    requirement = review_route_requirement(method, path)
    assert requirement is not None
    assert (requirement.feature, requirement.schema) == (feature, schema)


@pytest.mark.parametrize("path", ["/api/reviews/r1", "/api/reviews/r1/annotations", "/api/reviews/r1/askew"])
def test_routes_every_server_serves_need_nothing(path: str) -> None:
    assert review_route_requirement("GET", path) is None


# --------------------------------------------------------------------------- #
# Wired into the sessions                                                     #
# --------------------------------------------------------------------------- #


def test_a_session_records_the_negotiated_version_and_manifest(stub: StubServer, config: ClientConfig) -> None:
    session = RemoteSession(config)
    try:
        session.handshake()
        assert session.state.protocol_version == PROTOCOL_VERSION
        assert session.state.review_manifest is not None and session.state.review_manifest.declared
    finally:
        session.close()


def test_a_session_against_a_previous_protocol_server_keeps_that_version(
    stub: StubServer, config: ClientConfig
) -> None:
    """The negotiated version, not the newest, is what the session then registers with."""

    stub.state.protocol_version = PREVIOUS_PROTOCOL_VERSION
    stub.state.review_manifest = None
    report = RemoteSession(config).bootstrap(deadline_s=5.0)
    assert report.ok, report.reason
    assert stub.state.session_id == "sess_stub"


def _packet(schema_version: int) -> dict[str, Any]:
    return {"schema_version": schema_version, "files": []}


def test_a_packet_the_server_does_not_understand_is_never_sent(stub: StubServer, bootstrapped: RemoteSession) -> None:
    with pytest.raises(ClientError) as caught:
        bootstrapped.capture_review(packet=_packet(99), new_blobs={}, source_ref="review:test")
    assert caught.value.action is AgentAction.UPGRADE_SERVER
    assert stub.state.review_requests == []


def test_a_capture_option_the_server_does_not_honor_is_never_sent(stub: StubServer, config: ClientConfig) -> None:
    stub.state.review_manifest = _manifest(capture_options=["title", "restore_archived"])["product_capabilities"][
        "review"
    ]
    session = RemoteSession(config)
    try:
        assert session.bootstrap(deadline_s=5.0).ok
        with pytest.raises(ClientError, match="does not honor"):
            session.capture_review(packet=_packet(2), new_blobs={}, source_ref="review:test", review_scope="agent-1")
        assert stub.state.review_requests == []
    finally:
        session.close()


def test_a_reader_request_for_an_unsupported_feature_never_reaches_the_server(
    stub: StubServer, config: ClientConfig
) -> None:
    stub.state.review_manifest = _manifest(ask=False)["product_capabilities"]["review"]
    session = RemoteSession(config)
    try:
        with pytest.raises(ClientError, match="this server does not support Ask"):
            session.review_reader_request("POST", "/api/reviews/r1/ask", body={"body": "why?"})
        assert session.review_reader_request("GET", "/api/reviews/r1")["path"] == "/api/reviews/r1"
        assert [path for _method, path in stub.state.reader_requests] == ["/api/reviews/r1"]
    finally:
        session.close()


def test_a_reader_request_against_a_previous_protocol_server_uses_its_baseline(
    stub: StubServer, config: ClientConfig
) -> None:
    """No manifest route: the handshake says N-1 and the baseline serves Ask."""

    stub.state.protocol_version = PREVIOUS_PROTOCOL_VERSION
    stub.state.review_manifest = None
    session = RemoteSession(config)
    try:
        assert session.review_reader_request("GET", "/api/reviews/r1/ask")["path"] == "/api/reviews/r1/ask"
        assert session.state.protocol_version == PREVIOUS_PROTOCOL_VERSION
        assert session.state.review_manifest is not None and not session.state.review_manifest.declared
    finally:
        session.close()


def test_the_review_capture_session_gates_on_the_manifest(
    stub: StubServer, config: ClientConfig, tmp_path: Path
) -> None:
    from lemoncrow_client.review_session import ReviewSession

    stub.state.review_manifest = _manifest(capture_modes=["packet"])["product_capabilities"]["review"]
    head = tmp_path / "head"
    head.mkdir()
    (head / "a.txt").write_text("a\n", encoding="utf-8")
    session = ReviewSession(config)
    try:
        session.handshake(timeout_s=5.0)
        session.open_session(timeout_s=5.0)
        session.open_snapshot(head, source_revision="head", timeout_s=5.0)
        with pytest.raises(ClientError, match="this server does not support server-derived Review capture"):
            session.capture_from_views(range_spec={"mode": "working_tree"}, source_ref="review:test")
        assert stub.state.review_requests == []
    finally:
        session.close()
