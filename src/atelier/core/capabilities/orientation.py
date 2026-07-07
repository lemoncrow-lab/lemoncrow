"""On-demand tool-usage orientation (N8).

Returns Atelier's tool-usage playbook on demand so the optimal-sequencing
guidance can live in ONE fetch instead of being duplicated verbatim in every
system prompt. Content is static and deterministic -- no I/O, no model calls --
so the same fetch is byte-stable across sessions and cheap to cache.

The canonical sequence Atelier optimizes for is:

    explore  ->  navigate  ->  edit  ->  verify

Callers may request a focused ``topic`` to retrieve a single section instead of
the whole playbook; an unknown topic falls back to the overview plus the list of
valid topics (never an error), so this capability always returns usable text.
"""

from __future__ import annotations

from typing import Any

# Ordered so the rendered playbook reads as the canonical lifecycle. Each value
# is a (title, body) pair; bodies are plain text so any host can surface them.
_SECTIONS: dict[str, tuple[str, str]] = {
    "explore": (
        "1. Explore (orient before touching anything)",
        (
            "Ground before editing. `search` = ranked/relevant snippets; `grep` =\n"
            "regex/glob/type-filtered matches; `node` = one definition by name;\n"
            "`read` (outline mode) = cheap skim of large files. Batch independent\n"
            "reads in one `read` call. No edits until you can name the files +\n"
            "symbols defining the deliverable and its constraints."
        ),
    ),
    "navigate": (
        "2. Navigate (build the call graph in your head)",
        (
            "Once grounded, walk structure with the focused code-intel tools, not\n"
            "more grep: `node` = one definition; `explore` = grouped context --\n"
            "definition + callers/callees/usages in one call. Symbol known →\n"
            "prefer these over text search -- indexed and exact, not textual\n"
            "guesses."
        ),
    ),
    "edit": (
        "3. Edit (smallest correct change)",
        (
            "Narrowest change that satisfies the task. `edit` with multiple\n"
            "descriptors in ONE call for multi-file changes -- never file-by-file.\n"
            "`codemod` = AST-shaped rewrites text replace can't express safely.\n"
            "Re-read a fresh range or expanded outline before editing so old/new\n"
            "strings match. Delete dead code outright -- no deprecation shims or\n"
            "tombstones."
        ),
    ),
    "verify": (
        "4. Verify (prove it before reporting)",
        (
            "Close the loop with the narrowest authoritative check: repo's lint,\n"
            "typecheck, smallest relevant test selection via `bash`. Preserve\n"
            "failure evidence: read the delta; on failure change input/scope/\n"
            "approach -- never blind-retry the same command. Report verbatim\n"
            "pass/fail tails, no paraphrase."
        ),
    ),
    "selection": (
        "Tool selection cheat-sheet",
        (
            "- Ranked relevance / 'where is X handled?'  -> `search`\n"
            "- Regex / glob / type-filtered text match    -> `grep`\n"
            "- Find a definition by name                  -> `grep` / `search`\n"
            "- Read one definition's body                 -> `node`\n"
            "- Callers / callees / usages of a symbol     -> `explore` (folds the call graph + references into one call)\n"
            "- Grouped context for a change               -> `explore`\n"
            "- Read a file (outline first on large)       -> `read`\n"
            "- Apply edits (batch multi-file)             -> `edit`\n"
            "- AST-shaped structural rewrite              -> `codemod`\n"
            "- Run a command / tests                      -> `bash`\n"
            "- Recall durable cross-session knowledge     -> `memory`"
        ),
    ),
}

_OVERVIEW = (
    "Atelier tool-usage playbook. Canonical sequence:\n"
    "    explore -> navigate -> edit -> verify\n"
    "Each phase has dedicated tools; run them in order. Symbol known → prefer\n"
    "the exact tools (`node` = one definition, `explore` = call graph +\n"
    "references) over repeated grep."
)


def available_topics() -> list[str]:
    """Return the focused-topic keys accepted by :func:`orientation_playbook`."""
    return list(_SECTIONS.keys())


def orientation_playbook(topic: str | None = None) -> dict[str, Any]:
    """Return the tool-usage playbook, optionally focused on one ``topic``.

    With ``topic`` unset (or empty) the full ordered playbook is returned. With a
    known ``topic`` only that section is returned. An unknown ``topic`` is never
    an error: it returns the overview plus ``topics`` so the caller can retry,
    and sets ``unknown_topic`` to the requested value.
    """
    normalized = (topic or "").strip().lower()
    if not normalized:
        sections = [{"key": key, "title": title, "body": body} for key, (title, body) in _SECTIONS.items()]
        text = _OVERVIEW + "\n\n" + "\n\n".join(f"{title}\n{body}" for title, body in _SECTIONS.values())
        return {
            "topic": None,
            "sequence": ["explore", "navigate", "edit", "verify"],
            "overview": _OVERVIEW,
            "sections": sections,
            "topics": available_topics(),
            "text": text,
        }

    if normalized in _SECTIONS:
        title, body = _SECTIONS[normalized]
        return {
            "topic": normalized,
            "sections": [{"key": normalized, "title": title, "body": body}],
            "topics": available_topics(),
            "text": f"{title}\n{body}",
        }

    return {
        "topic": None,
        "unknown_topic": normalized,
        "overview": _OVERVIEW,
        "topics": available_topics(),
        "text": (f"Unknown topic {normalized!r}. Valid topics: {', '.join(available_topics())}.\n\n{_OVERVIEW}"),
    }


__all__ = ["available_topics", "orientation_playbook"]
