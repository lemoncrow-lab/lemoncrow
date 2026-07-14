"""N6 (savings gate) + N7 (columnar + string-intern encoding)."""

from __future__ import annotations

import json

from lemoncrow.pro.capabilities.tool_supervision.compact_output import (
    COLUMNAR_FORMAT,
    DEFAULT_SAVINGS_THRESHOLD,
    columnar_decode,
    columnar_encode,
    columnar_encode_json,
    gate_compact,
    savings_ratio,
)

# --------------------------------------------------------------------------- #
# N6 — savings gate                                                            #
# --------------------------------------------------------------------------- #


def test_gate_picks_json_below_threshold() -> None:
    original = json.dumps([{"a": 1, "b": 2}])  # small, low-redundancy
    compact_form = original[:-1]  # ~1 char smaller, well below 15%
    gate = gate_compact(original, compact_form)
    assert gate.used_compact is False
    assert gate.chosen == original
    assert gate.savings_ratio < DEFAULT_SAVINGS_THRESHOLD


def test_gate_picks_compact_above_threshold() -> None:
    original = "x" * 1000
    compact_form = "x" * 500  # 50% smaller
    gate = gate_compact(original, compact_form)
    assert gate.used_compact is True
    assert gate.chosen == compact_form
    assert gate.savings_ratio >= DEFAULT_SAVINGS_THRESHOLD


def test_gate_never_inflates() -> None:
    original = "small"
    inflated = "this is much much longer than the original payload"
    gate = gate_compact(original, inflated)
    assert gate.used_compact is False
    assert gate.chosen == original
    # An inflating encoding reports a 0.0 ratio, never negative.
    assert savings_ratio(original, inflated) == 0.0


def test_gate_respects_custom_threshold() -> None:
    original = "y" * 100
    compact_form = "y" * 80  # exactly 20% savings
    assert gate_compact(original, compact_form, threshold=0.25).used_compact is False
    assert gate_compact(original, compact_form, threshold=0.20).used_compact is True


def test_savings_ratio_empty_original() -> None:
    assert savings_ratio("", "anything") == 0.0


# --------------------------------------------------------------------------- #
# N7 — columnar + string intern                                               #
# --------------------------------------------------------------------------- #


def test_columnar_round_trip_basic() -> None:
    rows = [
        {"path": "src/a.py", "line": 1, "sym": "Foo"},
        {"path": "src/a.py", "line": 9, "sym": "Bar"},
        {"path": "src/b.py", "line": 3, "sym": "Foo"},
    ]
    encoded = columnar_encode(rows)
    assert encoded["format"] == COLUMNAR_FORMAT
    assert columnar_decode(encoded) == rows


def test_columnar_interns_repeated_strings_into_legend() -> None:
    rows = [{"path": "src/a.py"} for _ in range(5)]
    encoded = columnar_encode(rows)
    # The repeated path is interned once into the legend.
    assert encoded["legend"] == ["src/a.py"]
    # Each column entry references the legend rather than repeating the string.
    assert all(cell == {"$": 0} for cell in encoded["columns"]["path"])
    assert columnar_decode(encoded) == rows


def test_columnar_does_not_intern_singletons() -> None:
    rows = [{"path": "only_once.py", "line": 1}]
    encoded = columnar_encode(rows)
    # A string seen once is stored inline (interning it would add legend bytes).
    assert encoded["legend"] == []
    assert encoded["columns"]["path"] == ["only_once.py"]
    assert columnar_decode(encoded) == rows


def test_columnar_lossless_with_missing_keys_and_none() -> None:
    rows = [
        {"path": "a", "line": 1},
        {"path": "a", "line": 2, "extra": None},  # explicit None value
        {"path": "b"},  # missing 'line' entirely
    ]
    encoded = columnar_encode(rows)
    decoded = columnar_decode(encoded)
    assert decoded == rows
    # An absent key stays absent; a stored None stays None.
    assert "line" not in decoded[2]
    assert decoded[1]["extra"] is None


def test_columnar_self_describing_header() -> None:
    rows = [{"path": "a", "sym": "X"}, {"path": "a", "sym": "Y"}]
    encoded = columnar_encode(rows)
    # Header fields a consumer/model can interpret without out-of-band schema.
    assert set(encoded) >= {"format", "n", "keys", "legend", "columns", "present"}
    assert encoded["n"] == 2
    assert encoded["keys"] == ["path", "sym"]


def test_columnar_beats_json_on_redundant_rows() -> None:
    rows = [{"path": "src/services/orders.py", "fqn": "orders.OrderService"} for _ in range(40)]
    raw_json = json.dumps(rows, separators=(",", ":"))
    columnar_json = columnar_encode_json(rows)
    # Cross-row dictionary compression makes the columnar form clear the gate.
    assert len(columnar_json) < len(raw_json)
    assert gate_compact(raw_json, columnar_json).used_compact is True


def test_columnar_decode_rejects_unknown_format() -> None:
    import pytest

    with pytest.raises(ValueError):
        columnar_decode({"format": "some-other-format", "n": 0})
