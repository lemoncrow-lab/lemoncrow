import { describe, expect, it, vi } from "vitest";

import { MINIMAL_CHANGE, PRICING_CHANGE } from "./fixtures";
import { fetchXRayGraph, getXRayGraphOnce, normaliseXRayGraphState, requestXRayGraph, type JsonRequest } from "./xrayGraphApi";

const failure = (status: number, message = String(status)) => Object.assign(new Error(message), { status });
const answers = (...bodies: unknown[]): JsonRequest & ReturnType<typeof vi.fn> => {
  const fn = vi.fn();
  for (const body of bodies) {
    if (body instanceof Error) fn.mockRejectedValueOnce(body);
    else fn.mockResolvedValueOnce(body);
  }
  return fn as never;
};

describe("fetchXRayGraph", () => {
  it("reads a ready graph for the revision", async () => {
    const request = answers({ state: "ready", graph: PRICING_CHANGE });
    const state = await fetchXRayGraph("r 1", "rev/2", { request });
    expect(state).toEqual({ state: "ready", graph: PRICING_CHANGE });
    expect(request).toHaveBeenCalledWith("/api/reviews/r%201/xray-graph?revision_id=rev%2F2", undefined);
  });

  it("polls while pending, hands each pending answer on, and backs off", async () => {
    const request = answers({ state: "pending" }, { state: "pending", graph: MINIMAL_CHANGE }, { state: "ready", graph: PRICING_CHANGE });
    const sleep = vi.fn(async (_ms: number) => {});
    const onUpdate = vi.fn();
    const state = await fetchXRayGraph("r1", "rev1", { request, sleep, onUpdate, intervalMs: 1000, maxIntervalMs: 1200 });
    expect(state.state).toBe("ready");
    expect(onUpdate.mock.calls.map(([s]) => [s.state, Boolean(s.graph)])).toEqual([["pending", false], ["pending", true]]);
    expect(sleep.mock.calls.map(([ms]) => ms)).toEqual([1000, 1200]);
  });

  it("reads a 404 as a server without the endpoint, so the screen falls back", async () => {
    const state = await getXRayGraphOnce("r1", "rev1", answers(failure(404)));
    expect(state).toMatchObject({ state: "unavailable", missing: true });
  });

  it("passes other failures on, and stops when aborted", async () => {
    await expect(getXRayGraphOnce("r1", "rev1", answers(failure(500)))).rejects.toThrow("500");
    const controller = new AbortController();
    controller.abort();
    await expect(fetchXRayGraph("r1", "rev1", { signal: controller.signal, request: answers() })).rejects.toMatchObject({ name: "AbortError" });
  });

  it("turns an unreadable graph into unavailable, with the server's reason when it gives one", () => {
    expect(normaliseXRayGraphState({ state: "ready", graph: { version: 2 } as never })).toMatchObject({ state: "unavailable" });
    expect(normaliseXRayGraphState({ state: "pending", graph: { nope: true } as never })).toEqual({ state: "pending" });
    expect(normaliseXRayGraphState({ state: "unavailable", reason: "No index yet." })).toEqual({ state: "unavailable", reason: "No index yet." });
  });
});

describe("requestXRayGraph", () => {
  it("posts the revision and whether to use AI", async () => {
    const request = answers(null);
    expect(await requestXRayGraph("r1", "rev1", { ai: true }, request)).toEqual({ ok: true, state: { state: "pending" } });
    expect(request).toHaveBeenCalledWith("/api/reviews/r1/xray-graph/generate", { method: "POST", body: JSON.stringify({ revision_id: "rev1", ai: true }) });
  });

  it("uses a state answer as it is", async () => {
    expect(await requestXRayGraph("r1", "rev1", { ai: false }, answers({ state: "ready", graph: MINIMAL_CHANGE }))).toEqual({ ok: true, state: { state: "ready", graph: MINIMAL_CHANGE } });
  });

  it("turns a 409 into its reason", async () => {
    expect(await requestXRayGraph("r1", "rev1", { ai: true }, answers(failure(409, "Review AI is off for this workspace.")))).toEqual({ ok: false, reason: "Review AI is off for this workspace." });
    expect(await requestXRayGraph("r1", "rev1", { ai: true }, answers(failure(409)))).toEqual({ ok: false, reason: "AI drawing is not available for this Review right now." });
  });
});
