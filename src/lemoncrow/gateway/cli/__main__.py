"""Canonical LemonCrow CLI — package entry point for ``python -m lemoncrow.gateway.cli``.

Allows ``stack_run`` (and any other caller) to launch the CLI via::

    python -m lemoncrow.gateway.cli --root ... service start ...
"""

from lemoncrow.gateway.cli import main

main()
