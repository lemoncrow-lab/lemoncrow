"""The review's dollar figure for every model the Action's documents and defaults name.

A call's cost is the figure the provider reported, else the local price table's exact rate for the model (a nearest
sibling's rate is refused, never guessed), else, for the benchmark only, the person's own ``--input-usd-per-mtok`` and
``--output-usd-per-mtok``. On the Action's Anthropic route the provider figure is LiteLLM's ``response_cost``, which is
``None`` for a model LiteLLM's bundled price map does not list, and the SDK-free HTTPS route reports none at all. So for
a current model it is the local table (``pricing.yaml`` over the bundled LiteLLM map) that decides whether the footer
shows a cost and whether ``max-usd-per-pr`` and the benchmark's ``--max-usd`` can be enforced.

These tests fix what that table holds for the models the guide, ``action.yml`` and the benchmark reference name, in both
shapes it is read in: from the installed LiteLLM package, and from the snapshot bundled with LemonCrow when LiteLLM is
not installed (an installed ``lc`` ships no LiteLLM).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import pricing
from lemoncrow.core.capabilities.pricing import get_model_pricing
from lemoncrow.infra.internal_llm import openai_client
from lemoncrow.pro.capabilities.review.evaluation.benchmark import Pricer
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
from lemoncrow.pro.capabilities.review.pr_bot import ci_env
from lemoncrow.pro.capabilities.review.pr_bot.engine import call_usage

ROOT = Path(__file__).resolve().parents[2]
NAMING_FILES = (
    ROOT / "docs" / "setup" / "github-action.md",
    ROOT / "docs" / "reference" / "review-benchmark.md",
    ROOT / "action.yml",
)

# Anthropic's pricing page (https://platform.claude.com/docs/en/about-claude/pricing), read 2026-10-26.
# model -> (input, output, cache hit, 5m cache write, 1h cache write), USD per million tokens. Haiku 5.5 is priced by
# prompt length; this is its card for a prompt of up to 100,000 tokens (over that: 0.50, 2.50, 0.05, 0.625, 1.00).
PUBLISHED_RATES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.50, 4.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0, 8.0),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75, 6.0),
}

# A model a document or default names that has no exact price, with the reason. Each entry is checked to be
# unpriced, so the list cannot go out of date; an id that gains a price must leave it. Empty today.
UNPRICED_ON_PURPOSE: dict[str, str] = {}

MODEL_ID = re.compile(
    r"(?:anthropic/|openai/)?(?:claude-(?:opus|sonnet|haiku|fable|mythos)-\d+(?:-\d+)?|gpt-\d[a-z0-9.]*(?:-[a-z0-9.]+)*)"
)


@pytest.fixture(params=["litellm_package", "bundled_snapshot"])
def price_table(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The pricing table read from the installed LiteLLM package, or from LemonCrow's bundled snapshot."""

    def reload() -> None:
        pricing._load_pricing_table.cache_clear()
        pricing._nearest_table_match.cache_clear()

    monkeypatch.delenv("LEMONCROW_PRICING_LIVE", raising=False)
    if request.param == "bundled_snapshot":
        monkeypatch.setattr(pricing, "_litellm_price_map_path", lambda: None)
    reload()
    yield str(request.param)
    monkeypatch.undo()
    reload()


def named_models(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The model ids the Action's defaults, guide, ``action.yml`` and benchmark reference name."""

    monkeypatch.delenv("LEMONCROW_OPENAI_MODEL", raising=False)
    found = {ci_env.DEFAULT_ANTHROPIC_MODEL, openai_client._resolve_model()}
    for path in NAMING_FILES:
        found.update(MODEL_ID.findall(path.read_text(encoding="utf-8")))
    return found


def execution(model: str, usage: ModelCallUsage) -> StructuredExecution:
    return StructuredExecution(result={}, driver="litellm", model=model, usage=usage)


@pytest.mark.parametrize("model", sorted(PUBLISHED_RATES))
def test_a_current_model_has_an_exact_price_with_the_published_rates(price_table: str, model: str) -> None:
    for name in (model, f"anthropic/{model}"):  # the id as written, and as the LiteLLM route names it
        card = get_model_pricing(name)
        assert card.known and not card.approximate, f"{name} is not priced exactly ({price_table})"
        assert card.model_id == model
        assert (card.input, card.output, card.cache_read, card.cache_write, card.cache_write_1h) == pytest.approx(
            PUBLISHED_RATES[model]
        )


@pytest.mark.parametrize("model", sorted(PUBLISHED_RATES))
def test_the_bot_and_the_benchmark_price_a_call_that_reports_no_cost(price_table: str, model: str) -> None:
    rate_in, rate_out, rate_read, rate_write, _ = PUBLISHED_RATES[model]
    used = ModelCallUsage(
        calls=1,
        input_tokens=10_000,
        output_tokens=500,
        cache_read_input_tokens=4_000,
        cache_write_input_tokens=2_000,
    )
    expected = (10_000 * rate_in + 500 * rate_out + 4_000 * rate_read + 2_000 * rate_write) / 1_000_000
    for name in (model, f"anthropic/{model}"):
        assert call_usage(name, used).cost_usd == pytest.approx(expected)
        assert Pricer().can_price(name)
        cost, source = Pricer().price(execution(name, used))
        assert source == "price_table" and cost == pytest.approx(expected)


@pytest.mark.usefixtures("price_table")
def test_what_the_provider_reports_still_wins_over_the_table() -> None:
    used = ModelCallUsage(calls=1, input_tokens=10_000, output_tokens=500, provider_cost=0.5, currency="USD")
    assert call_usage("claude-haiku-5-5", used).cost_usd == 0.5
    assert Pricer().price(execution("claude-haiku-5-5", used)) == (0.5, "provider")


@pytest.mark.usefixtures("price_table")
def test_haiku_5_5_is_priced_by_prompt_length_each_request_on_its_own() -> None:
    card = get_model_pricing("claude-haiku-5-5")
    assert card.long_context_threshold() == 100_000
    million = 1_000_000
    assert card.request_cost_usd(input_tokens=million, output_tokens=million) == pytest.approx(0.10 + 0.50)
    assert card.request_cost_usd(input_tokens=million, output_tokens=million, long_context=True) == pytest.approx(
        0.50 + 2.50
    )
    assert card.request_cost_usd(cache_read_tokens=million, long_context=True) == pytest.approx(0.05)
    assert card.request_cost_usd(cache_write_tokens=million, long_context=True) == pytest.approx(0.625)
    assert card.request_cost_usd(cache_write_1h_tokens=million, long_context=True) == pytest.approx(1.00)


@pytest.mark.usefixtures("price_table")
def test_a_model_the_table_does_not_list_is_unpriced_never_priced_as_a_neighbour() -> None:
    used = ModelCallUsage(calls=1, input_tokens=1_000, output_tokens=100)
    for name in ("claude-sonnet-9-9", "anthropic/claude-haiku-9-9", "claude-opus-9-9-turbo", "no-such-model"):
        assert call_usage(name, used).cost_usd is None, name
        assert not Pricer().can_price(name), name
        assert Pricer().price(execution(name, used)) == (None, "unknown")


def test_every_model_the_documents_and_defaults_name_has_a_price_or_a_stated_reason(
    price_table: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = named_models(monkeypatch)
    # The scan must be reading something: the defaults, and the models the guide's cost section is written for.
    assert {"claude-sonnet-4-6", "gpt-4o-mini", "claude-sonnet-5-5"} <= {
        re.sub(r"^(?:anthropic|openai)/", "", n) for n in named
    }

    def priced_exactly(name: str) -> bool:
        card = get_model_pricing(name)
        return card.known and not card.approximate

    missing = sorted(name for name in named if name not in UNPRICED_ON_PURPOSE and not priced_exactly(name))
    assert not missing, (
        f"{missing} are named by a document or a default but have no exact price ({price_table}): add them to "
        "core/capabilities/pricing.yaml from the provider's pricing page, or list them in UNPRICED_ON_PURPOSE with why"
    )
    for name, reason in UNPRICED_ON_PURPOSE.items():
        assert reason.strip() and not priced_exactly(name), f"{name} now has a price: remove it from the list"


def test_the_default_anthropic_model_is_priced_by_the_published_rates(price_table: str) -> None:
    card = get_model_pricing(f"anthropic/{ci_env.DEFAULT_ANTHROPIC_MODEL}")
    assert card.known and not card.approximate
    assert ci_env.DEFAULT_ANTHROPIC_MODEL in PUBLISHED_RATES
