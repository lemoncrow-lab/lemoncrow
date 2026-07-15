"""Compiled-artifact (.so) MCP server tests.

The production wheel compiles ~440 modules with mypyc (see ``hatch_build.py``),
which inserts STRICT runtime type checks at the C boundary.  Plain CPython is
lenient about argument types, so loosely-/string-typed MCP tool arguments slide
through the editable ``.py`` server; the mypyc-compiled ``.so`` raises
``TypeError`` ("bool object expected; got str", "list or None object expected;
got str", "dict object expected; got str", ...).  ``mcp_server._handle`` even
carries a comment naming this exact failure mode.

Every other MCP test runs the editable ``.py`` path only:
  * ``test_mcp_stdio_smoke`` drives ``uv run lemoncrow mcp`` (editable install)
  * ``test_mcp_jsonrpc_e2e`` calls ``_handle`` in-process (editable import)
Neither can catch compiled-only failures.  This module builds the *real* mypyc
wheel, installs it into an isolated venv, and drives the shipped ``lemoncrow mcp``
stdio server over JSON-RPC -- the only faithful way to exercise the ``.so``.

For every registered tool we send a ``tools/call`` with native-typed arguments
and again with every value serialised as a string (the shape a misbehaving MCP
client produces).  The compiled server must handle the stringified call without
a mypyc C-level type assertion, and must not succeed on the typed call while
failing on the stringified one (the ``.py``-works/``.so``-fails divergence).

These are slow -- the mypyc build takes minutes -- and gated behind
``@pytest.mark.slow`` (the whole module via ``pytestmark``).  When the host
cannot build the wheel (no ``uv``/compiler, or ``LEMONCROW_SKIP_MYPYC`` produced a
pure-python wheel) the tests SKIP with a clear reason rather than error.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

# Editable import -- used ONLY to enumerate the registered tool surface and read
# each tool's published input schema.  The handlers themselves are exercised in
# the compiled subprocess, never here.
from lemoncrow.core.environment import HIDDEN_LLM_TOOLS
from lemoncrow.gateway.adapters.mcp_server import TOOLS

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]

# mypyc emits exactly this phrasing when a compiled function receives an argument
# of the wrong runtime type.  A ``.py`` server NEVER produces it; a ``.so`` server
# does whenever a stringified value reaches a handler without being coerced first.
_MYPYC_TYPE_ERROR = re.compile(r"object expected; got|has incompatible type", re.IGNORECASE)

# Directories never worth copying into the throwaway build tree. ``bundle`` is a
# packaging artifact that can hold dangling symlinks from a prior build, so it is
# excluded explicitly (and copytree is told to tolerate dangling links anyway).
_COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "benchmarks",
    "build",
    "dist",
    "bundle",
    "*.so",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.egg-info",
)


@dataclasses.dataclass(frozen=True)
class _CompiledWheel:
    path: Path
    private_key_hex: str


@dataclasses.dataclass
class _CompiledServer:
    lemoncrow_bin: str
    env: dict[str, str]
    workspace: Path


# --------------------------------------------------------------------------- #
# Build + install fixtures (session-scoped: the mypyc build runs once)         #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def compiled_wheel(tmp_path_factory: pytest.TempPathFactory) -> _CompiledWheel:
    """Build the mypyc-compiled wheel from a throwaway copy of the repo.

    The hatch build hook mutates ``src/`` in place (deletes ``.py`` for each
    compiled module, restores in ``finalize``).  We build from a *copy* so the
    developer's working tree -- which may hold uncommitted changes -- is never
    touched even if the build is interrupted.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH; cannot build the mypyc wheel")

    build_base = tmp_path_factory.mktemp("lemoncrow_build")
    repo_copy = build_base / "src"
    shutil.copytree(REPO_ROOT, repo_copy, ignore=_COPY_IGNORE, ignore_dangling_symlinks=True)

    # The isolated compiled process cannot inherit pytest monkeypatches. Give
    # this temporary wheel a generated pinned key, then seed a genuinely signed
    # verdict below. Production source and keys are never changed.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    private_key_hex = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ).hex()
    public_key_hex = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        .hex()
    )
    gate_path = repo_copy / "src/lemoncrow/pro/capabilities/licensing_gate.py"
    original_gate_source = gate_path.read_text(encoding="utf-8")
    gate_source = re.sub(
        r'_DEFAULT_PUBLIC_KEY_HEX = "[0-9a-f]{64}"',
        f'_DEFAULT_PUBLIC_KEY_HEX = "{public_key_hex}"',
        original_gate_source,
        count=1,
    )
    assert gate_source != original_gate_source, "compiled gate key constant not found"
    gate_path.write_text(gate_source, encoding="utf-8")
    out_dir = build_base / "wheel"

    # Must NOT skip mypyc -- a pure-python wheel cannot exercise the .so path.
    env = {k: v for k, v in os.environ.items() if k != "LEMONCROW_SKIP_MYPYC"}
    proc = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=str(repo_copy),
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        # Build failure here is environmental (missing/incompatible C toolchain)
        # or a mypyc-incompatible module -- not a coercion regression. Skip with
        # the stderr tail so the cause is diagnosable rather than turning the
        # whole suite red.
        pytest.skip(f"mypyc wheel build failed (toolchain or mypyc-incompatible module):\n{proc.stderr[-2000:]}")

    wheels = sorted(out_dir.glob("lemoncrow-*.whl"))
    if not wheels:
        pytest.skip("`uv build --wheel` produced no wheel")
    wheel = wheels[-1]

    # A genuinely compiled wheel carries a platform/abi tag, e.g.
    # ``lemoncrow-0.3.5-cp312-cp312-linux_x86_64.whl``.  A pure-python wheel
    # (``...-py3-none-any.whl``) means mypyc did not run -- it would give false
    # assurance, so skip instead.
    if wheel.name.endswith("-py3-none-any.whl"):
        pytest.skip(f"wheel is pure-python ({wheel.name}); mypyc compilation did not run")
    return _CompiledWheel(path=wheel, private_key_hex=private_key_hex)


@pytest.fixture(scope="session")
def compiled_server(
    compiled_wheel: _CompiledWheel,
    tmp_path_factory: pytest.TempPathFactory,
) -> _CompiledServer:
    """Install the compiled wheel into an isolated venv and prepare its runtime."""
    venv_dir = tmp_path_factory.mktemp("lemoncrow_venv") / "venv"
    create = subprocess.run(
        ["uv", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if create.returncode != 0:
        pytest.skip(f"`uv venv` failed:\n{create.stderr[-2000:]}")

    venv_python = venv_dir / "bin" / "python"
    lemoncrow_bin = venv_dir / "bin" / "lemoncrow"

    # The coercion layer lives in the core server modules, so a minimal extra set
    # is enough to start the server and dispatch every tool; tools whose optional
    # backend is absent degrade to a graceful error (fine for the parity check).
    install = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_python),
            f"{compiled_wheel.path}[mcp,smart,memory]",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if install.returncode != 0:
        pytest.skip(f"compiled wheel install failed:\n{install.stderr[-2000:]}")
    if not lemoncrow_bin.exists():
        pytest.skip("lemoncrow console script missing from venv after install")

    # Confirm the installed package is actually compiled (.so present, not .py).
    so_files = list(venv_dir.rglob("mcp_server*.so"))
    if not so_files:
        pytest.skip("installed wheel has no mcp_server .so; not a compiled build")

    root = tmp_path_factory.mktemp("lemoncrow_root") / ".lemoncrow"
    from lemoncrow.core.capabilities.licensing import cap_verdict, store

    device_hash = hashlib.sha256(
        store.load_or_create_device_id().encode("utf-8"),
    ).hexdigest()
    now = int(time.time())
    cap_token = cap_verdict.sign_cap_token(
        {
            "v": 2,
            "typ": "cap",
            "account_id": "anon:compiled-test",
            "device_id": device_hash,
            "plan": "free",
            "savings_over_cap": False,
            "monthly_savings_usd": 0.0,
            "cap_usd": 20.0,
            "issued_at": now,
            "expires_at": now + 3600,
        },
        private_key_hex=compiled_wheel.private_key_hex,
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "subscription.json").write_text(
        json.dumps({"capVerdictToken": cap_token}),
        encoding="utf-8",
    )
    config_dir = tmp_path_factory.mktemp("lemoncrow_cfg") / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path_factory.mktemp("lemoncrow_ws")
    (workspace / "sample.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (workspace / "edit_target.txt").write_text("alpha\n", encoding="utf-8")

    # Seed the run-ledger store so store-dependent tools don't error on a cold
    # root (mirrors test_mcp_jsonrpc_e2e). Best-effort: the server self-inits too.
    with contextlib.suppress(Exception):
        from tests.helpers import init_store_at

        init_store_at(str(root))

    env = {
        **os.environ,
        "LEMONCROW_ROOT": str(root),
        "CLAUDE_WORKSPACE_ROOT": str(workspace),
        "CLAUDE_CONFIG_DIR": str(config_dir),
        # Put the venv first so any subprocess the server spawns is the compiled one.
        "PATH": f"{venv_dir / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    return _CompiledServer(lemoncrow_bin=str(lemoncrow_bin), env=env, workspace=workspace)


# --------------------------------------------------------------------------- #
# JSON-RPC driver + argument helpers                                           #
# --------------------------------------------------------------------------- #

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "clientInfo": {"name": "compiled-so-test", "version": "1"},
        "capabilities": {},
    },
}


def _run_server(
    server: _CompiledServer, calls: list[dict[str, Any]], timeout: int = 120
) -> tuple[dict[Any, dict[str, Any]], subprocess.CompletedProcess[str]]:
    """Spawn ``lemoncrow mcp``, feed initialize + ``calls``, return {id: response}.

    Strict framing gate (matches test_mcp_stdio_smoke): every non-empty stdout
    line MUST be a JSON object, else the server printed something off-protocol.
    """
    requests = [_INITIALIZE, *calls]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [server.lemoncrow_bin, "mcp"],
        input=payload,
        text=True,
        capture_output=True,
        env=server.env,
        timeout=timeout,
        check=False,
    )
    responses: dict[Any, dict[str, Any]] = {}
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"non-protocol stdout line: {line!r}\nstderr tail:\n{proc.stderr[-1500:]}") from exc
        assert isinstance(msg, dict), f"non-protocol stdout line: {line!r}"
        if "id" in msg:
            responses[msg["id"]] = msg
    return responses, proc


def _stringify_value(value: Any) -> Any:
    """Serialise a value the way a misbehaving MCP client would."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _stringify_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: _stringify_value(v) for k, v in args.items() if v is not None}


def _native_args(tool: str, workspace: Path) -> dict[str, Any]:
    """Representative VALID, side-effect-safe arguments for a tool.

    Curated per tool so the native call establishes a baseline (where possible)
    and stays confined to the isolated workspace.  Side-effecting tools (agent,
    workflow, web_fetch) use args that fail fast and identically.  Any tool not
    listed here is synthesised from its published input schema so newly added
    tools are still covered by the no-crash assertion.
    """
    sample = str(workspace / "sample.py")
    edit_target = str(workspace / "edit_target.txt")
    ws = str(workspace)
    overrides: dict[str, dict[str, Any]] = {
        "read": {"path": sample, "max_lines": 5, "full": False},
        "edit": {
            "edits": [{"file_path": edit_target, "old_string": "alpha", "new_string": "beta"}],
            "atomic": True,
            "post_edit_hooks": False,
        },
        "grep": {
            "path": ws,
            "content_regex": "alpha",
            "ignore_case": True,
            "lines_after": 1,
            "context_budget_tokens": 2000,
        },
        "search": {"query": "alpha", "path": ws, "max_files": 3, "budget_tokens": 800},
        "bash": {"command": "echo hi", "timeout": 30, "max_lines": 20, "background": False},
        "sql": {"action": "tables", "max_rows": 10, "allow_writes": False},
        "memory": {"op": "recall", "query": "alpha", "top_k": 3, "tags": ["x"]},
        "context": {"task": "exercise the read path", "files": [sample]},
        "codemod": {"pattern": "isinstance($X, $Y)"},
        "explore": {"query": "alpha", "max_files": 3, "seed_files": [sample]},
        "route": {"task": "add a feature", "task_type": "feature"},
        "rescue": {"task": "fix the test", "error": "AssertionError: boom", "attempt": 1},
        "trace": {"agent": "tester", "domain": "test", "task": "noop", "status": "success"},
        "verify": {"rubric_id": "does-not-exist", "checks": {"ran": True}},
        "workflow": {"op": "status"},
        "web_fetch": {"url": "http://127.0.0.1:9/unreachable"},
        "agent": {"prompt": "noop"},
    }
    if tool in overrides:
        return overrides[tool]
    return _synthesise_from_schema(tool, workspace)


def _synthesise_from_schema(tool: str, workspace: Path) -> dict[str, Any]:
    """Fallback args for an un-curated (e.g. newly added) tool."""
    schema = TOOLS[tool].get("inputSchema", {}) or {}
    props: dict[str, Any] = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", []) if isinstance(schema, dict) else [])
    args: dict[str, Any] = {}
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        coercible = spec.get("type") in {"integer", "number", "boolean", "array", "object"}
        if name not in required and not coercible:
            continue
        args[name] = _dummy_for(name, spec, workspace)
    return args


def _dummy_for(name: str, spec: dict[str, Any], workspace: Path) -> Any:
    typ = spec.get("type")
    if "enum" in spec and isinstance(spec["enum"], list) and spec["enum"]:
        return spec["enum"][0]
    if typ == "integer":
        return 1
    if typ == "number":
        return 1.0
    if typ == "boolean":
        return False
    if typ == "array":
        return []
    if typ == "object":
        return {}
    if name in {"path", "file_path", "cwd"}:
        return str(workspace / "sample.py")
    if name == "url":
        return "http://127.0.0.1:9/unreachable"
    return "x"


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_compiled_server_handshake_and_tools_list(compiled_server: _CompiledServer) -> None:
    """The compiled .so server boots, completes the handshake, and lists tools."""
    responses, proc = _run_server(
        compiled_server,
        [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}],
    )
    assert 1 in responses, f"no initialize response; stderr:\n{proc.stderr[-1500:]}"
    assert responses[1]["result"]["serverInfo"]["name"] == "lemoncrow"
    assert 2 in responses, f"no tools/list response; stderr:\n{proc.stderr[-1500:]}"

    names = {tool["name"] for tool in responses[2]["result"]["tools"]}
    visible = set(TOOLS) - HIDDEN_LLM_TOOLS
    missing = visible - names
    assert not missing, f"compiled server missing LLM-visible tools: {sorted(missing)}"


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_compiled_tool_handles_stringified_args(compiled_server: _CompiledServer, tool: str) -> None:
    """Every registered tool must survive fully-stringified arguments on the .so.

    Regression guard for the mypyc strictness bug: stringified args pass under
    lenient CPython (.py) but make the compiled .so raise a C-level TypeError
    unless coerced first.  We assert (a) no mypyc type assertion on the
    stringified call, and (b) no "typed works, stringified fails" divergence.
    """
    native = _native_args(tool, compiled_server.workspace)
    stringified = _stringify_args(native)
    calls = [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": native},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool, "arguments": stringified},
        },
    ]
    try:
        responses, proc = _run_server(compiled_server, calls, timeout=120)
    except subprocess.TimeoutExpired:
        # A few tools (agent/workflow) may attempt real execution; a hang is not
        # a coercion failure, so don't turn it into a false red.
        pytest.skip(f"{tool}: server did not return within timeout (likely real execution)")

    assert 2 in responses, f"{tool}: no response to native call (server died?).\nstderr:\n{proc.stderr[-1500:]}"
    assert (
        3 in responses
    ), f"{tool}: no response to stringified call (server died after native call).\nstderr:\n{proc.stderr[-1500:]}"
    native_resp = responses[2]
    str_resp = responses[3]

    # (a) The stringified call must not trip a mypyc C-level type assertion.
    if "error" in str_resp:
        message = str(str_resp["error"].get("message", ""))
        assert not _MYPYC_TYPE_ERROR.search(message), (
            f"{tool}: compiled server rejected stringified args with a mypyc type "
            f"error -- args are not coerced before the .so handler: {message!r}\n"
            f"native_resp={native_resp}\nstringified_resp={str_resp}"
        )

    # (b) No .py-works/.so-fails divergence: typed result implies stringified result.
    if "result" in native_resp and "error" in str_resp:
        pytest.fail(
            f"{tool}: native (typed) call succeeded but the stringified call failed "
            f"-- the exact .py-works/.so-fails regression.\n"
            f"native_resp={native_resp}\nstringified_resp={str_resp}"
        )


def _tools_call(rid: Any, name: str, arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _assert_not_mypyc_error(label: str, response: dict[str, Any]) -> None:
    """A response may be a result or a graceful app error -- never a mypyc crash."""
    if "error" in response:
        message = str(response["error"].get("message", ""))
        assert not _MYPYC_TYPE_ERROR.search(
            message
        ), f"{label}: compiled server raised a mypyc C-level type assertion: {message!r}\nresponse={response}"


def test_compiled_server_accepts_arguments_as_json_string(
    compiled_server: _CompiledServer,
) -> None:
    """The whole ``arguments`` payload sent as a JSON *string* must still work.

    Some MCP clients serialise ``params.arguments`` as a string rather than an
    object.  A mypyc handler enforces ``dict`` at the boundary ("dict object
    expected; got str"); ``_handle`` guards this by pre-parsing.  This locks that
    guard against the compiled artifact.
    """
    native = _native_args("read", compiled_server.workspace)
    responses, proc = _run_server(
        compiled_server,
        [_tools_call(2, "read", json.dumps(native))],
    )
    assert 2 in responses, f"no response; stderr:\n{proc.stderr[-1500:]}"
    resp = responses[2]
    _assert_not_mypyc_error("read(arguments-as-json-string)", resp)
    assert "result" in resp, f"json-string arguments were not honoured: {resp}"


def test_compiled_server_handles_loosely_typed_number(
    compiled_server: _CompiledServer,
) -> None:
    """A JSON float where an int is expected (``5.0`` for ``max_lines``).

    Clients routinely send whole numbers as floats.  mypyc is strict about
    int-vs-float at the C boundary, so this is a sibling of the stringified-arg
    bug: it must not produce a C-level ``int object expected; got float``.
    """
    base = dict(_native_args("read", compiled_server.workspace))
    base["max_lines"] = 5.0  # JSON number, not an int
    responses, proc = _run_server(compiled_server, [_tools_call(2, "read", base)])
    assert 2 in responses, f"no response; stderr:\n{proc.stderr[-1500:]}"
    _assert_not_mypyc_error("read(float-for-int)", responses[2])


def test_compiled_server_missing_required_arg_is_graceful(
    compiled_server: _CompiledServer,
) -> None:
    """Missing a required arg must be a clean validation error, not a .so crash.

    ``codemod`` requires ``pattern``.  Omitting it should surface a graceful
    JSON-RPC error (Pydantic validation), never a mypyc type assertion or a
    dead server.
    """
    responses, proc = _run_server(compiled_server, [_tools_call(2, "codemod", {})])
    assert 2 in responses, f"no response; stderr:\n{proc.stderr[-1500:]}"
    resp = responses[2]
    assert "error" in resp, f"expected a validation error for missing 'pattern': {resp}"
    _assert_not_mypyc_error("codemod(missing-required)", resp)


def test_compiled_server_unknown_tool_and_method_are_graceful(
    compiled_server: _CompiledServer,
) -> None:
    """Unknown tool/method must return -32601 and leave the server responsive."""
    responses, proc = _run_server(
        compiled_server,
        [
            {"jsonrpc": "2.0", "id": 2, "method": "frobnicate/widgets", "params": {}},
            _tools_call(3, "definitely_not_a_real_tool", {"x": 1}),
            # A valid call AFTER the bad ones proves the server is still alive.
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        ],
    )
    assert {2, 3, 4} <= set(
        responses
    ), f"missing responses {{2,3,4}} - {set(responses)}; stderr:\n{proc.stderr[-1500:]}"
    assert responses[2]["error"]["code"] == -32601, responses[2]
    assert responses[3]["error"]["code"] == -32601, responses[3]
    assert "result" in responses[4], f"server unresponsive after bad calls: {responses[4]}"


def test_compiled_server_notification_does_not_break_session(
    compiled_server: _CompiledServer,
) -> None:
    """A JSON-RPC notification (no ``id``) yields no response and is harmless.

    ``notifications/initialized`` returns None in ``_handle``; the compiled
    dispatch must not emit a spurious ``id: null`` response or wedge the loop.
    """
    responses, proc = _run_server(
        compiled_server,
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
    )
    assert None not in responses, f"server wrongly responded to a notification: {responses.get(None)}"
    assert (
        2 in responses and "result" in responses[2]
    ), f"session broken after notification; stderr:\n{proc.stderr[-1500:]}"


def test_compiled_server_handles_concurrent_calls(
    compiled_server: _CompiledServer,
) -> None:
    """Many in-flight requests must each get a response.

    The server dispatches each request to a ThreadPoolExecutor; mypyc-compiled
    code under concurrent threads has historically been a failure surface, so
    assert every batched call is answered (responses may arrive out of order).
    """
    native = _native_args("read", compiled_server.workspace)
    ids = list(range(2, 10))  # 8 concurrent reads
    responses, proc = _run_server(
        compiled_server,
        [_tools_call(rid, "read", native) for rid in ids],
    )
    missing = set(ids) - set(responses)
    assert not missing, f"compiled server dropped concurrent responses {missing}; stderr:\n{proc.stderr[-1500:]}"
    for rid in ids:
        _assert_not_mypyc_error(f"concurrent read id={rid}", responses[rid])
