# Changelog

All notable changes to LemonCrow are documented here. This project adheres,
loosely, to [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **GitHub Action: AI pull request review (early access).** `lemoncrow-lab/lemoncrow@v1`
  reviews each pull request on your own model key and posts one summary comment, inline
  P0/P1 findings and a `LemonCrow Review` status as a bot (event `COMMENT` only). On each
  push it re-reviews only what changed and carries unchanged findings forward without a
  model call. Same engine from the full CLI: `lc review --pr N --ai [--publish-bot]`.
  See [GitHub Action: AI pull request review](docs/setup/github-action.md).

## [0.8.0] - 2026-10-18

### Behaviour changes for local installs

Security hardening changed how the local product behaves. Read this before
upgrading a local install; most items need no action.

- **A bare install is Local Review.** `curl -fsSL https://lemoncrow.com/install.sh | bash`
  installs `lc`, `lcr` and a loopback Review server on port 7420, with no
  account. Hosted Review is opt-in with `--hosted` (alias `--cloud`). A bare
  re-run keeps an existing hosted install hosted.
- **Connect the browser once.** Run `lc review --open` to pair the browser with
  the Local Review server. The browser then keeps a Review-only session for
  that exact origin (host and port) and renews it while it is valid. If the
  Reader says it is not connected or the connection expired, run
  `lc review --open` again.
- **The local server answers only its own loopback address.** Any `Host` other
  than `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>` gets `403`.
  An SSH tunnel must keep the same port: `ssh -L 7420:127.0.0.1:7420 host`
  works, `ssh -L 9000:127.0.0.1:7420 host` does not.
- **The full CLI's HTTP service requires a key.** `lc rt service start` (full
  CLI, source checkout) mints `<root>/service/api-token` (owner-only) when
  `LEMONCROW_API_KEY` is unset, and requires it as a bearer on every bind.
  On loopback it answers only its own exact `Host` (other hosts get `421`)
  and allows CORS only from its own origin. LemonCrow clients read the key
  automatically and do not follow redirects, so the key is never copied to
  another host.
- **Delivered agents keep their own approvals.** Review's **Send to agent**
  never adds a bypass, skip-permissions or allow-all flag. Codex runs in its
  `workspace-write` sandbox with network access off. Copilot and OpenCode get
  no auto-approve, so they stop at their own approval prompts.
- **Codex-driven Review Ask and Code Ask ignore your Codex configuration.**
  They run `codex exec --ignore-user-config --ignore-rules`, so your
  `~/.codex/config.toml` (including global MCP servers) and execpolicy rules
  do not load into those runs.
- **Surface runs use the trusted base's runtime.** A runtime that only the
  Change adds is refused with `422` and runs after it lands on the base branch.
- **`lc review --reproduce` runs only trusted-base commands.** The record's
  command must be a runtime command committed in the trusted base's
  `.lemoncrow/review.yaml`; anything else is refused, with or without
  `--use-recorded-policy`.
- **Compose validation needs a sandbox.** It needs bubblewrap (`bwrap`) on
  Linux or Seatbelt on macOS; without one the check reports `unavailable`
  instead of parsing reviewed compose files unsandboxed.
- **Hosted mode ignores `~/.lemoncrow/token`.** Use `lc auth login`, or set
  `LEMONCROW_TOKEN_FILE` (a path) or `LEMONCROW_TOKEN` (a value) for an
  operator-provided credential. A local machine token is never sent to a
  hosted server.
- **Remote MCP (`lc rt mcp serve`).** `--no-auth` is loopback only and cannot
  be combined with `--tunnel`. A pairing code is single use and valid for 15
  minutes. Wrong guesses lock out the guessing client (5 failures, then 60s
  doubling to 3600s), and a store-wide backstop (25 failures since the last
  successful pairing) makes every new pairing back off the same way while
  guessing continues. Connectors that are already paired keep working.
- **The standalone OpenAI-compatible gateway** (internal engineering) always
  requires `LEMONCROW_GATEWAY_TOKEN`; it mints and prints one when unset.
  Shell and edit tools ask for approval unless you pass `--yolo`.

### Fixed

- Review capture responds sooner, with bounded asynchronous polling; local Review preparation
  browser sessions now use authenticated pairing during end-to-end verification.
- Release qualification now uses bounded parallel suites, isolated PostgreSQL test databases,
  exact candidate-bound Runtime Lite evidence, and robust upgrade/canary checks.

- The installed `lc` client now prints one line plus a pointer for an unknown
  command (for example `lc host` points to `lc rt host`) instead of its full
  usage, accepts `--help` on `lc auth login|status|logout`, and explains that
  full-CLI Review flags (`--mark`, `--since-my-review`, `--feedback`, ...) and
  `lc rt telemetry|map|dashboard` live in a source checkout.
- Docs: README, CLI reference, Review guides and privacy page describe the
  installed client accurately (Local Review default, Reader-based review loop,
  no client telemetry, Cursor from a source checkout), and the docs site
  sidebar no longer lists a page that is not published.


### Late release-candidate fixes

- **Configurable local AI and background intelligence:** the Review settings
  expose local model routing, background automation, and budget controls.
- **More predictable Review navigation:** inbox ordering follows the most
  recently updated Reviews, with improved startup responsiveness.
- **Authward-powered hosted onboarding:** resource-scoped invitations provide
  branded entry links, optional email delivery, bounded quotas, revocation,
  single-use redemption, and persistent product admission. Product membership,
  repository access, and tenant roles remain separate.
- **Authentication and redirect hardening:** prioritize passkey UX and retain
  the verified product return path through hosted invitation claims. Hosted
  identity discovery, JWKS and token requests now use a named LemonCrow HTTP
  client rather than Python’s generic user agent (rejected by Authward’s edge).
- **Runtime Lite index migration:** v3 uses a separate versioned index database;
  after upgrading, start a new coding-host/MCP session to load the new client.
  Never delete the newer index to make an old process run.
- **Release-boundary repairs:** the GNU grep fallback excludes internal
  workspace state, same-host proof files cannot overwrite symlink targets,
  developer commands use a private-checkout manifest rather than exposing
  private repository paths, and status-index contracts match actual behavior.
- **Review CLI capture errors:** asynchronous capture failures preserve their
  typed server error and recovery instructions, including the discarded Review
  read-only explanation, instead of misleading HTTP 200 error text.
- **Hosted preview limitation:** the LemonCrow GKE staging instance is not yet
  qualified because of a GCS Fuse/metadata-service readiness failure. The
  public hosted service and organization-authorized end-to-end flows require
  separate operational qualification; these changes do not authorize enabling
  invite-only enforcement.


### Release highlights

- **Local/server/client architecture split:** LemonCrow now has a full local runtime,
  a public loopback server package, and a zero-dependency hosted thin client. The
  private hosted tenant plane stays outside the public distribution.
- **One shared Workbench and Review model:** local and hosted compositions reuse the
  same public UI and Review semantics instead of maintaining separate product
  implementations.
- **Durable, revision-oriented Review:** stable Review identity, immutable Snapshots,
  changed-since-review state, comments/history, fix lines, agent handoff, re-review,
  and explicit finish/publication flows now form one coherent lifecycle.
- **Stronger process and storage boundaries:** Review/Dashboard service isolation,
  revision-consistent indexing, safer connection reuse, and additive Review-store
  migrations harden the new architecture under concurrent use.
- **Release artifacts now match the architecture:** local bundles carry the public
  runtime/server/client pieces; hosted installs ship only the audited thin-client
  wheel.

### Upgrade notes

- Python **3.12 or 3.13** is required.
- **Breaking change (v0.8 Review storage):** Reviews created with v0.7.x are
  **not imported** into the new server-backed Review store. Their marks,
  comments, and Snapshot history will **not appear in the new Reader**.
  Export anything needed before upgrading. The old local database is not
  deliberately deleted by the release drill but is not opened by the new
  Review interface. This one-time compatibility break is approved for the new
  Review architecture; subsequent v0.8+ upgrades must preserve stored Review
  state unless a further breaking change is explicitly announced.
- The release pipeline verifies that the older release can be upgraded in
  place, the versioned compiled candidate starts, and **new** Review captures,
  marks, comments, and Snapshot identity remain readable. It does not claim to
  migrate pre-v0.8 Review data.
- Existing local installs continue through the local/full-distribution installer.
  Hosted users use `hosted.sh`; the hosted service remains invite-only/beta and its
  private server implementation is not part of the public v0.8 artifact.

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
- Installers and `make oss-*` / `make hosted-*` commands now show ordered progress,
  verify the installation before displaying next steps, and highlight command hints
  with restrained terminal colors. The public installer configures supported shell
  profiles automatically and supports `--preview` without installing anything.
- Review capture and Brief preparation show progress while work continues, and
  prepared source can be opened before the Brief finishes.
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
