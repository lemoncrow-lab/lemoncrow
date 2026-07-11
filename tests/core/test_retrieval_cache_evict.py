"""Batched, amortized LRU eviction for the retrieval cache."""

from pathlib import Path

from lemoncrow.core.capabilities.code_context.cache import RetrievalCache


def _fill(cache: RetrievalCache, n: int, size: int) -> None:
    for i in range(n):
        cache.set(
            tool_name="explore",
            args={"q": f"query-{i}"},
            index_version=1,
            repo_id="r",
            payload={"blob": "x" * size},
        )


def test_under_cap_evicts_nothing(tmp_path: Path) -> None:
    cache = RetrievalCache(tmp_path / "cc.sqlite", max_bytes=1 << 20)
    _fill(cache, 5, 128)
    stats = cache.stats(repo_id="r", index_version=1)
    assert stats["entry_count"] == 5


def test_over_cap_evicts_in_batch_toward_target(tmp_path: Path) -> None:
    cache = RetrievalCache(tmp_path / "cc.sqlite", max_bytes=4096)
    _fill(cache, 40, 512)
    stats = cache.stats(repo_id="r", index_version=1)
    # Cap is soft (checked on the 1st and then every 32nd set), so the table
    # may overshoot by up to 31 payloads — but never grow unbounded.
    assert stats["total_bytes"] < 40 * 512
    assert stats["total_bytes"] <= 4096 + 32 * 600


def test_eviction_keeps_recently_hit_entry(tmp_path: Path) -> None:
    cache = RetrievalCache(tmp_path / "cc.sqlite", max_bytes=2048)
    _fill(cache, 32, 256)  # next set() triggers the amortized check
    hit, _ = cache.get(tool_name="explore", args={"q": "query-31"}, index_version=1, repo_id="r")
    assert hit  # bumps last_hit_at past the insert-time ties
    _fill(cache, 1, 256)  # 33rd set: batched eviction runs
    stats = cache.stats(repo_id="r", index_version=1)
    assert stats["entry_count"] < 33  # eviction actually removed entries
    hit, payload = cache.get(tool_name="explore", args={"q": "query-31"}, index_version=1, repo_id="r")
    assert hit and payload is not None
