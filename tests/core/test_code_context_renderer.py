from __future__ import annotations

from lemoncrow.pro.capabilities.code_context.renderer import render_code_payload


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
    # Results are grouped by file: the path is emitted once as a header, then
    # one indented line per hit (line + symbol, or symbol-only when line == 0).
    assert "- src/pkg/worker.py" in rendered
    assert "  - 12 — pkg.run_command [function]" in rendered
    assert "  - pkg.Worker [class]" in rendered
    # The full path appears exactly once despite two hits in the same file.
    assert rendered.count("src/pkg/worker.py") == 1
    assert "def run_command(cmd: str)" not in rendered
    assert "class Worker:\n" not in rendered


def test_render_relations_omit_redundant_target_echo() -> None:
    rendered = render_code_payload(
        "callers",
        {
            "target": {
                "qualified_name": "orders.export_audit_bundle",
                "file_path": "src/orders.py",
                "start_line": 18,
            },
            "related": [
                {
                    "qualified_name": "api.create_app",
                    "file_path": "src/api.py",
                    "start_line": 42,
                }
            ],
        },
    )

    assert rendered is not None
    assert "- target:" not in rendered
    assert "- src/api.py" in rendered
    assert "  - 42 — api.create_app" in rendered


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
    assert "- id: sym-1" in rendered
    assert "src/orders.py:L20-L32" in rendered
    assert "- signature: def calculate_total(self, items: list[int]) -> int" in rendered
    assert "total = sum(items)" not in rendered


def test_render_context_includes_deterministic_sections_and_caps() -> None:
    rendered = render_code_payload(
        "context",
        {
            "task": "issue access token",
            "budget_tokens": 4000,
            "token_count": 900,
            "entry_points": [
                {
                    "qualified_name": "Auth.issue_access_token",
                    "file_path": "src/auth.py",
                    "start_line": 10,
                    "kind": "function",
                },
                {
                    "qualified_name": "Auth.issue_access_log",
                    "file_path": "src/auth.py",
                    "start_line": 20,
                    "kind": "function",
                },
                {
                    "qualified_name": "Auth.issue_refresh_token",
                    "file_path": "src/auth.py",
                    "start_line": 30,
                    "kind": "function",
                },
                {
                    "qualified_name": "Auth.issue_magic_link",
                    "file_path": "src/auth.py",
                    "start_line": 40,
                    "kind": "function",
                },
                {
                    "qualified_name": "Auth.issue_api_key",
                    "file_path": "src/auth.py",
                    "start_line": 50,
                    "kind": "function",
                },
                {"qualified_name": "Auth.import_helper", "file_path": "src/auth.py", "start_line": 3, "kind": "import"},
            ],
            "related_symbols": [
                {
                    "qualified_name": "Session.revoke_access_token",
                    "file_path": "src/session.py",
                    "start_line": 11,
                    "kind": "function",
                }
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
    assert "counts: files=560, symbols=6200, imports=1400" in index_rendered
    assert "/workspace/hidden" not in index_rendered
    assert cache_rendered is not None
    assert "- tools: code.search=2, code.symbol=1" in cache_rendered
    assert "last_hit_at" not in cache_rendered


def test_render_blame_compact_summary_and_hunks() -> None:
    rendered = render_code_payload(
        "blame",
        {
            "symbol_name": "calculate_total",
            "qualified_name": "OrderService.calculate_total",
            "file_path": "src/orders.py",
            "line_start": 12,
            "line_end": 20,
            "freshness": "fresh",
            "last_author": "dev@example.com",
            "last_commit_sha": "abcdef1234567890",
            "last_commit_summary": "add total",
            "age_days": 5,
            "local_edits": False,
            "distinct_authors": 2,
            "hunks": [
                {
                    "start_line": 12,
                    "end_line": 16,
                    "commit_sha": "abcdef1234567890",
                    "author_email": "dev@example.com",
                    "commit_time": 1700000000,
                }
            ],
            "churn": {"commit_count": 3, "score": 0.42, "window_days": 180},
            "provenance": "blame",
        },
    )

    assert rendered is not None
    assert "- target: OrderService.calculate_total (src/orders.py:12-20)" in rendered
    assert "- last: abcdef1234 dev@example.com — add total" in rendered
    assert "- hunks (1):" in rendered
    assert "  - 12-16 abcdef1234 dev@example.com" in rendered
    assert "commits=3" in rendered
    # full 40-char sha and per-hunk key noise are dropped
    assert "abcdef1234567890" not in rendered
    assert "commit_time" not in rendered


def test_render_outline_groups_symbols_and_drops_signature() -> None:
    rendered = render_code_payload(
        "outline",
        {
            "repo_id": "repo-x",
            "symbol_count": 2,
            "files": {
                "src/orders.py": [
                    {
                        "name": "OrderService",
                        "kind": "class",
                        "signature": "class OrderService:",
                        "line_start": 1,
                        "line_end": 10,
                    },
                    {
                        "name": "calculate_total",
                        "qualified_name": "OrderService.calculate_total",
                        "kind": "method",
                        "signature": "def calculate_total(self, items): ...",
                        "line_start": 2,
                        "line_end": 5,
                    },
                ]
            },
        },
    )

    assert rendered is not None
    assert "- outline: 2 symbols" in rendered
    assert "- src/orders.py" in rendered
    assert "  - 1-10: OrderService [class]" in rendered
    assert "  - 2-5: OrderService.calculate_total [method]" in rendered
    # signatures are dropped from the outline projection
    assert "def calculate_total" not in rendered
