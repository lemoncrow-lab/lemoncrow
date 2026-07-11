from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from lemoncrow.core.capabilities.prefix_cache.planner import PrefixCachePlanner
from lemoncrow.core.capabilities.prompt_compilation.models import BlockKind, PromptBlock, Stability

_REQUESTED_SPAWN_FIELDS = (
    "prompt",
    "cache_policy",
    "stable_prefix_hash",
    "stable_prefix_tokens",
    "dynamic_tokens",
    "spawn_group_id",
    "cache_scope_id",
    "role_id",
)


@dataclass(frozen=True)
class CompiledChildPrompt:
    prompt: str
    stable_prefix: str
    dynamic_tail: str
    stable_prefix_hash: str
    stable_prefix_tokens: int
    dynamic_tokens: int
    total_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "stable_prefix": self.stable_prefix,
            "dynamic_tail": self.dynamic_tail,
            "stable_prefix_hash": self.stable_prefix_hash,
            "stable_prefix_tokens": self.stable_prefix_tokens,
            "dynamic_tokens": self.dynamic_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class SpawnEnvelope:
    step_id: str
    role_id: str
    prompt: str
    spawn_group_id: str
    cache_scope_id: str
    cache_policy: str
    stable_prefix_hash: str
    stable_prefix_tokens: int
    dynamic_tokens: int
    requested_fields: tuple[str, ...] = _REQUESTED_SPAWN_FIELDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "role_id": self.role_id,
            "prompt": self.prompt,
            "spawn_group_id": self.spawn_group_id,
            "cache_scope_id": self.cache_scope_id,
            "cache_policy": self.cache_policy,
            "stable_prefix_hash": self.stable_prefix_hash,
            "stable_prefix_tokens": self.stable_prefix_tokens,
            "dynamic_tokens": self.dynamic_tokens,
            "requested_fields": list(self.requested_fields),
        }


@dataclass(frozen=True)
class WaveSpawnPlan:
    wave_id: str
    spawn_group_id: str
    cache_scope_id: str
    cache_policy: str
    parallel: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave_id": self.wave_id,
            "spawn_group_id": self.spawn_group_id,
            "cache_scope_id": self.cache_scope_id,
            "cache_policy": self.cache_policy,
            "parallel": self.parallel,
        }


def compile_child_prompt(
    *,
    stem_prompt: str,
    current_prompt: str,
    transcript: Sequence[Mapping[str, str]] = (),
) -> CompiledChildPrompt:
    stem = stem_prompt.strip()
    transcript_text = format_transcript(transcript)
    stable_parts = [part for part in (stem, _transcript_block_text(transcript_text)) if part]
    stable_prefix = "\n\n".join(stable_parts).strip()
    dynamic_tail = "\n".join(part for part in ("Current phase prompt:", current_prompt.strip()) if part).strip()
    blocks = _prompt_blocks(stem=stem, transcript_text=transcript_text, dynamic_tail=dynamic_tail)
    plan = PrefixCachePlanner().plan(blocks)
    prompt = "\n\n".join(part for part in (stable_prefix, dynamic_tail) if part).strip()
    return CompiledChildPrompt(
        prompt=prompt,
        stable_prefix=stable_prefix,
        dynamic_tail=dynamic_tail,
        stable_prefix_hash=plan.prefix_hash if plan.prefix_tokens > 0 else "",
        stable_prefix_tokens=plan.prefix_tokens,
        dynamic_tokens=plan.dynamic_tokens,
        total_tokens=plan.total_tokens,
    )


def compile_prompt_text(prompt: str) -> CompiledChildPrompt:
    text = prompt.strip()
    if not text:
        return CompiledChildPrompt(
            prompt="",
            stable_prefix="",
            dynamic_tail="",
            stable_prefix_hash="",
            stable_prefix_tokens=0,
            dynamic_tokens=0,
            total_tokens=0,
        )
    if "Current phase prompt:" in text:
        stable_prefix, _, dynamic_tail = text.partition("Current phase prompt:")
        stable_text = stable_prefix.strip()
        stem_text = stable_text
        transcript_text = ""
        if "Forked conversation transcript:" in stable_text:
            stem_text, _, transcript_text = stable_text.partition("Forked conversation transcript:")
            transcript_text = transcript_text.strip()
        return compile_child_prompt(
            stem_prompt=stem_text.strip(),
            current_prompt=dynamic_tail.strip(),
            transcript=_parse_transcript(transcript_text),
        )
    block = PromptBlock(
        id="spawn/current",
        kind=BlockKind.USER_TASK,
        stability=Stability.TURN,
        content=text,
    )
    return CompiledChildPrompt(
        prompt=text,
        stable_prefix="",
        dynamic_tail=text,
        stable_prefix_hash="",
        stable_prefix_tokens=0,
        dynamic_tokens=block.token_estimate,
        total_tokens=block.token_estimate,
    )


def build_spawn_envelope(
    *,
    step_id: str,
    role_id: str,
    compiled_prompt: CompiledChildPrompt,
    spawn_group_id: str = "",
    cache_scope_id: str = "",
    cache_policy: str = "inherit",
) -> SpawnEnvelope:
    return SpawnEnvelope(
        step_id=step_id,
        role_id=role_id,
        prompt=compiled_prompt.prompt,
        spawn_group_id=spawn_group_id,
        cache_scope_id=cache_scope_id,
        cache_policy=cache_policy,
        stable_prefix_hash=compiled_prompt.stable_prefix_hash,
        stable_prefix_tokens=compiled_prompt.stable_prefix_tokens,
        dynamic_tokens=compiled_prompt.dynamic_tokens,
    )


def new_wave_spawn_plan(*, cache_policy: str, parallel: bool) -> WaveSpawnPlan:
    return WaveSpawnPlan(
        wave_id=uuid.uuid4().hex,
        spawn_group_id=uuid.uuid4().hex,
        cache_scope_id=uuid.uuid4().hex,
        cache_policy=cache_policy,
        parallel=parallel,
    )


def format_transcript(transcript: Sequence[Mapping[str, str]]) -> str:
    lines: list[str] = []
    for turn in transcript:
        lines.extend(
            [
                f"[{turn['step_id']}] {turn['phase_prompt_id']}",
                "Prompt:",
                turn["input_prompt"],
                "Output:",
                turn["output"],
                "",
            ]
        )
    return "\n".join(lines).strip()


def scope_break_reason(
    *,
    cache_policy: str,
    prior_scope_id: str = "",
    prior_prefix_hash: str = "",
    current_prefix_hash: str = "",
    selected_model: str = "",
    executed_model: str = "",
    selected_provider: str = "",
    executed_provider: str = "",
    selected_transport: str = "",
    executed_transport: str = "",
) -> str:
    if cache_policy == "fresh":
        return "cache_policy_fresh"
    if prior_scope_id and not prior_prefix_hash:
        return "prior_scope_without_prefix"
    if prior_prefix_hash and current_prefix_hash and prior_prefix_hash != current_prefix_hash:
        return "stable_prefix_changed"
    if selected_provider and executed_provider and selected_provider != executed_provider:
        return "provider_changed"
    if selected_model and executed_model and selected_model != executed_model:
        return "model_changed"
    if selected_transport and executed_transport and selected_transport != executed_transport:
        return "transport_changed"
    return ""


def _prompt_blocks(*, stem: str, transcript_text: str, dynamic_tail: str) -> tuple[PromptBlock, ...]:
    blocks: list[PromptBlock] = []
    if stem:
        blocks.append(
            PromptBlock(
                id="spawn/stem",
                kind=BlockKind.SYSTEM,
                stability=Stability.STATIC,
                content=stem,
            )
        )
    if transcript_text:
        digest = sha256(transcript_text.encode("utf-8")).hexdigest()[:16]
        blocks.append(
            PromptBlock(
                id=f"spawn/transcript/{digest}",
                kind=BlockKind.PLAYBOOK,
                stability=Stability.BRANCH,
                content=_transcript_block_text(transcript_text),
            )
        )
    if dynamic_tail:
        blocks.append(
            PromptBlock(
                id="spawn/current",
                kind=BlockKind.USER_TASK,
                stability=Stability.TURN,
                content=dynamic_tail,
            )
        )
    return tuple(blocks)


def _transcript_block_text(transcript_text: str) -> str:
    if not transcript_text:
        return ""
    return "\n".join(("Forked conversation transcript:", transcript_text)).strip()


def _parse_transcript(transcript_text: str) -> tuple[dict[str, str], ...]:
    if not transcript_text:
        return ()
    turns: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    section = ""
    for raw_line in transcript_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("[") and "] " in line:
            if current is not None:
                turns.append(current)
            step_id, _, phase_prompt_id = line[1:].partition("] ")
            current = {
                "step_id": step_id.strip(),
                "phase_prompt_id": phase_prompt_id.strip(),
                "input_prompt": "",
                "output": "",
            }
            section = ""
            continue
        if current is None:
            continue
        if line == "Prompt:":
            section = "input_prompt"
            continue
        if line == "Output:":
            section = "output"
            continue
        if not section:
            continue
        current[section] = "\n".join(part for part in (current[section], line) if part).strip()
    if current is not None:
        turns.append(current)
    return tuple(turns)


__all__ = [
    "CompiledChildPrompt",
    "SpawnEnvelope",
    "WaveSpawnPlan",
    "build_spawn_envelope",
    "compile_child_prompt",
    "compile_prompt_text",
    "format_transcript",
    "new_wave_spawn_plan",
    "scope_break_reason",
]
