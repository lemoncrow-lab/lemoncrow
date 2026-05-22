---
id: template-python-fastapi-ruff-black-formatting
title: Ruff And Black Formatting Gate
domain: coding.python-fastapi
task_types:
  - feature
  - refactor
  - test
triggers:
  - Ruff
  - Black
  - formatting
  - lint
file_patterns:
  - "**/*.py"
  - "pyproject.toml"
tool_patterns:
  - ruff
  - black
situation: "A Python code change is ready for validation. TODO: replace command examples with your repo's make targets."
dead_ends:
  - "Declaring completion before local formatting/lint checks run."
  - "Hand-formatting around a formatter instead of using the configured tools."
procedure:
  - "Run the repository's formatter command or Black-compatible target."
  - "Run Ruff linting on the touched files or project subset."
  - "Fix reported issues in source rather than suppressing rules casually."
  - "If a suppression is necessary, keep it narrow and explain it in code review."
verification:
  - "Confirm formatter reports no diff."
  - "Confirm Ruff exits successfully on the touched files or documented subset."
failure_signals:
  - "CI formatting check changes files after the agent reports done."
  - "A broad noqa or per-file ignore hides unrelated issues."
required_rubrics: []
when_not_to_apply: "Documentation-only changes with no Python source edits."
---

TODO: Add your exact lint and format commands.
