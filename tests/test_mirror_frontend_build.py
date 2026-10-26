"""The public mirror carries every frontend file the Reader build reads.

The release workflow builds the Reader from the public mirror. v0.8.0 shipped an unstyled Reader because
``frontend/postcss.config.js`` was not in ``scripts/public-paths.txt``: Vite found no PostCSS config, never ran
Tailwind, and wrote the raw ``@tailwind`` directives into the stylesheet (11.7 KB where the real one is about 97 KB).
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_mirror_for_frontend_build", ROOT / "scripts" / "mirror.py")
assert _spec and _spec.loader
mirror = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mirror)

#: Read by ``npm run build`` (``tsc -b && vite build``) from the top of ``frontend/``.
BUILD_INPUTS = (
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/postcss.config.js",
    "frontend/tailwind.config.ts",
    "frontend/tsconfig.json",
    "frontend/tsconfig.node.json",
    "frontend/vite.config.ts",
)


def _tracked_frontend_files() -> list[str]:
    done = subprocess.run(["git", "ls-files", "frontend"], cwd=ROOT, capture_output=True, text=True, check=True)
    return done.stdout.split()


def test_the_public_mirror_carries_every_file_the_reader_build_reads() -> None:
    prefixes = mirror.load_public_prefixes()
    tracked = set(_tracked_frontend_files())
    absent = [name for name in BUILD_INPUTS if name not in tracked]
    assert absent == [], f"BUILD_INPUTS names files frontend/ no longer has: {absent}"
    private = [name for name in BUILD_INPUTS if not mirror.is_public(name, prefixes)]
    assert private == [], f"the public mirror omits files the Reader build reads: {private}"


def test_no_new_tool_config_in_frontend_is_left_out_of_the_mirror_unnoticed() -> None:
    """Vite, PostCSS and Tailwind find their ``*.config.*`` files by name, so a build config the mirror drops breaks the
    release silently. A new one is either public or, if it only drives tests, named here."""

    test_only = {
        "frontend/playwright.config.ts",
        "frontend/playwright.perf.config.ts",
        "frontend/playwright.perf.product.config.ts",
    }
    prefixes = mirror.load_public_prefixes()
    omitted = [
        name
        for name in _tracked_frontend_files()
        if name.count("/") == 1
        and ".config." in name
        and name not in test_only
        and not mirror.is_public(name, prefixes)
    ]
    assert omitted == [], f"add to scripts/public-paths.txt, or to test_only above if they only drive tests: {omitted}"
