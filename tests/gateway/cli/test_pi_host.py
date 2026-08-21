from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from lemoncrow.gateway.cli import pi_host


def _archive_bytes() -> bytes:
    payload = b"""#!/bin/sh
if [ "${1:-}" = "--help" ]; then
  cat <<'HELP'
--offline --no-tools --no-context-files --no-extensions --no-skills
--no-prompt-templates --no-approve --session-dir --provider --model --extension
HELP
  exit 0
fi
echo pi-test
"""
    theme = b"{}\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for directory in ("pi", "pi/theme"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        executable = tarfile.TarInfo("pi/pi")
        executable.mode = 0o755
        executable.size = len(payload)
        archive.addfile(executable, io.BytesIO(payload))
        theme_file = tarfile.TarInfo("pi/theme/dark.json")
        theme_file.mode = 0o644
        theme_file.size = len(theme)
        archive.addfile(theme_file, io.BytesIO(theme))
    return buffer.getvalue()


def test_pinned_release_is_exact_reviewed_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pi_host.platform, "machine", lambda: "x86_64")
    release = pi_host.pinned_release()
    assert release.tag == "v0.84.2"
    assert release.commit == "914cf1472e715297caa30db4b9535d534a9eb718"
    assert release.asset_name == "pi-linux-x64.tar.gz"
    assert release.checksum == "906fbe787fd225c4ac624fe7ebd5b1d55a60e0f5c7ef51795d231564f9ee1c13"


def test_install_verifies_checksum_and_records_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _archive_bytes()
    checksum = hashlib.sha256(archive).hexdigest()
    monkeypatch.setattr(pi_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pi_host.platform, "machine", lambda: "x86_64")
    monkeypatch.setitem(pi_host._ASSET_SHA256, ("linux", "x64"), checksum)
    monkeypatch.setattr(pi_host, "_download_bytes", lambda _url: archive)

    path = pi_host.install_host_release(tmp_path)
    assert path.is_file()
    assert path.stat().st_mode & 0o111
    assert (pi_host.managed_bundle_dir(tmp_path) / "pi").is_file()
    assert (pi_host.managed_bundle_dir(tmp_path) / "theme" / "dark.json").is_file()
    status = pi_host.host_status(tmp_path)
    assert status["installed"] is True
    metadata = status["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["tag"] == pi_host.PINNED_PI_TAG
    assert metadata["sha256"] == checksum
    assert pi_host.remove_host(tmp_path) is True
    assert not path.exists()


def test_install_rejects_checksum_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi_host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pi_host.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(pi_host, "_download_bytes", lambda _url: _archive_bytes())
    with pytest.raises(Exception, match="checksum mismatch"):
        pi_host.install_host_release(tmp_path)


def test_validate_host_binary_rejects_missing_fail_closed_flag(tmp_path: Path) -> None:
    binary = tmp_path / "pi"
    binary.write_text("#!/bin/sh\necho --offline --no-tools\n", encoding="utf-8")
    binary.chmod(0o755)
    with pytest.raises(Exception, match="lacks required managed-mode controls"):
        pi_host.validate_host_binary(binary)


def test_override_must_be_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "pi"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o644)
    monkeypatch.setenv("LEMONCROW_PI_HOST_BIN", str(binary))
    with pytest.raises(Exception, match="not executable"):
        pi_host.resolve_host_binary(tmp_path)
