export const HOST_SNIPPETS: Record<string, { title: string; body: string }> = {
  claude: {
    title: "Claude Code MCP snippet",
    body: `"lc": {\n  "command": "uv",\n  "args": ["run", "lc mcp"],\n  "cwd": "/path/to/repo/lemoncrow",\n  "env": {}\n}`,
  },
  codex: {
    title: "Codex MCP snippet",
    body: `"lc": {\n  "command": "uv",\n  "args": ["run", "lc mcp"],\n  "cwd": "/path/to/repo/lemoncrow",\n  "env": {}\n}`,
  },
  copilot: {
    title: "Copilot MCP snippet",
    body: `"lc": {\n  "command": "uv",\n  "args": ["run", "lc mcp"],\n  "cwd": "/path/to/repo/lemoncrow",\n  "env": {}\n}`,
  },
  opencode: {
    title: "OpenCode MCP snippet",
    body: `"lc": {\n  "command": "uv",\n  "args": ["run", "lc mcp"],\n  "cwd": "/path/to/repo/lemoncrow",\n  "env": {}\n}`,
  },
  lemoncode: {
    // Same opencode.json shape; global config lives in ~/.config/lemoncode.
    title: "LemonCode MCP snippet (~/.config/lemoncode/opencode.json)",
    body: `"lc": {\n  "command": "lemoncrow",\n  "args": ["mcp", "--host", "lemoncode"],\n  "cwd": "/path/to/repo/lemoncrow",\n  "env": {}\n}`,
  },
  gemini: {
    title: "Gemini CLI MCP snippet",
    body: `"lc": {\n  "command": "uv",\n  "args": ["run", "lc mcp"],\n  "cwd": "/path/to/repo/lemoncrow",\n  "env": {}\n}`,
  },
};
