"""Tool-argument coercion: stringified scalar/structured values are self-healed.

Some MCP clients intermittently serialise tool-call argument *values* as strings
(``"20"`` instead of ``20``, ``"true"`` instead of ``True``, ``'["a"]'`` instead of
``["a"]``). The server coerces them to each parameter's resolved type before
validation so otherwise-valid calls don't fail.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from lemoncrow.gateway.adapters.mcp_server import (
    _COERCE_UNCHANGED,
    _coerce_str_to_annotation,
    mcp_tool,
)


@pytest.fixture(autouse=True)
def _restore_tools_registry() -> Iterator[None]:
    """Probe tools registered via ``@mcp_tool`` in these tests mutate the global
    ``TOOLS`` registry; undo that after each test so the ``tools/list`` exact-
    surface assertions in test_mcp_tool_handlers.py stay isolated."""
    from lemoncrow.gateway.adapters.mcp_server import TOOLS

    before = set(TOOLS)
    yield
    for tool_name in set(TOOLS) - before:
        TOOLS.pop(tool_name, None)


def test_coerce_str_to_annotation_scalars_and_structured() -> None:
    assert _coerce_str_to_annotation("20", int | None) == 20
    assert _coerce_str_to_annotation("1.5", float) == 1.5
    assert _coerce_str_to_annotation("true", bool) is True
    assert _coerce_str_to_annotation("False", bool) is False
    assert _coerce_str_to_annotation("0", bool) is False
    # JSON and Python-repr collections both heal.
    assert _coerce_str_to_annotation('["a", "b"]', list[str] | None) == ["a", "b"]
    assert _coerce_str_to_annotation("['a', 'b']", list) == ["a", "b"]
    assert _coerce_str_to_annotation('{"k": 1}', dict) == {"k": 1}


def test_coerce_str_to_annotation_leaves_strings_and_uncoercible_alone() -> None:
    # A string is acceptable for a str / Optional[str] / Union-with-str param.
    assert _coerce_str_to_annotation("hello", str) is _COERCE_UNCHANGED
    assert _coerce_str_to_annotation("hello", str | None) is _COERCE_UNCHANGED
    # Non-numeric text for an int stays untouched (Pydantic will reject it).
    assert _coerce_str_to_annotation("not-an-int", int) is _COERCE_UNCHANGED
    # Ambiguous bool text is left for downstream validation.
    assert _coerce_str_to_annotation("maybe", bool) is _COERCE_UNCHANGED


def test_mcp_tool_handler_accepts_fully_stringified_args() -> None:
    captured: dict[str, object] = {}

    @mcp_tool(name="_coerce_probe_tool")
    def tool__coerce_probe_tool(
        count: int | None = None,
        flag: bool = False,
        names: list[str] | None = None,
        label: str = "x",
    ) -> dict[str, bool]:
        captured.update(count=count, flag=flag, names=names, label=label)
        return {"ok": True}

    # Every value arrives as a string, as a misbehaving client would send them.
    tool__coerce_probe_tool({"count": "20", "flag": "true", "names": '["a", "b"]', "label": "hello"})
    assert captured == {"count": 20, "flag": True, "names": ["a", "b"], "label": "hello"}

    # Native (already-typed) values keep working unchanged.
    tool__coerce_probe_tool({"count": 7, "flag": False, "names": ["z"], "label": "y"})
    assert captured == {"count": 7, "flag": False, "names": ["z"], "label": "y"}


def test_mcp_tool_handler_still_rejects_genuinely_invalid_args() -> None:
    @mcp_tool(name="_coerce_probe_strict")
    def tool__coerce_probe_strict(count: int | None = None) -> dict[str, bool]:
        return {"ok": True}

    # "abc" is not coercible to int -> left for Pydantic, which rejects it.
    with pytest.raises(ValidationError):
        tool__coerce_probe_strict({"count": "abc"})
