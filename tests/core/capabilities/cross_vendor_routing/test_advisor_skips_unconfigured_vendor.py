from __future__ import annotations

from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfig
from atelier.core.capabilities.cross_vendor_routing.router import CrossVendorRouter


def test_advisor_skips_unconfigured_vendor(tmp_path) -> None:
    router = CrossVendorRouter(
        RouteConfig(enabled_vendors=["anthropic", "google"]),
        env={"ANTHROPIC_API_KEY": "anthropic-key"},
    )

    recommendation = router.recommend(
        tool_name="read",
        task_text="inspect the docs",
        session_state={"expected_input_tokens": 1200, "expected_output_tokens": 200},
    )

    assert recommendation.vendor == "anthropic"
    assert all(candidate.vendor != "google" for candidate in recommendation.alternatives)
