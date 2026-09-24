"""Tool arguments, coerced the way the main package's MCP layer coerces them.

Hosts sometimes send ``"20"`` for an integer, ``"true"`` for a boolean or a
JSON string for an array. The main package accepts those spellings, so the
client's executors accept them too, and refuse anything else by name.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from typing import Any, Final

from ..errors import AgentAction, ClientError, ErrorCode

__all__ = ["bool_arg", "int_arg", "invalid_arg", "parse_json_list", "text_arg"]

_TRUE: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on"})
_FALSE: Final[frozenset[str]] = frozenset({"false", "0", "no", "off"})


def invalid_arg(tool: str, name: str, expected: str, raw: object) -> ClientError:
    return ClientError(
        ErrorCode.PAYLOAD_INVALID,
        f"{tool}: {name} must be {expected}, got {raw!r}",
        action=AgentAction.FIX_REQUEST,
    )


def text_arg(tool: str, args: Mapping[str, Any], name: str) -> str | None:
    raw = args.get(name)
    if raw is None or isinstance(raw, str):
        return raw
    raise invalid_arg(tool, name, "a string", raw)


def int_arg(tool: str, args: Mapping[str, Any], name: str, default: int) -> int:
    raw = args.get(name, default)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            pass
    raise invalid_arg(tool, name, "an integer", raw)


def bool_arg(tool: str, args: Mapping[str, Any], name: str, default: bool) -> bool:
    raw = args.get(name, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str) and raw.strip().lower() in _TRUE | _FALSE:
        return raw.strip().lower() in _TRUE
    raise invalid_arg(tool, name, "a boolean", raw)


def parse_json_list(raw: object) -> object:
    """A list sent as a JSON (or Python literal) string, as some hosts do; else ``raw``."""
    if not isinstance(raw, str):
        return raw
    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(raw)
        except (ValueError, SyntaxError):
            continue
        if isinstance(parsed, list):
            return parsed
    return raw
