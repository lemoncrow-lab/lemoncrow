import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import ReviewViewTabs from "./ReviewViewTabs";

afterEach(cleanup);
const tabs = (props: { current: "diff" | "map" | "preview"; previewCount: number | null }) => render(<MemoryRouter><ReviewViewTabs reviewRef="shop" {...props} /></MemoryRouter>);
const names = () => screen.getAllByRole("tab").map((tab) => tab.textContent);

describe("4.6 view tabs", () => {
  it("always lists Diff, Map and Preview, and carries the Preview count (review-9.1)", () => {
    tabs({ current: "diff", previewCount: 7 });
    expect(names()).toEqual(["Diff", "Map", "Preview · 7"]);
    cleanup();
    tabs({ current: "diff", previewCount: null });
    expect(names()).toEqual(["Diff", "Map", "Preview"]);
  });
  it("marks the open tab", () => {
    tabs({ current: "map", previewCount: null });
    expect(screen.getByRole("tab", { name: "Map" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tab", { name: "Diff" }).getAttribute("aria-selected")).toBe("false");
  });
});
