import random

import pytest
from lemoncrow_client.search_markdown import render_code_search_markdown

from benchmarks.codebench.retrieval_wire import code_search_markdown_header_path, code_search_markdown_paths


@pytest.mark.parametrize("query", ["issue_token", "token handling"])
def test_renderer_round_trip_keeps_navigation_tail(query):
    source = b"def issue_token():\n    return 1\n"
    for count in (1, 2, 3, 10, 11, 32):
        hits = [
            {"path": f"src/file_{i}.py", "detail": {"definitions": [{"name": "issue_token", "line": 1}]}}
            for i in range(count)
        ]
        rendered = render_code_search_markdown(query, {"hits": hits}, load_source=lambda hit: source)
        assert code_search_markdown_paths(rendered) == [hit["path"] for hit in hits]


def test_grouped_navigation_without_inline_source_and_duplicate_paths():
    paths = ["a.py", "b.py", "pkg/c.py", "pkg/d.py"]
    rendered = render_code_search_markdown(
        "unknown", {"hits": [{"path": p} for p in paths]}, load_source=lambda hit: None
    )
    assert code_search_markdown_paths(rendered + "\n→ a.py:L1 · duplicate") == paths


def test_legacy_candidate_groups_and_source_paths():
    text = "## src/a.py:L1-L2 · f\n1 return 'unrelated.py'\n  ## fake.py\ncandidate_files: src/{b.py,c.py}, d.py"
    assert code_search_markdown_paths(text) == ["src/a.py", "src/b.py", "src/c.py", "d.py"]


def test_randomized_legacy_headers_preserve_previous_parser_results():
    rng = random.Random(41)
    for _ in range(100):
        paths = [f"pkg{rng.randrange(20)}/f{i}.py" for i in range(rng.randrange(1, 30))]
        headers = [f"{path}:L1-L{rng.randrange(2, 100)} · symbol" for path in paths]
        assert code_search_markdown_paths("\n".join("## " + h for h in headers)) == [
            code_search_markdown_header_path(h) for h in headers
        ]


def test_code_search_markdown_header_path_strips_range_and_symbol() -> None:
    assert (
        code_search_markdown_header_path("django/contrib/admindocs/utils.py:L27-L39 · trim_docstring")
        == "django/contrib/admindocs/utils.py"
    )


def test_code_search_markdown_header_path_strips_single_line_range() -> None:
    assert code_search_markdown_header_path("src/flask/app.py:L105 · Flask") == "src/flask/app.py"


def test_code_search_markdown_header_path_preserves_plain_path() -> None:
    assert code_search_markdown_header_path("pkg/module.py") == "pkg/module.py"
