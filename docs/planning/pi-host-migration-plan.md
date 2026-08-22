# Pi Host Migration Execution Plan

> **Status:** Proposed; implementation not started  
> **Plan owner:** LemonCrow maintainers  
> **Last validated:** 2026-08-21  
> **Plan of record:** Add Pi as an opt-in managed frontend, prove parity, then change the automatic preference. Do not begin with a deep Pi fork.

## 1. Outcome

Replace the OpenCode-derived LemonCode frontend as LemonCrow's preferred managed
host with Pi while preserving LemonCrow's existing control boundary:

- `lc code` and the `lemoncode` console entry point remain the product commands.
- LemonCrow continues to own model selection, prompts, tools, subagents,
  compaction, verification, stopping, cache policy, and cost limits.
- Pi supplies the interactive terminal UI, input queueing, session UX, model
  picker, and rendering.
- LemonCode remains an explicit fallback until Pi has passed the rollout gates
  for at least two releases.
- Existing LemonCode sessions and binaries are never deleted or rewritten by
  this migration.

This plan does not redesign the LemonCrow gateway, routing policy, MCP surface,
or native fallback.

## 2. Decision and evidence

### Decision

Use the stock, pinned Pi release plus one explicitly loaded LemonCrow extension
for the first implementation. Run Pi in its normal interactive or print mode so
the stock TUI remains available. Use RPC only for automated contract tests; do
not embed the SDK or build another UI in the first implementation.

Create a branded Pi fork only if the parity-tested upstream package cannot meet
the product naming requirement through the wrapper, `piConfig`, and extension
APIs. If a fork is required, it must remain a packaging fork, not an agent-loop
fork.

### Evidence snapshot

This snapshot is context for the decision, not a permanent upstream status
claim. Refresh it at the start of Phase 0.

| Area | Evidence on 2026-08-21 | Consequence |
| --- | --- | --- |
| Current boundary | [LemonCode](../hosts/lemoncode-install.md) is already a thin frontend over the token-authenticated LemonCrow gateway. | Pi can be introduced behind the existing engine boundary. |
| Fork drift | The local LemonCode submodule was at `094e23e5ad`; GitHub compared the fork as 21 commits ahead and 110 commits behind OpenCode `dev`. | Continuing a broad OpenCode rebrand carries recurring merge cost. |
| Pi integration | Pi exposes a custom provider registry, `before_agent_start`, replaceable `before_provider_request` payloads, tool blocking, `user_bash` interception, session events, and TUI widgets/status. | The current downstream controls should fit in an extension and launcher. |
| Pi distribution | Pi publishes versioned binaries, source archives, and `SHA256SUMS`; the latest release checked was `v0.84.2`. | LemonCrow can pin and verify an upstream binary without vendoring the monorepo. |
| Rebranding | Pi documents `piConfig.name`, `piConfig.configDir`, and the package `bin` field as its rebranding boundary. | A fork, if needed, can remain manifest-sized. |
| Security | Pi has no general built-in permission sandbox. | Managed mode must disable all Pi model-callable tools and fail closed if one appears. |

## 3. Non-negotiable invariants

Implementation must preserve all of these:

1. The Pi process connects only to the token-authenticated loopback gateway for
   model traffic.
2. No Pi system prompt, context file, skill, prompt template, or tool schema
   reaches the real model.
3. Pi executes no model-requested tool. LemonCrow remains the only tool owner.
4. A configuration or extension-loading failure aborts startup; it must not
   silently fall back to stock Pi behavior.
5. Pi's auto-compaction, retry loop, update check, package/model refresh, and
   install telemetry are disabled in managed mode.
6. Pi uses isolated configuration and session directories. It must not read or
   change a user's normal `~/.pi` state.
7. Selecting a model in Pi pins the identical provider/model in LemonCrow; no
   second automatic router runs in the host.
8. `--engine auto` continues to prefer LemonCode until every cutover gate is
   green.
9. `--engine lemoncode`, the LemonCode importer, and existing data directories
   remain supported through the rollback window.
10. No LemonCode code, release, submodule, binary, or user data is removed
    without a later, separately approved destructive change.

## 4. Target architecture

```text
lc code / lemoncode
        |
        | --engine pi
        v
pinned, checksum-verified Pi binary
        |
        | isolated config + explicit managed extension only
        | no built-in tools, project resources, retries, or compaction
        v
LemonCrow custom Pi provider
        |
        | stripped host request, loopback token
        v
token-authenticated LemonCrow gateway
        |
        | LemonCrow prompt, model routing, tools, subagents,
        | compaction, verification, cache, stopping, cost cap
        v
real model provider
```

The product path uses Pi's normal TUI. RPC mode supplies a deterministic
stdin/stdout harness for tests that need to inspect events, session switching,
steering, and cancellation.

## 5. Execution phases

### Phase 0 — Freeze the baseline

**Goal:** Record what must be preserved before adding Pi.

- [ ] Refresh the OpenCode/LemonCode divergence numbers and record the exact
      LemonCode revision.
- [ ] Resolve the Pi version to test. Start with `v0.84.2` only if it is still
      the reviewed version; otherwise review the intervening changelog and pin a
      newer tag.
- [ ] Record the Pi release asset names, supported platforms, source archive,
      and checksums.
- [ ] Run the existing engine and session-import tests.
- [ ] Capture a LemonCode baseline for:
  - interactive startup and clean exit;
  - one-shot `-p` execution;
  - a two-turn session and `--resume`;
  - one model switch;
  - one tool-using edit;
  - cancellation during streaming;
  - status sidebar values after a completed turn.
- [ ] Save redacted request envelopes and metrics under
      `reports/migrations/pi-host/<run-id>/`. Do not store credentials or
      chain-of-thought.
- [ ] Record startup latency, provider input/output/cache tokens, total cost,
      tool-call count, and session identifier for every case.

Baseline commands:

```bash
uv run pytest -q   tests/gateway/cli/test_coding_engine.py   tests/gateway/cli/test_lemoncode_host.py   tests/gateway/test_session_import_registry.py

lc code --engine lemoncode -p "Reply with exactly: LEMONCODE_BASELINE_OK"
lc code --engine lemoncode
lc code host status --json
```

**Exit gate:** The report contains reproducible commands, the pinned Pi tag and
checksums, the LemonCode revision, and complete baseline results. No product
default has changed.

### Phase 1 — Prove the extension boundary

**Goal:** Demonstrate that stock Pi can be a pure frontend before building host
management around it.

Create:

- `integrations/pi/managed.mjs`
- `tests/integrations/pi/managed.test.mjs`
- a fake OpenAI-compatible capture server fixture for the Python engine tests

The controlled extension must:

- [ ] Register exactly one `lc` OpenAI-compatible provider whose base URL,
      token, and model catalog come from launcher-owned environment variables.
- [ ] Replace the per-turn system prompt with an empty or fixed carrier prompt.
- [ ] Use `before_provider_request` to remove Pi system/developer messages,
      Pi tool definitions, and duplicate host history before transmission.
- [ ] Preserve the latest user text, images, and the minimum session correlation
      data required by the LemonCrow gateway.
- [ ] Abort at the `message_end` barrier when an assistant message contains a
      tool call, before Pi tool preflight begins, and register a blocking
      `tool_call` handler as defense in depth.
- [ ] Return a blocked result from `user_bash` during the canary. Direct
      `!`/`!!` execution may be reconsidered only after an explicit trace and
      security review.
- [ ] Decline project trust and load no project-local Pi settings or resources.
- [ ] Fail startup when required environment values are absent or malformed.
- [ ] Export pure payload-transform functions so they can be tested without a
      live provider.

Launch the pinned Pi binary manually with the equivalent of:

```text
PI_CODING_AGENT_DIR=<store>/hosts/pi/config
PI_CODING_AGENT_SESSION_DIR=<store>/hosts/pi/sessions
PI_OFFLINE=1
PI_SKIP_VERSION_CHECK=1
PI_TELEMETRY=0
pi --no-tools --no-context-files --no-extensions    -e <managed-extension> --no-approve    --provider lc --model <exact-model>
```

Confirm the exact flag names against the pinned `pi --help`. Add the
corresponding `--no-skills` and `--no-prompt-templates` flags when present.
The launcher must reject a pinned Pi version that lacks a required fail-closed
control.

Contract assertions:

- [ ] The captured Pi-to-gateway request contains no `tools` entries.
- [ ] It contains no Pi-generated system or developer prompt.
- [ ] It contains no AGENTS.md, skills, templates, or project extensions.
- [ ] The gateway-to-provider request contains the LemonCrow prompt and tools
      exactly once.
- [ ] A synthetic tool call returned to Pi is aborted before tool preflight,
      executes nothing, and causes no second outer-provider request.
- [ ] Missing extension, invalid extension, or absent token produces a nonzero
      exit and no provider request.
- [ ] Print and interactive modes render the gateway response correctly.

Verification:

```bash
node --test tests/integrations/pi/managed.test.mjs
uv run pytest -q tests/gateway/cli/test_coding_engine.py
LEMONCROW_PI_CONTRACT_BIN=/path/to/pinned/pi uv run pytest -q tests/gateway/cli/test_pi_contract.py
```

**Exit gate:** Stock Pi passes every request and tool-ownership assertion. If it
does not, stop and document the missing hook. Do not create a fork merely to
continue the spike.

### Phase 2 — Add a managed, opt-in Pi engine

**Goal:** Make `lc code --engine pi` install and launch a pinned Pi host while
leaving `auto` unchanged.

Primary implementation surfaces:

- `src/lemoncrow/gateway/cli/coding_engine.py`
- `src/lemoncrow/gateway/cli/commands/code.py`
- new `src/lemoncrow/gateway/cli/pi_host.py`
- `integrations/pi/managed.mjs`
- `tests/gateway/cli/test_coding_engine.py`
- new `tests/gateway/cli/test_pi_host.py`
- `tests/gateway/cli/test_code_client.py`
- `tests/gateway/test_agent_cli_install_artifacts.py`

Tasks:

- [ ] Add `pi` to `EngineName` and the `--engine` Click choice.
- [ ] Keep the opt-in automatic order as
      `lemoncode, pi, codex, claude, native`.
- [ ] Add `_provision_pi_host()` and explicit resolution for `--engine pi`.
- [ ] Implement `pi_host.py` with the proven LemonCode lifecycle properties:
  - platform/architecture allowlist;
  - pinned release tag;
  - release checksum validation;
  - temporary download plus atomic install;
  - executable-mode validation;
  - metadata containing tag, commit, asset, checksum, and install time;
  - explicit binary override;
  - update policy and status;
  - safe removal of only the exact managed binary.
- [ ] Prefer official standalone Pi binaries. Do not require npm or lifecycle
      scripts on the product path.
- [ ] Extend `lc code host {status,install,update,build,remove}` with
      `--engine lemoncode|pi`, defaulting to `lemoncode` so existing commands
      remain compatible.
- [ ] Support source builds as
      `lc code host build --engine pi --source /path/to/pi`; do not add a Pi
      submodule in this phase.
- [ ] Map LemonCrow modes to Pi:
  - interactive: normal Pi TUI;
  - `-p`: Pi print mode;
  - `--resume`: Pi `--session <path-or-id>`;
  - prompt plus resume: resume first, then submit in print mode.
- [ ] Generate an isolated managed settings file with:
  - `compaction.enabled=false`;
  - agent and provider retry disabled;
  - install telemetry disabled;
  - project trust set to never;
  - no default tools.
- [ ] Pass the exact LemonCrow model catalog to the extension and pin the selected
      model. Preserve Zen's keyless default behavior.
- [ ] Start the existing `_managed_gateway` unchanged except for any
      engine-neutral naming needed by tests.
- [ ] Do not refactor `lemoncode_host.py` in the same change unless a small
      extracted helper is required to make checksum or atomic-install behavior
      identical.

Post-Phase-2 smoke commands:

```bash
lc code host install --engine pi
lc code host status --engine pi --json
lc code --engine pi -p "Reply with exactly: PI_ENGINE_OK"
lc code --engine pi
```

Verification:

```bash
uv run pytest -q   tests/gateway/cli/test_coding_engine.py   tests/gateway/cli/test_pi_host.py   tests/gateway/cli/test_code_client.py   tests/gateway/test_agent_cli_install_artifacts.py
```

**Exit gate:** Explicit Pi install, status, interactive, print, and failure paths
work on Linux x64 and one macOS architecture. `lc code` with no engine still
selects LemonCode.

### Phase 3 — Add session import, replay, and resume parity

**Goal:** Preserve Pi's JSONL session tree without changing or converting
LemonCode's SQLite sessions.

Primary implementation surfaces:

- new `src/lemoncrow/gateway/hosts/session_parsers/pi.py`
- `src/lemoncrow/gateway/hosts/session_parsers/registry.py`
- `src/lemoncrow/gateway/hosts/session_parsers/_session_parser.py`
- `src/lemoncrow/core/capabilities/session_replay.py`
- `src/lemoncrow/gateway/cli/commands/hosts.py`
- new `tests/gateway/test_pi_session_importer.py`
- `tests/gateway/test_session_import_registry.py`
- `tests/core/test_session_replay.py`

Tasks:

- [ ] Freeze representative session fixtures from the pinned Pi release:
      one-turn, multi-turn, tool call, image, compaction, steering, branch, clone,
      and resumed session.
- [ ] Add `PiImporter` with source `pi`.
- [ ] Discover external Pi sessions from
      `PI_CODING_AGENT_SESSION_DIR` or Pi's documented default, and managed
      sessions from the LemonCrow store path.
- [ ] Store the original JSONL unchanged as the redacted raw artifact before
      normalization.
- [ ] Reconstruct parent/child links and the active branch deterministically.
      Preserve entry IDs, parent IDs, labels, timestamps, model identifiers,
      token/cache/cost usage, tool calls, attachments, and compaction entries.
- [ ] Use file modification time and session ID for incremental import. A resumed
      session with new entries must re-import without `force=True`.
- [ ] Add `pi` to `SUPPORTED_SESSION_IMPORT_HOSTS`, host detection, CLI
      import commands, and replay parsing.
- [ ] Keep LemonCode and OpenCode parsing unchanged.
- [ ] Treat unknown future Pi entries as preserved raw records plus a warning,
      not as a fatal import error.
- [ ] Test import from paths containing spaces and from an absent directory.
- [ ] Confirm that Pi can resume a Pi session after process restart and that
      LemonCrow can import the completed session.

Verification:

```bash
uv run pytest -q   tests/gateway/test_pi_session_importer.py   tests/gateway/test_session_import_registry.py   tests/core/test_session_replay.py
```

**Exit gate:** All pinned session fixtures import without lost user/assistant
turns or double-counted usage; resume works in both interactive and print modes.
No existing host-import test changes behavior.

### Phase 4 — Reach UI and operational parity

**Goal:** Port only the LemonCrow-specific UI value, using Pi extension APIs
instead of Pi core patches.

Extend `integrations/pi/managed.mjs` to:

- [ ] Read the path in `LEMONCROW_STATUS_FILE`.
- [ ] Render a compact status/footer segment for input, cache-read, output,
      context, cost, and savings.
- [ ] Render an optional widget for tool-call and MCP-call counts.
- [ ] Update after each completed turn and when the status file changes.
- [ ] Show nothing before the first valid snapshot.
- [ ] Ignore an incomplete atomic-write window and retain the last valid
      snapshot.
- [ ] Remove watchers and UI state on session shutdown.
- [ ] Avoid provider calls, background telemetry, and unbounded polling.
- [ ] Work in regular and fullscreen TUI modes; remain a no-op in print mode.

Parity checklist:

| Capability | Required result |
| --- | --- |
| Interactive rendering | Streaming text, thinking visibility, tool summaries, and errors remain readable. |
| Input while running | Steering and follow-up queues preserve Pi's documented behavior. |
| Model selection | UI choice pins the identical LemonCrow model. |
| Cancellation | Escape aborts the current request and leaves the session resumable. |
| Session navigation | Resume, tree, fork, and clone keep valid JSONL state. |
| Status | Tokens, cache, context, cost, savings, tool calls, and MCP calls match the gateway snapshot. |
| Offline startup | No version, telemetry, package, model-catalog, or project-resource network request occurs. |
| Tool ownership | No Pi model-callable tool executes, including after resume or model change. |

Documentation to add or update after this phase:

- new `docs/hosts/pi-install.md`;
- `docs/hosts/host-capability-matrix.md`;
- `docs/hosts/all-agent-clis.md`;
- `docs/reference/cli.md`;
- `docs/README.md`;
- `CHANGELOG.md`.

Do not yet rewrite the roadmap, README positioning, landing page, or LemonCode
install guide to call Pi the default.

**Exit gate:** A manual TUI review and automated snapshot/parser tests confirm
the parity table. Any intentional UX difference is documented in
`docs/hosts/pi-install.md`.

### Phase 5 — Canary and default cutover

**Goal:** Demonstrate non-regression around the Pi-default cutover and retain an immediate LemonCode rollback.

Run paired LemonCode/Pi cases with the same LemonCrow revision, workspace,
provider, model, prompt, cache state, and budget. Use at least 20 paired startup
runs and 50 total agent turns across small edit, multi-file edit, test repair,
read-only analysis, resume, steering, and cancellation tasks.

Acceptance gates:

| Gate | Required result |
| --- | --- |
| Deterministic tests | 100% pass for engine, gateway, installer, importer, replay, and extension tests. |
| Prompt duplication | Zero Pi prompt or Pi tool-schema tokens in the real provider request. |
| Tool duplication | Zero host tool executions and exactly one LemonCrow trace per model tool call. |
| Model fidelity | Selected provider/model matches the gateway route on every turn. |
| Correctness | No paired task loses a previously passing verification or rubric gate. |
| Provider tokens | Pi median provider input tokens are no more than 1% above LemonCode for the same task and history. |
| Cost | No unexplained cost increase; any model-price variance is removed before comparison. |
| Startup | Pi p50 is at most 1.25x and p95 at most 1.5x the LemonCode baseline. |
| Reliability | No lost, repeated, or reordered user turns across 50 turns. |
| Resume/import | 100% of the canary sessions resume and import successfully. |
| Supply chain | Every installed binary matches the pinned release checksum and recorded tag. |
| Platforms | Managed install and smoke pass on Linux x64/arm64 and macOS x64/arm64. |

Rollout sequence:

1. `--engine pi` shipped opt-in first; the current cutover makes Pi the `auto` preference and fresh-install default.
2. Collect only redacted local evidence already allowed by LemonCrow's privacy
   policy; do not add Pi analytics or a new remote telemetry stream.
3. Add and document `LEMONCROW_CODE_AUTO_ENGINE=lemoncode|pi` as an immediate
   rollback override.
4. Automatic preference is now `pi, lemoncode, codex, claude, native`; keep the remaining canary/platform gates as release-readiness checks.
5. Keep `--engine lemoncode` and the old managed binary fully usable.
6. Update public documentation and product copy only in the cutover change.

**Exit gate:** Pi is the automatic preferred host, the rollback override is
tested, and the release notes state how to return to LemonCode.

### Phase 6 — Decide branding fork and later decommissioning

#### Optional minimal fork

Create a Pi fork only when all Phase 5 gates pass and the remaining naming gap
is unacceptable. The allowed downstream patch budget is:

- package `name`, `bin`, repository metadata, and `piConfig`;
- license/attribution and LemonCode downstream notes;
- deterministic release workflow and checksums;
- no changes under Pi's agent core, provider core, session engine, or TUI
  rendering source.

Target: at most five non-generated, non-release files different from the pinned
upstream tag. If branding requires a broader patch, keep using pinned upstream
Pi and accept the Pi implementation name inside the frontend.

If a fork is created:

- [ ] Base each release on a signed or checksum-pinned upstream tag.
- [ ] Run upstream `npm run check` and `./test.sh`.
- [ ] Publish source archives and `SHA256SUMS`.
- [ ] Automate an upstream-drift report, not an unattended merge to release.
- [ ] Require the managed extension contract tests before publication.

#### LemonCode retirement

Retirement is a separate destructive project, not part of the cutover.

Do not propose removal until Pi has been the default for two releases and at
least 30 days, with no open severity-1 parity defect. A later removal plan must
inventory and request explicit approval for:

- the `lemoncode/` submodule;
- LemonCode release workflows and fork;
- `lemoncode_host.py`;
- installer, verifier, and uninstaller scripts;
- LemonCode-specific tests and docs;
- managed binaries.

Never delete `~/.local/share/lemoncode`, imported artifacts, or user sessions.
The importer can remain after the host is retired.

## 6. File-level implementation ledger

| State | Path | Work |
| --- | --- | --- |
| Modify | `src/lemoncrow/gateway/cli/coding_engine.py` | Resolve, provision, configure, and launch Pi; later change automatic order. |
| Modify | `src/lemoncrow/gateway/cli/commands/code.py` | Add engine choice and engine-aware host lifecycle commands. |
| Add | `src/lemoncrow/gateway/cli/pi_host.py` | Pinned release metadata, checksummed install/build/status/remove. |
| Add | `integrations/pi/managed.mjs` | Provider, payload stripping, fail-closed tool policy, status UI. |
| Add | `src/lemoncrow/gateway/hosts/session_parsers/pi.py` | JSONL discovery, preservation, import, and normalization. |
| Modify | `src/lemoncrow/gateway/hosts/session_parsers/registry.py` | Register source `pi`. |
| Modify | `src/lemoncrow/gateway/hosts/session_parsers/_session_parser.py` | Parse Pi messages and usage. |
| Modify | `src/lemoncrow/core/capabilities/session_replay.py` | Detect and replay Pi transcripts. |
| Modify | `src/lemoncrow/gateway/cli/commands/hosts.py` | Expose Pi session import. |
| Add/modify | `tests/gateway/cli/` | Engine and host lifecycle contracts. |
| Add/modify | `tests/gateway/`, `tests/core/` | Session import and replay fixtures. |
| Add | `tests/integrations/pi/managed.test.mjs` | Pure extension payload/tool/status tests. |
| Add/modify | `docs/hosts/`, `docs/reference/cli.md`, `docs/README.md` | Operator and capability documentation. |
| Modify at cutover | `README.md`, roadmap, landing copy, `CHANGELOG.md` | Change the preferred-host claim only after acceptance. |

The existing wheel already force-includes `integrations/`; verify the built
wheel contains the Pi extension in
`tests/gateway/test_agent_cli_install_artifacts.py`.

## 7. Full verification sequence

Run narrow tests after each phase, then one broad verification before the
canary release:

```bash
node --test tests/integrations/pi/managed.test.mjs

uv run pytest -q   tests/gateway/cli/test_coding_engine.py   tests/gateway/cli/test_pi_host.py   tests/gateway/cli/test_code_client.py   tests/gateway/test_agent_cli_install_artifacts.py   tests/gateway/test_pi_session_importer.py   tests/gateway/test_session_import_registry.py   tests/core/test_session_replay.py

uv run pytest -q
make verify
```

When a branded fork exists, also run from that checkout:

```bash
npm ci --ignore-scripts
npm run check
./test.sh
```

A paid or rate-limited provider run is not required to develop the adapter.
Use the fake capture server and a keyless/local model first. Do not approve the
default cutover without at least one paired run on every provider protocol that
the managed gateway claims to support.

## 8. Rollback

### Before default cutover

No rollback action is necessary: omit `--engine pi` or run:

```bash
lc code --engine lemoncode
```

### After default cutover

Immediate operator rollback:

```bash
LEMONCROW_CODE_AUTO_ENGINE=lemoncode lc code
# or
lc code --engine lemoncode
```

Release rollback:

1. Restore the automatic order to LemonCode.
2. Keep the Pi binary and sessions installed but inactive.
3. Publish a patch release.
4. Import affected Pi JSONL sessions if users need cross-host replay.
5. Do not convert, move, or delete either host's data.

Rollback is successful when the existing LemonCode baseline commands pass and
the same provider/model can resume through LemonCode or start a new session.
Pi-specific sessions remain readable through the Pi importer even if the Pi
engine is disabled.

## 9. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Two agent loops or duplicate prompts | Strip the final Pi provider payload and inspect both sides of the gateway in a contract test. |
| Pi executes a tool | Disable all Pi tools, abort tool-bearing assistant messages before preflight, block every `tool_call`, fail startup if the extension is absent, and test a synthetic call. |
| Direct `!` shell bypasses LemonCrow | Block `user_bash` during canary; reconsider only with explicit trace/security evidence. |
| Project code loads a malicious Pi extension | Decline project trust and explicitly disable discovery; load only the packaged extension path. |
| Pi session format changes | Pin the version, retain raw JSONL, keep fixtures, warn on unknown entries, and update parser before host bump. |
| Model selection diverges | Register only LemonCrow's catalog and assert selected provider/model at the gateway. |
| Offline mode blocks the loopback provider | Test it explicitly; if needed, disable only startup refreshes while retaining the loopback allowlist. |
| Release asset or checksum changes | Pin tag, asset, and checksum; fail closed on mismatch; use atomic installation. |
| TUI parity requires core patches | Use status/widgets and accept documented keybinding differences; do not patch Pi core for cosmetic parity. |
| Minimal fork grows | Enforce the five-file patch budget and zero core-agent/provider changes. |
| LemonCode users lose history | Keep both data roots and importers; never perform an in-place migration. |

## 10. Suggested PR sequence

Keep review and rollback boundaries small:

1. **docs: record Pi baseline and contract**
2. **feat(pi): add managed extension and capture tests**
3. **feat(pi): add pinned host lifecycle and explicit engine**
4. **feat(pi): import and replay Pi sessions**
5. **feat(pi): add LemonCrow status UI**
6. **test(pi): add parity and platform evidence**
7. **feat(pi): prefer Pi with tested rollback**
8. **chore(pi): minimal branding fork**, only if the Phase 6 gate requires it

Do not combine the default switch, session parser, and LemonCode removal in one
change.

## 11. Progress ledger

| Phase | State | Evidence |
| --- | --- | --- |
| 0 — Baseline | Not started | LemonCode baseline report and paired metrics still need to be captured under `reports/migrations/pi-host/`. |
| 1 — Extension boundary | In progress | Managed extension and fail-closed safety are merged (`31a905965`, `b72a56fd0`). On Linux x64, 13 real pinned-Pi contracts cover sanitized requests, startup failures, tool-call abort, direct bash denial, model confinement/fidelity, multimodal transport, retry suppression, offline startup, steering, cancellation, and RPC session behavior. Downstream provider-envelope duplication and manual interactive rendering remain to be signed off. |
| 2 — Opt-in engine | In progress | Managed lifecycle/explicit engine is merged (`31a905965`); Linux x64 install/status/remove, print, resume, binary capability validation, and `auto` rollback override are proven. A macOS architecture and manual interactive smoke are still required for the exit gate. |
| 3 — Sessions | In progress | Pi importer/registry/replay tests pass; real RPC confirms append-only entries, switch-session restore, and fork-to-new-session behavior, and product `--resume` appends cleanly. The complete pinned fixture matrix and clone/branch coverage remain. |
| 4 — UI parity | In progress | Status/widget hooks, context/cost/tool data, last-valid snapshot retention, cancellation, model switching, steering, offline behavior, and tool ownership have automated coverage. Manual regular/fullscreen TUI review remains. |
| 5 — Canary/cutover | Not started | `auto` still prefers LemonCode; `LEMONCROW_CODE_AUTO_ENGINE=pi` is implemented as the canary/rollback override. |
| 6 — Fork/retirement decision | Not started | No branding fork or LemonCode retirement work has begun. |

Update this table after each merged phase with the commit, test command, and
report path. A phase is complete only when its exit gate is satisfied.

## References

- [Pi repository](https://github.com/earendil-works/pi/tree/main)
- [Pi coding-agent package](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)
- [Pi extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
- [Pi custom providers](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/custom-provider.md)
- [Pi settings](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md)
- [Pi RPC mode](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
- [Pi development and rebranding](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/development.md)
- [Current LemonCode host contract](../hosts/lemoncode-install.md)
- [Current host capability matrix](../hosts/host-capability-matrix.md)
