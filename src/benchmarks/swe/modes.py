"""Benchmark mode definitions.

Each mode configures (a) what Atelier surface the agent sees and (b) which
forced workflow steps the harness must observe. Modes are deliberately
distinct so the report can attribute deltas to specific Atelier features.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Mode(StrEnum):
    """Five benchmark modes, ordered from baseline to fully-warmed."""

    VANILLA = "vanilla"
    ATELIER_TOOLS_AVAILABLE = "atelier_tools_available"
    ATELIER_FORCED_WORKFLOW = "atelier_forced_workflow"
    ATELIER_FULL_RUNTIME = "atelier_full_runtime"
    ATELIER_WARM_REASONBLOCKS = "atelier_warm_reasonblocks"


@dataclass(frozen=True)
class ModeSpec:
    name: Mode
    label: str
    mcp_available: bool
    forced_steps: tuple[str, ...]
    enable_run_ledger: bool
    enable_monitors: bool
    enable_compressor: bool
    enable_savings_ledger: bool
    enable_smart_tools: bool
    requires_warm_blocks: bool = False
    description: str = ""
    forbidden_assets: tuple[str, ...] = field(default_factory=lambda: ("gold_patch",))


_FORCED = (
    "get_context",
    "rescue_failure",
    "run_rubric_gate",
    "record_trace",
)


def mode_specs() -> dict[Mode, ModeSpec]:
    """Return the canonical mode specification table."""
    return {
        Mode.VANILLA: ModeSpec(
            name=Mode.VANILLA,
            label="Vanilla",
            mcp_available=False,
            forced_steps=(),
            enable_run_ledger=False,
            enable_monitors=False,
            enable_compressor=False,
            enable_savings_ledger=False,
            enable_smart_tools=False,
            description="No Atelier tooling — agent sees only the task.",
        ),
        Mode.ATELIER_TOOLS_AVAILABLE: ModeSpec(
            name=Mode.ATELIER_TOOLS_AVAILABLE,
            label="Atelier (tools available)",
            mcp_available=True,
            forced_steps=(),
            enable_run_ledger=False,
            enable_monitors=False,
            enable_compressor=False,
            enable_savings_ledger=False,
            enable_smart_tools=False,
            description="MCP tools exposed; agent decides whether to use them.",
        ),
        Mode.ATELIER_FORCED_WORKFLOW: ModeSpec(
            name=Mode.ATELIER_FORCED_WORKFLOW,
            label="Atelier (forced workflow)",
            mcp_available=True,
            forced_steps=_FORCED,
            enable_run_ledger=False,
            enable_monitors=False,
            enable_compressor=False,
            enable_savings_ledger=False,
            enable_smart_tools=False,
            description=(
                "Agent must call get_context, rescue_failure, "
                "run_rubric_gate, and record_trace at the prescribed phases."
            ),
        ),
        Mode.ATELIER_FULL_RUNTIME: ModeSpec(
            name=Mode.ATELIER_FULL_RUNTIME,
            label="Atelier (full runtime)",
            mcp_available=True,
            forced_steps=_FORCED,
            enable_run_ledger=True,
            enable_monitors=True,
            enable_compressor=True,
            enable_savings_ledger=True,
            enable_smart_tools=True,
            description=(
                "Forced workflow + run ledger + monitors + context compressor + "
                "smart tools + savings ledger."
            ),
        ),
        Mode.ATELIER_WARM_REASONBLOCKS: ModeSpec(
            name=Mode.ATELIER_WARM_REASONBLOCKS,
            label="Atelier (warm ReasonBlocks)",
            mcp_available=True,
            forced_steps=_FORCED,
            enable_run_ledger=True,
            enable_monitors=True,
            enable_compressor=True,
            enable_savings_ledger=True,
            enable_smart_tools=True,
            requires_warm_blocks=True,
            description=(
                "Full runtime preloaded with calibration-derived ReasonBlocks "
                "(no task-specific gold patches)."
            ),
        ),
    }


def get_spec(mode: Mode) -> ModeSpec:
    return mode_specs()[mode]
