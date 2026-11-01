# Release qualification: concurrency, evidence, and isolation

The four-core local runner runs at most four release components concurrently.
Its unprivileged runner and nested Docker daemon share host CPUs 16-19
(four distinct physical cores) and each has a four-CPU cgroup ceiling.
Declaration-ordered reports and timestamped start/finish events remain.

Resource locks protect Redis, the browser/visual-sensitive tests and other
shared services. The shortened Gate H contract tests remain; repeated-sample
Gate H measurement is outside the release. Root and enterprise confidence
suites are fixed, small selectors, not CPU-wide pytest-xdist pools.

## One-hour release qualification (2026-11-02)

**Hard time limits:** GitHub's `full` qualification job has
`timeout-minutes: 60`; its release gate is independently limited to 55 minutes
by GNU `timeout`, leaving time for cleanup and report upload. Local
`make release-qualify` wraps **all** preflight, testing and stamping in a
59-minute deadline plus a 20-second termination grace. Any deadline breach is
a failed and **unreleasable** qualification, never a pass. The raw
`release-gate` developer command can still be invoked manually outside that
wrapper for diagnosis, without claiming it met the one-hour SLO.

**Measured baseline:** the October 27, 2026 gate took 7,945.89 seconds
(132.4 minutes) with two parallel slots. Its long wall time came primarily
from global exclusive execution barriers, not simply the number of tests.
The release contract now has **49 components**, with the 50k-file client-scale
benchmark and two repeated-sample Gate H measurements explicitly removed.
The normal release gate now:

- Runs the four approved high-value latency targets (`reader_open`,
  `capture_small`, `incremental_delta`, `brief_projection`) with unchanged
  sample counts and thresholds. A separate `make perf-slo` retains the full
  15-target, large-10k-file/memory diagnostic; 11 targets are not in the release
  stamp, explicitly reported as not covered. No missing required core target
  is silently accepted.
- Runs only 7 root, 8 enterprise, 5 client and 4 server test files, covering
  core MCP transport, authority, RBAC, tenant isolation, cryptographic signing,
  sandbox, Authward and installation behavior. All other regression tests remain
  runnable separately; the release does not assert that they passed.
- Keeps 8 critical browser E2E specs rather than 13, and selected high-risk
  Gate G/K matrix rows. The full UI/Matrix tests are available via
  `make review-e2e`, `make gate-g` and `make gate-k`.
- Includes up to four changed files per Python test group in impact mode;
  larger test changes are reported as coverage not exercised in release and
  must be handled by PR/developer testing. Gate G/K, PostgreSQL recovery,
  canaries, signed records and the installed compiled candidate stay required.

**Measured on the desktop, NOT on four CPUs:** the preceding `v0.8.2m`
qualification completed in 42.45 minutes but failed root tests and Gate H.
Those were the untrimmed 52 components. The new 49-component configuration
has not passed end-to-end. The `scripts.release_timing` model is a planning
heuristic using 1.75x historical durations for unchanged stages and explicit
unmeasured budgets for the shortened stages. It currently forecasts **52.2
minutes** with four concurrent components on four cores, but this is **not a
four-CPU benchmark** and has only about 2.8 minutes of modeled gate headroom.
The GitHub job itself enforces four CPUs and a 60-minute maximum; the release
step has a 55-minute limit, and neither timeout nor missing credential can
produce a release stamp. Repeat a complete clean-checkout qualification on
the restricted runner for real runtime evidence.

## Qualify

Before expensive installation/build/qualification, run:

```sh
uv run --locked --extra postgres python -m scripts.release_gate --preflight --tag v0.8.0
```

Then, after resolving preflight problems, run the normal
`make release-qualify tag=v0.8.0` command. Its preflight uses the same inputs. The runner must
provide Helm, kustomize or usable kubectl, the external GitHub canary
credentials, and the two real Runtime Lite provider-usage manifests by
absolute paths in `LEMONCROW_RELEASE_RUNTIME_CREDIT_BASELINE` and
`LEMONCROW_RELEASE_RUNTIME_CREDIT_CURRENT`. No
simulation or synthetic provider usage qualifies as release evidence.
Missing inputs keep the release blocked; use the corresponding component
log in `reports/release/release-report-work/` to diagnose a failing stage.

The gate provisions PostgreSQL 17, matching hosted Cloud SQL and the
PostgreSQL 17 client tools used by the backup/restore drills. The test
installation helper force-isolates HOME, LEMONCROW_HOME, runtime state,
XDG directories and service-manager access. Never run the upgrade drill
against the developer's exported local-server state.

## Remaining release prerequisites and verified resolutions

- **Rolling schema:** schema 40 was already merged on main. The rolling-upgrade
  drill now compares *all parents* of the candidate migrations, and refuses a
  missing adjacent predecessor rather than wrongly examining only the first
  parent of a side-branch merge. The full PostgreSQL 39/40/41 upgrade and rollback
  drill passed on 2026-10-18.
- **GitHub canary:** the release qualification command loads a temporary token
  from the operator's existing authenticated `gh` CLI, unless the operator
  supplied `LEMONCROW_RELEASE_GITHUB_TOKEN`. The controlled default is
  `lemoncrow-lab/lemoncrow-dev`, PR `54` (overridable by the two canary env
  vars). The three real GitHub API tests passed on 2026-10-18. No token is
  stored in a file or committed; the authentication belongs only to that
  invocation.
- **Sandbox tools:** Helm and kustomize MUST exist in the actual system-level
  sandbox PATH. On this releasing machine, they are currently absent from
  `/usr/local/bin`, `/usr/bin`, and `/bin`. Preflight correctly blocks
  the entire gate. The operator has already downloaded official, SHA256-
  verified Helm v3.22.1 and kustomize v5.7.1 to
  `~/.cache/lemoncrow/release-sandbox-tools/`. A host administrator can install
  these verified executables using:

  ```sh
  sudo install -m 0755 ~/.cache/lemoncrow/release-sandbox-tools/helm /usr/local/bin/helm
  sudo install -m 0755 ~/.cache/lemoncrow/release-sandbox-tools/kustomize /usr/local/bin/kustomize
  ```

  The test harness deliberately does not count user-local executables outside
  the sandbox's restricted PATH. A parent Bubblewrap overlay was investigated
  but its nested sandbox capability check skipped the tests; this is NOT a
  substitute for installing the executables on the runner. Re-run the two
  real artifact-analysis tests before accepting the gate.
- **Runtime Lite cost/quality:** the `claude` CLI reports genuine provider
  token usage, but a coding-model smoke call is not paired, independently
  verified Runtime Lite task evidence. The release still requires five
  matched successful tasks comparing two real commits; its preflight must
  reject missing, malformed, unpaired, or off-commit evidence. Do not mint a
  manifest from the CLI smoke call.

The release remains fail-closed until **all** components and the full
qualification pass. Do not tag or restart the developer's running servers.


## Qualification risk-tier reduction (2026-11-01)

The October 27 evidence lists 19,731 JUnit testcase executions, with 1,248
repeated identifiers between independent gates. Seven complete root pytest
files, totaling 280 measured cases, are repeated verbatim by the **always
required** G1, G8 and signing/landing gate components. The full root-suite
runner now omits those seven *second executions* only when the release gate
explicitly sets `LEMONCROW_RELEASE_DEDUP=1`. The required independent gate
components continue to run with the same assertions, and ordinary developer
`make test` retains all the files. Impact-selected root test files are not
pruned by this change.

The release's real Chromium E2E component now selects 13 explicit critical spec
files in `frontend/playwright.release.config.ts` rather than every UX scenario.
The October 27 inventory shows 35 of 64 historical cases covered by those
spec files, a reduction of **29 browser cases** for typical broad-release runs.
This is a selection estimate from historical logs, **not** a new run or measured
wall-time result. The excluded 29 tests are still in the developer's exhaustive
`make review-e2e` suite for targeted investigation. New browser-spec-only
changes are still selected directly by the Git-impact gate, even if outside the
critical preset. Do not interpret a reduced release suite as evidence that all
64 browser scenarios passed.

The GitHub tag workflow now passes `--changed-since-last-release`, matching the
existing `make release-qualify` local policy, while the scheduled and manual
main runs remain exhaustive. All performance SLOs, migrations/restore, security
and signing, canary, compiler and installer checks remain mandatory. See
`docs/operations/release-runner.md` to register an isolated self-hosted runner.

## Measured improvements

These are representative measurements on the development host, not a promise
of identical wall time on a loaded CI runner.

| Stage or target | Previous full gate | Optimized validation |
|---|---:|---:|
| Enterprise server tests | ~3,593 seconds | 4,945 tests in ~737 seconds |
| Public server tests | ~289 seconds | 528 tests in ~78 seconds |
| Small local Review capture p95 | 1,085 ms | 204 ms (620 ms unchanged limit) |
| Review preparation browser tests | Four failures | Four passes |

The small capture speedup removes a fixed 1s asynchronous-job poll:
the first local poll uses a short hint and subsequent polls back off.
Browser and performance stages remain exclusive to protect reliable
measurements. Full release qualification is required before tagging.

## Default release policy: test changes since the last published version

`make release-qualify tag=vX.Y.Z` now passes
`--changed-since-last-release` to the **real G11 release gate** (preflight and
execution). It locates the highest versioned Git tag below the requested tag
that is reachable from the candidate commit, verifies that tag against
`origin`, then diffs that exact commit against the candidate. Deleted files
count as changes, not evidence of safety. Unmapped executable inputs and
ambiguous module/test dependencies trigger broader test coverage, not omission.

The release report includes the baseline tag/SHA, candidate SHA, all changed
paths, the selected component/test files, and the policy digest. Changed
Python modules select their related test files using module names and imports;
unresolvable dependencies fall back to the entire owning suite. Browser and
UI-unit-test-only edits run only their corresponding spec/test files; product
UI changes run the full affected frontend/browser suites. Large rewrites use
the existing full-suite runner with its purpose-built isolated shards. A
component not impacted by the diff is recorded `not_affected`, **never
`pass`**. This is not a skip of a test that was supposed to run.

Regardless of the changes, the policy always requires the versioned
distribution build, installed/compiled candidate and upgrade checks, core
security/canary/signature checks, PostgreSQL upgrade/PITR/failover, egress
controls, agent CLI checks, and installer rehearsal. All selected tests must
pass with unchanged thresholds. The release stamp independently recomputes
the Git impact plan, checks every selected/unaffected component, demands a
clean candidate checkout, and refuses a forged, stale, partial or
other-commit report. `--only` stays nonreleasable.

Inspect the decision before qualification with:

```bash
uv run --locked python -m scripts.release_gate --impact-plan --tag v0.8.0
```

Force the previous exhaustive policy for a major release or an audit:

```bash
LEMONCROW_FULL_SUITE=1 make release-qualify tag=v0.8.0 RELEASE_FULL=1
```

**Important for v0.8.0:** the current last published tag is v0.7.4,
and this candidate changes more than 4,000 files across nearly all product
areas and test suites. This is genuinely a broad release, so an honest
impact selector will still require most or all components. The savings become
substantial for later incremental releases where the Git diff is small.
The policy does not reinterpret unreleased intermediate commits as published
release baselines.


## Fast candidate verification (not a release)

Running all release components after **every fix** is unnecessarily expensive. Use
`make release-quick tag=v0.8.0` for candidate iteration instead. It calls
`scripts.release_quick`, which selects 25 **existing** components of
`scripts.release_gate` and its dependency closure. Those components execute
normally in a clean, disposable checkout, using fresh PostgreSQL/Redis where
needed; no check is mocked, automatically skipped, or given a relaxed limit.

The fast profile covers locked environments, static checks, the public server
and both frontend unit suites, hosted artifact smoke, status/support evidence,
Gate I, credit-gate regression tests, PostgreSQL rolling upgrade and PITR,
network-egress checks, and real candidate installation/compiled verification.
It intentionally does **not** rerun the roughly 10k-case root suite, 5k-case
enterprise suite, complete Chromium E2E, hosted concurrency/load/failover
matrix, live GitHub canary, fresh Runtime credit measurement, or all G9
performance SLOs.

The resulting `reports/release/release-quick.json` is explicitly partial,
**never releasable**, and cannot be used by `release_record stamp`. The quick
command accepts exit code 2 from the underlying release gate only when the
report names the exact candidate SHA, proves a clean start/end tree, and shows
all 25 selected components passing. Missing or changed components, a dirty
tree, cleanup errors, or a missing report fail the quick command.

Recommended workflow: iterate with `release-quick`; run focused E2E tests
for the Review surfaces you changed; then **once** the candidate is stable run
`LEMONCROW_FULL_SUITE=1 make release-qualify tag=v0.8.0`. Only that
complete, fully releasable report may stamp/tag a release. Run comprehensive
suites in CI on each merge or overnight so missing coverage is surfaced well
before release preparation. Do not represent a quick green result as a
qualified v0.8.0 release.
