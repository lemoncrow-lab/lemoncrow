---
id: WP-22
title: Optional deterministic MCP tool `edit` (batched edits)
phase: C
pillar: 3
owner_agent: atelier:code
depends_on: []
status: done
---

# WP-22 — Optional deterministic batch edits

## Why

Apply many mechanical edits across many files in one explicit tool call. Each
round-trip the agent saves on individual edit acknowledgements is real money, but this packet must
not compete with host-native edit tools for ordinary coding.

## Implementation boundary

- **Host-native:** Claude, Codex, Copilot, and other hosts keep ownership of their normal
  Edit/MultiEdit/apply-patch flows, diff presentation, conflict handling, and user approval UX.
- **Atelier augmentation:** `edit` is an optional deterministic executor for large
  mechanical changes, benchmark replay, and cases where one audited atomic operation is cheaper
  than many host edit acknowledgements.
- **Not in scope:** do not intercept, monkeypatch, replace, or make mandatory the host's native edit
  tools.

## Files touched

- `src/atelier/core/capabilities/tool_supervision/batch_edit.py` — new
- `src/atelier/gateway/adapters/mcp_server.py` — edit (register tool)
- `src/atelier/gateway/adapters/cli.py` — edit (`batch-edit --from <file.json>`)
- `tests/core/test_batch_edit_atomicity.py`
- `tests/infra/test_batch_edit_round_trip.py`

## How to execute

1. Input:

   ```json
   &#123;
     "edits": [
       &#123;
         "path": "src/foo.py",
         "op": "replace",
         "old_string": "...",
         "new_string": "..."
       &#125;,
       &#123;
         "path": "src/bar.py",
         "op": "insert_after",
         "anchor": "def baz",
         "new_string": "..."
       &#125;,
       &#123;
         "path": "src/baz.ts",
         "op": "replace_range",
         "line_start": 42,
         "line_end": 58,
         "new_string": "..."
       &#125;
     ],
     "atomic": true
   &#125;
   ```

2. Output:

   ```json
   &#123;
     "applied": [&#123;"path": "...", "hunks": [&#123;"line_start":..,"line_end":..&#125;]&#125;],
     "failed": [&#123;"path": "...", "error": "..."&#125;],
     "rolled_back": false
   &#125;
   ```

3. Atomicity: if `atomic=true` (default) and any edit fails, all already-applied edits are reverted
   from a per-call working copy. Use `git stash`-style mechanic: snapshot files into
   `.atelier/run/<session_id>/batch_edit_backup/` before starting; restore on failure.

4. Expose this as an explicit `edit` MCP tool and CLI command only. Do not route
   native host Edit/MultiEdit calls through it.

5. Honor the project safety protocol: never delete files; never operate outside the repo root.

6. Tests:
   - Atomic mode: one failing edit → no files touched
   - Non-atomic mode: failed edit reported but successful ones persisted
   - Backup directory cleaned up on success
   - Host integration docs still state native edit tools are the default path

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_batch_edit_atomicity.py \
                     tests/infra/test_batch_edit_round_trip.py -v

# CLI smoke
echo '&#123;"edits":[&#123;"path":"/tmp/x.txt","op":"replace","old_string":"a","new_string":"b"&#125;]&#125;' | \
  LOCAL=1 atelier batch-edit --from-stdin --json

make verify
```

## Definition of done

- [ ] Tool atomic by default
- [ ] All-or-nothing semantics verified by test
- [ ] Host-native edit tools remain the default documented path
- [ ] CLI mirror works
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
