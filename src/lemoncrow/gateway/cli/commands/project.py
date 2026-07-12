"""``lc project`` — project intelligence snapshot.

Scans the current working directory and renders a rich breakdown:
  - language distribution (files + LOC)
  - top files by size / LOC
  - top directories by LOC
  - code-to-test ratio, doc coverage
  - LemonCrow projection savings (runs the real semantic_file_memory
    projection pipeline — AST / tree-sitter / generic outline — per file)

Run:
    lc project [PATH]
    lc project --json
    lc project --top 10
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------


@dataclass
class LangDef:
    name: str
    exts: tuple[str, ...]
    comment: str = "#"
    color: str = "cyan"


_LANGS: list[LangDef] = [
    LangDef("Python", (".py",), "#", "bright_yellow"),
    LangDef("TypeScript", (".ts", ".tsx"), "//", "bright_cyan"),
    LangDef("JavaScript", (".js", ".jsx", ".mjs"), "//", "yellow"),
    LangDef("Rust", (".rs",), "//", "red"),
    LangDef("Go", (".go",), "//", "cyan"),
    LangDef("Swift", (".swift",), "//", "orange3"),
    LangDef("Kotlin", (".kt", ".kts"), "//", "bright_magenta"),
    LangDef("Java", (".java",), "//", "bright_blue"),
    LangDef("C/C++", (".c", ".cpp", ".cc", ".h", ".hpp"), "//", "blue"),
    LangDef("C#", (".cs",), "//", "bright_green"),
    LangDef("Ruby", (".rb",), "#", "red"),
    LangDef("PHP", (".php",), "//", "magenta"),
    LangDef("Scala", (".scala",), "//", "bright_red"),
    LangDef("Shell", (".sh", ".bash", ".zsh"), "#", "green"),
    LangDef("HTML", (".html", ".htm"), "<!--", "orange1"),
    LangDef("CSS", (".css", ".scss", ".sass"), "/*", "bright_blue"),
    LangDef("TOML", (".toml",), "#", "dim white"),
    LangDef("YAML", (".yaml", ".yml"), "#", "dim white"),
    LangDef("JSON", (".json",), "", "dim white"),
    LangDef("Markdown", (".md", ".mdx"), "", "dim white"),
    LangDef("Astro", (".astro",), "//", "bright_cyan"),
]

_EXT_TO_LANG: dict[str, LangDef] = {}
for _ld in _LANGS:
    for _ext in _ld.exts:
        _EXT_TO_LANG[_ext] = _ld

# Files/dirs to always skip
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        "vendor",
        ".cargo",
        "coverage",
        ".coverage",
        "htmlcov",
        ".tox",
        "eggs",
        ".eggs",
        "buck-out",
        "_build",
    }
)


# ---------------------------------------------------------------------------
# File stats
# ---------------------------------------------------------------------------


@dataclass
class FileStats:
    path: Path
    rel: str
    lang: LangDef | None
    size_bytes: int
    lines: int
    code_lines: int  # non-blank, non-comment
    comment_lines: int
    blank_lines: int
    is_test: bool
    is_doc: bool
    tokens: int  # raw tokens (tiktoken, Claude-read baseline)
    proj_tokens: int  # tokens after real projection; == tokens if not projected
    proj_mode: str  # outline / minified / compact / full from the projection pipeline
    proj_reason: str | None  # None when projected; why not otherwise

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()


def _count_lines(text: str, comment_prefix: str) -> tuple[int, int, int]:
    """Returns (code, comment, blank).

    Handles block comments (``/* ... */`` for C-family/CSS, ``<!-- ... -->`` for
    HTML) so interior and closing lines aren't miscounted as code. Block
    delimiters are inferred from the line-comment prefix the language defines.
    """
    code = comment = blank = 0
    cp = comment_prefix.strip()
    if cp == "<!--":
        block_open, block_close = "<!--", "-->"
    elif cp in ("//", "/*"):
        block_open, block_close = "/*", "*/"
    else:
        block_open = block_close = ""
    in_block = False
    for line in text.splitlines():
        s = line.strip()
        if in_block:
            comment += 1 if s else 0
            blank += 0 if s else 1
            if block_close in s:
                in_block = False
            continue
        if not s:
            blank += 1
        elif block_open and s.startswith(block_open):
            comment += 1
            # Block stays open only if it isn't closed on the same line.
            if block_close not in s[len(block_open) :]:
                in_block = True
        elif cp and s.startswith(cp):
            comment += 1
        else:
            code += 1
    return code, comment, blank


def _scan_file(path: Path, root: Path, threshold: int) -> FileStats | None:
    ext = path.suffix.lower()
    lang = _EXT_TO_LANG.get(ext)
    if lang is None:
        return None  # skip files with unrecognized extensions
    try:
        size = path.stat().st_size
        if size > 2 * 1024 * 1024:  # skip files > 2MB
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return None

    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    cp = lang.comment if lang else "#"
    code, comment, blank = _count_lines(text, cp)

    name = path.name.lower()
    rel = str(path.relative_to(root))
    is_test = bool(
        re.search(r"(^|/)tests?/", rel)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.tsx")
    )
    is_doc = ext in (".md", ".mdx", ".rst", ".txt") or "docs/" in rel or "doc/" in rel

    preview = _project_file(path, text, threshold)
    raw_tokens = preview["raw_tokens"]
    # Clamp like smart_read's max(0, savings): never report negative savings.
    proj_tokens = min(preview["tokens"], raw_tokens)

    return FileStats(
        path=path,
        rel=rel,
        lang=lang,
        size_bytes=size,
        lines=lines,
        code_lines=code,
        comment_lines=comment,
        blank_lines=blank,
        is_test=is_test,
        is_doc=is_doc,
        tokens=raw_tokens,
        proj_tokens=proj_tokens,
        proj_mode=preview["mode"],
        proj_reason=_full_mode_reason(preview),
    )


# ---------------------------------------------------------------------------
# Projection — runs the real semantic_file_memory pipeline (cache-free) so the
# numbers here match exactly what the `read` tool ships to agents.
# ---------------------------------------------------------------------------


def _resolve_threshold(threshold: int | None) -> int:
    """CLI override, else the pipeline default (LEMONCROW_OUTLINE_THRESHOLD / 200)."""
    if threshold is not None:
        return max(0, threshold)
    from lemoncrow.core.capabilities.semantic_file_memory.capability import (
        default_outline_threshold,
    )

    return default_outline_threshold()


def _project_file(path: Path, source: str, threshold: int) -> dict[str, Any]:
    """Real projection preview: {"mode", "language", "loc", "text", "raw_tokens", "tokens"}."""
    from lemoncrow.core.capabilities.semantic_file_memory.capability import (
        SemanticFileMemoryCapability,
    )

    return SemanticFileMemoryCapability.project_preview(path, source, outline_threshold=threshold)


def _full_mode_reason(preview: dict[str, Any]) -> str | None:
    """None when the pipeline projected the file (outline / minified / compact);
    otherwise why it shipped in full at raw token cost."""
    if preview["mode"] != "full":
        return None
    if preview["language"] == "text":
        return "no structural grammar"
    return "already minimal"


@dataclass
class ProjectSnapshot:
    root: Path
    threshold: int = 0
    files: list[FileStats] = field(default_factory=list)

    # aggregated
    by_lang: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(
            lambda: {"files": 0, "loc": 0, "code": 0, "bytes": 0, "tokens": 0, "proj_tokens": 0}
        )
    )
    by_dir: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"files": 0, "loc": 0}))

    total_files: int = 0
    total_loc: int = 0
    total_code: int = 0
    total_bytes: int = 0
    test_files: int = 0
    doc_files: int = 0
    source_files: int = 0

    large_files: int = 0  # projectable files (known code lang, not data/markup)
    proj_tokens_saved: int = 0  # estimated tokens saved by projection on large files
    proj_tokens_total: int = 0  # total tokens if read raw
    proj_tokens_after: int = 0  # total tokens after projection

    todos: int = 0
    fixmes: int = 0

    def build(self) -> None:
        for f in self.files:
            lang_name = f.lang.name if f.lang else "Other"
            self.by_lang[lang_name]["files"] += 1
            self.by_lang[lang_name]["loc"] += f.lines
            self.by_lang[lang_name]["code"] += f.code_lines
            self.by_lang[lang_name]["bytes"] += f.size_bytes
            self.by_lang[lang_name]["tokens"] += f.tokens
            self.by_lang[lang_name]["proj_tokens"] += f.proj_tokens

            top_dir = f.rel.split("/")[0] if "/" in f.rel else "."
            self.by_dir[top_dir]["files"] += 1
            self.by_dir[top_dir]["loc"] += f.lines

            self.total_files += 1
            self.total_loc += f.lines
            self.total_code += f.code_lines
            self.total_bytes += f.size_bytes
            if f.is_test:
                self.test_files += 1
            elif f.is_doc:
                self.doc_files += 1
            else:
                self.source_files += 1

            raw = f.tokens
            self.proj_tokens_total += raw
            self.proj_tokens_after += f.proj_tokens
            self.proj_tokens_saved += raw - f.proj_tokens
            if f.proj_reason is None:
                self.large_files += 1

    def to_json(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "total_files": self.total_files,
            "total_loc": self.total_loc,
            "total_code_lines": self.total_code,
            "total_bytes": self.total_bytes,
            "test_files": self.test_files,
            "doc_files": self.doc_files,
            "source_files": self.source_files,
            "large_files_above_projection_threshold": self.large_files,
            "projection_tokens_saved_estimate": self.proj_tokens_saved,
            "projection_tokens_total_estimate": self.proj_tokens_total,
            "projection_tokens_after_estimate": self.proj_tokens_after,
            "by_lang": dict(self.by_lang),
            "by_dir": dict(self.by_dir),
            "top_files_by_loc": [
                {"path": f.rel, "loc": f.lines, "code": f.code_lines, "lang": f.lang.name if f.lang else "?"}
                for f in sorted(self.files, key=lambda x: x.lines, reverse=True)[:10]
            ],
        }


def _load_gitignore_patterns(root: Path) -> list[tuple[Path, str]]:
    """Walk the tree and collect (gitignore_dir, pattern) pairs from every .gitignore found."""
    import fnmatch as _fnmatch  # noqa: F401 — used in _is_gitignored

    pairs: list[tuple[Path, str]] = []
    for gi in root.rglob(".gitignore"):
        gi_dir = gi.parent
        try:
            for raw in gi.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue  # skip blanks, comments, negations (rarely used, keep safe)
                pairs.append((gi_dir, line))
        except OSError:
            pass
    return pairs


def _is_gitignored(path: Path, root: Path, patterns: list[tuple[Path, str]]) -> bool:
    """Return True if *path* matches any collected gitignore pattern."""
    import fnmatch

    rel = path.relative_to(root)
    rel_str = rel.as_posix()
    parts = rel.parts

    for gi_dir, pat in patterns:
        # Compute path relative to the gitignore's directory
        try:
            local_rel = path.relative_to(gi_dir).as_posix()
        except ValueError:
            continue  # file not under this gitignore's directory

        p = pat.rstrip("/")

        # Anchored pattern (contains "/" not at end): match from gitignore dir
        if "/" in p:
            if fnmatch.fnmatch(local_rel, p) or fnmatch.fnmatch(local_rel, p.lstrip("/")):
                return True
            # ** anywhere-depth shorthand
            if p.startswith("**/"):
                tail = p[3:]
                if any(fnmatch.fnmatch("/".join(parts[i:]), tail) for i in range(len(parts))):
                    return True
        else:
            # Unanchored: match against any individual path component (dir or filename)
            for part in parts:
                if fnmatch.fnmatch(part, p):
                    return True
            # Also match against the full relative string for patterns like *.lock
            if fnmatch.fnmatch(rel_str, p):
                return True

    return False


def _get_files(root: Path) -> list[Path]:
    """Return file list: git ls-files if in a git repo, else rglob respecting .gitignore."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            paths = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                p = root / line
                if p.is_file():
                    paths.append(p)
            return paths
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: rglob + manual _SKIP_DIRS + .gitignore parsing
    gitignore_patterns = _load_gitignore_patterns(root)

    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & _SKIP_DIRS:
            continue
        if path.name.startswith("."):
            continue
        if gitignore_patterns and _is_gitignored(path, root, gitignore_patterns):
            continue
        files.append(path)
    return files


def _scan(root: Path, threshold: int, respect_gitignore: bool = True) -> ProjectSnapshot:
    snap = ProjectSnapshot(root=root, threshold=threshold)
    for path in _get_files(root):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & _SKIP_DIRS:
            continue
        if path.name.startswith("."):
            continue
        fs = _scan_file(path, root, threshold)
        if fs is not None:
            snap.files.append(fs)
    snap.build()
    return snap


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n //= 1024
    return f"{n:.0f} TB"


def _fmt_num(n: int) -> str:
    return f"{n:,}"


def _bar(fraction: float, width: int = 20) -> str:
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _render(snap: ProjectSnapshot, top_n: int) -> None:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    proj_name = snap.root.name
    save_pct = int(snap.proj_tokens_saved / max(1, snap.proj_tokens_total) * 100)
    code_ratio = int(snap.source_files / max(1, snap.total_files) * 100)
    test_ratio = (
        int(snap.test_files / max(1, snap.source_files + snap.test_files) * 100)
        if (snap.source_files + snap.test_files)
        else 0
    )

    # ── Header ──
    console.print()
    console.rule(f"[bold bright_white]> {proj_name}[/]  [dim]{snap.root}[/]")
    console.print()

    # ── Hero metrics ──
    hero = Table.grid(expand=True)
    for _ in range(7):
        hero.add_column(justify="center")

    def _chip(label: str, value: str, color: str) -> Panel:
        return Panel(
            f"[bold {color}]{value}[/]\n[dim]{label}[/]",
            border_style="dim",
            padding=(0, 2),
        )

    total_k = snap.proj_tokens_total // 1000
    after_k = snap.proj_tokens_after // 1000
    hero.add_row(
        _chip("Files", _fmt_num(snap.total_files), "bright_white"),
        _chip("Lines of Code", _fmt_num(snap.total_loc), "bright_cyan"),
        _chip("Code Lines", _fmt_num(snap.total_code), "bright_yellow"),
        _chip("Size", _fmt_bytes(snap.total_bytes), "white"),
        _chip("Languages", str(len(snap.by_lang)), "bright_magenta"),
        _chip("Raw Tokens", f"{_fmt_num(total_k)}k", "dim white"),
        _chip("Projected", f"{_fmt_num(after_k)}k", "bright_green"),
    )
    console.print(hero)
    console.print()

    # ── Language breakdown ──
    console.print("[bold bright_white]  Languages[/]  [dim]by lines of code[/]")
    console.print()

    lang_table = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
    lang_table.add_column("Language", style="bold", min_width=14)
    lang_table.add_column("Files", justify="right", style="dim white")
    lang_table.add_column("LOC", justify="right")
    lang_table.add_column("Raw tok", justify="right")
    lang_table.add_column("Proj tok", justify="right")
    lang_table.add_column("Share", min_width=20)
    lang_table.add_column("%", justify="right")

    sorted_langs = sorted(snap.by_lang.items(), key=lambda x: x[1]["loc"], reverse=True)
    max_loc = sorted_langs[0][1]["loc"] if sorted_langs else 1

    for lang_name, stats in sorted_langs[:top_n]:
        ld_match = next((ld for ld in _LANGS if ld.name == lang_name), None)
        color = ld_match.color if ld_match else "white"
        frac = stats["loc"] / max_loc
        pct = stats["loc"] / max(1, snap.total_loc) * 100
        raw_k = stats["tokens"] // 1000
        proj_k = stats["proj_tokens"] // 1000
        proj_str = f"[bright_green]{_fmt_num(proj_k)}k[/]" if proj_k < raw_k else f"[dim]{_fmt_num(proj_k)}k[/]"
        lang_table.add_row(
            f"[{color}]{lang_name}[/]",
            _fmt_num(stats["files"]),
            _fmt_num(stats["loc"]),
            f"[dim]{_fmt_num(raw_k)}k[/]",
            proj_str,
            f"[{color}]{_bar(frac, 20)}[/]",
            f"[dim]{pct:.1f}%[/]",
        )

    console.print(lang_table)

    # ── Top files ──
    console.print("[bold bright_white]  Top Files[/]  [dim]by lines of code[/]")
    console.print()

    file_table = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
    file_table.add_column("#", justify="right", style="dim", width=3)
    file_table.add_column("File", no_wrap=False, min_width=38)
    file_table.add_column("Lang", style="dim", width=10)
    file_table.add_column("LOC", justify="right")
    file_table.add_column("Raw tok", justify="right")
    file_table.add_column("Proj tok", justify="right")
    file_table.add_column("Type", width=5, justify="center")

    top_files = sorted(snap.files, key=lambda x: x.lines, reverse=True)[:top_n]
    for i, f in enumerate(top_files, 1):
        lang_color = f.lang.color if f.lang else "white"
        lang_label = f.lang.name if f.lang else "?"
        ftype = "[blue]test[/]" if f.is_test else ("[dim]doc[/]" if f.is_doc else "[dim green]src[/]")
        reason = f.proj_reason
        proj_str = f"[bright_green]{_fmt_num(f.proj_tokens)}[/]" if reason is None else f"[dim]{_fmt_num(f.tokens)}[/]"
        file_table.add_row(
            str(i),
            f"[dim]{f.rel}[/]",
            f"[{lang_color}]{lang_label}[/]",
            f"[bright_white]{_fmt_num(f.lines)}[/]",
            f"[dim]{_fmt_num(f.tokens)}[/]",
            proj_str,
            ftype,
        )

    console.print(file_table)

    # ── Top directories ──
    console.print("[bold bright_white]  Top Directories[/]  [dim]by lines of code[/]")
    console.print()

    dir_table = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
    dir_table.add_column("#", justify="right", style="dim", width=3)
    dir_table.add_column("Directory", min_width=20)
    dir_table.add_column("Files", justify="right", style="dim")
    dir_table.add_column("LOC", justify="right")
    dir_table.add_column("Share", min_width=24)

    sorted_dirs = sorted(snap.by_dir.items(), key=lambda x: x[1]["loc"], reverse=True)[:top_n]
    max_dir_loc = sorted_dirs[0][1]["loc"] if sorted_dirs else 1

    for i, (d, stats) in enumerate(sorted_dirs, 1):
        frac = stats["loc"] / max_dir_loc
        dir_table.add_row(
            str(i),
            f"[bright_white]{d}/[/]",
            _fmt_num(stats["files"]),
            f"[bright_cyan]{_fmt_num(stats['loc'])}[/]",
            f"[cyan]{_bar(frac, 22)}[/]",
        )

    console.print(dir_table)

    # ── Code health + LemonCrow projection + non-projectable ──
    bottom = Table.grid(expand=True, padding=(0, 1))
    bottom.add_column(ratio=1)
    bottom.add_column(ratio=1)
    bottom.add_column(ratio=1)

    # Health panel
    health_lines = [
        f"  [dim]Source files[/]   [bright_white]{_fmt_num(snap.source_files)}[/]  [dim]({code_ratio}% of total)[/]",
        f"  [dim]Test files  [/]   [bright_white]{_fmt_num(snap.test_files)}[/]  [dim]({test_ratio}% test coverage)[/]",
        f"  [dim]Doc files   [/]   [bright_white]{_fmt_num(snap.doc_files)}[/]",
        "",
        f"  [dim]Projectable files [/]  [bright_white]{_fmt_num(snap.large_files)}[/]",
    ]
    health_panel = Panel(
        "\n".join(health_lines),
        title="[bold]Code Profile[/]",
        border_style="dim",
        padding=(1, 2),
    )

    # LemonCrow projection panel
    saved_k = snap.proj_tokens_saved // 1000
    lemoncrow_lines = [
        f"  [dim]Raw tokens      [/]  [white]{_fmt_num(total_k)}k[/]",
        f"  [dim]Projected tokens[/]  [bright_green]{_fmt_num(after_k)}k[/]",
        f"  [dim]Saved           [/]  [bright_green]{_fmt_num(saved_k)}k[/]  [dim]({save_pct}%)[/]",
        "",
        "  [dim]--files N  for per-file breakdown[/]",
    ]
    lemoncrow_panel = Panel(
        "\n".join(lemoncrow_lines),
        title="[bold bright_green]Projection Savings[/]",
        border_style="bright_green dim",
        padding=(1, 2),
    )

    # Non-projectable classification panel
    from collections import Counter

    reason_counts: Counter[str] = Counter()
    for f in snap.files:
        r = f.proj_reason
        if r is not None:
            reason_counts[r] += 1
    nonproj_lines: list[str] = []
    for reason, count in reason_counts.most_common():
        nonproj_lines.append(f"  [yellow]{reason:<22}[/]  [dim]{_fmt_num(count)} files[/]")
    if not nonproj_lines:
        nonproj_lines = ["  [dim]all files are projectable[/]"]
    nonproj_panel = Panel(
        "\n".join(nonproj_lines),
        title="[bold yellow]Non-projectable[/]",
        border_style="yellow dim",
        padding=(1, 2),
    )

    bottom.add_row(health_panel, lemoncrow_panel, nonproj_panel)
    console.print(bottom)
    console.print()
    _render_mode_breakdown(console, snap)


# ---------------------------------------------------------------------------
# --files view
# ---------------------------------------------------------------------------


def _render_mode_breakdown(console: Any, snap: ProjectSnapshot) -> None:
    """Per-mode projection breakdown — outline / minified / compact / full, nothing hidden."""
    from rich import box
    from rich.table import Table

    mode_agg: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "raw": 0, "proj": 0})
    for f in snap.files:
        agg = mode_agg[f.proj_mode]
        agg["files"] += 1
        agg["raw"] += f.tokens
        agg["proj"] += f.proj_tokens
    mode_meta = {
        "outline": ("Outline", "bright_green", "structure only · bodies omitted (LLM fetches on demand)"),
        "minified": ("Minified", "green", "comments · blank lines dropped · full code kept"),
        "compact": ("Compact", "yellow", "trailing whitespace · blank runs collapsed"),
        "full": ("Full", "dim", "raw — nothing worth dropping"),
    }
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
    tbl.add_column("Projection mode")
    tbl.add_column("Files", justify="right")
    tbl.add_column("Raw", justify="right")
    tbl.add_column("Shipped", justify="right")
    tbl.add_column("Saved", justify="right")
    tbl.add_column("What it does", style="dim")
    for name in ("outline", "minified", "compact", "full"):
        agg = mode_agg.get(name) or {}
        if not agg or agg["files"] == 0:
            continue
        label, color, desc = mode_meta[name]
        raw_t, proj_t = agg["raw"], agg["proj"]
        saved = int((raw_t - proj_t) / max(1, raw_t) * 100)
        saved_str = f"[bright_green]-{saved}%[/]" if saved > 0 else "[dim]0%[/]"
        tbl.add_row(f"[{color}]{label}[/]", _fmt_num(agg["files"]), _fmt_num(raw_t), _fmt_num(proj_t), saved_str, desc)
    console.print("[bold bright_white]  Projection by mode[/]  [dim]what each strategy shipped[/]")
    console.print()
    console.print(tbl)
    console.print()


def _render_files(snap: ProjectSnapshot, limit: int) -> None:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    all_files = sorted(snap.files, key=lambda f: f.tokens, reverse=True)

    console.print()
    console.rule(f"[bold bright_white]> {snap.root.name}[/]  [dim]per-file token breakdown[/]")
    console.print()

    # ── File table ──
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
    tbl.add_column("#", justify="right", style="dim", width=4)
    tbl.add_column("File", min_width=38, no_wrap=False)
    tbl.add_column("Lang", width=11)
    tbl.add_column("LOC", justify="right")
    tbl.add_column("Raw tok", justify="right")
    tbl.add_column("Proj tok", justify="right")
    tbl.add_column("Saved", justify="right")
    tbl.add_column("Reason", style="dim")

    shown = all_files[:limit]
    for i, f in enumerate(shown, 1):
        reason = f.proj_reason
        lang_color = f.lang.color if f.lang else "dim white"
        lang_label = f.lang.name if f.lang else "?"
        raw = f.tokens
        proj = f.proj_tokens
        saved_pct = int((raw - proj) / max(1, raw) * 100)

        if reason is None:
            saved_str = f"[bright_green]-{saved_pct}%[/]"
            proj_str = f"[bright_green]{_fmt_num(proj)}[/]"
            reason_str = ""
        else:
            saved_str = "[dim]—[/]"
            proj_str = f"[dim]{_fmt_num(proj)}[/]"
            reason_str = f"[dim]{reason}[/]"

        tbl.add_row(
            str(i),
            f"[dim]{f.rel}[/]",
            f"[{lang_color}]{lang_label}[/]",
            _fmt_num(f.lines),
            _fmt_num(raw),
            proj_str,
            saved_str,
            reason_str,
        )

    console.print(tbl)
    if len(all_files) > limit:
        console.print(f"  [dim]… {len(all_files) - limit} more files not shown. Increase --files N to see more.[/]")
    console.print()

    # ── Non-projectable summary ──
    non_proj = [(f, f.proj_reason) for f in snap.files if f.proj_reason is not None]

    # Group by reason
    by_reason: dict[str, list[FileStats]] = defaultdict(list)
    for f, r in non_proj:
        by_reason[r].append(f)

    console.print("[bold bright_white]  Non-projectable files[/]  [dim]by skip reason[/]")
    console.print()

    reason_tbl = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
    reason_tbl.add_column("Reason", min_width=22)
    reason_tbl.add_column("Files", justify="right")
    reason_tbl.add_column("Raw tokens", justify="right")
    reason_tbl.add_column("Notes", style="dim")

    reason_notes = {
        "no structural grammar": "plain text / data — no tree-sitter grammar",
        "already minimal": "nothing to drop — no comments, blank runs, or padding",
    }

    for reason, files in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        total_raw = sum(f.tokens for f in files)
        note = reason_notes.get(reason, "")
        reason_tbl.add_row(
            f"[yellow]{reason}[/]",
            _fmt_num(len(files)),
            _fmt_num(total_raw),
            note,
        )

    console.print(reason_tbl)

    # Detail drill-down: list top 10 non-projectable by token count
    if non_proj:
        console.print()
        console.print("  [dim]Top non-projectable files by token count:[/]")
        console.print()
        detail_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        detail_tbl.add_column("File", style="dim", min_width=40)
        detail_tbl.add_column("Reason", style="yellow", width=20)
        detail_tbl.add_column("Tokens", justify="right", style="dim")

        for f, reason in sorted(non_proj, key=lambda x: -x[0].tokens)[:10]:
            detail_tbl.add_row(f.rel, reason or "", _fmt_num(f.tokens))
        console.print(detail_tbl)

    _render_mode_breakdown(console, snap)

    # Summary panel
    proj_count = snap.large_files
    nonproj_count = len(non_proj)
    proj_raw = snap.proj_tokens_total
    proj_after = snap.proj_tokens_after
    summary = Panel(
        f"  [dim]Projectable[/]      [bright_green]{_fmt_num(proj_count)}[/] files  ·  "
        f"[white]{_fmt_num(proj_raw // 1000)}k[/] raw → [bright_green]{_fmt_num(proj_after // 1000)}k[/] projected  "
        f"[dim]({int(snap.proj_tokens_saved / max(1, proj_raw) * 100)}% saved)[/]\n"
        f"  [dim]Non-projectable[/]  [yellow]{_fmt_num(nonproj_count)}[/] files  ·  "
        f"[dim]read at full token cost[/]",
        title="[bold]Projection Summary[/]",
        border_style="dim",
        padding=(0, 2),
    )
    console.print(summary)
    console.print()


# ---------------------------------------------------------------------------
# --diff view
# ---------------------------------------------------------------------------


def _render_diff(file_path: Path, threshold: int) -> None:
    import json as _json

    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table

    from lemoncrow.core.capabilities.semantic_file_memory.capability import (
        SemanticFileMemoryCapability,
    )

    console = Console()

    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        console.print(f"[red]Cannot read file: {e}[/]")
        return

    info = SemanticFileMemoryCapability.project_modes(file_path, text, outline_threshold=threshold)
    language = info["language"]
    raw_tok = int(info["raw_tokens"])
    winner = info["winner"]
    modes = info["modes"]

    meta = {
        "full": ("Full", "white", "raw source · no projection"),
        "outline": ("Outline", "bright_green", "structure only · bodies omitted (LLM fetches bodies on demand)"),
        "minified": ("Minified", "green", "comments · blank lines dropped · re-parsed (full code kept)"),
        "compact": ("Compact", "yellow", "trailing whitespace · blank runs collapsed"),
    }
    raw_lines = text.splitlines()

    console.print()
    console.rule(
        f"[bold bright_white]{file_path.name}[/]  [dim]{language}  ·  {len(raw_lines)} lines  ·  {raw_tok:,} tok[/]"
    )
    console.print()

    # ── Every projection mode, nothing hidden ──
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 2))
    tbl.add_column("Mode")
    tbl.add_column("Tokens", justify="right")
    tbl.add_column("Saved", justify="right")
    tbl.add_column("")
    for name in ("full", "outline", "minified", "compact"):
        label, color, desc = meta[name]
        m = modes[name]
        available = bool(m["available"]) or name == "full"
        tok = min(int(m["tokens"]), raw_tok)
        saved = int((raw_tok - tok) / max(1, raw_tok) * 100)
        if name == winner:
            note = "[bold bright_green]◀ shipped to the LLM[/]"
        elif not available:
            note = f"[dim]n/a here — {desc}[/]"
        else:
            note = f"[dim]{desc}[/]"
        name_cell = f"[{color}]{label}[/]" if available else f"[dim]{label}[/]"
        tok_cell = f"{tok:,}" if available else "[dim]—[/]"
        if name == "full":
            saved_cell = "[dim]—[/]"
        elif not available:
            saved_cell = "[dim]n/a[/]"
        else:
            saved_cell = f"[bright_green]-{saved}%[/]" if saved > 0 else "[dim]0%[/]"
        tbl.add_row(name_cell, tok_cell, saved_cell, note)
    console.print(tbl)
    console.print()

    # ── Winner, side by side (outline pretty-printed for readability) ──
    label, color, desc = meta[winner]
    winner_text = modes[winner]["text"] or text
    proj_lexer = language
    display_text = winner_text
    pretty_note = ""
    if winner == "outline" and display_text.lstrip().startswith(("{", "[")):
        proj_lexer = "json"
        try:
            display_text = _json.dumps(_json.loads(display_text), indent=2, ensure_ascii=False)
            pretty_note = "pretty-printed for readability — shipped to the LLM as single-line JSON"
        except ValueError:
            pass

    proj_lines = display_text.splitlines()
    proj_tok = min(int(modes[winner]["tokens"]), raw_tok)

    console.print(f"  Winner: [{color}]{label}[/] [dim]({desc})[/]")
    if pretty_note:
        console.print(f"  [dim]{pretty_note}[/]")
    console.print()

    raw_syntax = Syntax(text, language, theme="monokai", line_numbers=True, word_wrap=False)
    proj_syntax = Syntax(display_text, proj_lexer, theme="monokai", line_numbers=True, word_wrap=False)
    split = Table.grid(expand=True, padding=(0, 1))
    split.add_column(ratio=1)
    split.add_column(ratio=1)
    split.add_row(
        Panel(
            raw_syntax,
            title=f"[dim]Raw ({len(raw_lines)} lines · {raw_tok:,} tok)[/]",
            border_style="dim",
            padding=(0, 0),
        ),
        Panel(
            proj_syntax,
            title=f"[{color}]{label} ({len(proj_lines)} lines · {proj_tok:,} tok shipped)[/]",
            border_style=f"{color} dim",
            padding=(0, 0),
        ),
    )
    console.print(split)
    console.print()


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("project")
@click.argument("path", default=".", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--top", default=5, show_default=True, type=int, help="Number of top entries per table.")
@click.option(
    "--files",
    "files_limit",
    default=0,
    type=int,
    metavar="N",
    help="Show per-file token breakdown table (top N files by token count). 0 = off.",
)
@click.option(
    "--diff",
    "diff_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Show raw vs projected outline for a specific file.",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.option(
    "--threshold",
    default=None,
    type=int,
    metavar="LOC",
    help="Outline threshold in effective LOC. Default: LEMONCROW_OUTLINE_THRESHOLD env or 0 "
    "(project every file) — same knob the read tool uses.",
)
@click.pass_context
def project_cmd(
    ctx: click.Context,
    path: Path,
    top: int,
    files_limit: int,
    diff_path: Path | None,
    as_json: bool,
    threshold: int | None,
) -> None:
    """Scan a project and show language breakdown, top files, directories, and LemonCrow savings.

    \b
    Examples:
      lc project               # overview of cwd
      lc project --files 30    # per-file token table (top 30 by tokens)
      lc project --diff src/foo.py   # raw vs projected side-by-side
      lc project --threshold 0      # outline-project every file
      lc project --json        # raw JSON
    """
    resolved = _resolve_threshold(threshold)
    if diff_path is not None:
        _render_diff(diff_path, resolved)
        return

    root = path.resolve()
    snap = _scan(root, resolved)

    if as_json:
        click.echo(json.dumps(snap.to_json(), indent=2))
        return

    if files_limit > 0:
        _render_files(snap, limit=files_limit)
    else:
        _render(snap, top_n=top)
