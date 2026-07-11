"""SWE-bench Verified loader: dataset rows -> filtered Python instance specs.

Multi-SWE-bench is, by design, the seven non-Python languages -- its dataset
ships an empty ``python`` placeholder -- so Python issue-resolution coverage
comes from SWE-bench (Verified), graded by the official ``swebench`` harness.
The in-container runner is shared with the multi-swe path: the agent edits the
repo checked out at ``/testbed`` in place and its git diff becomes the
candidate patch.

The gold ``patch``/``test_patch`` are retained only so the multi-file filter
can size the change; they are never placed in the agent prompt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from benchmarks.codebench.multiswe import changed_file_count

# Default Python issue-resolution benchmark + split (swebench naming).
DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_SPLIT = "test"

# swe-lite: a fixed, pinned 10-task slice so `--suite swe-lite` runs a small
# sample without the caller typing 10 --instance flags by hand. Drawn from
# SWE-bench VERIFIED (the princeton Lite split is no longer used): the 10
# LIGHTEST tasks (by baseline cost) from the swe50_2026_06_30 stress corpus
# that BOTH arms solved in 5/5 reps on claude-opus-4-8. Every task is provably
# solvable by the baseline, so an arm delta measures efficiency on real work
# instead of how long an unsolvable task spirals before the turn cap (the
# previous slice's matplotlib-23913 / sphinx-7975 / requests-2674 /
# sympy-16503: 0 solves in 12 attempts across both arms while dominating
# suite cost). 8 distinct repos.
SWE_LITE_INSTANCE_IDS: tuple[str, ...] = (
    "django__django-12155",
    "pallets__flask-5014",
    "pytest-dev__pytest-8399",
    "psf__requests-6028",
    "pydata__xarray-3993",
    "astropy__astropy-13579",
    "django__django-13837",
    "pydata__xarray-3305",
    "django__django-14007",
    "sympy__sympy-13877",
)

# suite name -> (default dataset, default instance ids). Consulted by
# multiswe_run.py's _select_backend() to fill in --dataset/--instances only
# when the caller didn't pass them explicitly (explicit flags always win).
SUITE_DEFAULTS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "swe-bench-verified": (DEFAULT_DATASET, None),
    "swe-lite": (DEFAULT_DATASET, SWE_LITE_INSTANCE_IDS),
}

# Every sweb.eval.* image checks the repo out here.
TESTBED = "/testbed"


def image_ref(instance_id: str, *, namespace: str = "swebench", arch: str = "x86_64", tag: str = "latest") -> str:
    """SWE-bench per-instance image tag.

    Mirrors ``swebench.harness.test_spec.TestSpec.instance_image_key``: the
    remote image is namespaced and ``__`` is rewritten to ``_1776_`` for Docker
    tag-safety, e.g. ``swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest``.
    """
    key = f"sweb.eval.{arch}.{instance_id.lower()}:{tag}"
    return f"{namespace}/{key}".replace("__", "_1776_")


@dataclass(frozen=True)
class SweBenchInstance:
    """One gradeable SWE-bench task (duck-compatible with the in-container runner).

    ``patch``/``test_patch`` are the gold answer + test; they are kept out of
    ``repr`` and never placed in the agent prompt.
    """

    instance_id: str
    repo: str
    base_commit: str
    language: str
    image: str
    problem_statement: str
    changed_files: int
    repo_dir: str = TESTBED
    patch: str = field(default="", repr=False)
    test_patch: str = field(default="", repr=False)


def load_instances(
    *,
    dataset: str | None = None,
    split: str = DEFAULT_SPLIT,
    instances: Iterable[str] | None = None,
    min_changed_files: int = 2,
    limit: int | None = None,
) -> list[SweBenchInstance]:
    """Load + filter SWE-bench instances from the dataset (HF or local json/jsonl).

    Defaults select the multi-file slice where LemonCrow's navigation/edit tooling
    matters, mirroring :func:`benchmarks.codebench.multiswe.load_instances`.
    """
    from swebench.harness.utils import load_swebench_dataset

    name = dataset or DEFAULT_DATASET
    wanted = list(instances) if instances else None
    rows = load_swebench_dataset(name, split, wanted)
    out: list[SweBenchInstance] = []
    for row in rows:
        instance_id = str(row["instance_id"])
        gold = str(row.get("patch") or "")
        changed = changed_file_count(gold)
        # Explicit --instances are deliberate requests: never drop them on the
        # multi-file filter (mirrors multiswe.load_instances), and preserve the
        # requested order so callers can sequence runs (e.g. shortest-first).
        if wanted is None and changed < min_changed_files:
            continue
        out.append(
            SweBenchInstance(
                instance_id=instance_id,
                repo=str(row.get("repo") or ""),
                base_commit=str(row.get("base_commit") or ""),
                language="python",
                image=image_ref(instance_id),
                problem_statement=str(row.get("problem_statement") or ""),
                changed_files=changed,
                patch=gold,
                test_patch=str(row.get("test_patch") or ""),
            )
        )
        if wanted is None and limit is not None and len(out) >= limit:
            break
    if wanted is not None:
        rank = {iid: i for i, iid in enumerate(wanted)}
        out.sort(key=lambda inst: rank.get(inst.instance_id, len(rank)))
    return out
