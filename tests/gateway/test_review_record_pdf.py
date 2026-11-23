"""P6-1: the Details rows and the PDF copy read from the payload alone, and say only what it holds."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from lemoncrow.pro.capabilities.review.record_details import LEGAL_SENTENCE, detail_rows
from lemoncrow.pro.capabilities.review.record_pdf import render_record_pdf
from tests.gateway.test_review_record import (  # noqa: F401
    RecordWorkspace,
    _ai_hint,
    _payload,
    record_workspace,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "review_record"


def _fixture_statement(name: str) -> dict[str, Any]:
    document = json.loads((FIXTURES / name).read_text())
    statement: dict[str, Any] = json.loads(base64.b64decode(document["envelope"]["payload"]))
    return statement


def _text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() for page in reader.pages)


def _rows(statement: dict[str, Any]) -> dict[str, str]:
    return {row["label"]: row["value"] for row in detail_rows(statement)}


def test_a_v1_record_lists_only_what_it_holds() -> None:
    rows = _rows(_fixture_statement("v1_signed_before_p3_2.json"))
    assert rows["Your verdict"] == "Approve · by local"
    # "you" only for the viewer's own id and own key.
    own = {
        row["label"]: row["value"]
        for row in detail_rows(_fixture_statement("v1_signed_before_p3_2.json"), viewer_id="local")
    }
    assert own["Your verdict"] == "Approve · by you"
    # The payload counts Snapshots from zero; the row says what a person reads.
    assert rows["Version"] == "Revision 1"
    assert rows["Signed at"].endswith(" UTC")
    assert rows["Saved as"] == "a signed file (in-toto)"
    # A v1 record signed no parts, hints, change or coverage: no row pretends it did.
    assert not {"Decisions", "Parts", "Not checked yet", "Coverage", "AI in this record"} & set(rows)


def test_parts_are_named_as_decisions_answered_and_a_zero_count_is_left_out() -> None:
    def rows_for(parts: dict[str, Any]) -> dict[str, str]:
        return {row["label"]: row["value"] for row in detail_rows({"predicate": {"parts": parts}})}

    decisions = [
        {"label": "app/auth.py::can_view_order", "state": "needs_changes"},
        {"label": "app/orders.py::apply_discount", "state": "reviewed"},
        {"label": "app/orders.py::order_total", "state": "reviewed"},
    ]
    rows = rows_for({"counts": {"quick_check": 3, "accepted_by_rule": 0}, "decisions": decisions, "later": 0})
    assert rows["Decisions"].startswith("app/auth.py::can_view_order · needs a change; ")
    assert rows["Parts"] == "3 decisions answered \N{MIDDLE DOT} 3 quick checks"
    assert "Reviewed one by one" not in rows
    one = rows_for({"counts": {"quick_check": 1, "accepted_by_rule": 2}, "decisions": decisions[:1], "later": 1})
    assert one["Parts"] == (
        "1 decision answered \N{MIDDLE DOT} 1 quick check \N{MIDDLE DOT} 2 accepted by rule"
        " \N{MIDDLE DOT} 1 left for later"
    )
    # Nothing to count and no decision: neither row claims a zero or a "none".
    empty = rows_for({"counts": {"quick_check": 0}, "decisions": [], "later": 0})
    assert "Parts" not in empty and "Decisions" not in empty


def test_a_p3_2_era_record_shows_its_coverage_and_nothing_added_later() -> None:
    rows = _rows(_fixture_statement("v2_p3_2_era.json"))
    assert rows["Version"] == "Revision 2 · ccccccc"
    assert rows["Coverage"].startswith("src/a.py: seen lines 3\u20135")
    assert "not proof of reading" in rows["Coverage"]
    assert "Parts" not in rows and "AI in this record" not in rows


def test_the_pdf_of_a_signed_human_record_carries_the_rows_the_digest_and_the_legal_sentence(
    record_workspace: RecordWorkspace,  # noqa: F811
) -> None:
    ws = record_workspace
    _ai_hint(ws, 10)
    ws.opt_in()
    ws.approve(outcome="changes_requested")
    record = ws.export().json()["record"]
    served = ws.workspace.get(f"/api/reviews/{ws.review_id}/records/{record['record_id']}/pdf")
    assert served.status_code == 200 and served.headers["content-type"] == "application/pdf"
    assert served.content.startswith(b"%PDF-") and "attachment" in served.headers["Content-Disposition"]
    text = _text(served.content)
    assert "Signed record" in text and "UNSIGNED" not in text
    assert "Your verdict" in text and "Request changes" in text
    assert "AI in this record" in text and "never decisions" in text
    assert record["payload_digest"] in text.replace("\n", "")
    assert LEGAL_SENTENCE in " ".join(text.split())


def test_the_pdf_of_an_unsigned_record_says_unsigned_before_anything_else(
    record_workspace: RecordWorkspace,  # noqa: F811
) -> None:
    ws = record_workspace
    ws.approve()
    record = ws.export("unsigned").json()["record"]
    served = ws.workspace.get(f"/api/reviews/{ws.review_id}/records/{record['record_id']}/pdf")
    assert served.status_code == 200 and "unsigned.pdf" in served.headers["Content-Disposition"]
    text = " ".join(_text(served.content).split())
    assert text.startswith("Record (not signed) UNSIGNED:")
    assert "Signed by nobody" in text


def test_a_record_naming_a_path_outside_latin_1_still_makes_a_pdf() -> None:
    statement = _fixture_statement("v2_p3_2_era.json")
    statement["predicate"]["seen_coverage"]["seen"][0]["path"] = "src/日本語/(a)\\b.py"
    pdf = render_record_pdf(statement, signed=True, payload_digest="sha256:" + "0" * 64)
    assert PdfReader(io.BytesIO(pdf)).pages
    assert "(a)\\b.py" in _text(pdf).replace("\n", "")


def test_a_long_list_is_capped_and_says_how_many_were_left_out() -> None:
    statement = _fixture_statement("v2_p3_2_era.json")
    statement["predicate"]["parts"] = {
        "counts": {"decision": 80, "quick_check": 0, "accepted_by_rule": 0, "rules": {}},
        "decisions": [
            {
                "unit_key": f"u{i}",
                "label": f"src/module_{i}.py::function_{i}",
                "path": f"src/module_{i}.py",
                "state": "reviewed",
            }
            for i in range(80)
        ],
        "quick_check_changes": [],
        "not_confirmed": [],
        "later": 0,
    }
    pdf = render_record_pdf(statement, signed=True, payload_digest="sha256:" + "1" * 64)
    text = " ".join(_text(pdf).split())
    # The Details list names the first eight and counts the rest: nothing is silently cut.
    assert "8 more" not in text and "72 more" in text
    assert LEGAL_SENTENCE in text


def test_rows_that_do_not_fit_one_page_continue_on_the_next_and_none_is_lost() -> None:
    from lemoncrow.pro.capabilities.review.record_pdf import _layout

    rows = [{"label": f"Row {i}", "value": f"value {i} " + "word " * 30} for i in range(40)]
    pages = _layout("Signed record", True, rows, "sha256:" + "2" * 64)
    assert len(pages) > 1
    flat = " ".join(text for page in pages for _size, _font, text in page)
    assert all(f"Row {i}" in flat for i in range(40)) and LEGAL_SENTENCE.split(".")[0] in flat
