"""Telegraphic benchmark: vanilla Claude Code vs full LemonCrow runtime.

Reproduces JuliusBrussee/caveman's benchmark+eval prompt sets
(https://github.com/JuliusBrussee/caveman/tree/main/benchmarks,
https://github.com/JuliusBrussee/caveman/tree/main/evals -- MIT, prompts used
verbatim, see prompts.json) as an ad-hoc ``benchmarks.codebench.run`` local
run: baseline = vanilla Claude Code, lemoncrow = the real ``lc:auto``
persona (LemonCrow's shipped ultra reply-register + real tools/MCP) -- apples
to apples with every other number in this repo's BENCHMARKS.md, not an
isolated system-prompt swap. See ``lc benchmark telegraphic --help``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

_PROMPTS_PATH = Path(__file__).parent / "prompts.json"

_SCRATCH_README = """\
# Telegraphic benchmark scratch repo

Intentionally minimal. Exists only so `lc benchmark telegraphic` has a
real git repo to hand each Claude Code arm -- the prompts are general dev
Q&A, not tied to any specific codebase, so there is deliberately nothing
here to explore. Answer from general knowledge.
"""


def load_prompts(limit: int | None = None) -> list[dict]:
    """The 20 caveman prompts (id/source/category/prompt), optionally truncated."""
    data = json.loads(_PROMPTS_PATH.read_text(encoding="utf-8"))
    prompts: list[dict] = data["prompts"]
    return prompts[:limit] if limit else prompts


def ensure_scratch_repo() -> Path:
    """A minimal, reusable git repo with nothing in it to explore.

    Default ``--repo`` target: a NOT-committed runtime fixture (a nested
    ``.git`` checked into this repo would be tracked as a submodule gitlink,
    not real content) so codebench's ad-hoc ``--prompt`` mode always has a
    real git repo to copy per run, without an irrelevant giant codebase for
    the agent to wander through on generic Q&A prompts.
    """
    repo = Path(tempfile.gettempdir()) / "lemoncrow-telegraphic-scratch-repo"
    if not (repo / ".git").is_dir():
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "README.md").write_text(_SCRATCH_README, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=bench@lemoncrow.local",
                "-c",
                "user.name=lemoncrow-bench",
                "commit",
                "-q",
                "-m",
                "scratch repo for telegraphic benchmark",
            ],
            cwd=repo,
            check=True,
        )
    return repo


__all__ = ["ensure_scratch_repo", "load_prompts"]
