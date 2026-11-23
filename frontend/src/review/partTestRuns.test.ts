import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { postCheckRecipesRun, type CheckRunResponse } from "./checkRecipesApi";
import { getPartTestRun, resetPartTestRuns, runPartTest, usePartTestRun } from "./partTestRuns";

vi.mock("./checkRecipesApi", () => ({ postCheckRecipesRun: vi.fn() }));
const api = vi.mocked(postCheckRecipesRun);
const MAPPING = { entry_id: "checks.recipe:orders", title: "Orders regression" };
const ARGS = { reviewId: "r1", revisionId: "rev1", unitKey: "sym:order_total", mapping: MAPPING };
const report = (extra: Partial<CheckRunResponse> = {}) => ({ revision_id: "rev1", entry_id: MAPPING.entry_id, verdicts: { [MAPPING.entry_id]: "PASS" }, executions: 1, reused: 0, notes: [], executor_class: "local", run_on: "local", ...extra }) as CheckRunResponse;

beforeEach(() => { api.mockReset(); resetPartTestRuns(); });
afterEach(() => resetPartTestRuns());

describe("P4-2 shared Run test state", () => {
  it("has no result until a run: a part that did not run here reads undefined", () => {
    expect(getPartTestRun("r1", "rev1", "sym:order_total")).toBeUndefined();
  });

  it("two screens reading the same part see running then passed without a reload", async () => {
    let finish!: (value: CheckRunResponse) => void;
    api.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const focus = renderHook(() => usePartTestRun("r1", "rev1", "sym:order_total"));
    const quick = renderHook(() => usePartTestRun("r1", "rev1", "sym:order_total"));
    let started!: Promise<void>;
    act(() => { started = runPartTest(ARGS); });
    expect(focus.result.current?.state).toBe("running");
    expect(quick.result.current?.state).toBe("running");
    await act(async () => { finish(report()); await started; });
    expect(focus.result.current).toEqual({ state: "done", verdict: "PASS", detail: "Orders regression · passed", reused: false });
    expect(quick.result.current).toEqual(focus.result.current);
  });

  it("sends fresh: true for the part and never joins a second click while one is running", async () => {
    api.mockReturnValue(new Promise(() => {}));
    void runPartTest(ARGS);
    void runPartTest(ARGS);
    expect(api).toHaveBeenCalledTimes(1);
    expect(api).toHaveBeenCalledWith("r1", "rev1", MAPPING.entry_id, true, "", "sym:order_total", true);
  });

  it("says so when the report still counts reused, carried or shared work", async () => {
    api.mockResolvedValue(report({ carried: 1 }));
    await runPartTest(ARGS);
    expect(getPartTestRun("r1", "rev1", "sym:order_total")).toMatchObject({ reused: true, detail: "Orders regression · passed · reused from an earlier run, not run again for this click" });
  });

  it("reads the verdict only from this run's report: a missing verdict is no result, never a pass", async () => {
    api.mockResolvedValue(report({ verdicts: {} }));
    await runPartTest(ARGS);
    expect(getPartTestRun("r1", "rev1", "sym:order_total")).toMatchObject({ verdict: "UNKNOWN", detail: "Orders regression · no result" });
  });

  it("a failed run and a report for another revision are errors or failures, never passed, and stay per revision", async () => {
    api.mockResolvedValueOnce(report({ verdicts: { [MAPPING.entry_id]: "FAIL" } }));
    await runPartTest(ARGS);
    expect(getPartTestRun("r1", "rev1", "sym:order_total")?.verdict).toBe("FAIL");
    api.mockResolvedValueOnce(report({ revision_id: "rev9" }));
    await runPartTest({ ...ARGS, unitKey: "sym:other" });
    expect(getPartTestRun("r1", "rev1", "sym:other")).toMatchObject({ state: "error", verdict: "UNKNOWN" });
    expect(getPartTestRun("r1", "rev2", "sym:order_total")).toBeUndefined();
  });

  it("an API failure is an error line, not a result", async () => {
    api.mockRejectedValue(new Error("sandbox down"));
    await runPartTest(ARGS);
    expect(getPartTestRun("r1", "rev1", "sym:order_total")).toEqual({ state: "error", verdict: "UNKNOWN", detail: "Test could not complete: sandbox down", reused: false });
  });
});
