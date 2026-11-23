"""The public release signs every asset keylessly, with the identity ``lc update`` pins (plan X5).

The release job of the public repository's release workflow signs each published file with cosign, writing
``<asset>.sigstore.json`` beside it, checks each signature against the workflow's own identity, and publishes nothing
without its bundle. ``lemoncrow_client.cli`` verifies the installer against that same identity before running it.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from lemoncrow_client import cli

ROOT = Path(__file__).resolve().parents[1]
# The private monorepo keeps the workflow under public-workflows/; the public mirror publishes it as workflows/.
CANDIDATES = (ROOT / ".github/public-workflows/release.yml", ROOT / ".github/workflows/release.yml")
ISSUER = "https://token.actions.githubusercontent.com"
SIGN = "Keylessly sign every release asset"
CREATE = "Create Release"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    path = next((candidate for candidate in CANDIDATES if candidate.is_file()), None)
    if path is None:
        pytest.skip("release workflow not present")
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in document and "on" not in document:
        document["on"] = document.pop(True)
    return document


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def _stub(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_only_the_release_job_can_mint_a_signing_token(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        if name == "release":
            assert job["permissions"] == {"contents": "write", "id-token": "write"}
        else:
            assert "id-token" not in str(job.get("permissions", {})), name


def test_the_release_signs_after_staging_and_before_publishing_with_a_pinned_cosign(workflow: dict[str, Any]) -> None:
    steps = workflow["jobs"]["release"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Stage release assets") < names.index("Install cosign") < names.index(SIGN) < names.index(CREATE)
    installer = _step(workflow["jobs"]["release"], "Install cosign")
    assert re.fullmatch(r"sigstore/cosign-installer@[0-9a-f]{40}", installer["uses"])
    assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", installer["with"]["cosign-release"])
    stage = _step(workflow["jobs"]["release"], "Stage release assets")["run"]
    assert 'LEMONCROW_RELEASE_TAG="${TAG}" bash scripts/stage_release.sh stage hosted binaries' in stage


@pytest.mark.parametrize("tag", ["v1.2.3", "v0.9.0-rc1"])
def test_the_signing_identity_is_the_one_the_client_pins(workflow: dict[str, Any], tag: str) -> None:
    step = _step(workflow["jobs"]["release"], SIGN)
    identity = (
        step["env"]["CERTIFICATE_IDENTITY"]
        .replace("${{ github.repository }}", "lemoncrow-lab/lemoncrow")
        .replace("${{ github.ref }}", f"refs/tags/{tag}")
    )
    assert identity == cli._RELEASE_SIGNER + tag
    assert re.search(cli._RELEASE_SIGNER_ANY_TAG, identity)
    assert cli._RELEASE_SIGNER_ISSUER == ISSUER
    assert f"--certificate-oidc-issuer {ISSUER}" in step["run"]


def _run(tmp_path: Path, workflow: dict[str, Any], step: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "step.sh").write_text(_step(workflow["jobs"]["release"], step)["run"], encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "GITHUB_REF_NAME": "v1.2.3",
        "CERTIFICATE_IDENTITY": cli._RELEASE_SIGNER + "v1.2.3",
    }
    return subprocess.run(
        ["bash", "-e", str(tmp_path / "step.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.fixture
def assets(tmp_path: Path) -> list[str]:
    names = ["binaries/lemoncrow-distribution-linux-x86_64.tar.gz", "stage/install.sh", "stage/runtime.sh"]
    for name in names:
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(name)
    log = tmp_path / "calls.log"
    # sign-blob writes the bundle it names; every call is recorded.
    _stub(
        tmp_path / "bin",
        "cosign",
        f'echo "$*" >> "{log}"\n[[ "$1" != sign-blob ]] || echo signed > "$4"',
    )
    _stub(tmp_path / "bin", "gh", f'echo "gh $*" >> "{log}"')
    return names


def test_every_asset_is_signed_and_checked_against_the_pinned_identity(
    tmp_path: Path, workflow: dict[str, Any], assets: list[str]
) -> None:
    result = _run(tmp_path, workflow, SIGN)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    identity = cli._RELEASE_SIGNER + "v1.2.3"
    for name in assets:
        assert (tmp_path / f"{name}.sigstore.json").read_text() == "signed\n"
        assert f"sign-blob --yes --bundle {name}.sigstore.json {name}" in calls
        assert (
            f"verify-blob --bundle {name}.sigstore.json --certificate-identity {identity}"
            f" --certificate-oidc-issuer {ISSUER} {name}"
        ) in calls


def test_the_release_publishes_every_asset_with_its_bundle(
    tmp_path: Path, workflow: dict[str, Any], assets: list[str]
) -> None:
    assert _run(tmp_path, workflow, SIGN).returncode == 0
    (tmp_path / "RELEASE_NOTES.md").write_text("notes")
    result = _run(tmp_path, workflow, CREATE)
    assert result.returncode == 0, result.stderr
    create = next(
        line for line in (tmp_path / "calls.log").read_text().splitlines() if line.startswith("gh release create")
    )
    for name in assets:
        assert f" {name} " in f"{create} " and f" {name}.sigstore.json" in create


def test_an_asset_without_its_bundle_stops_the_release_before_anything_is_published(
    tmp_path: Path, workflow: dict[str, Any], assets: list[str]
) -> None:
    assert _run(tmp_path, workflow, SIGN).returncode == 0
    (tmp_path / "stage/late.sh").write_text("added after signing")
    result = _run(tmp_path, workflow, CREATE)
    assert result.returncode != 0
    assert "stage/late.sh has no signature bundle" in result.stdout
    assert not [line for line in (tmp_path / "calls.log").read_text().splitlines() if line.startswith("gh ")]
