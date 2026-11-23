"""A Review that nobody named is titled by what changed, with no revision number in it."""

from __future__ import annotations

import pytest

from lemoncrow.pro.capabilities.review.api import _overview_title
from lemoncrow.pro.capabilities.review.titles import is_generic_title, packet_title


def _packet(*symbols: tuple[str, str], files: int = 2) -> dict[str, object]:
    return {
        "title": "working tree",
        "files": [{"path": f"app/f{n}.py", "status": "modified"} for n in range(files)],
        "symbols": [{"symbol_name": name, "change": change} for name, change in symbols],
    }


def test_the_shop_example_is_titled_by_its_three_functions() -> None:
    shop = _packet(("order_total", "modified"), ("apply_discount", "modified"), ("can_view_order", "modified"))
    assert packet_title(shop) == "Change order_total, apply_discount and can_view_order"


@pytest.mark.parametrize(
    ("symbols", "title"),
    [
        ([("a", "modified"), ("b", "modified")], "Change a and b"),
        ([("a", "added"), ("a", "added")], "Add a"),
        ([("a", "deleted")], "Remove a"),
        ([("a", "added"), ("b", "deleted")], "Change a and b"),
        ([(f"f{n}", "modified") for n in range(4)], "app: 2 files"),
        ([], "app: 2 files"),
    ],
)
def test_symbols_name_the_change_up_to_three_else_the_area(symbols: list[tuple[str, str]], title: str) -> None:
    assert packet_title(_packet(*symbols)) == title


def test_no_title_carries_a_revision_number() -> None:
    for packet in (None, {}, {"title": "working tree", "files": []}, _packet(("a", "modified"))):
        assert "Snapshot" not in packet_title(packet) and "Revision" not in packet_title(packet)


def test_the_reader_header_gets_the_same_name_as_the_inbox() -> None:
    shop = _packet(("order_total", "modified"))
    assert _overview_title("working tree", "", shop) == "Change order_total"
    assert _overview_title("working tree", "Fix totals", shop) == "Fix totals"
    assert _overview_title("Their own name", "", shop) == "Their own name"
    assert _overview_title("working tree", "", None) == "working tree"
    assert is_generic_title("Staged changes") and not is_generic_title("Fix totals")
