"""Bring-your-own-model substrate: register and describe self-hosted endpoints.

The split that matters: ``endpoints`` persists, ``probe`` measures, and neither
knows the other's job. ``record_probe`` is the only join, so a capability can
only reach ``providers.json`` by having been measured.

Deliberately free of re-exports: callers import ``.endpoints``, ``.models``,
``.probe`` or ``.transport`` directly. A package-level re-export made the
litellm transport chain import the prober, and the prober import the transport,
for no caller's benefit.
"""

from __future__ import annotations
