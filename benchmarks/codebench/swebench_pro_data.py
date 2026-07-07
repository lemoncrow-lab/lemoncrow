"""SWE-bench Pro loader: HF dataset rows -> typed instance specs.

SWE-bench Pro (ScaleAI) does not fit the SWE-bench (Verified/Lite) shape that
:mod:`swebench_data` handles: a different HF dataset (``ScaleAI/SWE-bench_Pro``),
16 columns instead of the SWE-bench schema, and several columns that are
themselves stringified list literals (e.g. ``fail_to_pass``) rather than plain
scalars. It is graded by ScaleAI's own harness (``scaleapi/SWE-bench_Pro-os``),
not the ``swebench`` package, so it gets its own loader here and its own
grader in :mod:`swebench_pro_grade` rather than another
:data:`swebench_data.SUITE_DEFAULTS` entry.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Default HF dataset + split (matches the HuggingFace dataset card exactly).
DEFAULT_DATASET = "ScaleAI/SWE-bench_Pro"
DEFAULT_SPLIT = "test"

# Fixed Docker Hub namespace where the prebuilt per-instance images live
# (verified against ``helper_code/image_uri.py`` in the harness repo and the
# HF dataset card). Single source of truth -- swebench_pro_grade imports this
# rather than re-hardcoding it, so the two call sites can't drift.
DOCKERHUB_USERNAME = "jefzda"

# All these prebuilt images set WORKDIR /app for the checked-out repo (fixed
# by the harness's own base_dockerfile convention -- "DO NOT MODIFY THIS
# SECTION"). Pinning it here skips incontainer_entry.sh's .git auto-discovery.
_APP_REPO_DIR = "/app"

# SWE-bench Pro: a fixed, pinned slice so `--suite swe-pro` runs a small
# representative sample without the caller typing 20 --instance flags by hand.
# Sourced from the eval/swe-bench-pro/instances.json list referenced by
# teamchong/pxpipe; every id below is a real instance_id in the
# ScaleAI/SWE-bench_Pro dataset (verified directly against the HF dataset).
SWE_PRO_INSTANCE_IDS: tuple[str, ...] = (
    "instance_future-architect__vuls-36456cb151894964ba1683ce7da5c35ada789970",
    "instance_flipt-io__flipt-2ce8a0331e8a8f63f2c1b555db8277ffe5aa2e63",
    "instance_element-hq__element-web-923ad4323b2006b2b180544429455ffe7d4a6cc3-vnan",
    "instance_qutebrowser__qutebrowser-0833b5f6f140d04200ec91605f88704dd18e2970-v059c6fdc75567943479b23ebca7c07b5e9a7f34c",
    "instance_tutao__tutanota-b4934a0f3c34d9d7649e944b183137e8fad3e859-vbc0d9ba8f0071fbe982809910959a6ff8884dbbf",
    "instance_navidrome__navidrome-677d9947f302c9f7bba8c08c788c3dc99f235f39",
    "instance_NodeBB__NodeBB-0e07f3c9bace416cbab078a30eae972868c0a8a3-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e",
    "instance_gravitational__teleport-89f0432ad5dc70f1f6a30ec3a8363d548371a718",
    "instance_internetarchive__openlibrary-e8084193a895d8ee81200f49093389a3887479ce-ve8c8d62a2b60610a3c4631f5f23ed866bada9818",
    "instance_qutebrowser__qutebrowser-c09e1439f145c66ee3af574386e277dd2388d094-v2ef375ac784985212b1805e1d0431dc8f1b3c171",
    "instance_NodeBB__NodeBB-cfc237c2b79d8c731bbfc6cadf977ed530bfd57a-v0495b863a912fbff5749c67e860612b91825407c",
    "instance_flipt-io__flipt-967855b429f749c28c112b8cb1b15bc79157f973",
    "instance_internetarchive__openlibrary-a48fd6ba9482c527602bc081491d9e8ae6e8226c-vfa6ff903cb27f336e17654595dd900fa943dcd91",
    "instance_navidrome__navidrome-0488fb92cb02a82924fb1181bf1642f2e87096db",
    "instance_element-hq__element-web-7c63d52500e145d6fff6de41dd717f61ab88d02f-vnan",
    "instance_gravitational__teleport-1a77b7945a022ab86858029d30ac7ad0d5239d00-vee9b09fb20c43af7e520f57e9239bbcf46b7113d",
    "instance_ansible__ansible-f327e65d11bb905ed9f15996024f857a95592629-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5",
    "instance_tutao__tutanota-befce4b146002b9abc86aa95f4d57581771815ce-vee878bb72091875e912c52fc32bc60ec3760227b",
)

# Flagged by the source project's own notes as "checkout failed both arms" --
# confirmed here too (overlay build fails on both arms, every rep). Removed
# from the default pinned slice above; kept here so an explicit --instance
# request still gets warned rather than silently eating a $0/0-turn slot.
KNOWN_BAD_INSTANCE_IDS: frozenset[str] = frozenset(
    {"instance_protonmail__webclients-32ff10999a06455cb2147f6873d627456924ae13"}
)


def _parse_list_field(value: str | None) -> list[str]:
    """Parse a stringified-list HF field into an actual list of strings.

    SWE-bench Pro's own harness reads these columns with ``eval(...)`` (see
    ``create_entryscript``/the F2P|P2P check in ``swe_bench_pro_eval.py``), and
    the rows are not uniformly JSON: some are double-quoted JSON
    (``'[\"a\", \"b\"]'``) and some are single-quoted Python repr
    (``\"['a']\"``, which ``json.loads`` rejects). ``ast.literal_eval`` parses
    both safely. Falls back to a single-element list for anything that isn't a
    list literal at all, rather than raising.
    """
    if not value:
        return []
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return [value]
    if isinstance(parsed, (list, tuple)):
        return [str(x) for x in parsed]
    return [str(parsed)]


def _load_dataset_rows(name: str, split: str) -> Any:
    """Thin, mockable seam around ``datasets.load_dataset`` (lazy import: heavy, rarely needed)."""
    import datasets

    return datasets.load_dataset(name, split=split)


@dataclass(frozen=True)
class SweBenchProInstance:
    """One gradeable SWE-bench Pro task -- all 16 HF dataset columns.

    The stringified-list columns (``fail_to_pass``, ``pass_to_pass``,
    ``selected_test_files_to_run``, ``issue_specificity``, ``issue_categories``)
    are parsed into real lists at load time so callers never re-parse JSON/repr
    strings themselves. ``patch``/``test_patch`` are the gold answer + test;
    kept out of ``repr`` and never placed in the agent prompt.
    """

    instance_id: str
    repo: str
    base_commit: str
    repo_language: str
    problem_statement: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    selected_test_files_to_run: list[str]
    issue_specificity: list[str]
    issue_categories: list[str]
    before_repo_set_cmd: str
    dockerhub_tag: str
    requirements: str | None = None
    interface: str | None = None
    # Duck-compatible with SweBenchInstance (incontainer.py reads these two off
    # any instance generically): the prebuilt image to overlay atelier/baseline
    # tooling onto, and the fixed in-image repo path.
    image: str = ""
    repo_dir: str = _APP_REPO_DIR
    patch: str = field(default="", repr=False)
    test_patch: str = field(default="", repr=False)

    @property
    def language(self) -> str:
        """Alias for ``repo_language`` -- multiswe_run.py's stub-task registration
        reads ``.language`` generically off any instance shape."""
        return self.repo_language


def load_instances(
    *,
    dataset: str | None = None,
    split: str = DEFAULT_SPLIT,
    instances: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[SweBenchProInstance]:
    """Load + filter SWE-bench Pro instances from the HF dataset.

    Defaults to the pinned :data:`SWE_PRO_INSTANCE_IDS` slice (sliced by
    ``limit``); an explicit ``instances`` always wins and is never sliced by
    ``limit`` (mirrors the swe-lite/verified convention: an explicit request is
    deliberate and must not be silently narrowed). Warns (does not drop) when
    the known-bad protonmail instance is part of the final selection, and warns
    on any requested id absent from the dataset.
    """
    name = dataset or DEFAULT_DATASET
    explicit = instances is not None
    wanted = list(instances) if explicit else list(SWE_PRO_INSTANCE_IDS[:limit])

    rows = _load_dataset_rows(name, split)
    wanted_set = set(wanted)
    by_id = {str(row["instance_id"]): row for row in rows if str(row["instance_id"]) in wanted_set}

    out: list[SweBenchProInstance] = []
    missing: list[str] = []
    for instance_id in wanted:
        row = by_id.get(instance_id)
        if row is None:
            missing.append(instance_id)
            continue
        out.append(
            SweBenchProInstance(
                instance_id=instance_id,
                repo=str(row.get("repo") or ""),
                base_commit=str(row.get("base_commit") or ""),
                repo_language=str(row.get("repo_language") or ""),
                problem_statement=str(row.get("problem_statement") or ""),
                fail_to_pass=_parse_list_field(row.get("fail_to_pass")),
                pass_to_pass=_parse_list_field(row.get("pass_to_pass")),
                selected_test_files_to_run=_parse_list_field(row.get("selected_test_files_to_run")),
                issue_specificity=_parse_list_field(row.get("issue_specificity")),
                issue_categories=_parse_list_field(row.get("issue_categories")),
                before_repo_set_cmd=str(row.get("before_repo_set_cmd") or ""),
                dockerhub_tag=(dockerhub_tag := str(row.get("dockerhub_tag") or "")),
                image=f"{DOCKERHUB_USERNAME}/sweap-images:{dockerhub_tag}",
                requirements=row.get("requirements") or None,
                interface=row.get("interface") or None,
                patch=str(row.get("patch") or ""),
                test_patch=str(row.get("test_patch") or ""),
            )
        )
    if missing:
        print(f"[swe-pro] requested --instance not found in dataset: {missing}", flush=True)

    selected_bad = KNOWN_BAD_INSTANCE_IDS & {inst.instance_id for inst in out}
    for bad_id in sorted(selected_bad):
        print(
            f"[swe-pro] warning: {bad_id} has a known history of checkout failures on both arms "
            "(flagged by the source project's own notes); keeping it selected, but expect it may fail.",
            flush=True,
        )
    return out
