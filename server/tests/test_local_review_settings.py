"""P5-4 over the local server: the reviewer's personal Review settings (SPEC 4.11)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

PATH = "/api/review-settings"
DEFAULTS = {
    "default_view": "one",
    "skip_overview_single_decision": True,
    "start_all_files_over_30_parts": True,
    "theme": "auto",
    "key_looks_good": "a",
    "key_needs_change": "c",
    "key_decide_later": "n",
}


def _session(tmp_path: Path, calls: list[tuple[str, dict[str, Any] | None]]) -> list[tuple[int, Any]]:
    async def exercise() -> list[tuple[int, Any]]:
        server, _state = build_local_server(
            LocalServerConfig(
                listen_port=0, state_root=tmp_path / "state", local_no_auth=True, token_file=None, allow_local_fs=False
            )
        )
        url = await server.start()
        out: list[tuple[int, Any]] = []
        try:
            async with ClientSession() as client:
                for method, body in calls:
                    response = await client.request(method, f"{url}{PATH}", json=body)
                    out.append((response.status, await response.json(content_type=None)))
        finally:
            await server.stop()
        return out

    return asyncio.run(exercise())


def test_get_returns_the_defaults_and_nothing_set(tmp_path: Path) -> None:
    [(status, body)] = _session(tmp_path, [("GET", None)])
    assert status == 200
    assert body == {"settings": DEFAULTS, "set": []}


def test_patch_saves_and_a_later_get_returns_it(tmp_path: Path) -> None:
    patched, fetched = _session(tmp_path, [("PATCH", {"default_view": "all", "key_looks_good": "y"}), ("GET", None)])
    assert patched[0] == 200
    assert patched[1]["settings"]["key_looks_good"] == "y"
    assert patched[1]["set"] == ["default_view", "key_looks_good"]
    assert fetched[1] == patched[1]


def test_a_taken_key_is_refused_with_the_sentence_and_changes_nothing(tmp_path: Path) -> None:
    _, refused, fetched = _session(
        tmp_path, [("PATCH", {"key_needs_change": "y"}), ("PATCH", {"key_looks_good": "y"}), ("GET", None)]
    )
    assert refused[0] == 400
    assert refused[1]["error"]["message"] == "Y is already used for Needs a change. Pick another key."
    assert fetched[1]["settings"]["key_looks_good"] == "a"


def test_null_forgets_a_key_and_delete_forgets_all(tmp_path: Path) -> None:
    _, forgotten, _, reset = _session(
        tmp_path,
        [
            ("PATCH", {"theme": "dark", "default_view": "all"}),
            ("PATCH", {"theme": None}),
            ("PATCH", {"theme": "dark"}),
            ("DELETE", None),
        ],
    )
    assert forgotten[1]["set"] == ["default_view"]
    assert reset[1] == {"settings": DEFAULTS, "set": []}


def test_an_unknown_setting_and_a_non_object_body_are_refused(tmp_path: Path) -> None:
    unknown, listed = _session(tmp_path, [("PATCH", {"review_mode": "focus"}), ("PATCH", ["a"])])  # type: ignore[list-item]
    assert unknown[0] == 400
    assert listed[0] == 400


def test_the_settings_are_not_a_repository(tmp_path: Path) -> None:
    from lemoncrow_server_core.local_reviews import LocalReviewRepository

    repository = LocalReviewRepository(tmp_path / "reviews")
    repository.user_store().patch_user_settings("local", {"theme": "dark"})
    assert repository.list_repository_ids() == ()


def _guarded(
    tmp_path: Path,
    monkeypatch: Any,
    host: str | None,
    *,
    no_auth: bool,
    calls: tuple[tuple[str, str, Any], ...] = (("PATCH", PATH, {"key_looks_good": "y"}), ("DELETE", PATH, None)),
) -> list[int]:
    """Status of PATCH then DELETE on the real running server, the caller's process resolving to *host*."""
    from lemoncrow_server_core import local_app

    monkeypatch.setattr(local_app, "peer_agent_host", lambda _transport: host)

    async def exercise() -> list[int]:
        token_file = None if no_auth else tmp_path / "token"
        server, _state = build_local_server(
            LocalServerConfig(
                listen_port=0,
                state_root=tmp_path / "state",
                local_no_auth=no_auth,
                token_file=token_file,
                allow_local_fs=False,
            )
        )
        url = await server.start()
        statuses: list[int] = []
        try:
            headers = (
                {}
                if token_file is None
                else {"Authorization": f"Bearer {token_file.read_text(encoding='utf-8').strip()}"}
            )
            async with ClientSession() as client:
                for method, path, body in calls:
                    response = await client.request(method, f"{url}{path}", json=body, headers=headers)
                    statuses.append(response.status)
        finally:
            await server.stop()
        return statuses

    return asyncio.run(exercise())


def test_a_coding_agent_process_cannot_change_the_settings(tmp_path: Path, monkeypatch: Any) -> None:
    assert _guarded(tmp_path, monkeypatch, "claude", no_auth=False) == [403, 403]


def test_a_person_with_the_machine_token_can(tmp_path: Path, monkeypatch: Any) -> None:
    assert _guarded(tmp_path, monkeypatch, "", no_auth=False) == [200, 200]


AI_CALL = (("PUT", "/api/review-intelligence/settings", {"settings": {"intelligence.monthly_budget_usd": 0}}),)


def test_a_coding_agent_process_cannot_change_the_ai_settings(tmp_path: Path, monkeypatch: Any) -> None:
    assert _guarded(tmp_path, monkeypatch, "claude", no_auth=False, calls=AI_CALL) == [403]


def test_a_person_with_the_machine_token_can_change_the_ai_settings(tmp_path: Path, monkeypatch: Any) -> None:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)
    try:
        assert _guarded(tmp_path, monkeypatch, "", no_auth=False, calls=AI_CALL) == [200]
    finally:
        set_active_store_root(None)


BUDGET_CALLS = (
    ("PUT", "/api/review-intelligence/settings", {"settings": {"intelligence.monthly_budget_usd": 0}}),
    ("GET", "/api/review-intelligence", None),
)


def test_the_ai_budget_write_is_refused_for_a_coding_agent_and_changes_nothing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)
    try:
        assert _guarded(tmp_path, monkeypatch, "claude", no_auth=False, calls=BUDGET_CALLS) == [403, 200]
        assert not (root / "plugin_settings.json").exists(), "the refused write reached no file"
    finally:
        set_active_store_root(None)


def test_the_ai_budget_is_written_by_a_person_and_the_capability_answer_carries_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import json

    from lemoncrow_server_core import local_app

    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    monkeypatch.setattr(local_app, "peer_agent_host", lambda _transport: None)
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)

    async def exercise() -> list[Any]:
        server, _state = build_local_server(
            LocalServerConfig(
                listen_port=0, state_root=tmp_path / "state", local_no_auth=True, token_file=None, allow_local_fs=False
            )
        )
        url = await server.start()
        bodies: list[Any] = []
        try:
            async with ClientSession() as client:
                for method, path, body in (
                    *BUDGET_CALLS,
                    ("PUT", BUDGET_CALLS[0][1], {"settings": {"intelligence.monthly_budget_usd": None}}),
                ):
                    response = await client.request(method, f"{url}{path}", json=body)
                    bodies.append((response.status, await response.json(content_type=None)))
        finally:
            await server.stop()
        return bodies

    try:
        put, got, cleared = asyncio.run(exercise())
    finally:
        set_active_store_root(None)
    assert (
        put[0] == 200 and put[1]["ai_budget"]["may_run"] is False and put[1]["ai_budget"]["reason"] == "AI budget is $0"
    )
    assert (
        got[1]["ai_budget"]["monthly_budget_usd"] == 0 and got[1]["preferences"]["intelligence.monthly_budget_usd"] == 0
    )
    assert {"used_usd", "unreported_calls", "complete", "resets_on", "may_run", "reason"} <= set(got[1]["ai_budget"])
    assert cleared[1]["ai_budget"]["monthly_budget_usd"] is None and cleared[1]["ai_budget"]["may_run"] is True
    assert "intelligence.monthly_budget_usd" not in json.loads((root / "plugin_settings.json").read_text())
