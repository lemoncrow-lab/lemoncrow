"""Usage substrate: one canonical, honestly-priced row per session and model.

The contract worth knowing before using any of it: ``UsageRow.cost_usd`` is
``float | None`` and ``None`` means the price is unknown, never zero. Sum with
``aggregate``/``totals``, which keep billed and estimated dollars apart and
count the unpriced rows rather than treating them as free.

Deliberately free of re-exports: callers import ``.collect``, ``.aggregate``,
``.explain`` or ``.models`` directly, from inside the function that needs them.
A package-level re-export made ``import usage.aggregate`` execute ``collect``
and ``explain`` as well, which is a cost every ``lc usage`` invocation paid and
no caller asked for.
"""

from __future__ import annotations
