"""G7-FLOW: the shared data-flow projection and ``lc review data-flow``.

Every policy input flips its row; a denial never shows as allowed, including a
denial from a higher layer than the allow; unknowns are explicit rows; and
environment values never reach the output (names only).
"""

from __future__ import annotations

import json
import random
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.model_setup.models import CustomEndpoint, CustomModel
from lemoncrow.pro.capabilities.review import capability_catalog as catalog
from lemoncrow.pro.capabilities.review.capability_catalog import DETECTION_FACTS, DetectedCapabilities
from lemoncrow.pro.capabilities.review.data_flow import (
    DataFlowInputs,
    DataFlowProjection,
    EvidenceFeed,
    FlowOrigin,
    HostedFacts,
    OperatorPolicy,
    OperatorSetting,
    RouteFact,
    build_data_flow,
    environment_route_facts,
    render_data_flow_text,
)
from lemoncrow.pro.capabilities.review.data_governance import (
    NOT_CONFIGURED,
    DataGovernanceAttestations,
    RouteAttestation,
)
from lemoncrow.pro.capabilities.review.outcomes.deployments import DeploymentSettings
from lemoncrow.pro.capabilities.review.outcomes.observability import (
    ConnectorConfig,
    CredentialRef,
    ObservabilitySettings,
)
from lemoncrow.pro.capabilities.review.run_plan import (
    PlanInputs,
    RunSettingsLayer,
    SettingOrigin,
    build_run_plan,
    parse_review_config_layer,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.run_plan_service import effective_policy_layers, resolve_execution_loci
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

CANARY = "canary-7f3a9c41e2"
_ROUTE_ENV = (
    "LEMONCROW_LLM_BACKEND",
    "LEMONCROW_OPENAI_MODEL",
    "LEMONCROW_LITELLM_MODEL",
    "LEMONCROW_OLLAMA_MODEL",
    "LEMONCROW_REVIEW_REVIEWER_ROUTES",
    "LEMONCROW_DATA_GOVERNANCE_FILE",
)


@pytest.fixture(autouse=True)
def _clean_route_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ROUTE_ENV:
        monkeypatch.delenv(name, raising=False)


def _config(text: str | None) -> TrustedBaseDocument | None:
    if text is None:
        return None
    return TrustedBaseDocument(
        path=".lemoncrow/review.yaml",
        content=text,
        state="present" if text else "absent",
        source="trusted base HEAD .lemoncrow/review.yaml",
    )


def _feeds(
    deployments: DeploymentSettings | None = None, observability: ObservabilitySettings | None = None
) -> dict[str, EvidenceFeed]:
    return {
        "deployments": EvidenceFeed(
            deployments or DeploymentSettings(source="local_operator_policy"),
            (FlowOrigin("review-deployments.json", "local_operator_policy"),),
        ),
        "observability": EvidenceFeed(
            observability or ObservabilitySettings(source="local_operator_policy", locus="local"),
            (FlowOrigin("review-observability.json", "local_operator_policy"),),
        ),
    }


def _route(key: str = "primary", *, driver: str = "openai", ask: bool = True, locus: str = "local") -> RouteFact:
    from lemoncrow.pro.capabilities.review.rules import model_route_trust

    return RouteFact(
        key,
        f"route {key}",
        "configured",
        (FlowOrigin("LEMONCROW_LLM_BACKEND", "environment"),),
        driver=driver,
        model="gpt-4o-mini",
        trust=model_route_trust(driver),
        locus=locus,
        ask=ask,
    )


def _effective(**extra: Any) -> dict[str, Any]:
    """An operator effective Review policy document shaped like ``ReviewReaderBridge.effective_review_policy``."""
    return {
        "behavior": {"effort": {"effective_value": "auto", "origin": {"layer": "built_in_default"}}},
        "trusted_base": {"policy_state": "final"},
        "rules": [],
        "ai_content_access": [],
        "allow_untrusted_runtime_execution_approval": None,
        "run_plan": {"layers": [], "allowed_run_on": ["managed"], "default_run_on": "managed"},
        **extra,
    }


def _operator(effective: dict[str, Any] | None = None, **kwargs: Any) -> OperatorPolicy:
    return OperatorPolicy(
        effective=effective if effective is not None else _effective(),
        source=FlowOrigin("LEMONCROW_SERVER_REVIEW_POLICY_FILE", "deployment_environment"),
        **kwargs,
    )


def _inputs(*, config: str | None = "", **overrides: Any) -> DataFlowInputs:
    values: dict[str, Any] = {
        "surface": "local",
        "repository": "repo",
        "trusted_base": _config(config),
        "routes": (_route(),),
        **_feeds(),
    }
    values.update(overrides)
    return DataFlowInputs(**values)


def _row(projection: DataFlowProjection, row_id: str) -> tuple[str, str] | None:
    row = projection.row(row_id)
    return None if row is None else (row.status, row.detail)


_OFF_FAMILIES = "review:\n  capabilities: {checks: off, behavior_diff: off, visual: off, deep_runtime: off}\n"
_ENDPOINT = CustomEndpoint("lab", "https://models.example.com/v1", locus="remote", models=(CustomModel("qwen-coder"),))
_OBSERVABILITY = ObservabilitySettings(
    connectors=(
        ConnectorConfig("prometheus", "prom", "https://prom.example.com", credential=CredentialRef("PROM_TOKEN")),
    ),
    source="local_operator_policy",
    locus="local",
)


_HOSTED = HostedFacts(("S",), ("S",), 90, 604800.0, "R", "B")
_ATTESTED = DataGovernanceAttestations(
    "attested",
    storage_region="eu-central-1",
    egress="proxy_only",
    routes=(
        ("claude", RouteAttestation(training="no_training", retention_days=0)),
        ("codex", RouteAttestation(training="may_train")),
        ("litellm", RouteAttestation(trust="customer_local", provider="acme-vllm")),
        (
            "openai",
            RouteAttestation(
                provider="azure-openai", data_region="eu-west-1", training="no_training", retention_days=30
            ),
        ),
    ),
)
_UNREADABLE = DataGovernanceAttestations(
    "unreadable",
    reason="LEMONCROW_DATA_GOVERNANCE_FILE could not be read (ValueError); error text withheld: it may quote the file",
)


def _with_operator(**effective: Any) -> Callable[[bool], DataFlowInputs]:

    def make(on: bool) -> DataFlowInputs:
        return _inputs(operator=_operator(_effective(**effective if on else {})))

    return make


FLIPS: dict[str, tuple[str, Callable[[bool], DataFlowInputs], tuple[str, str]]] = {
    "trusted base readable": (
        "policy.trusted_base",
        lambda on: _inputs(config="" if on else None, trusted_base_reason="no commit"),
        ("unknown", "fact"),
    ),
    "capabilities.ai": (
        "intelligence.ai",
        lambda on: _inputs(config="review:\n  capabilities: {ai: off}\n" if on else ""),
        ("allowed", "denied"),
    ),
    "ai_content_access": (
        "intelligence.route.primary",
        lambda on: _inputs(config="ai_content_access:\n  - {mode: ai_denied, paths: ['**']}\n" if on else ""),
        ("allowed", "denied"),
    ),
    "ai_content_access (path scoped)": (
        "content.execution_evidence",
        lambda on: _inputs(
            config="ai_content_access:\n  - {mode: customer_local_only, paths: [secrets/]}\n" if on else ""
        ),
        ("allowed", "restricted"),
    ),
    "runtime capability families": (
        "execution.reviewed_code",
        lambda on: _inputs(config=_OFF_FAMILIES if on else ""),
        ("allowed", "denied"),
    ),
    "execution.deny_all": (
        "execution.dependency_fetch",
        lambda on: _inputs(config="execution:\n  deny_all: true\n" if on else ""),
        ("allowed", "denied"),
    ),
    "unwired runtime locus": (
        "execution.runtime_locus",
        _with_operator(
            run_plan={
                "layers": [],
                "allowed_run_on": ["managed"],
                "unavailable_run_on_by_class": {"runtime": {"managed": "managed sandbox is not enabled"}},
            }
        ),
        ("fact", "off"),
    ),
    "untrusted execution approval": (
        "execution.untrusted_code",
        lambda on: _inputs(
            operator=_operator(untrusted_execution=OperatorSetting(True if on else None, "organization"))
        ),
        ("denied", "allowed"),
    ),
    "LEMONCROW_LLM_BACKEND": (
        "intelligence.route.primary",
        lambda on: _inputs(
            routes=(
                (_route(),)
                if on
                else environment_route_facts(layer="environment", agent_cli_allowed=True, locus="local")
            )
        ),
        ("off", "allowed"),
    ),
    "agent CLI host setting": (
        "intelligence.agent_cli",
        lambda on: _inputs(agent_cli_allowed=not on),
        ("allowed", "denied"),
    ),
    "agent CLI host setting (web)": ("web.ask", lambda on: _inputs(agent_cli_allowed=not on), ("allowed", "denied")),
    "deployment connector": (
        "evidence.deployments",
        lambda on: _inputs(
            **_feeds(deployments=DeploymentSettings(connector="github_deployments", source="x") if on else None)
        ),
        ("off", "allowed"),
    ),
    "observability connector": (
        "evidence.observability",
        lambda on: _inputs(**_feeds(observability=_OBSERVABILITY if on else None)),
        ("off", "allowed"),
    ),
    "proxy setting": (
        "network.proxy",
        lambda on: _inputs(environment_names=frozenset({"HTTPS_PROXY"} if on else ())),
        ("not_set", "set"),
    ),
    "custom CA setting": (
        "network.custom_ca",
        lambda on: _inputs(environment_names=frozenset({"SSL_CERT_FILE"} if on else ())),
        ("not_set", "set"),
    ),
    "organization AI block": (
        "intelligence.ai",
        _with_operator(organization_block={"ai": True, "execution": False}),
        ("allowed", "denied"),
    ),
    "organization execution block": (
        "execution.reviewed_code",
        _with_operator(organization_block={"ai": False, "execution": True}),
        ("allowed", "denied"),
    ),
    "organization block row": (
        "policy.organization_block",
        _with_operator(organization_block={"ai": True}),
        ("off", "denied"),
    ),
    "hosted storage region": (
        "source.region",
        lambda on: _inputs(hosted=HostedFacts((), ("S",), 90, 604800.0, "R", "B") if on else None),
        ("fact", "unknown"),
    ),
    "attested storage region": (
        "source.region",
        lambda on: _inputs(hosted=_HOSTED, governance=_ATTESTED if on else NOT_CONFIGURED),
        ("unknown", "fact"),
    ),
    "attested provider terms": (
        "models.terms.primary",
        lambda on: _inputs(governance=_ATTESTED if on else NOT_CONFIGURED),
        ("unknown", "set"),
    ),
    "attested provider data region": (
        "models.locus.primary",
        lambda on: _inputs(governance=_ATTESTED if on else NOT_CONFIGURED),
        ("unknown", "fact"),
    ),
    "attested agent CLI terms": (
        "models.terms.agent_cli.claude",
        lambda on: _inputs(governance=_ATTESTED if on else NOT_CONFIGURED),
        ("unknown", "set"),
    ),
    "attested egress": (
        "network.egress",
        lambda on: _inputs(governance=_ATTESTED if on else NOT_CONFIGURED),
        ("unknown", "restricted"),
    ),
    "data-governance document": (
        "policy.data_governance",
        lambda on: _inputs(governance=_ATTESTED if on else NOT_CONFIGURED),
        ("not_set", "set"),
    ),
    "unreadable data-governance document": (
        "policy.data_governance",
        lambda on: _inputs(governance=_UNREADABLE if on else _ATTESTED),
        ("set", "unknown"),
    ),
}


def test_data_flow_policy_precedence_contract() -> None:
    """case: test_flipping_each_policy_input_flips_its_row"""
    for name in sorted(FLIPS):
        row_id, make, (off, on) = FLIPS[name]
        before, after = (build_data_flow(make(False)).row(row_id), build_data_flow(make(True)).row(row_id))
        assert before is not None and after is not None
        assert (before.status, after.status) == (off, on)
        assert after.origins and before.origins
    "case: test_flipping_a_valued_input_changes_its_row_detail"
    for name, row_id, make, expect in [
        (
            "execution.retention_days",
            "retention.run_artifacts",
            lambda on: _inputs(config="execution:\n  retention_days: 3\n" if on else ""),
            ("14 days", "3 days"),
        ),
        (
            "run_on",
            "execution.runtime_locus",
            lambda on: _inputs(
                config="review:\n  run_on: worker\n" if on else "",
                operator=_operator(_effective(run_plan={"layers": [], "allowed_run_on": ["local", "worker"]})),
            ),
            ("(local)", "(worker)"),
        ),
        (
            "deployment AI locus",
            "execution.ai_locus",
            _with_operator(
                run_plan={"layers": [], "allowed_run_on": ["local"], "allowed_run_on_by_class": {"ai": ["worker"]}}
            ),
            ("(managed)", "(worker)"),
        ),
        (
            "hosted storage settings",
            "source.storage",
            lambda on: _inputs(hosted=HostedFacts(("S",) if on else (), ("S",), 90, 604800.0, "R", "B")),
            ("none of the storage settings", "configured by S"),
        ),
    ]:
        before, after = (_row(build_data_flow(make(False)), row_id), _row(build_data_flow(make(True)), row_id))
        assert before is not None and after is not None, name
        assert expect[0] in before[1] and expect[1] in after[1], (before, after)
    "case: test_higher_layer_capability_denial_beats_a_lower_layer_allow"
    org_always = _effective(
        run_plan={
            "layers": [
                {
                    "origin": {"layer": "organization", "source": "operator_review_policy"},
                    "settings": {"capabilities": {"ai": "always"}},
                }
            ],
            "allowed_run_on": ["local"],
        }
    )
    allowed = build_data_flow(_inputs(operator=_operator(org_always)))
    ai = allowed.row("intelligence.ai")
    assert ai is not None and ai.status == "allowed" and (ai.origins[0].layer == "organization")
    projection = build_data_flow(
        _inputs(
            config="review:\n  capabilities: {ai: off}\n",
            operator=_operator(org_always),
            routes=(_route(), _route("reviewer.1", ask=False)),
        )
    )
    ai = projection.row("intelligence.ai")
    assert ai is not None and ai.status == "denied"
    assert ai.origins == (FlowOrigin(".lemoncrow/review.yaml review.capabilities.ai", "trusted_base_repository"),)
    reviewer = projection.row("intelligence.route.reviewer.1")
    assert reviewer is not None and reviewer.status == "denied" and (reviewer.reason == "capabilities.ai is off")
    primary = projection.row("intelligence.route.primary")
    assert primary is not None and "automatic AI review" not in primary.detail and ("used for Ask" in primary.detail)
    "case: test_higher_layer_content_denial_beats_a_lower_layer_allow"
    org_allows = _effective(ai_content_access=[{"mode": "external_allowed", "paths": ["**"]}])
    allowed = build_data_flow(
        _inputs(
            operator=_operator(
                org_allows, content_access_layers=(("organization", ({"mode": "external_allowed", "paths": ["**"]},)),)
            )
        )
    )
    assert allowed.row("intelligence.route.primary").status == "allowed"
    denied = build_data_flow(
        _inputs(config="ai_content_access:\n  - {mode: ai_denied, paths: ['**']}\n", operator=_operator(org_allows))
    )
    for row_id in ("intelligence.route.primary", "intelligence.agent_cli", "intelligence.ask_models", "web.ask"):
        row = denied.row(row_id)
        assert row is not None and row.status == "denied", row_id
    primary = denied.row("intelligence.route.primary")
    assert primary is not None
    assert FlowOrigin(".lemoncrow/review.yaml ai_content_access", "trusted_base_repository") in primary.origins
    assert denied.row("content.execution_evidence").status == "denied"
    "case: test_lower_layer_content_denial_is_not_loosened_by_a_higher_allow"
    raw = {"mode": "customer_local_only", "paths": ["**"]}
    projection = build_data_flow(
        _inputs(
            config="ai_content_access:\n  - {mode: external_allowed, paths: ['**']}\n",
            operator=_operator(_effective(ai_content_access=[raw]), content_access_layers=(("organization", (raw,)),)),
            routes=(
                _route(),
                replace(_route("local", driver="ollama"), trust="customer_local"),
                _route("unattested", driver="ollama"),
            ),
        )
    )
    external = projection.row("intelligence.route.primary")
    assert external is not None and external.status == "denied"
    assert FlowOrigin("review policy ai_content_access", "organization") in external.origins
    local = projection.row("intelligence.route.local")
    assert local is not None and local.status == "allowed"
    unattested = projection.row("intelligence.route.unattested")
    assert unattested is not None and unattested.status == "denied"
    "case: test_an_operator_rule_no_layer_names_cites_the_operator_policy_not_a_guessed_layer"
    raw = {"mode": "ai_denied", "paths": ["**"]}
    row = build_data_flow(_inputs(operator=_operator(_effective(ai_content_access=[raw])))).row(
        "intelligence.route.primary"
    )
    assert row is not None and row.status == "denied"
    assert FlowOrigin("LEMONCROW_SERVER_REVIEW_POLICY_FILE", "deployment_environment") in row.origins
    assert all(origin.layer != "organization" for origin in row.origins)
    "case: test_a_route_a_whole_repository_rule_blocks_is_never_allowed"
    from lemoncrow.pro.capabilities.review.run_evidence import access_allows

    rng = random.Random(7)
    for _ in range(300):
        org = [
            {"mode": rng.choice(_MODES), "paths": [rng.choice(["**", "*", "src/**"])]} for _ in range(rng.randint(0, 2))
        ]
        base = [(rng.choice(_MODES), rng.choice(["**", "docs/"])) for _ in range(rng.randint(0, 2))]
        config = "".join((f"  - {{mode: {mode}, paths: ['{path}']}}\n" for mode, path in base))
        routes = tuple((_route(f"r{index}", driver=driver) for index, driver in enumerate(_DRIVERS)))
        projection = build_data_flow(
            _inputs(
                config=f"ai_content_access:\n{config}" if config else "",
                operator=_operator(_effective(ai_content_access=org)),
                routes=routes,
            )
        )
        whole = [row["mode"] for row in org if row["paths"][0] in {"**", "*"}] + [m for m, p in base if p == "**"]
        for fact in routes:
            row = projection.row(f"intelligence.route.{fact.key}")
            assert row is not None
            if any(not access_allows(mode, fact.trust) for mode in whole):
                assert row.status == "denied", (org, base, fact.driver, row)
                continue
            assert row.status != "denied", (org, base, fact.driver, row)
            if row.status == "allowed":
                assert all(access_allows(row["mode"], fact.trust) for row in org)
                assert all((access_allows(mode, fact.trust) for mode, _path in base))
    "case: test_a_whole_repository_denial_beats_an_unknown_trusted_base"
    raw = {"mode": "ai_denied", "paths": ["**"]}
    operator = _operator(_effective(ai_content_access=[raw]), content_access_layers=(("organization", (raw,)),))
    projection = build_data_flow(_inputs(config=None, trusted_base_reason="no commit", operator=operator))
    for row_id in ("intelligence.route.primary", "intelligence.ask_models", "content.execution_evidence"):
        row = projection.row(row_id)
        assert row is not None and row.status == "denied", row_id


_PERMISSIVE_BASE = (
    "review:\n  effort: deep\n  run_on: worker\n"
    "  capabilities: {ai: always, checks: always, behavior_diff: always}\n"
    "ai_content_access:\n  - {mode: external_allowed, paths: ['**']}\n"
)


@pytest.mark.parametrize("base", [_PERMISSIVE_BASE, None], ids=["permissive trusted base", "unknown trusted base"])
def test_the_organization_block_beats_every_lower_layer(base: str | None) -> None:
    run_plan = {"layers": [], "allowed_run_on": ["managed", "worker"], "default_run_on": "managed"}
    effective = _effective(run_plan=run_plan, organization_block={"ai": True, "execution": True})
    projection = build_data_flow(
        _inputs(
            config=base,
            trusted_base_reason="no commit",
            operator=_operator(effective, untrusted_execution=OperatorSetting(True, "repository")),
            routes=(_route("primary", driver="ollama", locus="managed"), _route("worker", ask=False, locus="worker")),
        )
    )
    ai_reason = "blocked by organization policy (execution_block.ai)"
    execution_reason = "blocked by organization policy (execution_block.execution)"
    for row_id, status, reason in (
        ("policy.organization_block", "denied", f"{ai_reason}; {execution_reason}"),
        ("intelligence.ai", "denied", ai_reason),
        ("intelligence.route.primary", "denied", ai_reason),
        ("intelligence.route.worker", "denied", ai_reason),
        ("intelligence.agent_cli", "denied", ai_reason),
        ("intelligence.ask_models", "denied", ai_reason),
        ("content.execution_evidence", "denied", ""),
        ("execution.ai_locus", "off", ai_reason),
        ("execution.runtime_locus", "off", execution_reason),
        ("execution.reviewed_code", "denied", execution_reason),
        ("execution.untrusted_code", "denied", execution_reason),
        ("web.ask", "denied", "follows intelligence.agent_cli"),
    ):
        row = projection.row(row_id)
        assert row is not None, row_id
        assert (row.status, row.reason) == (status, reason), row_id
    block = projection.row("policy.organization_block")
    assert block is not None and set(block.origins) == {
        FlowOrigin("review policy execution_block.ai", "organization"),
        FlowOrigin("review policy execution_block.execution", "organization"),
    }
    blocked_rule = next(row for row in projection.rows if row.detail == "ai_denied (organization AI block)")
    assert blocked_rule.origins == (FlowOrigin("review policy execution_block.ai", "organization"),)


def test_a_malformed_organization_block_blocks_both_kinds_without_echoing_it() -> None:
    effective = _effective(organization_block={"ai": CANARY})
    projection = build_data_flow(_inputs(operator=_operator(effective)))
    for row_id in ("policy.organization_block", "intelligence.ai", "execution.reviewed_code"):
        assert projection.row(row_id).status == "denied", row_id  # type: ignore[union-attr]
    assert CANARY not in json.dumps(projection.to_wire()) + render_data_flow_text(projection)


def test_without_an_operator_layer_nothing_is_blocked_above_the_repository() -> None:
    row = build_data_flow(_inputs()).row("policy.organization_block")
    assert row is not None and row.status == "off"
    assert row.origins == (FlowOrigin("execution_block", "built_in_default"),)


def test_data_flow_route_contract() -> None:
    """case: test_ask_model_choice_shows_its_strictest_driver"""
    config = "ai_content_access:\n  - {mode: customer_local_only, paths: ['**']}\n"
    row = build_data_flow(_inputs(config=config)).row("intelligence.ask_models")
    assert row is not None and row.status == "denied"
    assert "ollama (unknown: denied)" in row.detail and "openai (external: denied)" in row.detail
    # The one-line attestation makes the ollama route customer_local.
    attested = DataGovernanceAttestations("attested", routes=(("ollama", RouteAttestation(trust="customer_local")),))
    row = build_data_flow(_inputs(config=config, governance=attested)).row("intelligence.ask_models")
    assert row is not None and row.status == "denied"
    assert "ollama (customer_local: allowed)" in row.detail and "openai (external: denied)" in row.detail
    "case: test_a_route_whose_automatic_use_is_unknown_is_never_allowed"
    from lemoncrow.pro.capabilities.review.data_flow import _Access, _route_row, _Rows

    out = _Rows()
    origin = FlowOrigin("review.capabilities.ai", "built_in_default")
    _route_row(out, _route(), _Access((), "", origin), ai_status="unknown", ai_locus="local", ai_origin=origin)
    assert [(row.status, row.reason) for row in out.rows] == [
        ("unknown", "whether automatic AI review runs is unknown")
    ]
    "case: test_a_route_on_another_locus_is_off_for_automatic_review"
    projection = build_data_flow(_inputs(routes=(_route(), _route("worker", ask=False, locus="worker"))))
    row = projection.row("intelligence.route.worker")
    assert row is not None and row.status == "off"
    assert row.reason == "automatic AI work runs on local, not worker"
    "case: test_deny_all_is_named_as_the_reason_no_registry_is_reachable"
    row = build_data_flow(_inputs(config="execution:\n  deny_all: true\n")).row("execution.dependency_fetch")
    assert row is not None and row.status == "denied" and row.detail.startswith("execution.deny_all")
    assert row.origins == (FlowOrigin(".lemoncrow/review.yaml execution.deny_all", "trusted_base_repository"),)
    "case: test_unattested_provider_terms_regions_and_egress_stay_unknown"
    projection = build_data_flow(_inputs(endpoints=(_ENDPOINT,), hosted=_HOSTED))
    for row_id in (
        "models.terms.endpoint.lab",
        "models.terms.primary",
        "models.terms.agent_cli.claude",
        "models.terms.agent_cli.codex",
        "models.terms.agent_cli.opencode",
    ):
        row = projection.row(row_id)
        assert row is not None, row_id
        assert (row.status, row.detail) == ("unknown", "training: unknown; retention: unknown"), row_id
        assert "LEMONCROW_DATA_GOVERNANCE_FILE is not set" in row.reason, row_id
    # Neither a driver name nor a hostname proves where a provider keeps data.
    locus = projection.row("models.locus.primary")
    assert locus is not None and locus.status == "unknown"
    assert locus.detail == "external (by driver); data region unknown"
    endpoint = projection.row("models.locus.endpoint.lab")
    assert endpoint is not None and (endpoint.status, endpoint.detail) == ("unknown", "")
    assert (
        endpoint.reason
        == "no operator attestation says where the litellm route sends content; a hostname is never proof"
    )
    for row_id in ("source.region", "network.egress"):
        assert projection.row(row_id).status == "unknown", row_id  # type: ignore[union-attr]
    assert projection.row("policy.data_governance").status == "not_set"  # type: ignore[union-attr]


def test_attested_governance_fills_terms_locus_region_and_egress_with_their_origins() -> None:
    routes = (_route(), _route("litellm", driver="litellm", ask=False), _route("w", ask=False, locus="worker"))
    projection = build_data_flow(
        _inputs(routes=routes, endpoints=(_ENDPOINT,), hosted=_HOSTED, governance=_ATTESTED, environment_layer="E")
    )
    terms = projection.row("models.terms.primary")
    assert terms is not None and (terms.status, terms.detail) == ("set", "training: no_training; retention: 30 days")
    assert FlowOrigin("LEMONCROW_DATA_GOVERNANCE_FILE model_routes.openai", "E") in terms.origins
    locus = projection.row("models.locus.primary")
    assert locus is not None and locus.status == "fact"
    assert locus.detail == "external (by driver); provider azure-openai; data region eu-west-1"
    # A custom endpoint is reached through the litellm route, so it carries litellm's attestation.
    endpoint = projection.row("models.locus.endpoint.lab")
    assert endpoint is not None and endpoint.status == "fact"
    assert endpoint.detail == "customer_local (operator attested); provider acme-vllm; data region unknown"
    lab = projection.row("models.endpoint.lab")
    assert lab is not None and lab.detail.endswith("(content access: customer_local trust)")
    # A customer worker route is governed by its own route policy, never by this process's document.
    worker = projection.row("models.terms.w")
    assert worker is not None and worker.status == "unknown"
    assert "customer worker route policy" in worker.reason
    region = projection.row("source.region")
    assert region is not None and (region.status, region.detail) == ("fact", "eu-central-1 (operator attested)")
    assert region.origins == (FlowOrigin("LEMONCROW_DATA_GOVERNANCE_FILE storage.region", "E"),)
    egress = projection.row("network.egress")
    assert egress is not None and egress.status == "restricted" and "proxy_only" in egress.detail
    partial = projection.row("models.terms.agent_cli.codex")
    assert partial is not None and partial.status == "unknown"
    assert partial.reason == "LEMONCROW_DATA_GOVERNANCE_FILE does not declare model_routes.codex retention_days"


def test_an_attested_route_trust_changes_the_route_verdict_and_names_its_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    secrets = "ai_content_access:\n  - {mode: customer_local_only, paths: ['**']}\n"
    for governance, status, shown in (
        (NOT_CONFIGURED, "denied", "(unknown route)"),
        (_ATTESTED, "allowed", "(customer_local route, operator attested)"),
        (_UNREADABLE, "denied", "(unknown route, attestation unreadable)"),
    ):
        routes = environment_route_facts(
            layer="environment", agent_cli_allowed=True, locus="local", governance=governance
        )
        projection = build_data_flow(_inputs(config=secrets, routes=routes, governance=governance))
        row = projection.row("intelligence.route.primary")
        assert row is not None and row.status == status, governance.state
        assert shown in row.detail, (governance.state, row.detail)
        ask = projection.row("intelligence.ask_models")
        assert ask is not None
        assert ("litellm (customer_local: allowed)" in ask.detail) is (governance is _ATTESTED)


def test_an_unreadable_governance_document_is_unknown_everywhere_it_attests() -> None:
    projection = build_data_flow(_inputs(endpoints=(_ENDPOINT,), hosted=_HOSTED, governance=_UNREADABLE))
    for row_id in ("policy.data_governance", "source.region", "network.egress", "models.locus.endpoint.lab"):
        row = projection.row(row_id)
        assert row is not None and row.status == "unknown", row_id
        assert _UNREADABLE.reason in row.reason, row_id


def test_no_governance_value_path_or_error_text_reaches_the_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.data_governance import load_data_governance

    hostile = tmp_path / CANARY / "governance.json"
    hostile.parent.mkdir()
    documents = [
        {"version": 1, "storage": {"region": f"https://{CANARY}.example.com"}},
        {"version": 1, "model_routes": {"litellm": {"trust": "customer_local", "provider": f"sk-{CANARY}"}}},
        {"version": 1, "model_routes": {"litellm": {"trust": "customer_local", CANARY: 1}}},
        f'{{"version": 1, {CANARY}',
    ]
    monkeypatch.setenv("LEMONCROW_DATA_GOVERNANCE_FILE", str(hostile))
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    for document in documents:
        hostile.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
        governance = load_data_governance()
        assert governance.state == "unreadable"
        routes = environment_route_facts(layer="deployment_environment", agent_cli_allowed=False, locus="managed")
        projection = build_data_flow(
            _inputs(surface="hosted", routes=routes, hosted=_HOSTED, governance=governance, agent_cli_allowed=False)
        )
        output = json.dumps(projection.to_wire()) + render_data_flow_text(projection)
        assert CANARY not in output and str(tmp_path) not in output and "example.com" not in output
        # Fail closed: the trust the hostile document tried to attest never applies.
        route = projection.row("intelligence.route.primary")
        assert route is not None and "(unknown route, attestation unreadable)" in route.detail


@pytest.mark.parametrize(
    "config",
    [
        f"ai_content_access:\n  - {{mode: {CANARY}, paths: ['**']}}\n",
        f"ai_content_access: {{mode: ai_denied, note: {CANARY}}}\n",
        f"ai_content_access: [\n  {CANARY}: [\n",
    ],
    ids=["bad mode", "not a list", "invalid yaml"],
)
def test_an_unreadable_trusted_base_content_policy_fails_closed(config: str) -> None:
    projection = build_data_flow(_inputs(config=config))
    assert projection.row("content.trusted_base") is None
    row = projection.row("content.ai_access.1")
    assert row is not None and row.status == "denied"
    assert row.origins == (FlowOrigin(".lemoncrow/review.yaml ai_content_access", "trusted_base_repository"),)
    assert row.reason == (
        "trusted-base .lemoncrow/review.yaml ai_content_access could not be read, so it denies every route; "
        "error text withheld: it may quote the file"
    )
    for row_id in ("intelligence.route.primary", "intelligence.ask_models", "content.execution_evidence"):
        assert projection.row(row_id).status == "denied", row_id  # type: ignore[union-attr]
    assert CANARY not in json.dumps(projection.to_wire()) + render_data_flow_text(projection)


def test_data_flow_trusted_base_contract() -> None:
    """case: test_a_missing_trusted_base_is_unknown_with_its_reason_and_model_access_fails_closed"""
    projection = build_data_flow(
        _inputs(config=None, trusted_base_reason="trusted base 'origin/HEAD' does not resolve to a commit")
    )
    base = projection.row("policy.trusted_base")
    assert base is not None and base.status == "unknown"
    assert base.reason == "trusted base 'origin/HEAD' does not resolve to a commit"
    for row_id in (
        "intelligence.ai",
        "content.trusted_base",
        "execution.reviewed_code",
        "execution.dependency_fetch",
        "retention.run_artifacts",
    ):
        row = projection.row(row_id)
        assert row is not None and row.status == "unknown", row_id
        assert row.reason, row_id
    # The repository's restrictions on model access are unknown, so they cover every path.
    access = projection.row("content.ai_access.1")
    assert access is not None and (access.status, access.subject) == ("denied", "AI content access: **")
    assert access.detail == "ai_denied (unreadable trusted-base repository Review policy)"
    for row_id in ("intelligence.route.primary", "intelligence.ask_models", "content.execution_evidence"):
        row = projection.row(row_id)
        assert row is not None and row.status == "denied", row_id
    "case: test_an_unknown_trusted_base_leaves_a_multi_locus_choice_unknown"
    run_plan = {"layers": [], "allowed_run_on": ["managed", "worker"], "default_run_on": "managed"}
    projection = build_data_flow(
        _inputs(
            config=None,
            trusted_base_reason="no commit",
            operator=_operator(_effective(run_plan=run_plan)),
            routes=(_route(locus="managed"), _route("worker", ask=False, locus="worker")),
        )
    )
    ai = projection.row("execution.ai_locus")
    assert ai is not None and (ai.status, ai.detail) == ("unknown", "one of: managed, worker")
    worker = projection.row("intelligence.route.worker")
    assert worker is not None and (worker.status, worker.reason) == ("unknown", "where AI work runs is unknown")
    primary = projection.row("intelligence.route.primary")
    # Where it would run is unknown, but the unreadable base already denies it the content.
    assert primary is not None and primary.status == "denied"
    assert "whether automatic AI review uses it is unknown" in primary.detail
    "case: test_a_refused_deployment_settings_document_is_unknown_without_its_error_text"
    refused = DeploymentSettings(source="x", error=f"review-deployments.json has unknown field {CANARY!r}")
    refused_observability = ObservabilitySettings(source="x", locus="local", error=f"bad connector {CANARY!r}")
    projection = build_data_flow(_inputs(**_feeds(deployments=refused, observability=refused_observability)))
    for row_id, name in (
        ("evidence.deployments", "review-deployments.json"),
        ("evidence.observability", "review-observability.json"),
    ):
        row = projection.row(row_id)
        assert row is not None and row.status == "unknown", row_id
        assert (
            row.reason == f"{name} was refused, so nothing from it applies; error text withheld: it may quote the file"
        )
    "case: test_an_invalid_operator_content_policy_denies_without_quoting_its_errors"
    raw = {"mode": CANARY, "paths": ["**"]}
    operator = _operator(_effective(ai_content_access=[raw]), content_access_layers=(("organization", (raw,)),))
    projection = build_data_flow(_inputs(operator=operator))
    row = projection.row("content.ai_access.1")
    assert row is not None and row.status == "denied"
    assert (
        row.reason
        == "operator Review policy ai_content_access is invalid, so it denies every route; error text withheld: it may quote the file"
    )
    assert projection.row("intelligence.route.primary").status == "denied"
    "case: test_rule_document_errors_leave_trusted_base_content_access_known"
    access = "ai_content_access:\n  - {mode: customer_local_only, paths: [secrets/]}\n"
    known = build_data_flow(_inputs(config=access + "rule_documents: [docs/review-rules.yaml]\n"))
    assert known.row("content.trusted_base") is None
    assert known.row("intelligence.route.primary").status == "restricted"  # type: ignore[union-attr]
    # A malformed rules block never drops the access rows that parsed.
    rules = build_data_flow(_inputs(config=access + "rules: {not: a list}\n"))
    assert rules.row("intelligence.route.primary").status == "restricted"  # type: ignore[union-attr]
    # The converse: an unreadable ai_content_access fails closed.
    broken = build_data_flow(_inputs(config="ai_content_access: {mode: ai_denied}\n"))
    assert broken.row("content.trusted_base") is None
    assert broken.row("intelligence.route.primary").status == "denied"  # type: ignore[union-attr]
    "case: test_an_invalid_trusted_base_runs_nothing"
    effective = _effective(behavior=None, trusted_base={"policy_state": "invalid", "detail": "bad effort"})
    projection = build_data_flow(_inputs(operator=_operator(effective)))
    assert projection.row("policy.trusted_base").status == "unknown"
    assert projection.row("intelligence.ai").status == "denied"
    assert projection.row("execution.reviewed_code").status == "denied"
    assert all("bad effort" not in row.reason for row in projection.rows)


def test_data_flow_rendering_contract() -> None:
    """case: test_text_rendering_shows_each_rows_reason_and_origins"""
    from lemoncrow.pro.capabilities.review.data_flow import FlowRow

    proxy_origins = (FlowOrigin("HTTPS_PROXY", "environment"), FlowOrigin("NO_PROXY", "environment"))
    projection = DataFlowProjection(
        "local",
        "repo",
        (
            FlowRow(
                "source.region", "source", "Storage region", "fact", "", (FlowOrigin("lc --root", "command_line"),)
            ),
            FlowRow("network.proxy", "network", "Outbound proxy", "set", "set by HTTPS_PROXY", proxy_origins, "why"),
        ),
    )
    assert (
        render_data_flow_text(projection)
        == "Data flow for repo (local)\n\nSource storage\n  fact         Storage region\n                 from: lc --root (command_line)\n\nNetwork settings\n  set          Outbound proxy: set by HTTPS_PROXY\n                 why: why\n                 from: HTTPS_PROXY (environment); NO_PROXY (environment)\n\nSummary: 1 set, 1 fact\n"
    )
    "case: test_endpoint_and_connector_rows_show_the_host_only_and_credential_names"
    endpoint = CustomEndpoint("lab", _HOSTILE_URL, locus="remote", models=(CustomModel("qwen-coder"),))
    projection = build_data_flow(_inputs(endpoints=(endpoint,), **_feeds(observability=_OBSERVABILITY)))
    row = projection.row("models.endpoint.lab")
    assert row is not None and row.detail.startswith("host models.example.com, locus remote")
    observability = projection.row("evidence.observability")
    assert observability is not None
    assert observability.detail == "queried on local: prometheus prom at prom.example.com (credential from PROM_TOKEN)"
    output = json.dumps(projection.to_wire()) + render_data_flow_text(projection)
    assert CANARY not in output and "48443" not in output and ("user:" not in output)
    "case: test_a_row_without_an_origin_is_refused_not_emitted"
    settings = _feeds()["deployments"].settings
    with pytest.raises(ValueError, match="cites no origin"):
        build_data_flow(_inputs(deployments=EvidenceFeed(settings, ())))
    "case: test_only_non_empty_environment_settings_count_as_set"
    from lemoncrow.pro.capabilities.review.data_flow import set_environment_names

    environ = {"HTTPS_PROXY": "", "NO_PROXY": "  ", "SSL_CERT_FILE": CANARY}
    names = ("HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "ALL_PROXY")
    assert set_environment_names(names, environ) == frozenset({"SSL_CERT_FILE"})
    "case: test_unresolvable_operator_policy_is_unknown_everywhere_it_matters"
    projection = build_data_flow(_inputs(operator=_operator({"behavior": None})))
    for row_id in ("intelligence.ai", "execution.ai_locus", "execution.runtime_locus", "execution.reviewed_code"):
        row = projection.row(row_id)
        assert row is not None and row.status == "unknown", row_id
        assert (
            row.reason
            == "effective Review policy could not be resolved (RuntimeError); error text withheld: it may quote the file"
        ), row_id
    "case: test_every_row_cites_an_origin_and_uses_the_closed_vocabulary"
    from lemoncrow.pro.capabilities.review.data_flow import CATEGORIES, FLOW_STATUSES

    projection = build_data_flow(
        _inputs(
            endpoints=(_ENDPOINT,),
            operator=_operator(),
            hosted=HostedFacts(("S",), ("S",), 90, 604800.0, "R", "B"),
            **_feeds(DeploymentSettings(connector="argocd", source="x"), _OBSERVABILITY),
        )
    )
    categories = [category for category, _title in CATEGORIES]
    assert [row.category for row in projection.rows] == sorted(
        (row.category for row in projection.rows), key=categories.index
    )
    ids = [row.id for row in projection.rows]
    assert len(ids) == len(set(ids))
    for row in projection.rows:
        assert row.status in FLOW_STATUSES and row.origins, row.id
        assert all(origin.setting and origin.layer for origin in row.origins), row.id
    assert {row.category for row in projection.rows} == set(categories)
    "case: test_a_locus_fallback_is_noted_only_when_a_layer_asked_for_another"
    run_plan = {
        "layers": [],
        "allowed_run_on": ["managed"],
        "default_run_on": "managed",
        "allowed_run_on_by_class": {"runtime": ["worker"]},
    }
    operator = _operator(_effective(run_plan=run_plan))
    implicit = build_data_flow(_inputs(operator=operator)).row("execution.runtime_locus")
    assert implicit is not None and "(worker)" in implicit.detail and ("not enabled" not in implicit.detail)
    asked = build_data_flow(_inputs(config="review:\n  run_on: managed\n", operator=operator))
    explicit = asked.row("execution.runtime_locus")
    assert explicit is not None and explicit.detail.endswith("(worker); managed is not enabled here")


def test_flipping_endpoint_and_reviewer_route_inputs_adds_their_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_data_flow(_inputs()).row("models.endpoint.lab") is None
    endpoint = build_data_flow(_inputs(endpoints=(_ENDPOINT,))).row("models.endpoint.lab")
    assert endpoint is not None and endpoint.status == "fact"
    assert "host models.example.com" in endpoint.detail and "locus remote" in endpoint.detail
    no_reviewer = environment_route_facts(layer="environment", agent_cli_allowed=True, locus="local")
    monkeypatch.setenv("LEMONCROW_REVIEW_REVIEWER_ROUTES", "ollama=qwen3")
    reviewer = environment_route_facts(layer="environment", agent_cli_allowed=True, locus="local")
    assert build_data_flow(_inputs(routes=no_reviewer)).row("intelligence.route.reviewer.1") is None
    row = build_data_flow(_inputs(routes=reviewer)).row("intelligence.route.reviewer.1")
    # A driver name never proves where its endpoint runs: unattested ollama is an unknown route.
    assert row is not None and row.status == "allowed" and ("ollama / qwen3 (unknown route)" in row.detail)


def test_hosted_agent_cli_refusal_beats_a_configured_agent_cli_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "claude")
    routes = environment_route_facts(layer="deployment_environment", agent_cli_allowed=False, locus="managed")
    projection = build_data_flow(_inputs(surface="hosted", routes=routes, agent_cli_allowed=False))
    for row_id in ("intelligence.route.primary", "intelligence.agent_cli", "web.ask"):
        row = projection.row(row_id)
        assert row is not None and row.status == "denied", row_id


_MODES = ("external_allowed", "customer_local_only", "ai_denied")
_DRIVERS = ("openai", "ollama", "litellm", "claude")


def test_an_empty_registry_allowlist_is_denied_not_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import data_flow
    from lemoncrow.pro.capabilities.review.hermetic import load_hermetic_policy

    empty = replace(load_hermetic_policy(None), registries=())
    monkeypatch.setattr(data_flow, "load_hermetic_policy", lambda *_args, **_kwargs: empty)
    row = build_data_flow(_inputs()).row("execution.dependency_fetch")
    assert row is not None and (row.status, row.detail) == ("denied", "no registry is allowlisted")


@pytest.mark.parametrize(
    ("retention_days", "shown", "seconds"), [("0.01", "1 hour", 3600), ("400", "365 days", 365 * 86400)]
)
def test_raw_retention_shows_the_window_the_store_enforces(
    tmp_path: Path, retention_days: str, shown: str, seconds: int
) -> None:
    from lemoncrow.pro.capabilities.review.hermetic import load_hermetic_policy
    from lemoncrow.pro.capabilities.review.run_evidence import RunArtifactStore

    config = f"execution:\n  retention_days: {retention_days}\n"
    store = RunArtifactStore(tmp_path, retention_seconds=load_hermetic_policy(config).retention_seconds)
    assert store.retention_seconds == seconds
    row = build_data_flow(_inputs(config=config)).row("retention.run_artifacts")
    assert row is not None and row.detail.startswith(f"raw logs, traces, video and payloads expire after {shown} ")


_HOSTILE_URL = f"https://user:{CANARY}@models.example.com:48443/v1/{CANARY}?token={CANARY}"


def test_an_unrecognized_backend_is_unknown_and_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", CANARY)
    monkeypatch.setenv("LEMONCROW_REVIEW_REVIEWER_ROUTES", f"{CANARY}=model-a")
    projection = build_data_flow(
        _inputs(routes=environment_route_facts(layer="environment", agent_cli_allowed=True, locus="local"))
    )
    for row_id in ("intelligence.route.primary", "intelligence.route.reviewer.1"):
        row = projection.row(row_id)
        assert row is not None and row.status == "unknown", row_id
    assert CANARY not in json.dumps(projection.to_wire()) and CANARY not in render_data_flow_text(projection)


@pytest.fixture
def _simulated_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    for entry_id, entry in list(catalog.STATIC_CATALOG.items()):
        if not entry.executor:
            monkeypatch.setitem(catalog.STATIC_CATALOG, entry_id, replace(entry, executor="simulated"))


_LABEL = {"local": "(local)", "worker": "(worker)", "managed": "(managed)"}


@pytest.mark.usefixtures("_simulated_executors")
def test_loci_agree_with_the_run_planner() -> None:
    for allowed in [["local"], ["managed"], ["local", "worker"], ["managed", "worker"]]:
        for by_class in [None, {"ai": ["managed"], "runtime": ["worker"]}, {"runtime": ["local"]}]:
            for requested in [None, "local", "worker", "managed"]:
                run_plan: dict[str, Any] = {"layers": [], "allowed_run_on": allowed, "default_run_on": allowed[0]}
                if by_class is not None:
                    run_plan["allowed_run_on_by_class"] = by_class
                effective = _effective(run_plan=run_plan)
                config = "review:\n  effort: deep\n" + (f"  run_on: {requested}\n" if requested else "")
                projection = build_data_flow(_inputs(config=config, operator=_operator(effective)))
                layers, execution = effective_policy_layers(effective)
                layers.append(
                    parse_review_config_layer(config, source="t")
                    or RunSettingsLayer(SettingOrigin("trusted_base_repository", "t"))
                )
                default_run_on, allowed_loci, loci_by_class = resolve_execution_loci(execution)
                plan = build_run_plan(
                    PlanInputs(
                        review_id="r",
                        revision_id="v",
                        trigger="open",
                        settings=resolve_run_settings(layers, default_run_on=default_run_on),
                        detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS)),
                        changed_paths=("src/app.py",),
                        ai_candidates=5,
                        risk_signal=True,
                        allowed_run_on=allowed_loci,
                        allowed_run_on_by_class=loci_by_class,
                    )
                )
                executors = {row.id: row.executor for row in plan.entries}
                for row_id, entry_id in (
                    ("execution.ai_locus", "ai.investigators"),
                    ("execution.runtime_locus", "checks.test"),
                ):
                    row = projection.row(row_id)
                    assert row is not None and _LABEL[executors[entry_id]] in row.detail, (
                        row_id,
                        executors[entry_id],
                        row.detail,
                    )


def test_the_source_generation_row_names_the_review_snapshot_or_says_there_is_none() -> None:
    from lemoncrow.pro.capabilities.review.data_flow import SourceGeneration

    assert build_data_flow(_inputs()).row("policy.source_generation") is None  # local reads the live base
    none = build_data_flow(_inputs(surface="hosted", hosted=_HOSTED)).row("policy.source_generation")
    assert none is not None and none.status == "not_set"
    assert none.detail.startswith("no Review-specific source generation yet: ")
    proven = SourceGeneration("r1", 2, commit="c" * 40, basis="pull_base", provider="github", captured_at="t")
    fact = build_data_flow(_inputs(surface="hosted", hosted=_HOSTED, source_generation=proven)).row(
        "policy.source_generation"
    )
    assert fact is not None and fact.status == "fact"
    assert fact.detail == f"Review r1 Revision 3: trusted base github {'c' * 12} (pull_base), captured t"
    client = build_data_flow(_inputs(surface="hosted", source_generation=SourceGeneration("r2", 0))).row(
        "policy.source_generation"
    )
    assert client is not None and (client.status, client.detail) == ("unknown", "Review r2 Revision 1")


def _recorded(*models: Any) -> Any:
    from lemoncrow.pro.capabilities.review.data_flow import SourceGeneration

    generation = SourceGeneration("r1", 1, commit="c" * 40, models=models)
    return build_data_flow(_inputs(surface="hosted", hosted=_HOSTED, source_generation=generation)).row(
        "models.recorded"
    )


def test_recorded_models_name_observed_requested_and_default_identities_within_a_bound() -> None:
    from lemoncrow.pro.capabilities.review.data_flow import MAX_RECORDED_MODEL_ENTRIES
    from lemoncrow.pro.capabilities.review.model_identity import ModelIdentity

    assert build_data_flow(_inputs()).row("models.recorded") is None
    assert build_data_flow(_inputs(surface="hosted", hosted=_HOSTED)).row("models.recorded") is None
    empty = _recorded()
    assert (empty.status, empty.detail) == ("not_set", "Review r1 Revision 2 has no settled intelligence run")
    row = _recorded(
        ModelIdentity("claude", observed_models=("claude-sonnet-4-5-20250929",)),
        ModelIdentity("claude", observed_models=("claude-sonnet-4-5-20250929",)),
        ModelIdentity("openai_compatible", requested_model="gpt-4.1"),
        ModelIdentity("codex"),
        ModelIdentity("litellm", observed_models=("anthropic/claude-3-5-sonnet-20240620",), observed_truncated=True),
        ModelIdentity("ollama", observed_models=("qwen2.5:14b", "llama3.1:8b")),
    )
    assert row.status == "fact"
    assert row.origins == (FlowOrigin("Review r1 Revision 2 intelligence runs", "review_record"),)
    assert row.detail == (
        "Review r1 Revision 2: claude observed claude-sonnet-4-5-20250929; codex provider default (unreported); "
        "litellm observed anthropic/claude-3-5-sonnet-20240620 (+more); ollama observed qwen2.5:14b, llama3.1:8b; "
        "openai requested gpt-4.1 (not observed)"
    )
    at_bound = _recorded(
        *(ModelIdentity("ollama", observed_models=(f"m{i}",)) for i in range(MAX_RECORDED_MODEL_ENTRIES))
    )
    assert "more" not in at_bound.detail and at_bound.detail.count("ollama observed") == MAX_RECORDED_MODEL_ENTRIES
    past = _recorded(
        *(ModelIdentity("ollama", observed_models=(f"m{i}",)) for i in range(MAX_RECORDED_MODEL_ENTRIES + 3))
    )
    assert past.detail.endswith("; and 3 more") and past.detail.count("ollama observed") == MAX_RECORDED_MODEL_ENTRIES


@pytest.mark.parametrize(
    "model",
    [
        f"{CANARY}.corp.example.com",
        f"https://{CANARY}.example.com/v1",
        f"gpt-4o@{CANARY}.example.com",
        f"{CANARY}.internal:8080",
        "10.0.0.7:11434",
        "localhost:11434",
        f"sk-{CANARY}",
        "9f3a7c1e5b2d4f6a8c0e",
    ],
)
def test_a_recorded_model_or_driver_shaped_like_an_endpoint_or_secret_is_withheld(model: str) -> None:
    from lemoncrow.pro.capabilities.review.model_identity import ModelIdentity

    row = _recorded(
        ModelIdentity("openai", observed_models=(model,)),
        ModelIdentity("litellm", requested_model=model),
        ModelIdentity(f"https://{CANARY}.example.com", observed_models=("gpt-4.1",)),
    )
    assert model not in row.detail and CANARY not in row.detail and "example.com" not in row.detail
    assert row.detail == (
        "Review r1 Revision 2: litellm requested (model id withheld) (not observed); "
        "openai observed (model id withheld); unrecognized driver observed gpt-4.1"
    )


_REPORTER = FlowOrigin("customer worker registry", "deployment_environment")


def test_a_reported_customer_worker_locus_names_the_worker() -> None:
    from lemoncrow.pro.capabilities.review.data_flow import RuntimeExecutionLocus

    assert not [row for row in build_data_flow(_inputs()).rows if row.id.startswith("execution.locus.")]
    loci = (
        RuntimeExecutionLocus("observation", "customer_worker", _REPORTER, worker="svc-observer_1"),
        RuntimeExecutionLocus("observation", "customer_worker", _REPORTER, worker="svc-observer_1"),
        RuntimeExecutionLocus("runtime", "managed", FlowOrigin("managed sandbox", "built_in_default")),
    )
    projection = build_data_flow(_inputs(execution_loci=loci))
    observation = projection.row("execution.locus.observation")
    assert observation is not None
    assert (observation.status, observation.detail) == ("fact", "customer-operated worker svc-observer_1")
    assert observation.origins == (_REPORTER,)
    runtime = projection.row("execution.locus.runtime")
    assert runtime is not None and runtime.detail == "the hosted deployment's managed executor (managed)"


def test_flow_rows_carry_a_closed_locus_the_diagnostic_bundle_reads_and_the_wire_omits() -> None:
    from lemoncrow.pro.capabilities.review.data_flow import RuntimeExecutionLocus
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import DataFlowFacts, _execution_locus_section

    loci = (
        RuntimeExecutionLocus("observation", "customer_worker", _REPORTER, worker="svc-observer_1"),
        RuntimeExecutionLocus("runtime", "managed", FlowOrigin("managed sandbox", "built_in_default")),
    )
    projection = build_data_flow(_inputs(execution_loci=loci))
    observation, runtime = projection.row("execution.locus.observation"), projection.row("execution.locus.runtime")
    assert observation is not None and observation.locus == ("customer_worker",)
    assert runtime is not None and runtime.locus == ("managed",)
    model_rows = [row for row in projection.rows if row.id.startswith("models.locus.")]
    assert model_rows and all(
        set(row.locus) <= {"external", "customer_local", "unknown"} and row.locus for row in model_rows
    )
    assert all("locus" not in row for row in projection.to_wire()["rows"])
    section = _execution_locus_section(DataFlowFacts(projections=(("repo-a", projection),)), 5)
    rows = {row["id"]: row for row in section["repositories"][0]["rows"]}
    assert rows["execution.locus.observation"]["locus"] == ["customer_worker"]
    assert "svc-observer_1" not in json.dumps(section)


@pytest.mark.parametrize(
    "worker",
    [f"{CANARY}.corp.example.com", f"https://{CANARY}@h", f"sk-{CANARY}", "9f3a7c1e5b2d4f6a8c0e", "", "x" * 65],
)
def test_a_worker_label_that_is_not_a_plain_name_is_withheld(worker: str) -> None:
    from lemoncrow.pro.capabilities.review.data_flow import RuntimeExecutionLocus

    locus = RuntimeExecutionLocus("observation", "customer_worker", _REPORTER, worker=worker)
    projection = build_data_flow(_inputs(execution_loci=(locus,)))
    row = projection.row("execution.locus.observation")
    assert row is not None and row.detail == "a customer-operated worker (label withheld)"
    output = json.dumps(projection.to_wire()) + render_data_flow_text(projection)
    assert CANARY not in output and "example.com" not in output and (not worker or worker not in output)


# Each shape embeds a plain word that would be shown on its own: only the
# identifier's structure (credential, host, address, port) withholds it.
_LEAK_SHAPES: dict[str, Callable[[str], str]] = {
    "userinfo credential": lambda word: f"admin:{word}@gw/llama3",
    "host after at": lambda word: f"llama3@{word}.corp.example",
    "host as vendor": lambda word: f"{word}.corp/llama3",
    "host and port as vendor": lambda word: f"{word}-gw:8443/llama",
    "host as model": lambda word: f"ollama/{word}.internal",
    "ipv6 and port": lambda word: f"{word}@[fd00::1]:11434",
    "plus host": lambda word: f"llama+{word}.corp.example",
    "leet secret": lambda word: f"s3cr3t-{word}-p4ss",
}


@pytest.mark.parametrize("shape", sorted(_LEAK_SHAPES))
def test_no_configured_identifier_reaches_any_row_unless_it_is_a_plain_name(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    from lemoncrow.pro.capabilities.review.data_flow import RuntimeExecutionLocus, SourceGeneration
    from lemoncrow.pro.capabilities.review.model_identity import ModelIdentity

    leak = _LEAK_SHAPES[shape]
    words = {
        source: f"zq{source}"
        for source in ("primary", "reviewer", "endpointname", "endpointmodel", "endpointurl", "recorded", "worker")
    }
    words["connector"] = "zqconnector"
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    monkeypatch.setenv("LEMONCROW_LITELLM_MODEL", leak(words["primary"]))
    monkeypatch.setenv("LEMONCROW_REVIEW_REVIEWER_ROUTES", f"openai={leak(words['reviewer'])}")
    url_word = words["endpointurl"]
    endpoint = CustomEndpoint(
        leak(words["endpointname"]),
        f"https://{url_word}:{url_word}@gw.example.com:8443/{url_word}?key={url_word}#{url_word}",
        locus="remote",
        models=(CustomModel(leak(words["endpointmodel"])), CustomModel("qwen-coder")),
    )
    recorded = SourceGeneration(
        "r1",
        1,
        commit="c" * 40,
        models=(
            ModelIdentity("openai", observed_models=(leak(words["recorded"]),)),
            ModelIdentity("litellm", requested_model=leak(words["recorded"])),
            ModelIdentity("ollama", unverified_model=leak(words["recorded"])),
        ),
    )
    connector = ConnectorConfig("prometheus", leak(words["connector"]), "https://prom.example.com")
    observability = ObservabilitySettings(connectors=(connector,), source="local_operator_policy", locus="local")
    projection = build_data_flow(
        _inputs(
            surface="hosted",
            hosted=_HOSTED,
            source_generation=recorded,
            routes=environment_route_facts(layer="environment", agent_cli_allowed=False, locus="local"),
            endpoints=(endpoint,),
            execution_loci=(
                RuntimeExecutionLocus("observation", "customer_worker", _REPORTER, worker=leak(words["worker"])),
            ),
            **_feeds(observability=observability),
        )
    )
    wire = json.dumps(projection.to_wire())
    text = render_data_flow_text(projection)
    for source, word in words.items():
        assert word not in wire, (source, [row for row in projection.rows if word in json.dumps(row.to_wire())])
        assert word not in text, source
    # Each source still has its row: the identifier is withheld, not the fact.
    primary = projection.row("intelligence.route.primary")
    assert primary is not None and "model id withheld" in primary.detail
    reviewer = projection.row("intelligence.route.reviewer.1")
    assert reviewer is not None and "model id withheld" in reviewer.detail
    endpoint_row = projection.row("models.endpoint.1")
    assert endpoint_row is not None and endpoint_row.subject == "Custom endpoint 1 (name withheld)"
    assert "host gw.example.com" in endpoint_row.detail
    assert "models: qwen-coder, 1 model id(s) withheld" in endpoint_row.detail
    assert "(model id withheld)" in projection.row("models.recorded").detail  # type: ignore[union-attr]
    assert "connector 1 (id withheld)" in projection.row("evidence.observability").detail  # type: ignore[union-attr]


def test_an_unreported_or_failed_locus_is_unknown_without_its_text() -> None:
    from lemoncrow.pro.capabilities.review.data_flow import RuntimeExecutionLocus

    projection = build_data_flow(
        _inputs(
            execution_loci=(
                RuntimeExecutionLocus("observation", "unknown", _REPORTER),
                RuntimeExecutionLocus("runtime", "unknown", _REPORTER, failure="KeyError"),
                RuntimeExecutionLocus("ai", "unknown", _REPORTER, failure=f"{CANARY} at /etc/x"),
                RuntimeExecutionLocus(f"Obs/{CANARY}", "customer_worker", _REPORTER, worker="svc"),
                RuntimeExecutionLocus("behavior", CANARY, _REPORTER),  # type: ignore[arg-type]
            )
        )
    )
    reasons = {row.id: (row.status, row.reason) for row in projection.rows if row.id.startswith("execution.locus.")}
    withheld = "error text withheld: it may quote the file"
    assert reasons == {
        "execution.locus.ai": ("unknown", f"the locus reporter failed (Exception); {withheld}"),
        "execution.locus.behavior": ("unknown", "the executor did not report where this work runs"),
        "execution.locus.observation": ("unknown", "the executor did not report where this work runs"),
        "execution.locus.runtime": ("unknown", f"the locus reporter failed (KeyError); {withheld}"),
        "execution.locus.unrecognized": ("unknown", "a reported locus names work that is not an identifier"),
    }
    assert CANARY not in json.dumps(projection.to_wire())


def test_the_organization_block_turns_off_reported_execution_loci_by_kind() -> None:
    from lemoncrow.pro.capabilities.review.data_flow import RuntimeExecutionLocus

    loci = tuple(
        RuntimeExecutionLocus(work, "customer_worker", _REPORTER, worker="svc")
        for work in ("ai", "runtime", "observation", "index")
    )
    effective = _effective(organization_block={"ai": False, "execution": True})
    projection = build_data_flow(_inputs(operator=_operator(effective), execution_loci=loci))
    works = ("ai", "runtime", "observation", "index")
    statuses = {work: projection.row(f"execution.locus.{work}").status for work in works}  # type: ignore[union-attr]
    # The execution block stops customer-worker production observation too; unmapped work stays a fact.
    assert statuses == {"ai": "fact", "runtime": "off", "observation": "off", "index": "fact"}
    runtime = projection.row("execution.locus.runtime")
    assert runtime is not None and runtime.reason == "blocked by organization policy (execution_block.execution)"


def _git_repo(path: Path, config: str | None) -> Path:
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("x\n", encoding="utf-8")
    if config is not None:
        (path / ".lemoncrow").mkdir()
        (path / ".lemoncrow" / "review.yaml").write_text(config, encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return path


def _invoke(root: Path, *args: str) -> Any:
    return CliRunner().invoke(cli, ["--root", str(root), "review", "data-flow", *args], catch_exceptions=False)


def test_cli_never_prints_proxy_or_ca_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path / "repo", "review:\n  capabilities: {ai: off}\n")
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:{CANARY}@proxy.invalid:3128")
    monkeypatch.setenv("SSL_CERT_FILE", f"/etc/{CANARY}/ca.pem")
    for args in ((), ("--json",)):
        result = _invoke(tmp_path / "root", "--repo", str(repo), *args)
        assert result.exit_code == 0, result.output
        assert CANARY not in result.output
        assert "HTTPS_PROXY" in result.output and "SSL_CERT_FILE" in result.output
    document = json.loads(_invoke(tmp_path / "root", "--repo", str(repo), "--json").output)
    rows = {row["id"]: row for row in document["rows"]}
    assert document["surface"] == "local" and document["schema_version"] == 1
    assert rows["network.proxy"]["status"] == "set" and rows["network.custom_ca"]["status"] == "set"
    assert rows["intelligence.ai"]["status"] == "denied"
    assert rows["policy.trusted_base"]["status"] == "fact"
    assert rows["source.storage"]["origins"] == [{"setting": "lc --root", "layer": "command_line"}]


def test_cli_reads_the_trusted_base_not_the_working_tree(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", None)
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review.yaml").write_text("review:\n  capabilities: {ai: off}\n", encoding="utf-8")
    document = json.loads(_invoke(tmp_path / "root", "--repo", str(repo), "--json").output)
    rows = {row["id"]: row for row in document["rows"]}
    assert (
        rows["policy.trusted_base"]["status"] == "fact"
        and "no .lemoncrow/review.yaml" in rows["policy.trusted_base"]["detail"]
    )
    assert rows["intelligence.ai"]["status"] == "allowed"
    committed = _git_repo(tmp_path / "committed", "review:\n  capabilities: {ai: off}\n")
    (committed / ".lemoncrow" / "review.yaml").write_text("review:\n  capabilities: {ai: always}\n", encoding="utf-8")
    document = json.loads(_invoke(tmp_path / "root", "--repo", str(committed), "--json").output)
    assert {row["id"]: row for row in document["rows"]}["intelligence.ai"]["status"] == "denied"


def test_cli_never_passes_an_option_shaped_base_to_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli.commands import review_data_flow_cli as module

    repo = _git_repo(tmp_path / "repo", None)
    calls: list[tuple[str, ...]] = []
    real = module._git

    def recording(path: Path, *args: str) -> Any:
        calls.append(args)
        return real(path, *args)

    monkeypatch.setattr(module, "_git", recording)
    document, reason = module.read_trusted_base(repo, "--output=x")
    assert document is None and "does not resolve" in reason
    assert not any("--output" in arg for args in calls for arg in args)


def test_cli_prints_hosts_and_setting_names_never_values_or_settings_file_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.outcomes.local_deployments import load_local_deployment_settings

    root = tmp_path / "root"
    root.mkdir()
    endpoints = {"custom": {"endpoints": {"lab": {"base_url": _HOSTILE_URL, "models": {"qwen-coder": {}}}}}}
    (root / "providers.json").write_text(json.dumps(endpoints), encoding="utf-8")
    connector = {
        "kind": "prometheus",
        "id": "prom",
        "base_url": f"https://prom.example.com:48443/p-{CANARY}",
        "credential": {"env": "PROM_TOKEN"},
    }
    observability = {"version": 1, "observability": {"connectors": [connector], "services": ["checkout"]}}
    (root / "review-observability.json").write_text(json.dumps(observability), encoding="utf-8")
    (root / "review-deployments.json").write_text(json.dumps({"version": 1, f"{CANARY}-key": 1}), encoding="utf-8")
    assert CANARY in load_local_deployment_settings(root).error
    monkeypatch.setenv("PROM_TOKEN", CANARY)
    repo = _git_repo(tmp_path / "repo", None)
    for args in ((), ("--json",)):
        result = _invoke(root, "--repo", str(repo), *args)
        assert result.exit_code == 0, result.output
        assert CANARY not in result.output and "48443" not in result.output and ("user:" not in result.output)
        for shown in ("models.example.com", "prom.example.com", "PROM_TOKEN", "review-deployments.json was refused"):
            assert shown in result.output, shown


def test_cli_never_echoes_a_malformed_trusted_base_config(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", f"review:\n  capabilities: {{ai: off}}\n  token: {CANARY} : [\n")
    for args in ((), ("--json",)):
        result = _invoke(tmp_path / "root", "--repo", str(repo), *args)
        assert result.exit_code == 0, result.output
        assert CANARY not in result.output and "error text withheld" in result.output
    rows = {
        row["id"]: row for row in json.loads(_invoke(tmp_path / "root", "--repo", str(repo), "--json").output)["rows"]
    }
    # Unreadable YAML may hide content restrictions: model access fails closed.
    assert "content.trusted_base" not in rows
    assert rows["content.ai_access.1"]["status"] == "denied"
    assert rows["intelligence.ask_models"]["status"] == "denied"
    assert rows["execution.dependency_fetch"]["status"] == "unknown"


def test_cli_refuses_an_unknown_base_and_outside_a_repository(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", None)
    result = CliRunner().invoke(
        cli, ["--root", str(tmp_path / "root"), "review", "data-flow", "--repo", str(repo), "--base", "nope"]
    )
    assert result.exit_code != 0 and "does not resolve" in result.output
    outside = tmp_path / "plain"
    outside.mkdir()
    result = CliRunner().invoke(cli, ["--root", str(tmp_path / "root"), "review", "data-flow", "--repo", str(outside)])
    assert result.exit_code != 0 and "not inside a Git repository" in result.output
