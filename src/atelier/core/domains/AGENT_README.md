# Domains

## Purpose

Lightweight Domain Bundle system replacing the Phase D pack ecosystem.
Provides source-controlled reasoning assets (reasonblocks, rubrics, environments,
evals, benchmarks) without publish/install/registry complexity.

## Entry Points

- `models.py` — `DomainBundle`, `DomainBundleRef`, `bundle_manifest_path()`. No versioning, no deps.
- `loader.py` — `DomainLoader`: reads `bundle.yaml` + asset files from disk. Instance methods: `load()`, `load_builtin()`, `list_builtin()`, `list_from_root()`
- `manager.py` — `DomainManager(root)`: facade used by CLI, MCP, and runtime. `list_bundles()`, `info()`, `load_reasonblocks()`, `all_reasonblocks()`
- `builtin/` — source-tree built-in bundles (e.g. `swe.general`)
- `__init__.py` — re-exports `DomainBundle`, `DomainBundleRef`, `DomainLoader`, `DomainManager`

## Key Contracts

- **User bundles**: `<atelier_root>/domains/<bundle-id>/bundle.yaml`
- **Built-in bundles**: `src/atelier/domains/builtin/<bundle-id>/bundle.yaml`
- `DomainManager._resolve()` checks user bundles first — user can override a builtin by providing a same-id bundle in `<root>/domains/`
- No install step needed: drop a `bundle.yaml` directory in the right place and it is live
- `bundle.yaml` keys: `bundle_id`, `domain`, `description`, `author`, `reasonblocks`, `rubrics`, `environments`, `evals`, `benchmarks`

## Where to look next

- CLI: `src/atelier/adapters/cli.py` — `domain list`, `domain info`, `benchmark packs`
- Runtime: `src/atelier/adapters/runtime.py` — `_load_domain_reasonblocks()`
- Tests: `tests/test_domains.py`, `tests/test_phase_d3_d4.py`, `tests/test_runtime_pack_reasoning_context.py`
- Built-in bundle: `builtin/swe.general/`
