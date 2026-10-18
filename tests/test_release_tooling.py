from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import release_changelog, release_notes, release_preflight, release_version


def _write_project(path: Path, *, version: str, lemoncrow_minimum: str | None = None, exact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dependencies = "dependencies = []\n"
    if lemoncrow_minimum is not None:
        operator = "==" if exact else ">="
        dependencies = f'dependencies = ["lemoncrow{operator}{lemoncrow_minimum}"]\n'
    path.write_text(
        f'[project]\nname = "fixture"\nversion = "{version}"\n{dependencies}',
        encoding="utf-8",
    )


def _write_client(repo: Path, *, version: str = "0.1.0", constant: str | None = None) -> None:
    _write_project(repo / "client/pyproject.toml", version=version)
    init = repo / "client/src/lemoncrow_client/__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(
        f'CLIENT_NAME: Final[str] = "lemoncrow-client"\nCLIENT_VERSION: Final[str] = "{constant or version}"\n',
        encoding="utf-8",
    )


def test_release_version_starts_at_v08_and_preserves_forward_compatibility_floor(tmp_path: Path) -> None:
    _write_project(tmp_path / "pyproject.toml", version="0.7.4")
    root = tmp_path / "pyproject.toml"
    root.write_text(root.read_text().replace("dependencies = []", 'dependencies = ["lemoncrow-client==0.1.0"]'))
    _write_client(tmp_path)
    _write_project(tmp_path / "server/pyproject.toml", version="0.1.0", lemoncrow_minimum="0.7.4", exact=True)
    server = tmp_path / "server/pyproject.toml"
    server.write_text(server.read_text().replace('"lemoncrow==0.7.4"', '"lemoncrow==0.7.4", "lemoncrow-client==0.1.0"'))
    _write_project(
        tmp_path / "enterprise/server/pyproject.toml",
        version="0.1.0",
        lemoncrow_minimum="0.7.4",
        exact=True,
    )
    _write_project(
        tmp_path / "enterprise/runtime/pyproject.toml",
        version="0.1.0",
        lemoncrow_minimum="0.7.4",
    )

    release_version.set_versions(tmp_path, "0.8.0")

    assert release_version.project_version(tmp_path) == "0.8.0"
    for relative in (
        Path("server/pyproject.toml"),
        Path("enterprise/server/pyproject.toml"),
        Path("enterprise/runtime/pyproject.toml"),
    ):
        assert release_version.lemoncrow_minimum(tmp_path, relative) == "0.8.0"
    assert release_version.check_versions(tmp_path, "0.8.0", require_private=True) == "0.8.0"
    assert release_version.project_version(tmp_path, release_version.CLIENT_PROJECT) == "0.8.0"
    assert release_version.client_init_version(tmp_path) == "0.8.0"

    # Future public releases remain compatible with packages that declare the
    # architectural v0.8 floor; release bumps do not tighten the lower bound.
    release_version.set_versions(tmp_path, "0.9.0")
    assert release_version.project_version(tmp_path) == "0.9.0"
    assert release_version.lemoncrow_minimum(tmp_path, Path("server/pyproject.toml")) == "0.8.0"
    assert release_version.check_versions(tmp_path, "0.9.0", require_private=True) == "0.9.0"
    assert release_version.client_init_version(tmp_path) == "0.9.0"
    assert '"lemoncrow-client==0.9.0"' in root.read_text()
    assert '"lemoncrow-client==0.9.0"' in server.read_text()


def test_release_version_rejects_pre_v08_or_exact_legacy_dependency(tmp_path: Path) -> None:
    _write_project(tmp_path / "pyproject.toml", version="0.8.0")
    _write_client(tmp_path)
    _write_project(tmp_path / "server/pyproject.toml", version="0.1.0", lemoncrow_minimum="0.7.4", exact=True)

    with pytest.raises(ValueError, match=r"lemoncrow>= compatibility floor"):
        release_version.check_versions(tmp_path)


def test_public_version_check_does_not_require_private_tree(tmp_path: Path) -> None:
    _write_project(tmp_path / "pyproject.toml", version="0.8.0")
    _write_client(tmp_path)
    _write_project(tmp_path / "server/pyproject.toml", version="0.1.0", lemoncrow_minimum="0.8.0")

    assert release_version.check_versions(tmp_path, require_private=False) == "0.8.0"
    with pytest.raises(ValueError, match="required private package metadata"):
        release_version.check_versions(tmp_path, require_private=True)


def test_client_version_sources_must_agree_and_match_an_explicit_release(tmp_path: Path) -> None:
    _write_project(tmp_path / "pyproject.toml", version="0.8.0")
    _write_project(tmp_path / "server/pyproject.toml", version="0.1.0", lemoncrow_minimum="0.8.0")

    # Between releases the client may lag the root version, but its own two
    # version sources may never diverge.
    _write_client(tmp_path, version="0.1.0")
    assert release_version.check_versions(tmp_path) == "0.8.0"
    _write_client(tmp_path, version="0.1.0", constant="0.2.0")
    with pytest.raises(ValueError, match="CLIENT_VERSION"):
        release_version.check_versions(tmp_path)

    # A named release (tag check) requires the client to match it too.
    _write_client(tmp_path, version="0.1.0")
    with pytest.raises(ValueError, match=r"client/pyproject.toml"):
        release_version.check_versions(tmp_path, "0.8.0")
    release_version.set_versions(tmp_path, "0.8.0")
    assert release_version.check_versions(tmp_path, "0.8.0") == "0.8.0"


def test_changelog_finalize_and_release_notes_use_the_same_version_section() -> None:
    source = """# Changelog

## [Unreleased]

### Added
- New architecture.

## [0.7.4] - 2026-09-14
- Previous release.
"""
    finalized = release_changelog.finalize(source, "0.8.0", "2026-10-01")

    assert "## [Unreleased]\n\n## [0.8.0] - 2026-10-01" in finalized
    assert release_notes.extract_section(finalized, "0.8.0") == "### Added\n- New architecture."
    rendered = release_notes.render_release_notes(finalized, "0.8.0")
    assert rendered.startswith("## LemonCrow 0.8.0")
    assert "New architecture." in rendered
    assert "hosted.sh" in rendered
    assert "install.sh" in rendered
    assert 'source "$HOME/.lemoncrow/shell-env.sh"' in rendered
    assert "lc runtime status" in rendered
    assert "lc runtime enable codex" in rendered


def test_changelog_finalize_folds_late_candidate_notes_into_existing_version() -> None:
    source = """# Changelog

## [Unreleased]

### Release-candidate fixes
- Preserve the Authward invitation callback.

## [0.8.0] - 2026-10-14

### Added
- Review snapshots.

## [0.7.4] - 2026-09-14
- Old release.
"""
    finalized = release_changelog.finalize(source, "0.8.0", "2026-10-14")
    assert finalized.count("## [0.8.0]") == 1
    assert finalized.split("## [Unreleased]", 1)[1].split("## [0.8.0]", 1)[0].strip() == ""
    notes = release_notes.extract_section(finalized, "0.8.0")
    assert "Preserve the Authward invitation callback." in notes
    assert "Review snapshots." in notes
    assert release_changelog.finalize(finalized, "0.8.0", "2026-10-14") == finalized


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _synced_release_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True, text=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "release-test@lemoncrow.invalid")
    _git(repo, "config", "user.name", "Release Test")
    _write_project(repo / "pyproject.toml", version="0.7.4")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- Candidate.\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    return repo, origin


def test_release_preflight_requires_a_clean_synced_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _origin = _synced_release_repo(tmp_path)
    monkeypatch.setattr(release_preflight, "REPO_ROOT", repo)

    assert release_preflight.run_preflight("v0.8.0", check_remotes=False) == "0.8.0"

    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not clean"):
        release_preflight.run_preflight("v0.8.0", check_remotes=False)


def test_release_preflight_rejects_unpushed_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _origin = _synced_release_repo(tmp_path)
    monkeypatch.setattr(release_preflight, "REPO_ROOT", repo)

    (repo / "next.txt").write_text("next\n", encoding="utf-8")
    _git(repo, "add", "next.txt")
    _git(repo, "commit", "-m", "ahead")

    with pytest.raises(RuntimeError, match=r"ahead=1"):
        release_preflight.run_preflight("v0.8.0", check_remotes=False)


def _prepare(repo: Path, tag: str = "v0.8.0", subject: str | None = None) -> None:
    """What make release-prepare commits: the version and its changelog section, one commit on main."""
    version = tag[1:]
    _write_project(repo / "pyproject.toml", version=version)
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{version}]\n\n### Added\n- Candidate.\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", subject or f"chore(release): {tag}")


def test_the_prepared_release_commit_is_what_is_qualified_and_tagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _origin = _synced_release_repo(tmp_path)
    monkeypatch.setattr(release_preflight, "REPO_ROOT", repo)
    with pytest.raises(RuntimeError, match=r"is not the v0\.8\.0 release commit"):
        release_preflight.run_prepared("v0.8.0", check_remotes=False)
    _prepare(repo)
    assert release_preflight.run_prepared("v0.8.0", check_remotes=False) == "0.8.0"
    # The pre-version checks refuse the same commit: it is no longer main before versioning.
    with pytest.raises(ValueError, match="must be newer"):
        release_preflight.run_preflight("v0.8.0", check_remotes=False)
    # Resumed after its push: origin/main is the release commit itself.
    _git(repo, "push", "origin", "main")
    assert release_preflight.run_prepared("v0.8.0", check_remotes=False) == "0.8.0"
    _git(repo, "tag", "v0.8.0")
    with pytest.raises(RuntimeError, match="already exists"):
        release_preflight.run_prepared("v0.8.0", check_remotes=False)
    assert release_preflight.run_prepared("v0.8.0", force=True, check_remotes=False) == "0.8.0"


@pytest.mark.parametrize("case", ["other-version", "not-on-main", "dirty", "wrong-subject"])
def test_a_commit_that_is_not_the_prepared_release_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    repo, _origin = _synced_release_repo(tmp_path)
    monkeypatch.setattr(release_preflight, "REPO_ROOT", repo)
    if case == "not-on-main":
        (repo / "next.txt").write_text("next\n", encoding="utf-8")
        _git(repo, "add", "next.txt")
        _git(repo, "commit", "-m", "unpushed work under the release")
    _prepare(repo, subject="chore(release): v0.8.1" if case == "wrong-subject" else None)
    if case == "other-version":
        _write_project(repo / "pyproject.toml", version="0.8.1")
        _git(repo, "commit", "-qam", "chore(release): v0.8.0")
    if case == "dirty":
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    expected = {
        "other-version": r"carries version 0\.8\.1",
        "not-on-main": "directly on origin/main",
        "dirty": "not clean",
        "wrong-subject": r"is not the v0\.8\.0 release commit",
    }[case]
    with pytest.raises(RuntimeError, match=expected):
        release_preflight.run_prepared("v0.8.0", check_remotes=False)
