"""G13 — format=auto/compact/json caller-selectable selector (pure helper)."""

from __future__ import annotations

import json

from lemoncrow.pro.capabilities.tool_supervision.compact_output import columnar_decode
from lemoncrow.pro.capabilities.tool_supervision.output_format import (
    apply_output_format,
    normalize_format,
)


def test_normalize_format_defaults_to_auto() -> None:
    assert normalize_format(None) == "auto"
    assert normalize_format("") == "auto"
    assert normalize_format("nonsense") == "auto"
    assert normalize_format("COMPACT") == "compact"
    assert normalize_format(" json ") == "json"


def test_auto_returns_rendered_text_unchanged() -> None:
    result = {"matches": [{"path": "a", "sym": "X"} for _ in range(40)]}
    rendered = "### search\n- a X (x40)"
    text, used = apply_output_format(fmt="auto", result=result, rendered_text=rendered)
    assert text == rendered
    assert used is False


def test_json_forces_raw_json_of_structured_result() -> None:
    result = {"matches": [{"path": "a"}], "mode": "chunks"}
    rendered = "human readable"
    text, used = apply_output_format(fmt="json", result=result, rendered_text=rendered)
    assert used is False
    assert json.loads(text) == result


def test_json_passes_through_string_results() -> None:
    text, used = apply_output_format(fmt="json", result="already a string", rendered_text="x")
    assert text == "already a string"
    assert used is False


def test_compact_emits_columnar_when_it_beats_threshold() -> None:
    rows = [{"path": "src/services/orders.py", "fqn": "orders.OrderService"} for _ in range(40)]
    result = {"matches": rows, "mode": "chunks"}
    rendered = json.dumps(result, separators=(",", ":"))
    text, used = apply_output_format(fmt="compact", result=result, rendered_text=rendered)
    assert used is True
    assert len(text) < len(rendered)
    # Self-describing + losslessly reversible: the consumer can rebuild rows.
    payload = json.loads(text)
    assert payload["encoding"] == "columnar"
    assert payload["row_key"] == "matches"
    assert payload["meta"]["mode"] == "chunks"
    assert columnar_decode(payload["data"]) == rows


def test_compact_falls_back_to_rendered_when_no_savings() -> None:
    result = {"matches": [{"path": "a"}]}  # too small to beat the gate
    rendered = "a"  # tiny rendered form
    text, used = apply_output_format(fmt="compact", result=result, rendered_text=rendered)
    assert used is False
    assert text == rendered


def test_compact_falls_back_when_no_row_list() -> None:
    result = {"content": "just a blob with no row list", "mode": "map"}
    rendered = "rendered blob"
    text, used = apply_output_format(fmt="compact", result=result, rendered_text=rendered)
    assert used is False
    assert text == rendered
