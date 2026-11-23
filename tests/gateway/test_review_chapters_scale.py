"""P9-6: chapter target context scales with the number of targets, and answers exactly what the scan did."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from lemoncrow.pro.capabilities.review.chapters import _target_context


def _reference(chapters: Sequence[dict[str, Any]], targets: Sequence[Any]) -> list[dict[str, Any]]:
    """The scan this replaced: every chapter filters every target."""

    out: list[dict[str, Any]] = []
    for raw in chapters:
        chapter = dict(raw)
        paths = {str(row.get("path") or "") for row in chapter.get("rows") or () if isinstance(row, Mapping)}
        chapter_targets = [target for target in targets if str(getattr(target, "path", "")) in paths]
        chapter_targets.sort(
            key=lambda target: (
                1 if int(getattr(target, "attention_rank", 0) or 0) == 0 else 0,
                int(getattr(target, "attention_rank", 0) or 0),
                str(getattr(target, "path", "")),
                int(getattr(target, "start_line", 0) or 0),
            )
        )
        chapter["_order"] = [target.target_id for target in chapter_targets]
        out.append(chapter)
    return out


def _world(rng: random.Random, files: int, per_file: int, chapters: int) -> tuple[list[dict[str, Any]], list[Any]]:
    paths = [f"pkg{index % 7}/mod{index}.py" for index in range(files)]
    targets = [
        SimpleNamespace(
            target_id=f"target:{path}:{part}",
            path=path,
            label=f"{path}::f{part}",
            symbol=f"f{part}",
            kind="symbol",
            # Few distinct values, so ties are common: the order among them is what must not change.
            start_line=rng.choice((1, 1, 10, 20)),
            end_line=30,
            state=rng.choice(("unreviewed", "reviewed", "later", "unknown")),
            changed_since_mark=False,
            attention_rank=rng.choice((0, 0, 1, 2, 3)),
        )
        for path in paths
        for part in range(per_file)
    ]
    rng.shuffle(targets)
    grouped = [
        {
            "id": f"chapter:{index}",
            "reason": "ordered.",
            "rows": [{"path": path} for path in rng.sample(paths, min(len(paths), rng.randint(0, 6)))],
        }
        for index in range(chapters)
    ]
    return grouped, targets


def test_every_chapter_gets_the_targets_and_the_order_the_scan_gave_it() -> None:
    for seed in range(40):
        rng = random.Random(seed)
        chapters, targets = _world(
            rng, files=rng.randint(1, 30), per_file=rng.randint(1, 4), chapters=rng.randint(0, 12)
        )
        got = _target_context(chapters, targets)
        want = _reference(chapters, targets)
        assert [[row["target_id"] for row in chapter["targets"]] for chapter in got] == [
            item["_order"] for item in want
        ], seed
        assert [chapter["target_count"] for chapter in got] == [len(item["_order"]) for item in want]
        for chapter in got:
            assert chapter["progress"]["target_count"] == chapter["target_count"]


def _cpu(chapters: list[dict[str, Any]], targets: list[Any]) -> float:
    started = time.process_time()
    _target_context(chapters, targets)
    return time.process_time() - started


def test_the_cost_grows_with_the_targets_not_with_chapters_times_targets() -> None:
    small = _world(random.Random(1), files=1000, per_file=1, chapters=250)
    large = _world(random.Random(1), files=4000, per_file=1, chapters=1000)
    first, second = min(_cpu(*small) for _ in range(3)), min(_cpu(*large) for _ in range(3))
    # Four times the files and four times the chapters: linear is about 4x, the scan was about 16x.
    assert second < max(first, 0.005) * 9, (first, second)
