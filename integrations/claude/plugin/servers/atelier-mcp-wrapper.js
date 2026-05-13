#!/usr/bin/env node
/**
 * atelier-mcp-wrapper.js
 *
 * Launches the local `atelier-mcp` stdio server pointing at the local Atelier
 * HTTP service. Works when the atelier package is
 * installed in any of:
 *   1. `${ATELIER_VENV}/bin/atelier-mcp`             (explicit venv)
 *   2. `${CLAUDE_WORKSPACE_ROOT}/atelier/.venv/bin/atelier-mcp`  (repo venv)
 *   3. `atelier-mcp` on PATH                          (global install)
 *
 * Forwards stdin/stdout/stderr unchanged.
 */

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

function resolveBinary() {
  const candidates = [];
  if (process.env.ATELIER_VENV) {
    candidates.push(path.join(process.env.ATELIER_VENV, "bin", "atelier-mcp"));
  }
  if (process.env.CLAUDE_WORKSPACE_ROOT) {
    candidates.push(
      path.join(
        process.env.CLAUDE_WORKSPACE_ROOT,
        "atelier",
        ".venv",
        "bin",
        "atelier-mcp",
      ),
    );
  }
  candidates.push("atelier-mcp"); // PATH lookup
  for (const c of candidates) {
    if (c === "atelier-mcp") return c;
    try {
      if (fs.existsSync(c)) return c;
    } catch {
      /* ignore */
    }
  }
  return "atelier-mcp";
}

function resolveWorkspaceRoot() {
  return (
    process.env.ATELIER_WORKSPACE_ROOT ||
    process.env.CLAUDE_WORKSPACE_ROOT ||
    process.cwd()
  );
}

function resolveServiceUrl() {
  return process.env.ATELIER_SERVICE_URL || "http://127.0.0.1:8787";
}

const bin = resolveBinary();
const serviceUrl = resolveServiceUrl();
const workspaceRoot = resolveWorkspaceRoot();

const child = spawn(bin, [], {
  stdio: ["inherit", "inherit", "inherit"],
  env: {
    ...process.env,
    ATELIER_SERVICE_URL: serviceUrl,
    ATELIER_WORKSPACE_ROOT: workspaceRoot,
  },
});

child.on("error", (err) => {
  process.stderr.write(
    `[atelier-mcp-wrapper] failed to start ${bin}: ${err.message}\n` +
      `Install atelier with:  cd atelier && uv sync\n` +
      `Or set ATELIER_VENV / put atelier-mcp on PATH.\n`,
  );
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});

["SIGTERM", "SIGINT"].forEach((sig) =>
  process.on(sig, () => child.kill(sig)),
);
