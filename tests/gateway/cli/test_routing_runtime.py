from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("litellm", reason="lemoncrow[litellm] not installed")

from lemoncrow.gateway.cli.runtime import InteractiveRuntime
from lemoncrow.pro.capabilities.optimization.routing_calibration import CalibratedRoute


def _chunk(*, content=None, tool_calls=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls or []),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def test_owned_runtime_enforces_eligible_calibrated_phase_route(tmp_path, monkeypatch) -> None:
    called_models: list[str] = []

    def completion(**kwargs):
        called_models.append(kwargs["model"])
        if len(called_models) == 1:
            call = SimpleNamespace(
                index=0,
                id="read-1",
                function=SimpleNamespace(name="read", arguments=json.dumps({"path": "parser.py"})),
            )
            return [_chunk(tool_calls=[call], finish_reason="tool_calls")]
        return [_chunk(content="Found the parser.", finish_reason="stop")]

    async def execute(_self, _name, _args, session_id=""):
        return "parser.py:1 def parse():", True

    route_decision = SimpleNamespace(
        provider="openai",
        model="gpt-5",
        tier="high",
        reason="legacy",
        alternatives=(),
    )
    calibrated = CalibratedRoute(
        provider="openai",
        model="gpt-5",
        tier="high",
        direct_cost_usd=0.1,
        failure_probability=0.05,
        escalation_cost_usd=0.1,
        cache_break_cost_usd=0.0,
        expected_total_cost_usd=0.105,
        sample_count=20,
        eligible=True,
        reason="calibrated",
    )

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr(InteractiveRuntime, "_execute_tool_call", execute)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.owned_execution_routing.select_owned_route",
        lambda *_args, **_kwargs: route_decision,
    )
    monkeypatch.setattr(
        "lemoncrow.gateway.cli.runtime.choose_calibrated_route",
        lambda *_args, **_kwargs: calibrated,
    )
    runtime = InteractiveRuntime(
        root=tmp_path / ".lemoncrow",
        yolo=True,
        optimization_mode="enforce",
    )

    async def run():
        session_id = await runtime.start_session(str(tmp_path))
        messages = [{"role": "user", "content": "Explain the parser"}]
        return [
            event
            async for event in runtime._agent_loop(
                session_id,
                messages,
                model="openai/gpt-4o-mini",
                task_text="Explain the parser",
                dynamic_routing=True,
            )
        ]

    asyncio.run(run())
    assert called_models == ["openai/gpt-4o-mini", "openai/gpt-5"]
