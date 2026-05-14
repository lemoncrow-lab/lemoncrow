"""Dead-end approach tracker for reasoning reuse."""

from __future__ import annotations

import re


def _normalise_approach(text: str) -> str:
    """Normalise an approach description for fuzzy matching."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9_\s]", " ", text)
    return " ".join(text.split())


class DeadEndTracker:
    """
    Tracks approaches that have been tried and failed.

    When ranking procedures, dead-end approaches are penalised so the agent
    does not retry strategies that are known to not work.
    """

    def __init__(self) -> None:
        self._dead_ends: set[str] = set()

    def mark_dead_end(self, approach: str) -> None:
        """Mark an approach as a dead end."""
        self._dead_ends.add(_normalise_approach(approach))

    def is_dead_end(self, approach: str) -> bool:
        """Return True if the approach is known to be a dead end."""
        normalised = _normalise_approach(approach)
        if normalised in self._dead_ends:
            return True
        # Fuzzy check: if > 60% of tokens overlap with a known dead-end
        tokens = set(normalised.split())
        for de in self._dead_ends:
            de_tokens = set(de.split())
            if not de_tokens:
                continue
            overlap = len(tokens & de_tokens)
            if overlap / len(de_tokens) >= 0.6:
                return True
        return False

    def all_dead_ends(self) -> list[str]:
        return sorted(self._dead_ends)

    def dead_end_penalty(self, approach: str) -> float:
        """Return 0.0 (no penalty) or 0.8 (heavy penalty) for dead ends."""
        return 0.8 if self.is_dead_end(approach) else 0.0
