# Shared client kit

Status: approved design, 2026-09-22. Each phase below gets its own
implementation plan and its own change.

## Problem

`lemoncrow-client` must stay standard-library-only with no dependencies
(`client/pyproject.toml`, enforced by `client/tests/test_packaging_audit.py`),
so it cannot import `lemoncrow`. Its seven local tools (`bash`, `edit`, `grep`,
`blame`, `scan`, `codemod`, `sql`) were therefore written a second time in
`client/src/lemoncrow_client/localtools/`, next to the main package's handlers in
`src/lemoncrow/gateway/adapters/mcp/`. Both copies are live, and the client
copies miss most of the main package's behavior. Two examples:

- Client `edit` resolves the line ranges of a batch against the file as already
  edited by earlier hunks. Replacing `L1` with three lines and then `L4` rewrites
  the old line 2. `rich_edit.apply_rich_edits` already translates every range
  back to pre-batch line numbers.
- Client `bash` keeps the first 256 KB of output and drops the rest, which is
  where test and build failures are printed. The main package budgets output,
  keeps head and tail, extracts failures, redacts secrets and shows reruns as
  deltas.

## Decisions

1. **Location.** The shared library is `lemoncrow_client.kit`, a subpackage of
   the client. `lemoncrow` and `lemoncrow-server` already depend on
   `lemoncrow-client`, so they import it directly, and the client stays one
   zero-dependency distribution. A separate package was rejected: it either
   breaks `dependencies = []` or needs build-time vendoring.
2. **Behavior.** Where the two copies differ, kit takes the stronger behavior
   whenever standard-library code allows it. What the client audit forbids
   (threads, background sessions, third-party libraries, the code index) stays in
   the main package as add-ons on top of kit.
3. **Old copies.** When a tool moves, the main package's old implementation is
   deleted in the same change, once parity tests pass. Moved modules get no
   compatibility aliases; their importers are updated.

## Architecture

```text
lemoncrow_client/kit/            shared library, standard library only
  edit.py                        batch edit engine + test-weakening guard
  fsio.py                        atomic writes and overflow files (kit's only file writer)
  output.py                      bash output trimming, rerun deltas, secret redaction
  shell.py                       foreground command runner and command policy
  (grep, blame, ast-grep, sql)   later phases
lemoncrow_client/localtools/     client wrappers: path checks, blob push, text output
lemoncrow/gateway/adapters/mcp/  main-package wrappers: locks, ledger, reindex, background bash
```

Rules:

- kit imports only the standard library, `lemoncrow_client.errors` and other kit
  modules. It never imports the client's session, transport, config or
  `localtools`.
- kit takes plain inputs (a workspace root and argument mappings) and returns
  plain results. Each side renders them in its own format.
- The client audit applies to kit unchanged: no network, no threads, no signal
  handlers, no background primitives. `kit/fsio.py` is added to the audit's
  file-writer allowlist as a reviewed change.
- When kit behavior needs something only the main package has, kit takes an
  optional hook. The main package passes its implementation. The client passes
  nothing and gets the documented fallback.
- **Standing rule:** logic a client tool needs is implemented in kit. The main
  package's handlers for the seven client tools only wrap kit and add
  main-package-only features.

## Phase 1: edit

`kit/edit.py` receives the body of `rich_edit.apply_rich_edits` as
`apply_edits(edits, *, root, allowed_roots=(), atomic=True, extensions=None)`:

- target parsing (`:Lx-Ly`, `:head=`, `:tail=`, `:full`, `#cell=`)
- pre-batch line translation for range edits, with loud refusal when a range
  cannot be translated
- alias normalization for the `old`/`new` spellings models send
- exact, typography-normalized and placeholder matching with indentation
  adaptation, and "already applied" detection
- notebook cell edits and native full-file rewrite detection
- the Python parse gate, one atomic write per file, whole-batch rollback and
  retry hints
- protected paths (`.git`, `.lemoncrow`, `node_modules`, `.venv`)
- the test-weakening guard: `looks_like_test_path`, `classify_test_weakening`
  and `detect_test_weakening(snapshots, session_created)`

Hooks (`EditExtensions`), supplied only by the main package:

| Hook | Why it cannot live in kit | Fallback without it |
| --- | --- | --- |
| symbol edits (`kind: symbol`) | needs the code index | edit refused with a clear error |
| projection edits (`kind: projection`) | needs `source_projection` | edit refused with a clear error |
| minified-view matching | needs `source_projection` | matching rung skipped |
| fuzzy matching | needs `diff_match_patch` | matching rung skipped |
| post-write reindex | needs the code index | nothing |
| preserving a large `new` payload | uses the main package's spill store | no `new_preserved_at` hint |

Main package: `rich_edit.apply_rich_edits` becomes the wrapper that passes these
hooks, `mcp_server` calls kit's test-weakening functions, and
`path_safety.PROTECTED_PARTS` moves to kit.

Client: `localtools/editing.py` keeps its stricter path checks
(`LocalContext.resolve`), blob push and one-line summaries. Its `_apply` is
deleted. Failures render kit's error and `retry_with` hint so the agent can retry
without a re-read. The test-weakening guard runs with the same
`LEMONCROW_TEST_CONTRACT_GUARD` switch; files created during the session are
tracked in memory for the life of the process.

## Phase 2: bash

- `kit/output.py`: `bash_output_compression`, `bash_output_profiles`,
  `output_delta` and `core/foundation/redaction` move in, with the pure parts of
  `bash_exec` (ANSI stripping, `_compact_result` and its helpers).
- `kit/shell.py`: the foreground runner, `classify_command` and the inline
  `cat`/`head`/`tail`/`wc` answers that avoid starting a shell.
- Main package only: managed background and interactive sessions (threads) and
  external compactor binaries.
- Client: the 256 KB head-only cap is replaced by kit trimming plus the overflow
  file, and dangerous commands (`git reset --hard`, `git clean -fd`, shell writes
  outside the workspace) are blocked.
- The exact split of `bash_exec` is fixed in the phase-2 plan after a full read.

## Phases 3 and 4

Phase 3 moves `grep` and the offline `read_from_disk`. Phase 4 moves `blame`,
`scan`, `codemod` and `sql`. Each plan starts with a side-by-side feature list of
both copies.

## Testing and enforcement

- **Differential proof before deletion.** Old and new implementations run over
  the same random inputs. Every divergence is recorded in the phase's change as
  a fix or a regression; regressions block the change.
- **Parity tests** in `tests/`: the same fixtures through the client executor
  and the main-package handler give the same file bytes and the same
  applied/failed outcome (`edit`) or the same trimmed text (`bash`).
- **Boundary tests** in `client/tests/`: kit imports only what the rules above
  allow; moved modules no longer exist in `lemoncrow.pro`; the main package's
  handlers import `lemoncrow_client.kit`.
- The existing audit tests stay as they are apart from the `fsio.py` allowlist
  entry. `client/tests/test_mcp_startup_budget.py` must keep passing, so client
  executors import kit lazily.
- The phase-2 change reports before/after token counts on recorded outputs.

## Risks

- Unrelated uncommitted work exists in the main checkout. Implementation happens
  on `feat/shared-client-kit` in a separate git worktree.
- Downstream importers of
  `lemoncrow_client.localtools.shell.cancel_active_bash_commands` must keep
  working.
- Client users see new behavior (protected paths, the test-weakening guard,
  trimmed output, blocked commands). Each phase adds a changelog entry.

## Out of scope

- Background and interactive bash in the client: the audit forbids threads.
- `:minified`, symbol and projection edits in the client: they need the server's
  projection and index.
