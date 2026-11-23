import { describe, expect, it } from "vitest";
import type { ReviewSessionInfo, ReviewTarget } from "../types";
import { pickNextUp, type NextUpCandidate } from "./nextUp";
import shared from "./nextUpCases.json";

let position = 0;
const part = (state: string, category = "decision") => ({
  target_id: `t${position}`, unit_key: `t${position}`, path: "a.py", state, reasons: [], answer: { category },
  priority: { tier: 1, attention: "review_first", outstanding: state === "unreviewed", position: position++, reasons: [{ code: "", detail: "Needs your judgment" }] },
}) as unknown as ReviewTarget;
const review = (id: string) => ({ id, ref: id, title: id, status: "open" }) as ReviewSessionInfo;
const idle = { unpublished: 0, published: 0, in_flight: 0, open_total: 0, addressed: 0 };
const candidate = (id: string, states: string[], feedback = idle, snoozed = false): NextUpCandidate =>
  ({ review: { ...review(id), ...(snoozed ? { queue: { snoozed_until: "2999-01-01T00:00:00Z", resume: null } } : {}) } as ReviewSessionInfo, targets: states.map((state) => part(state)), feedback });

describe("P2-5 DONE-4 Next up reason", () => {
  it("picks the review with the fewest decisions and says so only after comparing", () => {
    const next = pickNextUp([candidate("big", ["unreviewed", "unreviewed", "unreviewed"]), candidate("small", ["unreviewed"])]);
    expect(next?.review.id).toBe("small");
    expect(next?.reason).toBe("your turn · quickest to finish (1 decision)");
  });

  it("says one of the quickest when not every open review was compared", () => {
    const next = pickNextUp([candidate("big", ["unreviewed", "unreviewed"]), candidate("small", ["unreviewed"])], false);
    expect(next?.reason).toBe("your turn · one of the quickest to finish (1 decision)");
  });

  it("does not claim quickest for a lone candidate or a tie", () => {
    expect(pickNextUp([candidate("only", ["unreviewed"])])?.reason).toBe("your turn (1 decision)");
    expect(pickNextUp([candidate("a", ["unreviewed"]), candidate("b", ["unreviewed"])])?.reason).toBe("your turn (1 decision)");
  });

  it("never says your turn while notes are with the agent or nothing is left to judge", () => {
    const withAgent = pickNextUp([candidate("waiting", ["unreviewed"], { ...idle, published: 1 })]);
    expect(withAgent?.yourTurn).toBe(false);
    expect(withAgent?.reason).toBe("waiting on the coding agent");
    const done = pickNextUp([candidate("done", ["reviewed"])]);
    expect(done?.yourTurn).toBe(false);
    expect(done?.reason).toBe("nothing left to judge");
  });

  it("prefers a review that is waiting on the reviewer over one that is not", () => {
    const next = pickNextUp([candidate("agent", ["unreviewed"], { ...idle, in_flight: 1 }), candidate("mine", ["unreviewed", "unreviewed"])]);
    expect(next?.review.id).toBe("mine");
  });

  it("returns null with no candidates", () => expect(pickNextUp([])).toBeNull());
});

describe("shared cases with `lc review next`", () => {
  for (const item of shared.cases) {
    it(item.name, () => {
      const candidates = item.candidates.map((c) => candidate(c.id, c.states, { ...idle, ...((c as { feedback?: object }).feedback ?? {}) }, (c as { snoozed?: boolean }).snoozed === true));
      const next = pickNextUp(candidates, (item.listed ?? candidates.length) <= 5);
      if (!item.expect) return expect(next).toBeNull();
      expect({ id: next?.review.id, reason: next?.reason, your_turn: next?.yourTurn }).toEqual(item.expect);
    });
  }
});
