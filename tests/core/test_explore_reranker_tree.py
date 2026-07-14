"""Tests for the global LambdaMART explore reranker serving path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from lemoncrow.pro.capabilities.code_context import engine as eng


def _single_feature_tree(feature_index: int) -> dict[str, Any]:
    """Tree: leaf 1.0 when features[feature_index] >= 0.5, else leaf 0.0."""
    return {
        "feature": [feature_index, -1, -1],
        "threshold": [0.5, 0.0, 0.0],
        "left": [1, -1, -1],
        "right": [2, -1, -1],
        "leaf": [0.0, 0.0, 1.0],
    }


def _model(trees: list[dict[str, Any]], window: int = 5) -> dict[str, Any]:
    return {
        "model_type": "lambdamart_trees",
        "version": 2,
        "enabled": True,
        "feature_names": list(eng._ER_FEATURE_NAMES),
        "window": window,
        "trees": trees,
    }


def test_er_tree_score_sums_leaves() -> None:
    trees = [_single_feature_tree(0), _single_feature_tree(1)]
    n = len(eng._ER_FEATURE_NAMES)
    a = [0.0] * n
    a[0] = 0.9  # tree0 -> 1.0
    a[1] = 0.1  # tree1 -> 0.0
    assert eng._er_tree_score(trees, a) == 1.0
    b = [0.0] * n
    b[0] = 0.9
    b[1] = 0.9
    assert eng._er_tree_score(trees, b) == 2.0


def test_rerank_reorders_when_top_changes(monkeypatch) -> None:
    # Score is driven entirely by a single synthetic feature on each entry.
    def fake_features(_query: str, entry: dict[str, Any], _rank: int) -> list[float]:
        feats = [0.0] * len(eng._ER_FEATURE_NAMES)
        feats[0] = float(entry["fscore"])
        return feats

    monkeypatch.setattr(eng, "_er_entry_features", fake_features)
    model = _model([_single_feature_tree(0)])
    fake_self = SimpleNamespace(_load_explore_reranker=lambda: model)

    payload = {"files": [{"path": "a.py", "fscore": 0.0}, {"path": "b.py", "fscore": 1.0}]}
    out = eng.CodeContextEngine._rerank_explore_result(fake_self, "q", payload)

    assert [e["path"] for e in out["files"]] == ["b.py", "a.py"]
    assert out["experiment"]["name"] == "explore_reranker_v2_lambdamart"


def test_rerank_noop_when_top_unchanged(monkeypatch) -> None:
    def fake_features(_query: str, entry: dict[str, Any], _rank: int) -> list[float]:
        feats = [0.0] * len(eng._ER_FEATURE_NAMES)
        feats[0] = float(entry["fscore"])
        return feats

    monkeypatch.setattr(eng, "_er_entry_features", fake_features)
    model = _model([_single_feature_tree(0)])
    fake_self = SimpleNamespace(_load_explore_reranker=lambda: model)

    payload = {"files": [{"path": "a.py", "fscore": 1.0}, {"path": "b.py", "fscore": 0.0}]}
    out = eng.CodeContextEngine._rerank_explore_result(fake_self, "q", payload)

    # Top file already best -> payload returned unchanged (no experiment tag).
    assert [e["path"] for e in out["files"]] == ["a.py", "b.py"]
    assert "experiment" not in out or out.get("experiment") != {"name": "explore_reranker_v2_lambdamart"}


def test_rerank_returns_payload_when_no_model() -> None:
    fake_self = SimpleNamespace(_load_explore_reranker=lambda: None)
    payload = {"files": [{"path": "a.py"}, {"path": "b.py"}]}
    out = eng.CodeContextEngine._rerank_explore_result(fake_self, "q", payload)
    assert out is payload
