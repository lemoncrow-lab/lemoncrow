import { afterEach, describe, expect, it } from "vitest";
import { clearOfflineSend, readOfflineSend, saveOfflineSend, serverAcceptedSend, type OfflineReviewSend } from "./offlineReviewSend";

const OPERATION = `fs-${"a".repeat(32)}`;
const DRAFT: OfflineReviewSend = {
  reviewRef: "shop", reviewId: "local-id", revisionId: "rev-1",
  operationId: OPERATION, outcome: "lgtm", summary: "Approved", feedback: null,
  createdAt: "2026-11-06T10:00:00Z", attempted: false, state: "waiting", error: "",
};
afterEach(() => { clearOfflineSend(OPERATION); });

describe("P2-5 client-local offline intent", () => {
  it("survives a cold load in the same tab, without claiming a server receipt", () => {
    expect(saveOfflineSend(DRAFT)).toBe(true);
    expect(readOfflineSend()).toEqual(DRAFT);
    expect(serverAcceptedSend({ state: "none", operation_id: "" } as never, DRAFT)).toBe(false);
    expect(serverAcceptedSend({ state: "pending", operation_id: OPERATION } as never, DRAFT)).toBe(true);
    expect(serverAcceptedSend({ state: "sent", operation_id: "fs-" + "b".repeat(32) } as never, DRAFT)).toBe(false);
    clearOfflineSend(OPERATION);
    expect(readOfflineSend()).toBeNull();
  });

  it("rejects corrupted or oversized stored input", () => {
    window.sessionStorage.setItem("lc.review.offline-send.v1", JSON.stringify({ ...DRAFT, operationId: "other" }));
    expect(readOfflineSend()).toBeNull();
    expect(saveOfflineSend({ ...DRAFT, summary: "x".repeat(33_000) })).toBe(false);
  });

  it("keeps the same operation identity after a network attempt", () => {
    expect(saveOfflineSend({ ...DRAFT, attempted: true })).toBe(true);
    expect(readOfflineSend()).toMatchObject({ operationId: OPERATION, attempted: true, state: "waiting" });
  });
});
