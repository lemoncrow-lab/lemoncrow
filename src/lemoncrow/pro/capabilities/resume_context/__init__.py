"""Resume context: a bounded, source-linked brief for continuing one session.

Deliberately free of re-exports: callers import ``.builder`` directly, from
inside the function that needs it, so nothing pays for the review packet the
builder pulls in unless it actually builds a brief.
"""

from __future__ import annotations
