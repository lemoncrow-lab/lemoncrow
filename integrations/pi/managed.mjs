import { existsSync, readFileSync, watch } from "node:fs";
import { basename, dirname } from "node:path";

const STATUS_FILE_ENV = "LEMONCROW_STATUS_FILE";
const PROVIDER_ID = "lc";
const CARRIER_PROMPT = "LemonCrow managed frontend. The local gateway owns model reasoning and tool execution.";

function requiredEnv(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`managed Pi requires ${name}`);
  return value;
}

function modelDefinitions(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  return Object.entries(raw).map(([id, value]) => {
    const item = value && typeof value === "object" ? value : {};
    const limit = item.limit && typeof item.limit === "object" ? item.limit : {};
    return {
      id,
      name: typeof item.name === "string" ? item.name : id,
      reasoning: true,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: Number(limit.context || 200000),
      maxTokens: Number(limit.output || 5200),
    };
  });
}


export function messageContainsToolCall(message) {
  return Boolean(
    message &&
      message.role === "assistant" &&
      Array.isArray(message.content) &&
      message.content.some((part) => part && typeof part === "object" && part.type === "toolCall"),
  );
}

export function sanitizeOpenAIRequest(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const out = { ...source };
  delete out.tools;
  delete out.tool_choice;
  delete out.parallel_tool_calls;
  if (Array.isArray(source.messages)) {
    out.messages = source.messages.flatMap((message) => {
      if (!message || typeof message !== "object") return [];
      if (message.role !== "user" && message.role !== "assistant") return [];
      if (message.role === "assistant" && Array.isArray(message.tool_calls) && message.tool_calls.length) return [];
      const clean = { role: message.role, content: message.content ?? "" };
      if (message.name) clean.name = message.name;
      return [clean];
    });
  }
  return out;
}

function compactNumber(value) {
  const number = Number(value || 0);
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
  if (number >= 1_000) return `${Math.round(number / 1_000)}k`;
  return String(number);
}

export function formatStatusLines(snapshot) {
  const item = snapshot && typeof snapshot === "object" ? snapshot : {};
  const route = [item.provider, item.model].filter(Boolean).join("/");
  const context = Number(
    item.context_tokens ??
      Number(item.input_tokens || 0) + Number(item.cache_read_tokens || 0) + Number(item.cache_write_tokens || 0),
  );
  const lines = [];
  if (route) lines.push(`LemonCrow · ${route}`);
  lines.push(
    `I ${compactNumber(item.input_tokens)} · C ${compactNumber(item.cache_read_tokens)} · O ${compactNumber(item.output_tokens)} · ${compactNumber(context)} ctx · $${Number(item.cost_usd || 0).toFixed(4)} spent`,
  );
  if (Number(item.saved_usd || 0) > 0) lines.push(`$${Number(item.saved_usd).toFixed(4)} saved`);
  if (Number(item.tool_call_total || 0) > 0) {
    lines.push(`${Number(item.tool_call_total)} tool calls · ${Number(item.mcp_calls || 0)} MCP`);
  }
  if (item.index?.present) {
    lines.push(`Index · ${compactNumber(item.index.files)} files · ${compactNumber(item.index.symbols)} symbols`);
  }
  return lines;
}

export function createStatusSnapshotReader(path) {
  let lastValid;
  return () => {
    try {
      if (!existsSync(path)) return lastValid;
      const parsed = JSON.parse(readFileSync(path, "utf8"));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) lastValid = parsed;
    } catch {
      // The gateway replaces the sidecar atomically. If a watcher fires during
      // a transient/incomplete read, keep showing the last valid snapshot.
    }
    return lastValid;
  };
}

export default async function managedPi(pi) {
  const baseUrl = requiredEnv("LEMONCROW_PI_GATEWAY_BASE_URL");
  requiredEnv("LEMONCROW_PI_GATEWAY_TOKEN");
  let catalog;
  try {
    catalog = JSON.parse(requiredEnv("LEMONCROW_PI_MODELS"));
  } catch (error) {
    throw new Error(`invalid LEMONCROW_PI_MODELS: ${error?.message || error}`);
  }
  const models = modelDefinitions(catalog);
  if (models.length === 0) throw new Error("managed Pi model catalog is empty");

  pi.registerProvider(PROVIDER_ID, {
    name: "LemonCrow",
    baseUrl,
    apiKey: "$LEMONCROW_PI_GATEWAY_TOKEN",
    authHeader: true,
    api: "openai-completions",
    models,
  });

  pi.on("project_trust", () => ({ trusted: "no" }));
  pi.on("before_agent_start", () => ({ systemPrompt: CARRIER_PROMPT }));
  pi.on("before_provider_request", (event) => sanitizeOpenAIRequest(event.payload));
  pi.on("session_before_compact", () => ({ cancel: true }));
  pi.on("message_end", (event, ctx) => {
    // message_end is a Pi barrier before tool preflight. Abort here so even a
    // provider that hallucinates a tool call despite an empty tool catalog
    // cannot enter Pi's outer tool/retry loop.
    if (messageContainsToolCall(event.message) && typeof ctx?.abort === "function") ctx.abort();
  });
  pi.on("tool_call", (event, ctx) => {
    // Pi v0.84.2 can block a tool call but cannot terminate the turn from the
    // block result. Abort the agent context as well so a malicious/invalid
    // provider tool call cannot trigger a second outer-host model turn.
    if (typeof ctx?.abort === "function") ctx.abort();
    return {
      block: true,
      reason: `Managed Pi must not execute tool ${event.toolName}; LemonCrow owns tools`,
    };
  });
  pi.on("user_bash", () => ({
    result: {
      output: "Managed Pi disables local shell execution; use the LemonCrow-owned runtime instead.\n",
      exitCode: 126,
      cancelled: false,
      truncated: false,
    },
  }));

  let watcher;
  let watchedPath;
  const clearWatcher = () => {
    try { watcher?.close(); } catch {}
    watcher = undefined;
    watchedPath = undefined;
  };

  pi.on("session_start", (_event, ctx) => {
    pi.setActiveTools([]);
    clearWatcher();
    const path = process.env[STATUS_FILE_ENV]?.trim();
    if (!path || ctx.mode !== "tui") return;
    const readSnapshot = createStatusSnapshotReader(path);
    const refresh = () => {
      const snapshot = readSnapshot();
      if (!snapshot) return;
      const lines = formatStatusLines(snapshot);
      ctx.ui.setStatus("lemoncrow", lines[0] || "LemonCrow");
      ctx.ui.setWidget("lemoncrow", lines.slice(1), { placement: "belowEditor" });
    };
    refresh();
    try {
      watchedPath = path;
      watcher = watch(dirname(path), { persistent: false }, (_kind, filename) => {
        if (!filename || basename(path) === String(filename)) refresh();
      });
    } catch {
      clearWatcher();
    }
  });

  pi.on("session_shutdown", (_event, ctx) => {
    clearWatcher();
    if (ctx.mode === "tui") {
      ctx.ui.setStatus("lemoncrow", undefined);
      ctx.ui.setWidget("lemoncrow", undefined);
    }
  });
}
