"""Canonical Atelier CLI — package entry point for ``python -m atelier.gateway.cli``.

Allows ``stack_run`` (and any other caller) to launch the CLI via::

    python -m atelier.gateway.cli --root ... service start ...
"""

from atelier.gateway.cli import main

main()
