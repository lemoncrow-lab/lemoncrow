#!/usr/bin/env bash
# Sourced by the release drills that install a real LemonCrow (scripts/verify_release_upgrade.sh
# and the compiled-candidate drill): one scratch WORK directory that holds everything they
# install, isolated from the host that runs them. Leaves WORK, DRILL_URL and an EXIT trap behind.
#
# Usage (after `set -euo pipefail`): source "$(dirname "$0")/release_drill_env.sh"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-upgrade.XXXXXX")"
release_drill_cleanup() {
  local status=$?
  local server_log="$HOME/.lemoncrow/server/server.log"
  if ((status != 0)) && [[ -s "$server_log" ]]; then
    echo "--- candidate local server log (last 120 lines) ---" >&2
    tail -n 120 "$server_log" >&2 || true
  fi
  # The candidate's server runs process-supervised under this drill; stop it before its files go.
  if [[ -f "$LEMONCROW_INSTALL_DIR/scripts/local_server.sh" ]]; then
    bash "$LEMONCROW_INSTALL_DIR/scripts/local_server.sh" stop >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap release_drill_cleanup EXIT
# An interrupted drill still stops its server and removes WORK (the EXIT trap), then ends.
trap 'exit 130' INT
trap 'exit 143' TERM

export HOME="$WORK/home"
export LEMONCROW_INSTALL_DIR="$WORK/install"
export LEMONCROW_BIN_DIR="$WORK/bin"
export LEMONCROW_NON_INTERACTIVE=1
export LEMONCROW_NO_PATH=1
export LEMONCROW_NO_HOSTS=1
export LEMONCROW_NO_STACK=1
# The package cache is content-addressed and holds no product state: keep the host's, so the drill
# does not download every wheel again. Resolved before XDG_CACHE_HOME moves.
if command -v uv >/dev/null 2>&1; then
  UV_CACHE_DIR="${UV_CACHE_DIR:-$(uv cache dir)}"
  export UV_CACHE_DIR
fi
# An isolated HOME is not enough: the XDG base directories still name the developer's own
# (telemetry id, host configs), so every one moves under the drill too.
export XDG_CONFIG_HOME="$WORK/xdg/config"
export XDG_CACHE_HOME="$WORK/xdg/cache"
export XDG_DATA_HOME="$WORK/xdg/data"
export XDG_STATE_HOME="$WORK/xdg/state"
mkdir -p "$HOME" "$LEMONCROW_BIN_DIR" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME"
# An isolated HOME does not isolate the host's service managers: `systemctl --user` reaches the
# login's real user manager, so an installer's restart restarted the developer's own
# lemoncrow-local-server.service. Without the session bus and runtime directory nothing can find
# that manager; no installer here can run one either, and the server runs process-supervised on a
# private port instead of the host's 7420. (The product reads no LEMONCROW_NO_SERVICECTL.)
unset DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR
NO_SERVICE_MANAGER="$WORK/no-service-manager"
mkdir -p "$NO_SERVICE_MANAGER"
for tool in systemctl systemd-run launchctl loginctl; do
  printf '#!/bin/sh\necho "%s: the release upgrade drill never reaches the host service manager" >&2\nexit 1\n' \
    "$tool" >"$NO_SERVICE_MANAGER/$tool"
  chmod +x "$NO_SERVICE_MANAGER/$tool"
done
export PATH="$NO_SERVICE_MANAGER:$PATH"
export LEMONCROW_LOCAL_SERVER_SUPERVISOR=none
LEMONCROW_LOCAL_SERVER_PORT="$(
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
)"
export LEMONCROW_LOCAL_SERVER_PORT
# Each `lc review` talks to this drill's server, never one the host already runs. Only the client
# gets it: an installer may read LEMONCROW_URL as a hosted endpoint.
# shellcheck disable=SC2034  # read by the sourcing drill
DRILL_URL="http://127.0.0.1:${LEMONCROW_LOCAL_SERVER_PORT}"
# Installers initialize the project they run in (git hooks, jj, AGENTS.md). Run them in the
# scratch directory, never in the checkout the drill was started from: a release worktree
# shares its hooks with the developer repository.
cd "$WORK" || exit 1
