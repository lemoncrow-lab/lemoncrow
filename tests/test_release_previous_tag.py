"""Upgrade smoke must never accidentally choose the candidate release itself."""

from __future__ import annotations

import pytest

from scripts import release_previous_tag
from scripts.release_previous_tag import previous_tag


def _release(tag: str, *, prerelease: bool = False, draft: bool = False) -> dict[str, object]:
    return {"tagName": tag, "isPrerelease": prerelease, "isDraft": draft}


def test_stable_published_before_upgrade_job_runs_is_not_its_own_baseline() -> None:
    releases = [_release("v0.8.0"), _release("v0.8.0-rc4", prerelease=True), _release("v0.7.4")]
    assert previous_tag("v0.8.0", releases) == "v0.7.4"
    assert previous_tag("v0.8.0-rc4", releases) == "v0.7.4"


def test_upgrade_selects_highest_semver_instead_of_api_order() -> None:
    releases = [_release("v0.7.3"), _release("v0.8.0"), _release("v0.7.11"), _release("v0.9.1")]
    assert previous_tag("v0.8.1", releases) == "v0.8.0"
    assert previous_tag("v0.8.0", releases) == "v0.7.11"


def test_draft_prerelease_or_newer_versions_are_not_valid_baselines() -> None:
    releases = [_release("v0.8.0"), _release("v0.7.5", prerelease=True), _release("v0.7.4", draft=True)]
    with pytest.raises(ValueError, match="no published stable"):
        previous_tag("v0.8.0", releases)


def test_missing_or_malformed_candidate_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported candidate"):
        previous_tag("latest", [_release("v0.7.4")])
    with pytest.raises(ValueError, match="no published stable"):
        previous_tag("v0.8.0", [])


def test_resolver_always_uses_published_public_repo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import subprocess

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[:6] == ["gh", "release", "list", "--repo", "lemoncrow-lab/lemoncrow", "--limit"]
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps([_release("v0.7.4"), _release("v0.8.0")]),
        )

    monkeypatch.setattr(release_previous_tag.subprocess, "run", fake_run)
    assert release_previous_tag.main(["v0.8.0"]) == 0
    assert capsys.readouterr().out.strip() == "v0.7.4"
