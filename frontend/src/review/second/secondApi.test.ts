import { beforeEach, describe, expect, it, vi } from "vitest";

import { requestBrowserJson } from "../reviewApi";
import { fetchReviewEnvironment } from "../reviewEnvironment";
import { fetchSecondReviewer, resetSecondReviewerSupport } from "./secondApi";

vi.mock("../reviewApi", () => ({ requestBrowserJson: vi.fn() }));
vi.mock("../reviewEnvironment", () => ({ fetchReviewEnvironment: vi.fn() }));

const ask = vi.mocked(requestBrowserJson);
const environment = vi.mocked(fetchReviewEnvironment);

beforeEach(() => {
  resetSecondReviewerSupport();
  ask.mockReset();
  environment.mockReset();
});

describe("fetchSecondReviewer (SPEC 7.1 P0-2: hosted only)", () => {
  it("a local install is known to have none from its own discovery, and is never asked", async () => {
    environment.mockResolvedValue({ mode: "local" } as never);
    expect(await fetchSecondReviewer("r1")).toBeNull();
    expect(ask).not.toHaveBeenCalled();
  });

  it("a hosted server is asked, and its answer is returned as it is", async () => {
    environment.mockResolvedValue({ mode: "managed_shared" } as never);
    ask.mockResolvedValue({ state: "waiting" } as never);
    expect(await fetchSecondReviewer("r1")).toEqual({ state: "waiting" });
    expect(ask).toHaveBeenCalledWith("/api/reviews/r1/second-reviewer");
  });

  it("an older hosted server without the route is absent, and is asked only once", async () => {
    environment.mockResolvedValue({ mode: "managed_shared" } as never);
    ask.mockRejectedValue(Object.assign(new Error("Not Found"), { status: 404 }));
    expect(await fetchSecondReviewer("r1")).toBeNull();
    expect(await fetchSecondReviewer("r2")).toBeNull();
    expect(ask).toHaveBeenCalledTimes(1);
  });

  it("a missing review is not 'no second reviewer here': it throws and is asked again", async () => {
    environment.mockResolvedValue({ mode: "managed_shared" } as never);
    ask.mockRejectedValue(Object.assign(new Error("the Review object is not available"), { status: 404 }));
    await expect(fetchSecondReviewer("r1")).rejects.toThrow("not available");
    await expect(fetchSecondReviewer("r1")).rejects.toThrow("not available");
    expect(ask).toHaveBeenCalledTimes(2);
  });

  it("a server that is down never reads as absent", async () => {
    environment.mockResolvedValue({ mode: "customer_hosted" } as never);
    ask.mockRejectedValue(Object.assign(new Error("502"), { status: 502 }));
    await expect(fetchSecondReviewer("r1")).rejects.toThrow("502");
  });
});
