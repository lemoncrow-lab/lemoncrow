export const HOST_SNIPPETS: Record<string, { title: string; body: string }> = {
  claude: {
    title: "Claude Code MCP snippet",
    body: `"atelier": {\n  "command": "uv",\n  "args": ["run", "atelier-mcp"],\n  "cwd": "/path/to/repo/atelier",\n  "env": {\n    "ATELIER_ROOT": ".atelier"\n  }\n}`,
  },
  codex: {
    title: "Codex MCP snippet",
    body: `"atelier": {\n  "command": "uv",\n  "args": ["run", "atelier-mcp"],\n  "cwd": "/path/to/repo/atelier",\n  "env": {\n    "ATELIER_ROOT": ".atelier"\n  }\n}`,
  },
  copilot: {
    title: "Copilot MCP snippet",
    body: `"atelier": {\n  "command": "uv",\n  "args": ["run", "atelier-mcp"],\n  "cwd": "/path/to/repo/atelier",\n  "env": {\n    "ATELIER_ROOT": ".atelier"\n  }\n}`,
  },
  opencode: {
    title: "OpenCode MCP snippet",
    body: `"atelier": {\n  "command": "uv",\n  "args": ["run", "atelier-mcp"],\n  "cwd": "/path/to/repo/atelier",\n  "env": {\n    "ATELIER_ROOT": ".atelier"\n  }\n}`,
  },
  gemini: {
    title: "Gemini CLI MCP snippet",
    body: `"atelier": {\n  "command": "uv",\n  "args": ["run", "atelier-mcp"],\n  "cwd": "/path/to/repo/atelier",\n  "env": {\n    "ATELIER_ROOT": ".atelier"\n  }\n}`,
  },
};
