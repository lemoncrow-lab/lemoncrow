"""Trusted-base authority is independent of any one Review policy parser."""

from lemoncrow.pro.capabilities.review.trusted_base import resolve_trusted_base_document


def test_trusted_base_document_distinguishes_present_absent_and_provisional() -> None:
    path = ".lemoncrow/review.yaml"

    present = resolve_trusted_base_document(path, "version: 1\n")
    assert present.state == "present"
    assert present.base_known is True
    assert present.provisional is False
    assert present.content == "version: 1\n"
    assert present.source == "base revision .lemoncrow/review.yaml"

    absent = resolve_trusted_base_document(path, "")
    assert absent.state == "absent"
    assert absent.base_known is True
    assert absent.content == ""
    assert absent.source == ""

    provisional = resolve_trusted_base_document(path, None)
    assert provisional.state == "provisional"
    assert provisional.base_known is False
    assert provisional.provisional is True
    assert provisional.content == ""
    assert provisional.source == ""
