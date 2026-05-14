"""Write SWE-bench-compatible predictions JSONL.

The official SWE-bench evaluator expects records of the shape::

    {"instance_id": "...", "model_name_or_path": "...", "model_patch": "..."}

Empty patches are still written (with an empty string) so the evaluator can
score them as failures rather than skip them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path


def export_predictions(
    items: Iterable[tuple[str, str, str]],
    out_path: Path,
) -> Path:
    """Write ``(instance_id, model_name_or_path, model_patch)`` triples."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for instance_id, model_name, patch in items:
            rec = {
                "instance_id": instance_id,
                "model_name_or_path": model_name,
                "model_patch": patch or "",
            }
            f.write(json.dumps(rec))
            f.write("\n")
    return out_path


def write_patch_file(patch: str, instance_id: str, mode: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = instance_id.replace("/", "__")
    p = out_dir / f"{safe}.{mode}.patch"
    p.write_text(patch or "")
    return p
