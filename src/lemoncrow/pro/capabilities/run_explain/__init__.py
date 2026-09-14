"""Run attribution: what the evidence says happened, and what was never recorded.

The contract worth knowing before using any of it: a category never appears
without a record behind it, and a source that was absent is named in
``AttributionReport.unresolved`` instead of being read as evidence for some
other category. "No shell signals" and "there was no run ledger" are different
answers and this package keeps them different.

Deliberately free of re-exports: callers import ``.attribution`` or ``.models``
directly, from inside the function that needs them.
"""

from __future__ import annotations
