# Validation Matrix

| Change surface | Minimum validation |
| --- | --- |
| Python runtime or CLI | `make lint && make typecheck && make test` |
| Frontend UI or API usage | `cd frontend && npm run build && npm run test` |
| Docs and repo scaffolding | `make docs-check && make check-agent-context` |
| Host instruction sources or generated host files | `make sync-agent-context && make check-agent-context` |
| Worktree bootstrap or runtime evidence scripts | `make docs-check && uv run pytest tests/gateway/test_generated_agent_contexts.py -q` |

## Notes

- Run the smallest targeted check first while iterating, then the broader project checks before concluding.
- `make verify` is the wide gate for repository changes and should include docs governance.
- Keep new validation paths inside existing tools and repo scripts whenever possible.
