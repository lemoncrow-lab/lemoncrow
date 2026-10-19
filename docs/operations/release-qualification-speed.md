# Release qualification: concurrency, evidence, and isolation

The release gate defaults to five concurrent **components** (`--jobs 5`).
It keeps declaration-ordered durable reports but streams timestamped START,
DONE, and BLOCKED events immediately, so a long test suite does not hide
completed stages.

The release gate does not run all tests concurrently. Resource locks prevent
the two heavy Python suites from competing; Redis users serialize, and the
Review browser and performance measurements are exclusive. The enterprise
suite uses five pytest workers with a separate disposable PostgreSQL database
per worker. The public server suite uses four file-distributed workers.

## Qualify

Before expensive installation/build/qualification, run:

```sh
LEMONCROW_FULL_SUITE=1 uv run --locked --extra postgres python -m scripts.release_gate --preflight --tag v0.8.0
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
