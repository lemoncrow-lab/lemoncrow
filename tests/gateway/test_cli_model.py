"""``lc model`` CLI surface: registration, probing, JSON shape, and clean failure.

The failure paths carry the weight here. ``lc model add`` is the first thing a
user runs against a box that may not be up yet, so an unreachable endpoint has
to produce one sentence naming the URL -- not a traceback -- and no credential
may ever appear in the output.

The probe half adds one more rule: an endpoint that did not answer must render
as ``?``. A CLI that printed ``tools=no`` for a box that was merely asleep would
teach the user something false and quietly demote the model in routing.

Model discovery and the probe are monkeypatched in every test: nothing here
touches a network except the one test that deliberately dials a closed port.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import model as model_cmd
from lemoncrow.pro.capabilities.model_setup import endpoints as endpoints_mod
from lemoncrow.pro.capabilities.model_setup import probe as probe_mod
from lemoncrow.pro.capabilities.model_setup.models import ProbeCheck, ProbeResult

_URL = "http://localhost:8000/v1"
_DISCOVERED = ["Qwen3-Coder-30B", "llama-3.1-8b", "nomic-embed-text"]
_PROBED_AT = "2026-09-08T10:11:12+00:00"
_REAL_PROBE_MODEL = probe_mod.probe_model


def _fake_probe(
    base_url: str,
    raw_model_id: str,
    *,
    endpoint_name: str,
    api_key: str = "",
    probe_vision: bool = False,
    timeout: float = 30.0,
    context_window: int | None = None,
) -> ProbeResult:
    """A server that supports tools and JSON, and was never asked about vision."""

    return ProbeResult(
        schema_version=1,
        endpoint=base_url,
        endpoint_name=endpoint_name,
        raw_model_id=raw_model_id,
        model_id=f"custom/{endpoint_name}/{raw_model_id}",
        checks=(
            ProbeCheck(name="chat_completion", status="ok", detail="HTTP 200 in 12 ms", value=12.0),
            ProbeCheck(name="vision", status="unknown", detail="not probed"),
        ),
        context_window=context_window or 131072,
        supports_tool_use=True,
        supports_structured_output=True,
        supports_vision=None,
        observed_tokens_per_second=42.0,
        probed_at=_PROBED_AT,
        ok=True,
    )


@pytest.fixture(autouse=True)
def _stub_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoints_mod, "discover_endpoint_models", lambda *a, **k: list(_DISCOVERED))
    monkeypatch.setattr(probe_mod, "probe_model", _fake_probe)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ctx() -> click.Context:
    return click.Context(cli, info_name="lc")


def _run(root: Path, *args: str, stdin: str | None = None) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), "model", *args], input=stdin)


def test_model_registered_and_help_exits_zero() -> None:
    ctx = _ctx()
    assert "model" in set(cli.list_commands(ctx))
    group = cli.get_command(ctx, "model")
    assert isinstance(group, click.Group)

    sub_ctx = click.Context(group, info_name="model")
    assert {"add", "list", "probe", "remove"} <= set(group.list_commands(sub_ctx))

    runner = CliRunner()
    for args in (["model", "--help"], ["model", "add", "--help"], ["model", "list", "--help"]):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, f"`lc {' '.join(args)}` failed:\n{result.output}"


def test_model_add_lists_discovered_models(tmp_path: Path) -> None:
    result = _run(tmp_path, "add", _URL)

    assert result.exit_code == 0, result.output
    for model_id in _DISCOVERED:
        assert model_id in result.output
    assert "localhost-8000" in result.output


def test_model_add_json_shape(tmp_path: Path) -> None:
    result = _run(tmp_path, "add", _URL, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "localhost-8000"
    assert payload["endpoint"] == _URL
    assert payload["models"] == _DISCOVERED
    assert payload["schema_version"] == 1
    assert payload["model_ids"][0] == "custom/localhost-8000/Qwen3-Coder-30B"


def test_model_add_unreachable_endpoint_errors_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoints_mod, "discover_endpoint_models", lambda *a, **k: [])

    result = _run(tmp_path, "add", _URL)

    assert result.exit_code != 0
    assert "localhost:8000" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_model_add_json_never_echoes_the_api_key(tmp_path: Path) -> None:
    result = _run(tmp_path, "add", _URL, "--api-key", "sk-super-secret", "--json")

    assert result.exit_code == 0, result.output
    assert "sk-super-secret" not in result.output
    payload = json.loads(result.stdout)
    assert "api_key" not in payload


def test_model_add_with_literal_key_warns_about_the_file(tmp_path: Path) -> None:
    result = _run(tmp_path, "add", _URL, "--api-key", "sk-1")

    assert result.exit_code == 0, result.output
    assert "providers.json" in result.stderr
    assert "--api-key-env" in result.stderr
    assert "sk-1" not in result.stderr


def test_model_add_records_the_env_var_name_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_GATEWAY_KEY", "sk-live-value")

    result = _run(tmp_path, "add", _URL, "--api-key-env", "MY_GATEWAY_KEY", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["api_key_env"] == "MY_GATEWAY_KEY"
    assert "sk-live-value" not in (tmp_path / "providers.json").read_text(encoding="utf-8")


def test_model_list_and_remove_roundtrip(tmp_path: Path) -> None:
    assert _run(tmp_path, "add", _URL, "--model", "qwen", "--tier", "high").exit_code == 0

    listed = _run(tmp_path, "list")
    assert listed.exit_code == 0, listed.output
    assert "localhost-8000" in listed.output
    assert "tier=high" in listed.output

    removed = _run(tmp_path, "remove", "localhost-8000", "--force")
    assert removed.exit_code == 0, removed.output
    assert "removed localhost-8000" in removed.output

    after = _run(tmp_path, "list")
    assert after.exit_code == 0
    assert "no custom endpoints registered" in after.output


def test_model_list_json_is_empty_on_a_fresh_root(tmp_path: Path) -> None:
    result = _run(tmp_path, "list", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["endpoints"] == []
    assert payload["schema_version"] == 1


def test_model_list_reports_unprobed_capabilities_as_unknown(tmp_path: Path) -> None:
    _run(tmp_path, "add", _URL, "--model", "qwen", "--no-probe")

    result = _run(tmp_path, "list")

    assert "tools=?" in result.output
    assert "probed=never" in result.output


def test_model_remove_unknown_endpoint_is_clean(tmp_path: Path) -> None:
    result = _run(tmp_path, "remove", "nope", "--force")

    assert result.exit_code == 0, result.output
    assert "no endpoint named" in result.output


def test_model_add_probes_by_default_and_persists_the_result(tmp_path: Path) -> None:
    assert _run(tmp_path, "add", _URL, "--model", "qwen").exit_code == 0

    listed = _run(tmp_path, "list")

    assert "tools=yes" in listed.output
    assert "ctx=131072" in listed.output
    assert _PROBED_AT in listed.output


def test_model_add_no_probe_registers_without_measuring(tmp_path: Path) -> None:
    result = _run(tmp_path, "add", _URL, "--model", "qwen", "--no-probe")

    assert result.exit_code == 0, result.output
    assert "not probed" in result.output
    assert "probed=never" in _run(tmp_path, "list").output


def test_model_add_json_carries_the_probes(tmp_path: Path) -> None:
    result = _run(tmp_path, "add", _URL, "--model", "qwen", "--json")

    payload = json.loads(result.stdout)
    assert payload["probed"] is True
    assert payload["probes"][0]["model_id"] == "custom/localhost-8000/qwen"
    assert payload["probes"][0]["supports_vision"] is None


def test_model_add_context_window_override_reaches_the_probe(tmp_path: Path) -> None:
    _run(tmp_path, "add", _URL, "--model", "qwen", "--context-window", "32000")

    assert "ctx=32000" in _run(tmp_path, "list").output


def test_model_probe_persists_and_reports(tmp_path: Path) -> None:
    _run(tmp_path, "add", _URL, "--model", "qwen", "--no-probe")

    result = _run(tmp_path, "probe", "custom/localhost-8000/qwen")

    assert result.exit_code == 0, result.output
    assert "chat_completion" in result.output
    assert "tools=yes" in result.output
    assert "tools=yes" in _run(tmp_path, "list").output


def test_model_probe_prints_unknown_as_a_gap_not_a_capability(tmp_path: Path) -> None:
    _run(tmp_path, "add", _URL, "--model", "qwen", "--no-probe")

    result = _run(tmp_path, "probe", "custom/localhost-8000/qwen")

    assert "vision=?" in result.output
    assert "vision=no" not in result.output


def test_model_probe_json_carries_schema_version(tmp_path: Path) -> None:
    _run(tmp_path, "add", _URL, "--model", "qwen", "--no-probe")

    result = _run(tmp_path, "probe", "custom/localhost-8000/qwen", "--json")

    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["supports_tool_use"] is True
    assert payload["checks"][0]["name"] == "chat_completion"


def test_model_probe_rejects_an_id_that_is_not_ours(tmp_path: Path) -> None:
    result = _run(tmp_path, "probe", "gpt-4o")

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "custom/<endpoint>/<model>" in result.output


def test_model_probe_unknown_endpoint_errors_cleanly(tmp_path: Path) -> None:
    result = _run(tmp_path, "probe", "custom/ghost/qwen")

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "no endpoint named" in result.output


def test_model_probe_unknown_model_errors_cleanly(tmp_path: Path) -> None:
    _run(tmp_path, "add", _URL, "--model", "qwen", "--no-probe")

    result = _run(tmp_path, "probe", "custom/localhost-8000/other")

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "is not registered under" in result.output


def test_model_probe_json_stays_parseable_when_the_endpoint_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed probe still has to be machine-readable -- that is how it gets reported."""

    monkeypatch.setattr(probe_mod, "probe_model", _REAL_PROBE_MODEL)
    port = _closed_port()
    _run(tmp_path, "add", f"http://127.0.0.1:{port}/v1", "--name", "dark", "--model", "qwen", "--no-probe")

    result = _run(tmp_path, "probe", "custom/dark/qwen", "--timeout", "1", "--json")

    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["supports_tool_use"] is None
    assert payload["schema_version"] == 1
    assert "Traceback" not in result.output


def test_model_probe_against_a_refused_connection_reports_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real probe, against a real refused connection: no hang, no traceback, no guess."""

    monkeypatch.setattr(probe_mod, "probe_model", _REAL_PROBE_MODEL)
    port = _closed_port()
    _run(tmp_path, "add", f"http://127.0.0.1:{port}/v1", "--name", "down", "--model", "qwen", "--no-probe")

    result = _run(tmp_path, "probe", "custom/down/qwen", "--timeout", "1")

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "tools=?" in result.output
    assert "not measured" in result.output
    # The failed attempt is still recorded: "asked, still unknown" is not
    # "never asked".
    listed = _run(tmp_path, "list")
    assert "tools=?" in listed.output
    assert "probed=never" not in listed.output


# ---------------------------------------------------------------------------
# Bulk probing
#
# A LiteLLM/OpenRouter-style gateway advertises hundreds of models and `lc
# model add` registers all of them. Probing each one costs three chat
# completions plus a GET, issued sequentially, so the unbounded loop was a
# ~20-minute command that printed nothing and had no way to say no.
# ---------------------------------------------------------------------------

_MANY = [f"gateway-model-{index:03d}" for index in range(40)]


@pytest.fixture
def _big_gateway(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A gateway advertising 40 models, with every probe counted."""

    probed: list[str] = []

    def _counting_probe(base_url: str, raw_model_id: str, **kwargs: object) -> ProbeResult:
        probed.append(raw_model_id)
        return _fake_probe(base_url, raw_model_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(endpoints_mod, "discover_endpoint_models", lambda *a, **k: list(_MANY))
    monkeypatch.setattr(probe_mod, "probe_model", _counting_probe)
    return probed


def test_a_large_discovery_list_is_not_probed_without_being_asked(tmp_path: Path, _big_gateway: list[str]) -> None:
    result = _run(tmp_path, "add", _URL, "--json")

    assert result.exit_code == 0, result.output
    assert _big_gateway == []
    payload = json.loads(result.stdout)
    assert payload["probed"] is False
    assert payload["probes"] == []
    # Registration still happened -- only the measurement was deferred.
    assert payload["models"] == _MANY
    assert "160 requests" in result.stderr
    assert "lc model probe" in result.stderr


def test_declining_the_bulk_probe_still_registers_every_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _big_gateway: list[str]
) -> None:
    monkeypatch.setattr(model_cmd, "_interactive", lambda: True)

    result = _run(tmp_path, "add", _URL, stdin="n\n")

    assert result.exit_code == 0, result.output
    assert _big_gateway == []
    assert "Probe all 40 now?" in result.stderr
    assert "not probed" in result.stdout
    assert "custom/localhost-8000/gateway-model-000" in result.stdout


def test_accepting_the_bulk_probe_probes_them_all_and_says_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _big_gateway: list[str]
) -> None:
    monkeypatch.setattr(model_cmd, "_interactive", lambda: True)

    result = _run(tmp_path, "add", _URL, stdin="y\n")

    assert result.exit_code == 0, result.output
    assert _big_gateway == _MANY
    assert "probing 1/40  localhost-8000/gateway-model-000" in result.stderr
    assert "probing 40/40  localhost-8000/gateway-model-039" in result.stderr


def test_a_short_discovery_list_is_probed_without_asking_anything(tmp_path: Path) -> None:
    """The single-box case -- the reason `lc model add` probes at all -- is untouched."""

    result = _run(tmp_path, "add", _URL, "--json")

    payload = json.loads(result.stdout)
    assert payload["probed"] is True
    assert len(payload["probes"]) == len(_DISCOVERED)
    assert "Probe all" not in result.stderr


def test_model_add_against_a_refused_connection_still_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed probe is not a failed registration -- the endpoint may just be down."""

    monkeypatch.setattr(probe_mod, "probe_model", _REAL_PROBE_MODEL)
    result = _run(
        tmp_path,
        "add",
        f"http://127.0.0.1:{_closed_port()}/v1",
        "--model",
        "qwen",
        "--timeout",
        "1",
    )

    assert result.exit_code == 0, result.output
    assert "tools=?" in result.output
    assert "not measured" in result.output
