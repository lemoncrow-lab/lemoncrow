"""A synthetic repository at enterprise scale, for measurement rather than fixtures.

The Phase-11 budget was measured over 2,001 files and 0.39 MB, which is about
2% of what an enterprise monorepo looks like. A budget measured there says
almost nothing about the one a developer waits for: at that size the per-file
constant is invisible and the byte cost is zero.

What "enterprise scale" means here, and why:

* **>= 50,000 files.** Big enough that the per-file constant -- one ``lstat``,
  one ``open``, the ignore decision -- dominates, which is the regime a real
  monorepo is in.
* **A realistic size distribution.** Source-file sizes are heavy-tailed, not
  uniform: a log-normal with a median near 1.8 kB, a 99th percentile in the
  tens of kB, and a thin tail of generated or vendored files up to a megabyte.
  A uniform distribution would make the byte cost a single multiply and hide
  the fact that a handful of files carry a large share of the bytes.
* **Several languages.** ``walk_worktree`` assigns a parser profile per
  extension and the server derives Layer 1 under it, so a single-language tree
  would exercise one branch of that mapping and one shape of content.
* **Directory fan-out that looks like a repository.** Files are spread over a
  three-level tree with a bounded number of entries per directory, so
  ``os.scandir`` is called thousands of times rather than once and the
  ``.gitignore`` stack is pushed and popped the way it is in a real walk.

Deterministic for a given seed: the same seed produces byte-identical trees, so
two measurements are comparable and a failing mutation can be replayed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["LANGUAGES", "SynthRepo", "generate_repository"]

#: Extension -> share of files. Roughly the mix of a polyglot monorepo: a large
#: application language, a large front end, two systems languages, and the
#: configuration and documentation that always outnumbers people's estimate.
LANGUAGES: Final[tuple[tuple[str, float], ...]] = (
    (".py", 0.26),
    (".ts", 0.20),
    (".tsx", 0.08),
    (".go", 0.14),
    (".rs", 0.10),
    (".js", 0.06),
    (".json", 0.06),
    (".md", 0.05),
    (".yaml", 0.03),
    (".txt", 0.02),
)

#: Log-normal parameters over file size in bytes. ``exp(_MU)`` is the median.
_MU: Final[float] = 7.50  # median ~1.8 kB
_SIGMA: Final[float] = 1.25
_MIN_BYTES: Final[int] = 96
_MAX_BYTES: Final[int] = 96 * 1024
#: One file in this many is a generated or vendored artifact: the heavy tail
#: that makes the byte total a distribution rather than a multiplication.
_HEAVY_EVERY: Final[int] = 400
_HEAVY_MIN: Final[int] = 200 * 1024
_HEAVY_MAX: Final[int] = 1_400 * 1024

#: Entries per directory, so the tree has real fan-out.
_LEAF_FANOUT: Final[int] = 24
_MID_FANOUT: Final[int] = 18

_HEADERS: Final[dict[str, str]] = {
    ".py": '"""Module {n}."""\n\nimport os\n\n\ndef handler_{n}(value: int) -> int:\n    return value + {n}\n\n\n',
    ".ts": (
        "export interface Shape{n} {{\n  id: string;\n  value: number;\n}}\n\n"
        "export function build{n}(v: number): number {{\n  return v + {n};\n}}\n\n"
    ),
    ".tsx": (
        'import * as React from "react";\n\n'
        'export function Panel{n}(): JSX.Element {{\n  return <div className="p-{n}">{n}</div>;\n}}\n\n'
    ),
    ".go": 'package pkg{n}\n\nimport "fmt"\n\nfunc Handle{n}(v int) int {{\n\tfmt.Println({n})\n\treturn v + {n}\n}}\n\n',
    ".rs": "pub struct Item{n} {{\n    pub id: u64,\n}}\n\npub fn build_{n}(v: u64) -> u64 {{\n    v + {n}\n}}\n\n",
    ".js": "const CONST_{n} = {n};\n\nexport function run{n}(v) {{\n  return v + CONST_{n};\n}}\n\n",
    ".json": '{{\n  "name": "record-{n}",\n  "version": "1.0.{n}",\n  "entries": [\n',
    ".md": "# Document {n}\n\nNotes about component {n} and how it is wired.\n\n",
    ".yaml": "name: service-{n}\nreplicas: {n}\nlabels:\n",
    ".txt": "record {n}\n",
}

_FILLERS: Final[dict[str, str]] = {
    ".py": "# note {i}: component {n} handles case {i} of the request path.\n",
    ".ts": "// note {i}: component {n} handles case {i} of the request path.\n",
    ".tsx": "// note {i}: component {n} handles case {i} of the request path.\n",
    ".go": "// note {i}: component {n} handles case {i} of the request path.\n",
    ".rs": "// note {i}: component {n} handles case {i} of the request path.\n",
    ".js": "// note {i}: component {n} handles case {i} of the request path.\n",
    ".json": '    {{"index": {i}, "owner": "team-{n}"}},\n',
    ".md": "- item {i} of document {n}, describing one behaviour.\n",
    ".yaml": "  key{i}: value-{n}-{i}\n",
    ".txt": "line {i} of record {n}\n",
}

_TAILS: Final[dict[str, str]] = {".json": '    {"index": -1, "owner": "end"}\n  ]\n}\n'}


@dataclass(frozen=True, slots=True)
class SynthRepo:
    """What was written, so a measurement can report the tree it ran on."""

    root: Path
    files: int
    bytes: int
    directories: int
    by_extension: dict[str, int]

    @property
    def mean_bytes(self) -> float:
        return self.bytes / self.files if self.files else 0.0


def _sizes(count: int, rng: random.Random) -> list[int]:
    out: list[int] = []
    for index in range(count):
        if index % _HEAVY_EVERY == _HEAVY_EVERY - 1:
            out.append(rng.randint(_HEAVY_MIN, _HEAVY_MAX))
            continue
        raw = rng.lognormvariate(_MU, _SIGMA)
        out.append(max(_MIN_BYTES, min(_MAX_BYTES, int(raw))))
    return out


def _extensions(count: int, rng: random.Random) -> list[str]:
    names = [name for name, _share in LANGUAGES]
    weights = [share for _name, share in LANGUAGES]
    return rng.choices(names, weights=weights, k=count)


def render(extension: str, ordinal: int, size: int) -> bytes:
    """Content of about ``size`` bytes, unique to ``ordinal``, shaped like the language."""
    header = _HEADERS[extension].format(n=ordinal)
    tail = _TAILS.get(extension, "")
    filler = _FILLERS[extension]
    parts = [header]
    written = len(header) + len(tail)
    index = 0
    while written < size:
        line = filler.format(i=index, n=ordinal)
        parts.append(line)
        written += len(line)
        index += 1
    parts.append(tail)
    return "".join(parts).encode("utf-8")


def generate_repository(root: Path, *, files: int = 50_000, seed: int = 20260914) -> SynthRepo:
    """Write a deterministic synthetic repository of ``files`` source files."""
    if files < 1:
        raise ValueError("files must be >= 1")
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        # The ignore rules a real repository carries: enough of them that the
        # per-entry ignore decision is a measurable cost rather than a no-op.
        "\n".join(
            (
                "build/",
                "dist/",
                "node_modules/",
                "target/",
                "*.log",
                "*.pyc",
                "__pycache__/",
                ".venv/",
                "coverage/",
                "*.tmp",
                "*.swp",
                ".DS_Store",
                ".lemoncrow-local-fs-proof",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    sizes = _sizes(files, rng)
    extensions = _extensions(files, rng)
    by_extension: dict[str, int] = {}
    total = 0
    directories = 1
    current: Path | None = None
    leaf_count = 0
    mid_count = 0
    top = 0
    mid = 0
    leaf = 0

    for ordinal in range(files):
        if current is None or leaf_count >= _LEAF_FANOUT:
            leaf_count = 0
            leaf += 1
            mid_count += 1
            if mid_count > _MID_FANOUT:
                mid_count = 0
                mid += 1
                if mid >= _MID_FANOUT:
                    mid = 0
                    top += 1
            current = root / f"service{top:03d}" / f"module{mid:03d}" / f"part{leaf:05d}"
            current.mkdir(parents=True, exist_ok=True)
            directories += 1
        extension = extensions[ordinal]
        data = render(extension, ordinal, sizes[ordinal])
        target = current / f"unit_{ordinal:06d}{extension}"
        target.write_bytes(data)
        total += len(data)
        by_extension[extension] = by_extension.get(extension, 0) + 1
        leaf_count += 1

    # Directories the walk must skip, so the ignore rules are exercised against
    # something rather than matching nothing.
    for name in ("build", "node_modules", "target"):
        noise = root / name
        noise.mkdir(exist_ok=True)
        for index in range(200):
            (noise / f"artifact_{index:04d}.o").write_bytes(f"object {index}\n".encode())

    return SynthRepo(
        root=root,
        files=files + 1,  # the ``.gitignore`` the walk also manifests
        bytes=total,
        directories=directories,
        by_extension=by_extension,
    )
