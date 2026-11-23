"""The local server counts Reader features, only by route, and only after the first-run notice."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from aiohttp import ClientSession
from lemoncrow_client import usage
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

REPO = Path(__file__).resolve().parents[2]
REVIEW = REPO / "src" / "lemoncrow" / "pro" / "capabilities" / "review"
ROUTES = [
    ("post", "/reviews/{review_id}/ask", "review-ask"),
    ("post", "/reviews/{review_id}/ask/stream", "review-ask"),
    ("get", "/reviews/{review_id}/map", "review-map"),
    ("get", "/reviews/{review_id}/revisions/{revision_number}/map", "review-map"),
    ("post", "/reviews/{review_id}/ai-review", "review-local-ai"),
    ("post", "/reviews/{review_id}/ai-review/fresh", "review-local-ai"),
    ("post", "/reviews/{review_id}/check-recipes/run", "review-check-runs"),
    ("post", "/reviews/{review_id}/data-path/run", "review-data-path"),
    ("post", "/reviews/{review_id}/part-proof/run", "review-part-proof"),
    ("post", "/reviews/{review_id}/premerge/evaluate", "review-premerge-checks"),
    ("post", "/reviews/{review_id}/runtime-validation", "review-runtime-validation"),
    ("post", "/reviews/{review_id}/threads/{thread_id}/summary", "review-discussion-summaries"),
    ("post", "/reviews/{review_id}/rule-suggestions/{suggestion_id}/adopt", "review-rule-suggestions"),
]


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_POLICY", str(tmp_path / "no-policy.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    for name in ("CI", "DO_NOT_TRACK", "LEMONCROW_TELEMETRY", "LEMONCROW_URL", "LEMONCROW_INSTALL_MODE"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_every_counted_route_is_a_real_review_route() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in REVIEW.glob("*api.py"))
    for method, template, feature in ROUTES:
        assert f'@router.{method}("{template}"' in source, template
        path = "/api" + template.replace("{review_id}", "r1").replace("{thread_id}", "t1")
        path = path.replace("{suggestion_id}", "s1").replace("{revision_number}", "2")
        assert usage.classify_reader_request(method.upper(), path) == feature


async def _ask(tmp_path: Path) -> None:
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=False)
    )
    url = await server.start()
    try:
        async with ClientSession() as client:
            headers = {"Origin": url, "Sec-Fetch-Site": "same-origin"}
            async with client.post(f"{url}/api/reviews/missing/ask", json={}, headers=headers) as response:
                assert response.status >= 400
            async with client.get(f"{url}/api/reviews/missing", headers=headers):
                pass
    finally:
        await server.stop()


def _rows() -> list[dict[str, object]]:
    return [row for rows in usage.pending().values() for row in rows]


def test_nothing_is_counted_before_the_notice(counting: Path) -> None:
    asyncio.run(_ask(counting))
    assert _rows() == []


def test_a_reader_request_is_counted_with_its_outcome(counting: Path) -> None:
    usage.announce(io.StringIO())
    asyncio.run(_ask(counting))
    rows = _rows()
    assert [(row["dim"], row["context"], row["outcome"], row["n"]) for row in rows] == [
        ("review-ask", "reader", "error", 1)
    ]
