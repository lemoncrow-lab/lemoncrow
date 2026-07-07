# Atelier OpenAI-Compatible Gateway

Atelier's service exposes a standards-compliant `/v1/chat/completions` streaming endpoint. Any TUI that supports custom OpenAI-compatible providers can use Atelier as its brain — routing, caching, subagents, memory, and verification all stay inside Atelier.

## Start the gateway

```bash
atelier service start --port 8787
```

Optional standalone mode (legacy):

```bash
atelier serve-openai --port 8787
```

Options for standalone mode:

| Flag             | Default | Description                                                    |
| ---------------- | ------- | -------------------------------------------------------------- |
| `--port`         | 8787    | TCP port                                                       |
| `--host`         | 0.0.0.0 | Bind address                                                   |
| `--project-root` | cwd     | Working directory for Atelier runtime                          |
| `--no-yolo`      | off     | Require manual approval for tool calls (default: auto-approve) |

## Connect a TUI

### OpenCode

`opencode.json` (project or `~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "atelier": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Atelier",
      "options": {
        "baseURL": "http://localhost:8787/v1",
        "apiKey": "local"
      },
      "models": {
        "atelier-default": { "name": "Atelier Default" }
      }
    }
  },
  "model": "atelier/atelier-default"
}
```

### Crush

`crush.json`:

```json
{
  "$schema": "https://charm.land/crush.json",
  "providers": {
    "atelier": {
      "type": "openai-compat",
      "base_url": "http://localhost:8787/v1",
      "api_key": "local",
      "models": [
        {
          "id": "atelier-default",
          "name": "Atelier",
          "context_window": 200000,
          "default_max_tokens": 16000
        }
      ]
    }
  }
}
```

### Codex (`~/.codex/config.toml`)

```toml
model = "atelier-default"
model_provider = "atelier"

[model_providers.atelier]
name     = "Atelier"
base_url = "http://localhost:8787/v1"
env_key  = "ATELIER_API_KEY"
wire_api = "chat"
```

Set `ATELIER_API_KEY=local` (or any non-empty value) in your shell.

### Claude Code (MCP — zero configuration)

Atelier already ships `atelier mcp`. Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "atelier": {
      "command": "atelier mcp",
      "env": { "ATELIER_SERVICE_URL": "http://127.0.0.1:8787" }
    }
  }
}
```

### curl smoke test

```bash
curl -X POST http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer local" \
  -d '{"model":"atelier-default","messages":[{"role":"user","content":"hello"}],"stream":true}' \
  --no-buffer
```

Expected: SSE stream of `data: {...}` chunks, terminated by `data: [DONE]`.

## Architecture

```
TUI  ──POST /v1/chat/completions──►  openai_gateway/app.py
                                              │
                                        adapter.py
                                    (OpenAI ↔ NDJSON)
                                              │
                                  InteractiveRuntime.handle_user_message()
                                              │
                               Atelier routing / caching / subagents
```

Key properties:

- **Per-request session isolation** — each HTTP request gets a fresh session ID; prior messages are injected as history so context is preserved within a conversation.
- **Auto-approve in gateway mode** — `--no-yolo` disables this; without it the agent loop would block waiting for terminal input that never comes.
- **Streaming by default** — set `"stream": false` in the request body for a buffered response.
- **Tool calls visible** — tool calls Atelier makes during execution are forwarded as OpenAI function-call deltas so capable TUIs can display them.

## Available models

| Model ID          | Description                                  |
| ----------------- | -------------------------------------------- |
| `atelier-default` | Atelier's auto-selected route (balanced)     |
| `atelier-auto`    | Same as default                              |
| `atelier-cheap`   | Routes to cheapest available provider        |
| `atelier-best`    | Routes to highest-quality available provider |

---

## Configuring Providers

Atelier reads provider credentials from `~/.atelier/providers.json` (or environment variables). Keys in the file supplement — but never override — environment variables.

### Setup

```bash
# 1. Copy the example
cp ~/.atelier/providers.json.example ~/.atelier/providers.json

# 2. Edit — uncomment only the providers you have credentials for
nano ~/.atelier/providers.json

# 3. Restart the service
atelier service start   # or kill the old process first

# 4. Verify model list
curl http://localhost:8787/v1/models | jq '.data[].id'

# 5. After editing providers.json, refresh without restarting
curl -X GET http://localhost:8787/v1/models/refresh | jq '.data[].id'
```

### Supported providers

| Provider        | Required field                                | Env var alias                                         |
| --------------- | --------------------------------------------- | ----------------------------------------------------- |
| `anthropic`     | `api_key`                                     | `ANTHROPIC_API_KEY`                                   |
| `openai`        | `api_key`                                     | `OPENAI_API_KEY`                                      |
| `google`        | `api_key`                                     | `GOOGLE_API_KEY`                                      |
| `bedrock`       | `aws_bearer_token_bedrock` + `aws_region`     | `AWS_BEARER_TOKEN_BEDROCK` + `AWS_REGION`             |
| `bedrock` (IAM) | `aws_access_key_id` + `aws_secret_access_key` | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`         |
| `vertex`        | `project` + `application_credentials`         | `VERTEXAI_PROJECT` + `GOOGLE_APPLICATION_CREDENTIALS` |
| `azure`         | `api_key` + `endpoint`                        | `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT`      |
| `openrouter`    | `api_key`                                     | `OPENROUTER_API_KEY`                                  |
| `groq`          | `api_key`                                     | `GROQ_API_KEY`                                        |
| `mistral`       | `api_key`                                     | `MISTRAL_API_KEY`                                     |
| `ollama`        | `base_url`                                    | `OLLAMA_HOST`                                         |
| `together`      | `api_key`                                     | `TOGETHER_API_KEY`                                    |
| `fireworks`     | `api_key`                                     | `FIREWORKS_API_KEY`                                   |

### Troubleshooting

| Symptom                         | Fix                                                                  |
| ------------------------------- | -------------------------------------------------------------------- |
| `/v1/models` returns empty list | No providers configured; check `~/.atelier/providers.json`           |
| Bedrock models missing          | `boto3` must be installed (`uv add boto3`) and credentials valid     |
| Azure models missing            | `endpoint` must be set (e.g. `https://my-resource.openai.azure.com`) |
| Vertex models missing           | `application_credentials` JSON file must exist and be valid          |
| Stale model list                | `GET /v1/models/refresh` to force re-fetch                           |

The example file is at `~/.atelier/providers.json.example` (auto-created on first service start).
