from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from lemoncrow.core.runtime import LemonCrowRuntimeCore
from tests.helpers import init_store_at


def test_smart_read_cache_disabled_env_bypasses_hits(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    target = tmp_path / "module.py"
    target.write_text("def stable_gid():\n    return 'gid'\n", encoding="utf-8")

    monkeypatch.setenv("LEMONCROW_CACHE_DISABLED", "1")
    runtime = LemonCrowRuntimeCore(root)

    first = runtime.smart_read(target, max_lines=20)
    second = runtime.smart_read(target, max_lines=20)

    assert first["cached"] is False
    assert second["cached"] is False
    assert runtime.capability_status()["tool_supervision"]["cache_enabled"] is False
