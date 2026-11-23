import { act, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ReviewStoreProvider, useReviewStoreDispatch, useReviewStoreState } from "./reviewStoreContext";
import type { ReviewStoreAction } from "./reviewStore";

describe("review store provider", () => {
  it("both hooks throw outside the provider", () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    function ReadsState() {
      useReviewStoreState();
      return null;
    }
    function ReadsDispatch() {
      useReviewStoreDispatch();
      return null;
    }
    expect(() => render(<ReadsState />)).toThrow(/ReviewStoreProvider/);
    expect(() => render(<ReadsDispatch />)).toThrow(/ReviewStoreProvider/);
    quiet.mockRestore();
  });

  function setup() {
    const dispatches: Array<(action: ReviewStoreAction) => void> = [];
    const states: Array<ReturnType<typeof useReviewStoreState>> = [];
    let renders = 0;
    function Probe() {
      renders += 1;
      states.push(useReviewStoreState());
      dispatches.push(useReviewStoreDispatch());
      return null;
    }
    render(<ReviewStoreProvider><Probe /></ReviewStoreProvider>);
    return { dispatches, states, renders: () => renders };
  }

  it("starts from the initial state", () => {
    const probe = setup();
    expect(probe.states[0].activeTargetId).toBe("");
    expect(probe.states[0].commentsState).toBe("loading");
  });

  it("dispatch keeps identity across state changes", () => {
    const probe = setup();
    act(() => probe.dispatches[0]({ type: "part/selected", targetId: "t1" }));
    act(() => probe.dispatches[0]({ type: "revision/advancedSet", advanced: true }));
    expect(probe.dispatches.length).toBeGreaterThan(1);
    expect(new Set(probe.dispatches).size).toBe(1);
    expect(probe.states.at(-1)).toMatchObject({ activeTargetId: "t1", revisionAdvanced: true });
  });

  it("a no-op dispatch does not re-render a consumer; a real dispatch does", () => {
    const probe = setup();
    const before = probe.renders();
    act(() => probe.dispatches[0]({ type: "part/selected", targetId: "" }));
    expect(probe.renders()).toBe(before);
    act(() => probe.dispatches[0]({ type: "part/selected", targetId: "t2" }));
    expect(probe.renders()).toBeGreaterThan(before);
  });
});
