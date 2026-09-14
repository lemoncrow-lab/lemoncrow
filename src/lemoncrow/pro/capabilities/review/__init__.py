"""Review substrate: deterministic, LLM-free change review packets.

Deliberately free of re-exports. Every caller imports the one submodule it
needs (``.gitdiff``, ``.packet``, ``.render``, ``.html``) from inside the
function that needs it, so a diff-only ``lc review --no-impact`` never pays for
the impact detectors or the history store. A package-level re-export would undo
that for everyone at once: with one here, importing ``review.gitdiff`` also
executed ``models``, ``packet`` and ``render``, which is exactly what the lazy
imports in ``packet.py`` and ``gateway/cli/commands/review.py`` exist to avoid.
"""

from __future__ import annotations
