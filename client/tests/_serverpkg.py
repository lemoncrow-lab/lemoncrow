"""Locating the private server package, when this checkout has one.

The client declares no dependency on it -- it is proprietary and the public
package must install, type-check and test without it -- so every test that
compares the two halves of a contract is conditional. Running them is the
documented command in the README:

    cd enterprise/server && PYTHONPATH=../../client/src .venv/bin/python -m pytest \\
        ../../client/tests/test_end_to_end_server.py -q

This module is the one place that decides whether they can run, so a skip
reason is the same sentence everywhere.
"""

from __future__ import annotations

import importlib.util

REASON = (
    "the private lemoncrow_server package is not importable here; run this module "
    "from enterprise/server/.venv with PYTHONPATH=client/src"
)


def server_available() -> bool:
    try:
        return importlib.util.find_spec("lemoncrow_server") is not None
    except (ImportError, ValueError):
        return False


def public_registry_available() -> bool:
    """Whether the public ``lemoncrow`` distribution can be imported."""
    try:
        return importlib.util.find_spec("lemoncrow.gateway.adapters.mcp_server") is not None
    except (ImportError, ValueError):
        return False
