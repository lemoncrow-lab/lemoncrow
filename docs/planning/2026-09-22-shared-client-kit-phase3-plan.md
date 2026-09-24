# Shared client kit, phase 3: grep and the offline read

**Goal:** one grep engine and one read-path grammar. Main's
`native_search.search_workspace`, its mode names and its grep text renderer
move into `lemoncrow_client.kit.search`; the thin client's `grep` runs it and its
own engine is deleted. Main's read-path parsing (`read_path.py`) moves into
`lemoncrow_client.kit.read_path`; the client's offline `read_from_disk` parses
paths with it.

**Spec:** `docs/planning/2026-09-22-shared-client-kit-design.md`.

## Global constraints

- Kit imports only the standard library, `lemoncrow_client.errors` and kit;
  `kit/fsio.py` stays its only writer.
- Main's behavior does not change: a golden master of `search_workspace` over
  a fixture repository, with `rg`, with `grep` only and with neither, replays
  byte-identical.
- Main-only capabilities plug in through `SearchHooks`.

## Side by side: grep

| Feature | Main | Client before | After |
| --- | --- | --- | --- |
| Modes | content, ranked map (scored, symbol-aware ranges, `next` actions), paths only, path + count | content (`path:line:text`), paths, ranked (count order) | main's four, on both |
| Output budget | content capped at `context_budget_tokens` (2000 tokens); larger results spill to a JSON file | 500 matches, 400-char lines | main's, on both |
| Symbol-aware windows, docstring collapse, auto outline of large code files, notebooks, images | yes | no | both |
| `path:Lx-Ly` scoping, `#imports` / `#imported-by` | yes | no | both |
| `file_limit`, `lines_per_file`, `summary`, `if_modified_since`, `include_meta` | yes | no | both |
| Invalid regex | falls back to a literal match | refused | falls back, on both |
| Candidate files | `rg` (honors `.gitignore`) or a walk skipping build/VCS dirs | the manifest walk (`.gitignore`, same set the server indexes) | main unchanged; the client's walk decides which files the engine may search |
| `rg` / `grep` subprocess fast path | yes | never shells out | main only (hook) |
| Third-party `regex` engine with a per-call timeout | yes | no | main only (hook); the kit bounds `re` by line length and a deadline, as main does without `regex` |
| PDF text (`pypdf`) | yes | no | main only (hook) |
| Read-cost baseline for `tokens_saved` | yes | no | main only (hook) |
| Call-graph badges, search verdicts | yes (index, ledger) | no | main only |

## Side by side: offline read

| Feature | Main `read` | Client `read_from_disk` | After |
| --- | --- | --- | --- |
| Path grammar (`:L10-L20`, `:10-20`, `:full`, `:head=N`, `:tail=N`, chained suffixes) | `split_file_opts` | `:L10-L20`, `head=`, `tail=`, one suffix | kit's `split_file_opts` on both |
| Outline, summary, symbol reads | index | refused or plain text | main only (needs the index) |

## Tasks

### 3a: the grep engine into kit

- [x] Golden master: fixture repository and a corpus of `search_workspace`
  calls across every mode and option, recorded with `rg`, with `grep` only and
  with neither.
- [x] Move `native_search.py` into `kit/search.py` verbatim except the hooks;
  `native_search.search_workspace` passes main's hooks; move
  `GREP_MODE_CANON`, `GREP_MODE_ALIASES`, `normalize_grep_mode` and
  `_render_grep_md` (as `render_grep_text`). Update importers and tests.
- [x] Replay: identical. Run the search suites. Commit
  `refactor(grep): move the search engine into kit`.

### 3b: client grep runs the kit engine

- [x] Tests first: content mode shows symbol-aware windows; ranked map;
  `path:Lx-Ly`; gitignored files stay unsearched; a large result spills to a
  file the client names; an invalid regex matches literally.
- [x] `localtools/search.py`: map the arguments, pass the manifest walk as the
  file universe and a spill hook, render with `render_grep_text`. Delete the
  old engine. Commit `feat(client): grep runs the kit search engine`.
- [x] Parity test: the client and `tool_grep`'s engine return the same text.

### 3c: read-path grammar

- [x] `git mv src/lemoncrow/gateway/tools/read_path.py` into kit; rewrite
  importers.
- [x] `read_from_disk` parses with `split_file_opts`; tests for `:10-20`,
  chained suffixes and `head=`/`tail=`. Commit.
- [x] Changelog entry and execution log. Commit.

## Execution log

- Golden master: 1,212 calls (random plus hand-written) over a fixture git
  repository with code, docs, notebooks, an image, a PDF, a binary, a file
  over the 5 MB cap, a 30,000-char line, ignored files and skipped
  directories; recorded on the `rg`, `grep`-only and pure-Python backends,
  3,636 rows. The move replayed byte-identical.
- Main's own backends disagree on 297 (`rg` vs Python) and 334 (`grep` vs
  Python) of the 1,212 calls: `.gitignore` handling, brace globs, regex
  dialects. Parity between the surfaces is therefore pinned on the shared
  engine with main's Python backend, not on `rg`.
- Found while recording: main's `rg` fast path kept `rg`'s thread-dependent
  print order, so two runs of one search could return different files under
  `file_limit` (20 of 1,212 calls differed between two runs). Fixed in the kit
  (sort by path); the recording pins `rg --sort path` until then. After the
  fix only 27 `grep`-backend rows change, in order, and three full runs with
  parallel `rg` are identical.
- Deliberate behavior change on both surfaces: an unknown `type` filter is
  refused with the known names (main used to ignore it and search every
  file). `go`, `rust`, `toml` and `shell` were added, and `python`/
  `javascript` now include `.pyi` and `.mjs`/`.cjs`, as the client had. Only
  the 276 unknown-type rows change.
- Client changes: output format, modes, budgets and spill are main's; the
  file universe is the manifest walk (listing only, `read_limit=0`, so a grep
  no longer hashes the tree); a named file is searched even when ignored, as
  `rg` does; images come back as MCP image blocks; an unrenderable result is
  compact JSON, as on main. Main's JSON spill caps content at the token
  budget before spilling, so the spill holds the capped content, not every
  match.
- Parity: `tests/gateway/test_grep_parity.py`, 250 seeded calls through the
  client executor and main's `tool_grep` + renderer + presentation: identical
  after normalizing spill paths; at least 5 spills and 10 empty results.
- Offline read: `read_path` moved into the kit; `read_from_disk` and the
  dispatcher's absolute-path routing parse entries with `split_file_opts` and
  main's dict keys. The offline rendering is unchanged (it is flagged
  degraded, and main's rendering comes from the index).
