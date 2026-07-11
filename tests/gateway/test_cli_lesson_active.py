from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner, Result

from lemoncrow.core.capabilities.lesson_promotion.models import TypedLesson
from lemoncrow.core.capabilities.lesson_promotion.store import TypedLessonStore
from lemoncrow.gateway.cli import cli


def _invoke(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), *args])


def test_cli_lesson_active_list_show_enable_disable(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    store = TypedLessonStore(root)
    lesson = TypedLesson(
        kind="route-preference",
        match={"tool": "read", "phase": "explore"},
        prefer={"vendor": "google", "model": "gemini-flash"},
        confidence=0.9,
    )
    store.upsert_lesson(lesson)

    listed = _invoke(root, "lesson", "active", "list", "--json")
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload[0]["id"] == lesson.id

    shown = _invoke(root, "lesson", "active", "show", lesson.id, "--json")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["kind"] == "route-preference"

    disabled = _invoke(root, "lesson", "active", "disable", lesson.id, "--json")
    assert disabled.exit_code == 0, disabled.output
    assert json.loads(disabled.output)["enabled"] is False

    enabled = _invoke(root, "lesson", "active", "enable", lesson.id, "--json")
    assert enabled.exit_code == 0, enabled.output
    assert json.loads(enabled.output)["enabled"] is True
