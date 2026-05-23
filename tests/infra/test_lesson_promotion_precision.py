from __future__ import annotations

import json
from pathlib import Path

import pytest

from atelier.core.capabilities.lesson_promotion import LessonPromoterCapability
from atelier.core.foundation.models import ReasonBlock, Trace
from atelier.core.foundation.store import ContextStore
from atelier.infra.storage.vector import generate_embedding


class _FixtureEmbedder:
    dim = 384
    name = "fixture-hash"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [generate_embedding(text, dim=self.dim) for text in texts]


def _fixture_path() -> Path:
    return Path("tests/fixtures/200_failed_traces.jsonl")


def test_lesson_promotion_precision_on_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atelier.core.capabilities.lesson_promotion.capability.draft_lesson_body",
        lambda traces: "fixture lesson body",
    )
    store = ContextStore(tmp_path / ".atelier")
    store.init()
    # Seed one existing block so edit_block candidates have a meaningful target.
    store.upsert_block(
        ReasonBlock(
            id="rb-permission-precheck",
            title="Permission precheck before writes",
            domain="coding",
            triggers=["permission", "write"],
            situation="Write operations fail when paths are not writable.",
            dead_ends=["permission denied"],
            procedure=["Verify write permissions for destination paths before mutation."],
        ),
        write_markdown=False,
    )

    promoter = LessonPromoterCapability(store, embedder=_FixtureEmbedder(), cluster_threshold=0.45)

    predicted = 0
    correct = 0

    with _fixture_path().open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= 120:
                break
            row = json.loads(line)
            expected_kind = row.pop("expected_kind", "")
            trace = Trace.model_validate(row)
            store.record_trace(trace, write_json=False)
            candidate = promoter.ingest_trace(trace)
            if candidate is None:
                continue
            predicted += 1
            if expected_kind and candidate.kind == expected_kind:
                correct += 1

    assert predicted > 0
    precision = correct / predicted
    assert precision >= 0.7, f"precision={precision:.3f}, correct={correct}, predicted={predicted}"
