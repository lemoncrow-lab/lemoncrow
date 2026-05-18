# Contributing

Thanks for helping with Atelier.

Atelier is early, experimental, and especially open to contributors who want to help shape the core abstractions before they harden.

Start here: [docs/engineering/contributing.md](docs/engineering/contributing.md)

Good areas to help:

- MCP integrations
- coding-agent workflows
- Python backend/runtime work
- memory and retrieval
- evals and benchmarks
- docs, tutorials, and examples
- security review

## Scope boundaries

Atelier is governance for AI-assisted coding. It does not own the agent loop, replace CI, replace linting, become a Slack/GitHub bot, auto-apply lesson candidates, or add a web editor for ReasonBlocks. Before proposing adoption tooling, read [docs/architecture/POSITIONING_AND_ADOPTION.md](docs/architecture/POSITIONING_AND_ADOPTION.md), especially the "What NOT to build" list.

Before opening a pull request, please run:

```bash
make pre-commit
```

To enable the repository-managed pre-push hook:

```bash
git config core.hooksPath .githooks
```

That hook runs `make install` before push-time validation.

Small issues, design critiques, bug reports, docs fixes, and "this abstraction feels wrong" feedback are all welcome.
