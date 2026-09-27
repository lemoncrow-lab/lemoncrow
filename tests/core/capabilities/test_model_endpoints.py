"""``providers.json``'s first writer: merge-preserving, atomic, mode 0600.

Every provider path in the repo reads this file and nothing wrote it before
``lc model add``, so the properties the readers assume are only guaranteed by
these tests: a hand-added ``"anthropic"`` key survives a registration, the file
never loses its 0600 mode, and an ``--api-key-env`` registration puts the
variable *name* on disk and never the value.

No test here touches the network -- ``discover_endpoint_models`` is stubbed for
the whole module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities.providers.config import load_providers_config, providers_config_path
from lemoncrow.pro.capabilities.model_setup import endpoints as endpoints_mod
from lemoncrow.pro.capabilities.model_setup import transport as transport_mod
from lemoncrow.pro.capabilities.model_setup.endpoints import (
    add_endpoint,
    derive_endpoint_name,
    get_endpoint,
    list_endpoints,
    remove_endpoint,
    user_candidate_models,
    write_providers_config,
)
from lemoncrow.pro.capabilities.model_setup.transport import apply_custom_transport

_URL = "http://localhost:8000/v1"


@pytest.fixture(autouse=True)
def _stub_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration must never reach a real endpoint from the test suite."""

    def _fake(base_url: str, api_key: str, *, timeout: float = 10.0) -> list[str]:
        return ["Qwen3-Coder-30B", "llama-3.1-8b"]

    monkeypatch.setattr(endpoints_mod, "discover_endpoint_models", _fake)


@pytest.fixture(autouse=True)
def _no_active_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded ``--root`` is process state; never let it leak between tests."""

    monkeypatch.setattr(transport_mod, "_active_store_root", None)


@pytest.fixture
def ambient_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``default_store_root()`` at an empty dir for the root-routing tests.

    Without this a maintainer who genuinely has ``localhost-8000`` in their own
    ~/.lemoncrow/providers.json would watch these tests pass for the wrong reason.
    """

    ambient = tmp_path / "ambient-home"
    ambient.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(ambient))
    return ambient


def _config(root: Path) -> dict[str, Any]:
    return json.loads(providers_config_path(root).read_text(encoding="utf-8"))


def test_add_endpoint_creates_providers_json(tmp_path: Path) -> None:
    endpoint = add_endpoint(tmp_path, _URL)

    assert endpoint.name == "localhost-8000"
    path = providers_config_path(tmp_path)
    assert path.exists()
    assert _config(tmp_path)["custom"]["endpoints"]["localhost-8000"]["base_url"] == _URL


def test_add_endpoint_preserves_existing_keys(tmp_path: Path) -> None:
    path = providers_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"anthropic": {"api_key": "x"}, "_comment": "hand-edited"}), encoding="utf-8")

    add_endpoint(tmp_path, _URL)

    raw = _config(tmp_path)
    assert raw["anthropic"] == {"api_key": "x"}
    assert raw["_comment"] == "hand-edited"
    assert "localhost-8000" in raw["custom"]["endpoints"]


def test_second_add_preserves_the_first_endpoint(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL)
    add_endpoint(tmp_path, "http://gpu-box.lan:9000/v1")

    assert set(_config(tmp_path)["custom"]["endpoints"]) == {"localhost-8000", "gpu-box-lan-9000"}


def test_providers_json_is_mode_600(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL, api_key="sk-secret")

    assert providers_config_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_api_key_env_is_not_written_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_GATEWAY_KEY", "sk-super-secret-value")

    add_endpoint(tmp_path, _URL, api_key_env="MY_GATEWAY_KEY")

    text = providers_config_path(tmp_path).read_text(encoding="utf-8")
    assert "MY_GATEWAY_KEY" in text
    assert "sk-super-secret-value" not in text
    assert _config(tmp_path)["custom"]["endpoints"]["localhost-8000"]["api_key"] == ""


def test_endpoint_projection_carries_no_credential(tmp_path: Path) -> None:
    endpoint = add_endpoint(tmp_path, _URL, api_key="sk-secret")

    assert "sk-secret" not in json.dumps(endpoint.to_dict())
    assert not hasattr(endpoint, "api_key")


def test_is_configured_sees_custom_endpoint(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL)

    assert load_providers_config(tmp_path).is_configured("custom") is True


def test_is_configured_is_false_without_any_endpoint(tmp_path: Path) -> None:
    assert load_providers_config(tmp_path).is_configured("custom") is False


def test_configured_providers_includes_custom(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL)

    assert "custom" in load_providers_config(tmp_path).configured_providers()


def test_configured_providers_tolerates_a_non_dict_key(tmp_path: Path) -> None:
    """``_comment`` is a documented string entry in providers.json.example.

    ``configured_providers`` now iterates the file's keys as well as ``_ENV``,
    so a string-valued key must neither raise nor be reported as a provider.
    (Ambient env credentials may legitimately configure others.)
    """

    path = providers_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_comment": "copy me"}), encoding="utf-8")

    assert "_comment" not in load_providers_config(tmp_path).configured_providers()


def test_apply_custom_transport_rewrites_model_and_api_base(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL, models=["qwen"])
    cfg = load_providers_config(tmp_path)

    patched = apply_custom_transport({"model": "custom/localhost-8000/qwen", "messages": []}, cfg)

    assert patched["model"] == "openai/qwen"
    assert patched["api_base"] == _URL


def test_apply_custom_transport_passthrough_for_other_models(tmp_path: Path) -> None:
    original = {"model": "claude-opus-5", "messages": []}

    assert apply_custom_transport(original, load_providers_config(tmp_path)) is original


def test_apply_custom_transport_passthrough_for_unknown_endpoint(tmp_path: Path) -> None:
    """A removed endpoint must surface as the provider's error, not a traceback."""

    original = {"model": "custom/gone/qwen", "messages": []}

    assert apply_custom_transport(original, load_providers_config(tmp_path)) is original


def test_apply_custom_transport_reads_the_key_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_endpoint(tmp_path, _URL, api_key_env="MY_GATEWAY_KEY", models=["qwen"])
    monkeypatch.setenv("MY_GATEWAY_KEY", "sk-live")

    patched = apply_custom_transport({"model": "custom/localhost-8000/qwen"}, load_providers_config(tmp_path))

    assert patched["api_key"] == "sk-live"


def test_apply_custom_transport_keeps_a_slashed_raw_id(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL, models=["Qwen/Qwen3-Coder-30B"])

    patched = apply_custom_transport(
        {"model": "custom/localhost-8000/Qwen/Qwen3-Coder-30B"},
        load_providers_config(tmp_path),
    )

    assert patched["model"] == "openai/Qwen/Qwen3-Coder-30B"


def test_remove_endpoint_is_idempotent(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL)

    assert remove_endpoint(tmp_path, "localhost-8000") is True
    assert remove_endpoint(tmp_path, "localhost-8000") is False
    assert list_endpoints(tmp_path) == []


def test_remove_endpoint_keeps_other_providers(tmp_path: Path) -> None:
    path = providers_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"openai": {"api_key": "sk-1"}}), encoding="utf-8")
    add_endpoint(tmp_path, _URL)

    remove_endpoint(tmp_path, "localhost-8000")

    assert _config(tmp_path)["openai"] == {"api_key": "sk-1"}


def test_name_collision_raises(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL)

    with pytest.raises(ValueError, match="already registered"):
        add_endpoint(tmp_path, _URL)


def test_explicit_models_skip_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An endpoint without ``GET /models`` must still be registrable."""

    def _explode(*_: object, **__: object) -> list[str]:
        raise AssertionError("discovery must not run when --model is given")

    monkeypatch.setattr(endpoints_mod, "discover_endpoint_models", _explode)

    endpoint = add_endpoint(tmp_path, _URL, models=["only-this"])

    assert [m.raw_id for m in endpoint.models] == ["only-this"]


def test_unreachable_endpoint_raises_naming_the_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoints_mod, "discover_endpoint_models", lambda *a, **k: [])

    with pytest.raises(ValueError, match="localhost:8000"):
        add_endpoint(tmp_path, _URL)

    assert not providers_config_path(tmp_path).exists()


def test_registered_models_start_honestly_unknown(tmp_path: Path) -> None:
    """An unprobed model is not a capability-less one."""

    endpoint = add_endpoint(tmp_path, _URL, models=["qwen"], tier="high")
    model = endpoint.models[0]

    assert model.supports_tool_use is None
    assert model.supports_structured_output is None
    assert model.supports_vision is None
    assert model.probed_at is None
    assert model.tier == "high"


def test_context_window_override_is_persisted(tmp_path: Path) -> None:
    add_endpoint(tmp_path, _URL, models=["qwen"], context_window=131072)

    endpoint = get_endpoint(tmp_path, "localhost-8000")
    assert endpoint is not None
    assert endpoint.models[0].context_window == 131072


def test_model_ids_are_namespaced_by_endpoint(tmp_path: Path) -> None:
    endpoint = add_endpoint(tmp_path, _URL, models=["qwen"])

    assert endpoint.model_ids() == ("custom/localhost-8000/qwen",)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8000/v1", "localhost-8000"),
        ("https://GPU-Box.internal.example.com/v1", "gpu-box-internal-example-com"),
        ("localhost:11434", "localhost-11434"),
        ("http://user:pw@gw.example.com:443/v1", "gw-example-com-443"),
    ],
)
def test_endpoint_name_is_derived_from_host_and_port(url: str, expected: str) -> None:
    assert derive_endpoint_name(url) == expected


def test_unusable_url_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        add_endpoint(tmp_path, "   ")


def test_malformed_config_is_never_clobbered(tmp_path: Path) -> None:
    """Rewriting JSON we could not parse would delete the user's providers."""

    path = providers_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"anthropic": {"api_key": "x",,,', encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        add_endpoint(tmp_path, _URL)

    assert path.read_text(encoding="utf-8") == '{"anthropic": {"api_key": "x",,,'


def test_list_endpoints_degrades_to_empty_on_a_broken_file(tmp_path: Path) -> None:
    """Reporting never raises, even when writing would refuse."""

    path = providers_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all", encoding="utf-8")

    assert list_endpoints(tmp_path) == []
    assert get_endpoint(tmp_path, "anything") is None


def test_write_providers_config_sees_a_concurrent_write(tmp_path: Path) -> None:
    """The mutate callback runs against the file as it is *now*, not a snapshot."""

    path = providers_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"groq": {"api_key": "gsk"}}), encoding="utf-8")
    seen: list[dict[str, Any]] = []

    def _mutate(raw: dict[str, Any]) -> None:
        seen.append(dict(raw))
        raw["mistral"] = {"api_key": "m"}

    written = write_providers_config(tmp_path, _mutate)

    assert seen == [{"groq": {"api_key": "gsk"}}]
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "groq": {"api_key": "gsk"},
        "mistral": {"api_key": "m"},
    }


# ── `lc --root X` has to reach the readers that never see a root ─────────────
#
# Registration always carries an explicit root (the CLI hands it down), but the
# two consumers that matter cannot: ``apply_custom_transport`` is handed only a
# litellm request, and ``user_candidate_models`` is called argument-less from a
# cached routing table. Both used to fall through to ``default_store_root()``,
# so an endpoint written to ``--root X`` was then looked for in ~/.lemoncrow.


def test_dispatch_resolves_a_custom_model_against_the_active_root(tmp_path: Path, ambient_root: Path) -> None:
    """The litellm call site passes no config; it must still find ``--root X``."""

    alt = tmp_path / "alt-root"
    add_endpoint(alt, _URL, models=["qwen"])
    request = {"model": "custom/localhost-8000/qwen", "messages": []}

    # Nothing redirected the process yet: the ambient root has no endpoints.
    assert apply_custom_transport(dict(request)) == request

    transport_mod.set_active_store_root(alt)
    patched = apply_custom_transport(dict(request))

    assert patched["model"] == "openai/qwen"
    assert patched["api_base"] == _URL


def test_routing_candidates_come_from_the_active_root(tmp_path: Path, ambient_root: Path) -> None:
    """An endpoint registered under ``--root X`` has to be routable under it."""

    alt = tmp_path / "alt-root"
    add_endpoint(alt, _URL, models=["qwen"])

    assert user_candidate_models() == []

    transport_mod.set_active_store_root(alt)

    assert [candidate.model_id for candidate in user_candidate_models()] == ["custom/localhost-8000/qwen"]


def test_the_root_flag_records_the_store_root_for_dispatch(tmp_path: Path, ambient_root: Path) -> None:
    """`lc --root X` is the only thing that can tell those readers where X is.

    Driven through an explicit context rather than ``CliRunner`` so both halves
    are observable: the root is live *inside* the invocation and gone once it
    closes -- an in-process host must not inherit the previous run's root.
    """

    from lemoncrow.gateway.cli import cli

    alt = tmp_path / "alt-root"
    add_endpoint(alt, _URL, models=["qwen"])

    with cli.make_context("lc", ["--root", str(alt)]) as ctx:
        cli.invoke(ctx)
        assert transport_mod.active_store_root() == alt
        assert apply_custom_transport({"model": "custom/localhost-8000/qwen"})["api_base"] == _URL
        assert [candidate.model_id for candidate in user_candidate_models()] == ["custom/localhost-8000/qwen"]

    assert transport_mod.active_store_root() is None


def test_an_invocation_without_the_flag_leaves_the_ambient_root_alone(tmp_path: Path, ambient_root: Path) -> None:
    """No ``--root`` must stay a pure no-op -- an env var would redirect the world.

    ``default_store_root()`` backs the daemon, the licensing store and the OAuth
    token store too, so the recorded root exists only when the user asked for it.
    """

    from lemoncrow.gateway.cli import cli

    add_endpoint(ambient_root, _URL, models=["qwen"])

    with cli.make_context("lc", []) as ctx:
        cli.invoke(ctx)
        assert transport_mod.active_store_root() is None
        assert apply_custom_transport({"model": "custom/localhost-8000/qwen"})["api_base"] == _URL
