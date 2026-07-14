from __future__ import annotations

from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.models import Playbook
from lemoncrow.infra.embeddings.null_embedder import NullEmbedder
from lemoncrow.infra.storage.bundle import StoreBundle
from lemoncrow.pro.capabilities.style_import.importer import import_files


def test_style_import_flags_near_duplicates(store: StoreBundle, tmp_path: Path) -> None:
    store.knowledge.upsert_block(
        Playbook(
            id="rb-existing",
            title="Use Schemas At Boundaries",
            domain="coding",
            situation="Use schemas at service boundaries.",
            procedure=["Use schemas at service boundaries before changing API code."],
        ),
        write_markdown=False,
    )
    guide = tmp_path / "STYLE.md"
    guide.write_text("## API\nUse schemas at service boundaries before changing API code.\n", encoding="utf-8")

    def fake_chat(messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "procedural": True,
            "title": "Use Schemas At Boundaries",
            "body": "Use schemas at service boundaries before changing API code.",
            "triggers": ["schemas", "API"],
            "procedure": ["Use schemas at service boundaries before changing API code."],
            "verification": ["Run API schema tests."],
            "confidence": 0.9,
        }

    candidates = import_files(
        [guide],
        "coding",
        store=store,
        write=False,
        chat_func=fake_chat,
        embedder=NullEmbedder(),
    )

    assert len(candidates) == 1
    assert candidates[0].evidence["near_duplicates"][0]["block_id"] == "rb-existing"
