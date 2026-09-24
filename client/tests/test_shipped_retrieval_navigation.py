"""Exercise current MCP navigation and warm hydration against both real stores."""

from pathlib import Path

from lemoncrow_client.mcpserver import McpServer
from test_shipped_server_search import shipped as shipped

from benchmarks.codebench.retrieval_wire import code_search_markdown_paths


def test_current_mcp_keeps_ten_hits_and_reuses_verified_navigation(shipped, monkeypatch):
    files = {f"file_{i:02}.py": "def issue_token():\n    return 1\n" for i in range(10)}
    session, dispatcher, repo = shipped("alice", files)
    mcp = McpServer(dispatcher._context.config, session)
    reads = []
    original = Path.read_bytes

    def counted(path):
        if path.parent == repo and path.suffix == ".py":
            reads.append(path.name)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted)

    def search():
        response = mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "code_search", "arguments": {"query": "issue_token", "limit": 10}},
            }
        )
        assert response is not None
        result = response["result"]
        assert not result.get("isError"), result
        assert "structuredContent" not in result
        text = "\n".join(block["text"] for block in result["content"] if block["type"] == "text")
        assert code_search_markdown_paths(text) == sorted(files)
        return text

    cold = search()
    assert len(reads) == 10
    reads.clear()
    assert search() == cold
    assert len(reads) == 2
