---
id: WP-21
title: MCP tool `search` (combined search + read)
phase: C
pillar: 3
owner_agent: atelier:code
depends_on: [WP-10]
status: done
---

# WP-21 — Combined search + read

## Why

Collapse `grep → read → read` into one tool that returns ranked
snippets _and_ the surrounding content. Removes a turn-tax on every search.

## Implementation boundary

- **Host-native:** shell search, `rg`, host `Grep`, host `Read`, and direct file inspection remain
  available for exact raw exploration.
- **Atelier augmentation:** `search` is a deterministic context compiler that combines
  ranked snippets, outlines, cache status, and token accounting for common search-to-read loops.
- **Not in scope:** do not build a replacement shell, universal search engine, editor, or file
  browser.

## Files touched

- `src/atelier/core/capabilities/tool_supervision/search_read.py` — new
- `src/atelier/gateway/adapters/mcp_server.py` — edit (register tool)
- `src/atelier/gateway/adapters/cli.py` — edit (`search-read` command)
- `tests/core/test_search_read.py`
- `tests/infra/test_search_read_token_savings.py`

## How to execute

1. Tool input: `&#123; query, [path], [max_files=10], [max_chars_per_file=2000], [include_outline=true] &#125;`
2. Tool output:

   ```json
   &#123;
     "matches": [
       &#123;
         "path": "src/foo.py",
         "lang": "python",
         "snippets": [
           &#123;"line_start": 42, "line_end": 58, "score": 0.91, "text": "..."&#125;,
           &#123;"line_start": 110, "line_end": 124, "score": 0.74, "text": "..."&#125;
         ],
         "outline": &#123;...&#125;,
         "tokens": 612
       &#125;
     ],
     "total_tokens": 5400,
     "tokens_saved_vs_naive": 8200,
     "cache_hit": false
   &#125;
   ```

3. Implementation:
   - Use cached_grep (WP-10) for the search.
   - Cluster matches per file; expand each match by N lines of context (configurable, default 8).
   - When a file has > 5 matches, return its AST outline (WP-11) plus the top-3 snippets, not the
     whole file.
   - Use `tiktoken` to compute `tokens` and `tokens_saved_vs_naive` (where naive = grep output +
     whole-file read for each hit file).
   - Keep raw host search/read available; this tool is an optimized path, not the only allowed path.

4. CLI: `atelier search-read --query "..." --path src/ --json`

5. Tests:
   - On a fixture corpus with 20 hit files, the combined tool returns ≤ 30 % of the tokens that
     `grep + read each file` would have.
   - Result is deterministic for the same inputs.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_search_read.py \
                     tests/infra/test_search_read_token_savings.py -v

LOCAL=1 atelier search-read --query "ReasonBlock" --path src --json | head -50

make verify
```

## Definition of done

- [ ] Tool registered, deterministic, AST-aware
- [ ] ≥ 70 % token reduction vs naive on the fixture corpus
- [ ] Host-native search/read fallback documented
- [ ] CLI mirror works
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
