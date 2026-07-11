#!/usr/bin/env bash
# Reconcile companion binaries (Node/Go/Zoekt) to THIS release's pinned versions.
#
# Reuses the installer's reconcile logic in lib/common.sh so the drift rules live
# in ONE place: a binary is (re)provisioned only when its pin in lib/versions.sh
# changed from what was last installed (~/.lemoncrow/companion_versions), or when
# it is missing. Invoked by `lemon update` after a git pull; the full installer
# runs these same functions during a normal install.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

install_node_if_needed
prompt_local_zoekt_selection
install_local_zoekt_if_selected
