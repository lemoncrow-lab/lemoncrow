# Zoekt local runtime seam

This package is the Phase 5 local-only seam for large-repo `search` workloads.

## Scope

- serves text-shaped `search` flows first
- does **not** replace `SymbolIntelStore`
- does **not** change `code op="search"` name-first behavior in Phase 5
- keeps lifecycle ownership outside ephemeral engine instances

## Lifecycle model

- one local Zoekt runtime per workspace
- bound to `127.0.0.1` only
- started lazily and reused across repeated searches in the same session
- never started once per `CodeContextEngine` call

## Binary provenance rules

- the runtime seam requires a pinned binary path
- the path must pass local checksum verification before it is accepted
- env override support is allowed only with a matching SHA-256 value
- managed installs must record provenance in `.atelier/bin/MANIFEST.json`

## Current bootstrap expectation

Phase 5 does not assume a Go toolchain on the developer machine. The default
managed path provisions a pinned local shim into `.atelier/bin/zoekt-webserver`
and records its checksum in `.atelier/bin/MANIFEST.json`; env override is still
supported when a matching SHA-256 value is supplied. Routing and benchmark work
build on top of this seam in later task steps.
