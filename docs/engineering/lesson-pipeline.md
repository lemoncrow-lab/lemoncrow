# Lesson Pipeline PR Sync

The lesson PR bot is an optional wrapper that turns an approved lesson candidate into a narrow, reviewable GitHub pull request for the corresponding ReasonBlock markdown patch.

## Security model

- Disabled by default.
- Requires both `ATELIER_LESSON_PR_BOT_ENABLED=true` and `GITHUB_TOKEN`.
- Uses host-native tools only: `git` and `gh`.
- Never pushes directly to `main`.
- Never uses force flags.

When disabled, `atelier lesson sync-pr` returns a skipped payload and performs zero subprocess side effects.

## Environment variables

```bash
ATELIER_LESSON_PR_BOT_ENABLED=false
GITHUB_TOKEN=
```

Set both values only in secure runtime environments where GitHub PR automation is explicitly approved.

## Usage

Dry run (safe preview):

```bash
uv run atelier --root .atelier lesson sync-pr <lesson_id> --dry-run --json
```

Enabled execution:

```bash
export ATELIER_LESSON_PR_BOT_ENABLED=true
export GITHUB_TOKEN=...
uv run atelier --root .atelier lesson sync-pr <lesson_id> --json
```

## Behavior

1. Resolve approved lesson candidate by ID.
2. Build ReasonBlock markdown patch under `./.knowledge/blocks/<block_id>.md`.
3. Create or checkout branch `atelier/lesson/<lesson_id>`.
4. Commit only the block file with co-author trailer.
5. Open PR using `gh pr create`, with evidence trace IDs in body.

## Troubleshooting

- `&#123;"skipped": true, "reason": "disabled"&#125;`
  Verify both env vars are present and non-empty.
- `lesson must be approved before sync-pr`
  Approve the candidate first via `atelier lesson approve`.
- `no_reasonblock_patch`
  Candidate has no promotable block artifact; only block-based lesson kinds are eligible.
