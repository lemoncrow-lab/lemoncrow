import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import managedPi, {
  createStatusSnapshotReader,
  formatStatusLines,
  messageContainsToolCall,
  sanitizeOpenAIRequest,
} from "../../../integrations/pi/managed.mjs";

function withManagedEnv(overrides, fn) {
  const keys = [
    "LEMONCROW_PI_GATEWAY_BASE_URL",
    "LEMONCROW_PI_GATEWAY_TOKEN",
    "LEMONCROW_PI_MODELS",
    "LEMONCROW_STATUS_FILE",
  ];
  const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  Object.assign(process.env, {
    LEMONCROW_PI_GATEWAY_BASE_URL: "http://127.0.0.1:43210/v1",
    LEMONCROW_PI_GATEWAY_TOKEN: "secret",
    LEMONCROW_PI_MODELS: JSON.stringify({
      "zen/big-pickle": { name: "zen · big-pickle", limit: { context: 200000, output: 5200 } },
    }),
    ...overrides,
  });
  for (const [key, value] of Object.entries(overrides || {})) {
    if (value === undefined) delete process.env[key];
  }
  return Promise.resolve(fn()).finally(() => {
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
  });
}

function fakePi() {
  const handlers = new Map();
  const providers = [];
  const activeTools = [];
  return {
    handlers,
    providers,
    activeTools,
    registerProvider(id, config) {
      providers.push({ id, config });
    },
    on(name, handler) {
      assert.equal(handlers.has(name), false, `duplicate handler for ${name}`);
      handlers.set(name, handler);
    },
    setActiveTools(tools) {
      activeTools.push([...tools]);
    },
  };
}

test("sanitizeOpenAIRequest removes host prompt and tools but preserves conversation content", () => {
  const image = { type: "image_url", image_url: { url: "data:image/png;base64,abc" } };
  const payload = {
    model: "zen/big-pickle",
    tools: [{ type: "function", function: { name: "bash" } }],
    tool_choice: "auto",
    parallel_tool_calls: true,
    messages: [
      { role: "system", content: "pi system prompt" },
      { role: "developer", content: "project instructions" },
      { role: "user", content: "first" },
      { role: "assistant", content: "safe answer" },
      { role: "assistant", content: "tool turn", tool_calls: [{ id: "x" }] },
      { role: "tool", content: "secret tool output" },
      { role: "user", content: [{ type: "text", text: "latest" }, image] },
    ],
  };

  const sanitized = sanitizeOpenAIRequest(payload);
  assert.equal(sanitized.tools, undefined);
  assert.equal(sanitized.tool_choice, undefined);
  assert.equal(sanitized.parallel_tool_calls, undefined);
  assert.deepEqual(sanitized.messages, [
    { role: "user", content: "first" },
    { role: "assistant", content: "safe answer" },
    { role: "user", content: [{ type: "text", text: "latest" }, image] },
  ]);
  assert.equal(JSON.stringify(sanitized).includes("secret tool output"), false);
  assert.equal(JSON.stringify(sanitized).includes("pi system prompt"), false);
});

test("managed extension registers one provider and every fail-closed hook", async () => {
  await withManagedEnv({}, async () => {
    const pi = fakePi();
    await managedPi(pi);

    assert.equal(pi.providers.length, 1);
    assert.equal(pi.providers[0].id, "lc");
    assert.equal(pi.providers[0].config.baseUrl, "http://127.0.0.1:43210/v1");
    assert.equal(pi.providers[0].config.apiKey, "$LEMONCROW_PI_GATEWAY_TOKEN");
    assert.deepEqual(pi.providers[0].config.models.map((model) => model.id), ["zen/big-pickle"]);

    assert.deepEqual(pi.handlers.get("project_trust")(), { trusted: "no" });
    assert.match(pi.handlers.get("before_agent_start")().systemPrompt, /LemonCrow managed frontend/);
    assert.deepEqual(pi.handlers.get("session_before_compact")(), { cancel: true });

    let messageAborted = false;
    pi.handlers.get("message_end")(
      { message: { role: "assistant", content: [{ type: "toolCall", name: "bash", arguments: {} }] } },
      { abort: () => { messageAborted = true; } },
    );
    assert.equal(messageAborted, true);
    assert.equal(messageContainsToolCall({ role: "assistant", content: [{ type: "text", text: "safe" }] }), false);

    let aborted = false;
    const blockedTool = pi.handlers.get("tool_call")({ toolName: "bash" }, { abort: () => { aborted = true; } });
    assert.equal(blockedTool.block, true);
    assert.equal(aborted, true);
    assert.match(blockedTool.reason, /LemonCrow owns tools/);

    const blockedBash = pi.handlers.get("user_bash")();
    assert.equal(blockedBash.result.exitCode, 126);
    assert.match(blockedBash.result.output, /disables local shell execution/);
  });
});

test("managed extension fails before provider registration when required env is absent", async () => {
  await withManagedEnv({ LEMONCROW_PI_GATEWAY_TOKEN: undefined }, async () => {
    const pi = fakePi();
    await assert.rejects(() => managedPi(pi), /requires LEMONCROW_PI_GATEWAY_TOKEN/);
    assert.equal(pi.providers.length, 0);
  });

  await withManagedEnv({ LEMONCROW_PI_MODELS: "{" }, async () => {
    const pi = fakePi();
    await assert.rejects(() => managedPi(pi), /invalid LEMONCROW_PI_MODELS/);
    assert.equal(pi.providers.length, 0);
  });
});

test("status reader retains the last valid atomic snapshot", () => {
  const root = mkdtempSync(join(tmpdir(), "lemoncrow-pi-status-"));
  const path = join(root, "status.json");
  try {
    const read = createStatusSnapshotReader(path);
    assert.equal(read(), undefined);

    const valid = { provider: "zen", model: "big-pickle", context_tokens: 6234 };
    writeFileSync(path, JSON.stringify(valid));
    assert.deepEqual(read(), valid);

    writeFileSync(path, "{not-complete");
    assert.deepEqual(read(), valid);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("formatStatusLines exposes LemonCrow-owned usage including context", () => {
  const lines = formatStatusLines({
    provider: "zen",
    model: "big-pickle",
    input_tokens: 1234,
    cache_read_tokens: 5000,
    cache_write_tokens: 500,
    output_tokens: 321,
    context_tokens: 6734,
    cost_usd: 0.0123,
    saved_usd: 0.02,
    tool_call_total: 4,
    mcp_calls: 2,
    index: { present: true, files: 1200, symbols: 9000 },
  });
  assert.match(lines[0], /zen\/big-pickle/);
  assert.ok(lines.some((line) => line.includes("7k ctx")));
  assert.ok(lines.some((line) => line.includes("$0.0200 saved")));
  assert.ok(lines.some((line) => line.includes("4 tool calls")));
  assert.ok(lines.some((line) => line.includes("Index")));
});
