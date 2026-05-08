"""Environments — packaged operating governors.

An Environment encodes the law for a class of work (e.g. debugging,
state changes, or source-of-truth corrections). It is enforced two ways:

1. At plan-check time: any plan step containing a `forbidden` phrase is
   blocked, and `required` items are surfaced as required checks.
2. At rubric-gate time: the `rubric_id` attached to the environment is
   the rubric whose checks must pass before the action is accepted.

Environments are read-only YAML files shipped with the package. They are
NOT user memory. They are reviewable, version-controlled operating laws.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from importlib import resources
from pathlib import Path

import yaml

from atelier.core.foundation.models import Environment


def load_environment_file(path: Path) -> Environment:
    """Load and validate a single environment YAML file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Environment file {path} must be a YAML mapping.")
    return Environment(**raw)


def load_environments_from_dir(directory: Path) -> list[Environment]:
    envs: list[Environment] = []
    for entry in sorted(directory.glob("*.yaml")):
        envs.append(load_environment_file(entry))
    return envs


def load_packaged_environments() -> list[Environment]:
    """Load the environments shipped inside the atelier package."""
    pkg_dir = resources.files("atelier") / "core" / "environments"
    envs: list[Environment] = []
    for entry in sorted(pkg_dir.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".yaml"):
            continue
        with resources.as_file(entry) as path:
            envs.append(load_environment_file(Path(path)))
    return envs


def match_environments(
    task: str,
    domain: str | None,
    environments: Sequence[Environment],
) -> list[Environment]:
    """Return environments whose triggers fire for the given task/domain.

    Matching rules:
    - Domain match: if env.domain is a prefix of `domain`, it matches.
    - Trigger match: any trigger phrase appearing (case-insensitive) in
      the task text matches.

    Multiple matches are allowed and ordered most-specific first
    (longest matching domain prefix).
    """
    task_lower = task.lower()
    matches: list[tuple[int, Environment]] = []
    for env in environments:
        score = 0
        if domain and env.domain and domain.startswith(env.domain):
            score += len(env.domain)
        for trig in env.triggers:
            if trig.lower() in task_lower:
                score += 1
        if score > 0:
            matches.append((score, env))
    matches.sort(key=lambda pair: pair[0], reverse=True)
    return [env for _, env in matches]


def find_forbidden_violations(
    plan: Iterable[str],
    environments: Sequence[Environment],
) -> list[tuple[Environment, str, str]]:
    """Return triples of (env, plan_step, forbidden_phrase) for each violation."""
    violations: list[tuple[Environment, str, str]] = []
    for env in environments:
        for step in plan:
            step_lower = step.lower()
            for phrase in env.forbidden:
                if phrase.lower() in step_lower:
                    violations.append((env, step, phrase))
    return violations
