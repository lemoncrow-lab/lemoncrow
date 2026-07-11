## Tool discipline

- **One search → one bulk edit.** `code_search` first — inline source = already read; `related_symbols`/`candidate_files` = every site. `read` only what's missing, all files ONE call, never repeat a file. ALL edits ONE `edit` `edits[]` array.
- **Known path → `read`; `bash` = execution only.** Never `sed`/`cat`/`head`/`tail`/grep for reads or search — `code_search` is the full index, never re-verify with shell grep.
- **Batch independent calls.** One turn for independent reads/searches/probes; serialize only when output feeds input.
- **Large output → a file, never prose.**

Host tools disabled — use LemonCrow: `bash`, `read`, `edit`, `code_search`.
