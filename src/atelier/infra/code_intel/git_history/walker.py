"""History walking helpers for deleted and renamed symbol ingestion."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from atelier.infra.code_intel.git_history import require_pygit2
from atelier.infra.code_intel.git_history.graveyard import SymbolGraveyard
from atelier.infra.code_intel.git_history.models import CommitRecord, GraveyardEntry
from atelier.infra.code_intel.git_history.renames import detect_renames
from atelier.infra.tree_sitter.tags import detect_language, extract_tags_from_text


def iter_commit_records(
    repo_path: str | Path,
    *,
    limit: int = 500,
    since_sha: str | None = None,
) -> Iterator[CommitRecord]:
    """Yield up to `limit` CommitRecord objects in reverse-chronological order.

    Stops when `since_sha` is encountered (resume support).

    Skip rules (LINEAGE-01):
    - Initial commits (no parents).
    - Merge commits with zero file-level diff patches.
    - Commits with >50 files touched, unless message contains "[lineage:keep]".
    - Bot commits (dependabot/renovate[bot]), unless "[lineage:keep]" present.
    """
    pygit2 = require_pygit2()
    repo = pygit2.Repository(str(repo_path))
    try:
        head = repo.revparse_single("HEAD")
    except Exception:
        return
    count = 0
    for commit in repo.walk(head.id, pygit2.enums.SortMode.TIME):
        if count >= limit:
            break
        sha = str(commit.id)
        if since_sha is not None and sha == since_sha:
            break
        if not commit.parents:
            continue  # initial commit
        is_merge = len(commit.parents) > 1
        parent = commit.parents[0]
        diff = parent.tree.diff_to_tree(commit.tree)
        patches = list(diff)
        if is_merge and len(patches) == 0:
            continue  # pure merge commit
        files_touched = [p.delta.new_file.path for p in patches if p.delta.new_file.path]
        msg = commit.message.strip()
        keep_override = "[lineage:keep]" in msg
        if not keep_override and len(files_touched) > 50:
            continue
        author_email = (commit.author.email or "").lower()
        is_bot = "dependabot" in author_email or "renovate[bot]" in author_email
        if is_bot and not keep_override:
            continue
        yield CommitRecord(
            sha=sha,
            author_date=commit.commit_time,
            message=msg[:2000],
            files_touched=files_touched,
            is_merge=is_merge,
        )
        count += 1


def _load_blob_text(repo: Any, tree: Any, file_path: str) -> str | None:
    try:
        entry = tree[file_path]
    except KeyError:
        return None
    blob = repo[entry.id]
    raw_bytes = cast(bytes, blob.read_raw())
    return raw_bytes.decode("utf-8", errors="replace")


def _iter_definition_entries(
    *,
    source_text: str,
    file_path: str,
    deleted_at_sha: str,
    deleted_at_ts: int,
    last_author: str | None,
    last_commit_msg: str | None,
    rename_target: str | None,
) -> list[GraveyardEntry]:
    language = detect_language(Path(file_path))
    tags = extract_tags_from_text(source_text, file_path, language=language)
    return [
        GraveyardEntry(
            symbol_name=tag.name,
            qualified_name=tag.name,
            file_path=file_path,
            language=language,
            deleted_at_sha=deleted_at_sha,
            deleted_at_ts=deleted_at_ts,
            last_author=last_author,
            last_commit_msg=last_commit_msg,
            rename_target=rename_target,
        )
        for tag in tags
        if tag.kind == "definition"
    ]


def walk_history(repo_path: str | Path, graveyard: SymbolGraveyard) -> None:
    """Populate the graveyard from historical delete and rename commits."""

    pygit2 = require_pygit2()
    repo = pygit2.Repository(str(repo_path))
    head = repo.revparse_single("HEAD")
    for commit in repo.walk(head.id, pygit2.enums.SortMode.TOPOLOGICAL):
        if not commit.parents:
            continue
        parent = commit.parents[0]
        diff = parent.tree.diff_to_tree(commit.tree)
        renames = detect_renames(diff)
        for patch in diff:
            delta = patch.delta
            old_path = delta.old_file.path
            if delta.status == pygit2.enums.DeltaStatus.RENAMED:
                source_text = _load_blob_text(repo, parent.tree, old_path)
                if source_text is None:
                    continue
                for entry in _iter_definition_entries(
                    source_text=source_text,
                    file_path=old_path,
                    deleted_at_sha=str(commit.id),
                    deleted_at_ts=commit.commit_time,
                    last_author=commit.author.email,
                    last_commit_msg=commit.message.strip()[:200],
                    rename_target=renames[old_path].new_path,
                ):
                    graveyard.upsert(entry)
            elif delta.status == pygit2.enums.DeltaStatus.DELETED:
                source_text = _load_blob_text(repo, parent.tree, old_path)
                if source_text is None:
                    continue
                for entry in _iter_definition_entries(
                    source_text=source_text,
                    file_path=old_path,
                    deleted_at_sha=str(commit.id),
                    deleted_at_ts=commit.commit_time,
                    last_author=commit.author.email,
                    last_commit_msg=commit.message.strip()[:200],
                    rename_target=None,
                ):
                    graveyard.upsert(entry)
