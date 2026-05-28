# M16 — Zoekt backend for large-repo text search

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M18 (build-vs-integrate checkpoint must run first).
> Independent of M1–M15.

**No new MCP tool.** Zoekt becomes an optional fourth backend in
`SymbolIntelStore`, activated automatically when a repo exceeds ~500k LOC.
`mcp__atelier__search` and `code op="search"` route to it transparently.

## Goal

Remove the scale ceiling on text and symbol search. Ripgrep reads source files
on every query — fast for small repos, slow and CPU-intensive for repos above
~1M LOC. Zoekt (Google's own trigram search engine, open-sourced and
maintained by Sourcegraph) builds a precomputed trigram index — queries are
microseconds regardless of repo size, and the index fits in RAM for codebases
up to ~50M LOC.

This is the same engine that powers Sourcegraph's search and Google's internal
code search infrastructure.

## Background — Zoekt vs ripgrep

| | ripgrep | Zoekt |
|---|---|---|
| Index | None — reads files every query | Precomputed trigram index |
| Query latency (1M LOC) | ~200ms | ~5ms |
| Query latency (10M LOC) | ~2s | ~5ms |
| Memory footprint | None | ~10–50 MB per 1M LOC |
| Cross-file aggregation | Sequential | Parallel shard scan |
| Activation cost | None | One-time index build (~30s/1M LOC) |

Reference: <https://github.com/sourcegraph/zoekt>.

## Module layout

```
src/atelier/infra/code_intel/zoekt/
  __init__.py
  AGENT_README.md
  server.py        Manages zoekt-indexserver + zoekt-webserver subprocesses
  client.py        HTTP client to Zoekt's JSON search API
  indexer.py       Drives zoekt-index; maps repo layout to Zoekt shards
  adapter.py       SymbolIntelProvider impl; routes large-repo queries
  binary.py        Resolves zoekt binary; lazy download to .atelier/bin/
```

## Binary management (`binary.py`)

Same pattern as M5's ast-grep binary:

- Zoekt ships pre-built Go binaries (~15 MB for the full suite).
- Fetched to `.atelier/bin/zoekt-{indexserver,webserver,index}` on first use.
- Checksum recorded in `.atelier/bin/MANIFEST` alongside ast-grep and SCIP
  indexers.
- Version pinned in `src/atelier/infra/code_intel/zoekt/VERSIONS.toml`.

## Server lifecycle (`server.py`)

Zoekt requires two processes: an index builder and a search server.

```python
class ZoektServer:
    """Manages zoekt-indexserver + zoekt-webserver as background processes.

    The index server watches the source directory and rebuilds shards when
    files change. The web server exposes a JSON search API on localhost.
    Processes are started lazily on first query for a large repo and kept
    alive for the session lifetime.
    """

    def start(self, repo_root: str, index_dir: str) -> int:
        """Returns the HTTP port the search server binds to."""
        self._indexer = subprocess.Popen([
            "zoekt-indexserver",
            "-index", index_dir,
            "-mirror_interval", "60s",
            repo_root,
        ])
        port = find_free_port()
        self._searcher = subprocess.Popen([
            "zoekt-webserver",
            "-index", index_dir,
            "-listen", f"localhost:{port}",
        ])
        wait_until_healthy(f"http://localhost:{port}/healthz", timeout=30)
        return port

    def stop(self): ...
```

The server is started **once per session** and shared across all queries. Port
is registered in the `CodeContextEngine` session state so multiple tool calls
reuse the same server.

## Client (`client.py`)

Zoekt exposes a JSON search API:

```python
import httpx

class ZoektClient:
    def __init__(self, base_url: str):
        self._client = httpx.Client(base_url=base_url, timeout=10.0)

    def search(self, query: str, *, num_matches: int = 50,
               file_glob: str | None = None) -> list[ZoektMatch]:
        params = {"q": query, "num": num_matches}
        if file_glob:
            params["q"] += f" file:{file_glob}"
        resp = self._client.get("/api/search", params=params)
        resp.raise_for_status()
        return [ZoektMatch.from_json(m) for m in resp.json()["Result"]["Files"]]
```

`ZoektMatch` fields mapped to the existing `SearchResult` shape so downstream
budget packing is unchanged.

## Routing in `SymbolIntelStore`

```python
LOC_THRESHOLD = 500_000   # configurable via [code_intel.zoekt] threshold_loc

def route_text_search(self, query, ...):
    # Three-axis decision: query shape, repo scale, backend health.
    if not self._is_text_shaped(query):
        # Name-shaped queries always go to SCIP first.
        scip = self._providers.get("scip")
        if scip and scip.health().ok:
            return scip.find_symbol(query, ...)
        # SCIP unhealthy → fall through to text search.

    if self._repo_stats.total_loc > LOC_THRESHOLD and self._zoekt_available():
        return self._zoekt_adapter.search(query, ...)

    return self._ripgrep_search(query, ...)   # existing path; default
```

The threshold is checked once per session and cached; repo LOC is computed
during `index_repo` (already available in `CodeContextEngine._stats`).

For **symbol search** (name-first): SCIP remains the primary path regardless
of repo size. Zoekt is for text/pattern search only.

### Process lifetime ownership

The MCP server is per-session but `tool_code` is invoked per call — Zoekt's
two long-lived subprocesses (`zoekt-indexserver` + `zoekt-webserver`) must
not be started/stopped per call. Ownership rules:

- `ZoektServer` is instantiated **once** by `CodeContextEngine` during its
  session init and held on `engine.intel_store._zoekt` (lazy: not started
  until first large-repo query).
- `ZoektServer.start()` is idempotent — concurrent first calls coalesce via
  an internal lock.
- `ZoektServer.stop()` is registered with `atexit` and with the MCP server's
  shutdown hook (`gateway/adapters/mcp_server.py:on_shutdown`). If the
  process is SIGKILLed, leftover `zoekt-*` processes are reaped on next
  startup by matching `argv[0]` and our `.atelier/zoekt/index/` path prefix.
- Port is allocated once via `find_free_port()` and persisted in
  `.atelier/zoekt/session.json` so reconnection after server crash is cheap.

## Integration with existing `mcp__atelier__search`

`tool_smart_search` already accepts `budget_tokens`. No signature change needed
— the routing happens inside `SymbolIntelStore` before results reach the tool.

The response gains:

```json
{ ..., "backend": "zoekt", "index_age_seconds": 42 }
```

when Zoekt serves the result, so the caller knows it's reading from a
potentially-stale index (up to 60s behind HEAD by default).

## Validation

Tests under `tests/infra/code_intel/zoekt/`:

- `test_server_starts_and_serves_health` — server starts, `/healthz` returns
  200 within 30s on a fixture repo.
- `test_search_finds_known_string` — fixture repo, known string → match
  returned with correct file_path + line.
- `test_routing_uses_zoekt_above_threshold` — mock `repo_stats.total_loc =
  600_000` → `ZoektClient.search` called, not ripgrep.
- `test_routing_uses_ripgrep_below_threshold` — `total_loc = 100_000` →
  ripgrep path taken.
- `test_server_reused_across_calls` — two sequential search calls in same
  session → server started once.

Benchmark `tests/benchmarks/code_intel/bench_zoekt_vs_ripgrep.py`:

- Fixture: 1M LOC repo (generated).
- Same text query via both backends.
- Zoekt must be ≥ 10× faster on second query (index warm).

## Exit criteria

- Zoekt binary auto-fetched, checksum verified, recorded in `MANIFEST`.
- Routing switches to Zoekt automatically when `total_loc > threshold`.
- `mcp__atelier__search` response includes `backend` field.
- Scale benchmark shows ≥ 10× latency improvement vs ripgrep on 1M LOC.
- Validation matrix row added.

## Open questions

- **Port management.** Use a random free port; record in session state. No
  port should be hardcoded.
- **Index cold-start time.** Initial index build is ~30s for 1M LOC; first
  query during bootstrap is slow. M11 job should pre-build the Zoekt index
  alongside the SCIP index so it's warm before the first user query.
- **Memory cap.** Default Zoekt index RAM = 200 MB. Configurable via
  `[code_intel.zoekt] max_index_size_mb = 200`.
- **M18 dependency.** This milestone is blocked on the M18 build-vs-integrate
  evaluation. If M18 concludes that a Sourcegraph `src` CLI adapter covers the
  use case, build `SrcCliAdapter` instead of running Zoekt locally. See M18.
