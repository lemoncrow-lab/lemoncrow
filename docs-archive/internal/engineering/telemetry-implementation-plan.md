# Atelier Product Telemetry — Implementation Plan

**Audience:** coding agent picking up implementation. **Status:** approved design, ready to build.
**Author context:** decisions captured 2026-05-06. See also `core/capabilities/telemetry/substrate.py` (existing internal substrate — do NOT extend that for product analytics).

---

## 1. Goal

Add a product-telemetry layer to Atelier so we can answer questions like "which features are used", "where do users abandon", "what value are we delivering", "where do users get frustrated". Distinct from:

- `core/capabilities/telemetry/substrate.py` — internal in-memory bus that feeds adaptive priors / rerankers / budget optimizer. **Untouched by this work.**
- `core/service/telemetry.py` — audit log + request-timing stub. **Untouched by this work.**
- `prometheus-client` deps — ops metrics. **Untouched by this work.**

Net new: a third layer for **product analytics** with strict privacy guarantees and a local-first design.

---

## 2. Architecture

```
Atelier Python (CLI, MCP server, HTTP API, runtime)
        │
        │  emit_product(event_name, **props)
        ▼
core/service/telemetry/  ── allowlist + PII scrubber ──┐
        │                                              │
        ├──► local SQLite (~/.atelier/telemetry.db)  ◄─┤  always written
        │                                              │
        └──► OTel SDK ──► OTel Collector ──┬──► PostHog (OTLP ingest)   ← product
                                           └──► GCP Cloud Operations    ← traces/metrics/logs
                                           (gated by opt-out flag)

Frontend (React)
        ├──► PostHog JS native (autocapture, gated by config)
        └──► Insights tab — reads ~/.atelier/telemetry.db via local API endpoint
```

**Routing rules:**

| Signal | Destination |
|---|---|
| Product events (commands, features, frustration, value) | PostHog (via OTel/OTLP) + local SQLite |
| Service traces, latency, error rates | GCP Cloud Trace |
| Infra metrics, uptime | GCP Cloud Monitoring |
| Logs | GCP Cloud Logging (selected logs may also tee to PostHog later) |
| Frontend product events | PostHog JS native + local SQLite via API |

**Why OTel from day one:** PostHog accepts OTLP. When we move from PostHog cloud to self-hosted, only the Collector exporter URL changes — no app code touches. Avoids a future migration.

**Why PostHog JS native (not OTel) on frontend:** browser OTel still trails native SDKs for autocapture, session replay, funnels. Frontend talks straight to PostHog.

---

## 3. File Layout (new code)

```
src/atelier/core/service/telemetry/
  __init__.py              # public API surface
  emit.py                  # emit_product(event_name, **props) — single entry point
  schema.py                # event registry, allowlist enforcement, type checks
  scrubber.py              # PII pass: paths, emails, IPs, repo URLs
  local_store.py           # SQLite writer + reader (~/.atelier/telemetry.db)
  identity.py              # anon UUID at ~/.config/atelier/telemetry_id
  config.py                # opt-in/out flags, env vars, config file
  banner.py                # first-run disclosure banner (CLI)
  frustration_lexicon.yaml # transparent in-repo word list
  frustration.py           # client-side matcher; emits category only
  exporters/
    __init__.py
    otel.py                # OTel SDK setup; trace + metric + log providers
    posthog_frontend.py    # exposes config to frontend (key, host, opt-out state)

src/atelier/gateway/adapters/
  cli.py                   # ADD: SIGINT/SIGTERM handlers, command lifecycle events
  mcp_server.py            # ADD: session start/end, tool_called wrapper events
  http_api.py              # ADD: request middleware emitting api_request events
  wrappers.py              # ADD: lifecycle hooks for atelier-task / atelier-context / etc.

src/atelier/core/runtime/
  engine.py                # ADD: lift loop_detection / monitor signals to product events
                           # ADD: plan-modified-by-user detection at gateway boundary

frontend/src/
  pages/Insights.tsx       # NEW tab — local-first dashboard
  pages/Insights.test.tsx
  lib/telemetry.ts         # PostHog JS init, gated by config from API
  lib/insightsApi.ts       # client for the local-store readout endpoint

src/atelier/core/service/
  api.py                   # ADD: GET /telemetry/local (paged events for Insights tab)
                           # ADD: GET /telemetry/config, POST /telemetry/config

deploy/
  otel-collector.yaml      # OTLP in → PostHog + GCP exporters
  otel-collector-dev.yaml  # local-only variant for development
```

**Existing files to leave alone:** `core/capabilities/telemetry/substrate.py`, `core/service/telemetry.py`, `core/foundation/metrics.py`. Those serve different purposes.

---

## 4. Public API

Single entry point. Capabilities and adapters call only this:

```python
from atelier.core.service.telemetry import emit_product

emit_product(
    "reasonblock_applied",
    block_id_hash="sha256:abcd1234",
    domain="swe.general",
    retrieval_score=0.82,
    session_id=session.id,
)
```

Internally, `emit_product`:

1. Looks up event in registry; rejects if unknown.
2. Drops any property not in the event's allowlist (logs a warning at DEBUG).
3. Type-checks values per registry.
4. Runs scrubber on string values.
5. Writes to local SQLite (always, regardless of opt-out).
6. If opt-out flag is False, hands off to OTel SDK for export.

OTel emission, batching, retries, network failures — all handled inside this module. Callers see one synchronous, side-effect-free function that never raises.

---

## 5. Event Registry

Define every event in `schema.py` as a dataclass or dict. Allowlist is hard-enforced.

```python
EVENTS = &#123;
    "session_start": &#123;
        "props": ["agent_host", "atelier_version", "os", "py_version",
                  "anon_id", "session_id"],
    &#125;,
    "session_end": &#123;
        "props": ["session_id", "duration_s_bucket", "exit_reason"],
    &#125;,
    "session_interrupted": &#123;
        "props": ["session_id", "signal", "elapsed_s_bucket", "last_phase"],
    &#125;,
    "cli_command_invoked": &#123;
        "props": ["command_name", "session_id", "anon_id"],
        # NOT args, NOT cwd
    &#125;,
    "cli_command_completed": &#123;
        "props": ["command_name", "session_id", "duration_ms_bucket", "ok"],
    &#125;,
    "mcp_tool_called": &#123;
        "props": ["tool_name", "session_id", "duration_ms_bucket", "ok"],
    &#125;,
    "api_request": &#123;
        "props": ["endpoint", "method", "status_code", "duration_ms_bucket"],
    &#125;,
    "reasonblock_retrieved": &#123;
        "props": ["block_id_hash", "domain", "retrieval_score",
                  "rank", "session_id"],
    &#125;,
    "reasonblock_applied": &#123;
        "props": ["block_id_hash", "domain", "retrieval_score", "session_id"],
    &#125;,
    "reasonblock_rejected": &#123;
        "props": ["block_id_hash", "domain", "rejection_reason", "session_id"],
    &#125;,
    "plan_check_passed": &#123;
        "props": ["domain", "rule_count", "session_id"],
    &#125;,
    "plan_check_blocked": &#123;
        "props": ["domain", "blocking_rule_id", "severity", "session_id"],
    &#125;,
    "plan_check_overridden": &#123;
        "props": ["domain", "blocking_rule_id", "session_id"],
    &#125;,
    "plan_modified_by_user": &#123;
        "props": ["domain", "edit_distance_bucket", "steps_added",
                  "steps_removed", "session_id"],
    &#125;,
    "failure_cluster_matched": &#123;
        "props": ["cluster_id_hash", "domain", "session_id"],
    &#125;,
    "rescue_offered": &#123;
        "props": ["cluster_id_hash", "rescue_type", "session_id"],
    &#125;,
    "rescue_accepted": &#123;
        "props": ["cluster_id_hash", "session_id"],
    &#125;,
    "frustration_signal_behavioral": &#123;
        "props": ["signal_type", "session_id"],
        # signal_type ∈ &#123;loop_detected, retry_burst, file_revert,
        #                abandon_after_error, plan_resubmitted_unchanged,
        #                repeated_dead_end&#125;
    &#125;,
    "frustration_signal_lexical": &#123;
        "props": ["category", "surface", "session_id"],
        # category ∈ lexicon-defined set
        # surface  ∈ &#123;cli_input, mcp_prompt, api_body&#125;
        # NEVER include the matched word, the input text, or any hash of either
    &#125;,
    "value_estimate": &#123;
        "props": ["session_id", "tokens_saved_estimate", "cache_hits",
                  "blocks_applied"],
    &#125;,
&#125;
```

**Bucketing rules** (avoid fingerprinting via high-cardinality numerics):

- `duration_ms_bucket`: `["<100", "100-500", "500-2000", "2000-10000", ">10000"]`
- `duration_s_bucket`: `["<10", "10-60", "60-300", "300-1800", ">1800"]`
- `elapsed_s_bucket`: same as duration_s_bucket
- `edit_distance_bucket`: `["none", "small", "medium", "large"]`

`anon_id` and `session_id` are UUIDs; `*_id_hash` fields are SHA-256 truncated to 16 hex chars.

---

## 6. Privacy Primitives

### Allowlist enforcement

```python
def emit_product(event: str, **props):
    spec = EVENTS.get(event)
    if not spec:
        logger.debug("telemetry.unknown_event", event=event)
        return
    allowed = set(spec["props"])
    filtered = &#123;k: v for k, v in props.items() if k in allowed&#125;
    dropped = set(props) - allowed
    if dropped:
        logger.debug("telemetry.dropped_props", event=event, dropped=list(dropped))
    ...
```

Add a unit test that asserts every `emit_product` call site uses only allowlisted props. Static check via AST walk of the gateway/adapters and runtime/engine modules.

### PII scrubber

`scrubber.py` runs regex passes on all string values:

- Filesystem paths: `/Users/...`, `/home/...`, `C:\\...` → `<path>`
- Email addresses → `<email>`
- IPv4/IPv6 → `<ip>`
- Git remote URLs (github.com, gitlab.com, bitbucket, ssh://git@...) → `<repo>`
- API-key-shaped strings (`sk-...`, `gh[opsu]_...`, JWT-shaped) → `<secret>`

Tests: round-trip a payload of realistic strings through the scrubber and assert none match the patterns.

### Anonymous identity

- Path: `~/.config/atelier/telemetry_id` (XDG-respecting; Windows: `%APPDATA%/atelier/telemetry_id`).
- Generated as UUID4 on first run.
- Permissions: 0600.
- Reset by deleting the file or via `atelier telemetry reset-id`.

### First-run banner

Shown once per machine on first CLI invocation and first frontend visit. Stored ack at `~/.config/atelier/telemetry_ack`.

```
Atelier collects anonymous usage telemetry to improve the product.
Disable any time:  atelier telemetry off  |  ATELIER_TELEMETRY=0
What's collected:  atelier telemetry show  (or open the Insights tab)
Privacy details:   https://atelier.dev/telemetry
```

---

## 7. Opt-out Model

**Default state: ON.** All three of these set the same flag:

- Env var: `ATELIER_TELEMETRY=0` (or `false`, `off`, `no`)
- CLI: `atelier telemetry off` / `on` / `status`
- Frontend toggle in the Insights tab → POST `/telemetry/config`

Stored in `~/.config/atelier/telemetry.toml`:

```toml
[telemetry]
remote_enabled = true
lexical_frustration_enabled = true   # separate switch — see §9
```

**Opt-out gates only the remote exporter.** The local SQLite writer always runs so the Insights tab works.

---

## 8. Local Store

`~/.atelier/telemetry.db` (SQLite). Schema:

```sql
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  event TEXT NOT NULL,
  session_id TEXT,
  props_json TEXT NOT NULL,
  exported INTEGER NOT NULL DEFAULT 0   -- 0/1: did we attempt remote export
);
CREATE INDEX events_ts ON events(ts DESC);
CREATE INDEX events_event_ts ON events(event, ts DESC);
CREATE INDEX events_session ON events(session_id);
```

**Retention:** keep 30 days locally; nightly prune in the SQLite writer's open-path. No quotas — SQLite is cheap.

**Read API** (for Insights tab):

- `GET /telemetry/local?since=<ts>&event=<name>&limit=500` → paged events
- `GET /telemetry/summary` → derived rollups (commands/day, top reasonblocks, frustration over time, value estimate)
- `GET /telemetry/schema` → registry dump for the privacy-audit view

---

## 9. Frustration Signals

### Behavioral (already partly captured)

Lift these from existing code into `emit_product` calls:

| Source (existing) | Emit |
|---|---|
| `capabilities/loop_detection` loop_probability > threshold | `frustration_signal_behavioral&#123;signal_type=loop_detected&#125;` |
| `monitors.RepeatedCommandFailure` triggers | `&#123;signal_type=retry_burst&#125;` |
| `SessionState.file_events` action == "revert" | `&#123;signal_type=file_revert&#125;` |
| `monitors.KnownDeadEnd` triggers | `&#123;signal_type=repeated_dead_end&#125;` |
| Plan rejected by check, then resubmitted unchanged | `&#123;signal_type=plan_resubmitted_unchanged&#125;` (NEW detection) |
| Error event followed by session_end within 30s | `&#123;signal_type=abandon_after_error&#125;` (NEW detection) |

### Lexical (client-side, new)

`frustration_lexicon.yaml`:

```yaml
# Reviewed, in-repo, transparent. Categories tuned for Atelier.
# Do NOT port leaked Claude Code prompt content.
categories:
  explicit_negative:
    - "this is broken"
    - "doesn't work"
    - "useless"
    # ... ~10 patterns
  command_repeat_marker:
    - "again"
    - "still"
    - "yet again"
    # ... ~5 patterns
  dissatisfaction:
    - "not what i asked"
    - "you keep"
    - "stop doing"
    # ... ~10 patterns
  correction:
    - "no, i said"
    - "i told you"
    # ... ~10 patterns
```

Matcher (`frustration.py`):

- Runs only on user-typed inputs to CLI/MCP/API (NOT on code, commit messages, ReasonBlocks, or model outputs).
- Lowercased substring match against the YAML patterns.
- Emits `frustration_signal_lexical&#123;category, surface&#125;`. Nothing else.
- Off by default? **No, on by default** since user opted in. But has its own kill switch in `telemetry.toml` and CLI: `atelier telemetry lexical off`.
- Unit test: feed in 50 sample frustrated-user inputs, assert correct categorization, assert nothing from the input text appears in the emitted event.

**Hard rule documented at the top of `frustration.py`:** never log, never hash, never bucket, never include the matched substring or any portion of the input. Only the category and the surface name leave the function.

---

## 10. Insights Tab (Frontend)

New route at `/insights`. Reads from local API endpoints (§8). Sections:

1. **Status banner** — "Remote telemetry: ON / OFF" with toggle. Same toggle for "Lexical frustration detection: ON / OFF".
2. **Live event stream** — last 100 events, displayed as the exact JSON that would be sent. Refresh every 5s.
3. **Usage** — line chart of commands/day; bar chart of top commands; agent-host pie.
4. **Reasoning value** — top reasonblocks applied (by hash + domain), retrieval-score distribution, plan-block effectiveness.
5. **Frustration trends** — stacked area chart of behavioral signal types over time; bar chart of lexical categories.
6. **Estimated value** — running totals of tokens saved, cache hit rate, plans gated.
7. **Privacy audit** — table of every event in the registry with its allowlisted properties and an example payload. This is the user-facing version of `schema.py`.

Component layout follows existing pages (e.g., `pages/Savings.tsx`). Use existing chart library / styling.

---

## 11. Exporters

### OTel (Python)

```python
# exporters/otel.py
from opentelemetry import trace, metrics, _logs
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk.resources import Resource

def init_otel(*, endpoint: str, service_version: str):
    resource = Resource.create(&#123;
        "service.name": "atelier",
        "service.version": service_version,
        # NO user-identifying resource attrs here
    &#125;)
    # set up tracer, meter, logger providers with batch processors
    # endpoint defaults to http://localhost:4318 (Collector)
```

Product events flow as OTel **logs** (the cleanest fit for discrete events) with `event.name = <our event name>` and props as attributes. Traces are reserved for service-internal spans (request handling, retrieval, plan check), **not** product events.

### OTel Collector

`deploy/otel-collector.yaml`:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 30s
    send_batch_size: 512
  attributes/scrub:
    actions:
      - key: cwd
        action: delete
      - key: file_path
        action: delete
      # belt-and-braces; the app already scrubs

exporters:
  otlphttp/posthog:
    endpoint: $&#123;POSTHOG_OTLP_ENDPOINT&#125;     # e.g. https://us.i.posthog.com/i/v0/otlp
    headers:
      Authorization: "Bearer $&#123;POSTHOG_PROJECT_API_KEY&#125;"
  googlecloud:
    project: $&#123;GCP_PROJECT_ID&#125;
    log:
      default_log_name: atelier

service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [attributes/scrub, batch]
      exporters: [otlphttp/posthog, googlecloud]
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [googlecloud]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [googlecloud]
```

Run as a sidecar container next to the Atelier API in production. For local dev: `otel-collector-dev.yaml` exports to a local file or stdout, no remote.

### PostHog Frontend

```ts
// frontend/src/lib/telemetry.ts
import posthog from 'posthog-js';

export async function initTelemetry() &#123;
  const cfg = await fetch('/telemetry/config').then(r => r.json());
  if (!cfg.remote_enabled) return;
  posthog.init(cfg.posthog_key, &#123;
    api_host: cfg.posthog_host,
    autocapture: true,
    capture_pageview: true,
    persistence: 'localStorage',
    // critical: do not capture form fields, do not capture clipboard, etc.
    mask_all_text: false,
    mask_all_element_attributes: false,
    sanitize_properties: (props) => scrubFrontend(props),  // mirror Python scrubber
  &#125;);
  posthog.identify(cfg.anon_id);
&#125;
```

The frontend always writes events to the local store too (via `POST /telemetry/local`). That keeps the Insights tab honest when remote is off.

---

## 12. Phases

### Phase 1 — Substrate, no remote (start here)

- Everything in `core/service/telemetry/` except `exporters/otel.py`.
- Local SQLite writer wired up.
- Identity, config, banner, opt-out flag.
- Allowlist enforcement and scrubber with full unit-test coverage.
- One CLI command emits a test event end-to-end into local SQLite.
- `atelier telemetry status / on / off / show / reset-id` CLI.

**Deliverable:** zero bytes leave the machine. Reviewable in isolation. Privacy primitives proven.

### Phase 2 — Instrumentation

- CLI: SIGINT/SIGTERM handlers; command-lifecycle events; first-run banner.
- MCP server: session lifecycle, tool_called wrapper.
- HTTP API: request middleware.
- Runtime: lift loop_detection, monitors, file_revert into product events; add plan-modified-by-user and abandon-after-error detection.
- Lexical frustration matcher with its own switch.

**Deliverable:** every event in §5 is emitted by the right code path. Verified by integration tests that read from the local SQLite store.

### Phase 3 — Remote exporters

- OTel SDK init in API/CLI/MCP entrypoints.
- Collector config for PostHog Cloud (free tier) + GCP.
- Docker compose entry for the Collector sidecar.
- Frontend PostHog JS init, gated by config endpoint.

**Deliverable:** events visible in PostHog and GCP. Opt-out verified to stop remote export but keep local capture.

### Phase 4 — Insights tab

- New route, components, charts.
- API endpoints (`/telemetry/local`, `/telemetry/summary`, `/telemetry/schema`, `/telemetry/config`).
- Privacy-audit view rendering the registry.

**Deliverable:** users see their own data and can audit what would be sent.

---

## 13. Dependencies to Add

`pyproject.toml` (add as a new optional group `telemetry`, then include in default install):

```
opentelemetry-api>=1.27
opentelemetry-sdk>=1.27
opentelemetry-exporter-otlp-proto-http>=1.27
```

`frontend/package.json`:

```
"posthog-js": "^1.150.0"
```

No `posthog` Python SDK — we go through OTel.

---

## 14. Hard Rules (do not violate)

1. **Never** include in any emitted event: prompts, code, ReasonBlock bodies, file paths, repo names/URLs, commit messages, model outputs, user input text, hashes of any of those.
2. **Never** parse user input text outside the lexical-frustration matcher, and even there, never emit anything beyond category + surface.
3. **Never** copy leaked Claude Code prompt content into the lexicon. Build it from open sources or first principles.
4. **Always** allow opt-out to take effect immediately (no batched-flush leak after toggle to OFF).
5. **Always** keep the local writer running on opt-out; the Insights tab depends on it.
6. **Never** emit at INFO level from the telemetry path. DEBUG only — no log spam.
7. **Never** raise from `emit_product`. Failures swallow + log at DEBUG.
8. **Do not** extend `core/capabilities/telemetry/substrate.py` — it serves a different (internal-adaptation) purpose. Keep them separate.

---

## 15. Open Items (defer to product owner)

- PostHog cloud project + API key (needed for Phase 3 only).
- GCP project ID + service-account credentials for Cloud Ops exporter (Phase 3 only).
- URL for the privacy-policy page linked from the banner.
- Whether to ship Phase 1 in a release before Phase 2 is done (recommend yes — it's harmless without instrumentation, and lets the privacy primitives bake).

---

## 16. Acceptance Criteria

- [ ] Unit tests prove every emit call site uses only allowlisted props (AST walk).
- [ ] Scrubber passes a fixture of 100 realistic strings; no PII leaks.
- [ ] `ATELIER_TELEMETRY=0` is honored within 1 second of being set (no batched-flush leak).
- [ ] With remote off, `~/.atelier/telemetry.db` still grows; with remote on, events appear in PostHog within 60s.
- [ ] Insights tab loads on a fresh machine with no events and renders empty states without errors.
- [ ] First-run banner shows once and only once.
- [ ] No event in §5 violates §14.
- [ ] `atelier telemetry show` prints the last 20 events as JSON, exactly as they would be sent.
