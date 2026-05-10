"""Phase 4 capabilities demo — end-to-end real examples.

Demonstrates all four Phase 4 components with live output:

  1. Pricing module         — model-aware cost calculations
  2. TelemetrySubstrate     — emit signals, query, aggregate
  3. CapabilityRegistry     — register dependencies, activation path
  4. PromptBudgetOptimizer  — OR-Tools CP-SAT / greedy, real blocks
  5. ToolSupervisionCapability — model-aware USD savings
  6. HTTP API               — start server, hit /metrics and /capabilities

Run:
    cd atelier
    LOCAL=1 uv run python examples/sdk/phase4_demo.py

Optional env vars:
    ATELIER_MODEL=claude-sonnet-4    # choose pricing row (default: _default)
    ATELIER_RUN_SERVER=1             # start the HTTP server and hit it
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from atelier.core.capabilities.budget_optimizer import ContextBlock, PromptBudgetOptimizer
from atelier.core.capabilities.pricing import (
    active_model,
    all_known_models,
    get_model_pricing,
    tokens_to_usd,
)
from atelier.core.capabilities.registry import CapabilityRegistry
from atelier.core.capabilities.telemetry import TelemetrySubstrate

# ---------------------------------------------------------------------------
# 1. Pricing module
# ---------------------------------------------------------------------------

print("\n" + "=" * 64)
print("1. MODEL PRICING")
print("=" * 64)


model = active_model()
print(f"  Active model  : {model}")

# Show pricing for the active model
pricing = get_model_pricing(model)
if pricing.known:
    print(
        f"  Pricing       : ${pricing.input:.2f} input / ${pricing.output:.2f} output / "
        f"${pricing.cache_read:.2f} cache_read  (USD per 1M tokens)"
    )
else:
    print("  Pricing       : Cost: unknown  (model not configured in pricing table)")

# Real cost example: a typical agent loop call
example_input = 4_200
example_output = 1_800
example_cache = 6_000
cost = pricing.cost_usd(example_input, example_output, example_cache)
print(f"\n  Example call  : {example_input} input + {example_output} output + {example_cache} cache_read")
print(f"  Estimated USD : ${cost:.6f}")

# Show cost across all models for 1K output tokens
print("\n  Per-1K output token cost across popular models:")
showcase_models = [
    "claude-opus-4",
    "claude-sonnet-4",
    "claude-haiku-4",
    "gpt-4o",
    "gpt-4o-mini",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "llama-3.1-70b",
]
for mid in showcase_models:
    p = get_model_pricing(mid)
    if p.known:
        per1k = tokens_to_usd(mid, 1000, "output")
        print(f"    {mid:<35} ${per1k:.5f}")
    else:
        print(f"    {mid:<35} Cost: unknown")

print(f"\n  Total models in config: {len(all_known_models())}")

# ---------------------------------------------------------------------------
# 2. Telemetry Substrate
# ---------------------------------------------------------------------------

print("\n" + "=" * 64)
print("2. TELEMETRY SUBSTRATE")
print("=" * 64)


ts = TelemetrySubstrate()

# Simulate tool supervision events
print("\n  Emitting telemetry events …")
for i in range(20):
    ts.emit("tool_supervision", "cache_hit", 1.0 if i % 3 != 0 else 0.0, tool="read_file", call_index=i)
for i in range(15):
    ts.emit("tool_supervision", "token_savings", float(200 * (1 + i % 5)), tool="grep", call_index=i)
for i in range(8):
    ts.emit("context_compression", "compression_ratio", 0.6 + (i * 0.03), stage="summarize")
for i in range(5):
    ts.emit("reasoning_reuse", "similarity_score", 0.7 + (i * 0.05), block_id=f"rb-{i:03d}")

print(f"  Events in ring buffer: {len(ts)}")

# Query with filters
hits = ts.query(capability="tool_supervision", metric="cache_hit", limit=5)
print("\n  Latest 5 cache_hit events:")
for e in hits:
    print(f"    {e.capability}/{e.metric} = {e.value}  | tool={e.context.get('tool')}")

# Aggregate stats
agg = ts.aggregates(capability="tool_supervision", metric="token_savings")
print("\n  token_savings aggregate (tool_supervision):")
print(f"    count={agg['count']}  mean={agg['mean']:.1f}  p95={agg['p95']:.1f}  total={agg['total']:.1f}")

agg_all = ts.aggregates(capability="context_compression")
print("\n  context_compression aggregate:")
print(f"    count={agg_all['count']}  mean={agg_all['mean']:.3f}  p95={agg_all['p95']:.3f}")

# ---------------------------------------------------------------------------
# 3. Capability Registry
# ---------------------------------------------------------------------------

print("\n" + "=" * 64)
print("3. CAPABILITY REGISTRY")
print("=" * 64)


reg = CapabilityRegistry()

# Register capabilities with real dependency graph
_ts_instance = TelemetrySubstrate()
_optimizer = PromptBudgetOptimizer()

reg.register("telemetry", _ts_instance, tags=["infrastructure"])
reg.register("reasoning_reuse", None, depends_on=[("telemetry", 1.0)], tags=["reasoning"])
reg.register("context_compression", None, depends_on=[("telemetry", 0.8)], tags=["compression"])
reg.register(
    "tool_supervision",
    None,
    depends_on=[("telemetry", 1.0), ("reasoning_reuse", 0.5)],
    tags=["tools"],
)
reg.register(
    "budget_optimizer",
    _optimizer,
    depends_on=[("telemetry", 0.6), ("context_compression", 0.9)],
    tags=["optimization"],
)
reg.register(
    "loop_detection",
    None,
    depends_on=[("telemetry", 1.0), ("tool_supervision", 0.7)],
    tags=["safety"],
)

print(f"\n  Capabilities registered: {len(reg)}")

# Activation paths
for target in ["tool_supervision", "budget_optimizer", "loop_detection"]:
    path = reg.activation_path(target)
    print(f"\n  activation_path({target!r}):")
    print(f"    {' → '.join(path)}")

# Fallback resolution
reg.register("budget_optimizer_greedy", None, tags=["optimization", "fallback"], fallback=None)
reg.register(
    "budget_optimizer",
    _optimizer,
    depends_on=[("telemetry", 0.6), ("context_compression", 0.9)],
    tags=["optimization"],
    fallback="budget_optimizer_greedy",
)

fb = reg.fallback_for("budget_optimizer")
print(f"\n  fallback_for('budget_optimizer') → {fb!r}")

# Dependency report
report = reg.dependency_report()
caps_info = report.get("capabilities", report)  # handle both formats
print("\n  Dependency report (partial):")
for name, info in list(caps_info.items())[:4]:
    print(f"    {name}: deps={info['depends_on']}  tags={info['tags']}")

# ---------------------------------------------------------------------------
# 4. Prompt Budget Optimizer
# ---------------------------------------------------------------------------

print("\n" + "=" * 64)
print("4. PROMPT BUDGET OPTIMIZER")
print("=" * 64)

opt = PromptBudgetOptimizer(diversity_bonus=0.15)

# Realistic blocks a real agent session might pass in
blocks = [
    ContextBlock(
        id="reasoning-001",
        content="Read existing architecture docs",
        token_cost=300,
        utility=0.9,
        source="reasoning_reuse",
    ),
    ContextBlock(
        id="reasoning-002",
        content="Prior fix for alembic migration pattern",
        token_cost=200,
        utility=0.85,
        source="reasoning_reuse",
    ),
    ContextBlock(
        id="mem-001",
        content="DB schema for catalog.products",
        token_cost=800,
        utility=0.8,
        source="semantic_memory",
    ),
    ContextBlock(
        id="mem-002",
        content="DB schema for sales.orders",
        token_cost=600,
        utility=0.75,
        source="semantic_memory",
    ),
    ContextBlock(
        id="mem-003",
        content="API endpoint list (auto-generated)",
        token_cost=1200,
        utility=0.5,
        source="semantic_memory",
    ),
    ContextBlock(
        id="tool-001",
        content="Last 5 grep results for 'estimate_cost'",
        token_cost=450,
        utility=0.7,
        source="tool_supervision",
    ),
    ContextBlock(
        id="tool-002",
        content="git diff output for pricing module",
        token_cost=250,
        utility=0.6,
        source="tool_supervision",
    ),
    ContextBlock(
        id="compress-001",
        content="Compressed session history (3 turns)",
        token_cost=400,
        utility=0.65,
        source="context_compression",
    ),
    ContextBlock(
        id="compress-002",
        content="Full session history (raw)",
        token_cost=2100,
        utility=0.5,
        source="context_compression",
    ),
    ContextBlock(
        id="sys-001",
        content="System prompt and instructions",
        token_cost=500,
        utility=1.0,
        source="system",
    ),
]

budget = 2200
plan = opt.solve(blocks, budget)
d = plan.to_dict()

# Build lookup by id for display
blocks_by_id = {b.id: b for b in blocks}

print(f"\n  Budget: {budget} tokens")
print(f"  Solver used: {d['solver_used']}")
print(f"  Selected {d['selected_count']} / {len(blocks)} blocks:")
for bid in sorted(d["selected_ids"], key=lambda x: -blocks_by_id[x].utility):
    b = blocks_by_id[bid]
    print(f"    [{b.source:<22}] {b.id:<20} cost={b.token_cost:>5} util={b.utility:.2f}")
print(f"\n  Total tokens used  : {d['total_tokens']} / {budget}")
print(f"  Total utility      : {d['total_utility']:.3f}")
print(f"  Dropped {len(d['dropped_ids'])} blocks:")
for bid in d["dropped_ids"]:
    b = blocks_by_id[bid]
    print(f"    [{b.source:<22}] {b.id:<20} cost={b.token_cost:>5} util={b.utility:.2f}")

# Tight budget to force more drops
plan_tight = opt.solve(blocks, 800)
dt = plan_tight.to_dict()
print(
    f"\n  Tight budget (800 tokens): selected={dt['selected_count']}  dropped={len(dt['dropped_ids'])}  "
    f"tokens_used={dt['total_tokens']}"
)

# ---------------------------------------------------------------------------
# 5. ToolSupervision with model-aware USD savings
# ---------------------------------------------------------------------------

print("\n" + "=" * 64)
print("5. TOOL SUPERVISION — MODEL-AWARE USD SAVINGS")
print("=" * 64)

with tempfile.TemporaryDirectory() as tmpdir:
    from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

    # Use the pricing-active model
    demo_model = os.environ.get("ATELIER_MODEL", "claude-sonnet-4")
    ts_cap = ToolSupervisionCapability(Path(tmpdir), model=demo_model)

    # Simulate 20 tool calls; second half are marked as cache hits
    tools_sequence = ["read_file"] * 8 + ["grep"] * 6 + ["edit_file"] * 4 + ["run_test"] * 2
    for i, tool in enumerate(tools_sequence):
        result = {"content": f"result_{i}", "type": tool}
        cache_hit = i >= len(tools_sequence) // 2
        ts_cap.observe(f"{tool}:key_{i}", result, cache_hit=cache_hit, tool_name=tool)

    status = ts_cap.status()
    print(f"\n  Model              : {status['model']}")
    p = get_model_pricing(status["model"])
    if p.known:
        print(f"  Pricing            : ${p.output:.2f}/1M output tokens")
    else:
        print("  Pricing            : Cost: unknown")
    print(f"  Total calls        : {status['total_tool_calls']}")
    print(f"  Avoided calls      : {status['avoided_tool_calls']}")
    print(f"  Cache hit rate     : {status['cache_hit_rate']:.1%}")
    print(f"  Token savings      : {status['token_savings']:,} tokens")
    print(f"  USD savings        : ${status['usd_savings']:.6f}")

    # Show what savings look like across different models
    print(f"\n  Token savings in USD across models (same {status['token_savings']:,} tokens saved):")
    for mid in ["claude-opus-4", "claude-sonnet-4", "claude-haiku-4", "gpt-4o", "gemini-2.5-flash"]:
        p = get_model_pricing(mid)
        if p.known:
            usd = tokens_to_usd(mid, status["token_savings"], "output")
            print(f"    {mid:<30} ${usd:.6f}")
        else:
            print(f"    {mid:<30} Cost: unknown")

# ---------------------------------------------------------------------------
# 6. HTTP API (optional)
# ---------------------------------------------------------------------------

if os.environ.get("ATELIER_RUN_SERVER", ""):
    print("\n" + "=" * 64)
    print("6. HTTP API — LIVE SERVER DEMO")
    print("=" * 64)

    import json as _json
    import subprocess
    import urllib.request

    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, "ATELIER_STORE_DIR": tmpdir}
        proc = subprocess.Popen(
            [sys.executable, "-m", "atelier.gateway.adapters.http_api"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(2.5)  # wait for server to start

        base = "http://127.0.0.1:8124"
        # Call real endpoints exposed by atelier.gateway.adapters.http_api
        for endpoint in ["/healthz", "/overview", "/savings", "/tokens"]:
            try:
                with urllib.request.urlopen(f"{base}{endpoint}", timeout=3) as resp:
                    body = _json.loads(resp.read())
                    print(f"\n  GET {endpoint}")
                    print(_json.dumps(body, indent=4)[:600])
            except Exception as exc:
                print(f"\n  GET {endpoint} → ERROR: {exc}")

        proc.terminate()
        proc.wait()
else:
    print("\n  (Set ATELIER_RUN_SERVER=1 to also demo the HTTP API)")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print("\n" + "=" * 64)
print("PHASE 4 DEMO COMPLETE")
print("=" * 64)
print("  ✓ Pricing module      — model-aware USD per 1M tokens from TOML config")
print("  ✓ TelemetrySubstrate  — ring buffer, query, aggregates (p95, mean)")
print("  ✓ CapabilityRegistry  — networkx DAG, activation_path, fallback resolution")
print("  ✓ PromptBudgetOptimizer — OR-Tools CP-SAT 0/1 knapsack, greedy fallback")
print("  ✓ ToolSupervision     — model-aware USD savings per cache hit")
print("")
print("  Config file   : src/atelier/model_pricing.toml  (edit to update prices)")
print("  Override      : ATELIER_PRICING_FILE=/path/custom.toml")
print("  Active model  : ATELIER_MODEL=<model_id>")
