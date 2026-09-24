from __future__ import annotations

from pathlib import Path

from lemoncrow.infra.runtime.frontend_bundle import frontend_dir, frontend_is_prebuilt


def test_frontend_is_prebuilt(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    assert frontend_is_prebuilt(tmp_path) is True
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert frontend_is_prebuilt(tmp_path) is False


def test_frontend_dir_honors_explicit_override(tmp_path: Path, monkeypatch) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_FRONTEND_DIR", str(frontend))
    assert frontend_dir() == frontend
