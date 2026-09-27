"""Tests for the Cursor and Hermes Agent adapters."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lemoncrow.gateway.adapters.cursor_adapter import CursorAdapter, CursorConfig
from lemoncrow.gateway.adapters.hermes_adapter import HermesAdapter, HermesConfig
from lemoncrow.gateway.sdk import LemonCrowClient
from lemoncrow.gateway.sdk.client import ContextResult

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LemonCrowClient)
    # Make get_context return a ContextResult
    client.get_context.return_value = ContextResult(
        context="test reasoning context",
    )
    return client


@pytest.fixture
def cursor_adapter(mock_client: MagicMock) -> CursorAdapter:
    config = CursorConfig(mode="suggest", default_tools=["context", "trace"])
    return CursorAdapter.from_config(config, client=mock_client)


@pytest.fixture
def hermes_adapter(mock_client: MagicMock) -> HermesAdapter:
    config = HermesConfig(mode="enforce", default_domain="Agent.hermes")
    return HermesAdapter.from_config(config, client=mock_client)


# --------------------------------------------------------------------------- #
# CursorAdapter tests                                                         #
# --------------------------------------------------------------------------- #


class TestCursorAdapter:
    def test_from_config_defaults(self) -> None:
        """Default CursorConfig produces an adapter in shadow mode."""
        client = MagicMock(spec=LemonCrowClient)
        adapter = CursorAdapter.from_config(CursorConfig(), client=client)
        assert adapter.host == "cursor"
        assert adapter.mode == "shadow"
        assert adapter.default_domain is None
        assert adapter.default_tools == []

    def test_from_config_custom(self) -> None:
        """Custom CursorConfig fields propagate to the adapter."""
        client = MagicMock(spec=LemonCrowClient)
        config = CursorConfig(
            mode="enforce",
            default_domain="Agent.cursor",
            default_tools=["context", "trace"],
        )
        adapter = CursorAdapter.from_config(config, client=client)
        assert adapter.mode == "enforce"
        assert adapter.default_domain == "Agent.cursor"
        assert adapter.default_tools == ["context", "trace"]

    def test_prime_context(self, cursor_adapter: CursorAdapter, mock_client: MagicMock) -> None:
        """prime_context returns context from the client."""
        result = cursor_adapter.prime_context(
            task="Add logging to auth module",
            domain="python",
            files=["auth.py"],
            tools=["read", "edit"],
        )
        assert isinstance(result, ContextResult)
        assert "test reasoning context" in result.context
        mock_client.get_context.assert_called_once_with(
            task="Add logging to auth module",
            domain="python",
            files=["auth.py"],
            tools=["read", "edit"],
        )

    def test_get_context(self, cursor_adapter: CursorAdapter) -> None:
        """get_context delegates to the base adapter."""
        result = cursor_adapter.get_context(
            task="Fix bug in parser",
            domain="python",
            files=["parser.py"],
            tools=["read"],
        )
        assert isinstance(result, ContextResult)
        assert "test reasoning context" in result.context

    def test_get_decision_suggest(self, cursor_adapter: CursorAdapter) -> None:
        """get_decision returns a non-blocked decision in suggest mode."""
        decision = cursor_adapter.get_decision(
            task="Refactor auth",
            domain="python",
            files=["auth.py"],
        )
        assert decision.host == "cursor"
        assert decision.mode == "suggest"
        assert not decision.blocked
        assert "test reasoning context" in decision.reasoning_context

    def test_get_decision_enforce(self, mock_client: MagicMock) -> None:
        """get_decision in enforce mode still returns non-blocked."""
        adapter = CursorAdapter.from_config(
            CursorConfig(mode="enforce"),
            client=mock_client,
        )
        decision = adapter.get_decision(task="Critical fix")
        assert decision.mode == "enforce"
        # get_decision never sets blocked=True — that's for rubric/rescue
        assert not decision.blocked

    def test_install_returns_instructions(self) -> None:
        """install() returns non-empty installation instructions."""
        instructions = CursorAdapter.install()
        assert isinstance(instructions, str)
        assert "Cursor" in instructions
        assert "lc" in instructions
        assert ".cursor/mcp.json" in instructions

    def test_host_label(self, cursor_adapter: CursorAdapter) -> None:
        """Host label is set correctly."""
        assert cursor_adapter.host == "cursor"


# --------------------------------------------------------------------------- #
# HermesAdapter tests                                                         #
# --------------------------------------------------------------------------- #


class TestHermesAdapter:
    def test_from_config_defaults(self) -> None:
        """Default HermesConfig produces an adapter in shadow mode."""
        client = MagicMock(spec=LemonCrowClient)
        adapter = HermesAdapter.from_config(HermesConfig(), client=client)
        assert adapter.host == "hermes"
        assert adapter.mode == "shadow"
        assert adapter.default_domain is None
        assert adapter.default_tools == []

    def test_from_config_custom(self) -> None:
        """Custom HermesConfig fields propagate to the adapter."""
        client = MagicMock(spec=LemonCrowClient)
        config = HermesConfig(
            mode="suggest",
            default_domain="Agent.hermes",
            default_tools=["context", "trace", "rescue"],
        )
        adapter = HermesAdapter.from_config(config, client=client)
        assert adapter.mode == "suggest"
        assert adapter.default_domain == "Agent.hermes"
        assert adapter.default_tools == ["context", "trace", "rescue"]

    def test_prime_context(self, hermes_adapter: HermesAdapter, mock_client: MagicMock) -> None:
        """prime_context returns context from the client."""
        result = hermes_adapter.prime_context(
            task="Deploy to staging",
            domain="infra",
            tools=["shell"],
        )
        assert isinstance(result, ContextResult)
        assert "test reasoning context" in result.context
        mock_client.get_context.assert_called_once_with(
            task="Deploy to staging",
            domain="infra",
            files=None,
            tools=["shell"],
        )

    def test_get_context(self, hermes_adapter: HermesAdapter) -> None:
        """get_context delegates to the base adapter."""
        result = hermes_adapter.get_context(
            task="Optimize query",
            domain="database",
            files=["queries.py"],
            tools=["sql"],
        )
        assert "test reasoning context" in result.context

    def test_get_decision_enforce(self, hermes_adapter: HermesAdapter) -> None:
        """get_decision in enforce mode returns correct mode."""
        decision = hermes_adapter.get_decision(
            task="Database migration",
            domain="infra",
        )
        assert decision.host == "hermes"
        assert decision.mode == "enforce"
        assert "test reasoning context" in decision.reasoning_context

    def test_get_decision_shadow(self, mock_client: MagicMock) -> None:
        """get_decision in shadow mode returns correct mode."""
        adapter = HermesAdapter.from_config(
            HermesConfig(mode="shadow"),
            client=mock_client,
        )
        decision = adapter.get_decision(task="Simple task")
        assert decision.mode == "shadow"
        assert not decision.blocked

    def test_install_returns_instructions(self) -> None:
        """install() returns non-empty installation instructions."""
        instructions = HermesAdapter.install()
        assert isinstance(instructions, str)
        assert "Hermes" in instructions
        assert "lc" in instructions
        assert "config.yaml" in instructions or "HERMES_HOME" in instructions

    def test_host_label(self, hermes_adapter: HermesAdapter) -> None:
        """Host label is set correctly."""
        assert hermes_adapter.host == "hermes"
