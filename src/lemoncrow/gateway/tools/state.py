"""Per-thread state shared by tool handlers and transport adapters."""

from __future__ import annotations

import contextlib
import threading

# Hosted/loopback servers may inject a revision-bound code engine for one
# request. Keep it thread-local so concurrent tenants never share index state.
request_code_engine_override: threading.local = threading.local()
NO_CODE_ENGINE_OVERRIDE = object()


def set_request_code_context_engine(engine: object) -> object:
    """Install a request-scoped code engine and return the prior value."""
    prior = getattr(request_code_engine_override, "value", NO_CODE_ENGINE_OVERRIDE)
    request_code_engine_override.value = engine
    return prior


def clear_request_code_context_engine(prior: object) -> None:
    """Restore a prior value returned by :func:`set_request_code_context_engine`."""
    if prior is NO_CODE_ENGINE_OVERRIDE:
        with contextlib.suppress(AttributeError):
            del request_code_engine_override.value
    else:
        request_code_engine_override.value = prior


# Per-call token-savings credit produced by handlers and consumed by accounting.
# This shares the same transport-independent lifetime as the other per-call
# state below; smart_state only persists machine-global counters.
tool_call_tokens_saved: threading.local = threading.local()

# Counterfactual accounting produced by code-intel handlers and consumed by the
# dispatcher accounting layer.
tool_call_counterfactual: threading.local = threading.local()

# Compact host-facing rendering produced by handlers/brokers. The dispatcher
# prefers this over dumping structured payloads into model context.
tool_call_rendered_text: threading.local = threading.local()

# Full structured result retained only for in-process callers such as
# ``lc tools call --json``; never serialized automatically to the host model.
tool_call_raw_result: threading.local = threading.local()

# Image content blocks accumulated by read handlers and drained into the final
# tool response.
tool_call_images: threading.local = threading.local()

__all__ = [
    "NO_CODE_ENGINE_OVERRIDE",
    "clear_request_code_context_engine",
    "request_code_engine_override",
    "set_request_code_context_engine",
    "tool_call_counterfactual",
    "tool_call_images",
    "tool_call_raw_result",
    "tool_call_rendered_text",
    "tool_call_tokens_saved",
]
