#!/usr/bin/env bash
# Companion-binary versions pinned to THIS Atelier release.
#
# Source of truth for the install/upgrade reconcile (scripts/lib/common.sh):
# each companion binary is (re)provisioned ONLY when its pin here differs from
# what was last installed (recorded in ~/.atelier/companion_versions), or when
# the binary is missing. Bump a value to make that binary upgrade on the next
# install / re-run of the installer; leave it and the reconcile is a no-op.
#
# Env overrides win, so CI can pin explicitly: ATELIER_PIN_<NAME>=...
ATELIER_PIN_NODE="${ATELIER_PIN_NODE:-v20.12.2}"
ATELIER_PIN_GO="${ATELIER_PIN_GO:-latest}"        # Go is only the build tool for Zoekt; "latest" = newest stable
ATELIER_PIN_ZOEKT="${ATELIER_PIN_ZOEKT:-latest}"  # go-install ref for Zoekt (tag/commit/pseudo-version, or "latest")
