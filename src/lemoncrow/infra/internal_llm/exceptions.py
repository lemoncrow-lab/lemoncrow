"""Centralized exceptions for the internal LLM subsystem."""

from __future__ import annotations


class InternalLLMError(RuntimeError):
    """Base exception for all internal LLM failures (Ollama, OpenAI, etc.)."""


class OllamaUnavailable(InternalLLMError):
    """Raised when the optional Ollama dependency or server is unavailable."""


class OpenAIClientUnavailable(InternalLLMError):
    """Raised when the openai package or API endpoint is unavailable."""


class LiteLLMUnavailable(InternalLLMError):
    """Raised when the optional litellm dependency or a provider call fails."""
