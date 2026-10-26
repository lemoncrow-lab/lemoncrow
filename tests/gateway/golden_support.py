"""A tiny answer key (a real git repository) and a golden-comment corpus that points at it, for the golden tests.

Nothing here is real: the titles and comments are invented, the commits are fake. The shape is the real one.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

IDENTITY = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
KEY_DIRECTORY = "code_review_benchmarks"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40

#: file -> entries; each entry is ``(pr_title, [(comment, severity), ...])``.
KEY: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {
    "alpha.json": [
        (
            "Cache the lookup",
            [
                ("The cache is never invalidated when the user changes plan", "High"),
                ("The TTL is read in seconds but compared in milliseconds", "Critical"),
                ("Typo in a log line", "Low"),
            ],
        ),
        ("Rename the flag", [("The old flag name is still read in one place", "Medium")]),
    ],
    "beta.json": [("Add retries", [("Retries are unbounded", "High"), ("A comment is out of date", "Low")])],
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **IDENTITY}
    ).stdout.strip()


def make_key(root: Path) -> tuple[Path, str]:
    """A git repository holding the invented answer key; returns its path and its commit."""

    key = root / "key"
    (key / KEY_DIRECTORY).mkdir(parents=True)
    for name, entries in KEY.items():
        payload = [
            {"pr_title": title, "comments": [{"comment": text, "severity": severity} for text, severity in comments]}
            for title, comments in entries
        ]
        (key / KEY_DIRECTORY / name).write_text(json.dumps(payload), encoding="utf-8")
    git(key, "init", "-q")
    git(key, "add", "-A")
    git(key, "commit", "-q", "-m", "key")
    return key, git(key, "rev-parse", "HEAD")


def corpus_dict(commit: str) -> dict[str, Any]:
    def pr(pr_id: str, repository: str, golden: dict[str, Any], base: str, head: str) -> dict[str, Any]:
        return {
            "id": pr_id,
            "repository": repository,
            "pull": 0,
            "title": golden["title"],
            "license": "MIT",
            "provenance": {
                "source": "public_repository",
                "repository": f"https://github.com/{repository}",
                "commit": head,
            },
            "base_sha": base,
            "head_sha": head,
            "labelled": False,
            "golden": golden,
        }

    return {
        "version": 1,
        "id": "golden-test",
        "visibility": "private",
        "golden_comments": {
            "repository": "https://example.invalid/key.git",
            "commit": commit,
            "directory": KEY_DIRECTORY,
        },
        "prs": [
            pr(
                "alpha-01",
                "acme/alpha",
                {"file": "alpha.json", "index": 1, "title": "Cache the lookup", "language": "python"},
                SHA_A,
                SHA_B,
            ),
            pr(
                "alpha-02",
                "acme/alpha",
                {"file": "alpha.json", "index": 2, "title": "Rename the flag", "language": "python"},
                SHA_B,
                SHA_C,
            ),
            pr(
                "beta-01",
                "acme/beta",
                {"file": "beta.json", "index": 1, "title": "Add retries", "language": "go"},
                SHA_C,
                SHA_D,
            ),
        ],
    }


def write_corpus(root: Path, commit: str, mutate: Any = None) -> Path:
    data = corpus_dict(commit)
    if mutate is not None:
        mutate(data)
    path = root / "golden.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
