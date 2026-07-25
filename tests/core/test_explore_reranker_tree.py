"""Tests for the global LambdaMART explore reranker serving path."""

from __future__ import annotations

import json
from pathlib import Path
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


def _model(
    trees: list[dict[str, Any]],
    window: int = 5,
    *,
    rerank_lower_when_top_unchanged: bool = False,
) -> dict[str, Any]:
    return {
        "model_type": "lambdamart_trees",
        "version": 2,
        "enabled": True,
        "feature_names": list(eng._ER_FEATURE_NAMES),
        "window": window,
        "trees": trees,
        "rerank_lower_when_top_unchanged": rerank_lower_when_top_unchanged,
    }


def test_bundled_reranker_model_is_valid() -> None:
    raw = json.loads(Path(eng.__file__).with_name("explore_reranker_model.json").read_text(encoding="utf-8"))
    model = eng._validate_er_model(raw)

    assert model is not None
    assert model["window"] == 10
    assert model["active_feature_indices"] == [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
    ]
    assert model["rerank_lower_when_top_unchanged"] is True


def test_legacy_feature_prefix_model_remains_valid() -> None:
    feature_count = 15
    raw = {
        "model_type": "linear",
        "enabled": True,
        "feature_names": list(eng._ER_FEATURE_NAMES[:feature_count]),
        "weights": [0.0] * feature_count,
        "active_feature_indices": [0, 14],
    }

    assert eng._validate_er_model(raw) is not None


def test_path_query_features_capture_query_shape_and_path_match() -> None:
    features = eng._er_entry_features("src/api/client.py", {"path": "src/api/client.py"}, 1)
    by_name = dict(zip(eng._ER_FEATURE_NAMES, features, strict=True))

    assert by_name["path_suffix_exact"] == 1.0
    assert by_name["extension_match"] == 1.0
    assert by_name["query_path_like"] == 1.0
    assert by_name["query_regex_like"] == 0.0
    assert by_name["ordered_path_terms"] == 1.0


def test_entry_features_skip_inactive_symbol_and_source_text(monkeypatch) -> None:
    calls: list[Any] = []

    def fake_flatten(value: Any) -> str:
        calls.append(value)
        return "match"

    monkeypatch.setattr(eng, "_er_flatten_text", fake_flatten)
    entry = {"path": "src/match.py", "symbols": ["symbol"], "source_sections": ["source"]}

    eng._er_entry_features("match", entry, 1, [0, 1, 2, 3, 4, 9, 10, 11, 12, 13, 14])
    assert calls == []

    eng._er_entry_features("match", entry, 1, [5, 7])
    assert calls == [["symbol"], ["source"]]


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
    def fake_features(
        _query: str,
        entry: dict[str, Any],
        _rank: int,
        _active_feature_indices: Any = None,
    ) -> list[float]:
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


def test_rerank_noop_when_top_unchanged_for_legacy_model(monkeypatch) -> None:
    def fake_features(
        _query: str,
        entry: dict[str, Any],
        _rank: int,
        _active_feature_indices: Any = None,
    ) -> list[float]:
        feats = [0.0] * len(eng._ER_FEATURE_NAMES)
        feats[0] = float(entry["fscore"])
        return feats

    monkeypatch.setattr(eng, "_er_entry_features", fake_features)
    model = _model([_single_feature_tree(0)])
    fake_self = SimpleNamespace(_load_explore_reranker=lambda: model)

    payload = {"files": [{"path": "a.py", "fscore": 1.0}, {"path": "b.py", "fscore": 0.0}]}
    out = eng.CodeContextEngine._rerank_explore_result(fake_self, "q", payload)

    assert out is payload


def test_rerank_can_improve_lower_ranks_when_top_is_unchanged(monkeypatch) -> None:
    def fake_features(
        _query: str,
        entry: dict[str, Any],
        _rank: int,
        _active_feature_indices: Any = None,
    ) -> list[float]:
        feats = [0.0] * len(eng._ER_FEATURE_NAMES)
        feats[0] = float(entry["fscore"])
        return feats

    monkeypatch.setattr(eng, "_er_entry_features", fake_features)
    model = _model([_single_feature_tree(0)], rerank_lower_when_top_unchanged=True)
    fake_self = SimpleNamespace(_load_explore_reranker=lambda: model)

    payload = {
        "files": [
            {"path": "a.py", "fscore": 1.0},
            {"path": "b.py", "fscore": 0.0},
            {"path": "c.py", "fscore": 1.0},
        ]
    }
    out = eng.CodeContextEngine._rerank_explore_result(fake_self, "q", payload)

    assert [e["path"] for e in out["files"]] == ["a.py", "c.py", "b.py"]
    assert out["experiment"]["name"] == "explore_reranker_v2_lambdamart"


def test_rerank_returns_payload_when_no_model() -> None:
    fake_self = SimpleNamespace(_load_explore_reranker=lambda: None)
    payload = {"files": [{"path": "a.py"}, {"path": "b.py"}]}
    out = eng.CodeContextEngine._rerank_explore_result(fake_self, "q", payload)
    assert out is payload
