from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from atelier.core.capabilities.lesson_promotion.models import TypedLesson
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

_SERIALIZER_PATH = (
    Path(__file__).resolve().parents[4] / "src" / "atelier" / "core" / "capabilities" / "sync" / "serializer.py"
)
_SERIALIZER_SPEC = importlib.util.spec_from_file_location("atelier_sync_serializer_test", _SERIALIZER_PATH)
assert _SERIALIZER_SPEC is not None and _SERIALIZER_SPEC.loader is not None
_SERIALIZER_MODULE = importlib.util.module_from_spec(_SERIALIZER_SPEC)
sys.modules[_SERIALIZER_SPEC.name] = _SERIALIZER_MODULE
_SERIALIZER_SPEC.loader.exec_module(_SERIALIZER_MODULE)
collect_sync_entities = _SERIALIZER_MODULE.collect_sync_entities


def test_serializer_syncs_typed_lessons(tmp_path) -> None:
    store = TypedLessonStore(tmp_path)
    lesson = TypedLesson(
        kind="route-preference",
        match={"tool": "read", "phase": "explore"},
        prefer={"vendor": "google", "model": "gemini-flash"},
        confidence=0.9,
    )
    store.upsert_lesson(lesson)

    entities = collect_sync_entities(tmp_path)

    entity = entities[f"typed_lesson:{lesson.id}"]
    assert entity.kind == "typed_lesson"
    assert entity.payload["lesson"]["kind"] == "route-preference"
