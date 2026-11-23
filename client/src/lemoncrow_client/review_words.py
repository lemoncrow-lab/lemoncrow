"""The words a person reads in Review, in one place so every surface counts and names things the same way."""

from __future__ import annotations


def revision_name(number: object) -> str:
    """``Revision N`` for a person.

    Wire and display numbers count revisions from 0; people count from 1, so ``0`` reads as ``Revision 1``.
    Every user-facing revision label (UI strings, CLI output, feedback sent to agents) goes through here once.
    """

    try:
        return f"Revision {int(str(number)) + 1}"
    except ValueError:
        return "Revision ?"


def part_count(count: int) -> str:
    """``1 part`` / ``6 parts``: the one unit for the reviewable pieces of a change."""

    return f"{count} {'part' if count == 1 else 'parts'}"


# One plain sentence per degraded signal. The raw codes stay in `--json`; a
# person reading the terminal is told what fell back, not the name of a flag.
_DEGRADED_SENTENCES: dict[str, str] = {
    "impact_disabled": "Map and caller impact were not analyzed.",
    "impact_unavailable": "Caller impact analysis is not available in this install.",
    "impact_failed": "Caller impact analysis failed, so callers and impact are missing.",
    "provenance_disabled": "Author attribution was not checked.",
    "provenance_unavailable": "Author attribution is not available in this install.",
    "provenance_failed": "Author attribution failed, so the authoring session is unknown.",
    "provenance_store_missing": "No local agent history was found, so the authoring session is unknown.",
    "provenance_session_not_found": "The given --session-id was not found in the local agent history.",
    "provenance_unmatched": "No recorded agent session matched this change.",
    "provenance_ambiguous": "No agent session matched this change clearly, so the attribution is a guess.",
    "agent_reads_unrecorded": "The authoring agent's host does not record which files it read.",
    "test_evidence_unavailable": "The authoring session recorded no exit codes, so test results cannot be confirmed.",
    "evidence_failed": "Reading the authoring session's command evidence failed.",
    "symbol_relations": "The code index has no caller relationships here, so callers may be missing.",
    "centrality": "The code index has no centrality data, so the review order uses fewer signals.",
    "symbol_line_ranges": "The code index is behind the changed files, so symbol line numbers may be off.",
    "astgrep_unavailable": "ast-grep is not available, so the structural contract checks did not run.",
    "ambiguous_symbol_counts": "Some usage counts could not be tied to the changed definition and were left out.",
    "caller_line_ranges": "Some caller line numbers did not match the reviewed code and were re-anchored or dropped.",
    "symbol_resolution_truncated": "There were too many changed symbols to resolve them all; some were skipped.",
    "caller_queries_truncated": "Callers were looked up for only the first changed symbols.",
    "index_outline_incomplete": "The code index outline missed some definitions in the changed files.",
    "non_definition_symbols": (
        "Some names in the changed files are imports, keys or literals, not definitions, so they are not listed."
    ),
    "cross_repository_sites": "Some impact sites belong to another repository (a submodule or nested clone) and are not listed.",
    "sites_absent_from_revision": "Some impact sites are not in the reviewed revision and are not listed.",
    "impact_sites_truncated": "There were more impact sites than the limit, so the list is cut short.",
    "hunk_patch_truncated": "Some hunks were too large to keep, so their patch text is left out.",
    "symlink_change": "The change includes a symbolic link.",
    "file_mode_change": "The change includes a file permission change.",
    "mode_only": "Some files changed only their file mode, with no content change.",
    "large_file_skipped": "Some files are too large to analyze, so their contents are not checked.",
    "blob_unreadable": "Some file contents could not be read, so they are not checked.",
    "intent_to_add_undetermined": "Could not tell which paths were only marked with `git add -N`.",
    "workspace_impact_failed": "Checking the other repositories of your workspace failed.",
    "workspace_impact_partial": "Only some repositories of your workspace could be checked.",
    "workspace_impact_unavailable": "Impact on the other repositories of your workspace is not available.",
}
# Signals that carry a count after a colon (`submodule_dirty:2`).
_COUNTED_SENTENCES: dict[str, str] = {
    "intent_to_add_excluded": "{n} `git add -N` path(s) hold no staged content and are left out; `git diff --cached` skips them too.",
    "submodule_dirty": "{n} submodule(s) have uncommitted work inside them, which is not part of this change.",
    "lemoncrow_store_excluded": "{n} path(s) under LemonCrow's own store folder are left out.",
    "unmerged_paths": "{n} path(s) have unresolved merge conflicts, so their content is unknown.",
}


def degraded_sentence(code: str) -> str:
    """One plain sentence for a degraded signal; an unknown code is still named, never dropped."""

    name, _, count = code.partition(":")
    if count and name in _COUNTED_SENTENCES:
        return _COUNTED_SENTENCES[name].format(n=count)
    return _DEGRADED_SENTENCES.get(code) or f"Another analysis step fell back ({code})."
