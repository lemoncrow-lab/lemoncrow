import { spawnSync } from "node:child_process"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

const helper = fileURLToPath(new URL("./atelier_nudge.py", import.meta.url))
const failures = new Map()
const pendingRescue = new Set()

const canImportAtelier = (python) =>
  spawnSync(python, ["-c", "import atelier"], { encoding: "utf8" }).status === 0

// Mirrors integrations/claude/plugin/scripts/_atelier_python.sh: atelier is
// normally installed in an isolated uv-tool venv, so bare `python3` cannot
// import it. Resolution order: $ATELIER_PYTHON -> atelier wrapper shebang ->
// uv tool default venv -> python3 fallback.
const resolvePython = () => {
  const override = process.env.ATELIER_PYTHON
  if (override && canImportAtelier(override)) return override
  try {
    const which = spawnSync("sh", ["-c", "command -v atelier"], { encoding: "utf8" })
    const wrapper = (which.stdout ?? "").trim()
    if (which.status === 0 && wrapper) {
      const firstLine = readFileSync(wrapper, "utf8").split("\n", 1)[0]
      if (firstLine.startsWith("#!")) {
        const shebang = firstLine.slice(2).trim()
        if (shebang && canImportAtelier(shebang)) return shebang
      }
    }
  } catch {
    // Fall through to the uv tool default paths.
  }
  const home = process.env.HOME ?? ""
  for (const py of [
    `${home}/.local/share/uv/tools/atelier/bin/python`,
    `${home}/.local/share/uv/tools/atelier/bin/python3`,
  ]) {
    if (canImportAtelier(py)) return py
  }
  return "python3"
}

let pythonBin

const failureKey = (input, output) => {
  const exitCode = output.metadata?.exitCode ?? output.metadata?.exit_code
  const failed =
    (typeof exitCode === "number" && exitCode !== 0) ||
    /(^|\n)(error|failed|traceback):/i.test(output.output)
  if (!failed) return null
  const command = input.args?.command ?? JSON.stringify(input.args ?? {})
  const error = output.output.trim().split("\n").slice(-4).join("\n")
  return `${command}\n${error}`
}

export const AtelierNudge = async ({ client, directory }) => ({
  "chat.message": async (input, output) => {
    const textParts = output.parts.filter(
      (part) => part.type === "text" && typeof part.text === "string" && !part.synthetic,
    )
    if (textParts.length === 0) return

    const prompt = textParts.map((part) => part.text).join("\n")

    // JS-only rescue nudge: needs no Python and no TUI, so it must fire even
    // when the helper exits non-zero or the toast call throws.
    if (pendingRescue.delete(input.sessionID)) {
      textParts[textParts.length - 1].text +=
        "\n\n<atelier-nudge>\nThis command failed twice with the same error. Call 'rescue' before any retry; do not repeat the same fix.\n</atelier-nudge>"
    }

    pythonBin ??= resolvePython()
    const result = spawnSync(pythonBin, [helper], {
      input: JSON.stringify({
        session_id: input.sessionID,
        prompt,
        cwd: directory,
      }),
      encoding: "utf8",
    })
    if (result.status !== 0) return

    try {
      const nudge = result.stdout.trim() ? JSON.parse(result.stdout) : {}
      if (typeof nudge.uiMessage === "string" && nudge.uiMessage.trim()) {
        await client.tui.showToast({
          body: {
            title: "Atelier",
            message: nudge.uiMessage
              .replace("Atelier context guard: high context", "Context high")
              .replace("consider compacting", "run /compact"),
            variant: "warning",
            duration: 8000,
          },
          query: { directory },
        })
      }
    } catch {
      // Fail open: prompt submission must continue if the helper output is
      // invalid or the toast fails (e.g. non-TUI serve mode).
    }
  },
  "tool.execute.after": async (input, output) => {
    const key = failureKey(input, output)
    if (!key) return
    const sessionFailures = failures.get(input.sessionID) ?? new Map()
    const count = (sessionFailures.get(key) ?? 0) + 1
    sessionFailures.set(key, count)
    failures.set(input.sessionID, sessionFailures)
    if (count >= 2) pendingRescue.add(input.sessionID)
  },
  })
