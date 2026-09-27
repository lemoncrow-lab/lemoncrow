from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from lemoncrow_server_core.dispatch import ToolInvocation, ToolResult
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.index import runtime_policy
from lemoncrow_server_core.index.candidates import definition_names, hit_candidate
from lemoncrow_server_core.index.query import QueryAnswer, QueryHit
from lemoncrow_server_core.tracing import TracingDispatcher

from lemoncrow.core.foundation.runtime_trace import TraceStore, active_trace, new_trace_id


class _Inner:
    available = True
    tools = frozenset({"code_search"})
    prewarm_workspace = staticmethod(lambda root, rev: True)

    def __init__(self, result: ToolResult | Exception) -> None:
        self.result = result
        self.seen_trace_id: str | None = None

    def dispatch(self, invocation: ToolInvocation) -> ToolResult:
        trace = active_trace()
        self.seen_trace_id = trace.trace_id if trace is not None else None
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _invocation(request_id: str, **arguments: Any) -> ToolInvocation:
    tenant = SimpleNamespace(request_id=request_id, session_id="sess", view_id="view-1", view_revision=4, org_id="org")
    return ToolInvocation(tool="code_search", arguments=arguments, tenant=tenant)  # type: ignore[arg-type]


def _ok() -> ToolResult:
    return ToolResult(tool="code_search", content=({"type": "text", "text": "x" * 40},))


def test_tracing_dispatcher_reuses_the_client_id_and_records_the_outcome(tmp_path) -> None:
    store = TraceStore(tmp_path)
    inner = _Inner(_ok())
    dispatcher = TracingDispatcher(inner, mode="raw", store=store)  # type: ignore[arg-type]
    tid = new_trace_id()
    dispatcher.dispatch(_invocation(tid, query="authorize_review"))
    assert inner.seen_trace_id == tid  # the runtime below joins this trace
    (record,) = store.find(tid)
    assert record["writer"] == "server" and record["request"]["arguments"] == {"query": "authorize_review"}
    assert record["outcome"]["output_chars"] == 40 and record["workspace"]["revision"] == 4
    assert dispatcher.available and dispatcher.tools == {"code_search"}
    assert dispatcher.prewarm_workspace("/r", 1) is True  # optional capabilities stay visible


def test_non_lt_request_ids_get_a_fresh_trace_id(tmp_path) -> None:
    store = TraceStore(tmp_path)
    TracingDispatcher(_Inner(_ok()), mode="raw", store=store).dispatch(_invocation("req_abc"))  # type: ignore[arg-type]
    (record,) = list(store.iter_records())
    assert record["trace_id"].startswith("lt_") and "req_abc" not in json.dumps(record)


def test_server_errors_are_traced_and_reraised(tmp_path) -> None:
    store = TraceStore(tmp_path)
    tid = new_trace_id()
    boom = ServerError(ErrorCode.TOOL_UNKNOWN, "nope")
    with pytest.raises(ServerError):
        TracingDispatcher(_Inner(boom), mode="raw", store=store).dispatch(_invocation(tid))  # type: ignore[arg-type]
    assert store.find(tid)[0]["outcome"]["is_error"] is True


def test_hosted_mode_never_persists_tenant_queries_or_paths(tmp_path) -> None:
    store = TraceStore(tmp_path)
    tid = new_trace_id()
    TracingDispatcher(_Inner(_ok()), mode="redacted", store=store).dispatch(  # type: ignore[arg-type]
        _invocation(tid, query="payroll_secret_fn", paths=["finance/salaries.py"])
    )
    (record,) = store.find(tid)
    text = json.dumps(record)
    assert "payroll_secret_fn" not in text and "salaries" not in text
    assert record["session_id"] != "sess" and set(record["session_id"]) == {"redacted_sha256"}


def _hit(path: str, score: float, defs: list[str] | None = None, **detail: Any) -> QueryHit:
    d: dict[str, Any] = dict(detail)
    if defs:
        d["definitions"] = [{"name": n, "line": 10, "end": 20} for n in defs]
    return QueryHit(path=path, score=score, detail=d)


def test_hit_candidate_projection_keeps_the_hit_and_symbols() -> None:
    hit = _hit("src/a.py", 2.0, ["authorize_review"])
    candidate = hit_candidate(hit, query="authorize_review")
    assert candidate.payload is hit and candidate.symbols == ("authorize_review",)
    assert (candidate.start_line, candidate.end_line) == (10, 20)
    assert candidate.feature("has_definitions") == 1.0 and definition_names(hit.detail) == ("authorize_review",)


def _answer(*hits: QueryHit) -> QueryAnswer:
    return QueryAnswer(kind="code_search", hits=tuple(hits), at_view_revision=1, searched_paths=9)


class _NoBackend:
    pass


class _FakeQuery:
    def __init__(self, found: dict[str, QueryAnswer]) -> None:
        self.found = found
        self.calls: list[str] = []

    def search(self, *, query: str, **_kw: Any) -> QueryAnswer:
        self.calls.append(query)
        return self.found[query]


def _apply(monkeypatch: pytest.MonkeyPatch, mode: str, query_obj: _FakeQuery, answer: QueryAnswer, **kw: Any):
    monkeypatch.setenv("LEMONCROW_CONTEXT_ENGINE_MODE", mode)
    return runtime_policy.apply_context_engine(
        _NoBackend(),  # type: ignore[arg-type]
        query_obj,  # type: ignore[arg-type]
        org_id="o",
        view_id="v",
        query=kw.pop("query", "where is authorize_review called"),
        answer=answer,
        limit=kw.pop("limit", 3),
        arguments=kw.pop("arguments", {}),
    )


def test_engine_shadow_is_a_strict_noop_on_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _answer(_hit("src/routes.py", 2.0), _hit("src/jwks.py", 1.0), _hit("src/x.py", 0.5))
    query_obj = _FakeQuery({})
    resolved, diag = _apply(monkeypatch, "shadow", query_obj, answer)
    assert resolved is answer and query_obj.calls == []
    assert diag["mode"] == "shadow" and diag["proposed"] == ["follow_symbol"] and diag["expanded"] == 0


def test_engine_off_and_default_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _answer(_hit("src/routes.py", 2.0))
    assert _apply(monkeypatch, "off", _FakeQuery({}), answer)[1] == {"mode": "off"}
    monkeypatch.delenv("LEMONCROW_CONTEXT_ENGINE_MODE", raising=False)
    assert (
        runtime_policy.apply_context_engine(
            _NoBackend(),  # type: ignore[arg-type]
            _FakeQuery({}),  # type: ignore[arg-type]
            org_id="o",
            view_id="v",
            query="where is authorize_review called",
            answer=answer,
            limit=3,
            arguments={},
        )[1]["mode"]
        == "shadow"
    )  # never enforcing by default


def test_engine_enforce_follows_the_missing_definition_and_records_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from lemoncrow.core.foundation.runtime_trace import open_trace

    answer = _answer(_hit("src/routes.py", 2.0), _hit("src/jwks.py", 1.0), _hit("src/x.py", 0.5))
    definition = _answer(_hit("src/review/auth.py", 3.0, ["authorize_review"]))
    query_obj = _FakeQuery({"authorize_review": definition})
    store = TraceStore(tmp_path)
    with open_trace(trace_id=None, writer="server", tool="code_search", mode="raw", store=store) as trace:
        trace.record_retrieval(query="q", candidates=[], selected=["src/routes.py"])
        resolved, diag = _apply(monkeypatch, "enforce", query_obj, answer, arguments={"budget": "normal"})
    paths = [h.path for h in resolved.hits]
    assert len(paths) == 3 and paths[:2] == ["src/routes.py", "src/jwks.py"] and "src/review/auth.py" in paths
    # Round 1 followed the missing definition. Relations would be round 2, but
    # the `normal` budget allows one, so the engine stops there, honestly
    # reporting the answer as still not sufficient.
    assert diag["expanded"] == 1 and query_obj.calls == ["authorize_review"]
    assert diag["proposed"] == ["follow_symbol", "expand_relations"] and diag["stop_reason"] == "round_budget"
    assert diag["sufficiency"] > diag["initial_sufficiency"] and diag["sufficient"] is False
    (record,) = list(store.iter_records())
    assert record["retrieval"]["selected"] == paths
    assert record["retrieval"]["extra"]["context_engine"]["expanded"] == 1
    assert {"context.sufficiency", "context.expand", "context.stop"} <= {d["kind"] for d in record["decisions"]}


def test_engine_fast_budget_does_not_expand_even_when_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _answer(_hit("src/routes.py", 2.0), _hit("src/jwks.py", 1.0))
    query_obj = _FakeQuery({"authorize_review": _answer()})
    resolved, diag = _apply(monkeypatch, "enforce", query_obj, answer, arguments={"budget": "fast"})
    assert resolved is answer and query_obj.calls == [] and diag["stop_reason"] == "round_budget"
