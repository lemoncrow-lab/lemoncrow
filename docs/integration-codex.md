# Codex integration

## 1. AGENTS.md

Add this to your repo's `AGENTS.md` (see the [Atelier AGENTS.md](https://github.com/pankaj4u4m/atelier/blob/main/AGENTS.md) for the full template):

```
# Agent Reasoning Runtime
Before editing code:
1. Call `reasoning` with the task, likely files, and known errors.
2. Draft a plan.
3. Call `lint` before modifying files.
4. If the same test/command fails twice, call `rescue`.
5. After finishing, call `trace`.

Never ignore high-severity Reasoning Runtime warnings.
Never store secrets or hidden chain-of-thought in traces.
```

## 2. MCP server config

Codex CLI / IDE configuration:

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

## 3. First-time setup

```bash
cd atelier
uv sync --all-extras
uv run atelier init   # creates .atelier/ + seeds 10 blocks + 7 rubrics
```

## 4. Smoke test

```bash
uv run atelier lint \
  --task "Fix a live state change" \
  --domain state.change \
  --step "Resolve target from URL slug alone" \
  --step "Apply the update"
# Expect: status BLOCKED + suggested plan that uses the canonical identifier.
```

## 5. Tool reference

| Tool        | Required input                      | Returns                                |
| ----------- | ----------------------------------- | -------------------------------------- |
| `reasoning` | `task`                              | injection text                         |
| `lint`      | `task`, `plan`                      | `status`, `warnings`, `suggested_plan` |
| `rescue`    | `task`, `error`                     | `rescue`, `matched_blocks`             |
| `trace`     | `agent`, `domain`, `task`, `status` | `id`                                   |
| `verify`    | `rubric_id`, `checks`               | `status`, `outcomes`                   |
