import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InvestigateTabs } from "./Investigate";

describe("InvestigateTabs", () => {
  it("shows the steps as underline tabs without numbers and moves the current step", () => {
    const onGo = vi.fn();
    render(<InvestigateTabs onGo={onGo} />);
    const nav = screen.getByRole("navigation", { name: "Investigation steps" });
    const tabs = nav.querySelectorAll("button");
    expect(tabs.length).toBeGreaterThan(1);
    for (const tab of tabs) expect(tab.textContent ?? "").not.toMatch(/\d/);
    expect(nav.querySelectorAll('[aria-current="step"]')).toHaveLength(1);
    const last = tabs[tabs.length - 1];
    fireEvent.click(last);
    expect(onGo).toHaveBeenCalledTimes(1);
    expect(last).toHaveAttribute("aria-current", "step");
  });
});
