"""Wire-format helpers shared by retrieval benchmark providers."""

from __future__ import annotations

import re

_CODE_SEARCH_RANGE_SUFFIX = re.compile(r":L\d+(?:-L\d+)?$")


def code_search_markdown_header_path(header: str) -> str:
    """Return the file path encoded by a compact ``code_search`` Markdown header.

    Current LemonCrow renders source blocks as e.g.::

        ## django/contrib/admindocs/utils.py:L27-L39 · trim_docstring

    The retrieval benchmark scores files. Treating the entire header as a path
    silently demotes every inline top hit once MCP ``structuredContent`` is
    absent, because the gold contains only ``django/.../utils.py``.
    """
    text = header.strip()
    if " · " in text:
        text = text.split(" · ", 1)[0].rstrip()
    return _CODE_SEARCH_RANGE_SUFFIX.sub("", text)


def code_search_markdown_paths(text: str) -> list[str]:
    """Recover only ranked headers/navigation, never paths embedded in source."""
    paths: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            paths.append(code_search_markdown_header_path(line[3:]))
        elif line.startswith("→ "):
            paths.extend(_expand_paths(code_search_markdown_header_path(line[2:])))
        elif line.startswith("candidate_files: "):
            paths.extend(_expand_paths(line[len("candidate_files: ") :]))
    return list(dict.fromkeys(paths))


def _expand_paths(text: str) -> list[str]:
    # Legacy candidate lists and current navigation share brace-compressed groups.
    segments = re.split(r",\s*(?![^{}]*\})", text)
    out: list[str] = []
    for segment in segments:
        segment = segment.strip()
        match = re.fullmatch(r"(.*)\{([^{}]*)\}", segment)
        if match:
            out.extend(match[1] + name.strip() for name in match[2].split(",") if name.strip())
        elif segment:
            out.append(segment)
    return out
