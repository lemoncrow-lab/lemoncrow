"""Regression benchmark for agent-facing tool output budgets.

This is intentionally deterministic and network-free. Each case feeds a
representative *raw structured payload* through the same production renderer
used by MCP, then compares raw-vs-rendered chars/tokens and asserts that the
facts an agent needs to act are still present.

Run with:
    uv run pytest tests/benchmarks/test_tool_output_budget_regression.py -v -s
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest
import tiktoken
from lemoncrow_client.dispatcher import ToolOutcome
from lemoncrow_client.kit.search import render_grep_text
from lemoncrow_client.kit.sql import render_sql_text
from lemoncrow_client.search_markdown import render_code_search_markdown

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.pro.capabilities.code_context.renderer import render_code_payload, render_graph_payload
from lemoncrow.pro.capabilities.tool_supervision.bash_exec import _compact_result

_ENCODING = tiktoken.get_encoding("cl100k_base")


@dataclass(frozen=True)
class OutputCase:
    name: str
    raw: dict[str, object]
    render: Callable[[], str | None]
    required: tuple[str, ...]
    forbidden: tuple[str, ...] = ()


def _raw_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def _code_search_case() -> OutputCase:
    source = b"class AuthService:\n    def issue_token(self, user):\n        return sign(user)\n"
    start = source.index(b"def issue_token")
    raw: dict[str, object] = {
        "kind": "code_search",
        "hits": [
            {
                "path": "src/auth.py",
                "score": 103.42,
                "language": "python",
                "line_count": 220,
                "size": 9012,
                "detail": {
                    "definitions": [
                        {
                            "name": "issue_token",
                            "kind": "method",
                            "line": 2,
                            "start": start,
                            "end": len(source),
                        }
                    ],
                    "content_indexed": True,
                },
            },
            {
                "path": "src/oauth.py",
                "score": 94.1,
                "language": "python",
                "line_count": 180,
                "size": 7021,
                "detail": {
                    "definitions": [{"name": "issue_token", "kind": "method", "line": 88, "start": 0, "end": 1}],
                    "related_symbol": "issue_token",
                },
            },
            {"path": "tests/test_auth.py", "score": 81.2, "detail": {"content_indexed": True}},
        ],
        "view_revision": 91,
        "searched_paths": 6120,
        "degraded": False,
        "degraded_reason": "",
        "truncated": True,
        "remaining_hits": 7,
        "_hydration": {"src/auth.py": "a" * 64, "src/oauth.py": "b" * 64},
    }

    def render() -> str:
        return render_code_search_markdown(
            "issue_token",
            raw,
            load_source=lambda hit: source if hit.get("path") == "src/auth.py" else None,
        )

    return OutputCase(
        "code_search",
        raw,
        render,
        ("= exact", "src/auth.py:L2", "issue_token", "return sign(user)", "src/oauth.py:L88", "+7 more"),
        ("searched_paths", "103.42", "_hydration"),
    )


def _cases() -> list[OutputCase]:
    grep_raw: dict[str, object] = {
        "mode": "ranked_file_map",
        "matches": [
            {"file": "src/auth.py", "ranges": ["12-18", "44-44", "88-93"], "score": 18.2},
            {"file": "src/oauth.py", "ranges": ["31-35", "71-72"], "score": 14.1},
            {"file": "tests/test_auth.py", "ranges": ["110-126"], "score": 9.8},
        ],
        "truncated": True,
        "searched_files": 718,
        "candidate_files_more": 9,
    }

    relations_raw: dict[str, object] = {
        "related": [
            {"qualified_name": "api.create_session", "file_path": "src/api.py", "start_line": 42, "kind": "function"},
            {"qualified_name": "api.refresh_session", "file_path": "src/api.py", "start_line": 81, "kind": "function"},
            {
                "qualified_name": "jobs.rotate_tokens",
                "file_path": "jobs/tokens.py",
                "start_line": 17,
                "kind": "function",
            },
        ],
        "truncated": True,
        "total_matches": 11,
        "query_symbol_id": "sym_very_long_internal_identifier",
        "view_revision": 91,
    }

    context_raw: dict[str, object] = {
        "context": (
            "### procedure\nUse canonical tenant identity before issuing tokens.\n"
            "<memory>Refresh tokens are short-lived and revocable.</memory>"
        ),
        "recalled_passages": [
            {"text": "Refresh tokens are short-lived and revocable.", "source_ref": "session#17", "score": 0.93}
        ],
        "tokens_breakdown": {"playbooks": 92, "bootstrap": 31, "memory": 23, "total": 146},
        "bootstrap": {
            "status": "warm",
            "repo_id": "repo-0123456789abcdef",
            "blocks": [
                {"label": "architecture", "kind": "bootstrap", "version": 4},
                {"label": "conventions", "kind": "bootstrap", "version": 3},
            ],
        },
        "run_ledger": {"session_id": "sess-123", "turn": 19, "events": 428},
    }

    graph_raw: dict[str, object] = {
        "kind": "pr_risk",
        "overall_score": 0.72,
        "overall_tier": "high",
        "file_count": 1,
        "files": [
            {
                "path": "src/auth.py",
                "score": 0.72,
                "tier": "high",
                "risk_level": "high",
                "factors": {
                    "blast_radius": {"impacted_files": 9, "affected_tests": ["tests/test_auth.py"], "factor": 0.8},
                    "churn": {"commit_count": 12, "factor": 0.6, "available": True},
                    "test_gap": {"missing_tests": False, "factor": 0.0},
                    "complexity": {"score": 17, "factor": 0.85},
                },
            }
        ],
        "weights": {"blast_radius": 0.35, "churn": 0.25, "test_gap": 0.25, "complexity": 0.15},
        "heuristic": True,
        "backend": "server_index",
        "view_revision": 91,
    }

    rescue_raw: dict[str, object] = {
        "rescue": "Stop retrying. Re-read the exact parser range, patch once, then rerun the focused parser test.",
        "matched_blocks": ["playbook/parser-recovery", "playbook/edit-safety"],
        "analysis": {
            "matched": True,
            "match_score": 0.83,
            "current_fingerprint": "assertionerror expected <n>",
            "incident": {
                "fingerprint": "assertionerror expected <n>",
                "count": 4,
                "trace_ids": ["trace-1", "trace-2", "trace-3", "trace-4"],
                "sample_errors": ["AssertionError: expected 3", "AssertionError: expected 4"],
                "common_commands": ["pytest tests/test_parser.py", "pytest -q"],
                "root_cause_hypothesis": "the parser range is stale after an insertion",
                "confidence": 0.9,
                "suggested_playbooks": ["playbook/parser-recovery"],
                "suggested_fixes": ["re-read the exact range", "rerun the focused parser test"],
            },
        },
    }

    sql_raw: dict[str, object] = {
        "isError": False,
        "dialect": "sqlite",
        "took_ms": 7,
        "results": [
            {
                "name": "items",
                "columns": ["id", "name", "note"],
                "rows": [[1, "Ada", "active"], [2, "Grace", "needs review"], [3, "Linus", None]],
                "row_count": 3,
                "truncated": False,
                "auto_limit_changed": True,
            }
        ],
    }

    memory_raw: dict[str, object] = {
        "op": "recall",
        "agent_id": "lemoncrow:code",
        "passages": [
            {
                "id": "fact-1",
                "fact": "Use tenant-scoped identity for access tokens.",
                "source_ref": "session#9",
                "score": 0.96,
                "created_at": "2026-09-20T12:00:00Z",
            },
            {
                "id": "fact-2",
                "text": "Refresh tokens are revocable.",
                "source_ref": "session#11",
                "score": 0.88,
                "created_at": "2026-09-20T13:00:00Z",
            },
        ],
        "query": "token identity",
        "trace_id": "trace-memory-123456",
    }

    verify_raw: dict[str, object] = {
        "rubric_id": "rubric_state_change_safety",
        "status": "pass",
        "outcomes": [
            {"name": "canonical_identifier_used", "status": "pass", "detail": ""},
            {"name": "read_after_write_completed", "status": "pass", "detail": ""},
            {"name": "observed_state_matches_intent", "status": "pass", "detail": ""},
        ],
        "started_at": "2026-09-20T12:00:00Z",
        "duration_ms": 17,
    }

    bash_lines = [f"2026-09-20T18:{i // 60:02d}:{i % 60:02d}Z INFO component=worker-{i:03d} ready" for i in range(120)]
    bash_lines[37] = "2026-09-20T18:00:37Z WARNING cache nearing capacity"
    bash_lines[91] = "2026-09-20T18:01:31Z ERROR upload failed for tenant-7"
    bash_raw_text = "\n".join(bash_lines)
    bash_raw: dict[str, object] = {
        "command": "vendorctl sync --all",
        "stdout": bash_raw_text,
        "stderr": "",
        "exit_code": 1,
        "duration_ms": 8210,
    }

    return [
        _code_search_case(),
        OutputCase(
            "grep",
            grep_raw,
            lambda: render_grep_text(grep_raw),
            ("src/auth.py:L12-L18,L44,L88-L93", "tests/test_auth.py:L110-L126", "+9 more"),
            ("searched_files", "score"),
        ),
        OutputCase(
            "relations",
            relations_raw,
            lambda: render_code_payload("callers", relations_raw),
            ("api.create_session", "api.refresh_session", "jobs.rotate_tokens", "+8 more"),
            ("query_symbol_id", "view_revision"),
        ),
        OutputCase(
            "context",
            context_raw,
            lambda: mcp_server._render_context_tool_md(context_raw),
            ("canonical tenant identity", "Refresh tokens are short-lived"),
            ("tokens_breakdown", "repo-0123456789abcdef", "run_ledger"),
        ),
        OutputCase(
            "graph",
            graph_raw,
            lambda: render_graph_payload(graph_raw),
            ("pr_risk high 0.72", "src/auth.py", "impact 9", "tests/test_auth.py", "complexity 17"),
            ("server_index", "view_revision"),
        ),
        OutputCase(
            "bash",
            bash_raw,
            lambda: _compact_result(
                command="vendorctl sync --all",
                raw_stdout=bash_raw_text,
                raw_stderr="",
                exit_code=1,
                duration_ms=8210,
                max_lines=200,
                max_chars=1000,
            ).stdout,
            ("WARNING cache nearing capacity", "ERROR upload failed for tenant-7", "omitted"),
        ),
        OutputCase(
            "rescue",
            rescue_raw,
            lambda: mcp_server._render_rescue_md(rescue_raw),
            ("Stop retrying", "root cause", "seen 4x", "re-read the exact range"),
            ("trace-1", "fingerprint"),
        ),
        OutputCase(
            "sql",
            sql_raw,
            lambda: render_sql_text(sql_raw),
            ("### sql query items · 3 rows · auto-limit", '1\t"Ada"', '3\t"Linus"\tnull'),
            ("took_ms", "dialect"),
        ),
        OutputCase(
            "memory",
            memory_raw,
            lambda: mcp_server._render_memory_md(memory_raw),
            ("Use tenant-scoped identity", "Refresh tokens are revocable"),
            ("trace-memory-123456", "created_at"),
        ),
        OutputCase(
            "verify",
            verify_raw,
            lambda: mcp_server._render_verify_md(verify_raw),
            ("status=pass", "pass canonical_identifier_used", "pass observed_state_matches_intent"),
            ("started_at", "duration_ms"),
        ),
    ]


CASES = _cases()

# Reviewed fixture budgets: current tokens plus 20% headroom (rounded up).
# Information assertions remain mandatory when updating these ceilings.
TOKEN_CEILINGS = {
    "code_search": 58,
    "grep": 50,
    "relations": 44,
    "context": 32,
    "graph": 89,
    "bash": 230,
    "rescue": 87,
    "sql": 52,
    "memory": 35,
    "verify": 41,
}


def _assert_token_budget(name: str, rendered: str) -> None:
    count = _tokens(rendered)
    assert count <= TOKEN_CEILINGS[name], f"{name}: output grew to {count} tokens; budget={TOKEN_CEILINGS[name]}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_budget_rejects_duplicate_output(case: OutputCase) -> None:
    rendered = case.render()
    assert rendered is not None
    with pytest.raises(AssertionError, match="output grew"):
        _assert_token_budget(case.name, rendered + "\n" + rendered)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_tool_output_preserves_actionable_information(case: OutputCase) -> None:
    rendered = case.render()
    assert rendered is not None and rendered.strip(), f"{case.name}: renderer returned no agent-facing text"
    for needle in case.required:
        assert needle in rendered, f"{case.name}: lost actionable fact {needle!r}\n{rendered}"
    for needle in case.forbidden:
        assert needle not in rendered, f"{case.name}: leaked machine-only metadata {needle!r}\n{rendered}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_mcp_wire_does_not_duplicate_rendered_content(case: OutputCase) -> None:
    rendered = case.render()
    assert rendered is not None
    wire = ToolOutcome(content=({"type": "text", "text": rendered},), structured=case.raw).to_mcp()
    assert "structuredContent" not in wire
    assert wire["content"] == [{"type": "text", "text": rendered}]
    # JSON framing/escaping gets its own fixed allowance.
    assert _tokens(_raw_text(wire)) <= TOKEN_CEILINGS[case.name] + 40


def test_tool_output_budget_regression() -> None:
    rows: list[tuple[str, int, int, int, int, float]] = []
    raw_tokens_total = 0
    rendered_tokens_total = 0
    for case in CASES:
        raw = _raw_text(case.raw)
        rendered = case.render()
        assert rendered is not None
        raw_tokens = _tokens(raw)
        _assert_token_budget(case.name, rendered)
        rendered_tokens = _tokens(rendered)
        raw_tokens_total += raw_tokens
        rendered_tokens_total += rendered_tokens
        reduction = 1.0 - rendered_tokens / max(raw_tokens, 1)
        rows.append((case.name, len(raw), len(rendered), raw_tokens, rendered_tokens, reduction))

        # Every representative optimized surface should be net-positive. Keep
        # this deliberately low: information preservation matters more than
        # winning a synthetic compression contest on each individual payload.
        assert (
            rendered_tokens < raw_tokens
        ), f"{case.name}: agent-facing output regressed: rendered={rendered_tokens} raw={raw_tokens} tokens"

    aggregate_reduction = 1.0 - rendered_tokens_total / raw_tokens_total
    assert (
        aggregate_reduction >= 0.30
    ), f"aggregate tool-output reduction regressed to {aggregate_reduction:.1%}; expected >=30%"

    print("\ntool         raw chars  out chars  raw tok  out tok  reduction")
    for name, raw_chars, out_chars, raw_tok, out_tok, reduction in rows:
        print(f"{name:12} {raw_chars:9} {out_chars:9} {raw_tok:8} {out_tok:8} {reduction:9.1%}")
    print(f"aggregate tokens: {raw_tokens_total} -> {rendered_tokens_total} " f"({aggregate_reduction:.1%} reduction)")
