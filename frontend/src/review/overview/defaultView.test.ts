import { beforeEach, describe, expect, it } from "vitest";

import {
  DEFAULT_VIEW_KEY,
  EVIDENCE_OPEN_KEY,
  getDefaultView,
  getEvidenceOpen,
  getPlanApproved,
  otherView,
  planKey,
  setDefaultView,
  setEvidenceOpen,
  setPlanApproved,
  viewLabel,
} from "./defaultView";

beforeEach(() => window.localStorage.clear());

describe("the remembered reading view", () => {
  it("is One at a time until All files is chosen", () => {
    expect(getDefaultView()).toBe("one");
    setDefaultView("all");
    expect(getDefaultView()).toBe("all");
    setDefaultView("one");
    expect(getDefaultView()).toBe("one");
    expect(window.localStorage.getItem(DEFAULT_VIEW_KEY)).toBeNull();
  });

  it("falls back to One at a time for junk", () => {
    window.localStorage.setItem(DEFAULT_VIEW_KEY, "full");
    expect(getDefaultView()).toBe("one");
  });

  it("names and flips the two views", () => {
    expect(otherView("one")).toBe("all");
    expect(otherView("all")).toBe("one");
    expect(viewLabel("one")).toBe("One at a time");
    expect(viewLabel("all")).toBe("All files");
  });
});

describe("what the Overview keeps", () => {
  it("opens Evidence only when it was left open", () => {
    expect(getEvidenceOpen()).toBe(false);
    setEvidenceOpen(true);
    expect(window.localStorage.getItem(EVIDENCE_OPEN_KEY)).toBe("open");
    expect(getEvidenceOpen()).toBe(true);
    setEvidenceOpen(false);
    expect(getEvidenceOpen()).toBe(false);
  });

  it("keeps plan approval per review, revision and plan", () => {
    const key = planKey("shop", "rev1", ["1. Allow guests", "2. Stack codes"]);
    setPlanApproved(key, true);
    expect(getPlanApproved(key)).toBe(true);
    expect(getPlanApproved(planKey("other", "rev1", ["1. Allow guests", "2. Stack codes"]))).toBe(false);
    expect(getPlanApproved(planKey("shop", "rev2", ["1. Allow guests", "2. Stack codes"]))).toBe(false);
    expect(getPlanApproved(planKey("shop", "rev1", ["1. Allow guests", "2. Stack codes", "3. New step"]))).toBe(false);
    expect(getPlanApproved(planKey("shop", "rev1", ["1. Allow guests", "2. Stack codes"]))).toBe(true);
    setPlanApproved(key, false);
    expect(getPlanApproved(key)).toBe(false);
  });

  it("does not confuse two plans whose lines run together", () => {
    expect(planKey("s", "r", ["ab", "c"])).not.toBe(planKey("s", "r", ["a", "bc"]));
  });

  it("survives blocked storage", () => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = () => {
      throw new Error("blocked");
    };
    try {
      expect(getDefaultView()).toBe("one");
      expect(getEvidenceOpen()).toBe(false);
      expect(getPlanApproved("shop.r.x")).toBe(false);
    } finally {
      Storage.prototype.getItem = original;
    }
  });
});
