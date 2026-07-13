<!-- lc:section write -->
## Tool discipline

- **One search → one bulk edit.** Start with `code_search`; inline source is already read, and `related_symbols`/`candidate_files` cover every site. Batch each missing file once into one `read`, then all changes into one `edit`.
- **Known path → `read`; `bash` = execution only.** Never use shell `sed`/`cat`/`head`/`tail`/grep to read, search, or recheck indexed results.
- **Batch independent calls.** One turn; serialize only dependencies.
- Large output → a file, never prose.

Host tools disabled — use lc: `bash`, `read`, `edit`, `code_search`.
<!-- lc:end -->

<!-- lc:section read-only -->
## Tool discipline

- **Read-only — `bash` never mutates.** Inspection/validation only: no redirects, `sed -i`, `tee`, or Git state changes.
- **Known path → `read`; `bash` = execution only.** Start with `code_search`; never use shell `sed`/`cat`/`head`/`tail`/grep to read, search, or recheck indexed results.
- Batch independent reads/searches in one turn; serialize only dependencies.

Host tools disabled — use lc: `bash`, `read`, `code_search`.
<!-- lc:end -->
