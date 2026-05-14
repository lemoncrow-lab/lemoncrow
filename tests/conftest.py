"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from atelier.core.foundation.store import ContextStore

if TYPE_CHECKING:
    from atelier.gateway.adapters.runtime import ContextRuntime


@pytest.fixture()
def store(tmp_path: Path) -> ContextStore:
    s = ContextStore(tmp_path / "atelier")
    s.init()
    return s


@pytest.fixture()
def seeded_runtime(tmp_path: Path) -> Iterator[ContextRuntime]:
    """Runtime backed by the bundled seed blocks + rubrics."""
    from importlib import resources

    import yaml

    from atelier.core.foundation.models import ReasonBlock, Rubric
    from atelier.gateway.adapters.runtime import ContextRuntime

    rt = ContextRuntime(root=tmp_path / "atelier")
    blocks_dir = resources.files("atelier") / "infra" / "seed_blocks"
    rubrics_dir = resources.files("atelier") / "core" / "rubrics"
    for p in blocks_dir.iterdir():
        if not p.name.endswith(".yaml"):
            continue
        data = yaml.safe_load(Path(str(p)).read_text(encoding="utf-8"))
        rt.store.upsert_block(ReasonBlock.model_validate(data))
    for p in rubrics_dir.iterdir():
        if not p.name.endswith(".yaml"):
            continue
        data = yaml.safe_load(Path(str(p)).read_text(encoding="utf-8"))
        rt.store.upsert_rubric(Rubric.model_validate(data))
    yield rt
