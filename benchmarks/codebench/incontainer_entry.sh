#!/usr/bin/env bash
# In-container entrypoint for CodeBench Multi-SWE-bench runs (option A).
#
# Bind-mounted read-only at /mnt/run.sh. The agent edits the repo in place; the
# resulting git diff is emitted between the DIFF markers for the host to
# capture. claude's --output-format json receipt is printed before the markers.
#
# Inputs (env): CODEBENCH_ARM (baseline|lemoncrow), CODEBENCH_MODEL,
# CODEBENCH_MAX_TURNS, CODEBENCH_AGENT (optional persona), CODEBENCH_REPO_DIR
# (optional; auto-discovered from the first .git dir otherwise).
set -uo pipefail

REPO="${CODEBENCH_REPO_DIR:-}"
if [ -z "$REPO" ]; then
  g="$(find / -maxdepth 6 -type d -name .git 2>/dev/null | head -1)"
  REPO="$(dirname "$g")"
fi
cd "$REPO" || { echo "no repo dir found" >&2; exit 3; }

# SWE-bench Pro instances (CODEBENCH_BEFORE_REPO_SET_CMD, unset otherwise): reset
# to base_commit and check out the one gold test file the task's fail_to_pass
# exercises. The harness re-applies this same command after grading the
# candidate patch (overwriting any test-file edits back to the gold version),
# so this is not optional setup -- it's the task's actual starting state.
# Runs for BOTH arms, before any arm-specific step, so neither is graded
# against a repo the other didn't also see.
if [ -n "${CODEBENCH_BEFORE_REPO_SET_CMD:-}" ]; then
  if ! bash -c "$CODEBENCH_BEFORE_REPO_SET_CMD"; then
    echo "FATAL: before_repo_set_cmd failed for $REPO -- aborting so the run is retried instead of grading against a bad repo state." >&2
    exit 5
  fi
fi

# Pin the workspace root so the live MCP server's code-index repo_id is
# deterministic and matches the index pre-built below. Without this the MCP
# server resolves its root from CLAUDE_WORKSPACE_ROOT/cwd, which can differ from
# the prewarm's --repo-root -> a different repo_id -> the MCP reads an EMPTY
# index -> grep returns nothing -> the agent wastes turns over-searching.
export LEMONCROW_WORKSPACE_ROOT="$REPO"

# Activate the project's conda env (SWE-bench images ship a `testbed` env) for
# BOTH arms so shells run the project interpreter. Claude Code's Bash sources
# .bashrc (env active); the LemonCrow shell tool's `bash -c` subprocesses do NOT
# source .bashrc -- but non-interactive bash reads $BASH_ENV before running a
# -c command. So we (a) activate in this entry shell, which `claude` and the
# LemonCrow MCP server inherit, and (b) point $BASH_ENV at an activation snippet
# so every LemonCrow `bash -c` re-activates even if inheritance is lost. Without
# this the LemonCrow arm burns turns rediscovering the interpreter. Identical for
# both arms keeps the comparison fair and matches production, where claude is
# launched from the user's already-activated shell.
_act=/tmp/codebench_activate.sh
: >"$_act"
for _cs in /opt/miniconda3/etc/profile.d/conda.sh /opt/conda/etc/profile.d/conda.sh; do
  if [ -f "$_cs" ]; then
    # Snippet is idempotent and cheap: re-activation is skipped once active.
    printf '[ "$CONDA_DEFAULT_ENV" = testbed ] || { . %s; conda activate testbed 2>/dev/null || true; }\n' "$_cs" >"$_act"
    . "$_cs"; conda activate testbed 2>/dev/null || true
    break
  fi
done
export BASH_ENV="$_act"

ARM="${CODEBENCH_ARM:-baseline}"
MODEL="${CODEBENCH_MODEL:-opus}"
DRIVER="${CODEBENCH_DRIVER:-claude}"
# The lemoncode/lemoncrow arm makes its provider calls from Python (the gateway),
# not from node, so NODE_EXTRA_CA_CERTS alone leaves httpx/requests unable to
# verify the hermetic mitmproxy's certificate -- every upstream call would fail
# TLS and the agent would finish with an empty patch.
if [ -f /mnt/mitm.pem ]; then
  export SSL_CERT_FILE=/mnt/mitm.pem
  export REQUESTS_CA_BUNDLE=/mnt/mitm.pem
fi
# LemonCrow MCP host: claude|cursor (which adapter the lc server speaks). Both
# drivers share the lc index/prewarm below; only the agent CLI + MCP wiring differ.
HOST="${CODEBENCH_HOST:-claude}"

if [ "$DRIVER" = "claude" ]; then
  export CLAUDE_CONFIG_DIR=/tmp/codebench-claude-config
  mkdir -p "$CLAUDE_CONFIG_DIR"
  if [ "$ARM" = "lemoncrow" ]; then
    printf '%s' '{"mcpServers":{"lc":{"type":"stdio","command":"lemoncrow","args":["mcp","--host","claude"],"alwaysLoad":true}}}' \
      >"$CLAUDE_CONFIG_DIR/.claude.json"
  else
    printf '%s' '{}' >"$CLAUDE_CONFIG_DIR/.claude.json"
  fi
elif [ "$DRIVER" = "cursor" ]; then
  # cursor-agent reads auth from ~/.config/cursor/auth.json. Copy the mounted
  # credential into a WRITABLE location so an access-token refresh mid-run can
  # write back without failing on the read-only bind mount.
  mkdir -p "$HOME/.config/cursor"
  if [ -f /mnt/cursor-auth.json ]; then
    cp /mnt/cursor-auth.json "$HOME/.config/cursor/auth.json"
  else
    echo "FATAL: cursor driver but /mnt/cursor-auth.json not mounted (run 'cursor-agent login' on the host)" >&2
    exit 8
  fi
fi

if [ "$ARM" = "lemoncrow" ]; then
  # The MCP process is a thin client now, so the benchmark must provide the
  # server it connects to. Keep both client and server state inside this Docker
  # container: no host server, host token, view quota, or developer session can
  # influence a SWE run.
  export LEMONCROW_HOME=/tmp/codebench-lemoncrow-client
  export LEMONCROW_URL=http://127.0.0.1:7420
  export LEMONCROW_TOKEN_FILE=/tmp/codebench-lemoncrow-token
  export LEMONCROW_LOCAL_FS=1
  # The benchmark must not fall into degraded local-tool mode merely because a
  # fresh container needs more than the interactive one-second bootstrap budget
  # to create and index its first server-backed View.
  export LEMONCROW_STARTUP_BUDGET_S=300
  export LEMONCROW_REQUEST_TIMEOUT_S=300
  export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="$NO_PROXY"
  mkdir -p "$LEMONCROW_HOME" /tmp/codebench-lemoncrow-server
  printf '%s\n' 'lc_codebench_isolated_token_2026' >"$LEMONCROW_TOKEN_FILE"
  chmod 600 "$LEMONCROW_TOKEN_FILE"

  # Initialize the isolated client home without indexing or interactive model
  # configuration. Run from $HOME, not $REPO, so init cannot scaffold files
  # into the task checkout. Authentication is no longer part of `lc init`.
  (cd "$HOME" && lemoncrow init --no-index --no-configure-models >/tmp/lemoncrow-init.log 2>&1) \
    || { echo "FATAL: lemoncrow init failed" >&2; tail -n 20 /tmp/lemoncrow-init.log >&2 || true; exit 7; }

  lemoncrow-server up \
    --ephemeral \
    --directory /tmp/codebench-lemoncrow-server \
    --port 7420 \
    --token-file "$LEMONCROW_TOKEN_FILE" \
    --allow-local-fs \
    >/tmp/lemoncrow-server.log 2>&1 &
  LEMONCROW_SERVER_PID=$!
  _stop_lemoncrow_server() {
    kill "$LEMONCROW_SERVER_PID" 2>/dev/null || true
    wait "$LEMONCROW_SERVER_PID" 2>/dev/null || true
  }
  trap _stop_lemoncrow_server EXIT INT TERM

  server_ready=0
  for _attempt in $(seq 1 100); do
    if curl -fsS "$LEMONCROW_URL/healthz" >/dev/null 2>&1; then
      server_ready=1
      break
    fi
    if ! kill -0 "$LEMONCROW_SERVER_PID" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  if [ "$server_ready" -ne 1 ]; then
    echo "FATAL: isolated LemonCrow server did not become healthy" >&2
    tail -n 40 /tmp/lemoncrow-server.log >&2 || true
    exit 4
  fi
  echo "lemoncrow server: isolated loopback ready at $LEMONCROW_URL" >&2

  # `lemoncrow code index` is a legacy local SQLite index and is no longer the
  # store read by the thin client/server architecture. The MCP preflight below
  # opens and syncs a real server View and executes code_search, which both warms
  # and validates the exact path the agent will use.

  # Prewarm Zoekt trigram index -- only when LEMONCROW_ZOEKT_MODE opts in (defaults
  # to "off": lexical/FTS5-only, matching the host default -- see zoekt_mode() in
  # src/lemoncrow/infra/code_intel/zoekt/binary.py). should_route() short-circuits
  # before ever touching zoekt when mode=="off", so the overlay doesn't even
  # install the binaries in that case (see ensure_overlay() in incontainer.py);
  # this check just keeps the two in sync.
  # Zoekt's host-binary mode builds its index synchronously on first
  # ensure_started(); we trigger it here so the MCP server's first search
  # call returns results immediately instead of paying the build cost on the
  # tool-call path. Failure is non-fatal: search falls back to SQLite FTS5.
  if [ "${LEMONCROW_ZOEKT_MODE:-off}" != "off" ] && command -v zoekt-index >/dev/null 2>&1; then
    lemoncrow zoekt up 2>/tmp/lemoncrow-zoekt.log \
      && echo "lemoncrow zoekt up: prewarm OK" >&2 \
      || { echo "[warn] lemoncrow zoekt up failed -- falling back to FTS5" >&2; tail -n 5 /tmp/lemoncrow-zoekt.log >&2 || true; }
  fi

  if ! lemoncrow mcp --host "$HOST" check --timeout 300 >/tmp/lemoncrow-mcp-check.log 2>&1; then
    echo "FATAL: LemonCrow MCP failed initialize/tools-list preflight; aborting before Claude starts." >&2
    tail -n 20 /tmp/lemoncrow-mcp-check.log >&2 || true
    exit 6
  fi
  echo "lemoncrow MCP: preflight OK" >&2
fi

prompt="$(cat /mnt/prompt.txt)"
if [ "$DRIVER" = "cursor" ]; then
  # Soft hermetic nudge (WebSearch/WebFetch are server-side — hooks cannot block
  # them). Still cuts some gold-PR lookups when the model complies.
  prompt="HERMETIC EVAL: do not use WebSearch or WebFetch (treat as unavailable). Solve only from this repo checkout.

${prompt}"
fi

if [ "$DRIVER" = "lemoncode" ]; then
  # Stock OpenCode (baseline) vs the LemonCode fork driven by LemonCrow. Both
  # arms run the same Zen model, so the only variable is LemonCrow itself.
  LC_MODEL="${CODEBENCH_LEMONCODE_MODEL:-big-pickle}"
  # Cold container: index prewarm + MCP boot can exceed the 30s default.
  export LEMONCROW_CODE_GATEWAY_START_TIMEOUT="${LEMONCROW_CODE_GATEWAY_START_TIMEOUT:-180}"
  if [ "$ARM" = "lemoncrow" ]; then
    # LemonCrow owns routing, tools, cache and output limits; it starts its own
    # gateway plus the managed LemonCode host (mounted at
    # /root/.lemoncrow/bin/lemoncode-host).
    (cd "$REPO" && lemoncrow code --engine lemoncode --project-root "$REPO" --model "zen/$LC_MODEL" --optimization-mode enforce -p "$prompt") 2>&1 | tee /tmp/lemoncode-arm.log
  else
    # Unmodified upstream OpenCode against Zen's keyless public tier.
    export OPENCODE_DISABLE_AUTOUPDATE=true
    (cd "$REPO" && opencode run --model "opencode/$LC_MODEL" "$prompt")
  fi
  rm -rf "$REPO/.opencode"
elif [ "$DRIVER" = "cursor" ]; then
  # stream-json: every tool_call event on stdout so the host can attribute
  # native vs MCP routing (plain json only emits the final result envelope).
  cargs=(-p "$prompt" --force --output-format stream-json)
  [ -n "${CODEBENCH_CURSOR_MODEL:-}" ] && cargs+=(--model "$CODEBENCH_CURSOR_MODEL")
  if [ "$ARM" = "lemoncrow" ]; then
    # Register the lc MCP server + always-on rule + hard-enforce hooks.
    # cursor-agent CLI loads user hooks from ~/.cursor/hooks.json (workspace
    # hooks alone are not enough in headless -p). Absolute paths required.
    # .cursor/ and .lemoncrow/ are stripped before the diff capture.
    mkdir -p "$REPO/.cursor/rules" "$REPO/.cursor/hooks" "$HOME/.cursor" "$HOME/.lemoncrow/cursor-hooks"
    printf '%s' '{"mcpServers":{"lemoncrow":{"type":"stdio","command":"lemoncrow","args":["mcp","--host","cursor"]}}}' \
      >"$REPO/.cursor/mcp.json"
    [ -f /mnt/cursor-rule.mdc ] && cp /mnt/cursor-rule.mdc "$REPO/.cursor/rules/lemoncrow.code.mdc"
    if [ -d /mnt/cursor-hooks ]; then
      cp /mnt/cursor-hooks/*.py "$REPO/.cursor/hooks/" 2>/dev/null || true
      cp /mnt/cursor-hooks/*.py "$HOME/.lemoncrow/cursor-hooks/" 2>/dev/null || true
      _write_hooks_json() {
        # $1 = hooks dir (absolute), $2 = output hooks.json path
        # MUST use /usr/bin/python3: conda testbed's python3 is 3.6 on many
        # SWE images, which cannot parse `from __future__ import annotations`
        # — failClosed then blocks LemonCrow MCP itself.
        local _hd="$1" _out="$2" _py="/usr/bin/python3"
        cat >"$_out" <<EOF
{
  "version": 1,
  "hooks": {
    "sessionStart": [{"command": "${_py} ${_hd}/session_start.py"}],
    "stop": [{"command": "${_py} ${_hd}/stop.py"}],
    "beforeShellExecution": [{"command": "${_py} ${_hd}/before_shell_execution.py", "failClosed": true}],
    "beforeReadFile": [{"command": "${_py} ${_hd}/before_read_file.py", "failClosed": true}],
    "preToolUse": [{"command": "${_py} ${_hd}/before_tool_use.py", "failClosed": true}]
  }
}
EOF
      }
      _write_hooks_json "$REPO/.cursor/hooks" "$REPO/.cursor/hooks.json"
      _write_hooks_json "$HOME/.lemoncrow/cursor-hooks" "$HOME/.cursor/hooks.json"
    fi
    (cd "$REPO" && cursor-agent mcp enable lemoncrow >/tmp/cursor-mcp-enable.log 2>&1) || true
  else
    # Baseline: hermetic web deny only (natives allowed). Same /usr/bin/python3 pin.
    if [ -f /mnt/cursor-deny-web.py ]; then
      mkdir -p "$HOME/.cursor" "$HOME/.lemoncrow/cursor-hooks"
      cp /mnt/cursor-deny-web.py "$HOME/.lemoncrow/cursor-hooks/before_tool_use.py"
      cat >"$HOME/.cursor/hooks.json" <<EOF
{
  "version": 1,
  "hooks": {
    "preToolUse": [{"command": "/usr/bin/python3 $HOME/.lemoncrow/cursor-hooks/before_tool_use.py", "failClosed": true}]
  }
}
EOF
    fi
  fi
  (cd "$REPO" && cursor-agent "${cargs[@]}")
  # Strip injected config + index artifacts so they never land in the patch.
  rm -rf "$REPO/.cursor" "$REPO/.lemoncrow"
else
args=(-p "$prompt" --model "$MODEL" --output-format json --permission-mode bypassPermissions)
[ -n "${CODEBENCH_MAX_TURNS:-}" ] && args+=(--max-turns "$CODEBENCH_MAX_TURNS")
if [ "$ARM" = "lemoncrow" ]; then
  # Isolated user config supplies exactly one non-plugin server. alwaysLoad=true
  # blocks headless turn 1 until lc connects and exposes mcp__lc__* schemas.
  args+=(--plugin-dir /mnt/plugin)
else
  args+=(--mcp-config '{"mcpServers":{}}' --strict-mcp-config)
fi
[ -n "${CODEBENCH_AGENT:-}" ] && args+=(--agent "$CODEBENCH_AGENT")

# Hermetic benchmark, BOTH arms: deny web + agent-orchestration tools at the CLI,
# independent of persona config. WebSearch/WebFetch are server-side (run on
# Anthropic's infra), so the mitmproxy egress guard never sees them -- they are
# the answer-fetch contamination vector that an autonomous agent will otherwise
# use to pull a SWE-bench task's public gold PR. Denying here also removes the
# tool schemas from every request (token trim). Based on the harbor list
# (benchmarks/harbor/lemoncrow_agent.py::_DISALLOWED_TOOLS), plus EnterPlanMode --
# a real tool as of claude 2.1.185, so denying ExitPlanMode alone still lets the
# agent enter plan mode and plan instead of execute. The flag is variadic, so it
# MUST stay last in args. Denying a tool an arm doesn't have is a harmless no-op
# (e.g. baseline has no mcp__* tools).
disallowed=(AskUserQuestion EnterPlanMode ExitPlanMode WebFetch WebSearch mcp__lc__web_fetch mcp__plugin_lemoncrow_lc__web_fetch Workflow ScheduleWakeup)
args+=(--disallowedTools "${disallowed[@]}")

claude "${args[@]}"
fi

# Drop agent-created lockfile noise (MCP bash `uv` often writes an untracked
# uv.lock that bloats the patch ~75KB and was the flask pollution vector).
if git -C "$REPO" cat-file -e HEAD:uv.lock 2>/dev/null; then
  git -C "$REPO" checkout -- uv.lock 2>/dev/null || true
else
  rm -f "$REPO/uv.lock"
fi

echo "<<<CODEBENCH_DIFF_BEGIN>>>"
git -C "$REPO" add -A 2>/dev/null
git -C "$REPO" diff --cached HEAD
echo "<<<CODEBENCH_DIFF_END>>>"
