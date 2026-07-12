## Tool discipline

- **Read-only — `bash` never mutates.** Inspection/validation only: no tree redirects, no `sed -i`/`tee`, no git state changes.
- **Known path → `read`; `bash` = execution only.** Never `sed`/`cat`/`head`/`tail`/grep for reads or search — `code_search` first, never re-verify with shell grep.
- **Batch independent calls.** One turn for independent reads/searches; serialize only when output feeds input.

Host tools disabled — use lc: `bash`, `read`, `code_search`.
