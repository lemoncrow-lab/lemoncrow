"""Resolve the actual previously published *stable* release for upgrade CI.

A tag-triggered release workflow can run after the current release is already
published (e.g. when a GH runner is delayed or a job is retried). "latest"
therefore is not a valid baseline: it can select the *candidate itself*.
Always choose the highest published stable semver STRICTLY OLDER than the
current candidate, and fail closed if none is known.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

_STABLE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_CANDIDATE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc\d+)?$")


def previous_tag(candidate: str, releases: list[dict[str, Any]]) -> str:
    match = _CANDIDATE.fullmatch(candidate)
    if match is None:
        raise ValueError(f"unsupported candidate tag: {candidate!r}")
    current = tuple(map(int, match.groups()))
    eligible: list[tuple[tuple[int, int, int], str]] = []
    for release in releases:
        tag = str(release.get("tagName", ""))
        stable = _STABLE.fullmatch(tag)
        if stable is None or release.get("isPrerelease") or release.get("isDraft"):
            continue
        version = tuple(map(int, stable.groups()))
        if version < current:
            eligible.append((version, tag))
    if not eligible:
        raise ValueError(f"no published stable release predates {candidate}")
    return max(eligible)[1]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: release_previous_tag.py vMAJOR.MINOR.PATCH[-rcN]")
    result = subprocess.run(
        [
            "gh",
            "release",
            "list",
            "--repo",
            "lemoncrow-lab/lemoncrow",
            "--limit",
            "100",
            "--json",
            "tagName,isPrerelease,isDraft",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    print(previous_tag(args[0], json.loads(result.stdout)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
