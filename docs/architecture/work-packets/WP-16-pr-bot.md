---
id: WP-16
title: Optional GitHub PR bot for promoted lessons
phase: D
pillar: 2
owner_agent: atelier:code
depends_on: [WP-15]
status: done
---

# WP-16 — Lesson PR bot

## Why

Lemma's pitch is auto-PR for proposed fixes. We mirror that for ReasonBlock changes: when a
`LessonCandidate` is approved, an opt-in bot opens a PR adding the new block markdown under
`./.knowledge/blocks/` so the team can review on GitHub.

This is **opt-in** and runs only when `ATELIER_LESSON_PR_BOT_ENABLED=true` and a `GITHUB_TOKEN`
is present. CI must pass in both modes.

## Implementation boundary

- **Host-native:** Git operations, GitHub authentication, PR creation, CI, and review workflows stay
  owned by `git`, `gh`, GitHub, and the host CLI's existing GitHub tooling.
- **Atelier augmentation:** Atelier only converts an approved `LessonCandidate` into a reviewable
  ReasonBlock patch and invokes `gh` through a narrow, disabled-by-default wrapper.
- **Not in scope:** do not build a general PR bot platform, CI orchestrator, review agent, or GitHub
  client library inside Atelier.

## Files touched

- `src/atelier/core/capabilities/lesson_promotion/pr_bot.py` — new
- `src/atelier/gateway/adapters/cli.py` — edit (`lesson sync-pr <id>`)
- `tests/core/test_pr_bot_dry_run.py`
- `tests/gateway/test_pr_bot_skipped_when_disabled.py`
- `docs/engineering/lesson-pipeline.md` — new
- `.env.production.example` — append `ATELIER_LESSON_PR_BOT_ENABLED=` and `GITHUB_TOKEN=`

## How to execute

1. PR bot uses `gh` CLI (already required for the project; do **not** add new Python deps).
2. On approval:

- Write the new/updated block to `./.knowledge/blocks/<id>.md` exactly as `extract_reasonblock`
  would.
- `git add` only that file.
- Create a branch `atelier/lesson/<lesson_id>`.
- Commit with `co-author: Atelier Lesson Bot <bot@atelier>`.
- `gh pr create` with body templated from the lesson's evidence trace IDs.

3. Dry-run mode (`--dry-run`) prints the diff and the would-be PR body without writing.
4. When disabled, all bot calls return `&#123; skipped: true, reason: "disabled" &#125;` — no `git` or `gh`
   subprocess invoked.
5. The enabled path must shell out only to the host-native `git`/`gh` tools; it must not implement
   its own GitHub API client.

> **Hard rule:** never push to `main`, never use `--force`, never bypass hooks. Match the project
> safety protocol in `CLAUDE.md` § 1.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_pr_bot_dry_run.py \
                     tests/gateway/test_pr_bot_skipped_when_disabled.py -v

# Disabled-default smoke
unset ATELIER_LESSON_PR_BOT_ENABLED
LOCAL=1 uv run atelier lesson sync-pr <some_id> --dry-run | grep -q "skipped"

make verify
```

## Definition of done

- [x] Bot disabled by default
- [x] Disabled path takes zero side-effects
- [x] Enabled path opens a branch + PR via `gh`, never pushes to `main`, never `--force`s
- [x] No general GitHub workflow engine or custom PR API client added
- [x] Docs cover env-var setup and security model
- [x] `make verify` green
- [x] `INDEX.md` updated; trace recorded
