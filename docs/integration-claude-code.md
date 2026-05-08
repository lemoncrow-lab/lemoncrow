# Claude Code integration

Claude Code supports MCP servers + lifecycle hooks (`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`). Wire the runtime into all four.

## 1. MCP server

`~/.config/claude-code/mcp.json` (or repo-local equivalent):

```json
&#123;
  "mcpServers": &#123;
    "atelier": &#123;
      "command": "uv",
      "args": ["run", "atelier-mcp"],
      "cwd": "/abs/path/to/repo/atelier",
      "env": &#123; "ATELIER_ROOT": "~/.atelier", "ATELIER_WORKSPACE_ROOT": "." &#125;
    &#125;
  &#125;
&#125;
```

## 2. Hooks

Save as `.claude/hooks.json` (or whatever your Claude Code version expects):

```json
&#123;
  "SessionStart": &#123;
    "command": "uv",
    "args": [
      "run",
      "atelier",
      "context",
      "--task",
      "$&#123;SESSION_TITLE:-repository session&#125;",
      "--domain",
      "coding"
    ],
    "cwd": "/abs/path/to/repo/atelier",
    "inject_as": "system_message"
  &#125;,
  "PreToolUse": &#123;
    "match": &#123; "tool": "Edit" &#125;,
    "command": "uv",
    "args": [
      "run",
      "atelier",
      "check-plan",
      "--task",
      "$&#123;SESSION_TITLE:-edit&#125;",
      "--step",
      "$&#123;TOOL_INPUT_DESCRIPTION&#125;",
      "--file",
      "$&#123;TOOL_INPUT_FILE_PATH&#125;",
      "--json"
    ],
    "cwd": "/abs/path/to/repo/atelier",
    "block_on_exit_code": 2
  &#125;,
  "PostToolUse": &#123;
    "match": &#123; "tool": "Bash" &#125;,
    "when": &#123; "exit_code": "non_zero" &#125;,
    "command": "uv",
    "args": [
      "run",
      "atelier",
      "rescue",
      "--task",
      "$&#123;SESSION_TITLE:-debug&#125;",
      "--error",
      "$&#123;TOOL_OUTPUT_LAST_LINE&#125;",
      "--json"
    ],
    "cwd": "/abs/path/to/repo/atelier"
  &#125;,
  "Stop": &#123;
    "command": "bash",
    "args": [
      "-c",
      "echo '$&#123;SESSION_SUMMARY_JSON&#125;' | uv run atelier record-trace"
    ],
    "cwd": "/abs/path/to/repo/atelier"
  &#125;
&#125;
```

Field names depend on your Claude Code version. The contract is:

- **SessionStart**: inject `atelier context` output as a system message.
- **PreToolUse / Edit**: run `atelier check-plan`. Exit code `2` blocks the edit.
- **PostToolUse / Bash (failed)**: run `atelier rescue`, surface the suggestion.
- **Stop**: feed an observable summary into `atelier record-trace`.

## 3. Smoke test

After wiring, ask Claude to "rename Shopify product by URL handle". The PreToolUse hook should call `lint`, get a `blocked` result, and prevent the edit.

## 4. Hard rules (mirror these in `CLAUDE.md`)

- Never bypass a `blocked` plan check by editing files anyway.
- Never include hidden chain-of-thought in `trace` payloads — only observable facts.
- For high-risk domains, call `verify` before declaring success.
