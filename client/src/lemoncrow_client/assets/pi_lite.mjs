import { spawn } from "node:child_process";

const EXPECTED_TOOLS = ["bash", "code_search", "edit", "read", "web_fetch"];
// The installer pins the CLI invocation in the copy registered in Pi settings.
const installedInvocation = null;

function requiredEnv(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Workspaces Lite Pi integration requires ${name}`);
  return value;
}

function parseArgs() {
  try {
    const configured = process.env.LEMONCROW_PI_LITE_ARGS;
    const value = configured ? JSON.parse(configured) : installedInvocation?.args;
    if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) throw new Error("not a string array");
    return value;
  } catch (error) {
    throw new Error(`invalid LEMONCROW_PI_LITE_ARGS: ${error?.message || error}`);
  }
}

class LiteRpc {
  constructor(command, args) {
    this.nextId = 1;
    this.pending = new Map();
    this.stderr = "";
    this.process = spawn(command, args, {
      cwd: process.cwd(),
      env: { ...process.env, LEMONCROW_AGENT: "pi", LEMONCROW_RUNTIME_LITE: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let buffer = "";
    this.process.stdout.setEncoding("utf8");
    this.process.stdout.on("data", (chunk) => {
      buffer += chunk;
      for (;;) {
        const newline = buffer.indexOf("\n");
        if (newline < 0) break;
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        let message;
        try { message = JSON.parse(line); } catch { continue; }
        const pending = this.pending.get(message.id);
        if (!pending) continue;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message || "Workspaces Lite RPC error"));
        else pending.resolve(message.result);
      }
    });
    this.process.stderr.setEncoding("utf8");
    this.process.stderr.on("data", (chunk) => {
      this.stderr = (this.stderr + chunk).slice(-4000);
    });
    this.process.on("error", (error) => {
      for (const pending of this.pending.values()) pending.reject(error);
      this.pending.clear();
    });
    this.process.stdin.on("error", () => {});
    this.process.on("exit", (code, signal) => {
      const detail = this.stderr.trim();
      const error = new Error(`Workspaces Lite MCP exited (${code ?? signal ?? "unknown"})${detail ? `: ${detail}` : ""}`);
      for (const pending of this.pending.values()) pending.reject(error);
      this.pending.clear();
    });
  }

  call(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n", (error) => {
        if (!error) return;
        this.pending.delete(id);
        reject(error);
      });
    });
  }

  close() {
    try { this.process.stdin.end(); } catch {}
    if (!this.process.killed) this.process.kill("SIGTERM");
  }
}

export default async function runtimeLitePi(pi) {
  const command = process.env.LEMONCROW_PI_LITE_COMMAND?.trim() || installedInvocation?.command;
  const rpc = new LiteRpc(command || requiredEnv("LEMONCROW_PI_LITE_COMMAND"), parseArgs());
  let byName;
  try {
    await rpc.call("initialize", {});
    const listed = await rpc.call("tools/list", {});
    const tools = Array.isArray(listed?.tools) ? listed.tools : [];
    byName = new Map(tools.map((tool) => [tool?.name, tool]));
    const missing = EXPECTED_TOOLS.filter((name) => !byName.has(name));
    if (missing.length) throw new Error(`Workspaces Lite Pi integration missing tools: ${missing.join(", ")}`);
  } catch (error) {
    rpc.close();
    // The installed copy loads on every Pi launch. Outside a Git worktree, or
    // after lc moved, Pi must still start, just without LemonCrow tools; Pi
    // treats a throwing extension as fatal.
    if (installedInvocation) {
      console.error(`LemonCrow Workspaces Lite is not available here: ${error?.message ?? error}`);
      return;
    }
    throw error;
  }

  for (const name of EXPECTED_TOOLS) {
    const tool = byName.get(name);
    pi.registerTool({
      name,
      label: `LemonCrow ${name}`,
      description: tool.description || `LemonCrow Workspaces Lite ${name}`,
      parameters: tool.inputSchema || { type: "object", additionalProperties: true },
      async execute(_toolCallId, params) {
        const result = await rpc.call("tools/call", { name, arguments: params || {} });
        const content = Array.isArray(result?.content)
          ? result.content
          : [{ type: "text", text: String(result ?? "") }];
        return { content, details: { lemoncrowRuntimeLite: true, isError: Boolean(result?.isError) } };
      },
    });
  }

  // Do not call setActiveTools here. Runtime Lite is additive for public Pi,
  // just like lc with Claude/Codex/OpenCode: Pi keeps its own model, provider,
  // session, prompts and non-conflicting native capabilities. LemonCrow's tools
  // override same-named tools only where Pi's extension API naturally does so.
  pi.on("session_shutdown", () => rpc.close());
}
