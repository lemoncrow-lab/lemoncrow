# Changelog

All notable changes to LemonCrow are documented here. This project adheres,
loosely, to [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **GitHub Action: AI pull request review (early access).** `lemoncrow-lab/lemoncrow@<sha>`
  (pin the release commit the release notes print) reviews each pull
  request on your own model key and posts one summary comment, inline P0/P1 findings and a
  `LemonCrow Review` status as a bot (event `COMMENT` only). A high-impact correctness, security or
  data-loss finding backed by evidence beyond its own code, or a violation of a rule you wrote, is a P0
  (must fix before merge); the sample workflow starts at `fail-on: none`. A review that left risk signals
  unexamined says `Partially reviewed`, a comment sits on the line the model cited (checked against the
  diff), a finding is `Fixed` only when the model says so and its cited lines changed, skipped runs post a
  `skipped: <reason>` status, and an optional per-pull-request budget caps the spend. A
  small fix LemonCrow has checked against the commit is posted as a one-click `suggestion`. On
  each push it re-reviews only what changed and carries unchanged findings forward without a
  model call. Same engine from the full CLI: `lc review --pr N --ai [--publish-bot]`.
  See [GitHub Action: AI pull request review](docs/setup/github-action.md).
- **GitHub Action: one step to set up, and honest when something is wrong.** The workflow is one
  `uses:` step and one decision, which key to pass (`anthropic-api-key` or `openai-api-key`, masked and
  given only to the steps that call the model): no `actions/checkout`, no `fetch-depth: 0`, no cleanup
  step. The Action checks out the pull request itself, deepens a shallow checkout to the merge base, and
  settles its own `pending` status if the job is cancelled. A rejected or out-of-credit key, a rate
  limit or a provider that is down sets the `LemonCrow Review` status to `error` with the cause, says it
  in the summary's headline and in an `::error` annotation, and no longer sends the rejected request
  twice; a token without `pull-requests: write` fails the step before any model is paid for, naming the
  `permissions:` line to add. With `fail-on: p0` the job fails exactly when the status does. The sample
  workflow is named `LemonCrow` so `LemonCrow Review` is the one status to require. One content-keyed
  cache holds the scanner and the parser bundle, the runner's own Python is used, LiteLLM's price table
  is no longer fetched at runtime, and a review call asks for at most 8,192 output tokens instead of
  the model's maximum. Auto now examines every risk signal of a pull request with up to six (about 1.4
  cents each) instead of calling a five-file pull request "Partially reviewed". README, guide and
  `lc review init-action` show one pin line, and the release notes print it with the real commit.
  See [GitHub Action: AI pull request review](docs/setup/github-action.md).
- **A change diagram in the pull request summary.** A collapsed `Change diagram` after the findings
  draws, as a Mermaid flowchart GitHub shows inline, the relationships between files that the pull
  request added (solid, `+`), removed (dashed, minus sign) or changed (bold), with the unchanged file
  at the other end in grey. It comes from the Snapshot's Architecture Delta, which compares the merge
  base with the head, so it never guesses: no model call, no model-written text, no current-index
  context, no sequence, entity or class diagram the delta cannot support. It is drawn only when a
  relationship was added or removed, at most 25 nodes (grouped by directory past that, with `N more
  relationships not shown`), and is the first thing dropped when the comment nears GitHub's size
  limit. `diagram: off` (or `--diagram off`) never builds it; a pull request that changes no code, or a
  head tree of more than 2,000 source files, skips it. See
  [Change diagram](docs/setup/github-action.md#change-diagram).
- **Noise controls for the pull request reviewer.** A team can tune volume in one place, the trusted-base
  `.lemoncrow/review.yaml` (read from the merge base, so a pull request cannot loosen its own review):
  `review.finding_threshold`, which was defined and never applied, now holds back what is below it
  (tracked, counted in the headline and the check as `N held back`, listed once, never inline); `strictness: 1|2|3`
  is a friendly alias (1 shows everything, 2 is the default, 3 never comments on P2); `ignore` path globs take
  files out of the review and out of every prompt (the existing `ai_denied` content-access mechanism);
  `rules: {<key>: {severity: p1}}` sets a rule's severity (a critical rule can be lowered, an imported rule can
  never be raised past P1); `inline_comments: false` is summary-only mode; and a pull request over `max_files`
  (default 100) is reviewed highest-risk files first, says `Partially reviewed: N of M files, highest risk
  first`, names the files left and reviews them on the next push, never skipping them. The Action gains
  `strictness`, `inline-comments` and `max-files` inputs as defaults for a repository whose file is silent. A
  P0 is never held back. See [Reducing noise](docs/setup/github-action.md#reducing-noise).
- **The pull request description and linked issues as review context.** The reviewer now reads the pull request
  title and description and the issues of the same repository it links (`Fixes #12`, an issue URL), with the
  workflow token, within a 1,000-token budget, and shows them to the investigators as untrusted data (never
  instructions, never evidence; HTML comments and credentials removed) to cut false positives and to flag code
  that contradicts what was written (`Differs from the stated intent:`, never above P1). Security, data-loss and
  rule investigations never see it. An issue the token cannot read is skipped with a note.
  `LEMONCROW_REVIEW_INTENT=0` turns it off. See [Intent](docs/setup/github-action.md#intent).
- **Repository context for AI review on a model-API route.** A reviewer reached through
  an API key has no tools, so it could not see a caller in another file. Before each
  investigation it now gets a small deterministic pack from the pull request snapshot:
  callers of the changed or removed symbols (files the pull request did not touch first),
  definitions the changed code uses, tests and configuration. The pack has a token ceiling
  per depth (1,500 at Quick, 3,000 at Auto, 5,000 at Thorough, 8,000 at Deep, estimated),
  adds no model call, skips credential files, honours `ai_content_access`, and is counted
  in the summary footer. `LEMONCROW_REVIEW_REPO_CONTEXT=0` turns it off.
- **Review benchmark on real pull requests.** `lc dev eval review benchmark --corpus
  evals/review/real_prs.yaml --depth quick,auto,deep --max-usd 20` reviews pinned public
  pull requests at each depth through the same pipeline as the GitHub Action, replays a
  follow-up push with and without carry-forward, and writes Markdown and JSON reports with
  tokens, provider dollars, model calls, wall time, findings by P0/P1/P2, catch rate against
  labels, noise rate and the extra cost of a push. `import-reviews` and `init-corpus` bring in
  a baseline reviewer's comments (you name its bot with `--reviewer-login`; there is no default);
  `derive-label` builds a label from a later fix
  commit and never invents one. Live runs need `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, stop
  at `--max-usd` and refuse CI. Nothing is measured yet. See
  [Review benchmark on real pull requests](docs/reference/review-benchmark.md).
- **AI review of a change that has no pull request, and a git hook for it.** `lc review --ai` with
  no `--pr` reviews the working tree, `--staged`, a revision or `--base`/`--head` on your own model key
  (or a local agent CLI) and posts nothing: the same engine path, findings and severity as the
  GitHub Action, printed for the terminal or as `--json`. It exits `0` clean, `1` while a finding at or
  above `--fail-on` (`p0` by default, or `p1`, or `none`) is open, `2` for an incomplete review under
  `--strict` (a missing key, the kill switch, a spent budget, risk signals left unexamined) and `3` on a
  usage error; without `--strict` an incomplete review fails open, so a flaky provider never blocks a push.
  A re-run on an unchanged tree reuses the carried results and makes no model call.
  `lc review hook install [--pre-push|--pre-commit]` writes a small git hook that runs it on what you
  are about to push (`--print` shows the script, an existing hook is never overwritten without `--force`).
  See [Local AI review and git hooks](docs/reference/cli.md#local-ai-review-and-git-hooks).
- **Switching to LemonCrow: other tools' instruction files and the workflow.** The review reads
  `.cursor/rules`, `.claude/rules`, `.cursorrules`, `.github/copilot-instructions.md` and an `AGENTS.md` /
  `CLAUDE.md` in a subdirectory from the trusted base as conventions context (a nested file only for code
  under its directory), within the existing token budget and content-access policy.
  `import.agent_docs: false` turns it off and `import.agent_docs_globs` adds files.
  `lc review init-action [--provider anthropic|openai] [--fail-on none|p0] [--chat]` writes
  `.github/workflows/lemoncrow-review.yml` from the setup guide's sample and prints the two steps left to
  you, the secret and the pinned SHA. See [Importing review rules](docs/setup/importing-review-rules.md).
- **AI review in the terminal from the installed `lc`.** `lc review --ai`, `lc review hook install`,
  `lc review init-action` and `lc review config` now run from the `lc` the installer puts on your PATH, not
  only from a source checkout: the client starts the Review engine's interpreter on them, so there is one `lc`
  and one help (`lc review --help` lists them). `lc review` itself still calls no model, even with a key in your
  environment; `--ai` is the only switch that does. With only `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` set the
  installed engine calls the provider over HTTPS itself (no SDK to install); `--agent claude|codex|opencode`
  reviews with an agent CLI you are signed in to. With no model set up the review prints the two commands that
  fix it.

- **Your first AI review is three steps, and every error says what to do next.** Install, then
  `export ANTHROPIC_API_KEY=...` (or `OPENAI_API_KEY`), or name an agent you are signed in to, then
  `lc review --ai` ([Your first AI review](docs/setup/ai-review.md)). With no model set up, `lc review --ai` now prints
  three lines (the `export` line, the `--agent claude|codex|opencode` alternative, one docs link) and exits 3,
  where it used to print a paragraph of internal terms and exit 0 (a hook still fails open; `--strict` still exits 2).
  When exactly one of `claude`, `codex` or `opencode` is on the PATH the message names it as the command to type, and
  never starts it: `--agent NAME` is what spends your plan, and `--agent NAME --remember` saves it as the route to use
  whenever no key is set (an exported key wins; `lc settings reset review.agent` undoes it). A key the provider
  rejects, an account with no credit and a rate limit are each one sentence (exit 3), with no LiteLLM banner.
- **`lc settings` in the installed client, and saved keys are owner-only.** `lc settings set llm.anthropic_api_key`
  (new) or `llm.openai_api_key` saves a key without it reaching your shell history (leave the value out to be asked),
  says where it went, and writes `plugin_settings.json` mode 600; an older world-readable copy is fixed the next time
  anything is saved. There is no system keychain.
- **A local review reads your working tree's `.lemoncrow/review.yaml`.** A file you have written and not committed
  yet applies to `lc review --ai`, and the output says which config it used in one line (`Config: ...`). Pull request
  reviews and the Action still read the committed base. `lc review config --effective` now shows `strictness`,
  `ignore`, `inline_comments`, `max_files` and per-rule severities, each with its source, and lists
  `.lemoncrow/review.yaml` among the files read.
- **The full engine says its telemetry is on, once.** One line on standard error on the first run, also when piped,
  with the command that turns it off. The default is unchanged.

### Fixed

- **A review no longer pays for a hypothesis call and then examines nothing.** On a change with many risk signals
  the model's proposed hypotheses can pair a category with a target the triage raised for another category; all of
  them were dropped, so the one call was spent, no investigation ran, and the summary said "0 of 39 risk signals
  examined (the depth limit)". When none of the proposed hypotheses is allowed, the highest-ranked candidates are
  investigated directly, as a review without that call does.
- **A local review that asked the model nothing says so.** The model is asked only about a risk signal or a rule;
  on an ordinary change in a repository with no rules (a utility function, a snake_case file name) none is raised,
  and `lc review --ai` printed `No must-fix findings` with `Model: none · no model calls` and nothing else, which
  reads as an AI verdict. A line under `Model:` now says no model was asked and how to have it check the change.
- **A review on the `claude` agent CLI reports the tokens it used.** The CLI's result carries its token counts,
  and LemonCrow dropped them: every line said "tokens not reported" and the JSON had `null` counts. They are now
  read from the result (prompt tokens include the cached part, as on the API routes). A dollar cost stays
  unreported: a subscription process has none to bill.
- **`lc review --pr N --publish-bot --dry-run` prints the comments it would post.** It printed the headline and
  "would post N inline comment(s) and the summary above", with no summary above it. It now prints the summary
  comment (without its hidden state block) and each inline comment with the file and lines it would sit on, and
  `--json` carries them under `comments`. Found by the first run against a real model and real GitHub.
- **The Reader no longer opens on "LemonCrow policy removed" when your repository commits `.lemoncrow/review.yaml`.** A working-tree review left the whole `.lemoncrow/` folder out of the changed side but kept the committed config on the base side, so an unchanged file showed as a deleted, critical agent-instruction change and blocked auto-approval. The one committed `.lemoncrow/review.yaml` is now on both sides; the rest of `.lemoncrow/` stays out, and a real deletion still shows as one.
- **The review engine builds again.** `pr_bot/model_failure.py` was missing from the engine allowlist, so the engine build and `test_review_engine_closure_contract` failed on the merged branch.
- **The local Reader says what to do when no AI model is set up.** Its AI review line read "Review Intelligence
  provider is disabled; configure a provider or select a local/subscription driver", with no next step. It now
  says "No AI model is set up", the `export ANTHROPIC_API_KEY=...` line, the `lc review --ai --agent claude`
  alternative and the docs link, built from the same source as the terminal message so the two cannot drift.
  A no-key re-run after a review that left findings shows those findings marked "Not re-checked" above the same
  three lines, not a one-line "Incomplete".
- **`lc uninstall` removes the git hooks `lc review hook install` wrote** (found by their marker in the
  repositories it listed, and in the one you run it in), without `--purge`: a hook calls `lc`, and one left behind
  printed "lc was not found" on every push. A hook it never saw (v3 of the script) now says so once per repository,
  names the file to delete, and stays silent after that; it still never blocks a push. `lc review hook install`
  upgrades an older hook in place.
- **A hosted workspace that syncs GitHub no longer opens on "Not ready" and a banner.** Settings > Repositories
  called every GitHub-synced repository "Not ready ... Repair before reviewing ... contact LemonCrow" because no
  developer's `lc` had a source view of it open, which pull requests never need, and showed an internal
  `Repo_<hash>` id; Setup said the same. A repository GitHub syncs is now "Can review now" unless it is
  disabled, GitHub access was lost or its last sync failed. And the Reader no longer opens on "Some review context
  is unavailable: review surface discovery failed: not a git repository" for a pull request's Review, which keeps
  only its changed files: the list of rendered surfaces is empty instead.
- **"Server does not support Ask" is gone from the Reader.** It says "Ask is not available" and why: it needs an AI
  model, and none is set up for this workspace yet.
- **The Action's note about a linked issue it could not read is not an error.** Without the optional `issues: read`
  permission the summary says "issue #12 not read: this workflow's token lacks the optional issues: read permission
  (add it under permissions: to include linked issues)", and the setup guide says so beside the sample workflow.
- **`lc review` no longer ends in `ModuleNotFoundError: lemoncrow_client.session`** (also `--open`, `--json`
  and `lcr`; present in v0.8.0). The Review client armed the browser pairing through the Runtime session,
  which its wheel does not ship. It now pairs through the Review session, and a test copies exactly the shipped
  files into a fresh interpreter and imports them all.
- **A git hook never blocks a push because LemonCrow broke.** The v0.8.0 pre-push hook ran whatever `lc` was
  first on the PATH and passed on its exit status, so a crashing `lc` (exit 1, the same as "a finding is open")
  refused every push with a traceback. The hook now calls the absolute `lc` that wrote it and stops only for an
  open finding at or above `--fail-on`, or an incomplete review under `--strict`; anything else lets the push
  through with one line. Reinstall it (`lc review hook install`) to replace an older hook.
- **The Review engine ships `config_import.py` and `repo_context.py`** (the Reader answered HTTP 500 on a build
  from this branch) and `diff-match-patch` (re-anchoring a comment whose text changed). The engine build now
  imports every module it ships.
- **The Reader was unstyled in v0.8.0**: `frontend/postcss.config.js` was missing from the public mirror, so the
  release build skipped Tailwind and shipped raw `@tailwind` directives (11.7 KB of CSS instead of 97 KB). The
  build now refuses such a bundle.
- **An unconnected browser is told so**: the printed URL of `lc review --no-open` now says "This browser isn't
  connected to your local review" with the command to run, instead of "ask your administrator".

### Changed

- **`lc uninstall` lists what it removes, and cleans up after itself.** It prints one plan (the same for `--dry-run`),
  removes what the installer added (commands, the Review client and engine, the installer's scripts, the background
  server, the lines in every shell startup file it may have written to, not only the first) and asks before it deletes
  anything of yours (settings and saved keys, the reviews you opened, the git hooks it wrote, a repository's
  `.lemoncrow` folder). `--yes` skips the question and keeps your data; `--purge` removes it. Without a terminal it
  removes nothing until `--yes`. The source checkout's `scripts/uninstall.sh` no longer runs `npm uninstall -g`, and
  its `--dry-run` no longer says "Removed".
- **Session import now reports through the telemetry rollup.** The periodic
  session import used to POST per-session stats to a separate sync endpoint,
  a leftover from the project's earlier name. It now sends a count-only
  rollup for each newly imported session (token total, tool-call count, turn
  count, duration, host name, under the same SHA-256 install and session keys)
  to the telemetry destination, `https://lemoncrow.com/api/telemetry/rollup`
  by default. It follows the rollup's own rules: `DO_NOT_TRACK=1`,
  `LEMONCROW_TELEMETRY=off`, an administrator policy and
  `lc rt telemetry remote off` stop it, and a `server` or `custom` destination
  receives it at its own host instead. Sessions older than 48 hours or already
  in the savings ledger are skipped, at most 20 are sent per import run, and
  prompts, titles and paths are never sent. `LEMONCROW_SYNC_URL` and
  `core/service/sync.py` are removed.
- **Removed leftover Beseam references.** The example domain ids
  `beseam.*` in the Claude and Codex integration docs now use the builtin
  `swe.general` domain, domain bundles default to author `LemonCrow`,
  `ContextRuntime.run` defaults its agent to `lemoncrow-agent`, and the
  unrelated Beseam benchmark page is gone.

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
