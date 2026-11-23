"""A readable PDF copy of one signed record (SPEC 4.18 "Download PDF").

The PDF is a plain page of the record's Details rows, built with ``pypdf`` objects
(no new dependency): Helvetica text in WinAnsi, characters outside Latin-1 shown as
``?``. It is a *copy to read*, not the evidence: the evidence is the signed JSON
file, and the PDF says so and carries the payload digest so the two can be matched.
An unsigned record's PDF says UNSIGNED at the top.
"""

from __future__ import annotations

import io
import textwrap
from collections.abc import Mapping, Sequence
from typing import Any, Final

from lemoncrow.pro.capabilities.review.record_details import INTRO, LEGAL_SENTENCE, detail_rows
from lemoncrow.pro.capabilities.review.review_record import UNSIGNED_NOTICE

_PAGE_WIDTH, _PAGE_HEIGHT = 612, 792
_MARGIN = 54
_BODY_SIZE, _TITLE_SIZE, _LEADING = 10, 18, 14
_WRAP = 92
_TOP, _BOTTOM = _PAGE_HEIGHT - _MARGIN, _MARGIN
_COPY_NOTE: Final[str] = (
    "This page is a readable copy. The signed JSON file is the evidence; check it with: lc review verify <record-link>"
)


def _latin1(text: str) -> str:
    return text.encode("cp1252", errors="replace").decode("cp1252")


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _layout(
    title: str, signed: bool, rows: Sequence[Mapping[str, str]], digest: str
) -> list[list[tuple[int, str, str]]]:
    """Pages of ``(size, font, text)`` lines; the font is ``F1`` (regular) or ``F2`` (bold)."""

    lines: list[tuple[int, str, str]] = [(_TITLE_SIZE, "F2", title)]
    if not signed:
        lines += [(_BODY_SIZE, "F2", chunk) for chunk in textwrap.wrap(UNSIGNED_NOTICE, _WRAP)]
    lines += [(_BODY_SIZE, "F1", chunk) for chunk in textwrap.wrap(INTRO, _WRAP)] + [(_BODY_SIZE, "F1", "")]
    for row in rows:
        wrapped = textwrap.wrap(row["value"], _WRAP - 4) or [""]
        lines.append((_BODY_SIZE, "F2", row["label"]))
        lines += [(_BODY_SIZE, "F1", f"    {chunk}") for chunk in wrapped]
    lines.append((_BODY_SIZE, "F1", ""))
    lines += [(_BODY_SIZE, "F1", chunk) for chunk in textwrap.wrap(f"Payload digest: {digest}", _WRAP)]
    lines += [(_BODY_SIZE, "F1", chunk) for chunk in textwrap.wrap(_COPY_NOTE, _WRAP)]
    lines += [(_BODY_SIZE, "F1", chunk) for chunk in textwrap.wrap(LEGAL_SENTENCE, _WRAP)]
    pages: list[list[tuple[int, str, str]]] = [[]]
    y = _TOP
    for size, font, text in lines:
        leading = _LEADING if size == _BODY_SIZE else size + 8
        if y - leading < _BOTTOM:
            pages.append([])
            y = _TOP
        pages[-1].append((size, font, text))
        y -= leading
    return pages


def render_record_pdf(
    statement: Mapping[str, Any], *, signed: bool, payload_digest: str, viewer_id: str = "", own_key_id: str = ""
) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    title = "Signed record" if signed else "Record (not signed)"
    pages = _layout(title, signed, detail_rows(statement, viewer_id=viewer_id, own_key_id=own_key_id), payload_digest)
    writer = PdfWriter()
    # lc-debt: pypdf has no public call that adds an indirect object, so this uses `_add_object`;
    # replace it when pypdf publishes one (pyproject pins pypdf>=6.19).
    fonts = DictionaryObject()
    for name, base in (("F1", "/Helvetica"), ("F2", "/Helvetica-Bold")):
        fonts[NameObject(f"/{name}")] = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject(base),
                    NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                }
            )
        )
    for page_lines in pages:
        page = writer.add_blank_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
        commands = ["BT"]
        y = _TOP
        for size, font, text in page_lines:
            leading = _LEADING if size == _BODY_SIZE else size + 8
            y -= leading
            commands.append(f"/{font} {size} Tf 1 0 0 1 {_MARGIN} {y} Tm ({_escape(_latin1(text))}) Tj")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("cp1252", errors="replace"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
        page[NameObject("/MediaBox")] = ArrayObject(
            [NumberObject(0), NumberObject(0), NumberObject(_PAGE_WIDTH), NumberObject(_PAGE_HEIGHT)]
        )
    writer.add_metadata({"/Title": title, "/Producer": "LemonCrow"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


__all__ = ["render_record_pdf"]
