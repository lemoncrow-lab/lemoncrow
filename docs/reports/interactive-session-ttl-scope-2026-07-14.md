# Scope: interactive bash sessions with idle-TTL

Goal: one long-lived interpreter (e.g. `python` with `mujoco` loaded) reused across `bash` tool calls — kills the repeated heavy-import tax of one-shot scripts. Session auto-dies after an idle TTL.

## API (tool surface)

```
bash(command="uv run python -u -i -q", interactive=true, idle_ttl=300)
  → {status: "running", id: X}                # opens session
bash(id=X, action="send", input="import mujoco; m=...")
  → delta output since last send              # resets idle clock
bash(id=X, action="status" | "cancel" | "update")   # existing verbs reuse
```

Schema (`BASH_TOOL_INPUT_SCHEMA`, mcp_server.py:11179): `action` enum += `send`; new props `interactive: bool`, `input: string`, `idle_ttl: int` (default 300, capped by `_MAX_EXPLICIT_TIMEOUT_S`).

## bash_exec.py changes

1. **stdin pipe** — `start_managed_command(..., interactive=True)`: `stdin=subprocess.PIPE` instead of `DEVNULL` (bash_exec.py:2449). The DEVNULL rationale (child inheriting the JSON-RPC pipe) doesn't apply — interactive mode owns a private pipe.
2. **`_ManagedCommand` fields** — `interactive: bool`, `idle_ttl: float`, `last_input: float`, per-stream `read_offset: int` (delta framing).
3. **`send_managed_input(session_id, text)`** — write `text + "\n"` to stdin; wait for output quiescence (no growth for ~200ms) or soft budget; return only bytes past `read_offset` (via existing `output_lock` + spool files); bump `last_input`. Quiet-period framing is REPL-agnostic (python/node/psql) — no sentinel injection needed; slow computations return `still_running` and are re-readable via `action=status`.
4. **Idle-TTL** — `_effective_deadline_s` (bash_exec.py:2286) gains a third mode: interactive → kill when `now - last_input > idle_ttl`. Watcher already re-reads the deadline live in 1s slices (bash_exec.py:2303) → drop-in. State on expiry: `timed_out`, stderr `"Session idle-expired after {idle_ttl}s"`.
5. **Lifecycle** — interactive sessions are MCP-owned (never `explicit_background`); `cleanup_managed_commands` terminates them at shutdown unchanged. `bg=true + interactive=true` rejected (stdin pipe dies with the MCP process anyway).
6. **Policy** — `classify_command` runs on the opening command as today. Sent `input` also passes through `classify_command` (cheap) to close the bash-inside-interactive escape hatch.
7. **Compaction** — reuse `_strip_ansi` + per-send `max_lines` cap on the delta.

## Caveats (v1: plain pipes, no pty)

- Child must be line-buffered: recommend `python -u -i -q`; python prompts (`>>>`) go to stderr — returned in the stderr field, harmless.
- REPLs that hard-require a tty get a pty option later, not v1.
- A syntax error doesn't kill the session; caller sees it in the delta.

## Tests

- Two sends share state (`x=1` then `print(x)` → `1`) — proves persistence.
- `idle_ttl=1` → dead within ~2s, state `timed_out`, process gone.
- A send resets the idle clock.
- Second send returns only new output (offset framing).
- MCP shutdown kills interactive sessions.
- `action=update` interplay: explicit deadline still wins over idle-TTL.

## Size

~250 lines bash_exec.py, ~40 mcp_server.py, ~6 tests. No new deps.
