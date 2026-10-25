"""The GitHub pull request reviewer: AI findings posted by a bot, in the customer's own CI.

``lc review --pr N --ai --publish-bot`` (the GitHub Action runs it) captures the
pull request into a throwaway Review store, runs the existing Auto AI review on
the customer's own model key, and posts the result as one summary comment plus
inline comments. Nothing here is a second review engine; each module owns one
step:

* ``ci_env`` -- the CI environment: event payload gating (drafts, forks, pushes)
  and the model route read from API-key variables. Standard library only, because
  ``action.yml`` runs it before LemonCrow is installed.
* ``findings`` / ``state`` -- the finding record and the hidden JSON block in the
  summary comment that carries it to the next run. No database survives a CI job.
* ``reconcile`` -- new, carried forward, fixed or stale, per finding.
* ``attribution`` -- the rule and context a finding cites, read from its validated refs, for the trailer.
* ``render`` -- the summary, inline comment and "Fix with your agent" text.
* ``github`` -- the bot's GitHub calls (comments, review, threads, status).
* ``engine`` -- capture and the in-process AI review stage.
* ``runner`` -- the whole run.

AI output never becomes human review state: reviews are posted with event
``COMMENT`` only, and the human publish path (``api.py``, ``github_publish``)
keeps refusing every non-human annotation.

Nothing is re-exported here, matching the parent package: import the submodule
you need from inside the function that needs it.
"""

from __future__ import annotations

__all__: list[str] = []
