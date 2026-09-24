# Shared client kit, phase 2b: bash command policy

**Goal:** one command policy for both surfaces. `classify_command` and
`execute_inline_op` move from `bash_exec.py` into
`lemoncrow_client.kit.command_policy`; the thin client's `bash` applies them.

**Spec:** `docs/planning/2026-09-22-shared-client-kit-design.md`.

## Global constraints

- Kit imports only the standard library, `lemoncrow_client.errors` and kit.
- No behavior change on the main package: a golden master of the policy's
  decisions and the inline ops' output replays byte-identical.
- Main-only capabilities (external compactors) plug in through a hook.
- The thin client reaches no network and starts no background process.

## Side by side

| Policy piece | Main | Client before | After |
| --- | --- | --- | --- |
| Destructive-git guard (`git reset --hard`, `git clean -fd`) through chaining, substitution, wrappers (`sudo`, `timeout`, `env`, `xargs`, ...), `eval`/`exec`, `busybox`, `bash -c` payloads and on-disk scripts | yes | no | both |
| Shell-write guard (`cat > f`, `python -c "open(f,'w')"`) outside the allowed roots and the temp dir | yes (workspace + additional dirs) | no | both; the client's root is its worktree, as for `edit` |
| Inline `cat` (several files, `-n`/`-b`), `head`, `tail`, `wc` without a fork | yes | no | both; the client compacts the result, as main's `run_command` does |
| `od <big file> \| tail` rewritten to a seek | yes | no | both |
| `cat f` to `read`, `sed -n A,Bp f` to a read range, `grep`/`rg` to the grep tool, `find . -name P -type f` to a glob, `curl URL` to `web_fetch` | yes (main tools) | no | main only; the client runs these as written. `grep` and `read_from_disk` join the kit in phase 3; `web_fetch` needs the network, which the client never touches |
| External compactors (`rtk`) | yes | no | main only, through `classify_command(..., fallback=...)` |

## Tasks

### Task A: golden master

- [x] Generate a corpus of commands covering every branch above (git forms,
  wrappers, operators, quoting, shell payloads, scripts on disk, a sparse file
  over the seek threshold, curl/find/sed/grep shapes, write targets inside and
  outside the roots), and fixture files for the inline ops.
- [x] Record every `classify_command` decision and every `execute_inline_op`
  result before any change.

### Task B: move the policy into kit

- [x] Cut the policy blocks verbatim into `kit/command_policy.py`;
  `classify_command(command, *, allowed_write_roots=None, cwd=None,
  fallback=None)` calls `fallback(tokens, command)` where main checked for an
  external compactor.
- [x] `bash_exec.classify_command` wraps it with the compactor fallback;
  `CommandPolicyDecision` and `execute_inline_op` come from kit. Update tests.
- [x] Replay the golden master: identical. Run the bash suites. Commit
  `refactor(bash): move the command policy into kit`.

### Task C: the client applies the policy

- [x] Tests first: `git reset --hard` is blocked, also inside `bash -c` and a
  script; a shell write outside the worktree is blocked and one into the temp
  dir is not; `head -n 3 f` runs inline; a blocked command in an array does not
  stop the others.
- [x] `localtools/shell.py`: classify each command; a block becomes that
  section's result with `[exit -1]`; an inline op runs in-process and its
  output is compacted; a pipeline seek runs the rewritten command under its
  note; everything else runs as written.
- [x] Client suite, mypy, audit. Commit `feat(client): bash applies the kit command policy`.

### Task D: parity and docs

- [x] Parity test: over a command corpus both surfaces make the same block or
  run decision, and inline ops return the same text.
- [x] Changelog entry and execution log. Commit.

## Execution log

- Golden master: 3,000 generated commands plus 300 hand-written ones, each
  classified three ways (with the worktree as write root, with only a cwd,
  bare), and 290 inline-op calls: 10,284 rows, 2,162 blocks, 631 rewrites,
  every rewrite target including `external_compactor`. Deterministic across
  two recordings; byte-identical after the move.
- The move is verbatim apart from `classify_command`'s `fallback` hook, which
  replaces the external-compactor branch at the same point. Nothing left in
  `bash_exec.py` needed a moved private name.
- Audit: reviewed entries for `kit/command_policy.py` (it recognizes
  `curl ... | pip` installs so it never rewrites them, and names its
  `pipeline` rewrite) and `localtools/shell.py` (the `"pipeline_seek"` target).
- Unrelated fix the commit hook forced: `mcp/bash.py` imported
  `tool_call_tokens_saved` through `smart_state`, which mypy rejects as an
  implicit re-export; it now imports it from `gateway/tools/state.py`.
- Client, deliberately narrower than main: write roots are the worktree plus
  the temp dir (main adds Claude `additionalDirectories`), matching the
  client's `edit`. Rewrites to `read`, `read_range`, `grep`, `search`,
  `find_glob` and `web_fetch` run as written.
- Parity: `tests/gateway/test_bash_policy_parity.py`: with compactors off, 428
  commands get identical decisions from `bash_exec.classify_command` and the
  kit (at least 100 blocks); 13 in-process reads render the same text through
  the client and `bash_exec.run_command`.
