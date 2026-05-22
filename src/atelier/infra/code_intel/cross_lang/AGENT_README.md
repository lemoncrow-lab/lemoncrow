# Cross-language edge infra

Phase 5 keeps this package deliberately small:

- literal-only static edges
- additive-only `code op="symbol"` and `code op="usages"` payload changes
- no `scope="external"` support
- no workspace or multi-repo routing
- no runtime tracing or interprocedural expansion

The runner contract is the scope ceiling for this phase. Resolver logic lives
here so `engine.py` only needs typed hydration hooks.
