import { describe, expect, it } from "vitest";

import { answerPressed } from "./answerTone";

describe("answerPressed", () => {
  it("marks only the recorded answer", () => {
    expect(answerPressed("reviewed", "good")).toEqual({ className: expect.stringContaining("bg-ok-bg"), "aria-pressed": true });
    expect(answerPressed("reviewed", "change")).toEqual({ className: "", "aria-pressed": false });
    expect(answerPressed("needs_changes", "change")["aria-pressed"]).toBe(true);
    expect(answerPressed("later", "later")["aria-pressed"]).toBe(true);
    expect(answerPressed(undefined, "good")["aria-pressed"]).toBe(false);
  });
});
