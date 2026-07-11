#!/usr/bin/env bash
# Companion-binary versions pinned to THIS LemonCrow release.
#
# Source of truth for the install/upgrade reconcile (scripts/lib/common.sh):
# each companion binary is (re)provisioned ONLY when its pin here differs from
# what was last installed (recorded in ~/.lemoncrow/companion_versions), or when
# the binary is missing. Bump a value to make that binary upgrade on the next
# install / re-run of the installer; leave it and the reconcile is a no-op.
#
# Env overrides win, so CI can pin explicitly: LEMONCROW_PIN_<NAME>=...
LEMONCROW_PIN_NODE="${LEMONCROW_PIN_NODE:-v20.12.2}"
LEMONCROW_PIN_GO="${LEMONCROW_PIN_GO:-latest}"        # Go is only the build tool for Zoekt; "latest" = newest stable
LEMONCROW_PIN_ZOEKT="${LEMONCROW_PIN_ZOEKT:-latest}"  # go-install ref for Zoekt (tag/commit/pseudo-version, or "latest")
