# Shared client kit, phase 2a (bash output): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The thin client's `bash` output goes through the same trimming,
failure extraction, secret redaction and rerun dedup as the main package's.

**Architecture:** Move the standard-library-only output modules into kit
unchanged (`redaction`, `bash_output_compression`, `bash_output_profiles`,
`output_delta`), and move the pure half of `bash_exec` (ANSI stripping, test
extraction, repeated-line collapse, per-command budgets, error windows,
suppress-on-success, stable-flag injection, `_compact_result`) into
`kit/bash_output.py`. The spill store stays per surface behind a hook. Command
execution stays per surface: the main package's managed sessions need threads,
the client's runner must not use them. Command policy (`classify_command`,
inline `cat`/`head` answers) is phase 2b.

**Spec:** [2026-09-22-shared-client-kit-design.md](2026-09-22-shared-client-kit-design.md)

## Global constraints

Same as phase 1: standard library only in kit, no threads/`atexit`/signal
handlers (a `threading.Lock` is allowed), `kit/fsio.py` stays kit's only file
writer, client executors import kit lazily, moved modules get no aliases.

## Tasks

### Task A: `kit/redaction.py`

- [x] `git mv src/lemoncrow/core/foundation/redaction.py client/src/lemoncrow_client/kit/redaction.py`.
- [x] Rewrite every `lemoncrow.core.foundation.redaction` import (src, tests,
  integrations) to `lemoncrow_client.kit.redaction`.
- [x] Run the redaction tests, the packaging audit and the kit boundary test.
- [x] Commit `refactor(redaction): move output redaction into kit`.

### Task B: compression, profiles and rerun delta

- [x] `git mv` `bash_output_compression.py`, `bash_output_profiles.py` and
  `output_delta.py` from `tool_supervision/` into kit; make intra-kit imports
  relative; rewrite importers.
- [x] Run their tests. Commit `refactor(bash): move output compression into kit`.

### Task C: the output pipeline

- [x] Golden master (recorded before any change): 1,500 generated cases through
  `bash_exec._compact_result` and `_inject_stable_flags`.
- [x] Create `kit/bash_output.py` with the moved helpers and
  `compact_command_output(command, raw_stdout, raw_stderr, exit_code, *,
  max_lines=200, max_chars=None, spill=None) -> CompactedOutput`, where
  `spill(full_text, kept_chars) -> str` returns the footer or `""`; plus
  `inject_stable_flags`, `spill_footer` and `unchanged_marker`.
- [x] `bash_exec._compact_result` becomes a wrapper that builds `RunResult`;
  `_spill_hint` keeps the main spill store and uses kit's footer.
- [x] Replay the golden master: identical output. Run the bash test files.
- [x] Commit `refactor(bash): move the output pipeline into kit`.

### Task D: client `bash` uses the pipeline

- [x] Tests first: a failing pytest-shaped run keeps its failures and summary
  and drops the passing noise; a long log keeps the lines around an error; a
  secret is redacted; an identical rerun returns the `unchanged` marker; a
  trimmed run names its overflow file.
- [x] `localtools/shell.py`: inject stable flags, run, compact through kit with
  the client's overflow file as the spill hook, apply the rerun delta.
- [x] Run the client suite and mypy. Commit `feat(client): bash output runs through the kit pipeline`.

### Task E: parity, cost evidence, docs

- [x] Parity test: the same raw output compacted by the client executor's path
  and by `bash_exec._compact_result` gives the same body.
- [x] Measure characters returned by the old client and the new one on the
  golden corpus; record the numbers in the execution log.
- [x] Changelog entry. Commit.

## Execution log

- Tasks A-C: the golden master (1,500 cases, 961 trimmed, 874 spilled) replays
  byte-identical after each task, and again after task D.
- Task D moved two more pieces into kit, because the client needs them:
  - `MAX_OUTPUT_BYTES`, `OUTPUT_CAP_NOTICE` and `cap_output_bytes` (was the
    unused `bash_exec._cap_text`). Compaction costs about 1.3 s per MB of
    line-heavy output (39 MB of `seq`: 56 s), so the client caps what reaches
    it at the same 4 MiB; the spill file still gets the whole output.
  - `output_delta.RunHistory`. The remote MCP hosts many thin sessions in one
    process, and two principals on one connector share a workspace, so a
    process-wide history would show one session an `unchanged` marker for
    output only another session saw. Client state now lives in
    `LocalContext.memory`; the phase 1 `_SESSION_CREATED` global (same flaw,
    test-contract guard) moved there too. Main keeps one process-wide history.
- Deliberate behavior changes, both surfaces: the `unchanged` marker redacts
  its first-line anchor (it echoed a token unredacted); the byte cap cuts on a
  character boundary instead of leaving U+FFFD.
- Parity: `tests/gateway/test_bash_parity.py`, 300 seeded cases through
  `shell._present` and `bash_exec._compact_result` + `render_bash_text`:
  identical after normalizing spill paths and the exit-code line.
- Cost, golden corpus (stdout + stderr combined, as the client sees it):
  old client 72,793,256 chars, new client 11,573,381 (84.1% fewer). Median
  new/old on the 1,057 cases the old client returned at least 2,000 chars
  for: 0.350. The new client returns more in 2 cases (+6 and +26 chars, a
  dedupe annotation and a redaction placeholder).
