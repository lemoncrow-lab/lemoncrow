# Zoekt managed runtime seam

This package is the Phase 5 managed seam for large-repo `search` workloads.

## Scope

- serves text-shaped `search` flows first
- does **not** replace `SymbolIntelStore`
- does **not** change `code op="search"` name-first behavior in Phase 5
- keeps lifecycle ownership outside ephemeral engine instances

## Lifecycle model

- one managed Zoekt runtime per workspace
- the default managed path uses the official `ghcr.io/sourcegraph/zoekt` image
- the runtime is started lazily and reused across repeated searches in the same session
- lifecycle ownership stays outside per-call `CodeContextEngine` instances

## Binary provenance rules

- env override support is still allowed only with a matching SHA-256 value
- the default managed path records its pinned image provenance in `.atelier/bin/MANIFEST.json`
- `VERSIONS.toml` is the source of truth for the pinned managed image reference
- managed runtime bootstrap must not silently downgrade to a fake local compatibility layer

## Current bootstrap expectation

Phase 5 does not assume a Go toolchain on the developer machine. The default
managed path provisions the real Zoekt runtime from the pinned official image
and records that image reference in `.atelier/bin/MANIFEST.json`; env override
is still supported when a matching SHA-256 value is supplied. Routing and
benchmark work build on top of this seam in later task steps.
