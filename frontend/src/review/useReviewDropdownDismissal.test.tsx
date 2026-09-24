import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { useReviewDropdownDismissal } from "./useReviewDropdownDismissal";

function Fixture() {
  useReviewDropdownDismissal();
  return (
    <div>
      <details data-review-dropdown>
        <summary>First menu</summary>
        <button type="button">First action</button>
      </details>
      <details data-review-dropdown>
        <summary>Second menu</summary>
        <button type="button">Second action</button>
      </details>
      <button type="button">Outside</button>
    </div>
  );
}

describe("useReviewDropdownDismissal", () => {
  it("closes every open Review dropdown when clicking outside it", async () => {
    render(<Fixture />);
    const firstSummary = screen.getByText("First menu");
    await userEvent.click(firstSummary);
    const firstDetails = firstSummary.closest("details") as HTMLDetailsElement;
    expect(firstDetails.open).toBe(true);

    await userEvent.click(screen.getByRole("button", { name: "Outside" }));
    expect(firstDetails.open).toBe(false);
  });

  it("closes an older dropdown when another dropdown is opened", async () => {
    render(<Fixture />);
    const firstSummary = screen.getByText("First menu");
    const secondSummary = screen.getByText("Second menu");
    const firstDetails = firstSummary.closest("details") as HTMLDetailsElement;
    const secondDetails = secondSummary.closest("details") as HTMLDetailsElement;

    await userEvent.click(firstSummary);
    expect(firstDetails.open).toBe(true);
    await userEvent.click(secondSummary);
    expect(firstDetails.open).toBe(false);
    expect(secondDetails.open).toBe(true);
  });
});
