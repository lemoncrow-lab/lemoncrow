<!-- lc:section write -->

## Tool discipline

- **Known path → straight to `read`**; otherwise start with `code_search`. Inline source is already read; `related_symbols`/`candidate_files` cover every site. Batch missing files into one `read`, all changes into one `edit`.
- **`bash` = Batch execution only.** Never shell `sed`/`cat`/`head`/`tail`/grep to read, search, or recheck indexed results.
- **Batch independent calls.** One turn; serialize only dependencies.
- Large output → a file, never prose.

Host tools disabled — use lc: `bash`, `read`, `edit`, `code_search`.

<!-- lc:end -->

<!-- lc:section read-only -->

## Tool discipline

- **Read-only — `bash` never mutates.** Inspection/validation only: no redirects, `sed -i`, `tee`, or Git state changes.
- **Known path → straight to `read`, no `code_search`.** Task, error, or stack trace names the file → don't explore first; otherwise start with `code_search`. Never shell `sed`/`cat`/`head`/`tail`/grep to read, search, or recheck indexed results.
- Batch independent reads/searches in one turn; serialize only dependencies.

Host tools disabled — use lc: `bash`, `read`, `code_search`.

<!-- lc:end -->
