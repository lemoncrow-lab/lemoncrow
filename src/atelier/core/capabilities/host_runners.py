from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

RUNNER_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "id": "claude",
        "label": "Claude Code",
        "supports_model": True,
        "model_placeholder": "claude-sonnet-4.5",
        "options_help": "Extra CLI flags appended before the generated swarm prompt.",
    },
    {
        "id": "codex",
        "label": "Codex CLI",
        "supports_model": True,
        "model_placeholder": "gpt-5",
        "options_help": "Extra `codex exec` flags appended before the generated swarm prompt.",
    },
    {
        "id": "copilot",
        "label": "Copilot CLI",
        "supports_model": True,
        "model_placeholder": "gpt-5.5",
        "options_help": "Extra Copilot CLI flags appended before the generated swarm prompt.",
    },
    {
        "id": "opencode",
        "label": "OpenCode",
        "supports_model": True,
        "model_placeholder": "provider/model",
        "options_help": "Extra `opencode run` flags appended before the generated swarm prompt.",
    },
    {
        "id": "ollama-claude",
        "label": "Ollama Claude bridge",
        "supports_model": True,
        "model_placeholder": "qwen3.6",
        "options_help": "Extra flags passed through `ollama launch claude -- ...`.",
    },
)


@dataclass(frozen=True)
class ProviderPreset:
    env: dict[str, str]
    env_from_host: dict[str, str]
    supported_drivers: tuple[str, ...]


CLAUDE_PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openrouter-claude": ProviderPreset(
        env={
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            "ANTHROPIC_API_KEY": "",
        },
        env_from_host={"ANTHROPIC_AUTH_TOKEN": "OPENROUTER_API_KEY"},
        supported_drivers=("claude",),
    ),
    "aws-claude": ProviderPreset(
        env={"CLAUDE_CODE_USE_BEDROCK": "1"},
        env_from_host={
            "AWS_REGION": "AWS_REGION",
            "AWS_BEARER_TOKEN_BEDROCK": "AWS_BEARER_TOKEN_BEDROCK",
        },
        supported_drivers=("claude",),
    ),
    "azure-claude": ProviderPreset(
        env={"ANTHROPIC_API_KEY": ""},
        env_from_host={
            "ANTHROPIC_BASE_URL": "AZURE_CLAUDE_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN": "AZURE_CLAUDE_AUTH_TOKEN",
        },
        supported_drivers=("claude",),
    ),
    "gcp-claude": ProviderPreset(
        env={"ANTHROPIC_API_KEY": ""},
        env_from_host={
            "ANTHROPIC_BASE_URL": "GCP_CLAUDE_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN": "GCP_CLAUDE_AUTH_TOKEN",
        },
        supported_drivers=("claude",),
    ),
}


def list_runner_profiles() -> list[dict[str, Any]]:
    return [dict(profile) for profile in RUNNER_PROFILES]


def resolve_swarm_runner_command(
    *,
    runner: str | None,
    runner_model: str | None,
    runner_args: list[str] | tuple[str, ...],
    child_command: list[str] | tuple[str, ...],
    prompt_template: str,
) -> list[str]:
    if runner and child_command:
        raise ValueError("choose either a built-in runner or a raw child command, not both")
    if child_command:
        return list(child_command)
    if not runner:
        raise ValueError("pass a raw child command or select a built-in runner")

    profile = runner.lower()
    extra_args = list(runner_args)
    if profile == "claude":
        command = ["claude"]
        if runner_model:
            command.extend(["--model", runner_model])
        command.extend(["--dangerously-skip-permissions", "--print", *extra_args, prompt_template])
        return command
    if profile == "codex":
        command = ["codex", "exec"]
        if runner_model:
            command.extend(["-m", runner_model])
        command.extend(["--dangerously-bypass-approvals-and-sandbox", *extra_args, prompt_template])
        return command
    if profile == "copilot":
        command = ["copilot"]
        if runner_model:
            command.extend(["--model", runner_model])
        command.extend(["--allow-all", *extra_args, "-p", prompt_template])
        return command
    if profile == "opencode":
        command = ["opencode", "run"]
        if runner_model:
            command.extend(["-m", runner_model])
        command.extend(["--dangerously-skip-permissions", *extra_args, prompt_template])
        return command
    if profile == "ollama-claude":
        command = ["ollama", "launch", "claude", "--yes"]
        if runner_model:
            command.extend(["--model", runner_model])
        command.extend(["--", "--dangerously-skip-permissions", "--print", *extra_args, prompt_template])
        return command
    raise ValueError(f"unsupported runner profile: {runner}")


def resolve_runner_metadata(
    *,
    runner: str | None,
    runner_model: str | None,
    child_command: list[str] | tuple[str, ...],
) -> tuple[str, str]:
    if runner:
        return runner.lower(), runner_model or ""
    if not child_command:
        return "custom", ""
    inferred_model = ""
    child_tokens = list(child_command)
    for index, token in enumerate(child_tokens[:-1]):
        if token in {"--model", "-m"} and index + 1 < len(child_tokens):
            inferred_model = child_tokens[index + 1]
            break
    return child_tokens[0], inferred_model


def build_vix_cli_command(
    *,
    cli_driver: str,
    prompt: str,
    model: str,
    workspace: str,
    agent_command: str = "claude",
    extra_args: list[str] | tuple[str, ...] = (),
) -> list[str]:
    runner_args = list(extra_args)
    if cli_driver == "claude":
        return [
            *shlex.split(agent_command),
            *runner_args,
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
        ]
    if cli_driver == "copilot":
        return [
            "copilot",
            *runner_args,
            "-p",
            prompt,
            "-C",
            workspace,
            "--model",
            model,
            "--output-format",
            "json",
            "--allow-all",
            "--stream",
            "off",
            "--no-ask-user",
        ]
    if cli_driver == "codex":
        return [
            "codex",
            "exec",
            *runner_args,
            "--json",
            "--cd",
            workspace,
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            model,
            prompt,
        ]
    if cli_driver == "opencode":
        return [
            "opencode",
            "run",
            *runner_args,
            "--format",
            "json",
            "--dir",
            workspace,
            "--model",
            model,
            "--dangerously-skip-permissions",
            prompt,
        ]
    raise ValueError(f"unsupported cli driver: {cli_driver}")


def resolve_claude_provider_preset(name: str, *, openrouter_key_env: str = "OPENROUTER_API_KEY") -> ProviderPreset:
    if name not in CLAUDE_PROVIDER_PRESETS:
        raise ValueError(f"unsupported Claude provider preset: {name}")
    preset = CLAUDE_PROVIDER_PRESETS[name]
    if name != "openrouter-claude" or openrouter_key_env == "OPENROUTER_API_KEY":
        return preset
    return ProviderPreset(
        env=dict(preset.env),
        env_from_host={"ANTHROPIC_AUTH_TOKEN": openrouter_key_env},
        supported_drivers=preset.supported_drivers,
    )
