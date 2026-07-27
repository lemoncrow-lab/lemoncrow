<!-- lc:section write -->

## Tool discipline

Always use LemonCrow for every file read, search, edit and shell command — every one, no exceptions. ONE `edit` call carries every hunk across every file, ONE `read` call every path and range as a minified projection, independent calls go in ONE message — each round-trip skipped never re-bills the conversation — use lc: `bash`, `read`, `edit`, `code_search`.

- **Known path → straight to `read`**; otherwise start with `code_search`. Inline source is already read; `related_symbols`/`candidate_files` cover every site.
- **`bash` = execution only.** Never shell `sed`/`cat`/`head`/`tail`/grep to read, search, or recheck indexed results.
- Large output → a file, never prose.

<!-- lc:end -->

<!-- lc:section read-only -->

## Tool discipline

Always use LemonCrow for every file read and search — every one, no exceptions. ONE `read` call returns every path and range as a minified projection, independent calls go in ONE message — each round-trip skipped never re-bills the conversation — use lc: `bash`, `read`, `code_search`.

- **Read-only — `bash` never mutates.** Inspection/validation only: no redirects, `sed -i`, `tee`, or Git state changes.
- **Known path → straight to `read`, no `code_search`.** Task, error, or stack trace names the file → don't explore first; otherwise start with `code_search`. Never shell `sed`/`cat`/`head`/`tail`/grep to read, search, or recheck indexed results.

<!-- lc:end -->
