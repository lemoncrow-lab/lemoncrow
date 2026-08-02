# LemonCrow Local LLM Gateway

The gateway exposes OpenAI Chat Completions, OpenAI Responses, and Anthropic
Messages APIs. It is the savings boundary behind `lc code`: the frontend
supplies its mature terminal UI and session store, while LemonCrow owns routing,
tools, caching, compaction, verification, and output limits.

## Start it directly

~~~bash
LEMONCROW_GATEWAY_TOKEN=change-me \
  lc serve-openai --host 127.0.0.1 --port 8787 --project-root "$PWD"
~~~

Normally use `lc code`; it starts an authenticated gateway on a random
loopback port and tears it down with the selected frontend.

## Frontends

OpenCode uses `POST /v1/chat/completions` through
`@ai-sdk/openai-compatible`. The managed launcher supplies an inline
`lc/lemoncrow` provider and tested model metadata.

Current Codex releases use the Responses API, which is their only supported
custom-provider wire protocol:

~~~toml
model = "lemoncrow"
model_provider = "lemoncrow"

[model_providers.lemoncrow]
name = "LemonCrow"
base_url = "http://127.0.0.1:8787/v1"
env_key = "LEMONCROW_GATEWAY_TOKEN"
wire_api = "responses"
~~~

Claude Code uses the Anthropic-compatible endpoint:

~~~bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 \
ANTHROPIC_API_KEY="$LEMONCROW_GATEWAY_TOKEN" \
claude --bare --model claude-sonnet-4-6
~~~

Supported routes:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `GET /v1/models`
- `POST /v1/models/refresh`
- `POST /v1/messages`
- `POST /v1/messages/count_tokens`
- `GET /health`

The OpenAI routes accept `Authorization: Bearer`; Anthropic clients may also
use `x-api-key`. With no configured token, only loopback clients are accepted.

## Cost behavior

Host system/developer prompts, host tool transcripts, and host tool schemas are
dropped before the real provider request. For Codex Responses requests, the
adapter also removes the injected AGENTS/environment envelope and outer tool
catalog while retaining real user/assistant history. LemonCrow injects its own
stable cached prefix, deterministic workspace primer, and owned tools.

Virtual model IDs (`lemoncrow`, `lemoncrow-default`, and `lc/lemoncrow`)
invoke phase-aware dynamic routing instead of being sent to LiteLLM as provider
model names. Client output limits are upper bounds; LemonCrow's smaller phase
cap always wins. Streaming adapters consume the runtime through its final
usage/cache events before closing. Tool calls stay server-side and are never
re-executed by the outer coding CLI.

Key properties:

- **Per-request session isolation** — each request gets a fresh runtime session; safe prior user/assistant messages are restored as history.
- **Owned execution** — managed gateway sessions approve LemonCrow's bounded tools without waiting on the outer frontend.
- **Complete streaming lifecycles** — Chat Completions, Responses, and Anthropic streams finish only after accounting/finalization events arrive.
- **Authenticated loopback by default** — `lc code` generates an ephemeral bearer token and stops the gateway with the frontend.

## Available models

Use a virtual LemonCrow model ID for dynamic routing, or request a concrete
provider model to pin it for that request. `GET /v1/models` returns
`lemoncrow` plus discovered provider models.

| Model ID | Description |
| --- | --- |
| `lemoncrow` / `lemoncrow-default` / `lc/lemoncrow` | Phase-aware dynamic provider/model routing |
| Concrete discovered model | Pin that provider model for the request |

---

## Configuring Providers

LemonCrow reads provider credentials from `~/.lemoncrow/providers.json` (or environment variables). Keys in the file supplement — but never override — environment variables.

### Setup

```bash
# 1. Copy the example
cp ~/.lemoncrow/providers.json.example ~/.lemoncrow/providers.json

# 2. Edit — uncomment only the providers you have credentials for
nano ~/.lemoncrow/providers.json

# 3. Restart the service
lc service start   # or kill the old process first

# 4. Verify model list
curl http://localhost:8787/v1/models | jq '.data[].id'

# 5. After editing providers.json, refresh without restarting
curl -X POST http://localhost:8787/v1/models/refresh | jq '.data[].id'
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
| `zen`           | none (free tier) / `api_key`                  | `OPENCODE_API_KEY`                                    |
| `together`      | `api_key`                                     | `TOGETHER_API_KEY`                                    |
| `fireworks`     | `api_key`                                     | `FIREWORKS_API_KEY`                                   |

### OpenCode Zen — zero-config fallback

Zen (`https://opencode.ai/zen/v1`) serves its zero-cost models against the
literal bearer token `public`, so LemonCrow can run a real model on a machine
with no credentials at all. Zen models are namespaced `zen/<model>` (e.g.
`zen/big-pickle`) and rewritten to the litellm OpenAI-compatible form at the
call site, so they never collide with a same-named OpenAI or Anthropic model.

Behaviour:

- **No credentials anywhere** — Zen is the routing vendor and only its free
  models are discovered.
- **Any other vendor configured** — the Zen public tier is not offered. Free
  models are free because the upstream vendors may train on the traffic, so
  they never silently out-compete a vendor you configured yourself.
- **`OPENCODE_API_KEY` set** (or an existing `opencode` login in
  `~/.local/share/opencode/auth.json`) — Zen is offered alongside your other
  vendors and the full paid Zen catalogue is discovered.
- **`LEMONCROW_ZEN_PUBLIC=0`** — disables the keyless fallback entirely.

The free-model set is read from the public `models.dev` catalogue (cost 0 in,
0 out) with a static fallback, so it tracks Zen's promotions instead of
pinning a hardcoded list.

### Troubleshooting

| Symptom                         | Fix                                                                  |
| ------------------------------- | -------------------------------------------------------------------- |
| `/v1/models` returns empty list | No providers configured; check `~/.lemoncrow/providers.json`           |
| Bedrock models missing          | `boto3` must be installed (`uv add boto3`) and credentials valid     |
| Azure models missing            | `endpoint` must be set (e.g. `https://my-resource.openai.azure.com`) |
| Vertex models missing           | `application_credentials` JSON file must exist and be valid          |
| Stale model list                | `GET /v1/models/refresh` to force re-fetch                           |

The example file is at `~/.lemoncrow/providers.json.example` (auto-created on first service start).
