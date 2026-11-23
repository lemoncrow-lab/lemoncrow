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
_MIN_CONCEPTUAL_FILES = 8
_MAX_CONCEPT_CHAPTERS = 6
_TOPIC_ACRONYMS = frozenset({"api", "cli", "http", "mcp", "oidc", "rbac", "scm", "sql", "ui"})
_SUMMARY_UTILITY = re.compile(
    r"\b(?:cleanup|follow[ -]?up|format(?:ting)?|lint|misc(?:ellaneous)?|test fixes?)\b", re.I
)
_IMPLEMENTATION_TOPIC_WORDS = frozenset({"postgres", "store", "storage", "migration", "sql"})
_GENERIC_TOPIC_TOKENS = frozenset(
    {
        "test",
        "tests",
        "spec",
        "specs",
        "sheet",
        "page",
        "component",
        "components",
        "type",
        "types",
        "model",
        "models",
        "index",
        "main",
        "local",
        "hosted",
        "bar",
        "app",
        "apps",
        "reviews",
        "py",
        "pyi",
        "ts",
        "tsx",
        "js",
        "jsx",
        "md",
        "mdx",
        "rst",
        "sql",
        "css",
        "scss",
        "json",
    }
)


# A directory area names at most this many leading directories, so a deep tree does not become one area per folder.
_AREA_MAX_DEPTH = 3


def _path_label(path: str, category: str = "") -> str:
    """The area a changed file belongs to, written exactly as the repository spells it.

    Generic categories (generated, tests, docs) come from the file's category or
    conventional names; anything else is its directory path (``lib/``,
    ``src/billing/api/``), or the file path itself for a root-level file. A path is
    never re-cased or re-spelled.
    """

    parts = [part for part in path.split("/") if part]
    low = path.lower()
    if category == "generated" or any(part in {"generated", "vendor", "vendored"} for part in parts):
        return "Generated / vendored"
    if category == "test" or category == "tests" or parts[:1] in (["test"], ["tests"]):
        return "Tests"
    if category == "docs" or parts[:1] == ["docs"] or low.endswith((".md", ".mdx", ".rst")):
        return "Docs"
    if not parts:
        return "Other"
    if len(parts) == 1:
        return parts[0]
    return "/".join(parts[: min(len(parts) - 1, _AREA_MAX_DEPTH)]) + "/"


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
    words = raw.replace("_", " ").replace("-", " ")
    return f"{words[:1].upper()}{words[1:]}"


def _split_words(value: str) -> list[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [word.lower() for word in re.split(r"[^A-Za-z0-9]+", value) if word]


def _summary_concept_label(summary: str) -> str:
    text = _CONVENTIONAL.sub("", summary.strip()).strip(" :-")
    if not text or _SUMMARY_UTILITY.search(text):
        return ""
    words = text.split()
    truncated = len(words) > 7
    if truncated:
        words = words[:7]
        # A hard word cap should not turn a grounded commit subject into an
        # obviously unfinished concept such as "picker and". Keep the same
        # source-derived wording, just remove a dangling connector at the cut.
        while words and words[-1].casefold().strip(".,:;") in {"and", "or", "with", "for", "to", "of", "in", "on"}:
            words.pop()
    label = " ".join(words).rstrip(".:")
    return f"{label[:1].upper()}{label[1:]}" if label else ""


def _path_topic_label(path: str, structural_label: str) -> str:
    """Derive a compact source-vocabulary topic without inventing a summary."""

    parts = [part for part in path.split("/") if part]
    name = parts[-1] if parts else path
    words = _split_words(name)
    context_review = "review" in words or "review" in {part.lower() for part in parts}
    meaningful = [
        word for word in words if word not in _GENERIC_TOPIC_TOKENS and word != "review" and not word.isdigit()
    ]
    if not meaningful:
        return ""
    topic_words = [word.upper() if word in _TOPIC_ACRONYMS else word for word in meaningful[:2]]
    topic = " ".join(topic_words)
    if context_review and not topic.lower().startswith("review"):
        topic = f"Review {topic}"
    elif topic:
        topic = f"{topic[:1].upper()}{topic[1:]}"
    if topic.casefold() == structural_label.casefold():
        return ""
    return topic


def _intent_assignments(rows: Sequence[Mapping[str, Any]], commits: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    scope_votes: dict[str, Counter[str]] = defaultdict(Counter)
    structural: dict[str, str] = {}
    row_paths = {str(row.get("path") or "") for row in rows if str(row.get("path") or "")}
    for row in rows:
        path = str(row.get("path") or "")
        structural[path] = _path_label(path, str(row.get("category") or ""))

    for commit in commits:
        scope = _scope_label(str(commit.get("scope") or ""))
        if not scope:
            continue
        for path in commit.get("paths") or ():
            scope_votes[str(path)][scope] += 1

    scope_candidates: dict[str, str] = {}
    for path in structural:
        votes = scope_votes.get(path)
        if not votes:
            continue
        scope, count = votes.most_common(1)[0]
        if list(votes.values()).count(count) == 1:
            scope_candidates[path] = scope
    scope_sizes = Counter(scope_candidates.values())

    # A large agent-authored Change often already contains its best conceptual
    # vocabulary in commit subjects. Use that evidence only when it yields at
    # least two substantial groups; otherwise filenames provide a deterministic
    # fallback and commit ordering remains a separate lens.
    commit_candidate: dict[str, str] = {}
    if len(row_paths) >= _MIN_CONCEPTUAL_FILES:
        for commit in commits:
            label = _summary_concept_label(str(commit.get("summary") or ""))
            if not label:
                continue
            for raw_path in commit.get("paths") or ():
                path = str(raw_path)
                if path in row_paths:
                    commit_candidate[path] = label
        commit_sizes = Counter(commit_candidate.values())
        useful_commit_labels = {label for label, count in commit_sizes.most_common(_MAX_CONCEPT_CHAPTERS) if count >= 2}
        covered = sum(1 for label in commit_candidate.values() if label in useful_commit_labels)
        fallback_commit_label = ""
        if len(useful_commit_labels) < 2 or covered < max(4, len(row_paths) // 2):
            # One broad commit can still provide a useful *remainder* concept
            # after repeated source-vocabulary topics have been carved out. It
            # must not swallow those specific topics or become the commit lens.
            if len(commit_sizes) == 1 and sum(commit_sizes.values()) >= max(4, (len(row_paths) + 1) // 2):
                fallback_commit_label = next(iter(commit_sizes))
            useful_commit_labels = set()

        topic_candidate = {path: _path_topic_label(path, structural[path]) for path in structural}
        topic_sizes = Counter(label for label in topic_candidate.values() if label)
        useful_topic_labels = {label for label, count in topic_sizes.most_common(_MAX_CONCEPT_CHAPTERS) if count >= 2}
    else:
        useful_commit_labels = set()
        fallback_commit_label = ""
        topic_candidate = {}
        useful_topic_labels = set()

    assignments: dict[str, str] = {}
    for path, structural_label in structural.items():
        commit_label = commit_candidate.get(path, "")
        topic_label = topic_candidate.get(path, "")
        scope_label = scope_candidates.get(path)
        if commit_label in useful_commit_labels:
            assignments[path] = commit_label
        elif topic_label in useful_topic_labels:
            assignments[path] = topic_label
        elif fallback_commit_label and commit_label == fallback_commit_label:
            assignments[path] = fallback_commit_label
        elif scope_label is not None and scope_sizes[scope_label] <= _MAX_SCOPE_CHAPTER_FILES:
            assignments[path] = scope_label
        else:
            assignments[path] = structural_label
    return assignments


def _is_structural(label: str, rows: Sequence[Mapping[str, Any]]) -> bool:
    """True when *label* is a repository area (a directory, file or category), not a concept."""

    return any(_path_label(str(row.get("path") or ""), str(row.get("category") or "")) == label for row in rows)


def _chapter_explanation(
    label: str,
    items: Sequence[Mapping[str, Any]],
    commits: Sequence[Mapping[str, Any]],
) -> str:
    paths = {str(row.get("path") or "") for row in items if str(row.get("path") or "")}
    count = f"{len(paths)} file{'s' if len(paths) != 1 else ''}"
    best_summary = ""
    best_overlap = 0
    for commit in commits:
        overlap = len(paths & {str(path) for path in (commit.get("paths") or ())})
        summary = str(commit.get("summary") or "").strip()
        if summary and overlap > best_overlap:
            best_summary, best_overlap = summary, overlap
    summary_label = _summary_concept_label(best_summary) if best_summary else ""
    if summary_label and best_overlap >= max(1, (len(paths) + 1) // 2):
        return f"Grounded in commit: {summary_label}."
    if _is_structural(label, items):
        return f"Changes in {label} across {count}."
    return f"Groups related {label.lower()} source changes across {count}."


def _intent_chapters(rows: Sequence[Mapping[str, Any]], commits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    assignments = _intent_assignments(rows, commits)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        path = str(row.get("path") or "")
        grouped[assignments.get(path, _path_label(path, str(row.get("category") or "")))].append(row)

    # Do not create a "miscellaneous" bucket. If a large projection still
    # fragments into many one-file concepts, fold those files back into their
    # concrete repository area instead.
    if len(grouped) > 8:
        for label, items in list(grouped.items()):
            if len(items) != 1:
                continue
            row = items[0]
            structural = _path_label(str(row.get("path") or ""), str(row.get("category") or ""))
            if structural == label:
                continue
            grouped.pop(label)
            grouped[structural].append(row)

    chapters = [
        {
            **_rollup(
                _chapter_key("intent", label),
                label,
                _chapter_explanation(label, items, commits),
                items,
            ),
            "structural": _is_structural(label, items),
        }
        for label, items in grouped.items()
    ]
    chapters.sort(
        key=lambda item: (1 if item["min_attention_rank"] == 0 else 0, item["min_attention_rank"], item["label"])
    )
    return chapters


_STORY_UTILITY_LABELS = frozenset(
    {
        "Other",
        "Tests",
        "Docs",
        "Generated / vendored",
        "Review",
        "Reviews",
        "App",
        "Frontend",
        "Backend",
        "Server",
        "Core",
        "Infra",
        "Gateway",
        "Enterprise",
        "Client",
    }
)


def conceptual_chapter_sort_key(chapter: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    label = str(chapter.get("label") or "")
    words = set(_split_words(label))
    implementation_only = int(bool(words & _IMPLEMENTATION_TOPIC_WORDS))
    rank = int(chapter.get("min_attention_rank") or 0)
    return (
        -int(chapter.get("file_count") or 0),
        implementation_only,
        1 if rank == 0 else 0,
        rank,
        label,
    )


def conceptual_chapters(chapters: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Specific conceptual groups suitable for primary orientation.

    Broad source areas stay available in the full chapter lenses, but they do
    not displace more explanatory concepts in Change Story / Major changes.
    When no concept exists, the repository areas themselves orient the reader,
    so the story is never empty for a change that has source files.
    Implementation-shaped groups remain available but lose equal-size ties to
    product/behavior concepts.
    """

    useful = [chapter for chapter in chapters if str(chapter.get("label") or "") not in _STORY_UTILITY_LABELS]
    out = [chapter for chapter in useful if not chapter.get("structural")] or useful
    out.sort(key=conceptual_chapter_sort_key)
    return out


def change_story_labels(chapters: Sequence[Mapping[str, Any]], *, limit: int = 4) -> tuple[tuple[str, ...], int]:
    """Return the few conceptual labels worth spending header space on.

    The full chapter lens remains available for navigation. The header is only
    orientation, so utility buckets are suppressed when production concepts
    exist and the remaining concepts are ordered by how much of the change they
    explain, with attention rank as the deterministic tie-break.
    """

    candidates = conceptual_chapters(chapters)
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


def _target_context(chapters: Sequence[dict[str, Any]], targets: Sequence[Any]) -> list[dict[str, Any]]:
    """Attach derived target navigation/progress without creating new review state."""

    out: list[dict[str, Any]] = []
    # Targets by path once, with their place in ``targets``: a chapter picks its paths' targets instead of scanning
    # every target (chapters x targets was the bulk of a 10,000-file Overview). Ties sort by that place, as the scan did.
    by_path: dict[str, list[tuple[int, Any]]] = {}
    for position, target in enumerate(targets):
        by_path.setdefault(str(getattr(target, "path", "")), []).append((position, target))
    for raw in chapters:
        chapter = dict(raw)
        paths = {str(row.get("path") or "") for row in chapter.get("rows") or () if isinstance(row, Mapping)}
        placed = sorted((item for path in paths for item in by_path.get(path, ())), key=lambda item: item[0])
        chapter_targets = [target for _position, target in placed]
        chapter_targets.sort(
            key=lambda target: (
                1 if int(getattr(target, "attention_rank", 0) or 0) == 0 else 0,
                int(getattr(target, "attention_rank", 0) or 0),
                str(getattr(target, "path", "")),
                int(getattr(target, "start_line", 0) or 0),
            )
        )
        target_rows = [
            {
                "target_id": str(getattr(target, "target_id", "")),
                "path": str(getattr(target, "path", "")),
                "label": str(getattr(target, "label", "")),
                "symbol": str(getattr(target, "symbol", "")),
                "kind": str(getattr(target, "kind", "")),
                "start_line": int(getattr(target, "start_line", 0) or 0),
                "end_line": int(getattr(target, "end_line", 0) or 0),
                "state": str(getattr(target, "state", "unknown")),
                "changed_since_mark": bool(getattr(target, "changed_since_mark", False)),
                "attention_rank": int(getattr(target, "attention_rank", 0) or 0),
            }
            for target in chapter_targets
        ]
        progress = {
            "target_count": len(chapter_targets),
            "reviewed": sum(getattr(target, "state", "") == "reviewed" for target in chapter_targets),
            "changed_since_review": sum(
                getattr(target, "state", "") == "changed_since_review" for target in chapter_targets
            ),
            "needs_changes": sum(getattr(target, "state", "") == "needs_changes" for target in chapter_targets),
            "unreviewed": sum(getattr(target, "state", "") == "unreviewed" for target in chapter_targets),
            "unknown": sum(getattr(target, "state", "") == "unknown" for target in chapter_targets),
            "later": sum(getattr(target, "state", "") == "later" for target in chapter_targets),
        }
        explanation = str(chapter.get("reason") or "")
        named = []
        for item in target_rows:
            label = str(item.get("symbol") or item.get("label") or "").strip()
            if label and label not in named:
                named.append(label)
        if named:
            explanation = f"{explanation.rstrip('.')} Key targets: {', '.join(named[:2])}."
        chapter["explanation"] = explanation
        chapter["targets"] = target_rows
        chapter["target_count"] = len(target_rows)
        chapter["progress"] = progress
        out.append(chapter)
    return out


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
    targets: Sequence[Any] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Return derived conceptual, dependency, and commit projections.

    Chapters are orientation only. Target identity, progress, and human judgment
    continue to come from ReviewTarget/mark state and are merely rolled up here.
    """

    commits = _commit_records(repo_root, base_sha, head_sha)
    intent = _intent_chapters(rows, commits)
    dependency = _dependency_order(intent, packet)
    commit_chapters = _commit_chapters(rows, commits)
    return {
        "intent": _target_context(intent, targets),
        "dependency": _target_context(dependency, targets),
        "commits": _target_context(commit_chapters, targets),
    }
