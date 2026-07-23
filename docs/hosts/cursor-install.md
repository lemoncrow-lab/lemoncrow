# Installing LemonCrow into Cursor (IDE + `cursor-agent` CLI)

**Support level**: MCP server (stdio JSON-RPC) + native lifecycle hooks

The Cursor IDE and the Cursor CLI agent (`cursor-agent`) share the same
`~/.cursor/mcp.json`, `~/.cursor/hooks.json`, and `.cursor/rules/` — so a single
install wires up **both**. LemonCrow treats them as one `cursor` host.

---

## Quick Install

```bash
make install            # detects cursor / cursor-agent and installs
# or, directly:
bash scripts/install_cursor.sh                      # global (~/.cursor)
bash scripts/install_cursor.sh --workspace /path    # project-local (.cursor/)
```

Detection covers the IDE launcher (`cursor`), the CLI (`cursor-agent`), and a
present `~/.cursor` dir.

---

## What Gets Installed

| Artifact          | Global install                                        | `--workspace DIR` install                           |
| ----------------- | ----------------------------------------------------- | --------------------------------------------------- |
| MCP server config | `~/.cursor/mcp.json`                                  | `<workspace>/.cursor/mcp.json`                      |
| Lifecycle hooks   | `~/.cursor/hooks.json` + `~/.lemoncrow/cursor-hooks/` | `<workspace>/.cursor/hooks.json` + `.cursor/hooks/` |
| Rules             | (none — Cursor rules are project-only)                | `<workspace>/.cursor/rules/*.mdc`                   |
| CLI approval      | `cursor-agent mcp enable lemoncrow`                   | same, run from the workspace                        |

The MCP entry (identical in every branch — server key `lemoncrow`, command `lc`):

```json
{
  "mcpServers": {
    "lemoncrow": {
      "type": "stdio",
      "command": "lc",
      "args": ["mcp", "--host", "cursor"],
      "alwaysAllow": ["bash", "code_search", "read", "edit", "context", "..."]
    }
  }
}
```

`--host cursor` tells the LemonCrow MCP server which environment it runs in so
traces and savings are labelled `cursor`.

### Lifecycle hooks

Cursor never sets a session env var for MCP subprocesses, so two hooks bridge the
gap (both loaded by the IDE and by `cursor-agent`):

- **`sessionStart`** → `session_start.py`: writes the live session id (and the
  pinned model, when not "auto") into the workspace attribution bridge. Without
  it, every MCP tool call's savings row is quarantined and the session shows
  **$0 saved** even though cost is correct.
- **`stop`** → `stop.py`: refreshes attribution and logs a savings recap. Cursor's
  `stop` output supports only `followup_message`, so the recap is a diagnostic
  breadcrumb — the user-facing savings surface is `lc savings` / `lc dashboard`.

### `cursor-agent` CLI approval

`cursor-agent` only loads an MCP server once it's on the per-workspace approved
list, so the installer runs `cursor-agent mcp enable lemoncrow` (from the
workspace in `--workspace` mode). Confirm with:

```bash
cursor-agent mcp list                       # → lemoncrow: ready
cursor-agent mcp list-tools lemoncrow       # → the lc tool set
```

---

## Verify

```bash
bash scripts/verify_cursor.sh                     # global
bash scripts/verify_cursor.sh --workspace /path   # project-local
```

Checks the MCP config, both hooks, the rules (workspace), `lc` on PATH, and that
`cursor-agent` lists the `lemoncrow` server. `make verify` runs it automatically.

### Real end-to-end smoke test

```bash
bash scripts/smoke_cursor.sh                       # install + verify + tool-load
LEMONCROW_CURSOR_SMOKE_RUN=1 bash scripts/smoke_cursor.sh   # + a real cursor-agent turn
```

Runs entirely in a throwaway temp workspace (your real `~/.cursor` is untouched)
and proves `cursor-agent` loads the lc MCP server, enumerates its tools, and —
with `LEMONCROW_CURSOR_SMOKE_RUN=1` — records an **attributed** cursor savings row.

---

## Cost savings vs. plain Cursor

Every LemonCrow MCP tool call inside Cursor records a savings row
(`sessions/<date>/cursor/<id>/savings.jsonl`), aggregated by:

```bash
lc savings          # headline $ saved / tokens / calls avoided (all hosts)
lc dashboard        # per-host breakdown, spend, 1D/7D/30D windows
```

Cursor sessions attribute via the `sessionStart` bridge, so they surface here
alongside (and separately from) other hosts.

**Honest accounting note.** Cursor is a flat-rate subscription product and does
**not** expose per-turn token usage to hooks or the local transcript under
privacy/ghost mode. So:

- **Token-saved** savings (context LemonCrow kept out of the model) are priced
  and shown. When you pin a concrete model, they price at that model; under
  "auto" they price at a conservative Sonnet default.
- The **avoided-round-trip** dollar component depends on live context size, which
  Cursor doesn't surface under privacy mode, so it stays conservative ($0) rather
  than being synthesized. Pin a model / disable privacy mode for fuller accounting.

---

## Session import

`lc import` (or `lc import --host cursor`) ingests **both**:

- Cursor **IDE** Composer sessions (`~/.config/Cursor/.../state.vscdb`).
- `cursor-agent` **CLI** conversations (`~/.config/cursor/chats/*/*/store.db`).

CLI conversations import content only (no token/cost data — Cursor doesn't persist
it), so they price at $0; ghost/empty sessions are skipped.

---

## Troubleshooting

| Problem                           | Fix                                                                       |
| --------------------------------- | ------------------------------------------------------------------------- |
| `lc: command not found`           | `pip install lemoncrow` or reinstall via `make install`                   |
| MCP tools not showing (IDE)       | Restart Cursor (Cmd+Shift+P → "Developer: Reload Window")                 |
| `cursor-agent`: server not loaded | `cursor-agent mcp enable lemoncrow` (run from the workspace)              |
| Session shows `Saved $0`          | Ensure the `sessionStart` hook is in `hooks.json` and MCP cwd = workspace |

---

## Uninstall

```bash
lc uninstall
# or: bash scripts/uninstall_cursor.sh [--workspace DIR]
```

Removes the `lemoncrow` MCP entry, both hooks, staged hook scripts, and rules.
