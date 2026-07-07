"""Tests for the shared workspace-confinement helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.foundation.paths import confine_to_root


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
