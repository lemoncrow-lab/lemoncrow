"""MCP tool framework: registry + @mcp_tool decorator + argument coercion.

Shared substrate imported by every MCP tool module so all tools register into
the same ``TOOLS`` dict. Deliberately has NO ``lemoncrow`` imports so any tool
module (public or engine) can import it without circular-import risk.

Extracted verbatim from ``mcp_server.py`` (behaviour-preserving); ``mcp_server``
re-exports these names for backward compatibility.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import types
from collections.abc import Callable
from functools import wraps
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import Field, ValidationError, create_model

# Registry: tool_name -> {name, handler, description, inputSchema, param_aliases}.
# Populated at import time by every @mcp_tool-decorated handler.
TOOLS: dict[str, dict[str, Any]] = {}


_COERCE_UNCHANGED: Any = object()


def _annotation_base_types(annotation: Any) -> set[Any]:
    """Resolve an annotation to the set of concrete base types it accepts.

    Unwraps Optional/Union (both ``Union[...]`` and ``X | Y``) and generic
    aliases (``list[str]`` -> ``list``). Returns an empty set for ``Any`` or
    anything unrecognised, signalling "leave the value alone".
    """
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        resolved: set[Any] = set()
        for arg in get_args(annotation):
            resolved |= _annotation_base_types(arg)
        return resolved
    if origin is not None:
        return {origin}
    if isinstance(annotation, type):
        return {annotation}
    return set()


def _coerce_str_to_annotation(value: Any, annotation: Any) -> Any:
    """Coerce a stringified value to its parameter's annotated type.

    Some MCP clients serialise argument *values* as strings (``"20"`` for an
    int, ``"true"`` for a bool, ``'["a"]'`` for a list). Returns the coerced
    value, or the ``_COERCE_UNCHANGED`` sentinel when the value should be left
    untouched (already acceptable as a str, ambiguous, or not coercible).
    """
    if not isinstance(value, str):
        return _COERCE_UNCHANGED
    base = _annotation_base_types(annotation)
    if not base or str in base:
        return _COERCE_UNCHANGED
    if bool in base:
        low = value.strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True
        if low in {"false", "0", "no", "off"}:
            return False
        return _COERCE_UNCHANGED
    if int in base:
        try:
            return int(value)
        except ValueError:
            return _COERCE_UNCHANGED
    if float in base:
        try:
            return float(value)
        except ValueError:
            return _COERCE_UNCHANGED
    if base & {list, dict, tuple, set}:
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(parsed, (list, dict, tuple, set)):
                return parsed
        return _COERCE_UNCHANGED
    return _COERCE_UNCHANGED


def _coerce_json_strings(args: dict[str, Any], param_annotations: dict[str, Any]) -> dict[str, Any]:
    """Self-heal stringified argument values before Pydantic validation.

    Some MCP clients serialise argument values as strings (``"20"`` instead of
    ``20``, ``"true"`` instead of ``True``, ``'["a"]'`` instead of ``["a"]``).
    Each value is coerced to its parameter's annotated type so otherwise-valid
    calls don't fail. This matters doubly for the mypyc-compiled build, whose
    handlers enforce argument types at runtime and reject a stringified value
    outright. ``param_annotations`` maps each parameter to its *resolved* type:
    resolution (``get_type_hints`` in ``mcp_tool``) is required because
    ``from __future__ import annotations`` makes raw annotations plain strings.
    """
    if not isinstance(args, dict):
        return args
    coerced = args
    for param_name, annotation in param_annotations.items():
        if param_name not in coerced:
            continue
        new_val = _coerce_str_to_annotation(coerced[param_name], annotation)
        if new_val is _COERCE_UNCHANGED:
            continue
        if coerced is args:
            coerced = dict(args)
        coerced[param_name] = new_val
    return coerced


def _slim_schema(node: Any) -> Any:
    """Shrink a generated JSON schema for LLM tool clients without changing its contract.

    Drops per-node ``title`` keys and collapses nullable ``anyOf`` unions
    (Pydantic's ``X | None``) down to ``X``: the parameter stays optional via
    its absence from ``required``, and the tool handler's Pydantic model is
    untouched, so omitted or ``None`` arguments are still accepted. Purely
    removes wire bytes that never guided the model.
    """
    if isinstance(node, dict):
        # Strip Pydantic's scalar `title` annotation, but keep a property that is
        # literally named `title` (its value is a schema dict, not a string).
        slimmed = {
            key: _slim_schema(value) for key, value in node.items() if not (key == "title" and isinstance(value, str))
        }
        branches = slimmed.get("anyOf")
        if isinstance(branches, list):
            non_null = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
            if non_null and len(non_null) < len(branches):
                if len(non_null) == 1:
                    collapsed = {key: value for key, value in slimmed.items() if key != "anyOf"}
                    collapsed.update(non_null[0])
                    if collapsed.get("default") is None:
                        collapsed.pop("default", None)
                    return collapsed
                # Multiple real branches: drop only the null option. The param
                # stays optional via its absence from `required`.
                slimmed["anyOf"] = non_null
                if slimmed.get("default") is None:
                    slimmed.pop("default", None)
                return slimmed
        return slimmed
    if isinstance(node, list):
        return [_slim_schema(item) for item in node]
    return node


class _ToolArgumentError(ValueError):
    """Malformed tool arguments (pre-dispatch or handler argument-shape checks).

    Split marker for the dispatcher: a params-shape fault maps to a JSON-RPC
    -32602 protocol error, while any other handler-raised execution failure is
    returned as a successful response whose result carries ``isError: true``.
    """


def mcp_tool(
    name: str | None = None,
    description: str | None = None,
    input_schema: dict[str, Any] | None = None,
    hidden_params: tuple[str, ...] = (),
    param_aliases: dict[str, str] | None = None,
    recover_args: Callable[[dict[str, Any], frozenset[str]], dict[str, Any]] | None = None,
) -> Callable[[Callable[..., Any]], Callable[[dict[str, Any]], Any]]:
    """Decorator to register a tool and auto-derive its MCP schema.

    ``param_aliases`` maps an old (deprecated) argument name to its current
    parameter name. The advertised schema only shows the current name, but the
    handler accepts either: incoming args carrying an old name are remapped
    before validation (the current name wins if both are present).

    ``recover_args`` is a structural self-heal hook: called with
    ``(args, known_params)`` after alias remapping and before the unknown-args
    check, it may rewrite a malformed-but-unambiguous call shape (e.g. a
    flattened single-edit call) into a valid one. Fail-open: an exception in
    the hook leaves the original args untouched.
    """
    aliases = dict(param_aliases or {})

    def decorator(
        func: Callable[..., Any],
    ) -> Callable[[dict[str, Any]], Any]:
        tool_name = name or func.__name__.removeprefix("tool_")
        # Use the full docstring as the description so agents see all op detail.
        tool_description = description or (func.__doc__ or "").strip()

        sig = inspect.signature(func)
        # `from __future__ import annotations` makes raw signature annotations
        # plain strings; resolve them to real types so stringified scalar args
        # ("20" -> 20) can be coerced before the (mypyc-strict) handler runs.
        try:
            resolved_hints = get_type_hints(func)
        except Exception:  # noqa: BLE001 - fall back to raw annotations if hints don't resolve
            resolved_hints = {}
        param_annotations = {
            param_name: resolved_hints.get(param_name, param.annotation) for param_name, param in sig.parameters.items()
        }
        fields = {}
        for param_name, param in sig.parameters.items():
            annotation = param.annotation if param.annotation is not inspect.Parameter.empty else Any
            default = param.default if param.default is not inspect.Parameter.empty else ...
            fields[param_name] = (
                annotation,
                Field(default=default) if default is not ... else Field(...),
            )

        if fields:
            # Convert to format expected by create_model: (type, default/Field)
            field_defs = {k: (v[0], v[1]) for k, v in fields.items()}
            known_params = frozenset(field_defs)
            visible_params = frozenset(k for k in field_defs if k not in hidden_params)
            # __module__ pins Pydantic's forward-ref resolution to the decorated
            # function's own module namespace (where Annotated / knob types live),
            # not framework's -- so @mcp_tool works from any tool module.
            ArgsModel = create_model(  # type: ignore[call-overload]
                f"{func.__name__}_Args", __module__=func.__module__, **field_defs
            )
            schema = ArgsModel.model_json_schema()
            # Niche params stay accepted by the handler but are not published to LLMs.
            for hidden in hidden_params:
                schema.get("properties", {}).pop(hidden, None)
            # Strip Pydantic schema noise (per-node `title`, nullable `anyOf`
            # unions) that costs tokens on every request without guiding the
            # LLM. The handler's model is unchanged, so omitted/None args are
            # still accepted; this only shrinks the wire schema.
            schema = _slim_schema(schema)

            @wraps(func)
            def handler_wrapper(args: dict[str, Any]) -> Any:
                # Remap deprecated arg names to their current parameter before
                # validation. The current name wins if both are present.
                if isinstance(args, dict) and aliases:
                    remapped: dict[str, Any] | None = None
                    for old_name, new_name in aliases.items():
                        if old_name in args and new_name not in args:
                            if remapped is None:
                                remapped = dict(args)
                            remapped[new_name] = remapped.pop(old_name)
                    if remapped is not None:
                        args = remapped
                # Structural self-heal (e.g. a flattened single-edit call).
                # Runs after alias remap so it sees canonical top-level names,
                # and before the unknown-args check so a recovered call does
                # not error. A rejection here throws away the entire tool_use
                # the model just emitted (often several KB of file content)
                # and forces a full re-emission on the retry.
                _recovery_failure: str | None = None
                if recover_args is not None and isinstance(args, dict):
                    try:
                        args = recover_args(args, known_params)
                    except Exception as recover_exc:
                        _recovery_failure = f"{type(recover_exc).__name__}: {recover_exc}"
                        logging.exception("Recovered from broad exception handler")
                # Pydantic's default config silently drops unknown keys, so a
                # typo'd argument (e.g. codemod `dryrun` for `dry_run`) would be
                # discarded and the wrong default used while the call still
                # "succeeds". Surface those keys instead of forbidding them, so
                # callers that legitimately pass extras are not broken. Accepted
                # aliases are not "unknown" even if a caller passes both names.
                if isinstance(args, dict):
                    unknown = [key for key in args if key not in known_params and key not in aliases]
                    if unknown:
                        msg = (
                            f"tool {tool_name!r} received unknown argument(s) {sorted(unknown)}; "
                            f"known: {sorted(visible_params)}"
                        )
                        if _recovery_failure:
                            msg += f" (argument recovery attempted and failed: {_recovery_failure})"
                        raise _ToolArgumentError(msg)
                try:
                    validated = ArgsModel.model_validate(_coerce_json_strings(args, param_annotations))
                except ValidationError as exc:
                    if isinstance(args, dict) and not args:
                        # An empty argument object almost always means the client
                        # dropped the call's arguments in transit -- typically a large
                        # batch (e.g. many `edits`) carrying non-ASCII characters that
                        # didn't serialise. Surface an actionable hint instead of a
                        # bare "field required".
                        raise _ToolArgumentError(
                            f"{tool_name}: received empty arguments. If this was a large batch "
                            "(e.g. many edits) with non-ASCII characters, the MCP client likely "
                            "dropped the arguments in transit -- retry with fewer items per call "
                            "and \\uXXXX escapes for any non-ASCII characters."
                        ) from exc
                    raise
                return func(**validated.model_dump())

        else:
            schema = {"type": "object", "properties": {}}

            @wraps(func)
            def handler_wrapper(_args: dict[str, Any]) -> Any:
                return func()

        TOOLS[tool_name] = {
            "name": tool_name,
            "handler": handler_wrapper,
            "description": tool_description,
            "inputSchema": input_schema or schema,
            "param_aliases": dict(aliases),
        }
        return handler_wrapper

    return decorator
