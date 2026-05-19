from __future__ import annotations

from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfig
from atelier.core.capabilities.cross_vendor_routing.router import CrossVendorRouter


def test_advisor_respects_phase_pin(tmp_path) -> None:
    router = CrossVendorRouter(
        RouteConfig(enabled_vendors=["anthropic", "openai", "google"], edit_mode="pin-actual-vendor"),
        env={
            "ANTHROPIC_API_KEY": "anthropic-key",
            "OPENAI_API_KEY": "openai-key",
            "GOOGLE_API_KEY": "google-key",
        },
    )

    recommendation = router.recommend(
        tool_name="edit",
        task_text="implement the bug fix",
        session_state={"expected_input_tokens": 3000, "expected_output_tokens": 600, "turn_number": 12},
        actual_vendor="anthropic",
    )

    assert recommendation.vendor == "anthropic"
    assert recommendation.model in {"claude-sonnet-4-6", "claude-opus-4-1"}
