# Atelier Python SDK

The Python SDK in `atelier.sdk` is the embeddable contract for service backends and custom hosts that want Atelier without shelling out.

The SDK now routes through one runtime orchestrator (`AtelierRuntimeCore`) that manages capability execution centrally.

## Install

```bash
cd atelier
uv sync --all-extras
```

## Client Types

- `AtelierClient.local(root=".atelier")` uses the in-process runtime and local store.
- `AtelierClient.remote(base_url=..., api_key=...)` targets the HTTP service.
- `AtelierClient.mcp(root=".atelier")` uses the MCP tool contract with a local loopback transport by default.

Concrete classes and namespaces shipped in Phase A/B:

- `AtelierClient`
- `LocalClient`
- `RemoteClient`
- `MCPClient`
- `ReasonBlockClient`
- `RubricClient`
- `TraceClient`
- `EvalClient`
- `SavingsClient`

## Core Workflow

```python
from atelier.sdk import AtelierClient

client = AtelierClient.local(root=".atelier")

context = client.get_reasoning_context(
    task="Fix generated output that drifts back after refresh",
    domain="source.truth",
)

check = client.check_plan(
    task="Apply a live state change",
    domain="state.change",
    plan=["Resolve target from URL slug alone"],
)

if check.status == "blocked":
    rescue = client.rescue_failure(
        task="Apply a live state change",
        domain="state.change",
        error="Known dead end triggered",
    )
    print(rescue.rescue)

gate = client.run_rubric_gate(
    rubric_id="rubric_state_change_safety",
    checks=&#123;
        "canonical_identifier_used": True,
        "pre_change_state_captured": True,
        "read_after_write_completed": True,
        "observed_state_matches_intent": True,
    &#125;,
)

trace = client.traces.record(
    agent="sdk",
    domain="state.change",
    task="Apply a live state change",
    status="success",
)
```

## Namespaces

- `client.reasonblocks` / `client.blocks`: list, search, and fetch ReasonBlocks.
- `client.rubrics`: list, fetch, and run rubric gates.
- `client.traces`: record, list, and inspect traces.
- `client.failures`: analyze failure clusters.
- `client.evals`: run local eval fixtures or query remote eval status.
- `client.savings`: summarize cost and benchmark savings.

## Adapter Integration

For custom host middleware, the SDK can be extended via `src/atelier/adapters/`.
Each adapter supports `shadow`, `suggest`, and `enforce` modes.

## Capability-Aligned Operations

When using MCP-backed SDK clients, these tools map directly to core runtime capabilities:

- `reasoning`
- `lint`
- `route`
- `rescue`
- `trace`
- `verify`
- `memory`
- `search`
- `read`
- `edit`
- `compact`
- `atelier_repo_map`

CLI-only workflows include `atelier sql inspect`, `atelier lesson inbox`, `atelier consolidation inbox`, `atelier report`, `atelier proof show`, and `atelier route contract`.
