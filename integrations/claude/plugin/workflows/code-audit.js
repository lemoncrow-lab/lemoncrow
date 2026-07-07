/**
 * code-audit.js
 *
 * Claude Code dynamic workflow for a multi-lens repository audit.
 *
 * This script is intentionally orchestration-only: it fans out three focused
 * audit passes (security, performance, tests) and then asks a final reviewer to
 * cross-check and consolidate the findings into one report.
 *
 * Dynamic workflows are in research preview and require Claude Code v2.1.154+.
 */

import { resolveClaudeRoleModel } from "./model-config.js"

const REVIEW_LENSES = [
  {
    name: "security",
    focus:
      "Review for auth bypasses, data exposure, injection risks, secrets handling, and unsafe shell or file operations.",
  },
  {
    name: "performance",
    focus:
      "Review for unnecessary work, N+1 patterns, hot-path allocations, expensive scans, and avoidable latency regressions.",
  },
  {
    name: "tests",
    focus:
      "Review for missing regression coverage, brittle assertions, untested edge cases, and verification gaps.",
  },
]

function resolveTask(runtime) {
  if (typeof runtime?.task === "string" && runtime.task.trim()) {
    return runtime.task.trim()
  }
  if (typeof runtime?.prompt === "string" && runtime.prompt.trim()) {
    return runtime.prompt.trim()
  }
  return "Audit the current repository and return only concrete, high-signal findings."
}

async function invokeAgent(runtime, spec) {
  const runners = [
    runtime?.runAgent,
    runtime?.spawnAgent,
    runtime?.runSubagent,
    runtime?.delegate,
  ].filter((candidate) => typeof candidate === "function")

  if (runners.length === 0) {
    throw new Error("Unsupported Claude workflow runtime: no subagent runner helper found")
  }

  for (const runner of runners) {
    try {
      return await runner.call(runtime, spec)
    } catch (error) {
      try {
        return await runner.call(runtime, spec.prompt, spec)
      } catch {
        if (runner === runners[runners.length - 1]) {
          throw error
        }
      }
    }
  }

  throw new Error("Claude workflow runtime did not accept any supported agent invocation shape")
}

function lensPrompt(task, lens) {
  return [
    `Primary task: ${task}`,
    "",
    `Audit lens: ${lens.name}`,
    lens.focus,
    "",
    "Constraints:",
    "- Read only what is needed to support real findings.",
    "- Do not change files.",
    "- Report only issues that materially matter.",
    "- For each finding include: severity, evidence, why it matters, and a concrete fix.",
  ].join("\n")
}

function consolidatePrompt(task, lensReports) {
  return [
    `Primary task: ${task}`,
    "",
    "You are consolidating independent audit passes from security, performance, and test reviewers.",
    "Cross-check the findings, remove duplicates, discard weak claims, and return one final report.",
    "",
    "Required output:",
    "1. Executive summary",
    "2. Findings grouped by severity",
    "3. Conflicts or findings rejected during cross-check",
    "4. Recommended next actions",
    "",
    "Lens reports:",
    JSON.stringify(lensReports, null, 2),
  ].join("\n")
}

export default async function codeAudit(runtime) {
  const task = resolveTask(runtime)
  const reviewerModel = resolveClaudeRoleModel("review")
  const consolidatorModel = resolveClaudeRoleModel("code")
  const lensReports = await Promise.all(
    REVIEW_LENSES.map((lens) =>
      invokeAgent(runtime, {
        name: `code-audit-${lens.name}`,
        ...(reviewerModel ? { model: reviewerModel } : {}),
        prompt: lensPrompt(task, lens),
      }),
    ),
  )

  const consolidated = await invokeAgent(runtime, {
    name: "code-audit-consolidator",
    ...(consolidatorModel ? { model: consolidatorModel } : {}),
    prompt: consolidatePrompt(task, lensReports),
  })

  return {
    workflow: "code-audit",
    reviewModel: reviewerModel || "auto",
    consolidatorModel: consolidatorModel || "auto",
    task,
    lenses: REVIEW_LENSES.map((lens) => lens.name),
    reports: lensReports,
    consolidated,
  }
}
