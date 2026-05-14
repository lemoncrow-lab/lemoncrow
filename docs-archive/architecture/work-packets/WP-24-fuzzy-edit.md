---
id: WP-24
title: Fuzzy matching inside `edit` only (fuzzy edit)
phase: C
pillar: 3
owner_agent: atelier:code
depends_on: [WP-22]
status: done
---

# WP-24 — Fuzzy matching inside batch edit only

## Why

Tolerate whitespace / indentation drift in `old_string` so edits land on the first
try inside `edit`. This reduces the "old_string not found, retry" loop for explicit
batch edits without changing host-native edit behavior.

## Implementation boundary

- **Host-native:** host Edit/MultiEdit/apply-patch semantics, previews, approvals, and conflict
  handling stay owned by the host CLI.
- **Atelier augmentation:** fuzzy matching is available only as an opt-in flag on
  `edit` operations.
- **Not in scope:** do not hook, wrap, intercept, or relax matching for host-native edit tools.

## Files touched

- `src/atelier/core/capabilities/tool_supervision/batch_edit.py` — edit
- `src/atelier/core/capabilities/tool_supervision/fuzzy_match.py` — new
- `tests/core/test_fuzzy_match.py`
- `tests/infra/test_batch_edit_fuzzy.py`

## How to execute

1. Per-edit `fuzzy: bool` flag (default `false`) on `edit` only. When `true`:
   - Normalize whitespace in both `old_string` and the file content (collapse runs of spaces,
     ignore trailing whitespace on each line, ignore tab-vs-space).
   - Try exact match first; if that fails, run a windowed Levenshtein search.
   - Accept matches with normalized edit-distance ≤ `0.05 * len(old_string)` (5 %).
   - If multiple matches, fail loudly with the candidate ranges so the agent disambiguates.

2. Pure-Python implementation, no new dep. Use `difflib.SequenceMatcher` with `autojunk=False` for
   the candidate window, then verify with a hand-rolled bounded Levenshtein when the ratio is
   borderline.

3. Tests:
   - Indentation drift: 4-space vs tab → fuzzy match succeeds, exact fails
   - Trailing whitespace difference → fuzzy succeeds
   - Genuine ambiguity (two near-matches) → fail with both ranges reported
   - Host-native edit tools are not imported, monkeypatched, or reconfigured

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_fuzzy_match.py \
                     tests/infra/test_batch_edit_fuzzy.py -v

make verify
```

## Definition of done

- [ ] Fuzzy mode opt-in per edit; default exact
- [ ] Ambiguity raises rather than silently picking
- [ ] Fuzzy matching is scoped to `edit` only
- [ ] Pure stdlib, no new deps
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
