from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_rust_outline_keeps_container_bodies_out(tmp_path: Path) -> None:
    source = """
pub struct Worker {
    id: usize,
}

impl Worker {
    pub fn run(&self) -> usize {
        let sentinel_body = 42;
        sentinel_body
    }
}
""".strip()
    path = tmp_path / "sample.rs"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    assert payload["mode"] == "outline"
    outline = payload["outline"]
    text = outline["text"] if isinstance(outline, dict) and "text" in outline else str(outline)
    assert "sentinel_body" not in text
    assert "pub struct Worker" in text
    assert "pub fn run" in text
