# lemoncrow-status

Sidebar panel for OpenCode showing what LemonCrow actually spent and saved on
the current run: tokens in / cached / out, context size, cache efficiency,
cost, savings, and tool + MCP call counts.

Works with stock OpenCode -- no fork required. The LemonCode fork ships the
same panel as a builtin (`packages/tui/src/feature-plugins/sidebar/lemoncrow.tsx`).

## Install

Add the plugin directory to `opencode.json` (project or `~/.config/opencode/`):

```json
{
  "plugin": ["/abs/path/to/integrations/opencode/plugins/lemoncrow-status"]
}
```

Then start OpenCode through LemonCrow so the snapshot path is exported:

```bash
lc code --engine opencode
```

## How it works

`lc code` picks one snapshot path per run and exports it as
`LEMONCROW_STATUS_FILE` to both the gateway and the frontend. The gateway
rewrites that JSON file after every turn
(`lemoncrow/core/capabilities/statusline_sidecar.py`); the panel polls it once
a second, re-parsing only when the file's mtime changed.

Without `LEMONCROW_STATUS_FILE`, or before the first turn completes, the panel
renders nothing -- it never blocks or slows the TUI.

To point it at a snapshot yourself (e.g. a long-running gateway):

```bash
LEMONCROW_STATUS_FILE=~/.lemoncrow/statusline/my-session.json opencode
```
