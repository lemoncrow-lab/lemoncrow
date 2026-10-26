"""The pull request bot on a cross-file bug (G4): found with repository context, missed without it.

A real git origin, a fake GitHub and a fake model, like ``test_review_pr_bot_flow.py``. The fake model here is
deliberately blind to anything outside the prompt it is given: it can report "the signature of ``quote`` changed
and ``build_invoice`` still calls it with two arguments" only if the caller's code is in that prompt. So the test
proves the pack reaches the investigator, not that a model is clever.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import repo_context as rc
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
from lemoncrow.pro.capabilities.review.pr_bot import engine
from lemoncrow.pro.capabilities.review.pr_bot.render import compact_count
from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state
from tests.gateway.pr_bot_support import FakeModel, World, build_world

PRICING_OLD = 'PRICES = {"basic": 10, "pro": 25}\n\n\ndef quote(plan, seats):\n    return PRICES[plan] * seats\n'
PRICING_NEW = (
    'PRICES = {"basic": 10, "pro": 25}\n'
    'REGION_FACTORS = {"eu": 1.0, "us": 1.2}\n\n\n'
    "def quote(plan, seats, region):\n    return PRICES[plan] * seats * REGION_FACTORS[region]\n"
)
INVOICE = (
    "from api.billing.pricing import quote\n\n\n"
    "def build_invoice(customer):\n"
    "    total = quote(customer.plan, customer.seats)\n"
    '    return {"customer": customer.id, "total": total}\n'
)
REPORTS = (
    "from api.billing.pricing import quote\n\n\n"
    "def monthly_revenue(customers):\n"
    "    return sum(quote(c.plan, c.seats, c.region) for c in customers)\n"
)
BASE = {
    "src/api/billing/pricing.py": PRICING_OLD,
    "src/api/billing/invoice.py": INVOICE,
    "src/api/billing/reports.py": REPORTS.replace("c.seats, c.region", "c.seats"),
    "tests/test_pricing.py": 'from api.billing.pricing import quote\n\n\ndef test_quote():\n    assert quote("basic", 3) == 30\n',
    "src/api/plain.py": "VALUE = 1\n",
}
SIGNATURE_PUSH = {"src/api/billing/pricing.py": PRICING_NEW}
TITLE = "quote() needs a region but build_invoice still calls it with two arguments"


class CallerAwareModel(FakeModel):
    """Confirms a broken caller only when the prompt shows one; reports tokens in proportion to the prompt."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[dict[str, Any]] = []
        self.sizes: list[int] = []

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        prompt = json.loads(kwargs["prompt"])
        if (
            "hypotheses" in kwargs["output_schema"].get("properties", {})
            or "decisions" in kwargs["output_schema"].get("properties", {})
            or "violated" in json.dumps(kwargs["output_schema"])
        ):
            return super().__call__(**kwargs)
        self.prompts.append(prompt)
        self.sizes.append(len(kwargs["prompt"]) + len(kwargs["system"]))
        hypothesis = prompt["hypothesis"]["value"]
        targets: list[str] = hypothesis["target_ids"]
        source = prompt["snapshot_source"]["value"]
        text = "\n".join(str(source[t].get("text", "")) for t in targets if t in source)
        usage = ModelCallUsage(calls=1, input_tokens=self.sizes[-1] // 4, output_tokens=120)
        signature = re.search(r"def (\w+)\(([^)]*)\)", text)
        shown = ((prompt.get("repo_context") or {}).get("value") or {}).get("items", [])
        broken = None
        if signature is not None:
            arity = len([part for part in signature.group(2).split(",") if part.strip()])
            for item in shown:
                if item["kind"] != "caller":
                    continue
                call = re.search(rf"{signature.group(1)}\(([^)]*)\)", item["text"])
                if call and len([part for part in call.group(1).split(",") if part.strip()]) < arity:
                    broken = item
                    break
        result: dict[str, Any] = {
            "status": "not_confirmed",
            "category": hypothesis["category"],
            "target_ids": targets,
            "title": "",
            "explanation": "",
            "consequence": "",
            "evidence_refs": [],
            "rule_refs": [],
        }
        if broken is not None:
            result.update(
                status="confirmed",
                title=TITLE,
                explanation=f"{broken['path']} calls {signature.group(1) if signature else ''} with the old signature.",  # type: ignore[union-attr]
                consequence="build_invoice raises TypeError for every invoice.",
                # The target's own ref and the caller's: context may corroborate, never prove alone.
                evidence_refs=[f"target:{targets[0]}", broken["ref"]],
            )
        self.calls.append(
            {"path": str(source[targets[0]]["path"]), "targets": targets, "category": hypothesis["category"]}
        )
        return StructuredExecution(result=result, driver="openai", model=self.model, usage=usage)

    def shown_paths(self) -> list[str]:
        return sorted(
            {
                item["path"]
                for prompt in self.prompts
                for item in ((prompt.get("repo_context") or {}).get("value") or {}).get("items", [])
            }
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.delenv("LEMONCROW_REVIEW_REPO_CONTEXT", raising=False)
    monkeypatch.delenv("LEMONCROW_REVIEW_REPO_CONTEXT_TOKENS", raising=False)
    built = build_world(tmp_path, monkeypatch, BASE)
    built.model = CallerAwareModel()
    return built


def model_of(world: World) -> CallerAwareModel:
    assert isinstance(world.model, CallerAwareModel)
    return world.model


# --------------------------------------------------------------------------- #
# the bug is caught because the caller is in the prompt
# --------------------------------------------------------------------------- #


def test_a_changed_signature_with_a_stale_caller_in_another_file_is_caught_with_the_context_pack(world: World) -> None:
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    report = world.review()

    assert [finding.title for finding in report.findings] == [TITLE]
    (finding,) = report.findings
    assert (finding.severity, finding.path) == ("p1", "src/api/billing/pricing.py")
    # The model was shown the caller that was not updated (and the one that was), from files the PR did not touch.
    prompt = model_of(world).prompts[0]
    items = prompt["repo_context"]["value"]["items"]
    stale = next(item for item in items if item["path"] == "src/api/billing/invoice.py")
    assert "total = quote(customer.plan, customer.seats)" in stale["text"]
    assert (stale["kind"], stale["in_change"], stale["symbol"]) == ("caller", False, "build_invoice")
    assert prompt["repo_context"]["authority"] == "data:reviewed_repository_source"
    assert prompt["repo_context_policy"]["authority"].startswith("instruction:")
    # The citation is a registry ref, bound to the target, so the existing validator accepted it.
    registry = {row["ref"]: row for row in prompt["evidence_registry"]["value"]}
    assert registry[stale["ref"]]["kind"] == "repo_context"
    # One inline comment on the changed signature, posted by the bot.
    (inline,) = world.github.inline()
    assert inline["path"] == "src/api/billing/pricing.py" and TITLE in inline["body"]


def test_the_same_bug_is_missed_without_the_context_pack(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_REPO_CONTEXT", "0")
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    report = world.review()

    assert report.findings == ()
    prompts = model_of(world).prompts
    assert prompts and all("repo_context" not in prompt for prompt in prompts)
    assert report.state is not None and report.state.usage.context_tokens == 0


def test_context_cannot_be_the_only_proof_of_a_finding(world: World) -> None:
    class ContextOnly(CallerAwareModel):
        def __call__(self, **kwargs: Any) -> StructuredExecution:
            execution = super().__call__(**kwargs)
            if execution.result["status"] == "confirmed":
                execution.result["evidence_refs"] = execution.result["evidence_refs"][1:]  # the context ref alone
            return execution

    world.model = ContextOnly()
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    assert world.review().findings == ()


def test_a_ref_the_pack_never_offered_is_rejected(world: World) -> None:
    class Invented(CallerAwareModel):
        def __call__(self, **kwargs: Any) -> StructuredExecution:
            execution = super().__call__(**kwargs)
            if execution.result["status"] == "confirmed":
                execution.result["evidence_refs"] = [
                    execution.result["evidence_refs"][0],
                    "context:caller:src/x.py:1-2@sha256:0",
                ]
            return execution

    world.model = Invented()
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    assert world.review().findings == ()


# --------------------------------------------------------------------------- #
# cost is visible and bounded
# --------------------------------------------------------------------------- #


def test_the_context_tokens_are_in_the_usage_the_summary_reports(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    with_context = world.review()
    assert with_context.state is not None
    on = with_context.state.usage
    calls_on = model_of(world).sizes[:]

    # The same pull request once more with the pack off, in a fresh world (no state to carry forward).
    monkeypatch.setenv("LEMONCROW_REVIEW_REPO_CONTEXT", "0")
    (world.root / "other").mkdir()
    other = build_world(world.root / "other", monkeypatch, BASE)
    other.model = CallerAwareModel()
    other.origin.push(SIGNATURE_PUSH, "quote needs a region")
    without = other.review()
    assert without.state is not None
    off = without.state.usage
    calls_off = model_of(other).sizes[:]

    assert on.context_tokens > 0 and off.context_tokens == 0
    # The provider-reported input tokens contain the context: what it added is what the pack says it added.
    assert on.input_tokens is not None and off.input_tokens is not None
    added = on.input_tokens - off.input_tokens
    assert added == pytest.approx(on.context_tokens, rel=0.08)
    assert (sum(calls_on) - sum(calls_off)) // 4 == pytest.approx(on.context_tokens, rel=0.08)
    # Same number of calls: context adds tokens, never a call.
    assert on.calls == off.calls

    (summary,) = world.github.summary_comments()
    footer = summary["body"].split("<sub>")[-1].split("</sub>")[0]
    assert f"~{compact_count(on.context_tokens)} of it repository context" in footer
    state = parse_state(summary["body"])
    assert state is not None and state.usage.context_tokens == on.context_tokens
    assert state.usage.to_wire()["context_tokens"] == on.context_tokens


def test_the_context_tokens_add_up_across_pushes_like_every_other_usage_total(world: World) -> None:
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    first = world.review()
    assert first.state is not None
    world.origin.push({"src/api/billing/pricing.py": PRICING_NEW + "\n# note\n"}, "touch pricing again")
    second = world.review()
    assert second.state is not None and first.state.usage.context_tokens > 0 and second.state.usage.context_tokens > 0
    assert (
        second.state.usage_total.context_tokens == first.state.usage.context_tokens + second.state.usage.context_tokens
    )


def test_the_pack_stays_inside_the_depths_budget_on_a_crowded_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LEMONCROW_REVIEW_REPO_CONTEXT", raising=False)
    crowd = dict(BASE)
    for index in range(40):
        crowd[f"src/api/users/handler_{index:02d}.py"] = (
            "from api.billing.pricing import quote\n\n\n"
            f"def handle_{index:02d}(request):\n"
            + "".join(f"    step_{n} = request.values[{n}]\n" for n in range(25))
            + "    return quote(request.plan, request.seats)\n"
        )
    world = build_world(tmp_path, monkeypatch, crowd)
    world.model = CallerAwareModel()
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    report = world.review()

    budget = rc.BUDGETS["auto"]
    for prompt in model_of(world).prompts:
        value = prompt["repo_context"]["value"]
        assert sum(1 for item in value["items"] if item["kind"] == "caller") <= budget.callers
        assert value["omitted"].get("caller", 0) > 0
    assert report.state is not None
    assert 0 < report.state.usage.context_tokens <= report.state.usage.calls * budget.tokens
    # And the finding is still there: a crowded repository does not push the stale callers out.
    assert [finding.title for finding in report.findings] == [TITLE]


def test_the_token_ceiling_can_be_lowered_without_a_code_change(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    floor = rc.RepoContextBlock((), {}, 0, False).tokens()
    monkeypatch.setenv("LEMONCROW_REVIEW_REPO_CONTEXT_TOKENS", str(floor + 400))
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    report = world.review()
    assert report.state is not None
    assert 0 < report.state.usage.context_tokens <= report.state.usage.calls * (floor + 400)


# --------------------------------------------------------------------------- #
# what never reaches the model
# --------------------------------------------------------------------------- #

POLICY = "ai_content_access:\n  - mode: ai_denied\n    paths: ['src/api/vault/**']\n"


def test_a_path_the_repositorys_content_access_policy_denies_never_reaches_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LEMONCROW_REVIEW_REPO_CONTEXT", raising=False)
    files = {
        **BASE,
        ".lemoncrow/review.yaml": POLICY,
        "src/api/vault/ledger.py": "from api.billing.pricing import quote\n\n\ndef vault_price(x):\n    return quote('pro', x)  # VAULT-ONLY\n",
    }
    world = build_world(tmp_path, monkeypatch, files)
    world.model = CallerAwareModel()
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    report = world.review()

    everything = json.dumps(model_of(world).prompts)
    assert "VAULT-ONLY" not in everything and "src/api/vault" not in everything
    assert "src/api/billing/invoice.py" in model_of(world).shown_paths()
    # The allowed caller still carries the finding.
    assert [finding.title for finding in report.findings] == [TITLE]


def test_credentials_in_the_repository_never_reach_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_REVIEW_REPO_CONTEXT", raising=False)
    files = {
        **BASE,
        ".env": "BILLING_TOKEN=sk-live-abcdefghijklmnopqrstuvwxyz012345  # quote(plan, seats)\n",
        "src/api/billing/gateway.py": 'from api.billing.pricing import quote\n\n\ndef charge(x):\n    api_key = "sk-live-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"\n    return quote("pro", x, "eu")\n',
    }
    world = build_world(tmp_path, monkeypatch, files)
    world.model = CallerAwareModel()
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    world.review()

    everything = json.dumps(model_of(world).prompts)
    assert "sk-live" not in everything and "BILLING_TOKEN" not in everything


def test_an_agent_cli_route_gets_no_pack_because_it_has_read_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AsRequested(CallerAwareModel):
        """The executor answers as the route it was asked for, like a real one."""

        def __call__(self, **kwargs: Any) -> StructuredExecution:
            return replace(super().__call__(**kwargs), driver=kwargs["driver"], model=kwargs["model"] or "")

    world.model = AsRequested()
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "claude")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    world.review()
    prompts = model_of(world).prompts
    assert prompts and all("repo_context" not in prompt for prompt in prompts)


# --------------------------------------------------------------------------- #
# the pack is part of what a reviewed unit's result means
# --------------------------------------------------------------------------- #


def test_turning_the_pack_on_or_off_re_reviews_everything_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class Governance:
        rules: tuple[Any, ...] = ()
        content_access: tuple[Any, ...] = ()

    monkeypatch.delenv("LEMONCROW_REVIEW_REPO_CONTEXT", raising=False)
    on = engine.policy_digest(Governance(), "auto")
    monkeypatch.setenv("LEMONCROW_REVIEW_REPO_CONTEXT", "0")
    off = engine.policy_digest(Governance(), "auto")
    assert on != off


def test_a_push_that_changes_nothing_reviewed_calls_no_model_and_sends_no_context(world: World) -> None:
    world.origin.push(SIGNATURE_PUSH, "quote needs a region")
    world.review(depth="thorough")  # covers all three targets of the change in one run
    model_of(world).prompts.clear()
    again = world.review(depth="thorough")
    assert again.noop and model_of(world).prompts == []
    assert again.state is not None and again.state.usage.context_tokens == 0
