from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from pytest import MonkeyPatch

from atelier.core.runtime import AtelierRuntimeCore
from atelier.gateway.cli import cli


def _init_root(root: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(root), "init"])
    assert result.exit_code == 0, result.output


def _cached_grep(root: Path, pattern: str, search_path: Path) -> dict[str, object]:
    result = CliRunner().invoke(
        cli,
        ["--root", str(root), "cached-grep", pattern, "--path", str(search_path)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, dict)
    return payload


def test_cached_grep_hits_at_least_95_percent_for_stable_content(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    target = tmp_path / "catalog.txt"
    target.write_text("GIDs are stable, handles are not\n", encoding="utf-8")

    results = [_cached_grep(root, "GIDs", target) for _ in range(20)]

    hits = sum(1 for item in results if item["cached"] is True)
    assert hits >= 19
    assert results[0]["cached"] is False
    assert all(item["cached"] is True for item in results[1:])


def test_cached_grep_content_mutation_causes_exactly_one_new_miss(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    target = tmp_path / "catalog.txt"
    target.write_text("GIDs are stable, handles are not\n", encoding="utf-8")

    before = [_cached_grep(root, "GIDs", target) for _ in range(10)]
    target.write_text("GIDs are stable, handles are not\nProduct IDs remain canonical\n", encoding="utf-8")
    after = [_cached_grep(root, "GIDs", target) for _ in range(10)]

    assert before[0]["cached"] is False
    assert all(item["cached"] is True for item in before[1:])
    assert after[0]["cached"] is False
    assert all(item["cached"] is True for item in after[1:])


def test_smart_read_cache_disabled_env_bypasses_hits(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    target = tmp_path / "module.py"
    target.write_text("def stable_gid():\n    return 'gid'\n", encoding="utf-8")

    monkeypatch.setenv("ATELIER_CACHE_DISABLED", "1")
    runtime = AtelierRuntimeCore(root)

    first = runtime.smart_read(target, max_lines=20)
    second = runtime.smart_read(target, max_lines=20)

    assert first["cached"] is False
    assert second["cached"] is False
    assert runtime.capability_status()["tool_supervision"]["cache_enabled"] is False
