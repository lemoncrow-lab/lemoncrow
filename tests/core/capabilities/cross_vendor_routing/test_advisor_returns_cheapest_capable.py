from __future__ import annotations

from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfig
from atelier.core.capabilities.cross_vendor_routing.router import CrossVendorRouter


def test_advisor_returns_cheapest_capable(tmp_path) -> None:
    router = CrossVendorRouter(
        RouteConfig(enabled_vendors=["anthropic", "openai", "google"]),
        env={
            "ANTHROPIC_API_KEY": "anthropic-key",
            "OPENAI_API_KEY": "openai-key",
            "GOOGLE_API_KEY": "google-key",
        },
    )

    recommendation = router.recommend(
        tool_name="read",
        task_text="find the failing test",
        session_state={"expected_input_tokens": 1200, "expected_output_tokens": 200, "turn_number": 1},
    )

    assert recommendation.vendor == "google"
    assert recommendation.model == "gemini-flash"
    assert recommendation.alternatives[0].estimated_cost_usd <= recommendation.alternatives[1].estimated_cost_usd
