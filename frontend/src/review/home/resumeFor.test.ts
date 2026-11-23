import { describe, expect, it } from "vitest";

import { focusResume, WRAP_RESUME } from "./resumeFor";

const part = (id: string, symbol = id) => ({ target_id: id, symbol }) as never;

describe("the place Home offers to continue from", () => {
  const decisions = [part("d1", "can_view_order"), part("d2", "apply_discount")];
  const all = [...decisions, part("q1", "order_total")];

  it("names a decision by its position among the decisions and its link slug", () => {
    expect(focusResume(all, decisions[1], decisions)).toEqual({ screen: "one", part: "apply_discount", position: 2, total: 2 });
  });

  it("names a quick check as the quick check", () => {
    expect(focusResume(all, all[2], decisions)).toEqual({ screen: "quick_check", part: "order_total", position: 1, total: 1 });
  });

  it("has no place when no part is open, and wrap up has no part", () => {
    expect(focusResume(all, null, decisions)).toBeNull();
    expect(WRAP_RESUME).toEqual({ screen: "wrap", part: "", position: 0, total: 0 });
  });
});
