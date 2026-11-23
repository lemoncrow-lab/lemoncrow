import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const helper = fileURLToPath(new URL("./lemoncrow_nudge.py", import.meta.url));
const canImportLemonCrow = (python) =>
  spawnSync(python, ["-c", "import lemoncrow"], { encoding: "utf8" }).status ===
  0;

// Mirrors integrations/claude/plugin/scripts/_lemoncrow_python.sh: lemoncrow is
// normally installed in an isolated uv-tool venv, so bare `python3` cannot
// import it. Resolution order: $LEMONCROW_PYTHON -> lc wrapper shebang ->
// uv tool default venv -> python3 fallback.
const resolvePython = () => {
  const override = process.env.LEMONCROW_PYTHON;
  if (override && canImportLemonCrow(override)) return override;
  try {
    const which = spawnSync("sh", ["-c", "command -v lc"], {
      encoding: "utf8",
    });
    const wrapper = (which.stdout ?? "").trim();
    if (which.status === 0 && wrapper) {
      const firstLine = readFileSync(wrapper, "utf8").split("\n", 1)[0];
      if (firstLine.startsWith("#!")) {
        const shebang = firstLine.slice(2).trim();
        if (shebang && canImportLemonCrow(shebang)) return shebang;
      }
    }
  } catch {
    // Fall through to the uv tool default paths.
  }
  const home = process.env.HOME ?? "";
  for (const py of [
    `${home}/.local/share/uv/tools/lemoncrow/bin/python`,
    `${home}/.local/share/uv/tools/lemoncrow/bin/python3`,
  ]) {
    if (canImportLemonCrow(py)) return py;
  }
  return "python3";
};

let pythonBin;

const invokeHelper = (payload) => {
  pythonBin ??= resolvePython();
  const result = spawnSync(pythonBin, [helper], {
    input: JSON.stringify(payload),
    encoding: "utf8",
  });
  if (result.status !== 0 || !result.stdout.trim()) return {};
  try {
    return JSON.parse(result.stdout);
  } catch {
    return {};
  }
};


// OpenCode V2 native plugin. Do not load this file in OpenCode V1.
// Its Python helper deliberately remains shared with the legacy OpenCode bridge.
const failures = new Map();
const pendingRescue = new Set();
const phases = new Map();
const pendingHints = new Map();
const continued = new Set();

const phaseCaps = {
  cheap: { explore: 900, execute: 2800, repair: 3600, finish: 900 },
  balanced: { explore: 1200, execute: 4096, repair: 5200, finish: 1200 },
  best: { explore: 1800, execute: 6144, repair: 7600, finish: 1600 },
};
const tokenBudget = ["cheap", "balanced", "best"].includes(process.env.LEMONCROW_CODE_BUDGET)
  ? process.env.LEMONCROW_CODE_BUDGET : "balanced";
const verifies = (args) =>
  /(pytest|npm test|pnpm test|yarn test|cargo test|go test|make (test|check)|ruff check|mypy|pyright|tsc\b)/i.test(
    Array.isArray(args?.command) ? args.command.join(" ") : String(args?.command ?? ""),
  );

const textOf = (result) => {
  if (typeof result === "string") return result;
  if (!result || typeof result !== "object") return "";
  if (typeof result.output === "string") return result.output;
  if (typeof result.content === "string") return result.content;
  if (Array.isArray(result.content)) return result.content
    .filter((part) => part?.type === "text").map((part) => part.text ?? "").join("\n");
  return "";
};
const failedKey = (event) => {
  const code = event.result?.metadata?.exitCode ?? event.result?.metadata?.exit_code;
  const output = textOf(event.result);
  const failed = event.status === "error" || (typeof code === "number" && code !== 0)
    || /(^|\n)(error|failed|traceback):/i.test(output);
  if (!failed) return null;
  const message = textOf(event.error) || event.error?.message || output;
  return `${event.tool}:${JSON.stringify(event.input ?? {})}\n${String(message).trim().split("\n").slice(-4).join("\n")}`;
};
const sessionOf = (event) => event?.sessionID ?? event?.properties?.sessionID ?? event?.data?.sessionID ?? "";
const modelOf = (event) => event?.model?.id ?? event?.model?.modelID ?? event?.model ?? "";
const directoryOf = (ctx, event) => event?.directory ?? event?.location?.directory ?? ctx.location?.directory ?? process.cwd();
const queueHint = (sessionID, hint) => {
  if (!sessionID || !hint?.trim()) return;
  pendingHints.set(sessionID, [...(pendingHints.get(sessionID) ?? []), hint]);
};

export default {
  id: "lemoncrow.nudge",
  async setup(ctx) {
    await ctx.session.hook("prompt", async (event) => {
      const sessionID = sessionOf(event);
      if (!sessionID) return;
      if (!phases.has(sessionID)) phases.set(sessionID, "explore");
      const prompt = event.prompt?.text ?? "";
      if (pendingRescue.delete(sessionID)) {
        queueHint(sessionID, "<lc-nudge>\nThis command failed twice with the same error. Call 'rescue' before retrying; do not repeat the same fix.\n</lc-nudge>");
      }
      const nudge = invokeHelper({event: "prompt", session_id: sessionID, prompt,
        cwd: directoryOf(ctx, event), model: modelOf(event)});
      if (nudge.additionalContext?.trim()) queueHint(sessionID, nudge.additionalContext);
      // The prompt is user-owned. Inject advice at model-context time, not as a
      // durable rewrite of the user's message or on provider title requests.
      if (nudge.uiMessage?.trim()) queueHint(sessionID, `<lc-nudge>${nudge.uiMessage}</lc-nudge>`);
    });
    await ctx.session.hook("context", (event) => {
      const sessionID = sessionOf(event);
      const cap = phaseCaps[tokenBudget][phases.get(sessionID) ?? "explore"];
      const previous = event.options?.maxTokens;
      event.options.maxTokens = typeof previous === "number" && previous > 0
        ? Math.min(previous, cap) : cap;
      const hints = pendingHints.get(sessionID);
      if (hints?.length) {
        event.system.push({type: "text", text: hints.join("\n\n")});
        pendingHints.delete(sessionID);
      }
    });
    await ctx.tool.hook("execute.after", async (event) => {
      const sessionID = sessionOf(event);
      if (!sessionID) return;
      const result = event.status === "error"
        ? {output: String(event.error?.message ?? "tool failed"), metadata: {exitCode: 1}}
        : {output: textOf(event.result), metadata: event.result?.metadata ?? {}};
      const nudge = invokeHelper({event: "post_tool", session_id: sessionID,
        tool_name: event.tool, tool_input: event.input, tool_response: result,
        cwd: directoryOf(ctx, event), model: modelOf(event)});
      if (nudge.additionalContext?.trim()) queueHint(sessionID, nudge.additionalContext);
      if (nudge.uiMessage?.trim()) queueHint(sessionID, `<lc-nudge>${nudge.uiMessage}</lc-nudge>`);
      const key = failedKey(event);
      if (key) {
        phases.set(sessionID, "repair");
        const previous = failures.get(sessionID) ?? new Map();
        const count = (previous.get(key) ?? 0) + 1;
        previous.set(key, count);
        failures.set(sessionID, previous);
        if (count >= 2) pendingRescue.add(sessionID);
      } else if (/edit|write|patch|codemod/i.test(event.tool)) {
        phases.set(sessionID, "execute");
      } else if (/bash|shell|run/i.test(event.tool) && verifies(event.input)) {
        phases.set(sessionID, "finish");
      } else if (/read|grep|search|glob/i.test(event.tool)) {
        phases.set(sessionID, "execute");
      }
    });
    const controller = new AbortController();
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({signal: controller.signal})) {
          if (event?.type !== "session.idle") continue;
          const sessionID = sessionOf(event);
          if (!sessionID || continued.has(sessionID)) continue;
          const status = invokeHelper({event: "idle", session_id: sessionID,
            cwd: directoryOf(ctx, event), model: modelOf(event)});
          if (status.continuePrompt?.trim()) {
            continued.add(sessionID);
            try {
              // Unlike V1's three speculative SDK shapes, V2 has a documented method.
              await ctx.session.prompt({sessionID, text: status.continuePrompt});
            } catch {
              queueHint(sessionID, status.continuePrompt);
            } finally {
              continued.delete(sessionID);
            }
          }
          if (status.uiMessage?.trim()) queueHint(sessionID, `<lc-nudge>${status.uiMessage}</lc-nudge>`);
        }
      } catch (error) {
        if (!controller.signal.aborted) console.warn("LemonCrow OpenCode V2 event stream exited", error);
      }
    })();
    return () => {
      controller.abort();
      failures.clear(); pendingRescue.clear(); phases.clear(); pendingHints.clear(); continued.clear();
    };
  },
};
