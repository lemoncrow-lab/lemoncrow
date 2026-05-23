"""Convert mini-SWE-agent output directory to SWE-bench preds.json format.

mini-SWE-agent writes one JSON file per instance under output/:
    {instance_id}.json  with keys: instance_id, patch, model_name_or_path, ...

This script collects all patches and writes a single preds.json suitable
for `sb-cli submit`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def make_preds(input_dir: Path, output_path: Path, run_id: str) -> None:
    preds: dict[str, dict] = {}

    for p in sorted(input_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception as exc:
            print(f"  WARN: could not parse {p.name}: {exc}")
            continue

        instance_id = data.get("instance_id") or p.stem
        patch = data.get("patch") or data.get("model_patch") or ""
        model = data.get("model_name_or_path") or run_id

        preds[instance_id] = {
            "model_patch": patch,
            "model_name_or_path": model,
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(preds, indent=2))
    n_with_patch = sum(1 for v in preds.values() if v["model_patch"])
    print(f"  Wrote {len(preds)} instances ({n_with_patch} with patches) → {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", default="atelier-eval")
    args = parser.parse_args()

    make_preds(Path(args.input), Path(args.output), args.run_id)


if __name__ == "__main__":
    main()
