#!/usr/bin/env bash
# Zero-LLM setup preflight (bundle variant). Replicates the agent install using
# the prebuilt LemonCrow bundle and verifies claude + lc AS ROOT -- the agent
# now runs as root with IS_SANDBOX=1 (matches the verifier's user). NEVER invokes
# the agent/LLM -> zero AI credits.
#   docker run --rm -v <repo>:/lemoncrow:ro -v <bundle>:/lemoncrow-bundle.tar.gz:ro <IMAGE> \
#       bash /lemoncrow/benchmarks/harbor/setup_preflight.sh <LABEL>
set +e
LABEL="${1:-image}"
fail(){ echo "RESULT:$LABEL:FAIL:$1"; exit 1; }

if grep -q '^VERSION_CODENAME=bullseye$' /etc/os-release 2>/dev/null; then
  cat >/etc/apt/sources.list <<'EOF'
deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260831T235959Z/ bullseye main
deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260831T235959Z/ bullseye-security main
EOF
  rm -rf /etc/apt/sources.list.d/* /var/lib/apt/lists/*
fi
i=0; while :; do apt-get -o Acquire::Check-Valid-Until=false update -qq && apt-get install -y -qq git curl ca-certificates gnupg && break; i=$((i+1)); [ $i -ge 3 ] && fail apt; sleep 3; done

i=0; while :; do curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y -qq nodejs && break; i=$((i+1)); [ $i -ge 3 ] && fail node; sleep 3; done
node -v | grep -qE 'v(1[89]|[2-9][0-9])' || fail "node_$(node -v 2>&1)"

tar -C /opt -xzf /lemoncrow-bundle.tar.gz || fail bundle_extract
chmod -R a+rX /opt/lemoncrow-venv /opt/headroom-venv /opt/uvpy
ln -sf /opt/lemoncrow-venv/bin/lemoncrow /usr/local/bin/lemoncrow
/opt/lemoncrow-venv/bin/python -c 'import lemoncrow, lemoncrow_client' || fail import_lemoncrow_runtime
/opt/headroom-venv/bin/python -c 'import headroom' || fail import_headroom

i=0; while :; do npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 && break; i=$((i+1)); [ $i -ge 3 ] && fail npm_claude; sleep 3; done
command -v claude >/dev/null || fail claude_bin

# Zero-provider-call Headroom proxy health check. The benchmark's Headroom arm
# points Claude at this local Anthropic-compatible proxy, so prove the isolated
# venv can actually start on the task image before spending any model credits.
HEADROOM_CCR_BACKEND=memory /opt/headroom-venv/bin/headroom proxy \
  --host 127.0.0.1 --port 18787 --mode cache --no-cache --no-rate-limit --stateless \
  >/tmp/headroom-preflight.log 2>&1 &
HRPID=$!
HRREADY=0
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:18787/health >/dev/null 2>&1 && { HRREADY=1; break; }
  sleep 0.25
done
kill "$HRPID" 2>/dev/null || true
wait "$HRPID" 2>/dev/null || true
[ "$HRREADY" -eq 1 ] || fail "headroom_proxy:$(tail -c 200 /tmp/headroom-preflight.log)"

# Optional: rtk external compactor (github.com/rtk-ai/rtk). LemonCrow's bash tool
# soft-detects it on PATH at run time (external_compactors.py); absence is
# never a failure there (`lc doctor` reports it as optional), so it must
# not be one here either -- record status in the final RESULT line, never fail.
RTK_STATUS=missing
i=0; while [ $i -lt 3 ]; do
  curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh \
    | RTK_INSTALL_DIR=/usr/local/bin sh >/tmp/rtk-install.log 2>&1 && break
  i=$((i+1)); sleep 3
done
command -v rtk >/dev/null 2>&1 && RTK_STATUS="ok:$(rtk --version 2>&1 | head -c 40)"

export LEMONCROW_ROOT=/root/.lemoncrow
cd /root && /opt/lemoncrow-venv/bin/lemoncrow init --no-login >/dev/null 2>&1 || fail lemoncrow_init

# Static plugin/agent check: the bench loads --plugin-dir + --agent lemoncrow:solve
# (the lean plugin prunes every agents/*.md except solve.md -- see lemoncrow_agent.py install()).
test -f /lemoncrow/integrations/claude/plugin/agents/solve.md || fail solve_agent_missing
grep -q 'name: solve' /lemoncrow/integrations/claude/plugin/agents/solve.md || fail solve_agent_name

# Zero-credit command probe: parse the REAL claude flags (model + effort high +
# stream-json/verbose + bypassPermissions + disallowedTools) AS ROOT with an
# EMPTY token. claude emits a stream-json `init` line then a SYNTHETIC reply
# (apiKeySource=none -> no API call -> no credit). Assert: (a) it started (init
# line present), (b) the root guard did NOT fire, (c) the disallowed tools are
# actually absent from the advertised tool set. The plugin+MCP path itself is
# exercised by the real run (rep1 already proved it loads in-container).
mkdir -p /tmp/cfgprobe && echo '{}' > /tmp/cfgprobe/.claude.json
PROBE=$(IS_SANDBOX=1 CLAUDE_CONFIG_DIR=/tmp/cfgprobe CLAUDE_CODE_OAUTH_TOKEN= timeout 40 claude -p noop \
  --model "${LEMONCROW_BENCH_MODEL:-claude-opus-4-8}" --effort high \
  --output-format stream-json --verbose \
  --permission-mode bypassPermissions \
  --disallowedTools AskUserQuestion ExitPlanMode WebFetch WebSearch mcp__lc__web_fetch Workflow ScheduleWakeup \
  < /dev/null 2>&1 | head -c 4000)
echo "$PROBE" | grep -qi 'root/sudo privileges' && fail cmdprobe_root_guard
echo "$PROBE" | grep -q '"subtype":"init"' || fail "cmdprobe_no_init:$(printf '%s' "$PROBE" | tr '\n' ' ' | head -c 160)"
echo "$PROBE" | grep -qE '"(AskUserQuestion|WebFetch|WebSearch|Workflow|ScheduleWakeup)"' && fail cmdprobe_disallow_not_applied
CMDPROBE=ok

# Prewarm path (the run-time `lc code index` step). Exercises tree-sitter
# native parsing on this image's glibc. The FTS index grep reads must build for
# (a) a git repo, (b) a NON-git dir with files (many TB workdirs are not git),
# and (c) an empty dir must not abort (exit 0). On a non-git dir the git-history
# pass logs a *caught* GitError (exit stays 0) -- benign -- so we gate on exit
# code and files_indexed, never on the presence of a traceback string.
idx_files(){ /opt/lemoncrow-venv/bin/python -c "import json,sys; print(json.load(open(sys.argv[1])).get('files_indexed',-1))" "$1" 2>/dev/null; }

# (a) git repo -> indexes, exit 0
IDXG=/tmp/idxgit
mkdir -p "$IDXG"
printf 'def alpha():\n    return 1\n' > "$IDXG/a.py"
printf 'from a import alpha\ndef beta():\n    return alpha()\n' > "$IDXG/b.py"
(cd "$IDXG" && git init -q && git config user.email b@b && git config user.name b && git add -A && git commit -qm init) >/dev/null 2>&1
(cd "$IDXG" && /opt/lemoncrow-venv/bin/lemoncrow code index --reindex --json) >/tmp/idxg.json 2>/tmp/idxg.err || fail "code_index_git:$(tail -c 200 /tmp/idxg.err)"
[ "$(idx_files /tmp/idxg.json)" -ge 1 ] 2>/dev/null || fail "index_git_zero:$(head -c 200 /tmp/idxg.json)"

# (b) NON-git dir with files -> explicit workspace registration first, then
# indexing. New workspace isolation deliberately rejects arbitrary non-git cwd
# until `lc init` creates the local marker; Harbor mirrors that production rule.
IDXN=/tmp/idxnogit
mkdir -p "$IDXN"
printf 'def gamma():\n    return 2\n' > "$IDXN/c.py"
(cd "$IDXN" && LEMONCROW_ROOT=/root/.lemoncrow /opt/lemoncrow-venv/bin/lemoncrow init --no-login) >/tmp/idxn-init.log 2>&1 \
  || fail "init_nogit:$(tail -c 200 /tmp/idxn-init.log)"
[ -d "$IDXN/.lemoncrow" ] || fail init_nogit_marker_missing
(cd "$IDXN" && LEMONCROW_ROOT=/root/.lemoncrow /opt/lemoncrow-venv/bin/lemoncrow code index --reindex --json) >/tmp/idxn.json 2>/tmp/idxn.err \
  || fail "code_index_nogit:$(tail -c 200 /tmp/idxn.err)"
[ "$(idx_files /tmp/idxn.json)" -ge 1 ] 2>/dev/null || fail "index_nogit_zero:$(head -c 200 /tmp/idxn.json)"

# (c) empty non-git dir -> register first, then index must still exit 0;
# no real crash (segfault) allowed.
IDXE=/tmp/idxempty
mkdir -p "$IDXE"
(cd "$IDXE" && LEMONCROW_ROOT=/root/.lemoncrow /opt/lemoncrow-venv/bin/lemoncrow init --no-login) >/tmp/idxe-init.log 2>&1 \
  || fail "init_empty:$(tail -c 200 /tmp/idxe-init.log)"
(cd "$IDXE" && LEMONCROW_ROOT=/root/.lemoncrow /opt/lemoncrow-venv/bin/lemoncrow code index --reindex --no-stats) >/dev/null 2>/tmp/idxe.err
EMPTYRC=$?
[ "$EMPTYRC" -eq 0 ] || fail "code_index_empty_rc$EMPTYRC:$(tail -c 200 /tmp/idxe.err)"
grep -qiE 'Segmentation|core dumped' /tmp/idxe.err && fail code_index_empty_segfault

# Run-command log path. harbor creates /logs/agent (chmod 0o777) and collects it;
# the agent writes its run log + prewarm log THERE, not in /logs root. The agent
# runs as root now, so confirm both files are writable under the harbor layout.
mkdir -p /logs/agent && chmod 777 /logs/agent
bash -c 'echo "{}" >/logs/agent/claude-run.json && echo ok >/logs/agent/lemoncrow-index.log' \
  || fail logs_agent_unwritable

echo "RESULT:$LABEL:PASS node=$(node -v) cmdprobe=$CMDPROBE headroom=ok idx_git=$(idx_files /tmp/idxg.json) idx_nogit=$(idx_files /tmp/idxn.json) emptyrc=$EMPTYRC logs_agent=ok rtk=$RTK_STATUS"
