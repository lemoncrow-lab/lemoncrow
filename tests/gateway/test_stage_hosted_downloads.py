"""scripts/stage_hosted_downloads.py writes the site-served hosted.sh (private: not mirrored)."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_site_hosted_installer_pins_the_artifact_it_is_served_beside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("stage_hosted_downloads", ROOT / "scripts/stage_hosted_downloads.py")
    assert spec and spec.loader
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    (tmp_path / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/hosted.sh", tmp_path / "scripts/hosted.sh")
    (tmp_path / "dist").mkdir()
    archive = tmp_path / "dist/lemoncrow-review-client.tar.gz"
    archive.write_bytes(b"audited review client")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (tmp_path / "dist/lemoncrow-review-client.tar.gz.sha256").write_text(digest + "\n")
    (tmp_path / "landing/functions").mkdir(parents=True)
    monkeypatch.setattr(stage, "ROOT", tmp_path)
    monkeypatch.setattr(stage.subprocess, "run", lambda *a, **k: None)  # the release-boundary audit
    stage.main()
    generated = (tmp_path / f"landing/public/downloads/hosted/{digest}/hosted.sh").read_text()
    assert (
        '[[ "$RELEASE_TAG" != latest || -n "${LEMONCROW_RELEASE_BASE_URL:-}" ]] || '
        f'RELEASE_PINS="lemoncrow-review-client.tar.gz={digest}"\n'
    ) in generated
    assert f'BASE_URL="https://lemoncrow.com/downloads/hosted/{digest}"' in generated
    subprocess.run(["bash", "-n", "-c", generated], check=True)
