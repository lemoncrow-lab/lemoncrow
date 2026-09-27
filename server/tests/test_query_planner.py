from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from lemoncrow_server_core.index.contracts import IndexBackend, ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.memory import build_memory_backend
from lemoncrow_server_core.index.planner import QueryIntent, plan_code_query
from lemoncrow_server_core.index.query import IndexQuery

CAP = 1024 * 1024
ORG = "org_planner"
SUBJECT = "alice"


def _entry(path: str, data: bytes) -> ManifestEntry:
    profile = "python-3" if path.endswith(".py") else "lexical-1"
    return ManifestEntry(path, sha256_hex(data), profile, 0o100644, len(data))


def _view() -> tuple[IndexBackend, str]:
    backend = build_memory_backend(content_size_cap=CAP)
    files = {
        "pkg/alpha.py": b"def AlphaWidget():\n    return 1\n",
        "pkg/caller.py": b"from pkg.alpha import AlphaWidget\n\ndef caller():\n    return AlphaWidget()\n",
        "pkg/auth.py": b"def authenticate_user(request):\n    return request.user\n",
        "docs/alpha.md": b"AlphaWidget documentation\n",
    }
    entries = tuple(_entry(path, data) for path, data in sorted(files.items()))
    chunks = chunk_manifest(entries)
    repo = backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", "planner-repo"))
    opened = backend.views.open(
        org_id=ORG,
        repo_id=repo,
        base_revision="HEAD",
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=3600,
        content_subject=SUBJECT,
        owner_subject=SUBJECT,
    )
    for chunk in chunks:
        backend.views.put_manifest_chunk(
            org_id=ORG,
            view_id=opened.state.view_id,
            chunk_id=chunk.chunk_id,
            entries=chunk.entries,
        )
    for path, data in files.items():
        entry = _entry(path, data)
        backend.content.put(ORG, entry.content_digest, data)
        backend.provenance.record(ORG, repo, SUBJECT, (entry.content_digest,))
        backend.analysis.ensure(ORG, entry.content_digest, entry.parser_profile, data)
    return backend, opened.state.view_id


@pytest.mark.parametrize(
    ("query", "intent", "channels"),
    (
        ("AlphaWidget", QueryIntent.EXACT_SYMBOL, ("symbol",)),
        ("pkg/alpha.py", QueryIntent.PATH, ("path",)),
        ("callers of AlphaWidget", QueryIntent.IMPACT, ("symbol", "relations")),
        ("class AlphaWidget", QueryIntent.DEFINITION, ("symbol", "lexical")),
        ("where is authentication user request handled", QueryIntent.NATURAL, ("lexical", "semantic")),
        ("AlphaWidget|caller", QueryIntent.PIPE, ("lexical", "symbol")),
        (r"^def\s+Alpha", QueryIntent.REGEX, ("regex",)),
        ("alpha widget helper", QueryIntent.HYBRID, ("symbol", "lexical", "relations")),
        ("needlex", QueryIntent.HYBRID, ("symbol", "lexical", "relations")),
    ),
)
def test_query_planner_is_deterministic_and_selects_expected_channels(
    query: str, intent: QueryIntent, channels: tuple[str, ...]
) -> None:
    first = plan_code_query(query)
    second = plan_code_query(query)
    assert first == second
    assert first.intent is intent
    assert first.channels == channels


class _AnalysisSpy:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.lexical_candidates_calls = 0
        self.definition_candidates_calls = 0
        self.nearest_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def lexical_candidates(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.lexical_candidates_calls += 1
        return self._delegate.lexical_candidates(*args, **kwargs)

    def definition_candidates(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.definition_candidates_calls += 1
        return self._delegate.definition_candidates(*args, **kwargs)

    def nearest(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.nearest_calls += 1
        return self._delegate.nearest(*args, **kwargs)


def test_exact_symbol_plan_avoids_generic_lexical_and_semantic_channels() -> None:
    backend, view_id = _view()
    spy = _AnalysisSpy(backend.analysis)
    guarded = replace(backend, analysis=spy)
    answer = IndexQuery(guarded, content_size_cap=CAP).search(
        org_id=ORG,
        view_id=view_id,
        query="AlphaWidget",
        limit=5,
    )
    assert [hit.path for hit in answer.hits] == ["pkg/alpha.py"]
    assert spy.definition_candidates_calls >= 1
    assert spy.lexical_candidates_calls == 0
    assert spy.nearest_calls == 0
    assert answer.plan["intent"] == "exact_symbol"
    assert answer.plan["channels"] == ["symbol"]
    assert answer.plan["reason"] == "unambiguous_code_identifier"


def test_path_plan_avoids_content_search_channels() -> None:
    backend, view_id = _view()
    spy = _AnalysisSpy(backend.analysis)
    guarded = replace(backend, analysis=spy)
    answer = IndexQuery(guarded, content_size_cap=CAP).search(
        org_id=ORG,
        view_id=view_id,
        query="pkg/alpha.py",
        limit=5,
    )
    assert [hit.path for hit in answer.hits] == ["pkg/alpha.py"]
    assert spy.definition_candidates_calls == 0
    assert spy.lexical_candidates_calls == 0
    assert spy.nearest_calls == 0
    assert answer.plan["intent"] == "path"


def test_impact_plan_uses_symbol_seed_and_graph_without_broad_retrieval() -> None:
    backend, view_id = _view()
    assert backend.links.refresh(ORG, view_id).ready
    spy = _AnalysisSpy(backend.analysis)
    guarded = replace(backend, analysis=spy)
    answer = IndexQuery(guarded, content_size_cap=CAP).search(
        org_id=ORG,
        view_id=view_id,
        query="callers of AlphaWidget",
        limit=8,
    )
    paths = {hit.path for hit in answer.hits}
    assert "pkg/alpha.py" in paths
    assert "pkg/caller.py" in paths
    assert spy.definition_candidates_calls >= 1
    assert spy.lexical_candidates_calls == 0
    assert spy.nearest_calls == 0
    assert answer.plan["intent"] == "impact"
    assert answer.degraded is False


def test_natural_language_plan_exposes_explain_output_and_metrics() -> None:
    backend, view_id = _view()
    answer = IndexQuery(backend, content_size_cap=CAP).search(
        org_id=ORG,
        view_id=view_id,
        query="where is authentication user request handled",
        limit=5,
    )
    assert answer.plan == {
        "intent": "natural_language",
        "channels": ["lexical", "semantic"],
        "reason": "multiword_natural_language",
    }
    assert answer.to_wire()["plan"] == answer.plan
    assert backend.ops is not None
    snapshot = backend.ops.snapshot()
    assert snapshot.value("lemoncrow_index_query_plans_total", intent="natural_language") == 1
    assert snapshot.value("lemoncrow_index_query_plan_channels_total", channel="lexical") >= 1
    assert snapshot.value("lemoncrow_index_query_plan_channels_total", channel="semantic") >= 1


def test_impact_plan_remains_useful_and_degraded_when_graph_is_cold() -> None:
    backend, view_id = _view()
    answer = IndexQuery(backend, content_size_cap=CAP).search(
        org_id=ORG,
        view_id=view_id,
        query="callers of AlphaWidget",
        limit=8,
    )
    assert [hit.path for hit in answer.hits] == ["pkg/alpha.py"]
    assert answer.degraded is True
    assert answer.degraded_reason.startswith("link:")
