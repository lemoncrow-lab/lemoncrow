from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bump_tool_pins.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("lc_bump_tool_pins", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    common = tmp_path / "common.sh"
    binaries = tmp_path / "binaries.py"
    compactors = tmp_path / "external_compactors.py"
    incontainer = tmp_path / "incontainer.py"
    common.write_text(
        "\n".join(
            [
                'LEMONCROW_RTK_TAG="${LEMONCROW_RTK_TAG-v0.49.0}"',
                'LEMONCROW_ASTGREP_VERSION="0.45.3"',
                'LEMONCROW_JJ_VERSION="0.45.1"',
                "https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-linux.zip old-linux",
                "https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-macos.zip old-macos",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    binaries.write_text(
        """_MANAGED_VERSION = "0.45.3"\n'
        'ManagedAstGrepAsset(archive_name="app-linux.zip", url="https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-linux.zip", sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")\n'
        'ManagedAstGrepAsset(archive_name="app-macos.zip", url="https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-macos.zip", sha256="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")\n""".replace(
            "'\n        '", ""
        ),
        encoding="utf-8",
    )
    # The installer copies the same checksums; the fixture mirrors that contract.
    common.write_text(
        common.read_text(encoding="utf-8").replace("old-linux", "a" * 64).replace("old-macos", "b" * 64),
        encoding="utf-8",
    )
    compactors.write_text('install_hint="cargo install --git https://github.com/rtk-ai/rtk --tag v0.49.0"\n')
    incontainer.write_text(
        "https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-linux.zip " + "a" * 64 + "\n",
        encoding="utf-8",
    )
    for name, value in {
        "ROOT": tmp_path,
        "COMMON": common,
        "BINARIES": binaries,
        "COMPACTORS": compactors,
        "INCONTAINER": incontainer,
        "ASTGREP_FILES": (common, binaries, incontainer),
    }.items():
        monkeypatch.setattr(module, name, value)
    return module, common, binaries, compactors, incontainer


def test_bump_script_rewrites_every_pin_and_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, common, binaries, compactors, incontainer = _fixture_files(tmp_path, monkeypatch)
    tags = {"ast-grep/ast-grep": "0.46.0", "rtk-ai/rtk": "v0.50.0", "jj-vcs/jj": "v0.46.0"}
    monkeypatch.setattr(module, "_latest_tag", lambda repo: tags[repo])
    monkeypatch.setattr(
        module,
        "_astgrep_shas",
        lambda version, names: {"app-linux.zip": "c" * 64, "app-macos.zip": "d" * 64},
    )

    assert module.bump_astgrep(False)
    assert module.bump_rtk(False)
    assert module.bump_jj(False)

    assert 'LEMONCROW_ASTGREP_VERSION="0.46.0"' in common.read_text()
    assert 'LEMONCROW_RTK_TAG="${LEMONCROW_RTK_TAG-v0.50.0}"' in common.read_text()
    assert 'LEMONCROW_JJ_VERSION="0.46.0"' in common.read_text()
    assert '_MANAGED_VERSION = "0.46.0"' in binaries.read_text()
    assert "c" * 64 in binaries.read_text() and "d" * 64 in binaries.read_text()
    assert "c" * 64 in incontainer.read_text()
    assert "--tag v0.50.0" in compactors.read_text()


def test_bump_script_never_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, common, binaries, compactors, incontainer = _fixture_files(tmp_path, monkeypatch)
    before = {path: path.read_text() for path in (common, binaries, compactors, incontainer)}
    tags = {"ast-grep/ast-grep": "0.45.2", "rtk-ai/rtk": "v0.48.0", "jj-vcs/jj": "v0.45.1"}
    monkeypatch.setattr(module, "_latest_tag", lambda repo: tags[repo])
    monkeypatch.setattr(module, "_astgrep_shas", lambda *_: pytest.fail("must not fetch checksums for a downgrade"))

    assert module.bump_astgrep(False) is None
    assert module.bump_rtk(False) is None
    assert module.bump_jj(False) is None
    assert {path: path.read_text() for path in before} == before


def test_bump_script_rejects_non_semver_release_tags() -> None:
    module = _load_module()
    with pytest.raises(SystemExit, match=r"expected x\.y\.z"):
        module._key("dev-0.50.0-rc.1")


def test_bump_workflow_does_not_expose_checkout_write_credentials_to_third_party_builds() -> None:
    workflow = (ROOT / ".github" / "workflows" / "bump-tool-pins.yml").read_text(encoding="utf-8")
    assert "persist-credentials: false" in workflow
    assert "gh auth setup-git" in workflow
    assert workflow.index("cargo install --git https://github.com/rtk-ai/rtk") < workflow.index("gh auth setup-git")
