# Changelog

All notable changes to LemonCrow are documented here. This project adheres,
loosely, to [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `opencode` git submodule (`lemoncrow-lab/lemoncode`, branch `dev`) alongside the
  existing `landing` submodule — both are kept.
- LemonCode CLI host/control plane: `coding_engine.py`, `interactive.py`,
  `lemoncode_host.py`, plus large `runtime.py` / `commands/code.py` updates.
- OpenAI gateway Anthropic + Responses surfaces:
  `openai_gateway/anthropic.py`, `openai_gateway/responses.py`.
- Savings optimization runtime modules:
  `cache_economics`, `evidence_reuse`, `local_retrieval`, `routing_calibration`,
  `runtime_decisions`, plus `primer_cache` / `runtime_policy`.
- Local IPC helper: `src/lemoncrow/infra/ipc/` (HTTP over UDS).
- Docs: `docs/savings-optimization-roadmap.md`.
- Bench helpers: `scripts/prep_cursor_ide_manual_bench.sh`,
  `scripts/prove_cursor_index_parity.sh`, codebench fixtures.
- Tests covering LemonCode CLI, optimization runtime, and gateway expansions.

### Changed
- **One edit engine for the thin client and the main package:** `edit` now runs
  `lemoncrow_client.kit.edit` everywhere. The thin client gains pre-batch line
  numbers for batched range edits (it used to rewrite the wrong lines),
  whole-batch rollback, typography- and placeholder-tolerant anchors with a
  `retry_with` hint on a miss, the Python parse gate, protected paths and the
  test-contract guard. `new: ""` on a line range now deletes those lines, and a
  `{path, new}` edit creates a missing file or rewrites a plausible whole file.
  Both surfaces now refuse a range that starts past the end of the file and no
  longer report a blank or repeated `new` as "already applied".
- **One grep engine and read-path grammar for the thin client and the main
  package:** the client's `grep` now answers with the main package's engine
  (`lemoncrow_client.kit.search`): ranked file maps, context windows,
  docstring collapse, `path:Lx-Ly` scoping, a token budget with a spill file,
  and a literal match when the regex does not compile. It still never shells
  out and searches only what the manifest walk lists. The offline
  `read_from_disk` takes the read tool's path grammar (`:10-20`, `:L10-`,
  chained suffixes, `head=`/`tail=`).
- **One sql engine for the thin client and the main package:** the client's
  `sql` now runs `lemoncrow_client.kit.sql`, with the main package's contract:
  all eight actions, discovery from `DATABASE_URL`/`.env` only with
  `LEMONCROW_SQL_AUTODISCOVER=1` (it no longer picks up LemonCrow's own
  `LEMONCROW_DATABASE_URL`), writes only with `write=true` and the operator's
  `LEMONCROW_SQL_ALLOW_WRITES=1`, auto-`LIMIT`, and cells over 4 KB spilled to a
  file.
- **sql reads cannot write:** a read opens the database read-only, so
  `PRAGMA journal_mode(wal)` no longer switches it to WAL and a mistyped DSN no
  longer creates an empty database file. A relative DSN opens the file under
  the repository root the sandbox checked, not one relative to the process's
  working directory. `LEMONCROW_SQL_ALLOW_WRITES=0` and
  `LEMONCROW_SQL_ALLOW_EXTERNAL_DB=0` now mean off.
- **sql lint checks the schema:** it compiles each statement, and each
  `queries[]` item, with `EXPLAIN`, so unknown tables and syntax errors fail
  lint instead of the query. Write refusals name `write=true` and the operator
  switch, writes report `rows_affected`, and `.env` `export KEY=` lines are
  read.
- **One ast-grep adapter and SAST scanner for the thin client and the main
  package:** the client's `scan` runs the bundled OWASP/CWE rule pack and the
  Python taint check (it used to run the project's own ast-grep rules), and
  without ast-grep still runs the taint check and says the rule pack was
  skipped (`summary.rules_skipped`). The client's `codemod` previews a rewrite
  unless `dry_run=false`; it used to apply by default. Neither downloads
  ast-grep; both honour `LEMONCROW_AST_GREP_BIN`.
- **codemod and scan results no longer depend on ast-grep's thread timing:**
  matches come back in path order, so `limit` keeps the same matches and a
  rewrite diff lists files the same way every run. codemod reports 1-based
  lines (it showed ast-grep's 0-based ones) and a truncated search reports how
  many matched, not how many it returned.
- **blame shows churn by default:** the client's `include_churn` defaults to
  true, as the contract says, and heads line-level blame with the commits that
  touched the path and lines per author instead of replacing it.
- **grep refuses an unknown `type` filter** instead of silently searching
  every file, and gains `go`, `rust`, `toml` and `shell`.
- **grep results no longer depend on ripgrep's thread timing:** the fast path
  orders files by path, so `file_limit` keeps the same files every run.
- **One bash command policy for the thin client and the main package:** the
  client's `bash` now applies `lemoncrow_client.kit.command_policy`, as the main
  package does. It refuses `git reset --hard` and `git clean -fd` however they
  are wrapped, and shell writes outside the worktree and the temp dir. It reads
  literal files with `cat`/`head`/`tail`/`wc` in-process, and seeks for
  `od <big file> | tail`.
- **One bash output pipeline for the thin client and the main package:**
  the client's `bash` output now runs through `lemoncrow_client.kit.bash_output`,
  as the main package's already does: a test run is cut to its failures and
  summary, a long log to the lines around an error, secrets are redacted, an
  identical rerun becomes a one-line `unchanged` marker, and anything trimmed is
  spilled to a file named in the footer. On a 1,500-case corpus the client
  returns 84% fewer characters than its old 256 KB head cut. Rerun history and
  created-file tracking are now kept per session, so sessions that the remote
  MCP hosts together no longer share them. The `unchanged` marker now redacts
  its first line.
- **Cursor messaging (CLI vs IDE):** measured SWE-bench Lite (10 tasks,
  `cursor-grok-4.5-high`): Cursor CLI + LemonCrow ~40% cheaper than Cursor CLI
  baseline (tokens −39.8%, cost −41.2%); Cursor IDE did not match that saving.
  Updated in `README.md`, `docs/marketing/landing-copy.md`, and landing
  `Hero.tsx`.
- README roadmap retitled to **Savings Optimization** (LemonCode + six runtime
  levers); details in `docs/savings-optimization-roadmap.md`.
- OpenAI gateway adapter/app/serve/schemas expanded; `docs/openai-gateway.md`
  and `docs/cli.md` refreshed.
- MCP server/bridge/daemon/HTTP and Cursor hooks
  (`before_read_file` / `before_shell_execution` / `before_tool_use`) hardened.
- Install/uninstall/smoke scripts for Claude, Codex, Cursor, OpenCode; MCP
  plugin configs updated.
- Codebench in-container runner and Harbor cursor agent wiring; Dockerfile /
  `pyproject.toml` / `hatch_build.py` packaging tweaks.
- Core service API + `frontend/src/api.ts` for optimization surfaces.
- Landing `Benchmarks.tsx`: link to Opus 5 Harbor job (no baseline yet).

### Submodules
- `.gitmodules` retains **both** `landing` and `opencode`.

### Notes / do not ship
- `benchmarks/codebench/.tree-sitter-cache/` is a local cache artifact — exclude
  from commits.

---

## Prior unreleased — Maintenance-mode / open-source transition

### Changed
- **Remote telemetry is now ON by default.** Opt out with `lc telemetry remote
  off`, or set `DO_NOT_TRACK=1` / `LEMONCROW_TELEMETRY=off`. What is sent is
  unchanged: aggregate counts, durations, and dollar estimates plus a hashed
  install key — never source code, prompts, or file paths.
- LemonCrow now runs **fully locally with no account**. Login, subscription
  checks, savings caps, and device registration no longer gate any feature.
- **Pure-Python is now the default, supported build.** The `mypyc` compile is
  experimental and opt-in (`LEMONCROW_ENABLE_MYPYC=1`); a compiled build should
  only be published once CI verifies it per-platform.
- **Commit co-author attribution is OFF by default** (opt in with
  `LEMONCROW_ATTRIBUTION=1`); the git hook is removed by `scripts/uninstall.sh`.
- The `lemoncrow.pro` engine is now **open source (Apache-2.0)** and shipped as
  readable source; `mypyc` compilation is an optional performance step.
- Relicensed the whole project to **Apache-2.0** (previously open-core with a
  proprietary compiled engine). Added `NOTICE`.
- Remote telemetry is now **off by default** (opt-in via `lc telemetry remote
  on`). Added a `DO_NOT_TRACK` / `LEMONCROW_TELEMETRY=off` env kill switch. The
  public savings rollup is now gated behind the same opt-in.
- Startup git auto-update for source installs is now **opt-in**
  (`LEMONCROW_AUTO_UPDATE=1`) instead of on by default.
- `lc init` no longer prompts for or requires login.

### Removed
- The savings-cap dormancy gate (the MCP server never hides tools).
- Automatic account provisioning / seamless background browser login.
- The hardware-derived device identifier; any local id is now a random,
  resettable `installation_id`.
- Automatic usage reporting and entitlement checks against a hosted server.

### Security
- Retained the local service API key and the OpenAI-gateway bearer token
  (local security), fully separated from any account identity.

See `docs/maintenance-mode-transition.md` for the full audit and rationale.
