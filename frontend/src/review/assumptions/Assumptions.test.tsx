import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { INVESTIGATION_FIXTURES } from "../investigation/fixtures";
import Assumptions from "./Assumptions";
import { brokenPaymentRetry, orderSnapshots } from "./fixtures";

describe("Assumptions", () => {
  it("opens the exact changed target and the consumer site from a row", async () => {
    const fixture = brokenPaymentRetry();
    const onOpenTarget = vi.fn();
    const onOpenCodeLocation = vi.fn();
    const onAsk = vi.fn();
    render(
      <Assumptions
        projection={fixture.projection}
        targets={fixture.targets}
        checks={fixture.checks}
        onOpenTarget={onOpenTarget}
        onOpenCodeLocation={onOpenCodeLocation}
        onAsk={onAsk}
      />,
    );
    const group = screen.getByTestId("assumption-group-idempotency");
    const claim = within(group).getAllByTestId("assumption-row")[0];
    expect(claim).toHaveAttribute("data-state", "contradicted");
    expect(within(claim).getByText("Author claim")).toBeInTheDocument();
    expect(within(claim).getByText(/^Contradicted by pytest/)).toBeInTheDocument();

    await userEvent.click(within(claim).getByRole("button", { name: "src/payments/retry.py:10–30 · charge_with_retry" }));
    expect(onOpenTarget).toHaveBeenCalledWith("pay-retry");

    const signature = screen.getByTestId("assumption-group-precondition");
    await userEvent.click(within(signature).getByRole("button", { name: "src/checkout/submit.py:41 · submit_order" }));
    expect(onOpenCodeLocation).toHaveBeenCalledWith({ path: "src/checkout/submit.py", line: 41, endLine: 41 });

    // AI rows say what they are; no Challenge control renders without a Challenge flow.
    const ai = within(group).getAllByTestId("assumption-row").find((row) => row.dataset.origin === "model_suggested")!;
    expect(within(ai).getByText("AI suggestion")).toBeInTheDocument();
    expect(within(ai).getByText("Unverified")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Challenge" })).toBeNull();
    await userEvent.click(within(ai).getByRole("button", { name: "Ask" }));
    expect(onAsk).toHaveBeenCalledWith("pay-retry");
  });

  it("says what it looked at on a clean change instead of inventing risk", () => {
    const fixture = INVESTIGATION_FIXTURES.cleanNoGraph();
    render(<Assumptions projection={fixture.projection} targets={fixture.targets} checks={fixture.checks} onOpenTarget={() => undefined} />);
    expect(screen.queryAllByTestId("assumption-row")).toEqual([]);
    expect(screen.getByTestId("assumptions-empty")).toHaveTextContent("1 claim, 0 declared assumptions");
    expect(screen.getByTestId("assumptions-empty")).toHaveTextContent("not the same as safe");
  });

  it("drops a checked state when a new Snapshot changes the source", () => {
    const { before, after } = orderSnapshots();
    const props = { onOpenTarget: () => undefined };
    const { rerender } = render(<Assumptions projection={before.projection} targets={before.targets} checks={before.checks} {...props} />);
    expect(screen.getByText("Checked by pytest tests/orders")).toBeInTheDocument();
    rerender(<Assumptions projection={after.projection} targets={after.targets} checks={after.checks} {...props} />);
    expect(screen.getByTestId("assumptions")).toHaveAttribute("data-snapshot", "review-orders@rev-order-2");
    expect(screen.queryByText(/^Checked by/)).toBeNull();
    expect(screen.getByText("Unverified")).toBeInTheDocument();
    expect(screen.getByText(/from Snapshot 0, not re-run on this one/)).toBeInTheDocument();
    expect(screen.getByText(/changed since review/)).toBeInTheDocument();
  });
});
