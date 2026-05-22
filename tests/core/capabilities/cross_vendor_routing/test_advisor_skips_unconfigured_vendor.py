from __future__ import annotations
from unittest.mock import patch

from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfig
from atelier.core.capabilities.cross_vendor_routing.router import CrossVendorRouter


def test_advisor_skips_unconfigured_vendor(tmp_path) -> None:
    # Isolate env-key detection from host-CLI detection so that installed CLIs
    # (e.g. agy on this machine) don't accidentally mark google as configured.
    def _which_no_google(command: str) -> str | None:
        return None if command in ("agy", "antigravity", "codex") else f"/fake/{command}"

    with patch("atelier.core.capabilities.cross_vendor_routing.configuration.shutil.which", side_effect=_which_no_google):
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
