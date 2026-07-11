"""Tree-sitter outline tests for Lua, HTML, CSS, and Markdown.

All four languages previously fell through to the generic regex outliner because
they lacked _LANG_CONFIG entries in treesitter_ast.py. These tests verify that
the tree-sitter path is now taken (outline["kind"] == "treesitter") and that the
structurally important nodes appear while prose / body content is suppressed.
"""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_lua_outline_reaches_treesitter(tmp_path: Path) -> None:
    """Lua .lua files get a tree-sitter outline showing function signatures and
    local variable declarations; function bodies are stripped."""
    source = """
local M = {}
local config = {
    debug = true,
    level = 3,
}

local function helper(a, b)
    local sentinel_body = a + b
    return sentinel_body
end

function M.greet(name)
    print("Hello, " .. name)
end

function M.compute(x, y)
    return helper(x, y)
end

return M
""".strip()
    path = tmp_path / "module.lua"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    assert payload["language"] == "lua"
    assert payload["mode"] == "outline"
    outline = payload["outline"]
    assert isinstance(outline, dict)
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # Top-level locals and function signatures are surfaced.
    assert "local M" in text
    assert "local config" in text
    assert "helper" in text
    assert "M.greet" in text
    assert "M.compute" in text
    # Function body content is stripped.
    assert "sentinel_body" not in text
    assert "Hello, " not in text


def test_html_outline_reaches_treesitter(tmp_path: Path) -> None:
    """HTML .html files get a tree-sitter outline flattening element start-tags;
    inner text content and end-tags are not shown."""
    source = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>My Page</title>
  <link rel="stylesheet" href="style.css">
  <script src="vendor.js"></script>
</head>
<body>
  <div class="container" id="app">
    <h1>Hello World</h1>
    <p class="lead">Sentinel prose content that must not appear.</p>
    <input type="text" placeholder="search" />
  </div>
  <script src="main.js"></script>
</body>
</html>
""".strip()
    path = tmp_path / "index.html"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    assert payload["language"] == "html"
    assert payload["mode"] == "outline"
    outline = payload["outline"]
    assert isinstance(outline, dict)
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # Structural tags and attributes are present.
    assert "<!DOCTYPE" in text
    assert "lang=" in text  # <html lang="en">
    assert "charset" in text  # <meta charset="UTF-8">
    assert "style.css" in text  # <link> with href
    assert "vendor.js" in text  # <script src=...>
    assert 'id="app"' in text  # <div id="app">
    assert "main.js" in text
    # Inner prose text is not shown.
    assert "Sentinel prose" not in text
    assert "Hello World" not in text


def test_css_outline_reaches_treesitter(tmp_path: Path) -> None:
    """CSS .css files get a tree-sitter outline showing selectors and at-rules
    with their blocks replaced by { ... }; declaration bodies are stripped."""
    source = """
@import url('reset.css');

:root {
    --primary: #333;
    --sentinel-body-var: #fff;
}

.container {
    display: flex;
    flex-direction: row;
    sentinel-body-prop: do-not-leak;
}

#app {
    width: 100%;
}

@media (max-width: 768px) {
    .container {
        flex-direction: column;
    }
}

@keyframes fadeIn {
    from { opacity: 0; }
    to   { opacity: 1; }
}
""".strip()
    path = tmp_path / "styles.css"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    assert payload["language"] == "css"
    assert payload["mode"] == "outline"
    outline = payload["outline"]
    assert isinstance(outline, dict)
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # At-rules and selectors are surfaced.
    assert "@import" in text
    assert ":root" in text
    assert ".container" in text
    assert "#app" in text
    assert "@media" in text
    assert "@keyframes" in text
    assert "fadeIn" in text
    # Declaration body values are stripped.
    assert "sentinel-body-var" not in text
    assert "sentinel-body-prop" not in text
    assert "do-not-leak" not in text


def test_markdown_outline_reaches_treesitter(tmp_path: Path) -> None:
    """Markdown .md files get a tree-sitter outline showing ATX and setext
    headings plus fenced code-block openers; paragraph prose is suppressed."""
    source = """
# Chapter One

Sentinel paragraph prose that must not appear in the outline.

## Section 1.1

More prose text here, also excluded.

### Subsection 1.1.1

```python
# code block sentinel line
def foo(): pass
```

Subsection Prose
----------------

Setext-style heading above.
""".strip()
    path = tmp_path / "guide.md"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    assert payload["language"] == "markdown"
    assert payload["mode"] == "outline"
    outline = payload["outline"]
    assert isinstance(outline, dict)
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # ATX headings at every level are present.
    assert "# Chapter One" in text
    assert "## Section 1.1" in text
    assert "### Subsection 1.1.1" in text
    # Fenced code-block opener is present.
    assert "```python" in text
    # Setext heading is present.
    assert "Subsection Prose" in text
    # Paragraph prose is excluded.
    assert "Sentinel paragraph" not in text
    assert "More prose" not in text
    assert "Setext-style heading above" not in text
