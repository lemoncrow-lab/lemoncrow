"""BI0: the effective intelligence route policy and its seam into the Review Intelligence runtime.

Legacy configuration keeps its behavior; ``auto`` and explicit routes resolve
through the one resolver; requested, resolved and observed identity stay
separate; route/model/content-access changes move cache identity; the hosted
agent-CLI refusal is structural; invalid routes and malformed or denying
content policy fail closed before any model call; and no credential reaches
the wire projection.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.infra.internal_llm import InternalLLMError
from lemoncrow.pro.capabilities.review import intelligence_runtime
from lemoncrow.pro.capabilities.review.data_flow import (
    DataFlowInputs,
    EvidenceFeed,
    FlowOrigin,
    build_data_flow,
    environment_route_facts,
)
from lemoncrow.pro.capabilities.review.data_governance import parse_data_governance
from lemoncrow.pro.capabilities.review.intelligence_policy import (
    BACKGROUND_NOTE,
    MODEL_WITHHELD,
    ContentAccessRefused,
    IntelligencePolicyError,
    PolicyOrigin,
    RouteRequest,
    ask_route_options,
    bind_content_access,
    parse_intelligence_policy,
    process_intelligence_policy,
    resolve_intelligence_policy,
    resolve_intelligence_route,
    route_request,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    AgentCliDriverRefused,
    IntelligenceDisabled,
    IntelligenceRunRequest,
    ModelCallBudget,
    ModelCallUsage,
    RouteOverrideRefused,
    StructuredExecution,
    bound_intelligence_policy,
    build_intelligence_identity,
    execute_structured,
    resolve_driver_model,
    run_configured_route,
    run_intelligence,
)
from lemoncrow.pro.capabilities.review.outcomes.deployments import DeploymentSettings
from lemoncrow.pro.capabilities.review.outcomes.observability import ObservabilitySettings
from lemoncrow.pro.capabilities.review.rules import ContentAccessRule
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

_ROUTE_ENV = (
    "LEMONCROW_LLM_BACKEND",
    "LEMONCROW_OPENAI_MODEL",
    "LEMONCROW_LITELLM_MODEL",
    "LEMONCROW_OLLAMA_MODEL",
    "LEMONCROW_REVIEW_REVIEWER_ROUTES",
    "LEMONCROW_DATA_GOVERNANCE_FILE",
)


@pytest.fixture(autouse=True)
def settings_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty settings root for this process (what ``process_intelligence_policy`` reads)."""

    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    for name in _ROUTE_ENV:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)
    try:
        yield root
    finally:
        set_active_store_root(None)


def _settings(root: Path, **values: Any) -> None:
    (root / "plugin_settings.json").write_text(json.dumps(values), encoding="utf-8")


def _store(tmp_path: Path) -> tuple[ReviewStore, ReviewSession, ReviewRevision]:
    store = ReviewStore(tmp_path / "store")
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="/not/durable/identity",
            range_mode="working_tree",
            title="policy test",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-aaa",
            source_fingerprint="source-aaa",
            packet_schema_version=1,
        ),
        [],
    )
    return store, session, revision


_SCHEMA = {
    "type": "object",
    "properties": {"body": {"type": "string"}},
    "required": ["body"],
    "additionalProperties": False,
}


def _request(session: ReviewSession, revision: ReviewRevision, **overrides: Any) -> IntelligenceRunRequest:
    values: dict[str, Any] = {
        "review_id": session.id,
        "revision_id": revision.id,
        "snapshot_identity": "source-aaa",
        "capability": "ask",
        "scope": {"path": "src/a.py"},
        "context": {"question": "why"},
        "policy": {"read_only": True},
        "driver": None,
        "model": None,
        "output_schema": _SCHEMA,
        "prompt_version": 1,
        "schema_version": 1,
        "budget": ModelCallBudget(max_calls=1, max_review_calls=20),
    }
    values.update(overrides)
    return IntelligenceRunRequest(**values)


def _executor(driver: str, model: str, *, observed: tuple[str, ...] = (), calls: list[int] | None = None) -> Any:
    def execute() -> StructuredExecution:
        if calls is not None:
            calls.append(1)
        return StructuredExecution(
            result={"body": "ok"},
            driver=driver,  # type: ignore[arg-type]
            model=model,
            usage=ModelCallUsage(calls=1),
            observed_models=observed,
        )

    return execute


def _policy(**document: Any) -> Any:
    return resolve_intelligence_policy((parse_intelligence_policy(document, origin=PolicyOrigin("organization", "t")),))


def _attest_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Attest litellm as customer-local for this process (RC6), and return the attestations."""

    document = '{"version":1,"model_routes":{"litellm":{"trust":"customer_local"}}}'
    path = tmp_path / "governance.json"
    path.write_text(document, encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_DATA_GOVERNANCE_FILE", str(path))
    return parse_data_governance(document)


# --------------------------------------------------------------------------- #
# legacy configuration
# --------------------------------------------------------------------------- #


def test_legacy_configuration_resolves_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(InternalLLMError, match="provider is disabled"):
        resolve_driver_model(None, None)
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai_compatible")
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "gpt-4.1")
    assert resolve_driver_model(None, None) == intelligence_runtime.ResolvedDriver("openai", "gpt-4.1")
    assert resolve_driver_model("configured", "pinned-x") == intelligence_runtime.ResolvedDriver("openai", "pinned-x")
    assert resolve_driver_model("ollama", None).driver == "ollama"
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "claude")
    # A subscription CLI's own default stays unreported: never a guessed model name.
    assert resolve_driver_model(None, None) == intelligence_runtime.ResolvedDriver("claude", "")
    with pytest.raises(InternalLLMError, match="Unsupported"):
        resolve_driver_model("gpt", None)


def test_legacy_requests_keep_their_run_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    store, session, revision = _store(tmp_path)
    request = _request(session, revision, driver="openai", model="m1")
    outcome = run_intelligence(store=store, request=request, executor=_executor("openai", "m1"))
    expected = build_intelligence_identity(
        revision_id=revision.id,
        snapshot_identity="source-aaa",
        capability="ask",
        scope={"path": "src/a.py"},
        context={"question": "why"},
        policy={"read_only": True},
        driver="openai",
        model="m1",
        output_schema=_SCHEMA,
        prompt_version=1,
        schema_version=1,
    )
    row = store.get_intelligence_run(session.id, expected.run_key)
    assert row is not None and row.id == outcome.run_id
    # No policy was supplied: the wire shape is unchanged (no configured route).
    assert outcome.configured is None and "configured_route" not in outcome.to_wire()


# --------------------------------------------------------------------------- #
# auto and explicit routes
# --------------------------------------------------------------------------- #


def test_auto_route_resolves_the_configured_provider_with_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    monkeypatch.setenv("LEMONCROW_LITELLM_MODEL", "anthropic/claude-sonnet-4")
    route = resolve_intelligence_route()
    assert route.status == "ready" and route.run_on == "local"
    assert (route.requested.driver, route.requested.model) == ("auto", "auto")
    assert (route.driver, route.model) == ("litellm", "anthropic/claude-sonnet-4")
    assert route.driver_source == PolicyOrigin("environment", "LEMONCROW_LLM_BACKEND")
    assert route.model_source == PolicyOrigin("environment", "LEMONCROW_LITELLM_MODEL")
    assert route.policy.driver.origin.layer == "built_in_default"


def test_explicit_settings_route_wins_over_the_environment_but_not_over_a_caller(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    _settings(settings_root, **{"intelligence.driver": "ollama", "intelligence.model": "qwen2.5:7b"})
    assert resolve_driver_model(None, None) == intelligence_runtime.ResolvedDriver("ollama", "qwen2.5:7b")
    assert resolve_driver_model("auto", None) == intelligence_runtime.ResolvedDriver("ollama", "qwen2.5:7b")
    # An explicit caller driver is the caller's choice: the policy's model is not forced onto it.
    assert resolve_driver_model("openai", None) == intelligence_runtime.ResolvedDriver("openai", "gpt-4o-mini")
    route = resolve_intelligence_route()
    assert route.requested.driver == "ollama" and route.requested.model == "qwen2.5:7b"
    assert route.driver_source == PolicyOrigin("settings", "intelligence.driver")
    assert route.model_source == PolicyOrigin("settings", "intelligence.model")
    # Explicit and auto routes produce stable identities.
    assert resolve_intelligence_route().identity() == route.identity()


def test_subscription_cli_default_model_is_recorded_as_unreported(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(intelligence_runtime, "_agent_cli_drivers_allowed", True)
    _settings(settings_root, **{"intelligence.driver": "codex"})
    route = resolve_intelligence_route()
    assert (route.driver, route.model) == ("codex", "")
    wire = route.to_wire()
    assert wire["resolved"]["model"] == "" and wire["resolved"]["model_source"] == "unreported_default"
    assert wire["resolved"]["model_origin"]["source"] == "codex CLI default model"
    assert route.resolved_identity.source == "unreported_default"


# --------------------------------------------------------------------------- #
# identity and cache
# --------------------------------------------------------------------------- #


def test_requested_resolved_and_observed_identity_persist_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    store, session, revision = _store(tmp_path)
    policy = process_intelligence_policy()
    request = _request(session, revision, route_policy=policy)
    first = run_intelligence(
        store=store, request=request, executor=_executor("openai", "gpt-4o-mini", observed=("gpt-4o-mini-2024-07-18",))
    )
    again = run_intelligence(store=store, request=request, executor=_executor("openai", "gpt-4o-mini"))
    for outcome in (first, again):
        assert outcome.configured is not None
        assert (outcome.configured.driver, outcome.configured.model) == ("auto", "auto")
        assert (outcome.driver, outcome.model) == ("openai", "gpt-4o-mini")
        assert outcome.identity is not None
        assert outcome.identity.requested_model == "gpt-4o-mini"
        assert outcome.identity.observed_models == ("gpt-4o-mini-2024-07-18",)
        assert outcome.to_wire()["configured_route"]["driver"] == "auto"
    assert again.cache_hit and again.provider_calls_this_request == 0


def test_route_model_and_content_access_changes_move_cache_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    monkeypatch.setenv("LEMONCROW_LITELLM_MODEL", "m1")
    store, session, revision = _store(tmp_path)
    calls: list[int] = []

    def run(policy: Any, *, model: str = "m1", binding: Any = None) -> Any:
        return run_intelligence(
            store=store,
            request=_request(
                session,
                revision,
                route_policy=policy,
                content_access=binding,
                content_paths=None if binding is None else (),
            ),
            executor=_executor("litellm", model, calls=calls),
        )

    auto = run(_policy(driver="auto"))
    # ``auto`` and an explicit choice that resolve to one route execute the same model: one result.
    explicit = run(_policy(driver="litellm", model="m1"))
    assert explicit.cache_hit and explicit.run_id == auto.run_id and len(calls) == 1
    # A different model is a different result.
    other = run(_policy(driver="litellm", model="m2"), model="m2")
    assert not other.cache_hit and len(calls) == 2

    rule = ContentAccessRule("customer_local_only", ("secret/**",), "org", "organization_platform", "org-policy")
    external = resolve_intelligence_route(_policy(driver="auto"))
    bound = run(_policy(driver="auto"), binding=bind_content_access((), route=external))
    assert not bound.cache_hit and len(calls) == 3
    tightened = run(_policy(driver="auto"), binding=bind_content_access((rule,), route=external))
    assert not tightened.cache_hit and len(calls) == 4
    # The same rules decided for an attested customer-local route are another identity.
    local = resolve_intelligence_route(_policy(driver="auto"), governance=_attest_local(tmp_path, monkeypatch))
    attested = run(_policy(driver="auto"), binding=bind_content_access((rule,), route=local))
    assert not attested.cache_hit and len(calls) == 5
    assert run(_policy(driver="auto"), binding=bind_content_access((rule,), route=local)).cache_hit
    assert len(calls) == 5


# --------------------------------------------------------------------------- #
# hosted refusal, degradation, fail closed
# --------------------------------------------------------------------------- #


def test_hosted_agent_cli_refusal_is_structural_whatever_chose_the_driver(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(intelligence_runtime, "_agent_cli_drivers_allowed", False)
    _settings(settings_root, **{"intelligence.driver": "claude"})
    with pytest.raises(AgentCliDriverRefused):
        resolve_driver_model(None, None)
    for driver in ("claude", "codex", "opencode"):
        with pytest.raises(AgentCliDriverRefused):
            resolve_driver_model(None, None, policy=_policy(driver=driver))
    route = resolve_intelligence_route(allowed_run_on=("managed",))
    assert route.status == "refused" and not route.ready and route.driver == ""

    started: list[str] = []
    with pytest.raises(AgentCliDriverRefused):
        execute_structured(
            system="s",
            prompt="p",
            output_schema=_SCHEMA,
            project_root=settings_root,
            origin="machine",
            cli_executor=lambda **kwargs: started.append(kwargs["driver"]) or {},
            policy=_policy(driver="opencode"),
        )
    assert started == []


def test_invalid_disabled_or_unavailable_routes_degrade_explicitly(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    _settings(settings_root, **{"intelligence.driver": "gpt-secret-route"})
    route = resolve_intelligence_route()
    assert route.status == "invalid" and "intelligence.driver" in route.reason
    assert "gpt-secret-route" not in json.dumps(route.to_wire())
    with pytest.raises(IntelligenceDisabled):
        resolve_driver_model(None, None)
    with pytest.raises(IntelligenceDisabled):  # an unreadable policy refuses explicit routes too
        resolve_driver_model("openai", None)

    # An operator layer that turned AI off reads as a $0 budget that names who set it (OWNER-2).
    off = _policy(enabled=False)
    assert resolve_intelligence_route(off).status == "disabled"
    assert "AI budget is $0 (set by the organization policy)" in resolve_intelligence_route(off).reason
    with pytest.raises(IntelligenceDisabled, match="AI budget is \\$0"):
        resolve_driver_model("ollama", None, policy=off)
    # A person has no such switch: a stored intelligence.enabled is a retired key, read as the budget it means.
    _settings(settings_root, **{"intelligence.enabled": False})
    assert process_intelligence_policy().enabled.value is True
    assert json.loads((settings_root / "plugin_settings.json").read_text()) == {"intelligence.monthly_budget_usd": 0.0}
    assert resolve_intelligence_route().status == "ready"  # a label: it never refuses for a budget
    with pytest.raises(IntelligenceDisabled, match="AI budget is \\$0"):
        execute_structured(
            system="s", prompt="p", output_schema={"type": "object"}, origin="machine", cli_executor=lambda **_: {}
        )

    # run_on has no consumer until background scheduling: it never degrades a route.
    _settings(settings_root, **{"intelligence.run_on": "worker"})
    elsewhere = resolve_intelligence_route(allowed_run_on=("local",))
    assert elsewhere.status == "ready" and elsewhere.run_on == "worker"
    assert elsewhere.to_wire()["run_on"]["offered_here"] is False
    assert BACKGROUND_NOTE in json.dumps(elsewhere.to_wire())
    assert resolve_intelligence_route(allowed_run_on=("managed", "worker")).run_on == "worker"

    _settings(settings_root)
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "none")
    assert resolve_intelligence_route().status == "not_configured"
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "bogus")
    assert resolve_intelligence_route().status == "invalid"

    with pytest.raises(IntelligencePolicyError):
        parse_intelligence_policy({"driver": "auto", "budget": 5}, origin=PolicyOrigin("organization", "t"))
    with pytest.raises(IntelligencePolicyError):
        parse_intelligence_policy({"background": {"enabled": "yes"}}, origin=PolicyOrigin("organization", "t"))
    with pytest.raises(ValueError):
        resolve_intelligence_policy(
            (
                parse_intelligence_policy({}, origin=PolicyOrigin("repository", "t")),
                parse_intelligence_policy({}, origin=PolicyOrigin("organization", "t")),
            )
        )


@pytest.mark.parametrize(
    "rows",
    [
        [{"mode": "ai_maybe", "paths": ["**"], "trust_class": "organization_platform"}],
        [{"mode": "ai_denied", "paths": "**", "trust_class": "organization_platform"}],
        [{"mode": "ai_denied", "paths": ["**"], "trust_class": "organization_platform", "digest": "forged"}],
        [{"mode": "external_allowed", "paths": ["**"], "trust_class": "organization_platform", "extra": 1}],
        ["ai_denied"],
        "ai_denied",
    ],
)
def test_malformed_content_policy_fails_closed_before_the_model(
    rows: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    binding = bind_content_access(rows, route=resolve_intelligence_route())
    assert not binding.valid and not binding.decide("README.md").allowed
    with pytest.raises(ContentAccessRefused):
        binding.require(())
    store, session, revision = _store(tmp_path)
    calls: list[int] = []
    outcome = run_intelligence(
        store=store,
        request=_request(session, revision, content_access=binding, content_paths=("README.md",)),
        executor=_executor("openai", "gpt-4o-mini", calls=calls),
    )
    assert outcome.status == "failed" and "content access refused" in outcome.error
    assert calls == [] and store.intelligence_calls(session.id) == 0


def test_denied_content_never_reaches_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    rules = (
        ContentAccessRule("ai_denied", ("secrets/**",), "org", "organization_platform", "org-policy"),
        ContentAccessRule("customer_local_only", ("internal/**",), "org", "organization_platform", "org-policy"),
    )
    # A wire round trip of valid rows binds the same policy.
    external = resolve_intelligence_route()
    binding = bind_content_access([rule.to_wire() for rule in rules], route=external)
    assert binding.valid and binding.digest == bind_content_access(rules, route=external).digest
    store, session, revision = _store(tmp_path)
    calls: list[int] = []
    for paths in (("secrets/key.pem",), ("src/a.py", "internal/x.py")):
        outcome = run_intelligence(
            store=store,
            request=_request(session, revision, content_access=binding, content_paths=paths),
            executor=_executor("litellm", "gpt-4o-mini", calls=calls),
        )
        assert outcome.status == "failed" and calls == []
    # Only an attested customer-local route (RC6), never the hostname, admits customer_local_only content.
    monkeypatch.setenv("LEMONCROW_LITELLM_BASE_URL", "http://localhost:4000")
    assert bind_content_access(rules, route=resolve_intelligence_route()).allowed(("internal/x.py",)) == ()
    attested = resolve_intelligence_route(governance=_attest_local(tmp_path, monkeypatch))
    local = bind_content_access(rules, route=attested)
    assert local.allowed(("internal/x.py", "secrets/key.pem")) == ("internal/x.py",)
    ready = run_intelligence(
        store=store,
        request=_request(session, revision, content_access=local, content_paths=("internal/x.py",)),
        executor=_executor("litellm", "gpt-4o-mini", calls=calls),
    )
    assert ready.status == "ready" and calls == [1]


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #


def _flow_inputs(**overrides: Any) -> DataFlowInputs:
    values: dict[str, Any] = {
        "surface": "local",
        "repository": "repo",
        "trusted_base": TrustedBaseDocument(
            path=".lemoncrow/review.yaml", content="", state="absent", source="trusted base HEAD"
        ),
        "deployments": EvidenceFeed(
            DeploymentSettings(source="local_operator_policy"), (FlowOrigin("d.json", "local_operator_policy"),)
        ),
        "observability": EvidenceFeed(
            ObservabilitySettings(source="local_operator_policy", locus="local"),
            (FlowOrigin("o.json", "local_operator_policy"),),
        ),
    }
    values.update(overrides)
    return DataFlowInputs(**values)


def test_origin_and_data_locus_projection(settings_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    _settings(settings_root, **{"intelligence.driver": "litellm", "intelligence.background.enabled": "false"})
    governance = parse_data_governance(
        '{"version":1,"model_routes":{"litellm":{"trust":"customer_local","provider":"acme-vllm",'
        '"data_region":"eu-west-1","training":"no_training","retention_days":0}}}'
    )
    route = resolve_intelligence_route(governance=governance)
    wire = route.to_wire()
    assert wire["data_locus"]["data_locus"] == "customer_local"
    assert wire["data_locus"]["trust_source"] == "operator_attested"
    assert wire["data_locus"]["provider"] == "acme-vllm"
    assert wire["background_enabled"] is False
    assert wire["policy"]["background"]["enabled"]["origin"]["source"] == "intelligence.background.enabled"
    # Unattested, the same route is unknown: never inferred from its name or endpoint.
    assert resolve_intelligence_route().to_wire()["data_locus"]["trust"] == "unknown"

    policy = process_intelligence_policy()
    routes = environment_route_facts(
        layer="environment", agent_cli_allowed=True, locus="local", governance=governance, policy=policy
    )
    projection = build_data_flow(_flow_inputs(routes=routes, governance=governance, intelligence=route))
    primary = projection.row("intelligence.route.primary")
    assert primary is not None and "litellm" in primary.detail
    assert FlowOrigin("intelligence.driver", "user_settings") in primary.origins
    row = projection.row("intelligence.policy")
    assert row is not None and row.status == "set"
    assert "driver litellm" in row.detail and "background intelligence off" in row.detail
    assert FlowOrigin("intelligence.background.enabled", "user_settings") in row.origins

    disabled = environment_route_facts(
        layer="environment", agent_cli_allowed=True, locus="local", policy=_policy(enabled=False)
    )
    off = build_data_flow(_flow_inputs(routes=disabled)).row("intelligence.route.primary")
    assert off is not None and off.status == "off" and "AI budget is $0" in off.reason
    # A $0 personal budget is not a label: the data-flow row still names the configured route.
    _settings(settings_root, **{"intelligence.monthly_budget_usd": 0})
    zero = environment_route_facts(layer="environment", agent_cli_allowed=True, locus="local")
    assert build_data_flow(_flow_inputs(routes=zero)).row("intelligence.route.primary").status != "off"


def test_no_credential_or_endpoint_reaches_the_wire(settings_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = ("sk-live-4f9Q2xT7mZ8kL1pR6vB3nW0y", "hunter2pass", "gw.internal.example")
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_OPENAI_API_KEY", secrets[0])
    monkeypatch.setenv("LEMONCROW_OPENAI_BASE_URL", f"https://svc:{secrets[1]}@{secrets[2]}:8443/v1")
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", f"{secrets[2]}/v1")
    wire = json.dumps(resolve_intelligence_route().to_wire())
    assert not any(secret in wire for secret in secrets)
    assert MODEL_WITHHELD in wire

    _settings(settings_root, **{"intelligence.driver": "openai", "intelligence.model": secrets[0]})
    wire = json.dumps(resolve_intelligence_route().to_wire())
    assert not any(secret in wire for secret in secrets)
    assert MODEL_WITHHELD in wire


# --------------------------------------------------------------------------- #
# fix round: the kill switch on every model call
# --------------------------------------------------------------------------- #

_PROVIDER_MODULES = frozenset(
    {
        "lemoncrow.infra.internal_llm.openai_client",
        "lemoncrow.infra.internal_llm.litellm_client",
        "lemoncrow.infra.internal_llm.ollama_client",
    }
)
_PROVIDER_CALLS = frozenset({"chat", "chat_stream", "summarize", "chat_with_result", "tool_completion"})
_PROVIDER_SDKS = frozenset({"litellm", "openai", "anthropic", "ollama"})
#: Product modules that reach a provider client (or SDK) without the internal_llm facade,
#: each with why the intelligence kill switch does not (or already does) govern it.
_DIRECT_PROVIDER_CALLERS: dict[str, str] = {
    "pro/capabilities/review/intelligence_runtime.py": (
        "execute_structured: resolve_driver_model (the kill switch) runs first, guarded in test_gate_g"
    ),
    "core/capabilities/auth/wizard.py": "provider setup's credential test: the user is configuring the provider",
    "gateway/cli/runtime.py": "the coding runtime's own model loop (the user's agent), not intelligence",
    "gateway/openai_gateway/app.py": "the gateway proxy forwards the user's own model requests",
    "infra/embeddings/openai_embedder.py": "retrieval embeddings, configured separately from model routes",
    "pro/capabilities/live_reviewer/agentic.py": "runtime live reviewer lane (owned execution), not intelligence",
    "pro/capabilities/optimization/local_retrieval.py": "runtime retrieval reranking lane, not intelligence",
    "pro/capabilities/owned_agent_session/keepalive.py": "owned agent session runtime lane",
    "pro/capabilities/owned_agent_session/phase_runner.py": "owned agent session runtime lane",
    "pro/capabilities/owned_execution_lanes.py": "owned execution runtime lanes",
    "pro/capabilities/verified_work/model.py": "verified-work runtime lane (a pinned worker model)",
}


_CLIENT_MODULE_NAMES = frozenset({"openai_client", "litellm_client", "ollama_client", "direct_http"})
_CLIENT_ATTRIBUTE_CALLS = _PROVIDER_CALLS | {"openai_chat_with_result", "anthropic_chat_with_result"}


def _reaches_a_provider(tree: ast.AST) -> bool:
    """Whether a module imports a provider client function or SDK, or calls a client through an imported module."""

    reached = False
    client_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in _PROVIDER_MODULES and any(alias.name in _PROVIDER_CALLS for alias in node.names):
                reached = True
            if node.module == "lemoncrow.infra.internal_llm":  # `from ...internal_llm import openai_client`
                client_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name in _CLIENT_MODULE_NAMES
                )
            if node.module.split(".")[0] in _PROVIDER_SDKS:
                reached = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _PROVIDER_SDKS:
                    reached = True
                if alias.name.startswith("lemoncrow.infra.internal_llm.") and alias.asname:
                    if alias.name.rsplit(".", 1)[-1] in _CLIENT_MODULE_NAMES:
                        client_aliases.add(alias.asname)
    return reached or any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in client_aliases
        and node.attr in _CLIENT_ATTRIBUTE_CALLS
        for node in ast.walk(tree)
    )


def _direct_provider_callers() -> set[str]:
    import lemoncrow

    root = Path(lemoncrow.__file__).parent
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("infra/internal_llm/"):
            continue  # the facade and the provider clients themselves
        if _reaches_a_provider(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            found.add(relative)
    return found


@pytest.mark.parametrize(
    "source,expected",
    [
        ("from lemoncrow.infra.internal_llm import openai_client\nopenai_client.chat_with_result([])", True),
        ("from lemoncrow.infra.internal_llm import litellm_client as llm\nllm.chat([])", True),
        ("import lemoncrow.infra.internal_llm.ollama_client as oc\noc.chat_stream([])", True),
        ("from lemoncrow.infra.internal_llm import direct_http\ndirect_http.openai_chat_with_result()", True),
        ("from lemoncrow.infra.internal_llm.openai_client import chat_with_result", True),
        ("from lemoncrow.infra.internal_llm import openai_client\nopenai_client.OpenAIClientUnavailable", False),
        ("from lemoncrow.infra.internal_llm import chat, chat_stream", False),
    ],
)
def test_the_direct_caller_guard_sees_module_imports_and_attribute_calls(source: str, expected: bool) -> None:
    assert _reaches_a_provider(ast.parse(source)) is expected


def test_every_model_call_entry_point_consults_the_intelligence_policy() -> None:
    """Guard: a model call goes through the gated facade, or is a named, reasoned exception.

    The facade's ``chat``/``chat_stream`` enter ``_model_call`` (the policy and the AI budget) first thing, ``summarize``
    refuses a disabled policy first and gates the computed call, and ``logprobs`` sends nothing. Any new
    direct provider-client or SDK caller fails here until it is gated or
    listed with its reason.
    """

    import importlib

    import lemoncrow.infra.internal_llm as facade

    logprobs_module = importlib.import_module("lemoncrow.infra.internal_llm.logprobs")
    assert _direct_provider_callers() == set(_DIRECT_PROVIDER_CALLERS)

    tree = ast.parse(Path(facade.__file__).read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for name in ("chat", "chat_stream", "summarize"):
        body = functions[name].body
        first = body[1] if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) else body[0]
        if name == "summarize":
            assert (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Call)
                and isinstance(first.value.func, ast.Name)
                and first.value.func.id == "_require_intelligence_enabled"
            ), name
            continue
        assert isinstance(first, ast.With) and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "_model_call"
            for item in first.items
        ), name
    gate = functions["_require_intelligence_enabled"]
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "require_intelligence_enabled"
        for node in ast.walk(gate)
    )
    source = Path(logprobs_module.__file__).read_text(encoding="utf-8")
    assert "model_call()" in source


@pytest.mark.parametrize("stored", [{"intelligence.driver": "not-a-driver"}])
def test_kill_switch_refuses_the_facade_code_ask_and_ask_options(
    stored: dict[str, Any], settings_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.infra.internal_llm as facade
    from lemoncrow.core.service import code_ask
    from lemoncrow.infra.internal_llm import openai_client

    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    sent: list[str] = []
    for name in ("chat", "chat_stream", "summarize", "_resolve_client"):
        monkeypatch.setattr(openai_client, name, lambda *args, _name=name, **kwargs: sent.append(_name))
    _settings(settings_root, **stored)
    with pytest.raises(IntelligenceDisabled):
        facade.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(IntelligenceDisabled):
        facade.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _: None, backend="openai")
    with pytest.raises(IntelligenceDisabled):
        facade.summarize("text")
    assert facade.logprobs("some text") is None

    source = tmp_path / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    for backend in (None, "openai", "claude"):  # an explicit driver is refused too
        with pytest.raises(IntelligenceDisabled):
            code_ask.answer_code_ask(root=tmp_path, path="a.py", question="?", backend=backend, on_delta=lambda _: None)
    assert sent == []

    options = ask_route_options()
    assert options["driver"] == "configured" and options["status"] in {"disabled", "invalid"} and options["reason"]


def test_ask_options_show_the_policy_route_not_the_environment(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_LITELLM_MODEL", "m-lite")
    _settings(settings_root, **{"intelligence.driver": "litellm"})
    assert ask_route_options() == {"driver": "litellm", "model": "m-lite", "status": "ready", "reason": ""}
    operator = _policy(driver="ollama", model="qwen")
    assert ask_route_options(operator)["driver"] == "ollama" and ask_route_options(operator)["model"] == "qwen"


# --------------------------------------------------------------------------- #
# fix round: operator locks, bound policy, empty model, invalid combinations
# --------------------------------------------------------------------------- #


def test_operator_pins_bound_requests_settings_do_not_and_disabled_blocks_everything(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    organization = _policy(driver="litellm", model="org-model")
    with pytest.raises(RouteOverrideRefused):
        resolve_driver_model("openai", None, policy=organization)
    with pytest.raises(RouteOverrideRefused):
        resolve_driver_model("litellm", "other-model", policy=organization)
    assert resolve_driver_model("litellm", None, policy=organization).model == "org-model"
    assert resolve_driver_model(None, "", policy=organization).model == "org-model"
    repository = resolve_intelligence_policy(
        (
            parse_intelligence_policy({"driver": "litellm"}, origin=PolicyOrigin("organization", "t")),
            parse_intelligence_policy({"driver": "ollama", "model": "q"}, origin=PolicyOrigin("repository", "t")),
        )
    )
    with pytest.raises(RouteOverrideRefused):
        resolve_driver_model("litellm", None, policy=repository)
    # Locally, an explicit caller driver still beats the user's own settings.
    _settings(settings_root, **{"intelligence.driver": "litellm", "intelligence.model": "set-model"})
    assert resolve_driver_model("openai", "gpt-x") == intelligence_runtime.ResolvedDriver("openai", "gpt-x")
    # A disabled policy blocks explicit drivers at every layer.
    for disabled in (_policy(enabled=False), _policy(enabled=False, driver="litellm")):
        with pytest.raises(IntelligenceDisabled):
            resolve_driver_model("openai", "gpt-x", policy=disabled)
    # The route projection names the refusal instead of a route.
    assert resolve_intelligence_route(organization).status == "ready"


def test_a_bound_policy_governs_nested_resolution_and_the_facade(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.infra.internal_llm as facade

    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    with bound_intelligence_policy(_policy(driver="litellm", model="org-model")):
        assert resolve_driver_model(None, None) == intelligence_runtime.ResolvedDriver("litellm", "org-model")
        with pytest.raises(RouteOverrideRefused):
            resolve_driver_model("openai", None)
    with bound_intelligence_policy(_policy(enabled=False)):
        with pytest.raises(IntelligenceDisabled):
            facade.chat([{"role": "user", "content": "hi"}])
    assert resolve_driver_model(None, None).driver == "openai"  # unbound again


def test_empty_model_is_no_model_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "gpt-env")
    pinned = _policy(driver="openai", model="gpt-pinned")
    for model in ("", "  "):
        assert resolve_driver_model(None, model, policy=pinned) == resolve_driver_model(None, None, policy=pinned)
        assert route_request(None, model, pinned) == route_request(None, None, pinned)
        assert resolve_driver_model("openai", model).model == "gpt-env"


def test_honest_projection_pinned_model_needs_a_driver_and_ready_is_not_credentials(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    for policy in (_policy(model="gpt-x"), _policy(driver="auto", model="gpt-x")):
        assert not policy.valid and "requires a pinned driver" in policy.errors[0]
        with pytest.raises(IntelligenceDisabled):
            resolve_driver_model("openai", None, policy=policy)
    _settings(settings_root, **{"intelligence.model": "gpt-x"})
    assert resolve_intelligence_route().status == "invalid"
    _settings(settings_root)
    wire = resolve_intelligence_route().to_wire()
    assert wire["status"] == "ready" and "credentials are checked on the first model call" in wire["status_note"]
    assert wire["background_note"] == BACKGROUND_NOTE

    from lemoncrow.core.settings_registry import SETTINGS

    helps = {spec.key: spec.description for spec in SETTINGS if spec.key.startswith("intelligence.")}
    # BI2: both settings govern background work now, and their help says what they do.
    assert "Never degraded" in helps["intelligence.run_on"] and "route_unavailable" in helps["intelligence.run_on"]
    assert "explicit requests still run" in helps["intelligence.background.enabled"]
    row = build_data_flow(_flow_inputs(intelligence=resolve_intelligence_route())).row("intelligence.policy")
    assert row is not None and "background and run_on govern automatic AI Review" in row.detail


# --------------------------------------------------------------------------- #
# fix round: a stored disabled never reads as enabled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content",
    [
        '{"intelligence.enabled": false,}',
        '{"intelligence": {"enabled": false}}',
        '[{"intelligence.enabled": false}]',
        '{"intelligence.driver": null}',
        '{"intelligence.driver": ""}',
        '"off"',
        "",
    ],
)
def test_a_stored_setting_that_cannot_be_read_is_invalid_never_enabled(
    content: str, settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    path = settings_root / "plugin_settings.json"
    path.write_text(content, encoding="utf-8")
    policy = process_intelligence_policy()
    assert not policy.valid
    reason = "; ".join(policy.errors)
    assert "false" not in reason and "null" not in reason  # names the file or key, never the content
    with pytest.raises(IntelligenceDisabled):
        resolve_driver_model("openai", None)


def test_settings_file_absent_is_default_and_unreadable_is_invalid(
    settings_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert process_intelligence_policy() == resolve_intelligence_policy(())
    (settings_root / "plugin_settings.json").mkdir()  # exists, cannot be read as a file
    policy = process_intelligence_policy()
    assert not policy.valid and "plugin_settings.json" in policy.errors[0]


# --------------------------------------------------------------------------- #
# fix round: content access binding and trust
# --------------------------------------------------------------------------- #


def test_content_access_binding_refuses_unknown_policy_undeclared_paths_and_unsafe_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    route = resolve_intelligence_route()
    unknown = bind_content_access(None, route=route)
    assert not unknown.valid and not unknown.decide("README.md").allowed
    assert bind_content_access([], route=route).valid
    bogus = ContentAccessRule("ai_denied", ("secret/**",), "o", "organization_policy", "o")  # type: ignore[arg-type]
    assert not bind_content_access([bogus], route=route).valid
    for pattern in ("../secret/**", "/etc/**", "a/../b/**"):
        wire = {"mode": "ai_denied", "paths": [pattern], "trust_class": "organization_platform"}
        assert not bind_content_access([wire], route=route).valid, pattern
        typed = ContentAccessRule("ai_denied", (pattern,), "o", "organization_platform", "o")
        assert not bind_content_access([typed], route=route).valid, pattern

    store, session, revision = _store(tmp_path)
    calls: list[int] = []
    valid = bind_content_access([], route=route)

    def run(binding: Any, paths: Any) -> Any:
        return run_intelligence(
            store=store,
            request=_request(session, revision, content_access=binding, content_paths=paths),
            executor=_executor("openai", "gpt-4o-mini", calls=calls),
        )

    assert "unknown policy" in run(unknown, ()).error
    undeclared = run(valid, None)
    assert undeclared.status == "failed" and "did not declare" in undeclared.error
    for unsafe in ("a/../secret/k.py", "/etc/passwd"):
        refused = run(valid, (unsafe,))
        assert refused.status == "failed" and "not a safe repository-relative path" in refused.error
    assert calls == []
    assert run(valid, ()).status == "ready" and calls == [1]


def test_deny_rules_match_case_insensitively_allow_rules_stay_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    rules = (ContentAccessRule("ai_denied", ("secrets/**",), "org", "organization_platform", "org-policy"),)
    binding = bind_content_access(rules, route=resolve_intelligence_route())
    for path in ("secrets/k.pem", "Secrets/k.pem", "SECRETS/K.PEM"):
        assert not binding.decide(path).allowed, path
    assert binding.decide("src/secrets.py").allowed
    local_only = (ContentAccessRule("customer_local_only", ("internal/**",), "org", "organization_platform", "o"),)
    assert not bind_content_access(local_only, route=resolve_intelligence_route()).decide("Internal/x.py").allowed


def test_a_binding_for_another_route_trust_refuses_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    rules = (ContentAccessRule("customer_local_only", ("internal/**",), "org", "organization_platform", "o"),)
    # Decided for an attested customer-local route the process does not attest: a lie about trust.
    claimed = resolve_intelligence_route(
        governance=parse_data_governance('{"version":1,"model_routes":{"litellm":{"trust":"customer_local"}}}')
    )
    binding = bind_content_access(rules, route=claimed)
    assert binding.route_trust == "customer_local" and binding.decide("internal/x.py").allowed
    store, session, revision = _store(tmp_path)
    calls: list[int] = []
    outcome = run_intelligence(
        store=store,
        request=_request(session, revision, content_access=binding, content_paths=("internal/x.py",)),
        executor=_executor("litellm", "gpt-4o-mini", calls=calls),
    )
    assert outcome.status == "failed" and "route trust" in outcome.error and calls == []
    explicit = bind_content_access(rules, route_trust="customer_local")
    assert (
        "route trust"
        in run_intelligence(
            store=store,
            request=_request(session, revision, content_access=explicit, content_paths=("internal/x.py",)),
            executor=_executor("litellm", "gpt-4o-mini", calls=calls),
        ).error
    )
    assert calls == []


# --------------------------------------------------------------------------- #
# fix round: cache hits and persistence
# --------------------------------------------------------------------------- #


def test_a_cache_hit_reports_the_current_requests_configured_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "gpt-4o-mini")
    store, session, revision = _store(tmp_path)
    calls: list[int] = []
    explicit = resolve_intelligence_policy(
        (
            parse_intelligence_policy(
                {"driver": "openai", "model": "gpt-4o-mini"}, origin=PolicyOrigin("repository", "t")
            ),
        )
    )
    first = run_intelligence(
        store=store,
        request=_request(session, revision, route_policy=explicit),
        executor=_executor("openai", "gpt-4o-mini", calls=calls),
    )
    second = run_intelligence(
        store=store,
        request=_request(session, revision, route_policy=_policy()),
        executor=_executor("openai", "gpt-4o-mini", calls=calls),
    )
    assert second.cache_hit and second.run_id == first.run_id and calls == [1]
    assert first.configured is not None and first.configured.driver == "openai"
    assert second.configured == route_request(None, None, _policy())
    assert second.configured is not None and second.configured.driver == "auto"
    assert second.to_wire()["configured_route"]["driver"] == "auto"
    # The row keeps what its producing request asked for.
    row = store.get_intelligence_run(session.id, store.list_intelligence_runs(session.id)[0].run_key)
    assert run_configured_route(row) == first.configured


def test_the_raw_requested_model_is_persisted_and_only_the_wire_is_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    raw = "anthropic.claude-3-5-sonnet-20240620-v1:0"
    store, session, revision = _store(tmp_path)
    policy = _policy(driver="litellm", model=raw)
    outcome = run_intelligence(
        store=store,
        request=_request(session, revision, route_policy=policy),
        executor=_executor("litellm", raw),
    )
    assert outcome.status == "ready"
    [row] = store.list_intelligence_runs(session.id)
    recorded = run_configured_route(row)
    assert recorded is not None and recorded.model == raw
    assert RouteRequest.from_record(recorded.to_record()) == recorded
    shown = recorded.to_wire()["model"]
    assert shown in {raw, MODEL_WITHHELD}
