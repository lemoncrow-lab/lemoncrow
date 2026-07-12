import json
import os
import subprocess

import pytest


@pytest.mark.slow
def test_mcp_stdio_smoke() -> None:
    """
    MCP stdio protocol smoke test.
    Sends JSON-RPC messages over stdin/stdout to lemoncrow mcp and asserts correct responses.
    """
    # Build the JSON-RPC batch
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "verify-script", "version": "1"},
                "capabilities": {},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "context",
                "arguments": {
                    "task": "Update Shopify product metafields",
                    "domain": "beseam.shopify.publish",
                    "tools": ["shopify.update_metafield"],
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "rescue",
                "arguments": {
                    "task": "fix test",
                    "error": "AssertionError: expected 200 got 500",
                    "attempt": 1,
                    "context": "pytest run",
                },
            },
        },
    ]

    input_str = "\n".join(json.dumps(m) for m in messages) + "\n"

    # Run lemoncrow mcp via uv run to ensure dependencies
    env = os.environ.copy()

    result = subprocess.run(
        ["uv", "run", "lemoncrow", "mcp"],
        input=input_str,
        text=True,
        capture_output=True,
        check=True,
        env=env,
        timeout=30,
    )

    responses = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strict framing gate (QBL-LOG-04): every non-empty stdout line MUST be a
        # JSON object. A stray print() now fails the test with its raw line.
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"non-protocol stdout line: {line!r}") from exc
        assert isinstance(msg, dict), f"non-protocol stdout line: {line!r}"
        if "id" in msg:
            responses[msg["id"]] = msg

    # 1. tools/list
    assert 2 in responses, "No tools/list response"
    tools_result = responses[2].get("result", {})
    tool_names = {t["name"] for t in tools_result.get("tools", [])}
    required = {
        "context",
        "route",
        "rescue",
        "trace",
        "verify",
        "memory",
        "read",
        "edit",
        "search",
        "compact",
    }
    missing = required - tool_names
    assert not missing, f"Missing tools: {missing}"

    # 2. reasoning (context tool) -> no error
    assert 4 in responses, "No reasoning (context) response"
    assert "error" not in responses[4], f"Unexpected error in context tool: {responses[4].get('error')}"

    # 3. rescue -> no error
    assert 5 in responses, "No rescue response"
    assert "error" not in responses[5], f"Unexpected error in rescue tool: {responses[5].get('error')}"
