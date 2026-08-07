"""Launch mature coding CLIs as thin frontends over LemonCrow's owned runtime."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import click

from lemoncrow.core.capabilities.statusline_sidecar import STATUS_FILE_ENV, status_file_path

EngineName = Literal["auto", "lemoncode", "codex", "claude", "native"]
_UPSTREAM_ENGINES = ("lemoncode", "codex", "claude")


@dataclass(frozen=True)
class EngineLaunch:
    """Resolved upstream process invocation."""

    engine: str
    command: tuple[str, ...]
    env: dict[str, str]


def _provision_lemoncode_host(store_root: Path) -> str:
    """Return the LemonCode host binary, downloading it if it is not present yet.

    LemonCode is the host this project ships, so asking for it is taken as
    permission to install it. Third-party engines are never auto-installed.
    """
    from lemoncrow.gateway.cli.lemoncode_host import install_host_release, resolve_host_binary

    executable = resolve_host_binary(store_root)
    if executable is not None:
        return executable
    click.echo("  LemonCode host not found - installing it now...", err=True)
    try:
        installed = install_host_release(store_root)
    except click.ClickException as exc:
        raise click.ClickException(
            f"LemonCode host is not installed and could not be downloaded ({exc.format_message()}); "
            "run `lc code host install` or build it with `lc code host build --source opencode`"
        ) from exc
    click.echo(f"  ✓ LemonCode host installed: {installed}", err=True)
    return str(installed)


def _resolve_engine(requested: EngineName, *, store_root: Path) -> tuple[str, str | None]:
    normalized = requested.strip().lower()
    if normalized == "opencode":
        normalized = "lemoncode"
    if normalized == "native":
        return "native", None

    from lemoncrow.gateway.cli.lemoncode_host import resolve_host_binary

    if normalized != "auto":
        if normalized == "lemoncode":
            return normalized, _provision_lemoncode_host(store_root)
        executable = shutil.which(normalized)
        if executable is None:
            raise click.ClickException(f"{normalized} is not installed; install it or use --engine native")
        return normalized, executable

    for engine in _UPSTREAM_ENGINES:
        executable = resolve_host_binary(store_root) if engine == "lemoncode" else shutil.which(engine)
        if executable is not None:
            return engine, executable
    return "native", None


def _output_ceiling(budget: str) -> int:
    return {"cheap": 3600, "balanced": 5200, "best": 7600}.get(budget, 5200)


def _picker_model_entries(store_root: Path) -> list[tuple[str, str, str]]:
    """Real, currently-runnable (litellm model id, provider, raw model) triples.

    Best-effort: any failure here (no configured provider, pro module
    unavailable, catalog error) just yields nothing -- the picker still has
    "Auto" and launch must never fail because of this.
    """
    try:
        from lemoncrow.gateway.cli.commands.run import _resolve_litellm_model
        from lemoncrow.pro.capabilities.owned_execution_routing import (
            OwnedExecutionRouteSelector,
            OwnedRouteRequest,
        )

        catalog = OwnedExecutionRouteSelector(store_root).catalog(
            OwnedRouteRequest(tool_name="edit", task_text="", mode="auto", budget="balanced")
        )
    except Exception:
        return []

    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in catalog:
        for tier, raw_model in (("cheap", item.cheap_model), ("high", item.high_model)):
            resolved = _resolve_litellm_model(item.provider, raw_model)
            if resolved in seen:
                continue
            seen.add(resolved)
            suffix = "" if item.cheap_model == item.high_model else f" ({tier})"
            display = raw_model.split("/", 1)[-1] if "/" in raw_model else raw_model
            entries.append((resolved, item.provider, f"{display}{suffix}"))
    return entries


def _picker_models(store_root: Path, output_ceiling: int) -> dict[str, dict[str, Any]]:
    """opencode.json `provider.lc.models`.

    "Auto" plus every real, currently runnable provider/model pair, so the
    host's own model picker can switch mid-session instead of only ever
    offering the single "Auto" placeholder.
    """
    limit = {"context": 200_000, "output": output_ceiling}
    models: dict[str, dict[str, Any]] = {
        "lemoncrow": {
            # Distinct from the provider name: the frontend status line
            # renders "<provider> <model>".
            "name": "Auto",
            "limit": limit,
        }
    }
    for resolved_model, provider, label in _picker_model_entries(store_root):
        models[resolved_model] = {"name": f"{provider} · {label}", "limit": limit}
    return models


def _build_engine_launch(
    *,
    engine: str,
    executable: str,
    base_url: str,
    token: str,
    store_root: Path,
    project_root: Path,
    empty_mcp_config: Path,
    budget: str,
    prompt: str | None,
    resume: str | None,
    status_file: Path | None = None,
    base_env: dict[str, str] | None = None,
) -> EngineLaunch:
    """Build a shell-free invocation for one supported upstream CLI."""
    env = dict(base_env or os.environ)
    env["LEMONCROW_GATEWAY_TOKEN"] = token
    env["NO_PROXY"] = _no_proxy_with_loopback()
    env["no_proxy"] = env["NO_PROXY"]
    if status_file is not None:
        env[STATUS_FILE_ENV] = str(status_file)
    output_ceiling = _output_ceiling(budget)

    if engine in {"lemoncode", "opencode"}:
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": "lc/lemoncrow",
            "default_agent": "build",
            "enabled_providers": ["lc"],
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "compaction": {"auto": False, "prune": True},
            "provider": {
                "lc": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "LemonCode",
                    "options": {
                        "baseURL": f"{base_url}/v1",
                        "apiKey": token,
                    },
                    "models": _picker_models(store_root, output_ceiling),
                }
            },
        }
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, separators=(",", ":"))
        env["LEMONCODE_MANAGED"] = "1"
        env["LEMONCODE_STRIP_HOST_PROMPT"] = "1"
        env["LEMONCODE_STRIP_HOST_TOOLS"] = "1"
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        env["OPENCODE_DISABLE_AUTOCOMPACT"] = "true"
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
        env["OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER"] = "true"
        env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = str(output_ceiling)
        command = [
            executable,
            "--pure",
            "--model",
            "lc/lemoncrow",
            "--agent",
            "build",
        ]
        if prompt is not None:
            command.append("run")
            if resume:
                command.extend(["--session", resume])
            command.append(prompt)
        elif resume:
            command.extend(["--session", resume])
        return EngineLaunch(engine, tuple(command), env)

    if engine == "codex":
        provider_table = (
            '{ name = "LemonCrow", '
            f'base_url = "{base_url}/v1", '
            'env_key = "LEMONCROW_GATEWAY_TOKEN", wire_api = "responses" }'
        )
        common = [
            executable,
            "-C",
            str(project_root),
            "-m",
            "lemoncrow",
            "-c",
            'model_provider="lemoncrow"',
            "-c",
            f"model_providers.lemoncrow={provider_table}",
            "-a",
            "never",
            "-s",
            "read-only",
        ]
        if prompt is not None:
            if resume:
                command = [*common, "exec", "resume", resume, prompt]
            else:
                command = [*common, "exec", "--skip-git-repo-check", prompt]
        elif resume:
            command = [*common, "resume", resume]
        else:
            command = common
        return EngineLaunch(engine, tuple(command), env)

    if engine == "claude":
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_API_KEY"] = token
        env["ANTHROPIC_AUTH_TOKEN"] = token
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        command = [
            executable,
            "--model",
            "claude-sonnet-4-6",
            "--bare",
            "--strict-mcp-config",
            "--mcp-config",
            str(empty_mcp_config),
            "--permission-mode",
            "plan",
        ]
        if resume:
            command.extend(["--resume", resume])
        if prompt is not None:
            command.extend(["-p", prompt, "--output-format", "text"])
        return EngineLaunch(engine, tuple(command), env)

    raise click.ClickException(f"unsupported coding engine: {engine}")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_gateway(process: subprocess.Popen[bytes], health_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    # The gateway is on loopback: never send its health probe through an ambient
    # HTTP(S)_PROXY (corporate proxy, or the hermetic benchmark mitmproxy). urllib
    # honours those env vars by default, which makes readiness always time out.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"gateway exited during startup with status {process.returncode}")
        try:
            with opener.open(health_url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    raise RuntimeError(f"gateway did not become ready within {timeout:g}s")


def _no_proxy_with_loopback() -> str:
    """Existing NO_PROXY plus the loopback names the gateway is reached by."""
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    entries = [item.strip() for item in existing.split(",") if item.strip()]
    for host in ("127.0.0.1", "localhost", "::1"):
        if host not in entries:
            entries.append(host)
    return ",".join(entries)


@contextmanager
def _managed_gateway(
    *,
    store_root: Path,
    status_file: Path,
    project_root: Path,
    provider: str,
    model: str,
    budget: str,
    cache_policy: str,
    max_cost: float | None,
    mcp_enabled: bool,
    mcp_schema_mode: str,
    optimization_mode: str,
    local_retrieval: str,
    local_retrieval_model: str,
) -> Iterator[tuple[str, str]]:
    port = _free_loopback_port()
    token = secrets.token_urlsafe(32)
    base_url = f"http://127.0.0.1:{port}"
    log_path = store_root / "logs" / "lemoncode-gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "LEMONCROW_ROOT": str(store_root),
            "LEMONCROW_WORKSPACE_ROOT": str(project_root),
            "CLAUDE_WORKSPACE_ROOT": str(project_root),
            "LEMONCROW_GATEWAY_TOKEN": token,
            # Frontend -> gateway traffic is loopback; an ambient proxy must never
            # intercept it (it cannot route back into this host's localhost).
            "NO_PROXY": _no_proxy_with_loopback(),
            "no_proxy": _no_proxy_with_loopback(),
            STATUS_FILE_ENV: str(status_file),
            "LEMONCROW_CODE_BUDGET": budget,
            "LEMONCROW_CODE_CACHE_POLICY": cache_policy,
            "LEMONCROW_CODE_MCP": "1" if mcp_enabled else "0",
            "LEMONCROW_CODE_MCP_SCHEMA_MODE": mcp_schema_mode,
            "LEMONCROW_OPTIMIZATION_MODE": optimization_mode,
            "LEMONCROW_LOCAL_RETRIEVAL": "off" if optimization_mode == "off" else local_retrieval,
            "LEMONCROW_LOCAL_RETRIEVAL_MODEL": local_retrieval_model,
        }
    )
    if provider:
        env["LEMONCROW_CODE_PROVIDER"] = provider
    else:
        env.pop("LEMONCROW_CODE_PROVIDER", None)
    if model:
        env["LEMONCROW_CODE_MODEL"] = model
    else:
        env.pop("LEMONCROW_CODE_MODEL", None)
    if max_cost is not None:
        env["LEMONCROW_CODE_MAX_COST"] = str(max_cost)
    else:
        env.pop("LEMONCROW_CODE_MAX_COST", None)

    # `-m` needs a code object, which a mypyc-compiled module (.so, no .py) does
    # not have -- runpy fails with "No code object available for ...". Import the
    # entrypoint instead so compiled and pure-Python installs both launch.
    command = [
        sys.executable,
        "-c",
        "from lemoncrow.gateway.openai_gateway.serve import main; main()",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--project-root",
        str(project_root),
    ]
    process: subprocess.Popen[bytes] | None = None
    with log_path.open("ab", buffering=0) as log_handle:
        try:
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _wait_for_gateway(
                process,
                f"{base_url}/health",
                timeout=float(os.environ.get("LEMONCROW_CODE_GATEWAY_START_TIMEOUT", "30")),
            )
            yield base_url, token
        except (OSError, RuntimeError) as exc:
            detail = ""
            try:
                detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:].strip()
            except OSError:
                pass
            suffix = f"\n\nGateway log:\n{detail}" if detail else f"\nGateway log: {log_path}"
            raise click.ClickException(f"could not start LemonCode gateway: {exc}{suffix}") from exc
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def run_coding_engine(
    *,
    store_root: Path,
    project_root: Path,
    engine: EngineName,
    provider: str,
    model: str,
    budget: str,
    cache_policy: str,
    max_cost: float | None,
    yolo: bool,
    prompt: str | None,
    resume: str | None,
    mcp_enabled: bool,
    mcp_schema_mode: str,
    optimization_mode: str = "shadow",
    local_retrieval: str = "auto",
    local_retrieval_model: str = "",
) -> int:
    """Run the selected mature frontend, with LemonCrow owning every model/tool turn."""
    if provider == "zen" and not model:
        # Zen's public tier needs no key -- default to its free model instead of
        # forcing the user to already know a model id just to pin the vendor.
        from lemoncrow.core.capabilities.providers.zen import ZEN_DEFAULT_FREE_MODEL

        model = ZEN_DEFAULT_FREE_MODEL.removeprefix("zen/")
        click.echo(f"lemoncrow: --provider zen with no --model; pinned to zen/{model}", err=True)
    if provider and not model:
        raise click.ClickException("--provider requires --model")

    if engine in {"auto", "lemoncode"}:
        from lemoncrow.gateway.cli.lemoncode_host import maybe_auto_update_host

        maybe_auto_update_host(store_root)
    selected, executable = _resolve_engine(engine, store_root=store_root)
    if selected == "native":
        from lemoncrow.gateway.cli.interactive import run_code_cli

        run_code_cli(
            store_root=store_root,
            project_root=project_root,
            provider=provider,
            model=model,
            budget=budget,
            cache_policy=cache_policy,
            max_cost=max_cost,
            yolo=yolo,
            prompt=prompt,
            resume=resume,
            mcp_enabled=mcp_enabled,
            mcp_schema_mode=mcp_schema_mode,
            optimization_mode=optimization_mode,
            local_retrieval=local_retrieval,
            local_retrieval_model=local_retrieval_model,
        )
        return 0

    empty_mcp = store_root / "cache" / "lemoncode-empty-mcp.json"
    empty_mcp.parent.mkdir(parents=True, exist_ok=True)
    empty_mcp.write_text('{"mcpServers":{}}\n', encoding="utf-8")

    # One structured snapshot path per run, shared by the gateway (writer) and
    # the frontend sidebar (reader).
    status_file = status_file_path(store_root, f"code-{os.getpid()}")

    with _managed_gateway(
        store_root=store_root,
        status_file=status_file,
        project_root=project_root,
        provider=provider,
        model=model,
        budget=budget,
        cache_policy=cache_policy,
        max_cost=max_cost,
        mcp_enabled=mcp_enabled,
        mcp_schema_mode=mcp_schema_mode,
        optimization_mode=optimization_mode,
        local_retrieval=local_retrieval,
        local_retrieval_model=local_retrieval_model,
    ) as (base_url, token):
        launch = _build_engine_launch(
            engine=selected,
            executable=executable or selected,
            base_url=base_url,
            token=token,
            store_root=store_root,
            project_root=project_root,
            empty_mcp_config=empty_mcp,
            budget=budget,
            prompt=prompt,
            resume=resume,
            status_file=status_file,
        )
        try:
            completed = subprocess.run(
                launch.command,
                cwd=project_root,
                env=launch.env,
                check=False,
            )
        except OSError as exc:
            raise click.ClickException(f"could not launch {selected}: {exc}") from exc
        return int(completed.returncode)


__all__ = ["EngineLaunch", "EngineName", "run_coding_engine"]
