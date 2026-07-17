<!-- lc:section write -->

## Tool discipline

- **Known path → straight to `read`**.
- **Known path → Start with `code_search`** Inline source is already read, and `related_symbols`/`candidate_files` cover every site. Batch each missing file once into one `read`, then all changes into one `edit`.
- **`bash` = execution only.** Never use shell `sed`/`cat`/`head`/`tail`/grep to read, search, or recheck indexed results.
- **Batch independent calls.** One turn; serialize only dependencies.
- Large output → a file, never prose.
- **Graphical data → render and look.** Output meant to be seen (plots, rendered text, pixel grids, UI) → write a PNG and read the image; don't infer visuals from raw bytes or coordinates.

Host tools disabled — use lc: `bash`, `read`, `edit`, `code_search`.

<!-- lc:end -->

<!-- lc:section read-only -->

## Tool discipline

- **Read-only — `bash` never mutates.** Inspection/validation only: no redirects, `sed -i`, `tee`, or Git state changes.
- **Known path → straight to `read`, no `code_search`.** Task, error, or stack trace already names the file — don't explore first; otherwise start with `code_search`. Never use shell `sed`/`cat`/`head`/`tail`/grep to read, search, or recheck indexed results.
- Batch independent reads/searches in one turn; serialize only dependencies.

Host tools disabled — use lc: `bash`, `read`, `code_search`.

<!-- lc:end -->
