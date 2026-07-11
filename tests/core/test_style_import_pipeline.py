from __future__ import annotations

from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.style_import.importer import import_files
from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.infra.embeddings.null_embedder import NullEmbedder


def _chat(messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    content = messages[-1]["content"]
    title = "Imported API Rule" if "API" in content else "Imported Test Rule"
    return {
        "procedural": True,
        "title": title,
        "body": "Use the documented project convention before changing code.",
        "triggers": ["project convention"],
        "procedure": ["Read the convention.", "Apply it to the change."],
        "verification": ["Run the focused check."],
        "confidence": 0.8,
    }


def test_style_import_writes_lesson_candidates(store: ContextStore, tmp_path: Path) -> None:
    guide = tmp_path / "CONTRIBUTING.md"
    guide.write_text(
        "## API Rules\nUse schemas at boundaries.\n\n## Test Rules\nAdd focused tests.\n",
        encoding="utf-8",
    )

    candidates = import_files(
        [guide],
        "coding",
        store=store,
        write=True,
        limit=5,
        chat_func=_chat,
        embedder=NullEmbedder(),
    )

    assert len(candidates) == 2
    inbox = store.list_lesson_candidates(domain="coding", status="inbox")
    assert len(inbox) == 2
    assert inbox[0].evidence["source"] == "style-guide-import"
    assert inbox[0].proposed_block is not None
