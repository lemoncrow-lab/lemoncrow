import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ fetchReviewEvidenceContent: vi.fn() }));
vi.mock("./reviewApi", () => api);

import ScreenshotEvidenceCompare from "./ScreenshotEvidenceCompare";
import type { ReviewEvidence } from "./types";

function evidence(id: string, title = ""): ReviewEvidence {
  return { id, title, path: `screens/${id}.png` } as unknown as ReviewEvidence;
}

describe("ScreenshotEvidenceCompare", () => {
  beforeEach(() => {
    let next = 0;
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => `blob:shot-${++next}`) });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  });

  afterEach(() => {
    cleanup();
    api.fetchReviewEvidenceContent.mockReset();
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });

  it("names a failed evidence fetch instead of comparing a partial pair", async () => {
    api.fetchReviewEvidenceContent.mockImplementation(async (id: string) => {
      if (id === "ev-before") throw new Error("baseline screenshot was not preserved");
      return new Blob(["png"]);
    });
    render(<ScreenshotEvidenceCompare before={evidence("ev-before")} after={evidence("ev-after", "Checkout")} />);

    expect(await screen.findByText("Screenshot comparison unavailable: baseline screenshot was not preserved")).toBeTruthy();
    expect(screen.queryByText("Loading exact revision screenshots…")).toBeNull();
    expect(screen.queryByTestId("image-overlay-compare")).toBeNull();
    expect(screen.getByRole("region", { name: "Screenshot diff Checkout" })).toBeTruthy();
  });

  it("revokes the pair's object URLs when it unmounts", async () => {
    api.fetchReviewEvidenceContent.mockResolvedValue(new Blob(["png"]));
    const view = render(<ScreenshotEvidenceCompare before={evidence("ev-before")} after={evidence("ev-after")} />);

    expect(await screen.findByTestId("image-overlay-compare")).toBeTruthy();
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:shot-1");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:shot-2");
  });
});
