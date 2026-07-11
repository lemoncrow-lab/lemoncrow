"""Multi-SWE-bench loader: dataset rows -> filtered instance specs.

Maps Multi-SWE-bench (ByteDance Seed) dataset JSONL into the spec the
in-container runner and the diff->Docker grader consume. Each row is one
resolved GitHub PR with a prebuilt Docker image carrying the repo + toolchain.

The loader never exposes the gold ``fix_patch``/``test_patch`` to the agent;
they are retained only so the grader can build the per-arm patch JSONL and so
the multi-file filter can size the change.

Schema reference (flash row): org, repo, number, title, body, base{sha},
resolved_issues[{number,title,body}], fix_patch, test_patch, f2p_tests,
p2p_tests, instance_id, difficulty, language.
"""

from __future__ import annotations

import collections
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Counts file headers in a unified diff -> number of files the gold patch touches.
_DIFF_FILE_RE = re.compile(r"(?m)^diff --git ")

# Difficulty labels treated as trivial (no navigation to exercise). Grounded in
# the flash distribution: {"≤15mins", "15mins - 1h", "1h - 4h", "≥4h", None}.
TRIVIAL_DIFFICULTIES: frozenset[str] = frozenset({"≤15mins"})


def image_ref(org: str, repo: str, number: int | str) -> str:
    """Multi-SWE-bench per-instance image tag, e.g. mswebench/burntsushi_m_ripgrep:pr-1294."""
    return f"mswebench/{org}_m_{repo}:pr-{number}".lower()


def changed_file_count(patch: str) -> int:
    return len(_DIFF_FILE_RE.findall(patch or ""))


def build_problem_statement(row: Mapping[str, Any]) -> str:
    """Assemble the agent-facing issue text from title/body + linked issues.

    The raw ``body`` is often just "Resolves #NNNN", so the real statement lives
    in ``resolved_issues``. Excludes any patch/test content (the answer).
    """
    parts: list[str] = []
    title = str(row.get("title") or "").strip()
    body = str(row.get("body") or "").strip()
    if title:
        parts.append(f"# {title}")
    if body:
        parts.append(body)
    issues = row.get("resolved_issues") or []
    rendered: list[str] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        i_title = str(issue.get("title") or "").strip()
        i_body = str(issue.get("body") or "").strip()
        number = issue.get("number")
        header = f"### Issue #{number}: {i_title}".rstrip().rstrip(":")
        block = header + (("\n\n" + i_body) if i_body else "")
        if block.strip():
            rendered.append(block)
    if rendered:
        parts.append("## Linked issues")
        parts.extend(rendered)
    return "\n\n".join(parts).strip()


@dataclass(frozen=True)
class MultiSweInstance:
    """One gradeable Multi-SWE-bench task.

    ``fix_patch``/``test_patch`` are the gold answer + test; they are kept out of
    ``repr`` and never placed in the agent prompt.
    """

    instance_id: str
    org: str
    repo: str
    number: int
    language: str
    base_sha: str
    repo_url: str
    image: str
    problem_statement: str
    difficulty: str | None
    changed_files: int
    f2p_tests: tuple[str, ...]
    p2p_tests: tuple[str, ...]
    fix_patch: str = field(default="", repr=False)
    test_patch: str = field(default="", repr=False)

    def patch_row(self, fix_patch: str) -> dict[str, Any]:
        """Agent-submission row for the grader's patch JSONL (multi_swe_bench Patch)."""
        return {"org": self.org, "repo": self.repo, "number": self.number, "fix_patch": fix_patch}

    @property
    def report_id(self) -> str:
        """Instance id as multi_swe_bench's final_report.json keys it (org/repo:pr-N)."""
        return f"{self.org}/{self.repo}:pr-{self.number}"


def _row_to_instance(row: Mapping[str, Any]) -> MultiSweInstance:
    org = str(row["org"])
    repo = str(row["repo"])
    number = int(row["number"])
    base = row.get("base") or {}
    base_sha = str(base.get("sha") or "") if isinstance(base, Mapping) else ""
    fix_patch = str(row.get("fix_patch") or "")
    f2p = row.get("f2p_tests") or {}
    p2p = row.get("p2p_tests") or {}
    return MultiSweInstance(
        instance_id=str(row.get("instance_id") or f"{org}__{repo}-{number}"),
        org=org,
        repo=repo,
        number=number,
        language=str(row.get("language") or ""),
        base_sha=base_sha,
        repo_url=f"https://github.com/{org}/{repo}",
        image=image_ref(org, repo, number),
        problem_statement=build_problem_statement(row),
        difficulty=(str(row["difficulty"]) if row.get("difficulty") else None),
        changed_files=changed_file_count(fix_patch),
        f2p_tests=tuple(f2p.keys()) if isinstance(f2p, Mapping) else (),
        p2p_tests=tuple(p2p.keys()) if isinstance(p2p, Mapping) else (),
        fix_patch=fix_patch,
        test_patch=str(row.get("test_patch") or ""),
    )


def iter_rows(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_instances(
    path: str | Path,
    *,
    languages: Iterable[str] | None = None,
    min_changed_files: int = 2,
    exclude_trivial: bool = True,
    per_language_limit: int | None = None,
    limit: int | None = None,
) -> list[MultiSweInstance]:
    """Load + filter instances from a Multi-SWE-bench JSONL.

    Defaults select the non-trivial, multi-file slice where LemonCrow's
    navigation/edit tooling actually matters. ``per_language_limit`` stratifies
    the sample so one language can't dominate a small run.
    """
    allowed = {lang.lower() for lang in languages} if languages else None
    per_lang: collections.Counter[str] = collections.Counter()
    out: list[MultiSweInstance] = []
    for row in iter_rows(path):
        inst = _row_to_instance(row)
        if allowed is not None and inst.language.lower() not in allowed:
            continue
        if inst.changed_files < min_changed_files:
            continue
        if exclude_trivial and inst.difficulty in TRIVIAL_DIFFICULTIES:
            continue
        if per_language_limit is not None and per_lang[inst.language] >= per_language_limit:
            continue
        per_lang[inst.language] += 1
        out.append(inst)
        if limit is not None and len(out) >= limit:
            break
    return out
