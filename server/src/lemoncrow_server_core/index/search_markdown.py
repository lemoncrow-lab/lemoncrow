"""Server adapter for the thin-client-owned code-search Markdown renderer."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from lemoncrow_client.search_markdown import render_code_search_markdown as _render

from .query import QueryAnswer, QueryHit


def render_code_search_markdown(
    query: str,
    answer: QueryAnswer,
    *,
    load_source: Callable[[QueryHit], bytes | None],
) -> str:
    by_path = {hit.path: hit for hit in answer.hits}

    def source_for(raw: Mapping[str, object]) -> bytes | None:
        hit = by_path.get(str(raw.get("path") or ""))
        return None if hit is None else load_source(hit)

    return _render(query, answer.to_wire(), load_source=source_for)
