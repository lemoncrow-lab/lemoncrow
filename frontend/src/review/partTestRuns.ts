/**
 * P4-2: one Run test state per part, shared by the Focus card, Quick check and All files.
 *
 * A part has a result here only after the reviewer's own click ran its mapped test with `fresh: true`
 * (nothing replayed, carried forward, shared or satisfied by CI). No entry means "did not run here": a
 * CI or carried-forward result is never written into this store. The state lives for the page session and is
 * keyed by review, revision and part, so a new revision never shows an older run.
 */
import { useSyncExternalStore } from "react";
import { postCheckRecipesRun, type CheckVerdict } from "./checkRecipesApi";
import { testOutcome } from "./focus/focusModel";
import { getPartProof, partOutcome, refreshPartProof } from "./partProof";

export interface PartTestRun {
  state: "running" | "done" | "error";
  verdict: CheckVerdict;
  /** "<recipe title> · passed", "· failed" or "· no result" (plus the reused note), or the error line. */
  detail: string;
  /** The server still counted reused, carried or shared work in this run's report. */
  reused: boolean;
  /** P11-1: the part's outcome in the shared vocabulary after this run wrote the Snapshot's record (never a second store). */
  proof?: string;
}

export interface PartTestMapping {
  entry_id: string;
  title: string;
}

const runs = new Map<string, PartTestRun>();
const listeners = new Set<() => void>();
let version = 0;

const keyOf = (reviewId: string, revisionId: string, unitKey: string) => `${reviewId}\u0000${revisionId}\u0000${unitKey}`;

function put(key: string, run: PartTestRun) {
  runs.set(key, run);
  version += 1;
  for (const listener of [...listeners]) listener();
}

export function getPartTestRun(reviewId: string, revisionId: string, unitKey: string): PartTestRun | undefined {
  return runs.get(keyOf(reviewId, revisionId, unitKey));
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** The part's shared run, re-rendering every screen that reads it the moment any of them starts or finishes a run. */
export function usePartTestRun(reviewId: string, revisionId: string, unitKey: string): PartTestRun | undefined {
  return useSyncExternalStore(subscribe, () => getPartTestRun(reviewId, revisionId, unitKey), () => undefined);
}

/** Re-renders on any change, for a screen that lists many parts and reads each with `getPartTestRun`. */
export function usePartTestRunsVersion(): number {
  return useSyncExternalStore(subscribe, () => version, () => 0);
}

/** Run the part's mapped test fresh. A run already in flight for the part is joined, not started twice. */
export async function runPartTest(args: { reviewId: string; revisionId: string; unitKey: string; mapping: PartTestMapping; path?: string }): Promise<void> {
  const { reviewId, revisionId, unitKey, mapping, path } = args;
  const key = keyOf(reviewId, revisionId, unitKey);
  if (runs.get(key)?.state === "running") return;
  put(key, { state: "running", verdict: "NOT_RUN", detail: "Running…", reused: false });
  try {
    const finished = await postCheckRecipesRun(reviewId, revisionId, mapping.entry_id, true, "", unitKey, true);
    if (finished.revision_id !== revisionId) throw new Error("The revision changed during the check; refresh before deciding.");
    const outcome = testOutcome(finished, mapping.entry_id, mapping.title);
    // The run wrote the same stored record the automatic run writes: re-read it so every screen shows the same words.
    if (finished.part_proof?.recorded) await refreshPartProof(reviewId, revisionId);
    const proof = path && finished.part_proof?.recorded ? partOutcome(getPartProof(reviewId, revisionId), path).label : undefined;
    put(key, { state: "done", verdict: outcome.verdict, detail: proof ? `${outcome.detail} · ${proof}` : outcome.detail, reused: outcome.reused, ...(proof ? { proof } : {}) });
  } catch (cause) {
    put(key, { state: "error", verdict: "UNKNOWN", detail: `Test could not complete: ${cause instanceof Error ? cause.message : "Unknown error"}`, reused: false });
  }
}

/** For tests only. */
export function resetPartTestRuns() {
  runs.clear();
  version += 1;
  for (const listener of [...listeners]) listener();
}
