from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_rust_outline_keeps_container_bodies_out(tmp_path: Path) -> None:
    # Body is padded so the outline clears the 25%-savings guard (which now
    # also accounts for the projection-notice overhead); the point of the test
    # is that the stripped container body keeps ``sentinel_body`` out of the
    # outline, not that any tiny snippet outlines.
    body = "\n".join(f"        let sentinel_body_{i} = {i};" for i in range(30))
    source = f"""
pub struct Worker {{
    id: usize,
}}

impl Worker {{
    pub fn run(&self) -> usize {{
{body}
        sentinel_body_0
    }}
}}
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
