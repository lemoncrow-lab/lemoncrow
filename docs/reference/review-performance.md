# Review performance qualification

G9 remains the release regression guard in `evals/performance/slo.toml`.
`make perf-slo` measures the existing fourteen targets with its existing sample
counts, warm-ups and rerun policy. RC2 does not raise any regression limit.

`make perf-product` measures the three 10,000-file browser targets against a
separate product contract: first useful paint p95 ≤ 5,000 ms, navigation to a
known file p95 ≤ 750 ms, and settled V8 heap ≤ 300 MiB. Each has twenty measured
samples after two warm-ups, with no rerun. The same fixture must contain all
10,000 changed files. The browser probe also rejects more than 5,000 total DOM
nodes and any startup Map request before explicit reviewer interest.

A product qualification requires the owner-designated dedicated machine in
`evals/performance/reference-runner.toml`, a matching CI assignment through
`REVIEW_SLO_RUNNER_ID`, exact hardware facts and load averages at or below the
manifest's quiet ceiling before and after the measurement. The manifest remains
unapproved because no reference runner has been designated in this package.
The preflight fails before starting a fixture service. Do not designate a loaded
developer machine merely to produce a passing reference verdict.

`make perf-product DIAGNOSTIC=1` measures the same product targets on a shared
machine and records why it is unqualified. Reports use `RC2-PRODUCT`; they never
satisfy a `G9-SLO` release verdict. A diagnostic target failure remains a failure.

The large outline mounts a viewport plus overscan, while its complete canonical
queue remains available across pages and filters. Arrow keys, Home, End and page
keys move keyboard focus across windows; native button activation selects the
file. Search and collapsed attention sections retain their existing semantics.
Map/architecture work for large changes starts only after the reviewer requests
it, and stale architecture replies cannot cross revision identity.

This package has no native macOS qualification environment. Linux tests of mocked
macOS wiring do not qualify Seatbelt sandbox, rendered reproduction or native
pixel capture. Their advertised support must remain explicitly unqualified until
the native tests in `tests/gateway/test_macos_review_sandbox.py` run on macOS.

## Evidence

Before/after measurement records and remaining blockers are recorded by the RC-A
coordinator after inspecting this package. Shared-machine diagnostics cannot
establish the dedicated reference-runner product guarantee.
