import { beforeEach, describe, expect, it, vi } from "vitest";

import { requestBrowserJson } from "../reviewApi";
import { postRunSpecific, type RunPlanEntry } from "../runPlanApi";
import { DATA_PATH_ENTRY, dataPathGate, fetchDataPathGate, postDataPathRun } from "./dataPathApi";

vi.mock("../reviewApi", () => ({ requestBrowserJson: vi.fn(async () => ({})) }));
vi.mock("../runPlanApi", () => ({ postRunSpecific: vi.fn() }));

const entry = (extra: Partial<RunPlanEntry>) => ({ id: DATA_PATH_ENTRY, status: "run", executor: "local", reason: "requested for this Review", ...extra }) as RunPlanEntry;

beforeEach(() => { vi.mocked(requestBrowserJson).mockClear(); vi.mocked(postRunSpecific).mockReset(); });

describe("P10-7 data path run gate", () => {
  it("runs only where the plan schedules the pick on the local executor", () => {
    expect(dataPathGate(entry({}))).toEqual({ runnable: true, reason: "" });
    const placed = "Data path difference (experimental) runs only where the revision is held; this deployment places it on the worker executor";
    expect(dataPathGate(entry({ status: "not_available", executor: "worker", reason: placed }))).toEqual({ runnable: false, reason: placed });
    expect(dataPathGate(entry({ status: "skipped", reason: "static analysis is off" }))).toEqual({ runnable: false, reason: "static analysis is off" });
    expect(dataPathGate(entry({ executor: "managed" })).runnable).toBe(false);
    expect(dataPathGate(undefined)).toEqual({ runnable: false, reason: "The run plan does not schedule the data path here." });
  });

  it("asks the run plan with an unconfirmed pick, which adds nothing", async () => {
    vi.mocked(postRunSpecific).mockResolvedValue({ confirmed: false, preview: { plan: { entries: [entry({ id: "other" }), entry({})] } } } as never);
    await expect(fetchDataPathGate("review-1", "rev1")).resolves.toEqual({ runnable: true, reason: "" });
    expect(postRunSpecific).toHaveBeenCalledWith("review-1", "rev1", [{ id: "data_path.diff" }], false);
  });

  it("runs on the exact revision as a per-Review pick", async () => {
    await postDataPathRun("review/1", "rev1");
    expect(requestBrowserJson).toHaveBeenCalledWith("/api/reviews/review%2F1/data-path/run", {
      method: "POST", body: JSON.stringify({ revision_id: "rev1", pick: true }),
    });
  });
});
