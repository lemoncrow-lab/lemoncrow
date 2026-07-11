# LemonCrow Python SDK

The Python SDK in `lemoncrow.sdk` is the embeddable contract for custom hosts,
tests, and service-side integrations that want LemonCrow without shelling out.

## Install

From a source checkout:

```bash
cd lemoncrow
uv sync --all-extras
```

## Client Types

- `LemonCrowClient.local(root=".lemoncrow")` uses the in-process runtime and local store.
- `LemonCrowClient.remote(base_url=..., api_key=...)` targets the HTTP service.
- `LemonCrowClient.mcp(root=".lemoncrow")` mirrors the MCP contract with a local loopback transport by default.

## Core Workflow

```python
from lemoncrow.sdk import LemonCrowClient

client = LemonCrowClient.local(root=".lemoncrow")

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
  follow the same `LEMONCROW_DEV_MODE=1` gating rules as the runtime.
- Trace recording and remote-service access remain available outside developer mode.

## MCP-Aligned Operations

When using `LemonCrowClient.mcp()`, the client mirrors the current MCP tool
surface documented in [mcp.md](mcp.md), including `context`, `route`, `rescue`,
`record`, `verify`, `memory`, `read`, `edit`, `search`, and `compact`.

Use the CLI for operational workflows such as `lemon report`, `lemon
benchmark ...`, `lemon service ...`, `lemon background ...`, and `lemon
domain ...`.
