# Shared client kit, phase 1 (edit): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `lemoncrow_client.kit.edit` the only edit engine, used by both the
thin client's `edit` executor and the main package's `edit` handler.

**Architecture:** Move `rich_edit.apply_rich_edits` and its helpers into a
standard-library-only kit module. Behavior that needs the main package (symbol,
projection, minified and fuzzy matching, reindex, payload preservation) becomes
optional hooks that only `rich_edit.py` passes. The client executor becomes a
thin wrapper that keeps its path checks, blob push and one-line summaries.

**Tech stack:** Python 3.12+, standard library only inside `lemoncrow_client`,
pytest, mypy strict, ruff, black.

**Spec:** [2026-09-22-shared-client-kit-design.md](2026-09-22-shared-client-kit-design.md)

## Global constraints

- `lemoncrow_client` keeps `dependencies = []` and imports only the standard
  library (`client/tests/test_packaging_audit.py`).
- kit imports only the standard library, `lemoncrow_client.errors` and kit.
- `kit/fsio.py` is kit's only module that writes files.
- No threads, `atexit`, signal handlers or network in kit.
- Client executors import kit lazily (inside the executor function).
- Old main-package copies are deleted in this change; no compatibility aliases.
- All commands run from `.lc-worktrees/shared-client-kit` with `uv run`.
- Commit messages end with the two trailer lines required for this session.

## File map

| File | Change | Responsibility |
| --- | --- | --- |
| `client/src/lemoncrow_client/kit/__init__.py` | create | package docstring and rules, no imports |
| `client/src/lemoncrow_client/kit/fsio.py` | create | capped UTF-8 reads, atomic writes, snapshots, restore |
| `client/src/lemoncrow_client/kit/contract_guard.py` | create | test-weakening guard (from `mcp_server.py`) |
| `client/src/lemoncrow_client/kit/edit.py` | create | edit engine (from `rich_edit.py`), aliases, hooks |
| `src/lemoncrow/pro/capabilities/tool_supervision/rich_edit.py` | rewrite | main-package hooks + `apply_rich_edits` wrapper |
| `src/lemoncrow/pro/capabilities/tool_supervision/path_safety.py` | delete | constant moved to `kit.edit.PROTECTED_PARTS` |
| `src/lemoncrow/gateway/adapters/mcp_server.py` | modify | call kit for guard, snapshots, aliases, content check |
| `src/lemoncrow/gateway/adapters/mcp/tools_edit.py` | modify | import `parse_target`, aliases, message from kit |
| `client/src/lemoncrow_client/localtools/editing.py` | rewrite | client wrapper over kit |
| `client/tests/test_kit_*.py` | create | kit unit tests and import boundary |
| `client/tests/test_local_tools.py` | modify | edit tests for the new behavior |
| `client/tests/test_packaging_audit.py` | modify | writer allowlist: `fsio.py` in, `editing.py` out |
| `tests/gateway/test_edit_parity.py` | create | client executor vs main handler |
| `tests/gateway/test_client_kit_adoption.py` | create | moved code is gone from the main package |
| `client/README.md`, `CHANGELOG.md` | modify | standing rule, behavior change |

---

### Task 1: kit package and `fsio`

**Files:** create `kit/__init__.py`, `kit/fsio.py`, `client/tests/test_kit_boundary.py`,
`client/tests/test_kit_fsio.py`; modify `client/tests/test_packaging_audit.py`.

**Produces:** `FileSnapshot(path, existed, content)` (a `NamedTuple`),
`read_text(path, *, max_bytes=MAX_TEXT_BYTES) -> str`, `atomic_write(path, text)`,
`restore_bytes(path, payload: bytes | None)`, `snapshot(paths) -> dict[str, FileSnapshot]`,
`restore(snapshots, applied_content=None) -> list[str]`.

- [ ] Write `client/tests/test_kit_boundary.py`: parse every `kit/*.py` with `ast`
  and assert each import is standard library, `lemoncrow_client.errors`,
  `lemoncrow_client.kit` or `lemoncrow_client.kit.*` (relative imports resolve
  level 1 to `lemoncrow_client.kit`, level 2 to `lemoncrow_client`).
- [ ] Write `client/tests/test_kit_fsio.py`: mode kept and no litter after
  `atomic_write` on a 0755 script; `read_text` refuses `max_bytes` overflow and
  non-UTF-8 bytes; `snapshot`/`restore` round trip restores an edited file and
  deletes a created one; `restore` with `applied_content` skips a file changed by
  someone else and returns its display path; `restore_bytes(path, None)` deletes.
- [ ] Run `cd client && uv run pytest tests/test_kit_boundary.py tests/test_kit_fsio.py -q`: FAIL (no kit).
- [ ] Create `kit/__init__.py` (docstring stating the four rules, no imports) and
  `kit/fsio.py`. Writes go through `tempfile.mkstemp(dir=path.parent,
  prefix=".lemoncrow-edit-")`, then `os.chmod(tmp, old_mode & 0o7777)` when the
  target exists, then `os.replace`; the temporary file is unlinked on any
  exception. `snapshot` and `restore` keep the exact semantics of
  `mcp_server._snapshot_paths` / `_restore_snapshots` (unreadable file:
  `existed=True, content=None`, never deleted or truncated on restore).
- [ ] Add `"fsio.py"` to the sorted writer list in
  `test_persistent_state_writers_are_confined_to_the_worktree_or_state_directory`.
- [ ] Run the two new test files plus `tests/test_packaging_audit.py`: PASS.
- [ ] Commit `feat(client): add shared kit with fsio`.

### Task 2: test-contract guard in kit

**Files:** create `kit/contract_guard.py`, `client/tests/test_kit_contract_guard.py`.

**Consumes:** `FileSnapshot`. **Produces:** `guard_enabled(environ=None) -> bool`,
`looks_like_test_path(path) -> bool`, `classify_weakening(old, new) -> str | None`,
`detect_weakening(snapshots, *, session_created) -> list[dict[str, str]]`,
`weakening_message(findings) -> str`.

- [ ] Write tests: net assertion removal and added `@pytest.mark.skip` are
  classified, an in-place assertion change is not; `tests/test_x.py` and
  `src/a.spec.ts` look like tests, `src/app.py` does not; a test-only weakening
  is reported, the same weakening next to a production-file change is not, a
  session-created test is not; `LEMONCROW_TEST_CONTRACT_GUARD=0` disables;
  the message names `path: reason`.
- [ ] Run: FAIL (module missing).
- [ ] Move `_ASSERTION_RE`, `_SKIP_XFAIL_RE`, `_looks_like_test_path`,
  `_classify_test_weakening`, `_detect_test_weakening`,
  `_test_contract_guard_enabled` from `mcp_server.py` into
  `kit/contract_guard.py` under the public names above. `detect_weakening` takes
  the session-created set as a parameter instead of reading a global.
  `weakening_message` returns the exact text `tools_edit.py` builds today.
- [ ] Run: PASS. Commit `feat(client): add test-contract guard to kit`.

### Task 3: edit engine in kit

**Files:** create `kit/edit.py`, `client/tests/test_kit_edit.py`.

**Produces:** `PROTECTED_PARTS`, `OLD_ALIASES`, `NEW_ALIASES`,
`normalize_edit_aliases(edit) -> dict[str, Any]`,
`require_content(edits, *, sources="new") -> None`, `TargetSpec`,
`parse_target(raw) -> TargetSpec`, `SymbolTarget`, `ProjectionResult`,
`EditExtensions`, `apply_edits(edits, *, root, allowed_roots=(), atomic=True,
extensions=None) -> dict[str, Any]`.

`EditExtensions` fields, all optional:

```python
resolve_symbol: Callable[[dict[str, Any], Path], SymbolTarget] | None
after_symbol_edits: Callable[[list[SymbolTarget]], None] | None
apply_projection: Callable[[dict[str, Any], str, Path], ProjectionResult] | None
minified_replace: Callable[[str, str, str, str], tuple[str, int, int] | None] | None
fuzzy_replace: Callable[[str, str, str], tuple[str, int, int] | None] | None
after_write: Callable[[Path, list[Path]], None] | None
preserve_payload: Callable[[str], str | None] | None
structured_errors: tuple[type[Exception], ...] = ()
```

- [ ] Write `client/tests/test_kit_edit.py`: the batch-range regression
  (`L1` to three lines, then `L4` gives `A1 A2 A3 b c D e`); whole-batch
  rollback across two files; `retry_with` hint on a miss (`m.py:L1-L2` with the
  disk text); the parse gate refuses `x = (`; `.git/config` refused as
  protected; an absolute path outside the root refused; symbol and projection
  edits refused without hooks; a whitespace-divergent anchor misses without the
  fuzzy hook; a fake fuzzy hook and `after_write` hook are called; a fake symbol
  hook tags the entry and `after_symbol_edits` runs; alias normalization and
  `require_content`; `parse_target` suffixes.
- [ ] Run: FAIL (module missing).
- [ ] Create `kit/edit.py` from `rich_edit.py` lines 1-957 with exactly these
  changes:
  1. Imports: standard library only, plus `from .fsio import atomic_write,
     read_text, restore_bytes`. Drop `source_projection`, `fuzzy_match`,
     `path_safety`, `symbol_edit`, `CodeContextEngine`, `tool_output_spill`.
  2. `PROTECTED_PARTS = frozenset({".git", ".lemoncrow", "node_modules", ".venv"})`.
  3. Add `OLD_ALIASES`/`NEW_ALIASES` and the alias half of
     `mcp_server._normalize_edit_aliases` (no `new_file`) as
     `normalize_edit_aliases`; add `require_content` from `_require_edits` with
     the message `edits[{i}] has no replacement content: provide {sources}`.
  4. Rename `_parse_target` to `parse_target`; keep `TargetSpec`.
  5. `_replace_in_scope(..., extensions)`: the minified rung calls
     `extensions.minified_replace(content, spec.path, old, new)` and the fuzzy
     rung calls `extensions.fuzzy_replace(scoped, old, new)`; each rung is skipped
     when its hook is `None` or returns `None`.
  6. `kind: symbol` calls `extensions.resolve_symbol(edit, root)`; without the
     hook raise `ValueError("symbol edits need the LemonCrow code index; use
     old/new or a line range")`. `kind: projection` calls
     `extensions.apply_projection(edit, content, path)`; without it raise
     `ValueError("projection edits need the LemonCrow server's projection; use
     old/new or a line range")`.
  7. Reads use `fsio.read_text`; writes use `fsio.atomic_write`; rollback uses
     `fsio.restore_bytes`. The old `_atomic_write` (which also copied the old
     mtime) is deleted.
  8. After writes: `extensions.after_write(root, paths)` inside
     `contextlib.suppress(Exception)`; then `after_symbol_edits`.
  9. The large-payload hint calls `extensions.preserve_payload(text)`.
  10. Structured failures: `isinstance(exc, extensions.structured_errors)` then
      `failed.append(exc.to_dict())`.
  11. Recovered failures log at `debug`, not `exception`.
- [ ] Run kit tests, `uv run mypy --strict client/src`: PASS.
- [ ] Commit `feat(client): move the edit engine into kit`.

### Task 4: main package uses kit

**Files:** rewrite `rich_edit.py`; delete `path_safety.py`; modify
`mcp_server.py`, `tools_edit.py`, `native_search.py` (comment),
`tests/core/test_rich_edit.py`, `tests/gateway/test_mcp_tool_handlers.py`;
create `tests/gateway/test_client_kit_adoption.py`.

- [ ] Before touching `rich_edit.py`, copy it to the scratchpad as
  `rich_edit_old.py` for the differential run.
- [ ] Write `tests/gateway/test_client_kit_adoption.py`: `path_safety.py` is gone;
  `rich_edit.py` imports `lemoncrow_client.kit.edit` and defines neither
  `_replace_in_scope` nor `_parse_target`; `mcp_server.py` no longer defines
  `_classify_test_weakening`, `_looks_like_test_path`, `_snapshot_paths`,
  `_restore_snapshots` or `_OLD_ALIASES`. Run: FAIL.
- [ ] Rewrite `rich_edit.py`: hook adapters for symbol (wrapping
  `ResolvedSymbolEdit` as `SymbolTarget.handle`), projection (the moved
  projection branch), minified (`language_for_minify` + `apply_minified_edit`,
  `None` on `MinifiedEditError`), fuzzy (`None` when
  `normalize_for_fuzzy(old)` is empty), reindex (`CodeContextEngine`), payload
  preservation (`tool_output_spill.spill`); one `EditExtensions` built lazily;
  `apply_rich_edits(edits, *, repo_root=None, atomic=True, allowed_roots=None)`
  keeps its signature and calls `kit.edit.apply_edits`.
- [ ] `mcp_server.py`: delete the moved guard, snapshot, restore and alias code;
  the edit hook factory passes `kit.fsio.snapshot`, `kit.fsio.restore`,
  `kit.contract_guard.guard_enabled` and a lambda binding
  `detect_weakening(..., session_created=_SESSION_CREATED_FILES)`;
  `_normalize_edit_aliases` calls `kit.edit.normalize_edit_aliases` and keeps
  only its `new_file` handling; `_require_edits` wraps `require_content(edits,
  sources="new (or new_file)")` in `_ToolArgumentError`.
- [ ] `tools_edit.py`: import `NEW_ALIASES`, `parse_target` and
  `weakening_message` from kit and delete the local copies.
- [ ] Update `tests/core/test_rich_edit.py` to import `parse_target` from kit and
  the path-safety test in `test_mcp_tool_handlers.py` to read
  `kit.edit.PROTECTED_PARTS`.
- [ ] Differential: a scratchpad script generates 2,000 random batches (range
  edits with growth and shrink, exact/normalized/placeholder anchors, misses,
  replaces, creations, Python files) and runs each through
  `rich_edit_old.apply_rich_edits` and the new wrapper on identical copies.
  File bytes and result dicts must match. Record every divergence.
- [ ] Run the edit-related main tests (listed in the execution log) and
  `uv run mypy --explicit-package-bases` on touched modules: PASS.
- [ ] Commit `refactor(edit): main package uses the kit edit engine`.

### Task 5: client executor uses kit

**Files:** rewrite `localtools/editing.py`; modify `client/tests/test_local_tools.py`,
`client/tests/test_packaging_audit.py`.

- [ ] Copy the old `editing.py` to the scratchpad as `editing_old.py`.
- [ ] Tests first: the batch-range regression through the executor; a failing
  second edit leaves the first file untouched; a miss returns `retry_with` in the
  error details; a test-only assertion removal is rolled back; `:minified` and
  `kind` edits are refused; the ambiguous-anchor test asserts the message
  instead of `occurrences`. Run: FAIL.
- [ ] Rewrite `editing.py`: validate entries, refuse `kind` and `:minified`,
  confine every path with `LocalContext.resolve`, normalize aliases,
  `require_content`, snapshot targets, call `apply_edits(edits,
  root=context.repo_root)`, raise `ClientError(PAYLOAD_INVALID, error,
  details=<failure fields>, action=FIX_REQUEST)` on failure, run the
  contract guard (restore and refuse on findings), record created files in a
  process-local set, report changed paths by comparing snapshots, and render the
  existing one-line summaries (plus `, <mode> match` for non-exact matches).
- [ ] Writer allowlist: remove `"editing.py"`.
- [ ] Differential: 2,000 random batches through `editing_old.run_edit` and the
  new executor; classify each divergence as fix or regression in the execution
  log. Regressions block the task.
- [ ] Run `cd client && uv run pytest -q` and `uv run mypy --strict client/src`: PASS
  apart from the three failures already present on `main`.
- [ ] Commit `feat(client): edit runs on the shared kit engine`.

### Task 6: parity, docs, full verification

**Files:** create `tests/gateway/test_edit_parity.py`; modify `client/README.md`,
`CHANGELOG.md`.

- [ ] Parity test: for each case (exact edit, batched ranges with growth, range
  plus anchor, create with replace, ambiguous anchor, miss, Python parse error,
  test-only weakening) seed identical trees, run the client executor and the
  main handler (`post_edit_hooks=False`, `LEMONCROW_RANGE_EDIT_GUARD=0` because
  the served-range freshness guard is main-only), and assert the same
  success/failure and identical file bytes.
- [ ] `client/README.md`: one paragraph stating the standing rule.
- [ ] `CHANGELOG.md` under Unreleased/Changed: the client edit behavior change.
- [ ] Run client suite, the edit-related main suites, ruff and mypy.
- [ ] Commit `test(edit): client and main handler parity`.

## Execution log (2026-09-22)

All six tasks are done on `feat/shared-client-kit`.

**Baseline failures on `main`, unrelated and unchanged:**
`tests/gateway/test_mcp_tool_handlers.py::test_cli_tools_list_hides_internal_tools_even_with_legacy_flag`,
`client/tests/test_contract_conformance.py::test_the_bundled_tool_surface_is_the_public_registrys`
and `client/tests/test_local_tools.py::test_no_module_in_the_package_can_download_or_self_update`
(`review.py` imports `tarfile`). `mypy --strict client/src` reports the same 8
errors in `credentials.py` and `search_markdown.py` with and without this work.

**Differential, old main engine vs kit-backed `apply_rich_edits`** (2,000 random
batches: ranges with growth and shrink, exact, typography, placeholder and
fuzzy anchors, misses, replaces, creations, Python parse errors, atomic and
non-atomic): 0 divergences for the move itself. After the two kit fixes below, 3
divergences, all from the stricter "already applied" rule and all fail-safe (a
batch that reported false success is now refused).

**Differential, old client executor vs kit-backed executor** (2,000 batches):
943 identical. Divergences, classified:

| Count | Divergence | Class |
| ---: | --- | --- |
| 465 | `{path, new}` on a missing file creates it | intended (main behavior) |
| 400 | a failing batch no longer leaves earlier hunks written | fix |
| 69 | Python that no longer parses is refused | fix |
| 42 | `{path, new}` on an existing file rewrites it when the body is a plausible whole file | intended (main behavior) |
| 39 | same outcome, different bytes: `new: ""` on a range deletes the lines; ranges translate to pre-batch numbers | intended / fix |
| 27 | anchor found through typography normalization or pre-batch translation | intended / fix |
| 19 | ambiguous range edits after an earlier hunk are refused instead of hitting shifted lines | fix |
| 1 | a valid `L9` edit the old client refused after miscounting lines | fix |

Two regressions surfaced and were fixed in kit, for both surfaces: a blank or
repeated `new` no longer counts as proof that an edit was already applied, and a
range that starts past the end of the file is refused.
