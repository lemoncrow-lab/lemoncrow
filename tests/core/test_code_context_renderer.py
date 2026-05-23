from __future__ import annotations

from atelier.core.capabilities.code_context.renderer import render_code_payload


def test_render_search_compact_is_location_only_and_omits_source_fields() -> None:
    rendered = render_code_payload(
        "search",
        {
            "items": [
                {
                    "symbol_name": "run_command",
                    "qualified_name": "pkg.run_command",
                    "kind": "function",
                    "file_path": "src/pkg/worker.py",
                    "start_line": 12,
                    "snippet": "def run_command(cmd: str) -> int:",
                    "source": "def run_command(cmd: str) -> int:\n    return len(cmd)\n",
                },
                {
                    "symbol_name": "Worker",
                    "qualified_name": "pkg.Worker",
                    "kind": "class",
                    "file_path": "src/pkg/worker.py",
                    "start_line": 0,
                    "source": "class Worker:\n    pass\n",
                },
            ]
        },
    )

    assert rendered is not None
    assert rendered.startswith("### search")
    assert "- src/pkg/worker.py:12 — pkg.run_command [function]" in rendered
    assert "- src/pkg/worker.py — pkg.Worker [class]" in rendered
    assert "def run_command(cmd: str)" not in rendered
    assert "class Worker:\n" not in rendered


def test_render_symbol_compact_summary_excludes_source_body() -> None:
    rendered = render_code_payload(
        "symbol",
        {
            "symbol_id": "sym-1",
            "qualified_name": "orders.OrderService.calculate_total",
            "symbol_name": "calculate_total",
            "kind": "method",
            "signature": "def calculate_total(self, items: list[int]) -> int",
            "file_path": "src/orders.py",
            "start_line": 20,
            "end_line": 32,
            "source": "def calculate_total(self, items):\n    total = sum(items)\n    return total\n",
        },
    )

    assert rendered is not None
    assert rendered.startswith("### symbol")
    assert "- id: sym-1" in rendered
    assert "- location: src/orders.py:20-32" in rendered
    assert "- signature: def calculate_total(self, items: list[int]) -> int" in rendered
    assert "total = sum(items)" not in rendered


def test_render_outline_compact_summary_is_sorted_and_includes_signatures() -> None:
    rendered = render_code_payload(
        "outline",
        {
            "files": {
                "src/orders.py": [
                    {
                        "name": "run",
                        "qualified_name": "Worker.run",
                        "kind": "method",
                        "signature": "def run(self) -> None",
                        "line_start": 25,
                        "line_end": 30,
                        "source": "def run(self): ...",
                    },
                    {
                        "name": "Worker",
                        "qualified_name": "Worker",
                        "kind": "class",
                        "signature": "class Worker",
                        "line_start": 10,
                        "line_end": 40,
                    },
                ]
            }
        },
    )

    assert rendered is not None
    assert rendered.startswith("### outline")
    worker_idx = rendered.index("10-40: Worker [class] — class Worker")
    run_idx = rendered.index("25-30: Worker.run [method] — def run(self) -> None")
    assert worker_idx < run_idx
    assert "def run(self): ..." not in rendered


def test_render_context_includes_deterministic_sections_and_caps() -> None:
    rendered = render_code_payload(
        "context",
        {
            "task": "issue access token",
            "budget_tokens": 4000,
            "token_count": 900,
            "entry_points": [
                {"qualified_name": "Auth.issue_access_token", "file_path": "src/auth.py", "start_line": 10, "kind": "function"},
                {"qualified_name": "Auth.issue_access_log", "file_path": "src/auth.py", "start_line": 20, "kind": "function"},
                {"qualified_name": "Auth.issue_refresh_token", "file_path": "src/auth.py", "start_line": 30, "kind": "function"},
                {"qualified_name": "Auth.issue_magic_link", "file_path": "src/auth.py", "start_line": 40, "kind": "function"},
                {"qualified_name": "Auth.issue_api_key", "file_path": "src/auth.py", "start_line": 50, "kind": "function"},
                {"qualified_name": "Auth.import_helper", "file_path": "src/auth.py", "start_line": 3, "kind": "import"},
            ],
            "related_symbols": [
                {"qualified_name": "Session.revoke_access_token", "file_path": "src/session.py", "start_line": 11, "kind": "function"}
            ],
            "code_blocks": [
                {
                    "qualified_name": "Auth.issue_access_token",
                    "file_path": "src/auth.py",
                    "start_line": 10,
                    "end_line": 14,
                    "language": "python",
                    "source": "def issue_access_token() -> str:\n    return 'x'",
                }
            ],
        },
    )

    assert rendered is not None
    assert "#### entry_points" in rendered
    assert "#### related_symbols" in rendered
    assert "#### code_blocks" in rendered
    assert "Auth.import_helper" not in rendered
    assert rendered.count("src/auth.py:") == 4
    assert "Session.revoke_access_token" in rendered
    assert "```python" in rendered


def test_render_index_and_cache_status_compact_summaries() -> None:
    index_rendered = render_code_payload(
        "index",
        {
            "repo_id": "repo-x",
            "index_version": 7,
            "files_indexed": 560,
            "symbols_indexed": 6200,
            "imports_indexed": 1400,
            "repo_root": "/workspace/hidden",
            "db_path": "/workspace/hidden.sqlite",
        },
    )
    cache_rendered = render_code_payload(
        "cache_status",
        {
            "repo_id": "repo-x",
            "index_version": 7,
            "entry_count": 3,
            "entries_by_tool": {"code.search": 2, "code.symbol": 1},
            "total_bytes": 1024,
            "max_bytes": 2048,
            "scope": {"cache_tool": "all"},
            "last_hit_at": "2026-01-01T00:00:00Z",
        },
    )

    assert index_rendered is not None
    assert index_rendered.startswith("### index")
    assert "counts: files=560, symbols=6200, imports=1400" in index_rendered
    assert "/workspace/hidden" not in index_rendered
    assert cache_rendered is not None
    assert cache_rendered.startswith("### cache_status")
    assert "- tools: code.search=2, code.symbol=1" in cache_rendered
    assert "last_hit_at" not in cache_rendered
