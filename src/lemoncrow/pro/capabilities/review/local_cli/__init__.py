"""The terminal commands of local Review that need a model route or a git hook, shipped with the Review engine.

``lc review --ai``, ``lc review hook``, ``lc review init-action`` and ``lc review config`` live here, in the
public engine, because the installed ``lc`` is the stdlib-only Review client: it hands these four to the engine's
interpreter (``python -m lemoncrow.pro.capabilities.review.local_cli``) instead of carrying a second copy. The
full CLI of a source checkout imports the same modules, so there is one implementation.

Nothing here may import ``lemoncrow.gateway``: the engine does not ship it.
"""

from __future__ import annotations

__all__: list[str] = []
