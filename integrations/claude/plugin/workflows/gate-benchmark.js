/**
 * gate-benchmark.js
 *
 * Claude Code dynamic workflow for benchmark gating.
 *
 * It delegates to focused agents that:
 *   1. collect benchmark evidence from LemonCrow's existing benchmark surfaces,
 *   2. assess whether the evidence is statistically credible,
 *   3. return one final PASS / FAIL / INSUFFICIENT_DATA verdict.
 *
 * Dynamic workflows are in research preview and require Claude Code v2.1.154+.
 */

import { resolveClaudeRoleModel } from "./model-config.js"

function resolveTask(runtime) {
  if (typeof runtime?.task === "string" && runtime.task.trim()) {
    return runtime.task.trim()
  }
  if (typeof runtime?.prompt === "string" && runtime.prompt.trim()) {
    return runtime.prompt.trim()
  }
  return [
    "Compare a baseline and candidate benchmark result using LemonCrow's existing benchmark surfaces.",
    "Prefer repeated paired results when available.",
    "Return PASS, FAIL, or INSUFFICIENT_DATA with evidence.",
  ].join(" ")
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

function collectorPrompt(task) {
  return [
    `Primary task: ${task}`,
    "",
    "Collect benchmark evidence using only benchmark/reporting surfaces that already exist in this repository.",
    "",
    "Preferred sources, in order:",
    "1. paired multi-run benchmark reports from benchmarks/codebench/run.py --report <dir>",
    "2. real A/B calibration data from tests/benchmarks/test_*_ab_real.py outputs",
    "3. wire captures compared with uv run python -m benchmarks.wire_savings.report base=... cand=...",
    "",
    "Requirements:",
    "- Produce a structured summary of baseline vs candidate metrics.",
    "- Call out whether the data is paired, repeated, and large enough to support a gate verdict.",
    "- If the evidence is incomplete, say exactly what is missing.",
    "- Do not edit files.",
  ].join("\n")
}

function statisticianPrompt(task, collectedEvidence) {
  return [
    `Primary task: ${task}`,
    "",
    "You are the benchmark gate statistician.",
    "Decide whether the available evidence supports a strict PASS, FAIL, or INSUFFICIENT_DATA verdict.",
    "",
    "Verdict rules:",
    "- PASS: candidate shows a real savings win and there is no credible quality regression signal.",
    "- FAIL: candidate is worse on the primary savings metric or shows a credible quality regression.",
    "- INSUFFICIENT_DATA: not enough paired/repeated evidence, unclear quality evidence, or unverifiable inputs.",
    "",
    "Statistical-rigor expectations:",
    "- Prefer repeated paired runs over one-off anecdotes.",
    "- Treat a single wire capture diff as useful evidence but not sufficient for a final PASS on its own.",
    "- Be explicit about confidence limits and missing quality evidence.",
    "",
    "Collected evidence:",
    JSON.stringify(collectedEvidence, null, 2),
  ].join("\n")
}

function consolidatePrompt(task, collectedEvidence, gateVerdict) {
  return [
    `Primary task: ${task}`,
    "",
    "Produce the final benchmark gate report.",
    "Return exactly one verdict: PASS, FAIL, or INSUFFICIENT_DATA.",
    "",
    "Required sections:",
    "1. Verdict",
    "2. Savings evidence",
    "3. Quality evidence",
    "4. Statistical confidence / limitations",
    "5. Next action",
    "",
    "Collected evidence:",
    JSON.stringify(collectedEvidence, null, 2),
    "",
    "Statistician assessment:",
    JSON.stringify(gateVerdict, null, 2),
  ].join("\n")
}

export default async function gateBenchmark(runtime) {
  const task = resolveTask(runtime)
  const reviewModel = resolveClaudeRoleModel("review")

  const collectedEvidence = await invokeAgent(runtime, {
    name: "gate-benchmark-collector",
    ...(reviewModel ? { model: reviewModel } : {}),
    prompt: collectorPrompt(task),
  })

  const gateVerdict = await invokeAgent(runtime, {
    name: "gate-benchmark-statistician",
    ...(reviewModel ? { model: reviewModel } : {}),
    prompt: statisticianPrompt(task, collectedEvidence),
  })

  const consolidated = await invokeAgent(runtime, {
    name: "gate-benchmark-consolidator",
    ...(reviewModel ? { model: reviewModel } : {}),
    prompt: consolidatePrompt(task, collectedEvidence, gateVerdict),
  })

  return {
    workflow: "gate-benchmark",
    model: reviewModel || "auto",
    task,
    collectedEvidence,
    gateVerdict,
    consolidated,
  }
}
