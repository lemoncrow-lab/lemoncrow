from __future__ import annotations

from lemoncrow.core.capabilities.source_projection import (
    build_compact_projection,
    resolve_projected_range,
    suggest_exact_reread_range,
)


def test_compact_projection_mapping_resolves_exact_span() -> None:
    source = 'package   main\n\nfunc   main()   {\n    println("hi")\n}\n'

    projection = build_compact_projection(source, "go", path="sample.go", include_mapping=True)

    assert projection.mapping is not None
    projected_start = projection.content.index("println")
    projected_end = projected_start + len("println")
    source_range = resolve_projected_range(
        projection.mapping,
        projected_start=projected_start,
        projected_end=projected_end,
    )

    assert source_range is not None
    assert source[source_range.start_offset : source_range.end_offset] == "println"


def test_compact_projection_mapping_fails_closed_for_collapsed_whitespace_span() -> None:
    source = "package   main\n"

    projection = build_compact_projection(source, "go", path="sample.go", include_mapping=True)

    assert projection.mapping is not None
    projected_start = projection.content.index(" ") + 1
    projected_end = projected_start

    assert (
        resolve_projected_range(
            projection.mapping,
            projected_start=projected_start,
            projected_end=projected_end,
        )
        is None
    )
    assert (
        suggest_exact_reread_range(
            projection.mapping,
            projected_start=projected_start,
            projected_end=projected_end,
        )
        == "L1-L1"
    )


def test_compact_projection_mapping_resolves_exact_span_for_conservative_language() -> None:
    source = "def run():\n    value = 1\n    return value\n"

    projection = build_compact_projection(source, "python", path="sample.py", include_mapping=True)

    assert projection.mapping is not None
    projected_start = projection.content.index("value = 1")
    projected_end = projected_start + len("value = 1")
    source_range = resolve_projected_range(
        projection.mapping,
        projected_start=projected_start,
        projected_end=projected_end,
    )

    assert source_range is not None
    assert source[source_range.start_offset : source_range.end_offset] == "value = 1"
