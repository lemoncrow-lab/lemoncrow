from lemoncrow_client.kit.search import render_grep_text


def test_ranked_grep_is_one_pointer_per_file() -> None:
    result = {
        "mode": "ranked_file_map",
        "matches": [
            {"file": "src/a.py", "ranges": ["2-4", "9-9"]},
            {"file": "src/b.py", "ranges": ["1-1"]},
        ],
        "truncated": True,
    }
    assert render_grep_text(result) == "→ src/a.py:L2-L4,L9\n→ src/b.py:L1\n+more"
