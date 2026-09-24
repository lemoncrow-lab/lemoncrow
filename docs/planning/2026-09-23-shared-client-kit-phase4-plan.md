# Shared client kit, phase 4: sql, codemod, scan, blame

**Goal:** the last client-side tools run the main package's implementations.
`sql` and its renderer, the ast-grep adapter behind `codemod`, and the
security scanner behind `scan` move into `lemoncrow_client.kit`; the thin client
runs them. `blame` has no shareable engine and only gets its contract default.

**Spec:** `docs/planning/2026-09-22-shared-client-kit-design.md`.

## Global constraints

- Kit imports only the standard library, `lemoncrow_client.errors` and kit;
  `kit/fsio.py` stays its only writer.
- Main's behavior does not change: golden masters of `sql_tool` and of the
  ast-grep adapter and scanner replay byte-identical.
- The client never downloads ast-grep; it uses the one on PATH or refuses by name.

## Side by side

| Tool | Main | Client before | After |
| --- | --- | --- | --- |
| `sql` actions | connect, tables, schema, table, relationships, search, lint, query | query, lint, table, search | main's eight, on both |
| `sql` safety | explicit `connection` unless `LEMONCROW_SQL_AUTODISCOVER=1`; lint refuses multi-statement, data-modifying CTEs, ATTACH/DETACH/GRANT/REVOKE/VACUUM; `PRAGMA query_only` without `write`; DSN sandboxed to the repo | auto-discovers from env and `.env`; verb regex; `mode=ro` | main's, on both (the public contract already says discovery is opt-in) |
| `sql` output | auto-`LIMIT`, `max_rows`, cells over 4 KB bounded with a spill, compact tables | 200 rows, plain table | main's, on both |
| Non-SQLite DSN | "driver required" note, no driver loaded | refused by name | main's note, on both |
| `codemod` | ast-grep search, or a rewrite with a unified-diff preview (`dry_run=true` default) or apply, reporting `files_changed` | runs ast-grep; **applies a rewrite unless `dry_run` is passed**, then hashes the tree to find changes | main's, on both; the client no longer rewrites by default |
| `scan` | SAST: bundled OWASP/CWE ast-grep rules plus a Python taint check | runs the project's own ast-grep rules | main's, on both (it is the public contract) |
| ast-grep binary | PATH, or a managed download | PATH only | unchanged; the download stays main-only |
| `blame` | symbol resolution through the index, `pygit2` annotation and churn | `git blame` CLI; `include_churn` defaults to false | engines unchanged (the index and `pygit2` are main-only); the client honours the contract default `include_churn=true` |

## Tasks

### 4a: sql

- [x] Golden master: `sql_tool` and main's `tool_sql` + renderer over fixture
  databases, DSN spellings (inside/outside the repo, `file:` URIs, memory,
  encoded escapes, other dialects) and every action, read and write.
- [x] Move `sql_tool.py`'s engine into `kit/sql.py`, `SqlPathError` with it,
  `_render_sql_md` as `render_sql_text`, and `sql_auto_limit`/
  `postgres_try_auto_fix`; main keeps a wrapper that adds its cell spill.
  Replay: identical. Commit.
- [x] Client: tests first (actions, opt-in discovery, write gate, sandbox),
  then `localtools/sqltool.py` runs the kit with main's argument handling.
  Parity test. Commit.

### 4b: codemod and scan

- [x] Golden master: the adapter's search, rewrite (dry run and apply) and
  scan, and `scan_repository`, over a fixture repository.
- [x] Move the adapter (binary passed in) and `security/` into kit; main
  resolves the binary (with its download) and renders rules to YAML. Replay:
  identical. Commit.
- [x] Client: tests first (`codemod` previews by default and applies with
  `dry_run=false`; `scan` reports bundled-rule and taint findings), then the
  executors. Commit.

### 4c: blame

- [x] `include_churn` defaults to true, as the contract says. Commit.
- [x] Changelog entry and execution log. Commit.

## Execution log

- **4a sql.** Golden master of 2,728 rows (handler text and database side
  effects over 27 DSN spellings, every action, the env switches; `lint_sql`,
  `sql_auto_limit`, discovery, the renderer). The verbatim move replayed
  byte-identical (`3c2144444`). The fixes (`ecc3a8100`) changed 233 handler and
  raw rows, 64 lint rows and 2 discovery rows, each explained by one of: reads
  open `mode=ro` (a `PRAGMA journal_mode(wal)` read no longer rewrites the file;
  a missing file is refused instead of created, including DSNs that used to
  raise `OperationalError`), a relative DSN resolves under the repo root (the
  old code sandbox-checked one file and opened another when `repo_root` was not
  the working directory), `=0` switches are off, lint compiles with `EXPLAIN`
  and checks `queries[]`, refusals name the switch, `rows_affected`, and
  `.env` `export` lines. `kit/present.py` took main's empty-value stripping and
  JSON fallback so the client presents payloads as main does (`2cbe42b61`).
  Client parity: 300 seeded calls, same text and same database (`bd0b12e2f`).
- **4b codemod and scan.** Golden master of 805 rows: adapter search (702),
  rule scan, rewrite (dry run and apply, file contents and modes),
  `scan_repository`, taint, discovery, and main's `scan`/`codemod` text. Two
  recordings of unchanged main already disagreed on 58 rows: ast-grep prints
  matches in thread order, and main truncated and concatenated in that order.
  After the move (`c766edd70`), every divergence is the same result in path
  order, or a truncation that now keeps the first N matches in path order.
  Rules render as JSON (valid YAML; verified against ast-grep 0.45.3), so the
  kit needs no `yaml`. Two further fixes, each replayed and classified: 1-based
  lines in codemod (`e4a39dbbe`; every changed row is exactly +1), and a
  skipped rule pack named in `summary.rules_skipped` plus the real match total
  on a truncated search (`d20623826`; 514 search rows gain their total). Main
  keeps its managed download (`astgrep_adapter`) and its index-backed Python
  matcher. Client parity with main's handlers: 10 scans and 43 codemod calls,
  same text and same files written (`93f1d8fdd`).
- **4c blame.** `include_churn` defaults to true; line-level blame always
  shows, and churn heads it (`fix(client): blame defaults include_churn`).
- **Retrieval cache.** The engine caches `code.pattern` payloads under a
  fingerprint of the retrieval sources. The ast-grep adapter left the
  fingerprinted `infra/code_intel` for the kit, so a kit-only change would have
  kept serving payloads from older code; the fingerprint now hashes
  `lemoncrow_client.kit` too, and still falls back to the package version when
  the retrieval sources are unreadable (`fix(cache): fingerprint the client
  kit`).
