"""History walking helpers for deleted and renamed symbol ingestion."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from atelier.infra.code_intel.git_history import require_pygit2
from atelier.infra.code_intel.git_history.graveyard import SymbolGraveyard
from atelier.infra.code_intel.git_history.models import GraveyardEntry
from atelier.infra.code_intel.git_history.renames import detect_renames
from atelier.infra.tree_sitter.tags import detect_language, extract_tags_from_text


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
