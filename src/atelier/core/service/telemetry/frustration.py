"""Lexical frustration matching for user-provided inputs only.

Hard rule: never log, never hash, never bucket, and never include the matched
substring or any portion of the input. Only the category and surface name may
leave this function.
"""

from __future__ import annotations

from importlib import resources
from typing import Literal

import yaml

from atelier.core.service.telemetry.config import lexical_frustration_enabled

Surface = Literal["cli_input", "mcp_prompt", "api_body"]

_LEXICON: dict[str, list[str]] | None = None


def match_frustration(
    text: str | None,
    *,
    surface: Surface,
    session_id: str | None = None,
    emit: bool = True,
) -> str | None:
    if not text or not lexical_frustration_enabled():
        return None
    lowered = text.lower()
    for category, patterns in _load_lexicon().items():
        if any(pattern in lowered for pattern in patterns):
            if emit:
                from atelier.core.service.telemetry.emit import emit_product

                props = {"category": category, "surface": surface}
                if session_id:
                    props["session_id"] = session_id
                emit_product("frustration_signal_lexical", **props)
            return category
    return None


def _load_lexicon() -> dict[str, list[str]]:
    global _LEXICON
    if _LEXICON is not None:
        return _LEXICON
    with (
        resources.files("atelier.core.service.telemetry")
        .joinpath("frustration_lexicon.yaml")
        .open("r", encoding="utf-8") as handle
    ):
        loaded = yaml.safe_load(handle) or {}
    categories = loaded.get("categories", {}) if isinstance(loaded, dict) else {}
    _LEXICON = {
        str(category): [str(pattern).lower() for pattern in patterns]
        for category, patterns in categories.items()
        if isinstance(patterns, list)
    }
    return _LEXICON
