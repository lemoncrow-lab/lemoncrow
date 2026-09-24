/** Local gets a Review-only capability; hosted Authward credentials remain HttpOnly. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __setBootstrapForTest,
  adoptBootstrapFragment,
  currentReviewId,
  applyChangeProposal,
  createChangeProposal,
  deliverFeedback,
  exportFeedback,
  fetchChangeProposals,
  fetchFile,
  fetchLiveWebPreview,
  fetchMarkdownImage,
  fetchMediaFile,
  fetchOverview,
  fetchReviewEvidence,
  fetchReviewPreparation,
  fetchProposalSelection,
  fetchSourceComparison,
  fetchSourceState,
  fetchTargets,
  finishReview,
  hasToken,
  historicalReviewHref,
  postBulkReviewed,
  postMark,
  reviewHref,
  sourceCompareHref,
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
  it("keeps review bootstrap state but scrubs a legacy bearer token", () => {
    navigateTo("/review#t=secret-token&r=rev-123&p=prepare-7");
    const bootstrap = adoptBootstrapFragment();
    expect(bootstrap).toEqual({ token: "", reviewId: "rev-123", preparationId: "prepare-7" });
    expect(currentReviewId()).toBe("rev-123");
    expect(hasToken()).toBe(false);
    expect(window.location.hash).toBe("#r=rev-123&p=prepare-7");
    expect(window.location.href).not.toContain("secret-token");
    expect(JSON.stringify(sessionStorage)).not.toContain("secret-token");
    expect(JSON.stringify(localStorage)).not.toContain("secret-token");
  });

  it("restores non-secret review state after a reload", () => {
    navigateTo("/review#r=rev-123&p=prepare-7");
    adoptBootstrapFragment();
    __setBootstrapForTest({ token: "", reviewId: "" });
    navigateTo("/review");
    expect(adoptBootstrapFragment()).toEqual({ token: "", reviewId: "rev-123", preparationId: "prepare-7" });
  });

  it("never reports a JavaScript-visible machine credential", () => {
    navigateTo("/review");
    expect(adoptBootstrapFragment().token).toBe("");
    expect(hasToken()).toBe(false);
  });
});

describe("canonical Review links", () => {
  it("uses the exact durable id with only the public r/ and rr/ namespaces", () => {
    const reviewId = "01abc23401ab723481abc23401abc234";
    const revisionId = "01def67801de723481def67801def678";
    expect(reviewHref(reviewId)).toBe(`/r/${reviewId}`);
    expect(reviewHref(`r/${reviewId}`)).toBe(`/r/${reviewId}`);
    expect(historicalReviewHref(revisionId)).toBe(`/rr/${revisionId}`);
    expect(historicalReviewHref(`rr/${revisionId}`)).toBe(`/rr/${revisionId}`);
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

  it("never constructs a machine Authorization header", async () => {
    __setBootstrapForTest({ token: "secret-token", reviewId: "rev-1" });
    const spy = stubFetch(200, { session: {} });
    await fetchOverview("rev-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1");
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
    expect(init.credentials).toBe("same-origin");
  });

  it("claims a local Review capability once and retries without exposing the machine bearer", async () => {
    navigateTo("/reviews/r-12345678");
    const spy = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "missing browser capability" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: "browser-capability", expires_at: 9999999999 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ session: { id: "rev-1" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", spy);

    await fetchOverview("rev-1");

    expect(spy).toHaveBeenCalledTimes(3);
    expect(spy.mock.calls[0]?.[0]).toBe("/api/reviews/rev-1");
    expect(spy.mock.calls[1]?.[0]).toBe("/v1/auth/local-browser/claim");
    expect(JSON.parse(String(spy.mock.calls[1]?.[1]?.body))).toEqual({ path: "/reviews/r-12345678" });
    expect(spy.mock.calls[2]?.[0]).toBe("/api/reviews/rev-1");
    const retriedHeaders = new Headers(spy.mock.calls[2]?.[1]?.headers);
    expect(retriedHeaders.get("X-LemonCrow-Browser-Session")).toBe("browser-capability");
    expect(retriedHeaders.get("Authorization")).toBeNull();
    expect(localStorage.getItem("lemoncrow.review.browserSession")).toBe("browser-capability");
  });

  it("shares one local capability claim across concurrent startup requests", async () => {
    navigateTo("/r/12345678");
    let claimCount = 0;
    const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/auth/local-browser/claim") {
        claimCount += 1;
        await Promise.resolve();
        return new Response(JSON.stringify({ token: "shared-browser-capability" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      const capability = new Headers(init?.headers).get("X-LemonCrow-Browser-Session");
      return capability
        ? new Response(JSON.stringify(url.includes("/targets") ? { targets: [] } : { session: {} }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        : new Response(JSON.stringify({ detail: "missing browser capability" }), { status: 401 });
    });
    vi.stubGlobal("fetch", spy);

    await Promise.all([fetchOverview("rev-1"), fetchTargets("rev-1")]);

    expect(claimCount).toBe(1);
    expect(localStorage.getItem("lemoncrow.review.browserSession")).toBe("shared-browser-capability");
    const retried = spy.mock.calls.filter(([, init]) =>
      new Headers(init?.headers).get("X-LemonCrow-Browser-Session") === "shared-browser-capability",
    );
    expect(retried).toHaveLength(2);
  });

  it("reuses the origin-scoped Review capability in a fresh module state", async () => {
    localStorage.setItem("lemoncrow.review.browserSession", "persisted-browser-capability");
    __setBootstrapForTest({ token: "machine-secret-that-must-be-ignored", reviewId: "rev-1" });
    const spy = stubFetch(200, { session: { id: "rev-1" } });

    await fetchOverview("rev-1");

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("X-LemonCrow-Browser-Session")).toBe("persisted-browser-capability");
    expect(headers.get("Authorization")).toBeNull();
  });

  it("falls back to hosted Authward BFF refresh when the local pairing endpoint is absent", async () => {
    __setBootstrapForTest({ token: "", reviewId: "rev-1" });
    const spy = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "not found" }), { status: 404 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ refreshed: true }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "rev-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", spy);

    await fetchOverview("rev-1");

    expect(spy).toHaveBeenCalledTimes(4);
    expect(spy.mock.calls[0]?.[0]).toBe("/api/reviews/rev-1");
    expect(spy.mock.calls[1]?.[0]).toBe("/v1/auth/local-browser/claim");
    expect(spy.mock.calls[2]?.[0]).toBe("/v1/auth/browser/refresh");
    expect(spy.mock.calls[2]?.[1]).toMatchObject({ method: "POST", credentials: "same-origin" });
    expect(spy.mock.calls[3]?.[0]).toBe("/api/reviews/rev-1");
  });

  it("keeps preparation generation in the fragment while scrubbing legacy credentials", async () => {
    navigateTo("/review#t=secret-token&r=rev-123&p=prepare-7");
    expect(adoptBootstrapFragment()).toEqual({ token: "", reviewId: "rev-123", preparationId: "prepare-7" });
    expect(window.location.href).not.toContain("secret-token");
    const spy = stubFetch(200, { state: "impact_background", detail: "" });
    const status = await fetchReviewPreparation("rev-123", "prepare-7");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-123/preparation?preparation_id=prepare-7");
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
    expect(status.state).toBe("impact_background");
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

  it("pins rich preview requests to the file projection's exact revision", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { url: "http://127.0.0.1:9999/preview" });

    await fetchMarkdownImage("rev-1", "README.md", "new", "docs/demo.gif", "rrv-7");
    await fetchMediaFile("rev-1", "docs/demo.gif", "new", "rrv-7");
    await fetchLiveWebPreview("rev-1", "frontend/src/main.tsx", "new", "/", "rrv-7");

    const urls = (spy.mock.calls as unknown as Array<[string, RequestInit?]>).map(([url]) => String(url));
    expect(urls[0]).toContain("revision_id=rrv-7");
    expect(urls[1]).toContain("revision_id=rrv-7");
    expect(urls[2]).toContain("revision_id=rrv-7");
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

  it("delivers exactly the prepared feedback operation through the generic route", async () => {
    __setBootstrapForTest({ token: "secret-token", reviewId: "rev-1" });
    const spy = stubFetch(200, {
      state: "sent",
      target_ref: "codex:session",
      remote_ref: "pid:42",
      message: "sent",
      annotation_count: 2,
      operation_id: "fop-1",
    });
    const result = await deliverFeedback("rev-1", {
      markdown: "## Review feedback",
      open: 2,
      orphaned: 0,
      resolved: 0,
      revision_id: "rrv-7",
      feedback_hash: "abc123",
      operation_id: "fop-1",
      annotation_versions: { "ann-1": 2 },
      delivery: { supported: true, host: "codex", session_id: "session", target_ref: "codex:session", label: "Codex", reason: "" },
    });
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/feedback/deliver");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      operation_id: "fop-1",
      expected_revision_id: "rrv-7",
      expected_feedback_hash: "abc123",
    });
    expect(result.annotation_count).toBe(2);
  });

  it("lists revision-bound review evidence through the authenticated review route", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { revision_id: "rrv-1", evidence: [] });
    await fetchReviewEvidence("rev-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/evidence");
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
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
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
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

  it("builds and fetches generic source comparison URLs", async () => {
    expect(sourceCompareHref("git/feature/foo", "worktree")).toBe("/r/x/git/feature/foo..worktree");
    expect(sourceCompareHref("rr/11111111", "rr/22222222")).toBe(
      "/r/x/rr/11111111..rr/22222222",
    );
    expect(sourceCompareHref("rr/11111111", "rr/22222222", "r/aaaaaaaa")).toBe(
      "/r/aaaaaaaa/compare/11111111..22222222",
    );
    expect(sourceCompareHref("git/main", "git/HEAD", "r/aaaaaaaa")).toBe(
      "/r/x/git/main..git/HEAD?scope=r%2Faaaaaaaa",
    );

    const spy = stubFetch(200, {
      from: { spec: "git/HEAD~1", label: "HEAD~1", kind: "git" },
      to: { spec: "git/HEAD", label: "HEAD", kind: "git" },
      summary: { files: 0, additions: 0, deletions: 0 },
      files: [],
    });
    await fetchSourceComparison("git/HEAD~1", "git/HEAD");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/compare?from_ref=git%2FHEAD%7E1&to_ref=git%2FHEAD");
    expect(init.method).toBeUndefined();
  });

  it("uses dedicated source-proposal routes without reconstructing patches in the browser", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { proposal: { id: "rcp-1" }, proposals: [] });

    await fetchProposalSelection("rev-1", "src/a b.py", 4, 5, "additions");
    await fetchChangeProposals("rev-1");
    await createChangeProposal("rev-1", {
      expected_revision_id: "rrv-1",
      path: "src/a b.py",
      start_line: 4,
      end_line: 5,
      side: "additions",
      replacement_text: "replacement\n",
      target_unit_key: "sym-1",
      intent: "Fix this.",
    });
    await applyChangeProposal("rcp-1");

    const calls = spy.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0][0]).toBe("/api/reviews/rev-1/proposal-selection?path=src%2Fa+b.py&start_line=4&end_line=5&side=additions");
    expect(calls[0][1].method).toBeUndefined();
    expect(calls[1][0]).toBe("/api/reviews/rev-1/proposals");
    expect(calls[2][0]).toBe("/api/reviews/rev-1/proposals");
    expect(calls[2][1].method).toBe("POST");
    expect(JSON.parse(String(calls[2][1].body))).toEqual({
      expected_revision_id: "rrv-1",
      path: "src/a b.py",
      start_line: 4,
      end_line: 5,
      side: "additions",
      replacement_text: "replacement\n",
      target_unit_key: "sym-1",
      intent: "Fix this.",
    });
    expect(calls[3][0]).toBe("/api/proposals/rcp-1/apply");
    expect(calls[3][1].method).toBe("POST");
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
