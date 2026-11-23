/**
 * P10-7 data path for the Map's Data path lens: whether this deployment runs it, and the on-request run.
 *
 * The run is `POST /api/reviews/{id}/data-path/run` (data_path_api.py); its stored report reaches the Map payload as
 * `data_path`. Whether it may run is the effective run plan's own answer, never a client rule.
 */
import { requestBrowserJson } from "../reviewApi";
import { postRunSpecific, type RunPlanEntry } from "../runPlanApi";
import type { ReviewDataPathReport } from "../types";

export const DATA_PATH_ENTRY = "data_path.diff";

export type DataPathGate = { runnable: true; reason: "" } | { runnable: false; reason: string };

/**
 * The plan entry a per-Review pick would add: it runs only when the plan schedules it on the local executor (the
 * catalog's only locus for it). A worker or managed locus, an organization's execution block, a family turned off or a
 * budget limit each plan it otherwise, with the plan's reason.
 */
export function dataPathGate(entry: RunPlanEntry | undefined): DataPathGate {
  if (entry?.status === "run" && entry.executor === "local") return { runnable: true, reason: "" };
  return { runnable: false, reason: entry?.reason || "The run plan does not schedule the data path here." };
}

/** Previews the pick (`confirm: false`): nothing is added to the plan and nothing runs. */
export async function fetchDataPathGate(reviewId: string, revisionId: string): Promise<DataPathGate> {
  const response = await postRunSpecific(reviewId, revisionId, [{ id: DATA_PATH_ENTRY }], false);
  return dataPathGate(response.preview.plan.entries.find((entry) => entry.id === DATA_PATH_ENTRY));
}

export interface DataPathRunResponse extends ReviewDataPathReport {
  run_plan_digest: string;
  run_on: string;
  wall_seconds: number;
}

/** Runs the data path on this exact Snapshot, adding it to the plan as a per-Review pick first. */
export function postDataPathRun(reviewId: string, revisionId: string): Promise<DataPathRunResponse> {
  return requestBrowserJson<DataPathRunResponse>(`/api/reviews/${encodeURIComponent(reviewId)}/data-path/run`, {
    method: "POST",
    body: JSON.stringify({ revision_id: revisionId, pick: true }),
  });
}
