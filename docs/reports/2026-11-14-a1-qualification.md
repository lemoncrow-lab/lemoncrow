# `lc verify init` on 20 public repositories (2026-11-14)

**Result: 8 of 20 pass. The bar is 15, so `lc verify init` is not qualified.** The baseline client passed 5. The fixes below account for the other 3 (attrs, commander, lz4). None of the 12 failures is a command `lc verify init` reads wrongly. They fail because nothing can install the repository's dependencies, because the official image lacks a tool, or because of the repository's own environment.

The criterion: on 20 public repositories across Python, Node, Go, Rust and Java, the written file loads, and the checks it selects run and pass at the default branch's head on at least 15 of them, run in containers rather than on a workstation. A repository passes when the file loads and every check it keeps passes.

## Method

- **Choice.** The 20 repositories were chosen and recorded before any run, and none was swapped out afterwards. The mix is 5 Python, 5 JavaScript or TypeScript, 3 Go, 3 Rust, 2 JVM (Java with Maven, Kotlin with Gradle), 1 C and 1 Ruby. Between them they cover Makefile-, `package.json`-, GitHub Actions- and `pyproject.toml`-driven projects. Each was shallow-cloned (`git clone --depth 1`) at its default branch's head on 2026-11-14.
- **Init.** `lc verify init --toolchain IMAGE` ran on the host against each clone, which it only reads. Nothing from a clone ran on the host. Two clients were used:
  - the baseline: `main` at `aa48938d3`;
  - the fixed client: this change.
- **Images.** Each repository got an official public image, pinned by the digest init resolved:
  - Python: `ghcr.io/astral-sh/uv:python3.12-bookworm` for uv projects, `python:3.12` otherwise;
  - Node: `node:24`;
  - Go: `golang:1.27`, and `golangci/golangci-lint:v2.15` for cobra, whose `make lint` runs it;
  - Rust: `rust:1.100`;
  - JVM: `maven:3-eclipse-temurin-21`, `eclipse-temurin:21`;
  - C and Ruby: `gcc:15`, `ruby:3.4`.

  Docker Hub's anonymous pull limit was exhausted on this network. Its images were pulled through mirrors that serve the same digests: library images through `public.ecr.aws/docker/library`, and golangci-lint through `mirror.gcr.io`.
- **Containers.** Docker ran in a Linux VM on an Apple-silicon machine (linux/arm64, 6 CPUs, 16 GB). Each repository got a fresh container from its pinned image, limited to 4 CPUs and 10 GB. Its only volume was a named one holding a copy of the clone at `/w`. There were no host mounts, no Docker socket, no credentials and no `--privileged`, and the network was open for installs. Commands ran as uid 1000, which owns the checkout, with `HOME` under `/tmp`, as a rootless Podman server with `--userns=keep-id` runs them. `LC_CHANGED` listed every tracked file.
- **Order and time.** The file's `[[dependencies]]` installs ran first, as a Workspaces server runs them, with its 1800 s limit. The checks then ran in file order, with 600 s of check time per repository. A check still running when that time ran out was stopped, and later checks did not run.
- **A discarded first pass.** An earlier baseline pass ran as root. Root made permission tests pass that expect to fail (cobra, starlette) and tripped Git's ownership check (click's pre-commit). Both runs reported here ran as uid 1000.

## Results

| Repository | Language | Driver | Head | Image | Baseline | Fixed |
| --- | --- | --- | --- | --- | --- | --- |
| pallets/click | Python | pyproject, uv.lock, tox | `e38188e9e` | uv:python3.12-bookworm | fail | fail |
| psf/requests | Python | Makefile, no lockfile | `3c7baaa05` | python:3.12 | fail | fail |
| Textualize/rich | Python | Makefile, uv.lock | `3a109ccdf` | uv:python3.12-bookworm | fail | fail |
| encode/starlette | Python | pyproject, uv.lock | `271c277da` | uv:python3.12-bookworm | pass | pass |
| python-attrs/attrs | Python | pyproject, uv.lock, tox in CI | `2a1fb86d4` | uv:python3.12-bookworm | fail | pass |
| expressjs/express | JavaScript | package.json, no lockfile | `43fca52d2` | node:24 | fail | fail |
| tj/commander.js | JavaScript | package.json, package-lock.json | `a5a7dc084` | node:24 | fail | pass |
| chalk/chalk | JavaScript | package.json, no lockfile | `72110019e` | node:24 | fail | fail |
| pmndrs/zustand | TypeScript | package.json, pnpm-lock.yaml | `9fed2eb2f` | node:24 | fail | fail |
| unjs/ofetch | TypeScript | package.json, pnpm-lock.yaml | `1dbc37fd1` | node:24 | fail | fail |
| spf13/cobra | Go | Makefile | `adbc88139` | golangci-lint:v2.15 | pass | pass |
| go-chi/chi | Go | Makefile | `c64ed7d66` | golang:1.27 | pass | pass |
| charmbracelet/lipgloss | Go | GitHub Actions | `ff16a4ef6` | golang:1.27 | pass | pass |
| dtolnay/anyhow | Rust | GitHub Actions, no Cargo.lock | `87c857132` | rust:1.100 | fail | fail |
| sharkdp/hyperfine | Rust | GitHub Actions, Cargo.lock | `c51615249` | rust:1.100 | fail | fail |
| clap-rs/clap | Rust | GitHub Actions, workspace | `ec93e7125` | rust:1.100 | fail | fail |
| google/gson | Java | Maven via GitHub Actions | `d938a10f7` | maven:3-eclipse-temurin-21 | pass | pass |
| square/kotlinpoet | Kotlin | Gradle via GitHub Actions | `5a120729f` | eclipse-temurin:21 | fail | fail |
| lz4/lz4 | C | Makefile, GitHub Actions | `08cb1a67e` | gcc:15 | fail | pass |
| rack/rack | Ruby | Rakefile, GitHub Actions | `da90464e3` | ruby:3.4 | fail (no file) | fail |

**Totals.**

| | Files written | Files that load | Pass |
| --- | --- | --- | --- |
| Baseline | 19 of 20 | 19 of 20 | 5 of 20 |
| Fixed | 20 of 20 | 20 of 20 | **8 of 20** |

By language, the fixed client passes:

| Language | Pass |
| --- | --- |
| Python | 2 of 5 |
| JavaScript and TypeScript | 1 of 5 |
| Go | 3 of 3 |
| Rust | 0 of 3 |
| JVM | 1 of 2 |
| C and Ruby | 1 of 2 |

## Why the 12 fail

1. **Nothing installs the dependencies: 6 repositories** (requests, express, chalk, rack, zustand, ofetch).
   - `[[dependencies]]` is the format's only setup step, and it names a lockfile.
   - express, chalk and requests commit none: requests' `requirements-dev.txt` is a range list, not hash-pinned. rack has a Gemfile but no `Gemfile.lock`.
   - zustand and ofetch have `pnpm-lock.yaml`. Review refuses a file that declares any lockfile but `package-lock.json`, `uv.lock` or a hash-pinned `requirements*.txt`, so init does not declare it. It writes a note instead.
   - Each check therefore starts without its tools: `eslint: not found`, `No module named pytest`, `Could not find gem`. node:24 also has no `pnpm` on `PATH`.
   - This is the format's gap, not init's. Covering these 6 would take one of two things: Review provisioning more lockfiles, or a setup step that a server runs and Review either runs or skips with a stated reason. Neither is in the format, and writing `npm install` into a check would rewrite the working tree.
2. **The official image lacks a tool: 4 repositories.**
   - `rust:1.100` installs Rust's minimal profile, which has no rustfmt or clippy. That fails `cargo fmt --check` (hyperfine, clap) and `cargo clippy` (anyhow).
   - `eclipse-temurin:21` has no `libatomic.so.1`, which the Node that Gradle downloads for kotlinpoet's Wasm target needs.
   - The format cannot add a tool to an image. A repository needs its own `[toolchain]` image, which init does not build.
   - A supplementary run added the missing pieces to the same images. It is not counted above. Its outcomes:
     - anyhow, hyperfine and clap passed every check once rustfmt and clippy were added;
     - kotlinpoet passed `./gradlew build` in 533 s, inside the time-box, once libatomic1 was added.

     With images that have these tools, 12 of 20 would pass. The bar would then still need 3 of the 6 repositories in finding 1.
3. **The repository's own environment fails in a fresh container: 2 repositories.**
   - click:
     - Its locked pyright 1.1.408 fetches Node through nodeenv, which cannot parse Node's `27.0.0-alpha` version string.
     - Its tox test environments fail 216 cases of `test_echo_via_pager`, because the image has no `less`.
   - rich: its `.python-version` asks for 3.15, so uv fetches 3.15.0a5. pyyaml 6.0.3, which pre-commit needs, has no wheel for that version and does not build from source.

## Fixes

Each fix has a unit test in `client/tests/test_verify_init.py`. All five new tests fail on the baseline client.

1. **Options of `make` in CI steps are read as `make` reads them** (`verify_init.py:272`, the `-j` count at `:285`).
   - The baseline took the word after `-j` as its count, even when it was `-C`.
   - So each of lz4's `make -j -C tests <target>` steps became a check of a target named `tests`: 13 extra checks, 7 of which failed and 1 timed out.
   - `-j`, `-l` and `--max-load` now take a count only when one follows. lz4 keeps `make check`, `make test` and `make -C ossfuzz test`, and passes.
   - Test: `test_make_options_and_rake_in_ci_are_read_as_they_run`.
2. **`rake` in a CI step is a test** (`verify_init.py:404`). This covers its default task, `test` or `spec`; any other task, such as `rake release`, is not a check. rack had no file before (init exited 2). It now gets `bundle exec rake`, which fails only for want of an install (finding 1). Same test.
3. **A CI step that runs only scripts already in the file is left out, and `check:lint` and `check:type` have kinds** (`verify_init.py:98`, `:104`; `runs` in `detect`, `:1239`).
   - commander's CI runs `npm run check`, which runs `check:type`, `check:lint` and `check:format`. The baseline kept that aggregate, without a kind.
   - The CI step is now seen through the script it runs, and the two scripts are lint and typecheck checks.
   - Test: `test_a_ci_step_that_runs_only_scripts_already_in_is_left_out`.
4. **With a lockfile, a `pyproject.toml` tool must be installed where it runs** (`_development_requirements` `:995`, `_dependency_groups` `:1017`, `_uv_selector` `:1034`, `prefix` in `_from_pyproject` `:1095`).
   - The baseline wrote `uv run mypy` and `uv run pyright` for attrs because both are configured. Neither is in a group uv installs by default, so both failed with `Failed to spawn`.
   - A tool is now written only when a dependency list names it. `uv run` adds `--group G` or `--extra E` when only a non-default group or an extra has it. This follows `include-group` and `[tool.uv] default-groups`, and the legacy `dev-dependencies`.
   - attrs now gets `uv run --group pyright pyright`. mypy is left out, because no dependency list names it: the `mypy` group gets it only through pytest-mypy-plugins. attrs passes.
   - Test: `test_with_a_lockfile_a_tool_runs_only_where_the_project_installs_it`.
5. **A build manifest gives a test when nothing else did** (`_from_manifests` `:1144`, after pyproject in `detect` `:1234`).
   - `go.mod` gives `go test ./...`, `Cargo.toml` gives `cargo test`, `pom.xml` gives `./mvnw -B test` (`mvn` without the wrapper), and a Gradle build gives `./gradlew check`.
   - hyperfine's CI test step uses an expression and is skipped, so hyperfine now gets `cargo test` from `Cargo.toml` (it passes). A manifest adds nothing when a Makefile target or CI step already runs that tool.
   - Test: `test_a_build_manifest_gives_a_test_only_when_nothing_else_did`.
6. **Root lockfiles that Review provisions become `[[dependencies]]`** (`_DECLARED_LOCKFILES` `:1180`, `_dependencies` `:1198`, `render` `:1286`, read-back `:1532`).
   - `package-lock.json` gets `npm ci`, and `uv.lock` gets `uv sync --frozen`. A server installs them before the checks; init prints the install it wrote.
   - Any other lockfile is only named in a note, since declaring it would make Review refuse the run.
   - commander passes because of this. starlette, attrs and click now install up front instead of inside the first `uv run`.
   - Test: `test_root_lockfiles_review_provisions_become_dependencies`.

`docs/reference/environment.md` describes the five sources and the dependencies.

## Gaps left in `lc verify init`

The run showed these, but they did not decide any repository's result:

- **One check per tool, not per environment or task.**
  - tox, nox and Gradle invocations share one key per tool, so attrs keeps `tox run -e docset` (a docs build) as its only test-kind check. Its `tox run -f lint`, `tox run -f typing` and `tox run -e docs-doctests,changelog` are dropped.
  - Its test suite is not in the file at all: the CI step runs `-e $PYTHON-tests`, an expression, so it is skipped. attrs passes on a file that runs no tests.
  - kotlinpoet's `./gradlew :kotlinpoet:check` likewise folds into `./gradlew build`.
- **pre-commit with fixing hooks.** click's `pre-commit run --all-files` includes `ruff format` and whitespace fixers. When they find something, they rewrite the working tree before failing. Init keeps `pre-commit run` as a check.
- **A configured tool without a lockfile.** With no lockfile, a tool `pyproject.toml` configures is written as `python3 -m TOOL`, even when nothing installs it. requests gets `python3 -m ruff`, although its ruff runs only through pre-commit. requests fails on finding 1 regardless.
- **Hash-pinned `requirements*.txt`**, which Review can provision, is not written as a dependency. None of the 20 has one.
- **The Python version is the repository's, not the image's.** `uv` follows `.python-version` (rich), so the toolchain image's Python does not settle it.
- **Architecture.** The containers were arm64, and lz4's CI also builds with `-m32` and `-m64`, which are x86-only. The fixed reading no longer turns those steps into checks, but a repository whose own check is x86-only would fail on an arm64 server.

## Per repository

The times are the checks' wall-clock seconds. In the baseline, `uv run` built the environment inside the first check (for example, ruff's 61 s in click). In the fixed run, the `[[dependencies]]` install did that first.

### click

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `ruff` | `uv run ruff check .` | pyproject.toml [tool.ruff] | pass 61s | pass 0s |  |
| `mypy` | `uv run mypy` | pyproject.toml [tool.mypy] | pass 2s | pass 1s |  |
| `pyright` | `uv run pyright` | pyproject.toml [tool.pyright] | fail 1s | fail 1s | pyright 1.1.408 fetches Node through nodeenv, which cannot parse Node's `27.0.0-alpha` version (`ValueError: invalid literal for int()`) |
| `tox` | `uv run --locked --no-default-groups --group dev tox run` | .github/workflows/nightly.yaml | fail 281s | fail 251s | every test env fails 216 cases of `test_echo_via_pager`: the image has no `less`; the `typing` env fails on pyright as above |
| `pre-commit` | `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` | .github/workflows/pre-commit.yaml | pass 2s | pass 2s |  |
| install `uv sync --frozen` |  | uv.lock | — | pass 23s |  |

### requests

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `ruff-format` | `python3 -m ruff format --check .` | pyproject.toml [tool.ruff] | fail 0s | fail 0s | nothing installed: no lockfile, so no `[[dependencies]]` (`No module named ruff`) |
| `ruff` | `python3 -m ruff check .` | pyproject.toml [tool.ruff] | fail 0s | fail 0s | as above (`No module named ruff`) |
| `pyright` | `python -m pyright src/requests/` | .github/workflows/typecheck.yml | fail 0s | fail 0s | as above (`No module named pyright`) |
| `test` | `make test` | Makefile target "test" | fail 0s | fail 0s | as above (`No module named pytest`) |
| `pre-commit` | `pre-commit run --show-diff-on-failure --color=always --all-files` | .github/workflows/lint.yml | fail 0s | fail 0s | as above (`pre-commit: not found`) |

### rich

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `format-check` | `make format-check` | Makefile target "format-check" | fail 64s | fail 0s | the same environment build fails under `uv run` |
| `typecheck` | `make typecheck` | Makefile target "typecheck" | fail 0s | fail 0s | as above |
| `test` | `make test` | Makefile target "test" | fail 0s | fail 0s | as above |
| install `uv sync --frozen` |  | uv.lock | — | fail 52s | `.python-version` asks for 3.15; uv fetches 3.15.0a5, and pyyaml 6.0.3 has no wheel for it and does not build (`Cython does not appear to be installed`) |

### starlette

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `ruff` | `uv run ruff check .` | pyproject.toml [tool.ruff] | pass 133s | pass 0s |  |
| `mypy` | `uv run mypy starlette` | pyproject.toml [tool.mypy] | pass 2s | pass 2s |  |
| `pytest` | `uv run pytest` | pyproject.toml [tool.pytest] | pass 12s | pass 13s |  |
| install `uv sync --frozen` |  | uv.lock | — | pass 20s |  |

### attrs

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `ruff` | `uv run ruff check .` | pyproject.toml [tool.ruff] | pass 57s | pass 0s |  |
| `mypy` | `uv run mypy src` | pyproject.toml [tool.mypy] | fail 0s | — | `Failed to spawn: mypy`: no default group installs it, and no dependency list names it (fixed: left out) |
| `pyright` | `uv run --group pyright pyright` | pyproject.toml [tool.pyright] | fail 0s | pass 64s | `Failed to spawn: pyright`: only the `pyright` group has it (fixed: `--group pyright`) |
| `tox` | `uv run --only-group tox tox run -e docset` | .github/workflows/build-docset.yml | pass 19s | pass 51s |  |
| install `uv sync --frozen` |  | uv.lock | — | pass 104s |  |

### express

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `lint` | `npm run lint` | package.json script "lint" | fail 1s | fail 0s | no lockfile, so nothing installs (`eslint: not found`) |
| `test` | `npm run test` | package.json script "test" | fail 0s | fail 0s | as above (`mocha: not found`) |

### commander

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `check-format` | `npm run check:format` | package.json script "check:format" | fail 2s | pass 2s | no install step (`prettier: not found`); fixed declares `package-lock.json` |
| `test` | `npm run test` | package.json script "test" | fail 13s | pass 6s | no install step (`tsd: not found`) |
| `check` | `npm run check` | package.json script "check" | fail 1s | — | no install step (`tsc: not found`) |
| install `npm ci` |  | package-lock.json | — | pass 68s |  |
| `check-lint` | `npm run check:lint` | package.json script "check:lint" | — | pass 4s |  |
| `check-type` | `npm run check:type` | package.json script "check:type" | — | pass 8s |  |

### chalk

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `test` | `npm run test` | package.json script "test" | fail 0s | fail 0s | no lockfile, so nothing installs (`xo: not found`) |

### zustand

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `test` | `pnpm run test` | package.json script "test" | fail 0s | fail 0s | `pnpm: not found`; and `pnpm-lock.yaml` cannot be declared (Review refuses it) |

### ofetch

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `lint` | `pnpm run lint` | package.json script "lint" | fail 0s | fail 0s | as zustand |
| `test` | `pnpm run test` | package.json script "test" | fail 0s | fail 0s | as zustand |

### cobra

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `lint` | `make lint` | Makefile target "lint" | pass 8s | pass 8s |  |
| `test` | `make test` | Makefile target "test" | pass 4s | pass 2s |  |

### chi

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `test` | `make test` | Makefile target "test" | pass 56s | pass 57s |  |

### lipgloss

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `go-test` | `go test -race -covermode atomic -coverprofile=profile.cov ./...` | .github/workflows/coverage.yml | pass 22s | pass 17s |  |

### anyhow

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `cargo-clippy` | `cargo clippy --tests -- -Dclippy::all -Dclippy::pedantic` | .github/workflows/ci.yml | fail 7s | fail 19s | the official image's minimal profile has no clippy (`'cargo-clippy' is not installed`) |
| `cargo-check` | `cargo check --no-default-features` | .github/workflows/ci.yml | pass 2s | pass 5s |  |
| `cargo-test` | `cargo test` | .github/workflows/ci.yml | pass 9s | pass 10s |  |

### hyperfine

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `cargo-fmt` | `cargo fmt -- --check` | .github/workflows/CICD.yml | fail 0s | fail 0s | the official image's minimal profile has no rustfmt (`'cargo-fmt' is not installed`) |
| `cargo-test` | `cargo test` | Cargo.toml | — | pass 54s |  |

### clap

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `cargo-fmt` | `cargo fmt --check` | .github/workflows/ci.yml | fail 0s | fail 0s | as hyperfine |
| `cargo-check` | `cargo +stable check --workspace --all-features --locked --exclude clap_complete_nushell --exclude clap_bench` | .github/workflows/ci.yml | pass 114s | pass 135s |  |
| `cargo-test` | `cargo test -p clap_complete -F unstable-dynamic` | .github/workflows/ci.yml | pass 20s | pass 18s |  |

### gson

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `maven` | `mvn clean test --projects gson --activate-profiles gson-subset` | .github/workflows/build.yml | pass 118s | pass 71s |  |

### kotlinpoet

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `gradle` | `./gradlew build` | .github/workflows/build.yml | timeout 600s | fail 328s | the Node that Gradle downloads for the Wasm target needs `libatomic.so.1`, which the image lacks (fixed run); the baseline run, which shared the machine with another run, was still compiling at the 600 s time-box |

### lz4

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `check` | `make check` | Makefile target "check" | pass 26s | pass 26s |  |
| `test` | `make test` | Makefile target "test" | pass 155s | pass 150s |  |
| `ossfuzz-test` | `make -C ossfuzz test V=1` | .github/workflows/core-tests.yml | pass 0s | pass 0s |  |
| `tests` | `CFLAGS='-Werror -O1' make -j -C tests test-lz4c32 V=1` | .github/workflows/compilers.yml | fail 0s | — | misread CI step (fixed: not a check); `-m32` is x86 only and the containers were arm64 |
| `ci-tests` | `make -j -C tests test-lz4c V=1` | .github/workflows/core-tests.yml | pass 0s | — |  |
| `tests-2` | `make -j -C tests test-fullbench V=1` | .github/workflows/core-tests.yml | pass 57s | — |  |
| `tests-3` | `make -j -C tests test-fullbench32 V=1` | .github/workflows/core-tests.yml | fail 0s | — | misread CI step; `-m32` on arm64 |
| `tests-4` | `make -j -C tests test-fuzzer V=1` | .github/workflows/core-tests.yml | pass 90s | — |  |
| `tests-5` | `make -j -C tests versionsTest V=1` | .github/workflows/core-tests.yml | fail 10s | — | misread CI step; builds older releases with `-m32` on arm64 |
| `tests-6` | `make -j -C tests abiTests V=1` | .github/workflows/core-tests.yml | fail 10s | — | misread CI step; `-m64` on arm64 |
| `tests-7` | `make -j -C tests test-frametest V=1` | .github/workflows/core-tests.yml | pass 90s | — |  |
| `tests-8` | `make -j -C tests test-frametest32 V=1` | .github/workflows/core-tests.yml | fail 0s | — | misread CI step; `-m32` on arm64 |
| `tests-9` | `CFLAGS=-fsanitize=address LDFLAGS=-fsanitize=address make -j -C tests test-frameMT-quick V=1` | .github/workflows/sanitizers.yml | pass 69s | — |  |
| `tests-10` | `CC=clang CFLAGS=-fsanitize=memory LDFLAGS=-fsanitize=memory make -j -C tests frameMT V=1` | .github/workflows/sanitizers.yml | fail 0s | — | misread CI step; the image has no clang |
| `tests-11` | `CC=clang CPPFLAGS="-fsanitize=thread" make -j -C tests test-lz4 V=1` | .github/workflows/sanitizers.yml | fail 0s | — | misread CI step; the image has no clang |
| `tests-12` | `CFLAGS=-fsanitize=address LDFLAGS=-fsanitize=address make -j -C tests test-frametest test-unittest V=1` | .github/workflows/sanitizers.yml | timeout 91s | — | misread CI step; stopped when the 600 s time-box ran out |

### rack

| Check | Command | From | Baseline | Fixed | Why it failed |
| --- | --- | --- | --- | --- | --- |
| `rake` | `bundle exec rake` | .github/workflows/test.yaml | — | fail 0s | no `Gemfile.lock` and no install, so Bundler finds none of the Gemfile's gems |

## Rerun behind the egress gateway (2026-11-14)

**Result: 7 of 20 count. The bar is 15, so `lc verify init` is still not qualified.** The same 7 repositories count as in the first run: starlette, commander, cobra, chi, lipgloss, gson and lz4. attrs, which passed the first run on a docs build alone, now fails: behind the gateway its pyright cannot fetch Node from `nodejs.org`, and its docs build cannot fetch Python's intersphinx inventory from `docs.python.org`. No other repository changed between pass and fail. For five failing repositories the gateway changed where or why they fail (click, rich, anyhow, clap, kotlinpoet), but each of them also fails for its first-run reason.

The owner set the counting rule for this rerun: a repository counts only if the file loads, every check it keeps passes, and at least one test-kind check actually runs its tests. Under that rule the first run counted 7 too, since attrs ran no tests.

### Method differences from the first run

- **Client.** `main` at `547fc3d5b`, which gives the sandbox the proxy settings for Node, Maven and Gradle and grows the default egress to 14 hosts. `lc verify init --toolchain IMAGE` ran on the host (in-process, through `init_main`, which the command calls) and only read the clone. For all 20 repositories it wrote a file identical byte for byte to the first run's fixed client. The files are today's format, with no `[[setup]]`, no toolchain additions and no hand edits.
- **Heads.** Each repository was fetched from GitHub at the full SHA of the first run's clone (`git fetch --depth 1 origin SHA`), under the same branch name. GitHub still serves all 20 heads.
- **Images.** The same tags through the same mirrors. Each still resolves to the digest the first run pinned.
- **Sandbox.** The real `TaskSandbox.up()` made each sandbox:
  - its two networks, of which the sandbox's is `--internal`;
  - its gateway, configured by `_start_gateway`;
  - the sandbox container, with the environment a task gets (`HOME=/tmp/lc-home`, `proxy_env`, `java_homes_env`) and the files `proxy_files` writes through `write_file`: Maven's `settings.xml` and Gradle's `gradle.properties` under `HOME`;
  - what the server's client gives every sandbox: all capabilities dropped except the kept set, `no-new-privileges`, `--init` and a tmpfs at `/run/lemoncrow`.

  A thin subclass of the server's `OciClient` changed two things the A1 method needs. Each bind mount became a named volume: the clone was copied into one at `/workspace/<name>`, and the gateway's script and config into a read-only one at `/lc`. The sandbox ran as uid 1000 instead of the host user. There were still no host mounts, no Docker socket, no credentials and no `--privileged`.
- **Gateway and egress.** The gateway ran the worktree's `egress_gateway.py` on the helper image `python:3.12`, pinned by digest, as uid 65534 with a read-only root file system and 256 MB. The task's egress was `grant(needs, Ceiling())`. With the default ceiling, that is exactly `DEFAULT_REGISTRIES`: 14 hosts on ports 80 and 443, without `github.com`. No written file asks for a host. A probe in every sandbox found no direct route (`/dev/tcp/1.1.1.1/443: Network is unreachable`).
- **Running.** Installs and checks went through `TaskSandbox.run` with `_deadline`, as `Tasks._install` and `checks.verify_repo` run them. `[[dependencies]]` ran first, 1800 s each. The checks then ran in file order, with 600 s of check time per repository. Each command ran from `/workspace/<name>`; the first run used `/w`. `LC_CHANGED` named `/tmp/lc-verify/changed`, which listed every tracked file. As before, each sandbox had 4 CPUs and 10 GB and ran as uid 1000.
- **Not reproduced.** The server's Git mounts (the task's `.git` file, Git directory and published objects) were not reproduced. The copied clone has its own `.git` directory, so `GIT_CONFIG_COUNT` is 0 instead of one `safe.directory` entry. Neither was the server's cache of installed dependency trees, which a first install misses in any case.
- **Machine and sharing.** An Apple M1 Pro (10 cores, 32 GB) ran colima's VM (linux/arm64, 6 CPUs, 16 GB, Docker 29.5.2). The VM also ran other work: a Postgres container, a Temporal stack, a BuildKit builder, and an application stack whose containers were in restart loops. For the first three minutes two repositories ran at a time, in two sequential lanes; after that one ran at a time, and kotlinpoet ran alone. The whole run took 9 minutes, because most failures are immediate. The longest check was lz4's `make test`: 150.5 s, against 150.3 s in the first run.

### Results

"First run" is the fixed client's result in the table above.

| Repository | Head | Image | First run | Rerun | Counted |
| --- | --- | --- | --- | --- | --- |
| pallets/click | `e38188e9e` | uv:python3.12-bookworm | fail | fail | no |
| psf/requests | `3c7baaa05` | python:3.12 | fail | fail | no |
| Textualize/rich | `3a109ccdf` | uv:python3.12-bookworm | fail | fail | no |
| encode/starlette | `271c277da` | uv:python3.12-bookworm | pass | pass | yes |
| python-attrs/attrs | `2a1fb86d4` | uv:python3.12-bookworm | pass | fail | no |
| expressjs/express | `43fca52d2` | node:24 | fail | fail | no |
| tj/commander.js | `a5a7dc084` | node:24 | pass | pass | yes |
| chalk/chalk | `72110019e` | node:24 | fail | fail | no |
| pmndrs/zustand | `9fed2eb2f` | node:24 | fail | fail | no |
| unjs/ofetch | `1dbc37fd1` | node:24 | fail | fail | no |
| spf13/cobra | `adbc88139` | golangci-lint:v2.15 | pass | pass | yes |
| go-chi/chi | `c64ed7d66` | golang:1.27 | pass | pass | yes |
| charmbracelet/lipgloss | `ff16a4ef6` | golang:1.27 | pass | pass | yes |
| dtolnay/anyhow | `87c857132` | rust:1.100 | fail | fail | no |
| sharkdp/hyperfine | `c51615249` | rust:1.100 | fail | fail | no |
| clap-rs/clap | `ec93e7125` | rust:1.100 | fail | fail | no |
| google/gson | `d938a10f7` | maven:3-eclipse-temurin-21 | pass | pass | yes |
| square/kotlinpoet | `5a120729f` | eclipse-temurin:21 | fail | fail | no |
| lz4/lz4 | `08cb1a67e` | gcc:15 | pass | pass | yes |
| rack/rack | `da90464e3` | ruby:3.4 | fail | fail | no |

**Totals.**

| | Files written | Files that load | Every check passes | Counted |
| --- | --- | --- | --- | --- |
| First run (fixed client, open network) | 20 of 20 | 20 of 20 | 8 of 20 | 7 of 20 |
| Rerun (egress gateway) | 20 of 20 | 20 of 20 | 7 of 20 | **7 of 20** |

By language, the rerun counts Python 1 of 5, JavaScript and TypeScript 1 of 5, Go 3 of 3, Rust 0 of 3, JVM 1 of 2, and C and Ruby 1 of 2.

The test checks of the 7 that count ran these tests:

- starlette: `uv run pytest`, 1423 passed;
- commander: `npm run test`, which runs `node --test` (1430 tests), then `tsd` and `tsc`;
- cobra and chi: `make test`, which runs `go test`;
- lipgloss: `go test -race ./...`;
- gson: `mvn clean test`, 3647 tests;
- lz4: `make check` and `make test`.

### Why the 13 fail

1. **Nothing installs the dependencies: 6 repositories.** requests, express, chalk, zustand, ofetch and rack fail exactly as in the first run, and the gateway denied nothing for them. Their key lines are `No module named pytest`, `eslint: not found`, `mocha: not found`, `xo: not found`, `pnpm: not found`, and `Could not find gem 'minitest-global_expectations' in locally installed gems.`
2. **The official image lacks a tool: 4 repositories.** Behind the gateway, clap and anyhow now fail in more places, and kotlinpoet fails earlier.
   - hyperfine: `'cargo-fmt' is not installed for the toolchain '1.100.0-aarch64-unknown-linux-gnu'`, as before. Its `cargo test` passes, with crates fetched from `index.crates.io` and `static.crates.io`.
   - clap: `cargo fmt --check` fails the same way. `cargo +stable check` now fails too. It names a toolchain the image does not have, and rustup's download is denied: `could not download file from 'https://static.rust-lang.org/dist/channel-rust-stable.toml.sha256'`. The first run downloaded that toolchain, and the check passed in 135 s. `cargo test -p clap_complete` passes.
   - anyhow: all three checks fail before compiling. Its `rust-toolchain.toml` asks for the `rust-src` component, and rustup's download is denied: `could not download file from 'https://static.rust-lang.org/dist/channel-rust-1.100.0.toml.sha256'`. In the first run only clippy failed, because the image lacks it.
   - kotlinpoet: the Gradle wrapper downloads `https://services.gradle.org/distributions/gradle-9.9.1-bin.zip`, which is allowed. That URL redirects to `github.com`, which is denied: `Unable to tunnel through proxy. Proxy returns "HTTP/1.1 403 Forbidden"`, after 0.4 s. The first run got Gradle and failed after 328 s on the missing `libatomic.so.1`. Under the default egress the wrapper cannot download Gradle at all, as `config.py` says of `DEFAULT_REGISTRIES`.
3. **The repository's own environment fails: 2 repositories.**
   - click: tox's py3.12 and py3.11 environments each fail the same 24 cases of `test_echo_via_pager`, because the image has no `less`. Several other things now fail behind the gateway:
     - The 7 environments for other interpreters are skipped instead of run, because uv cannot download those interpreters (`could not find python interpreter with spec(s): cpython3.15`). `releases.astral.sh`, `github.com` and `downloads.python.org` are denied.
     - The `pre-commit` check and tox's `style` environment fail fetching hook repositories: `fatal: unable to access 'https://github.com/astral-sh/ruff-pre-commit/': CONNECT tunnel failed, response 403`. `pre-commit` passed in the first run.
     - `pyright` and tox's `typing` environment fail as before, but now at the download: nodeenv gets `Tunnel connection failed: 403 Forbidden` from `nodejs.org`.
     - tox's `docs` environment fails on the intersphinx inventory: `intersphinx inventory 'https://docs.python.org/3/objects.inv' not fetchable`, a warning that `-W` makes an error.
   - rich: `uv sync --frozen` fails at once. `.python-version` asks for 3.15, and uv's download of that Python from `github.com` is denied: `Failed to download https://github.com/astral-sh/python-build-standalone/releases/download/20260203/cpython-3.15.0a5...`. Every check then fails the same way inside `uv run`. In the first run uv got that Python, and the install failed later on pyyaml.
4. **The gateway alone: attrs.**
   - `uv run --group pyright pyright` fails: nodeenv gets `Tunnel connection failed: 403 Forbidden` from `nodejs.org`, then `RuntimeError: nodeenv failed`. It passed in the first run.
   - `tox run -e docset` fails: the intersphinx inventory from `docs.python.org` is not fetchable, every reference to the standard library becomes a warning, and `-W` stops the build: `build finished with problems, 131 warnings (with warnings treated as errors)`.
   - attrs did not count in the first run either, because its only test-kind check is that docs build.

### Hosts the gateway denied

The counts are the gateway log's `deny` lines.

| Repository | Denied | What it decided |
| --- | --- | --- |
| click | `github.com` (28), `releases.astral.sh` (24), `downloads.python.org` (4), `nodejs.org` (2), `docs.python.org` (1) | the `pre-commit` check; tox's `style`, `docs` and seven interpreter environments; `pyright`. click fails without them too (`less`). |
| rich | `github.com` (16) | the install and every check. rich fails without it too (pyyaml). |
| attrs | `nodejs.org` (1), `docs.python.org` (1) | `pyright` and the docs build: pass to fail |
| anyhow | `static.rust-lang.org` (3) | all three checks |
| clap | `static.rust-lang.org` (1) | `cargo +stable check` |
| gson | `oss.sonatype.org` (2), `repository.apache.org` (1), `central.sonatype.com` (1) | nothing: Maven fetched what it needed from `repo.maven.apache.org`, and the check passed |
| kotlinpoet | `github.com` (1) | the only check |

The gateway denied nothing for the other 13 repositories. The counted repositories used `pypi.org` and `files.pythonhosted.org` (starlette), `registry.npmjs.org` (commander), `proxy.golang.org` (cobra, lipgloss), `sum.golang.org` (cobra) and `repo.maven.apache.org` (gson); chi and lz4 fetched nothing.

### Compared with the hand-run

- **The six repositories that need setup.** The hand-run ran them with hand-added `[[setup]]` and derived images, and 4 passed. With today's format they fail here as in the first run. The gateway denied nothing for any of them: what they lack is an install step, not egress.
- **Maven.** In the hand-run, Maven ignored the proxy variables. With `547fc3d5b`'s `settings.xml`, gson passes behind the gateway (`Tests run: 3647`). The hosts denied for gson are a subset of the hand-run's Maven probe, and none of them decided anything.
- **Gradle.** In the hand-run, Gradle also ignored the proxy variables. With `gradle.properties` the wrapper now reaches `services.gradle.org`. It then stops at `github.com`, as the hand-run's probe with Java proxy properties did.

### What the rerun shows

- The gateway did not cost a counted repository. Every repository that counted in the first run counts again. The one that lost its pass, attrs, did not count before.
- The hosts the gateway denied are toolchain and documentation downloads, not package registries:
  - `static.rust-lang.org`: rustup components and named toolchains;
  - `github.com`: uv's Python builds, pre-commit hook repositories and the Gradle distribution;
  - `releases.astral.sh` and `downloads.python.org`: Python interpreters;
  - `nodejs.org`: pyright's Node;
  - `docs.python.org`: Sphinx's intersphinx inventory.
- Allowing these hosts would not make any repository count. Each failing repository also fails for a first-run reason (rustfmt or clippy missing, no `less`, pyyaml on Python 3.15, no `libatomic.so.1`). attrs would pass again, but it runs no tests.
- No product bug turned up in the code the rerun drove: `init_main`, `TaskSandbox.up`, `proxy_env`, `java_homes_env`, `proxy_files`, `TaskSandbox.run` with `_deadline`, and the gateway. The Gradle result is what the comment on `DEFAULT_REGISTRIES` predicts.
- Reaching 15 still needs what the owner chose to build after B1: the setup step and toolchain additions.
