"""Tests for the shared workspace-confinement helper."""

from __future__ import annotations

import random
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from lemoncrow.core.foundation.paths import confine_relative_to_root, confine_to_root


def test_in_root_path_ok(tmp_path: Path) -> None:
    inside = tmp_path / "sub" / "file.txt"
    inside.parent.mkdir(parents=True)
    inside.write_text("hi", encoding="utf-8")

    result = confine_to_root(inside, tmp_path)
    assert result == inside.resolve()


def test_root_itself_ok(tmp_path: Path) -> None:
    assert confine_to_root(tmp_path, tmp_path) == tmp_path.resolve()


def test_dot_dot_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(ValueError, match="path escapes the allowed root"):
        confine_to_root(root / ".." / "secret.txt", root)


def test_absolute_outside_path_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="path escapes the allowed root"):
        confine_to_root(outside, root)


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside_target = tmp_path / "outside.txt"
    outside_target.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(outside_target)
    with pytest.raises(ValueError, match="path escapes the allowed root"):
        confine_to_root(link, root)


def _agree(root: Path, relative: str) -> bool:
    def verdict(check: Callable[[], object]) -> bool:
        try:
            check()
        except ValueError:
            return False
        return True

    fast = verdict(partial(confine_relative_to_root, root, relative))
    full = verdict(partial(confine_to_root, root / relative, root))
    return fast == full


def test_relative_confine_matches_confine_to_root(tmp_path: Path) -> None:
    """Over a tree with in-root, escaping and looping symlinks the two agree on every path."""
    root = (tmp_path / "workspace").resolve()
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "a.py").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    (root / "to_outside").symlink_to(outside)
    (root / "to_inside").symlink_to(root / "src")
    (root / "src" / "up").symlink_to(root / "src" / "..")
    (root / "loop").symlink_to(root / "loop")
    (root / "src" / "dangling").symlink_to(tmp_path / "nowhere")
    names = [
        "src",
        "pkg",
        "a.py",
        "to_outside",
        "to_inside",
        "up",
        "loop",
        "dangling",
        "secret.txt",
        "..",
        ".",
        "",
        "gone",
    ]
    rng = random.Random(7)
    seen = set()
    for _ in range(600):
        relative = "/".join(rng.choice(names) for _ in range(rng.randint(1, 5)))
        seen.add(relative)
        assert _agree(root, relative), relative
    assert len(seen) > 100
    for relative in (
        "",
        "/etc/passwd",
        "../outside/secret.txt",
        "to_outside/secret.txt",
        "src/pkg/a.py",
        "src/pkg/gone/x.py",
    ):
        assert _agree(root, relative), relative


def test_relative_confine_rejects_a_symlink_that_escapes(tmp_path: Path) -> None:
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="path escapes the allowed root"):
        confine_relative_to_root(root, "link.txt")
    with pytest.raises(ValueError, match="path escapes the allowed root"):
        confine_relative_to_root(root, "../outside.txt")


def test_relative_confine_refuses_a_nul_byte_like_confine_to_root(tmp_path: Path) -> None:
    """A NUL below a missing component never reaches lstat, so the fast path must not accept it."""
    root = tmp_path.resolve()
    for relative in ("gone/a\x00b", "a\x00b", "gone/deeper/a\x00b/x.py"):
        with pytest.raises(ValueError):
            confine_to_root(root / relative, root)
        with pytest.raises(ValueError):
            confine_relative_to_root(root, relative)


def test_relative_confine_refuses_an_unencodable_name_like_confine_to_root(tmp_path: Path) -> None:
    """A lone surrogate below a missing component never reaches lstat either."""
    root = tmp_path.resolve()
    for relative in ("gone/\ud800", "\ud800", "gone/deeper/\ud800/x.py"):
        with pytest.raises(ValueError):
            confine_to_root(root / relative, root)
        with pytest.raises(ValueError):
            confine_relative_to_root(root, relative)


def test_relative_confine_expands_a_home_root_like_confine_to_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root written with ``~`` names the home directory, not a ``~`` folder under the working directory."""
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "proj" / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        confine_to_root(Path("~/proj") / "link/x.py", "~/proj")
    with pytest.raises(ValueError):
        confine_relative_to_root("~/proj", "link/x.py")
    confine_relative_to_root("~/proj", "src/x.py")


def test_relative_confine_accepts_a_path_that_is_not_on_disk(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    confine_relative_to_root(root, "src/pkg/not_there.py")
    (root / "file.txt").write_text("x", encoding="utf-8")
    confine_relative_to_root(root, "file.txt/below")
