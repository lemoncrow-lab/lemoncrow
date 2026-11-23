"""The personal preferred fixing agent: route step 2 of a Review Fix (plan §3.22/§3.23).

Personal, never policy. It only chooses which local coding agent a Fix starts
when the exact authoring session is unavailable; it cannot change what is
reviewed, required or allowed. Locally it lives in the user-level settings
file (``<LEMONCROW_ROOT>/plugin_settings.json``, key ``review.fix_agent``),
written with ``lemoncrow settings set review.fix_agent <agent>`` and read fresh
on every Fix preview. A hosted server has no durable personal-settings store
yet, so it projects the setting read-only and never reads its own machine's
file on a reviewer's behalf.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

FIX_AGENT_SETTING: Final[str] = "review.fix_agent"
FIX_AGENT_EDIT_COMMAND: Final[str] = "lemoncrow settings set review.fix_agent <claude|codex|opencode>"
FIX_AGENT_UNSET_REASON: Final[str] = f"no preferred fixing agent is set; run `{FIX_AGENT_EDIT_COMMAND}`"

PreferenceSource = Literal["personal_settings", "unset", "invalid"]


@dataclass(frozen=True, slots=True)
class FixAgentPreference:
    """The effective preferred fixing agent and where it came from."""

    host: str
    source: PreferenceSource
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "source": self.source,
            "detail": self.detail,
            "setting_key": FIX_AGENT_SETTING,
            "edit_command": FIX_AGENT_EDIT_COMMAND,
        }


def fix_agent_choices() -> tuple[str, ...]:
    from lemoncrow.core.settings import find_by_key

    spec = find_by_key(FIX_AGENT_SETTING)
    return spec.choices if spec is not None else ()


def settings_root() -> Path:
    """The LemonCrow state root this process serves (``--root`` when given)."""

    from lemoncrow.core.foundation.paths import default_store_root

    with contextlib.suppress(Exception):
        from lemoncrow.pro.capabilities.model_setup.transport import active_store_root

        active = active_store_root()
        if active is not None:
            return Path(active)
    return default_store_root()


def local_fix_agent_preference(root: Path | None = None) -> FixAgentPreference:
    """The reviewer's preferred fixing agent from the user-level settings file."""

    from lemoncrow.core.settings import load_raw

    try:
        raw = load_raw(root if root is not None else settings_root()).get(FIX_AGENT_SETTING)
    except Exception:  # settings must never break a Fix preview; unset is the honest answer
        raw = None
    value = str(raw or "").strip().lower()
    if not value:
        return FixAgentPreference("", "unset", FIX_AGENT_UNSET_REASON)
    choices = fix_agent_choices()
    if value not in choices:
        return FixAgentPreference(
            "",
            "invalid",
            f"{FIX_AGENT_SETTING} is {value!r}; LemonCrow can start {', '.join(choices)}",
        )
    return FixAgentPreference(value, "personal_settings", "your preferred fixing agent")


def hosted_fix_agent_projection() -> dict[str, Any]:
    """Read-only hosted view: no durable personal store exists there yet."""

    return {
        "effective_value": None,
        "origin": {"layer": "personal_presentation", "source": "unset", "scope_id": ""},
        "editable": False,
        "edit_source": f"your local LemonCrow: {FIX_AGENT_EDIT_COMMAND}",
        "reason": "A Fix started from your local LemonCrow uses the agent set there.",
        "choices": list(fix_agent_choices()),
        "routing": ["exact_session", "preferred_agent", "prompt_fallback"],
    }


__all__ = [
    "FIX_AGENT_EDIT_COMMAND",
    "FIX_AGENT_SETTING",
    "FIX_AGENT_UNSET_REASON",
    "FixAgentPreference",
    "fix_agent_choices",
    "hosted_fix_agent_projection",
    "local_fix_agent_preference",
    "settings_root",
]
