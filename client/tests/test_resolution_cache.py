from __future__ import annotations

from lemoncrow_client.resolution_cache import ResolutionCache, cache_budget_bytes, make_cache_key

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


def _key(revision: int, query: str):
    return make_cache_key(
        session_id="sess_1",
        view_id="view_1",
        view_revision=revision,
        tool="code_search",
        arguments={"query": query},
    )


def test_auto_budget_is_ten_percent_of_available_memory_with_a_large_machine_cap() -> None:
    assert cache_budget_bytes(available_bytes=16 * _GIB) == int(1.6 * _GIB)
    assert cache_budget_bytes(available_bytes=64 * _GIB) == 4 * _GIB
    assert 50 * _MIB <= cache_budget_bytes(available_bytes=512 * _MIB) <= 128 * _MIB


def test_explicit_budget_accepts_human_units_and_zero_disables() -> None:
    assert cache_budget_bytes("1.5G", available_bytes=1) == int(1.5 * _GIB)
    assert cache_budget_bytes("0", available_bytes=16 * _GIB) == 0
    assert cache_budget_bytes("nonsense", available_bytes=16 * _GIB) == int(1.6 * _GIB)


def test_lru_is_bounded_by_entries_and_revision_is_part_of_the_key() -> None:
    cache = ResolutionCache(4 * _MIB, max_entries=2)
    first = _key(1, "alpha")
    second = _key(1, "beta")
    third = _key(1, "gamma")
    assert first is not None and second is not None and third is not None
    cache.put(first, {"content": [{"text": "a"}]}, "v1")
    cache.put(second, {"content": [{"text": "b"}]}, "v2")
    assert cache.get(first) is not None  # refresh first, so second is LRU
    cache.put(third, {"content": [{"text": "c"}]}, "v3")
    assert cache.get(second) is None
    assert cache.get(first) is not None
    assert _key(2, "alpha") != first
    assert cache.stats.entries == 2
    assert cache.stats.bytes_used <= cache.stats.budget_bytes


def test_pruning_discards_previous_view_revisions() -> None:
    cache = ResolutionCache(4 * _MIB)
    old = _key(3, "alpha")
    current = _key(4, "alpha")
    assert old is not None and current is not None
    cache.put(old, {"content": [{"text": "old"}]}, "old")
    cache.put(current, {"content": [{"text": "new"}]}, "new")
    cache.prune_scope("sess_1", "view_1", 4)
    assert cache.get(old) is None
    assert cache.get(current) is not None
