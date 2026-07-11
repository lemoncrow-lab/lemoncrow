from __future__ import annotations

from pathlib import Path

from lemoncrow.core.foundation.watchdog_profiles import save_watchdog_profile_config
from lemoncrow.gateway.adapters.runtime import ContextRuntime


def test_runtime_session_uses_persisted_watchdog_profile(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    runtime = ContextRuntime(root=root)
    save_watchdog_profile_config(
        root,
        active_profile="coding",
        profiles={"coding": {"repeated_tool_call": 0.0}},
    )

    with runtime.run(domain="coding", task="Disable repeated tool call for this workspace") as session:
        watchdog_names = {w.name for w in session.watchdogs}

    assert "repeated_tool_call" not in watchdog_names
    assert "repeated_command_failure" in watchdog_names
