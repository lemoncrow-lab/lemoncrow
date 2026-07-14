from __future__ import annotations

from lemoncrow.pro.capabilities.source_projection.compact import build_compact_projection


def test_safe_language_collapses_interior_whitespace_and_saves_tokens() -> None:
    source = (
        "package   main\n\n"
        'import   "fmt"\n\n'
        "func   main()   {\n"
        '    message := "keep   quoted   spacing"\n'
        "    fmt.Println(   message   )\n"
        "}\n"
    )

    projection = build_compact_projection(source, "go")
    compact = projection.content

    assert "package main" in compact
    assert "func main() {" in compact
    assert "fmt.Println( message )" in compact
    assert '"keep   quoted   spacing"' in compact
    assert projection.projected_tokens < projection.original_tokens


def test_whitespace_significant_languages_keep_conservative_path() -> None:
    source = "def run():\n    value    =    1\n    return value\n"

    compact = build_compact_projection(source, "python").content

    assert "value    =    1" in compact


def test_unknown_languages_keep_conservative_path() -> None:
    source = "value    =    still    spaced\n"

    compact = build_compact_projection(source, "text").content

    assert compact == source


def test_compact_projection_is_pure_and_preserves_json_string_content() -> None:
    source = '{  "message"  :  "keep   inner   spacing",  "count"  :  1  }\n'

    first = build_compact_projection(source, "json")
    second = build_compact_projection(source, "json")

    assert first == second
    assert '"keep   inner   spacing"' in first.content
