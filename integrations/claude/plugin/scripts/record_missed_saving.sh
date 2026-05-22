#!/usr/bin/env bash
# PostToolUse hook: silently records native Read/Bash/Grep calls as missed savings.
# Fires after the tool has already run — zero noise to the agent conversation.
#
# Called by Claude Code with the tool input piped to stdin as JSON.
# Required env:  ATELIER_ROOT  (default ~/.atelier)
# Optional env:  ATELIER_MISSED_SAVINGS_LOG (override log path)

set -u

ATELIER_ROOT="${ATELIER_ROOT:-$HOME/.atelier}"
LOG="${ATELIER_MISSED_SAVINGS_LOG:-$ATELIER_ROOT/missed_savings.jsonl}"
TOOL_NAME="${CLAUDE_TOOL_NAME:-unknown}"
SESSION_ID="${CLAUDE_SESSION_ID:-}"

# Read tool input from stdin (Claude passes it as JSON).
INPUT=$(cat 2>/dev/null || echo "{}")

# Estimate how many chars the tool returned — unavailable in PostToolUse,
# so we estimate from the input (file size for Read, command length for Bash).
CHARS_EST=0
if command -v python3 >/dev/null 2>&1; then
  CHARS_EST=$(python3 2>/dev/null -c "
import json, os, sys
data = json.loads('''$INPUT''' if '''$INPUT''' else '{}')
tool = '$TOOL_NAME'
if tool == 'Read':
    path = data.get('file_path') or data.get('path') or ''
    try:
        CHARS_EST = os.path.getsize(path) if path else 0
    except Exception:
        CHARS_EST = 0
elif tool in ('Bash', 'Grep'):
    CHARS_EST = 0   # unknown without capturing output
print(CHARS_EST)
" 2>/dev/null || echo 0)
fi

TOKENS_EST=$(( CHARS_EST / 4 ))

# Map native tool → preferred atelier tool
case "$TOOL_NAME" in
  Read)   PREFERRED="mcp__atelier__read"   ;;
  Bash)   PREFERRED="mcp__atelier__shell"  ;;
  Grep)   PREFERRED="mcp__atelier__search" ;;
  *)      PREFERRED=""                      ;;
esac

[ -z "$PREFERRED" ] && exit 0   # not a tracked tool — nothing to record

# Emit a JSONL event (silent, no stdout/stderr to agent)
mkdir -p "$(dirname "$LOG")"
python3 2>/dev/null -c "
import json, sys
from datetime import datetime, timezone
event = {
    'at': datetime.now(timezone.utc).isoformat(),
    'session_id': '$SESSION_ID',
    'tool_name': '$TOOL_NAME',
    'preferred': '$PREFERRED',
    'chars_est': $CHARS_EST,
    'tokens_est': $TOKENS_EST,
    'kind': 'missed_saving',
}
with open('$LOG', 'a', encoding='utf-8') as f:
    f.write(json.dumps(event) + '\n')
" 2>/dev/null || true

exit 0
