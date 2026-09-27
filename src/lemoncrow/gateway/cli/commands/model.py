"""``lc model`` -- bring your own model: register OpenAI-compatible endpoints.

A self-hosted vLLM box, an Ollama daemon, LM Studio, or a corporate gateway is
reachable in one command: LemonCrow asks the endpoint what it serves, writes
the answer into ``providers.json`` (mode 0600, merge-preserving), and from then
on the models route under ``custom/<endpoint>/<model>`` like any other vendor.

Registration then asks the endpoint what it can do (``lc model probe``, and
``lc model add`` unless ``--no-probe``), because the capability flags decide
routing and only the server can answer them truthfully. Everything that could
not be measured prints as ``?`` and is stored as null -- never as "no".

Credentials are the reason this module is careful. ``--api-key-env`` is the
documented path -- the variable *name* is stored and the value is read at
request time -- and nothing here ever echoes a key, in text or in ``--json``.

Heavy imports stay inside the callbacks so ``lc --help`` stays fast.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from lemoncrow.gateway.cli.commands._shared import _emit

if TYPE_CHECKING:  # `from __future__ import annotations` keeps this off the runtime path.
    from lemoncrow.pro.capabilities.model_setup.models import CustomEndpoint, ProbeResult

# Printed whenever anything came back unmeasured. "?" has to be readable as a
# gap in our knowledge, never as a capability the endpoint lacks.
_UNKNOWN_FOOTER = "? = not measured, not unsupported. Re-run `lc model probe ID` once the endpoint answers."

# A corporate gateway or an OpenRouter-style proxy can advertise hundreds of
# models, and `lc model add` registers all of them. Probing that many unasked
# is a multi-hour command, so past this count the user is asked first.
_PROBE_WITHOUT_ASKING = 8

# Requests probe_model() issues per model: three chat completions plus one GET.
_REQUESTS_PER_PROBE = 4


@click.group("model")
def model_group() -> None:
    """Bring your own model: register and probe OpenAI-compatible endpoints."""


@model_group.command("add")
@click.argument("endpoint")
@click.option("--name", default=None, help="Endpoint alias. Default: derived from host and port.")
@click.option(
    "--api-key-env",
    default=None,
    help="Env var holding the key. Preferred -- nothing is written to disk.",
)
@click.option("--api-key", default=None, help="Literal key. Stored in providers.json (mode 0600).")
@click.option("--model", "model_filter", multiple=True, help="Only register these model ids. Repeatable.")
@click.option(
    "--tier",
    type=click.Choice(["cheap", "high"]),
    default="cheap",
    show_default=True,
    help="Routing tier to register the models under.",
)
@click.option("--context-window", type=int, default=None, help="Override the reported context limit.")
@click.option("--no-probe", is_flag=True, help="Register without running the capability probe.")
@click.option("--probe-vision", is_flag=True, help="Also probe vision (sends a tiny inline test image).")
@click.option(
    "--timeout",
    default=30.0,
    show_default=True,
    type=float,
    help="Per-request timeout for discovery and probing (s).",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def model_add_cmd(
    ctx: click.Context,
    endpoint: str,
    name: str | None,
    api_key_env: str | None,
    api_key: str | None,
    model_filter: tuple[str, ...],
    tier: str,
    context_window: int | None,
    no_probe: bool,
    probe_vision: bool,
    timeout: float,
    as_json: bool,
) -> None:
    """Register an OpenAI-compatible endpoint (vLLM, Ollama, LM Studio, internal gateway)."""

    from lemoncrow.pro.capabilities.model_setup.endpoints import add_endpoint
    from lemoncrow.pro.capabilities.model_setup.models import PROBE_SCHEMA_VERSION

    root = _root(ctx)
    try:
        registered = add_endpoint(
            root,
            endpoint,
            name=name,
            api_key=api_key,
            api_key_env=api_key_env,
            models=list(model_filter) or None,
            tier="high" if tier == "high" else "cheap",
            context_window=context_window,
            timeout=timeout,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if api_key:
        # Warn on stderr so `--json` stays machine-parseable, and name the file
        # the secret just landed in.
        click.echo(
            f"warning: the literal key is stored in {_config_path(root)} (mode 0600). "
            "Prefer --api-key-env NAME, which writes only the variable name.",
            err=True,
        )

    probes: list[ProbeResult] = []
    probed = not no_probe and _confirm_bulk_probe(registered)
    if probed:
        probes = _probe_all(
            root,
            registered,
            probe_vision=probe_vision,
            timeout=timeout,
            context_window=context_window,
        )
        registered = _reload(root, registered)

    if as_json:
        payload: dict[str, Any] = {
            "schema_version": PROBE_SCHEMA_VERSION,
            "name": registered.name,
            "endpoint": registered.base_url,
            "api_key_env": registered.api_key_env,
            "models": [model.raw_id for model in registered.models],
            "model_ids": list(registered.model_ids()),
            "config_path": str(_config_path(root)),
            "probed": probed,
            "probes": [probe.to_dict() for probe in probes],
        }
        _emit(payload, as_json=True)
        return

    lines = [f"registered {registered.name}  ->  {registered.base_url}"]
    if not probed:
        lines.extend(f"  {model_id}" for model_id in registered.model_ids())
    else:
        lines.extend(_capability_lines(registered))
    lines.append(f"\nwritten to {_config_path(root)} (mode 0600)")
    if registered.api_key_env:
        lines.append(f"key read at request time from ${registered.api_key_env}")
    if not probed:
        lines.append("not probed. `lc model probe ID` measures what each model actually supports.")
    elif any(not probe.ok for probe in probes):
        lines.append(_UNKNOWN_FOOTER)
    _emit("\n".join(lines), as_json=False)


@model_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def model_list_cmd(ctx: click.Context, as_json: bool) -> None:
    """List registered custom endpoints and their probed capabilities."""

    from lemoncrow.pro.capabilities.model_setup.endpoints import list_endpoints
    from lemoncrow.pro.capabilities.model_setup.models import PROBE_SCHEMA_VERSION

    root = _root(ctx)
    endpoints = list_endpoints(root)

    if as_json:
        _emit(
            {
                "schema_version": PROBE_SCHEMA_VERSION,
                "config_path": str(_config_path(root)),
                "endpoints": [ep.to_dict() for ep in endpoints],
            },
            as_json=True,
        )
        return

    if not endpoints:
        _emit(
            "no custom endpoints registered.\n  lc model add http://localhost:8000/v1 --api-key-env MY_GATEWAY_KEY",
            as_json=False,
        )
        return
    _emit("\n".join(_render_endpoints(endpoints)), as_json=False)


@model_group.command("probe")
@click.argument("model_id")
@click.option("--probe-vision", is_flag=True, help="Also probe vision (sends a tiny inline test image).")
@click.option("--timeout", default=30.0, show_default=True, type=float, help="Per-request probe timeout (s).")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def model_probe_cmd(ctx: click.Context, model_id: str, probe_vision: bool, timeout: float, as_json: bool) -> None:
    """Re-probe one registered model and persist the result."""

    from lemoncrow.pro.capabilities.model_setup.endpoints import get_endpoint, record_probe
    from lemoncrow.pro.capabilities.model_setup.models import split_custom_model_id
    from lemoncrow.pro.capabilities.model_setup.probe import probe_model

    root = _root(ctx)
    parts = split_custom_model_id(model_id)
    if parts is None:
        raise click.ClickException(
            f"{model_id!r} is not a registered model id -- expected custom/<endpoint>/<model>. "
            "`lc model list` prints the registered ids."
        )
    endpoint_name, raw_id = parts
    endpoint = get_endpoint(root, endpoint_name)
    if endpoint is None:
        raise click.ClickException(
            f"no endpoint named {endpoint_name!r} is registered. Register it first: `lc model add URL`."
        )
    if raw_id not in {model.raw_id for model in endpoint.models}:
        raise click.ClickException(
            f"{raw_id!r} is not registered under {endpoint_name!r}. `lc model list` prints what is."
        )

    result = probe_model(
        endpoint.base_url,
        raw_id,
        endpoint_name=endpoint_name,
        api_key=_api_key(root, endpoint_name),
        probe_vision=probe_vision,
        timeout=timeout,
    )
    # Persist even a failed probe: "asked 10 minutes ago, still unknown" is a
    # different fact from "never asked", and only the timestamp carries it.
    record_probe(root, result)

    if as_json:
        _emit(result.to_dict(), as_json=True)
    else:
        _emit("\n".join(_render_probe(result)), as_json=False)
    if not result.ok:
        # The report is on stdout either way; the exit code is what a script or
        # a `&&` chain reads. Deliberate, message already printed.
        ctx.exit(1)


@model_group.command("remove")
@click.argument("name")
@click.option("-f", "--force", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def model_remove_cmd(ctx: click.Context, name: str, force: bool) -> None:
    """Remove a registered endpoint and its models."""

    from lemoncrow.pro.capabilities.model_setup.endpoints import remove_endpoint

    root = _root(ctx)
    if not force:
        click.confirm(f"Remove endpoint {name!r} and every model registered under it?", abort=True)
    if remove_endpoint(root, name):
        _emit(f"removed {name}", as_json=False)
        return
    _emit(f"no endpoint named {name!r} is registered", as_json=False)


def _root(ctx: click.Context) -> Path:
    """The store root. ``lc model --help`` must work without a seeded context."""

    from lemoncrow.core.foundation.paths import default_store_root

    obj = ctx.obj or {}
    return Path(obj.get("root") or default_store_root())


def _config_path(root: Path) -> Path:
    from lemoncrow.core.capabilities.providers.config import providers_config_path

    return providers_config_path(root)


def _api_key(root: Path, endpoint_name: str) -> str:
    """The credential for one endpoint, resolved from *this* root's config.

    Reads the env var named by ``--api-key-env`` at call time, which is why the
    value is fetched here rather than carried on ``CustomEndpoint``.
    """

    from lemoncrow.core.capabilities.providers.config import load_providers_config
    from lemoncrow.pro.capabilities.model_setup.transport import endpoint_entry, resolve_endpoint_api_key

    return resolve_endpoint_api_key(endpoint_entry(endpoint_name, load_providers_config(root)) or {})


def _interactive() -> bool:
    """Whether there is a human on stdin to answer a prompt."""

    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _confirm_bulk_probe(endpoint: CustomEndpoint) -> bool:
    """Whether to probe every model that was just registered.

    At or below ``_PROBE_WITHOUT_ASKING`` this is a no-op. Above it the cost is
    stated first, because a gateway advertising 200 models turns registration
    into ~800 sequential requests -- tens of minutes against a healthy gateway,
    and ``--timeout`` per model against a throttled one. A non-interactive run
    (a script, a pipe, CI) has nobody to ask, so it registers *without* probing
    rather than spending an unannounced half hour. Either way the endpoint and
    its models are registered; only the measurement is deferred.
    """

    count = len(endpoint.models)
    if count <= _PROBE_WITHOUT_ASKING:
        return True
    cost = (
        f"{count} models registered -- probing them all sends about "
        f"{count * _REQUESTS_PER_PROBE} requests, one at a time"
    )
    if not _interactive():
        # stderr, so `--json` on stdout stays machine-parseable.
        click.echo(
            # Not "re-run with --model": the endpoint is registered by the time
            # this prints, and a second `lc model add` for the same URL is
            # refused as a duplicate name. `lc model probe` is the way back in.
            f"{cost}. Nothing is attached to answer a prompt, so they were registered without probing. "
            "Run `lc model probe ID` for the ones you need.",
            err=True,
        )
        return False
    return bool(click.confirm(f"{cost}. Probe all {count} now?", default=False, err=True))


def _probe_all(
    root: Path,
    endpoint: CustomEndpoint,
    *,
    probe_vision: bool,
    timeout: float,
    context_window: int | None,
) -> list[ProbeResult]:
    """Probe every model just registered, persisting each result as it lands.

    Sequential on purpose: these are usually one box serving one GPU, and firing
    four concurrent completions at it would measure the queue, not the model.
    Which is why it reports progress: a silent multi-minute loop is
    indistinguishable from a hang, and the user cannot tell what a Ctrl-C costs.
    """

    from lemoncrow.pro.capabilities.model_setup.endpoints import record_probe
    from lemoncrow.pro.capabilities.model_setup.probe import probe_model

    api_key = _api_key(root, endpoint.name)
    results: list[ProbeResult] = []
    total = len(endpoint.models)
    for index, model in enumerate(endpoint.models, start=1):
        if total > 1:
            # stderr, so `--json` on stdout stays machine-parseable.
            click.echo(f"probing {index}/{total}  {endpoint.name}/{model.raw_id}", err=True)
        result = probe_model(
            endpoint.base_url,
            model.raw_id,
            endpoint_name=endpoint.name,
            api_key=api_key,
            probe_vision=probe_vision,
            timeout=timeout,
            context_window=context_window,
        )
        record_probe(root, result)
        results.append(result)
    return results


def _reload(root: Path, endpoint: CustomEndpoint) -> CustomEndpoint:
    """Re-read after probing so the printed capabilities are the persisted ones."""

    from lemoncrow.pro.capabilities.model_setup.endpoints import get_endpoint

    return get_endpoint(root, endpoint.name) or endpoint


def _render_endpoints(endpoints: list[CustomEndpoint]) -> list[str]:
    """Plain-text listing: one header per endpoint, one line per model.

    ``?`` is printed for every capability nothing has measured yet -- an
    unprobed model must not read as an unsupported one.
    """

    lines: list[str] = []
    for endpoint in endpoints:
        key_note = f"  key=${endpoint.api_key_env}" if endpoint.api_key_env else ""
        lines.append(f"{endpoint.name}  {endpoint.base_url}{key_note}")
        if not endpoint.models:
            lines.append("  (no models registered)")
        for model in endpoint.models:
            lines.append(
                f"  {model.raw_id}"
                f"  tier={model.tier}"
                f"  tools={_flag(model.supports_tool_use)}"
                f"  json={_flag(model.supports_structured_output)}"
                f"  vision={_flag(model.supports_vision)}"
                f"  ctx={model.context_window if model.context_window else '?'}"
                f"  probed={model.probed_at or 'never'}"
            )
    return lines


def _capability_lines(endpoint: CustomEndpoint) -> list[str]:
    """One line per model, keyed by the id you would actually route to."""

    lines: list[str] = []
    for model, model_id in zip(endpoint.models, endpoint.model_ids(), strict=True):
        rate = f"{model.observed_tokens_per_second:.1f}" if model.observed_tokens_per_second else "?"
        lines.append(
            f"  {model_id}"
            f"  tools={_flag(model.supports_tool_use)}"
            f"  json={_flag(model.supports_structured_output)}"
            f"  vision={_flag(model.supports_vision)}"
            f"  ctx={model.context_window if model.context_window else '?'}"
            f"  tok/s={rate}"
        )
    return lines


def _render_probe(result: ProbeResult) -> list[str]:
    """The check table, then the summary. Statuses are printed verbatim.

    ``unknown`` is never rewritten as ``no``: the detail column says why the
    answer is missing, and that is the only thing separating a model without
    tool support from an endpoint that was asleep.
    """

    lines = [f"{result.model_id}  @  {result.endpoint}"]
    for check in result.checks:
        lines.append(f"  {check.name:<19}{check.status:<13}{check.detail}")
    lines.append("")
    rate = f"{result.observed_tokens_per_second:.1f}" if result.observed_tokens_per_second else "?"
    lines.append(
        f"tools={_flag(result.supports_tool_use)}"
        f"  json={_flag(result.supports_structured_output)}"
        f"  vision={_flag(result.supports_vision)}"
        f"  ctx={result.context_window if result.context_window else '?'}"
        f"  tok/s={rate}"
    )
    lines.append(f"probed at {result.probed_at}")
    if not result.ok:
        lines.append(_UNKNOWN_FOOTER)
    return lines


def _flag(value: bool | None) -> str:
    if value is None:
        return "?"
    return "yes" if value else "no"


__all__ = ["model_group"]
