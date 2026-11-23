"""The environment core both products import (Workspaces and Review).

Runners, execution identity and the environment fingerprint, the preview proxy and
state, the dependency cache and the services runner live here; Review's modules
import them from this package. The definition parser itself is the public client's
``lemoncrow_client.environment``, so ``lc verify`` needs no server.
"""
