"""Deterministic review chapters: make large changes fit in a human head.

Chapters are a projection of the current revision, never durable review state.
Files/marks remain authoritative; a chapter merely groups those rows under a
conceptual label and rolls their existing state up.  Recomputing chapters can
therefore improve organization without invalidating a single human verdict.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_CONVENTIONAL = re.compile(r"^[a-zA-Z][\w-]*(?:\((?P<scope>[^)]+)\))?[!:]")
_MAX_COMMITS = 50
_MAX_SCOPE_CHAPTER_FILES = 30


def _path_label(path: str, category: str = "") -> str:
    parts = [part for part in path.split("/") if part]
    low = path.lower()
    if category == "generated" or any(part in {"generated", "vendor", "vendored"} for part in parts):
        return "Generated / vendored"
    if category == "test" or category == "tests" or parts[:1] in (["test"], ["tests"]):
        return "Tests"
    if category == "docs" or parts[:1] == ["docs"] or low.endswith((".md", ".mdx", ".rst")):
        return "Docs"
    if parts[:1] == ["frontend"]:
        return "Frontend"
    if parts[:1] == ["integrations"]:
        return "Host integrations"
    if (
        len(parts) >= 5
        and parts[0] == "src"
        and parts[1] == "lemoncrow"
        and parts[2] == "pro"
        and parts[3] == "capabilities"
    ):
        return parts[4].replace("_", " ").title()
    if len(parts) >= 3 and parts[0] == "src" and parts[1] == "lemoncrow":
        return parts[2].replace("_", " ").title()
    if len(parts) >= 2 and parts[0] in {"src", "lib", "packages", "apps"}:
        return parts[1].replace("_", " ").title()
    return (parts[0] if parts else "Other").replace("_", " ").title()


def _chapter_key(prefix: str, label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "other"
    return f"{prefix}:{slug}"


def _rollup(
    key: str, label: str, reason: str, rows: Sequence[Mapping[str, Any]], *, depends_on: Sequence[str] = ()
) -> dict[str, Any]:
    material = [dict(row) for row in rows]
    attention = sum(1 for row in material if row.get("group") in {"needs_attention", "changed_since_my_review"})
    reviewed = sum(1 for row in material if row.get("group") == "reviewed")
    changed = sum(1 for row in material if row.get("group") == "changed_since_my_review")
    ranks = [int(row.get("attention_rank") or 0) for row in material if int(row.get("attention_rank") or 0) > 0]
    return {
        "key": key,
        "label": label,
        "reason": reason,
        "rows": material,
        "file_count": len({str(row.get("path") or "") for row in material}),
        "attention_count": attention,
        "reviewed_count": reviewed,
        "changed_count": changed,
        "min_attention_rank": min(ranks) if ranks else 0,
        "depends_on": list(depends_on),
    }


def _open_repo(repo_root: Path) -> Any | None:
    try:
        import pygit2

        return pygit2.Repository(str(repo_root / ".git" if (repo_root / ".git").is_dir() else repo_root))
    except Exception:
        return None


def _commit_records(repo_root: Path, base_sha: str, head_sha: str) -> list[dict[str, Any]]:
    if not base_sha or not head_sha or base_sha == head_sha:
        return []
    repo = _open_repo(repo_root)
    if repo is None:
        return []
    try:
        import pygit2

        head = repo.get(head_sha)
        base = repo.get(base_sha)
        if head is None or base is None:
            return []
        walker = repo.walk(head.id, pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE)
        walker.hide(base.id)
        commits = list(walker)[:_MAX_COMMITS]
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    previous_tree = getattr(base, "tree", None)
    for commit in commits:
        paths: set[str] = set()
        try:
            parent_tree = commit.parents[0].tree if commit.parents else previous_tree
            diff = repo.diff(parent_tree, commit.tree)
            for patch in diff:
                delta = patch.delta
                path = str(delta.new_file.path or delta.old_file.path or "")
                if path:
                    paths.add(path)
        except Exception:
            paths = set()
        summary = str(getattr(commit, "message", "") or "").splitlines()[0].strip()
        match = _CONVENTIONAL.match(summary)
        scope = (match.group("scope") if match else "") or ""
        out.append({"sha": str(commit.id), "summary": summary or str(commit.id)[:12], "scope": scope, "paths": paths})
        previous_tree = getattr(commit, "tree", previous_tree)
    return out


def _scope_label(scope: str) -> str:
    """One human-readable intent label, or ``""`` when a scope names several.

    Conventional commits sometimes use ``feat(review,usage,model): ...`` for a
    cross-cutting change. Treating that comma-separated list as one chapter
    creates labels like ``Review,Usage,Model`` that explain less than the paths
    themselves. Multi-scope commits therefore fall back to repository structure;
    a single scope remains useful intent evidence.
    """

    raw = scope.strip()
    if not raw or re.search(r"[,/+|]", raw):
        return ""
    return raw.replace("_", " ").replace("-", " ").title()


def _intent_assignments(rows: Sequence[Mapping[str, Any]], commits: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    scope_votes: dict[str, Counter[str]] = defaultdict(Counter)
    for commit in commits:
        scope = _scope_label(str(commit.get("scope") or ""))
        if not scope:
            continue
        for path in commit.get("paths") or ():
            scope_votes[str(path)][scope] += 1

    scope_candidates: dict[str, str] = {}
    structural: dict[str, str] = {}
    for row in rows:
        path = str(row.get("path") or "")
        structural[path] = _path_label(path, str(row.get("category") or ""))
        votes = scope_votes.get(path)
        if not votes:
            continue
        scope, count = votes.most_common(1)[0]
        if list(votes.values()).count(count) == 1:
            scope_candidates[path] = scope

    scope_sizes = Counter(scope_candidates.values())
    assignments: dict[str, str] = {}
    for path, structural_label in structural.items():
        candidate = scope_candidates.get(path)
        if candidate is not None and scope_sizes[candidate] <= _MAX_SCOPE_CHAPTER_FILES:
            assignments[path] = candidate
        else:
            assignments[path] = structural_label
    return assignments


def _intent_chapters(rows: Sequence[Mapping[str, Any]], commits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    assignments = _intent_assignments(rows, commits)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[assignments.get(str(row.get("path") or ""), "Other")].append(row)

    # Avoid turning a heterogeneous change into fifteen one-file headings. If
    # there are already many groups, merge singleton path fallbacks into Other.
    if len(grouped) > 8:
        singles = [label for label, items in grouped.items() if len(items) == 1]
        if len(singles) > 1:
            other: list[Mapping[str, Any]] = []
            for label in singles:
                other.extend(grouped.pop(label))
            grouped["Other"] += other

    chapters = [
        _rollup(
            _chapter_key("intent", label),
            label,
            "grouped from commit scope when unambiguous, otherwise repository structure",
            items,
        )
        for label, items in grouped.items()
    ]
    chapters.sort(
        key=lambda item: (1 if item["min_attention_rank"] == 0 else 0, item["min_attention_rank"], item["label"])
    )
    return chapters


_STORY_UTILITY_LABELS = frozenset({"Other", "Tests", "Docs", "Generated / vendored"})


def change_story_labels(chapters: Sequence[Mapping[str, Any]], *, limit: int = 4) -> tuple[tuple[str, ...], int]:
    """Return the few conceptual labels worth spending header space on.

    The full chapter lens remains available for navigation. The header is only
    orientation, so utility buckets are suppressed when production concepts
    exist and the remaining concepts are ordered by how much of the change they
    explain, with attention rank as the deterministic tie-break.
    """

    meaningful = [chapter for chapter in chapters if str(chapter.get("label") or "") not in _STORY_UTILITY_LABELS]
    candidates = meaningful or list(chapters)
    candidates.sort(
        key=lambda chapter: (
            -int(chapter.get("file_count") or 0),
            1 if int(chapter.get("min_attention_rank") or 0) == 0 else 0,
            int(chapter.get("min_attention_rank") or 0),
            str(chapter.get("label") or ""),
        )
    )
    labels: list[str] = []
    seen: set[str] = set()
    for chapter in candidates:
        label = str(chapter.get("label") or "").strip()
        folded = label.casefold()
        if not label or folded in seen:
            continue
        seen.add(folded)
        labels.append(label)
    visible = tuple(labels[: max(0, limit)])
    return visible, max(0, len(labels) - len(visible))


def _chapter_attention_key(chapter: Mapping[str, Any]) -> tuple[int, int, int, str]:
    """Spend attention first when dependency constraints leave a choice.

    A dependency lens is not a license to forget review priority. Among chapters
    that are equally ready, prefer one with a ranked attention item, then the
    earliest rank, then more attention-bearing files. Alphabetical order is the
    final deterministic tie-break only.
    """

    rank = int(chapter.get("min_attention_rank") or 0)
    return (
        1 if rank == 0 else 0,
        rank,
        -int(chapter.get("attention_count") or 0),
        str(chapter.get("label") or ""),
    )


def _dependency_order(chapters: Sequence[dict[str, Any]], packet: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not chapters:
        return []
    by_path: dict[str, str] = {}
    by_key = {str(chapter["key"]): chapter for chapter in chapters}
    for chapter in chapters:
        for row in chapter["rows"]:
            by_path[str(row.get("path") or "")] = str(chapter["key"])

    deps: dict[str, set[str]] = {key: set() for key in by_key}
    for site in (packet or {}).get("impact") or ():
        if not isinstance(site, dict):
            continue
        source_path = str(site.get("source_path") or "")
        target_path = str(site.get("path") or "").split(":L", 1)[0]
        source = by_path.get(source_path)
        target = by_path.get(target_path)
        if source and target and source != target:
            # Changed source influences target: understand the source chapter first.
            deps[target].add(source)

    indegree = {key: len(value) for key, value in deps.items()}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for target, sources in deps.items():
        for source in sources:
            outgoing[source].add(target)

    ready = [key for key, degree in indegree.items() if degree == 0]
    ordered: list[str] = []
    while ready:
        ready.sort(key=lambda key: _chapter_attention_key(by_key[key]))
        key = ready.pop(0)
        ordered.append(key)
        for target in sorted(outgoing.get(key, ()), key=lambda item: _chapter_attention_key(by_key[item])):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    # Cycles are normal in code. Keep the cyclic tail attention-ordered too.
    ordered.extend(
        sorted((key for key in by_key if key not in ordered), key=lambda key: _chapter_attention_key(by_key[key]))
    )

    result: list[dict[str, Any]] = []
    for key in ordered:
        chapter = dict(by_key[key])
        chapter["depends_on"] = sorted(deps[key])
        chapter["reason"] = "ordered by observed in-patch dependencies; equally-ready chapters stay attention-first"
        result.append(chapter)
    return result


def _commit_chapters(rows: Sequence[Mapping[str, Any]], commits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_path = {str(row.get("path") or ""): row for row in rows}
    out: list[dict[str, Any]] = []
    previous = ""
    for index, commit in enumerate(commits, start=1):
        touched = [by_path[path] for path in sorted(commit.get("paths") or ()) if path in by_path]
        if not touched:
            continue
        sha = str(commit.get("sha") or "")
        key = f"commit:{sha}"
        out.append(
            _rollup(
                key,
                str(commit.get("summary") or sha[:12]),
                f"commit {index} of {len(commits)} · {sha[:12]}",
                touched,
                depends_on=(previous,) if previous else (),
            )
        )
        previous = key
    return out


def build_chapter_lenses(
    rows: Sequence[Mapping[str, Any]],
    packet: Mapping[str, Any] | None,
    *,
    repo_root: Path,
    base_sha: str = "",
    head_sha: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """Return the three chapter projections beside the existing Attention lens."""

    commits = _commit_records(repo_root, base_sha, head_sha)
    intent = _intent_chapters(rows, commits)
    dependency = _dependency_order(intent, packet)
    commit_chapters = _commit_chapters(rows, commits)
    return {"intent": intent, "dependency": dependency, "commits": commit_chapters}
