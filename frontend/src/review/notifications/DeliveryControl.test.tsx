import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { hostedJson } from "../hostedOnly";
import { DeliveryControl } from "./DeliveryControl";
import { fetchDelivery, saveDelivery } from "./notificationsApi";

vi.mock("../hostedOnly", () => ({ hostedJson: vi.fn() }));
const ask = vi.mocked(hostedJson);
const DEFAULTS = ["review_requested", "work_returned"];
const prefs = (delivery: "immediate" | "digest", version = 3) => ({ delivery, version, enabled_classes: DEFAULTS, default_classes: DEFAULTS });

beforeEach(() => ask.mockReset());
afterEach(cleanup);

describe("Delivery: Immediately | Daily digest (SPEC 4.13)", () => {
  it("shows the person's choice, saves the other with the version it read, and keeps their classes as default", async () => {
    ask.mockResolvedValueOnce(prefs("immediate"));
    render(<DeliveryControl />);
    const digest = await screen.findByRole("button", { name: "Daily digest" });
    expect(screen.getByRole("button", { name: "Immediately" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("Digest at 9:00 · One message instead of each one")).toBeTruthy();
    ask.mockResolvedValueOnce(prefs("digest", 4));
    await userEvent.click(digest);
    expect(ask).toHaveBeenLastCalledWith("/v1/reviews/notification-preferences", {
      method: "PUT",
      body: JSON.stringify({ enabled_classes: null, delivery: "digest", version: 3 }),
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Daily digest" }).getAttribute("aria-pressed")).toBe("true"));
  });

  it("keeps an explicit narrowing of classes when saving", async () => {
    ask.mockResolvedValueOnce({ ...prefs("immediate"), enabled_classes: ["review_requested"] });
    const current = await fetchDelivery();
    ask.mockResolvedValueOnce(prefs("digest", 4));
    await saveDelivery(current!, "digest");
    expect(JSON.parse((ask.mock.calls[1]![1] as { body: string }).body).enabled_classes).toEqual(["review_requested"]);
  });

  it("draws nothing where the server has no preferences (a local install)", async () => {
    ask.mockResolvedValueOnce(null);
    const view = render(<DeliveryControl />);
    await waitFor(() => expect(ask).toHaveBeenCalled());
    expect(view.container.querySelector("[data-testid=delivery-control]")).toBeNull();
  });
});
