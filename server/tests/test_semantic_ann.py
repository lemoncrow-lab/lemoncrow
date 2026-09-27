from __future__ import annotations

from lemoncrow_server_core.index.analysis import SEMANTIC_EMBEDDING_VERSION, embed_terms
from lemoncrow_server_core.index.contracts import SemanticUnit
from lemoncrow_server_core.index.semantic_ann import SemanticAnnIndex, SemanticAnnItem, signatures


def _item(index: int) -> SemanticAnnItem:
    terms = (f"topic{index % 200}", f"entity{index}", f"group{index % 37}", "common")
    return SemanticAnnItem(
        path=f"src/f{index}.py",
        digest=f"{index:064x}"[-64:],
        unit=SemanticUnit(
            unit_id=f"unit:{index}",
            start_line=1,
            end_line=4,
            symbol=f"entity{index}",
            embedding_version=SEMANTIC_EMBEDDING_VERSION,
            embedding=embed_terms(terms),
        ),
    )


def test_ann_scores_bounded_candidates_at_large_generation_scale() -> None:
    index = SemanticAnnIndex.build(_item(item) for item in range(10_000))
    result = index.query(embed_terms(("topic17", "entity3417")), 10)
    assert result.total_units == 10_000
    assert result.candidates_scored <= 640
    assert result.candidates_scored < result.total_units // 10
    assert result.matches[0].path == "src/f3417.py"


def test_ann_quality_tracks_exact_oracle_on_representative_queries() -> None:
    index = SemanticAnnIndex.build(_item(item) for item in range(10_000))
    queries = (
        ("topic17", "entity3417"),
        ("topic17",),
        ("group11", "entity5011"),
        ("entity7777",),
    )
    recalls: list[float] = []
    for terms in queries:
        vector = embed_terms(terms)
        approximate = {match.path for match in index.query(vector, 10).matches}
        exact = {match.path for match in index.exact(vector, 10).matches}
        recalls.append(len(approximate & exact) / max(1, len(exact)))
    assert min(recalls) >= 0.6
    assert sum(recalls) / len(recalls) >= 0.8


def test_ann_roundtrip_preserves_results_without_rebuilding_vectors() -> None:
    index = SemanticAnnIndex.build(_item(item) for item in range(2_000))
    restored = SemanticAnnIndex.from_json(index.to_json())
    vector = embed_terms(("topic43", "entity1243"))
    assert restored.query(vector, 12) == index.query(vector, 12)


def test_ann_excluded_paths_remove_generation_rows_replaced_by_overlay() -> None:
    index = SemanticAnnIndex.build(_item(item) for item in range(600))
    target = "src/f417.py"
    vector = embed_terms(("topic17", "entity417"))
    assert index.query(vector, 5).matches[0].path == target
    excluded = index.query(vector, 5, excluded_paths=frozenset({target}))
    assert all(match.path != target for match in excluded.matches)


def test_an_index_built_from_remembered_signatures_is_the_same_index() -> None:
    # Generations share nearly all units, so the semantic layer remembers each
    # embedding's signatures; the index must not depend on where they came from.
    remembered: dict[object, tuple[int, ...]] = {}

    def remember(vector: object) -> tuple[int, ...]:
        if vector not in remembered:
            remembered[vector] = signatures(vector)  # type: ignore[arg-type]
        return remembered[vector]

    items = [_item(item) for item in range(1_000)]
    plain = SemanticAnnIndex.build(items)
    first = SemanticAnnIndex.build(items, signatures_of=remember)
    again = SemanticAnnIndex.build(reversed(items), signatures_of=remember)
    assert plain.to_json() == first.to_json() == again.to_json()
    query = embed_terms(("topic17", "entity417"))
    assert plain.query(query, 5) == again.query(query, 5)
