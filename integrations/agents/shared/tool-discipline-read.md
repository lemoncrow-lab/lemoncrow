## Tool discipline

- **Read-only role — `bash` never mutates.** Inspection and validation only; no redirects into the tree, no `sed -i`/`tee`, no git state changes.
- **Known path → `read`; `bash` = execution only.** Never `sed`/`cat`/`head`/`tail` or grep chains; `code_search` BEFORE reading or grepping — never re-verify its results with shell grep.
- **Batch independent calls.** Independent reads and searches in one turn; serialize only when one output feeds the next.

Host tools disabled — use Atelier: `bash`, `read`, and `code_search` / `explore` for search.
