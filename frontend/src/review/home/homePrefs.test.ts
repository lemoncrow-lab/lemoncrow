import { describe, expect, it } from "vitest";

import { DEFAULT_PREFS, parsePrefs } from "./homePrefs";

describe("remembered sort and filters", () => {
  it("reads what was saved", () => {
    const saved = { sort: "fewest", filters: { repos: ["shop"], sources: ["GitHub"], cameBack: true } };
    expect(parsePrefs(JSON.stringify(saved))).toEqual(saved);
  });

  it("falls back field by field on a malformed or older value", () => {
    expect(parsePrefs(null)).toEqual(DEFAULT_PREFS);
    expect(parsePrefs("{not json")).toEqual(DEFAULT_PREFS);
    expect(parsePrefs("7")).toEqual(DEFAULT_PREFS);
    expect(parsePrefs(JSON.stringify({ sort: "loudest", filters: { repos: ["shop", 3], sources: ["Mail"], cameBack: "yes" } }))).toEqual({
      sort: "waiting",
      filters: { repos: ["shop"], sources: [], cameBack: false },
    });
  });
});
