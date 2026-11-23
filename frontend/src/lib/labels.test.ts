import { describe, expect, it } from "vitest";

import { domainLabel } from "./labels";

describe("domainLabel", () => {
  it("names domain bundle ids for people", () => {
    expect(domainLabel("swe.general")).toBe("Software engineering");
    expect(domainLabel("swe.frontend_perf")).toBe("Software engineering · Frontend perf");
    expect(domainLabel("data.etl")).toBe("Data etl");
    expect(domainLabel("")).toBe("");
  });
});
