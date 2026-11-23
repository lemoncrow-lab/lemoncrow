import { beforeEach, describe, expect, it, vi } from "vitest";

import { requestBrowserJson } from "../reviewApi";
import { fetchReviewEnvironment } from "../reviewEnvironment";
import { fetchTeammates, handOffReview, releaseReview, sendPresence, takeReview } from "./teamApi";

vi.mock("../reviewApi", () => ({ requestBrowserJson: vi.fn() }));
vi.mock("../reviewEnvironment", () => ({ fetchReviewEnvironment: vi.fn() }));

const ask = vi.mocked(requestBrowserJson);
const environment = vi.mocked(fetchReviewEnvironment);

beforeEach(() => {
  ask.mockReset();
  environment.mockReset();
});

describe("presence (SPEC 7.1 P0-2: hosted only)", () => {
  it("a local install is known to have no team from its own discovery, and is never asked", async () => {
    environment.mockResolvedValue({ mode: "local", discovered: true } as never);
    expect(await sendPresence("r1")).toBeNull();
    expect(ask).not.toHaveBeenCalled();
  });

  it("a hosted server gets one POST and its answer comes back as it is", async () => {
    environment.mockResolvedValue({ mode: "managed_shared", discovered: true } as never);
    ask.mockResolvedValue({ reviewing: [], claim: null, handoff: null } as never);
    expect(await sendPresence("r 1")).toEqual({ reviewing: [], claim: null, handoff: null });
    expect(ask).toHaveBeenCalledWith("/api/reviews/r%201/team/presence", { method: "POST", body: "{}" });
  });

  it("an older hosted server without the route reads as no team", async () => {
    environment.mockResolvedValue({ mode: "managed_shared", discovered: true } as never);
    ask.mockRejectedValue(Object.assign(new Error("Not Found"), { status: 404 }));
    expect(await sendPresence("r1")).toBeNull();
  });

  it("a discovery that failed is a failure, never 'local' and never 'no team'", async () => {
    environment.mockResolvedValue({ mode: "local", discovered: false } as never);
    await expect(sendPresence("r1")).rejects.toThrow("Couldn't reach the server");
    expect(ask).not.toHaveBeenCalled();
  });

  it("a missing review is not 'no team here', and a server that is down never reads as absent", async () => {
    environment.mockResolvedValue({ mode: "customer_hosted", discovered: true } as never);
    ask.mockRejectedValue(Object.assign(new Error("no such review"), { status: 404 }));
    await expect(sendPresence("r1")).rejects.toThrow("no such review");
    ask.mockRejectedValue(Object.assign(new Error("502"), { status: 502 }));
    await expect(sendPresence("r1")).rejects.toThrow("502");
    expect(ask).toHaveBeenCalledTimes(2);
  });
});

describe("claim and handoff", () => {
  it("send the shapes the server reads", async () => {
    ask.mockResolvedValue({} as never);
    await takeReview("r1");
    expect(ask).toHaveBeenLastCalledWith("/api/reviews/r1/team/claim", { method: "POST", body: "{}" });
    await releaseReview("r1");
    expect(ask).toHaveBeenLastCalledWith("/api/reviews/r1/team/release", { method: "POST", body: "{}" });
    await handOffReview("r1", "usr_maya", "Start at auth.");
    expect(ask).toHaveBeenLastCalledWith("/api/reviews/r1/team/handoff", { method: "POST", body: JSON.stringify({ to: "usr_maya", note: "Start at auth." }) });
    ask.mockResolvedValue({ teammates: [{ id: "usr_maya", name: "Maya Chen", away: null }] } as never);
    expect(await fetchTeammates("r1")).toEqual([{ id: "usr_maya", name: "Maya Chen", away: null }]);
    expect(ask).toHaveBeenLastCalledWith("/api/reviews/r1/team/teammates");
  });
});
