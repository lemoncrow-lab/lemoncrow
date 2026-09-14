/**
 * The credential rules, asserted rather than asserted-in-a-comment.
 *
 * The workspace token is per-process. It reaches the page through the URL
 * fragment, stays in the address bar so ordinary browser copy/paste reopens the
 * review, and is also retained for this tab/origin so F5 keeps working.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __setBootstrapForTest,
  adoptBootstrapFragment,
  currentReviewId,
  deliverFeedbackToClaude,
  exportFeedback,
  fetchFile,
  fetchOverview,
  fetchReviewEvidence,
  fetchSourceState,
  fetchTargets,
  finishReview,
  hasToken,
  postBulkReviewed,
  postMark,
  uploadReviewEvidence,
} from "./reviewApi";

function navigateTo(url: string) {
  window.history.replaceState(null, "", url);
}

beforeEach(() => {
  __setBootstrapForTest({ token: "", reviewId: "" });
  localStorage.clear();
  sessionStorage.clear();
  navigateTo("/review");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bootstrap fragment", () => {
  it("reads the token and review id out of the fragment", () => {
    navigateTo("/review#t=secret-token&r=rev-123");
    const bootstrap = adoptBootstrapFragment();
    expect(bootstrap.token).toBe("secret-token");
    expect(bootstrap.reviewId).toBe("rev-123");
    expect(currentReviewId()).toBe("rev-123");
    expect(hasToken()).toBe(true);
  });

  it("keeps the authenticated fragment in the address bar so normal copy and paste works", () => {
    navigateTo("/review#t=secret-token&r=rev-123");
    adoptBootstrapFragment();
    expect(window.location.hash).toBe("#t=secret-token&r=rev-123");
    expect(window.location.href).toContain("secret-token");
  });

  it("keeps the token tab-scoped, never in localStorage or a cookie", () => {
    navigateTo("/review#t=secret-token&r=rev-123");
    adoptBootstrapFragment();
    expect(JSON.stringify(localStorage)).not.toContain("secret-token");
    expect(sessionStorage.getItem("lemoncrow.review.token")).toBe("secret-token");
    expect(sessionStorage.getItem("lemoncrow.review.id")).toBe("rev-123");
    expect(document.cookie).not.toContain("secret-token");
  });

  it("restores the workspace after a browser reload", () => {
    navigateTo("/review#t=secret-token&r=rev-123");
    adoptBootstrapFragment();
    // Simulate a fresh JS module after F5 while the tab's sessionStorage stays.
    __setBootstrapForTest({ token: "", reviewId: "" });
    navigateTo("/review");
    expect(adoptBootstrapFragment()).toEqual({ token: "secret-token", reviewId: "rev-123" });
    expect(hasToken()).toBe(true);
    expect(window.location.hash).toBe("#t=secret-token&r=rev-123");
  });

  it("survives a second call with the fragment still present", () => {
    // React 18 double-invokes effects in StrictMode; a second read must not
    // disturb the credential the first one captured.
    navigateTo("/review#t=secret-token&r=rev-123");
    adoptBootstrapFragment();
    const again = adoptBootstrapFragment();
    expect(again.token).toBe("secret-token");
    expect(again.reviewId).toBe("rev-123");
  });

  it("reports no token when the page was opened without one", () => {
    navigateTo("/review");
    expect(adoptBootstrapFragment().token).toBe("");
    expect(hasToken()).toBe(false);
});
});

describe("requests", () => {
  function stubFetch(status: number, body: unknown) {
    const spy = vi.fn(async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", spy);
    return spy;
  }

  it("sends the token as a bearer header and no credentials", async () => {
    __setBootstrapForTest({ token: "secret-token", reviewId: "rev-1" });
    const spy = stubFetch(200, { session: {} });
    await fetchOverview("rev-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer secret-token");
    expect(init.credentials).toBe("omit");
  });

  it("never puts the token in the URL", async () => {
    __setBootstrapForTest({ token: "secret-token", reviewId: "rev-1" });
    const spy = stubFetch(200, {});
    await fetchFile("rev-1", "src/session.py");
    const [url] = spy.mock.calls[0] as unknown as [string];
    expect(url).not.toContain("secret-token");
    expect(url).toBe("/api/reviews/rev-1/files/src/session.py/patch");
  });

  it("escapes each path segment without eating the separators", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, {});
    await fetchFile("rev-1", "src/a b/c#d.py");
    const [url] = spy.mock.calls[0] as unknown as [string];
    expect(url).toBe("/api/reviews/rev-1/files/src/a%20b/c%23d.py/patch");
  });

  it("surfaces the server's refusal text rather than a generic failure", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    stubFetch(403, { detail: "Origin 'http://localhost:9999' is not this workspace" });
    await expect(fetchOverview("rev-1")).rejects.toThrow("is not this workspace");
  });

  it("renders the feedback bundle through the same route the CLI uses", async () => {
    __setBootstrapForTest({ token: "secret-token", reviewId: "rev-1" });
    const spy = stubFetch(200, { markdown: "## Review feedback", open: 1, orphaned: 0, resolved: 0 });
    const bundle = await exportFeedback("rev-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/feedback/export");
    expect(init.method).toBe("POST");
    expect(bundle.markdown).toBe("## Review feedback");
  });

  it("delivers feedback through the explicit Claude route", async () => {
    __setBootstrapForTest({ token: "secret-token", reviewId: "rev-1" });
    const spy = stubFetch(200, {
      state: "sent",
      target_ref: "claude:session",
      remote_ref: "deadbeef",
      message: "sent",
      annotation_count: 2,
    });
    const result = await deliverFeedbackToClaude("rev-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/feedback/claude");
    expect(init.method).toBe("POST");
    expect(result.annotation_count).toBe(2);
  });

  it("lists revision-bound review evidence through the authenticated review route", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { revision_id: "rrv-1", evidence: [] });
    await fetchReviewEvidence("rev-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/evidence");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t");
  });

  it("classifies a Playwright trace upload without wrapping it in multipart", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(201, { evidence: { id: "evd-1" } });
    const file = new File(["trace"], "checkout-trace.zip", { type: "application/zip" });
    await uploadReviewEvidence("rev-1", file, "src/checkout.tsx");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/reviews/rev-1/evidence/upload?");
    expect(url).toContain("kind=playwright_trace");
    expect(url).toContain("path=src%2Fcheckout.tsx");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(file);
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/zip");
  });

  it("fetches reader targets with an explicit order", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { revision_id: "rrv-1", order: "file", targets: [], progress: {}, outline: [] });
    await fetchTargets("rev-1", "file");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/targets?order=file");
    expect(init.method).toBeUndefined();
  });

  it("checks source state through a read-only route", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { supported: true, changed: false, fingerprint: "abc" });
    await fetchSourceState("rev-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/source-state");
    expect(init.method).toBeUndefined();
  });

  it("finishes and reopens a review through one route", async () => {
    __setBootstrapForTest({ token: "secret-token", reviewId: "rev-1" });
    const spy = stubFetch(200, { status: "finished" });
    await finishReview("rev-1");
    await finishReview("rev-1", "open");
    const calls = spy.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map((call) => JSON.parse(String(call[1].body)))).toEqual([
      { status: "finished" },
      { status: "open" },
    ]);
    expect(calls[0][0]).toBe("/api/reviews/rev-1/finish");
  });

  it("posts a mark as JSON", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { recorded: "reviewed" });
    await postMark("rev-1", "fil:abc", "reviewed");
    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ unit_key: "fil:abc", state: "reviewed" });
  });

  it("posts chapter close-out keys without a disposition override", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { marked: [], skipped: [], group_counts: {} });
    await postBulkReviewed("rev-1", ["fil:a", "fil:b"]);
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/marks/bulk");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ unit_keys: ["fil:a", "fil:b"] });
  });
});
