---
id: WP-11
title: AST outline-first injection for files > 200 LOC
phase: C
pillar: 3
owner_agent: atelier:code
depends_on: []
status: done
---

# WP-11 — AST-aware truncation

## Why

Large TS/JS files can use **40-60 % fewer tokens read** when returned as signatures during
exploration and only expanded once narrowed. We extend the existing `semantic_file_memory`
capability to do the same for Python and TypeScript. Tree-sitter is already a dep.

## Files touched

- `src/atelier/core/capabilities/semantic_file_memory/capability.py` — edit
- `src/atelier/core/capabilities/semantic_file_memory/python_ast.py` — edit (add `outline()`)
- `src/atelier/core/capabilities/semantic_file_memory/typescript_ast.py` — edit (add `outline()`)
- `src/atelier/gateway/adapters/mcp_server.py` — edit: `read` returns outline for files > 200 LOC unless `expand=true` or `range=L1-L2`
- `tests/core/test_python_outline.py`
- `tests/core/test_typescript_outline.py`
- `tests/infra/test_smart_read_outline_first.py`

## How to execute

1. Outline format (same for both languages):

   ```python
   class FileOutline(BaseModel):
       path: str
       lang: Literal["python", "typescript", "tsx", "javascript"]
       loc: int
       symbols: list[Symbol]   # class/function/method, with line ranges
       imports: list[str]
       hint: str = "Pass range=L1-L2 or expand=true for full body"
   ```

2. Threshold: files **> 200 effective LOC** (excluding blank + comment-only lines) return the
   outline by default. ≤ 200 → return full content as today.

3. Symbol extraction: use existing tree-sitter query helpers. For Python: `class_definition`,
   `function_definition`, `decorated_definition`. For TS/TSX/JS: `class_declaration`,
   `function_declaration`, `method_definition`, `arrow_function` _only_ when bound to a top-level
   `const`.

4. `read` API:
   - Existing: `&#123;file_path&#125;` → `&#123;content, cache_hit, tokens_saved&#125;`
   - Extended: `&#123;file_path, [range, expand=false]&#125;` →
     `&#123;outline?, content?, cache_hit, tokens_saved, mode: "outline"|"range"|"full"&#125;`

5. Tests:
   - 600-LOC sample Python file → outline returned by default
   - same file with `expand=true` → full content
   - same file with `range="42-118"` → only those lines
   - tokens_saved > 0 in outline mode

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_python_outline.py \
                     tests/core/test_typescript_outline.py \
                     tests/infra/test_smart_read_outline_first.py -v

make verify
```

## Definition of done

- [x] Outline correctness verified for both languages on real fixtures
- [x] Token-savings reported in tool result
- [x] Backward-compat: callers that omit `expand`/`range` and read small files see no change
- [x] `make verify` green
- [x] `INDEX.md` updated; trace recorded
