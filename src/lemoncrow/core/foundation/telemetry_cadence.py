"""Shared telemetry push cadence.

Single source of truth for how often this client talks to the server about
usage/savings. Two independent pushers read it:

- ``lemoncrow.core.capabilities.licensing.usage_report.REPORT_INTERVAL_SECONDS``
  -- personal cumulative saved/spend totals, drives this device's cap verdict.
- ``lemoncrow.core.service.telemetry.public_rollup.FLUSH_INTERVAL_SECONDS``
  -- anonymous daily aggregate, feeds the public lemoncrow.com/savings page.

They are unrelated systems (different endpoints, different payloads, different
identities -- one is account-bound, the other anonymous) that happen to share
a cadence. Importing this constant instead of hardcoding the number in both
places means they cannot silently drift back apart the way they did before
(one at 1h, the other at 24h, with no shared name to grep for).

If a real reason ever requires the two to diverge again, give each its own
constant back explicitly -- don't quietly reintroduce a second literal here.
"""

from __future__ import annotations

TELEMETRY_PUSH_INTERVAL_SECONDS = 60 * 60
