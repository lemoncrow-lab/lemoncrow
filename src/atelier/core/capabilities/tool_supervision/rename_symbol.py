"""Scope-correct symbol rename — builds a batch of rich-edit descriptors.

Backends (by language, in fallback order):
  python    -> rope (pip install atelier[rename]) -> ast-grep -> naive
  typescript/javascript -> ts-morph (node subprocess) -> ast-grep -> naive
  rust      -> ast-grep -> naive
  unknown   -> naive

The returned list of dicts is passed directly to apply_rich_edits, which handles
atomic writes, rollback, and diff recording.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any


# -- backend detection --------------------------------------------------------

_LANGUAGE_BACKENDS: dict[str, list[str]] = {
    "python":     ["rope", "ast-grep", "naive"],
    "typescript": ["ts-morph", "ast-grep", "naive"],
    "javascript": ["ts-morph", "ast-grep", "naive"],
    "rust":       ["ast-grep", "naive"],
}


def _best_backend(language: str) -> str:
    for backend in _LANGUAGE_BACKENDS.get(language, ["naive"]):
        if backend == "rope":
            try:
                import rope  # type: ignore[import-untyped]  # noqa: F401,PLC0415
                return "rope"
            except ImportError:
                continue
        elif backend == "ts-morph":
            if shutil.which("node"):
                return "ts-morph"
        elif backend == "ast-grep":
            if shutil.which("ast-grep") or shutil.which("sg"):
                return "ast-grep"
        elif backend == "naive":
            return "naive"
    return "naive"


# -- naive rename -------------------------------------------------------------

def _naive_rename(
    symbol: dict[str, Any],
    usages: list[dict[str, Any]],
    new_name: str,
    old_name: str,
) -> list[dict[str, Any]]:
    """Build rich-edit descriptors using old_string/new_string pairs.

    Searches for the exact old_name within the snippet of each usage.
    Not scope-correct for shadowed locals, but safe for public API renames.
    """
    edits: list[dict[str, Any]] = []
    # Definition first
    edits.append({
        "file_path": symbol["file_path"],
        "old_string": old_name,
        "new_string": new_name,
    })
    seen: set[tuple[str, int]] = set()
    for usage in usages:
        fp = str(usage.get("file_path") or "")
        line = int(usage.get("line") or 0)
        snippet = str(usage.get("snippet") or "")
        if not fp or not snippet or old_name not in snippet:
            continue
        key = (fp, line)
        if key in seen:
            continue
        seen.add(key)
        edits.append({
            "file_path": fp,
            "old_string": old_name,
            "new_string": new_name,
        })
    return edits


# -- rope backend (Python only) -----------------------------------------------

def _rope_rename(
    symbol: dict[str, Any],
    new_name: str,
    repo_root: Path,
) -> list[dict[str, Any]]:
    """Scope-correct rename using rope. Returns overwrite-style edits."""
    from rope.base.project import Project  # type: ignore[import-untyped]
    from rope.base import libutils  # type: ignore[import-untyped]
    from rope.refactor.rename import Rename  # type: ignore[import-untyped]

    project = Project(str(repo_root))
    try:
        abs_path = repo_root / symbol["file_path"]
        resource = libutils.path_to_resource(project, str(abs_path))
        # Convert byte offset -> character offset for files with multi-byte chars
        raw_bytes = abs_path.read_bytes()
        char_offset = len(raw_bytes[:symbol["start_byte"]].decode("utf-8", errors="replace"))
        renamer = Rename(project, resource, char_offset)
        changes = renamer.get_changes(new_name)
        edits: list[dict[str, Any]] = []
        for change in changes.changes:
            # rope ChangeContents has .resource.path and .new_contents
            if hasattr(change, "new_contents"):
                rel_path = str(Path(change.resource.path).relative_to(repo_root))
                edits.append({
                    "file_path": rel_path,
                    "overwrite": True,
                    "new_string": change.new_contents,
                })
        return edits
    finally:
        project.close()


# -- ts-morph backend (TypeScript/JavaScript) ---------------------------------

_TS_MORPH_SCRIPT = textwrap.dedent("""\
    const { Project } = require('ts-morph');
    const path = require('path');
    const fs = require('fs');

    const repoRoot = process.argv[2];
    const filePath = process.argv[3];
    const byteOffset = parseInt(process.argv[4], 10);
    const newName = process.argv[5];

    const project = new Project({ tsConfigFilePath: path.join(repoRoot, 'tsconfig.json'), skipLoadingLibFiles: false });
    const sourceFile = project.getSourceFileOrThrow(filePath);
    const absSource = fs.readFileSync(filePath, 'utf8');

    // Convert byte offset to character position
    const charOffset = Buffer.from(absSource).slice(0, byteOffset).toString('utf8').length;
    const node = sourceFile.getDescendantAtPos(charOffset);
    if (!node) { console.error('no node at offset'); process.exit(1); }

    node.rename(newName);

    const result = {};
    for (const sf of project.getSourceFiles()) {
        if (sf.wasForgotten()) continue;
        result[sf.getFilePath()] = sf.getFullText();
    }
    console.log(JSON.stringify(result));
""")


def _tsmorph_rename(
    symbol: dict[str, Any],
    new_name: str,
    repo_root: Path,
) -> list[dict[str, Any]]:
    with tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode="w") as f:
        f.write(_TS_MORPH_SCRIPT)
        script_path = f.name
    abs_file = str(repo_root / symbol["file_path"])
    try:
        proc = subprocess.run(
            ["node", script_path, str(repo_root), abs_file, str(symbol["start_byte"]), new_name],
            capture_output=True, text=True, timeout=30, cwd=str(repo_root),
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ts-morph rename failed: {proc.stderr[:500]}")
    import json
    changed: dict[str, str] = json.loads(proc.stdout)
    edits: list[dict[str, Any]] = []
    for abs_path, content in changed.items():
        try:
            rel = str(Path(abs_path).relative_to(repo_root))
        except ValueError:
            continue
        edits.append({"file_path": rel, "overwrite": True, "new_string": content})
    return edits


# -- ast-grep backend ---------------------------------------------------------

def _astgrep_rename(
    symbol: dict[str, Any],
    usages: list[dict[str, Any]],
    new_name: str,
    old_name: str,
    language: str,
    repo_root: Path,
) -> list[dict[str, Any]]:
    """ast-grep structural rewrite scoped to files that contain usages."""
    from atelier.infra.code_intel.astgrep.adapter import AstGrepAdapter  # type: ignore[import-untyped]

    # Build set of files with usages (including definition file)
    usage_files = {symbol["file_path"]} | {
        str(u.get("file_path") or "") for u in usages if u.get("file_path")
    }

    adapter = AstGrepAdapter(repo_root)
    edits: list[dict[str, Any]] = []

    for fp in usage_files:
        if not fp:
            continue
        result = adapter.rewrite(
            pattern=old_name,
            rewrite=new_name,
            language=language,
            file_glob=fp,
            dry_run=False,
        )
        if result.files_changed:
            # rewrite already wrote the file; record as applied (no edit needed)
            # Return empty so caller knows files were changed
            edits.append({"file_path": fp, "_astgrep_applied": True})

    return edits


# -- public API ---------------------------------------------------------------

def build_rename_edits(
    engine: Any,  # CodeContextEngine -- avoid circular import
    *,
    symbol_id: str | None = None,
    qualified_name: str | None = None,
    symbol_name: str | None = None,
    file_path: str | None = None,
    new_name: str,
    repo_root: Path,
    backend: str = "auto",
) -> list[dict[str, Any]]:
    """Resolve symbol, collect usages, return rich-edit descriptors for atomic rename.

    Raises:
        ValueError: if new_name is empty or symbol cannot be resolved.
        LookupError: if symbol not found.
    """
    if not new_name or not new_name.isidentifier():
        raise ValueError(f"new_name must be a valid identifier, got: {new_name!r}")

    symbol = engine.get_symbol(
        symbol_id=symbol_id,
        qualified_name=qualified_name,
        symbol_name=symbol_name,
        file_path=file_path,
    )
    old_name = str(symbol["symbol_name"])
    language = str(symbol.get("language") or "unknown").lower()

    # Collect all usages
    usages_payload = engine.find_references(
        symbol_id=str(symbol["symbol_id"]),
        qualified_name=str(symbol["qualified_name"]),
        symbol_name=old_name,
        file_path=str(symbol["file_path"]),
        snippet_lines=1,
        limit=500,
        budget_tokens=8000,
    )
    # Flatten usages from grouped or flat payload
    usages: list[dict[str, Any]] = []
    raw_refs = usages_payload.get("references") or usages_payload.get("usages") or []
    if isinstance(raw_refs, list):
        usages = [r for r in raw_refs if isinstance(r, dict)]
    elif isinstance(raw_refs, dict):
        for items in raw_refs.values():
            if isinstance(items, list):
                usages.extend(r for r in items if isinstance(r, dict))

    chosen = backend if backend != "auto" else _best_backend(language)

    if chosen == "rope" and language == "python":
        return _rope_rename(symbol, new_name, repo_root)

    if chosen == "ts-morph" and language in ("typescript", "javascript"):
        return _tsmorph_rename(symbol, new_name, repo_root)

    if chosen == "ast-grep":
        return _astgrep_rename(symbol, usages, new_name, old_name, language, repo_root)

    return _naive_rename(symbol, usages, new_name, old_name)


__all__ = ["build_rename_edits"]
