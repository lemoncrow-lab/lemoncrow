# Atelier Python SDK

The Python SDK in `atelier.sdk` is the embeddable contract for custom hosts,
tests, and service-side integrations that want Atelier without shelling out.

## Install

From a source checkout:

```bash
cd atelier
uv sync --all-extras
```

## Client Types

- `AtelierClient.local(root=".atelier")` uses the in-process runtime and local store.
- `AtelierClient.remote(base_url=..., api_key=...)` targets the HTTP service.
- `AtelierClient.mcp(root=".atelier")` mirrors the MCP contract with a local loopback transport by default.

## Core Workflow

```python
from atelier.sdk import AtelierClient

client = AtelierClient.local(root=".atelier")

context = client.get_context(
    task="Fix generated output that drifts back after refresh",
    domain="source.truth",
)

rescue = client.rescue_failure(
    task="Apply a live state change",
    domain="state.change",
    error="Known dead end triggered",
)
print(rescue.rescue)

gate = client.run_rubric_gate(
    rubric_id="rubric_state_change_safety",
    checks={
        "canonical_identifier_used": True,
        "pre_change_state_captured": True,
        "read_after_write_completed": True,
        "observed_state_matches_intent": True,
    },
)

trace = client.traces.record(
    agent="sdk",
    domain="state.change",
    task="Apply a live state change",
    status="success",
)
```

## Namespaces

- `client.playbooks` / `client.blocks` — list, search, and fetch Playbooks.
- `client.rubrics` — list, fetch, and run rubric gates.
- `client.traces` — record, list, and inspect traces.
- `client.failures` — analyze failure clusters.
- `client.evals` — list and run eval cases.
- `client.savings` — summarize savings and cost deltas.
- `client.memory` — upsert, fetch, archive, and recall memory blocks.
- `client.lessons` — review lesson candidates where supported.

## Dev-Mode Alignment

The SDK follows the same runtime policy as the CLI and MCP surfaces.

- Context retrieval, rescue, verification, memory, and MCP-backed helper flows
  follow the same `ATELIER_DEV_MODE=1` gating rules as the runtime.
- Trace recording and remote-service access remain available outside developer mode.

## MCP-Aligned Operations

When using `AtelierClient.mcp()`, the client mirrors the current MCP tool
surface documented in [mcp.md](mcp.md), including `context`, `route`, `rescue`,
`record`, `verify`, `memory`, `read`, `edit`, `search`, and `compact`.

Use the CLI for operational workflows such as `atelier report`, `atelier
benchmark ...`, `atelier service ...`, `atelier background ...`, and `atelier
domain ...`.
