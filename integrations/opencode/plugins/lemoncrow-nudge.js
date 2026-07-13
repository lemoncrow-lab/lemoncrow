import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const helper = fileURLToPath(new URL("./lemoncrow_nudge.py", import.meta.url));
const failures = new Map();
const pendingRescue = new Set();

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

const showToast = async (client, message, title = "lc", duration = 8000) => {
  if (!message?.trim()) return;
  try {
    await client.tui.showToast({
      body: { title, message, variant: "warning", duration },
    });
  } catch {
    // Fail open when the TUI is unavailable.
  }
};

// Re-drive an idle session with a follow-up instruction -- verify-before-done's
// emulated block, since OpenCode exposes no Stop-block hook. Tries the known
// @opencode-ai/sdk client shapes and fails open: if none work the caller falls
// back to a toast, so this never throws. The Python gate's fire-once dedup means
// a given nudge is emitted at most once, so re-driving cannot loop.
const continueSession = async (client, sessionID, text) => {
  if (!sessionID || !text?.trim()) return false;
  const attempts = [
    () =>
      client.session.prompt({
        path: { id: sessionID },
        body: { parts: [{ type: "text", text }] },
      }),
    () => client.session.prompt({ sessionID, parts: [{ type: "text", text }] }),
    () =>
      client.session.chat({
        path: { id: sessionID },
        body: { parts: [{ type: "text", text }] },
      }),
  ];
  for (const attempt of attempts) {
    try {
      await attempt();
      return true;
    } catch {
      // Try the next client shape.
    }
  }
  return false;
};

const eventSessionID = (event) =>
  event?.sessionID ??
  event?.sessionId ??
  event?.properties?.sessionID ??
  event?.properties?.sessionId ??
  "";

const failureKey = (input, output) => {
  const exitCode = output.metadata?.exitCode ?? output.metadata?.exit_code;
  const failed =
    (typeof exitCode === "number" && exitCode !== 0) ||
    /(^|\n)(error|failed|traceback):/i.test(output.output);
  if (!failed) return null;
  const command = input.args?.command ?? JSON.stringify(input.args ?? {});
  const error = output.output.trim().split("\n").slice(-4).join("\n");
  return `${command}\n${error}`;
};

export const LemonCrowNudge = async ({ client, directory }) => ({
  "chat.message": async (input, output) => {
    const textParts = output.parts.filter(
      (part) =>
        part.type === "text" &&
        typeof part.text === "string" &&
        !part.synthetic,
    );
    if (textParts.length === 0) return;

    const prompt = textParts.map((part) => part.text).join("\n");

    // JS-only rescue nudge: needs no Python and no TUI, so it must fire even
    // when the helper exits non-zero or the toast call throws.
    if (pendingRescue.delete(input.sessionID)) {
      textParts[textParts.length - 1].text +=
        "\n\n<lc-nudge>\nThis command failed twice with the same error. Call 'rescue' before any retry; do not repeat the same fix.\n</lc-nudge>";
    }

    const nudge = invokeHelper({
      event: "prompt",
      session_id: input.sessionID,
      prompt,
      cwd: directory,
      model: input.model,
    });
    await showToast(
      client,
      nudge.uiMessage
        ?.replace("LemonCrow context guard: high context", "Context high")
        .replace("consider compacting", "run /compact"),
    );
  },
  "tool.execute.after": async (input, output) => {
    const nudge = invokeHelper({
      event: "post_tool",
      session_id: input.sessionID,
      tool_name: input.tool,
      tool_input: input.args,
      tool_response: output,
      cwd: directory,
      model: input.model,
    });
    await showToast(client, nudge.uiMessage);
    const key = failureKey(input, output);
    if (!key) return;
    const sessionFailures = failures.get(input.sessionID) ?? new Map();
    const count = (sessionFailures.get(key) ?? 0) + 1;
    sessionFailures.set(key, count);
    failures.set(input.sessionID, sessionFailures);
    if (count >= 2) pendingRescue.add(input.sessionID);
  },
  event: async ({ event }) => {
    if (event?.type !== "session.idle") return;
    const sessionID = eventSessionID(event);
    if (!sessionID) return;
    const status = invokeHelper({
      event: "idle",
      session_id: sessionID,
      cwd: directory,
      model: event?.model,
    });
    // Verify-before-done "block": re-drive the session with the FIXME so the
    // agent keeps going (parity with Claude/Codex Stop). Fall back to a toast
    // if the client can't be driven.
    if (status.continuePrompt) {
      const continued = await continueSession(
        client,
        sessionID,
        status.continuePrompt,
      );
      if (!continued)
        await showToast(client, status.continuePrompt, "lc verify", 12000);
    }
    await showToast(client, status.uiMessage, "lc status", 12000);
  },
});
