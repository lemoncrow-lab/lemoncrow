import { spawnSync } from "node:child_process"
import { fileURLToPath } from "node:url"

const helper = fileURLToPath(new URL("./atelier_nudge.py", import.meta.url))
const failures = new Map()
const pendingRescue = new Set()

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
    const result = spawnSync("python3", [helper], {
      input: JSON.stringify({
        session_id: input.sessionID,
        prompt,
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
      const modelMessages = []
      if (pendingRescue.delete(input.sessionID)) {
        modelMessages.push(
          "This command failed twice with the same error. Call 'rescue' before any retry; do not repeat the same fix.",
        )
      }
      if (modelMessages.length > 0) {
        textParts[textParts.length - 1].text += `\n\n<atelier-nudge>\n${modelMessages.join("\n")}\n</atelier-nudge>`
      }
    } catch {
      // Fail open: prompt submission must continue if the helper output is invalid.
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
