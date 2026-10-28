/** Local gets a Review-only capability; hosted Authward credentials remain HttpOnly. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __setBootstrapForTest,
  adoptBootstrapFragment,
  currentReviewId,
  applyChangeProposal,
  createChangeProposal,
  deliverFeedback,
  detachHostedAttachment,
  exportFeedback,
  fetchHostedAttachment,
  fetchChangeProposals,
  fetchFile,
  fetchLiveWebPreview,
  fetchSurfaceImpact,
  fetchSurfaceImpactWithRetry,
  fetchMarkdownImage,
  fetchMediaFile,
  fetchOverview,
  fetchReviewEvidence,
  fetchReviewReadiness,
  armReviewLanding,
  cancelReviewLanding,
  fetchReviewVerification,
  fetchReviewExecutionArtifact,
  fetchReviewDependencies,
  fetchReviewSurfaces,
  fetchSurfacePreview,
  fetchReviewAsk,
  fetchReviewPreparation,
  fetchReviewMap,
  fetchHistoricalReviewMap,
  fetchProposalSelection,
  fetchSourceComparison,
  fetchSourceState,
  fetchTargets,
  finishReview,
  hasToken,
  historicalReviewHref,
  LOCAL_BROWSER_RECONNECT_MESSAGE,
  postBulkReviewed,
  postMark,
  postReviewAsk,
  resolveHostedAction,
  reviewCompareHref,
  runReviewSurface,
  runRuntimeValidation,
  fetchRuntimeValidationJob,
  cancelRuntimeValidationJob,
  sendReviewAskHandoff,
  reviewHref,
  sourceCompareHref,
  streamReviewAsk,
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

  it("carries a non-secret source-ready hint for progressive Review startup", () => {
    navigateTo("/review#r=rev-123&p=prepare-7&sr=1");
    const bootstrap = adoptBootstrapFragment();
    expect(bootstrap.reviewId).toBe("rev-123");
    expect(bootstrap.preparationId).toBe("prepare-7");
    expect(bootstrap.sourceReady).toBe(true);
  });

  it("restores non-secret review state after a reload", () => {
    navigateTo("/review#r=rev-123&p=prepare-7");
    adoptBootstrapFragment();
    __setBootstrapForTest({ token: "", reviewId: "" });
    navigateTo("/review");
    expect(adoptBootstrapFragment()).toEqual({ token: "", reviewId: "rev-123", preparationId: "prepare-7" });
  });

  it("opens a named saved Review without resuming an old preparation job", () => {
    navigateTo("/review#r=rev-123&p=prepare-7");
    adoptBootstrapFragment();
    __setBootstrapForTest({ token: "", reviewId: "" });
    navigateTo("/r/5f5be0d3");

    expect(adoptBootstrapFragment({ restorePreparation: false })).toEqual({
      token: "", reviewId: "rev-123",
    });
  });

  it("preserves an explicit new-review preparation even on a named route", () => {
    navigateTo("/r/5f5be0d3#r=rev-123&p=prepare-7&sr=1");

    expect(adoptBootstrapFragment({ restorePreparation: false })).toEqual({
      token: "", reviewId: "rev-123", preparationId: "prepare-7", sourceReady: true,
    });
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

  it("uses Review-owned latest and historical map routes with stable drill-down ids", async () => {
    const spy = stubFetch(200, { graph: { nodes: [], edges: [] } });

    await fetchReviewMap("rev-1");
    await fetchReviewMap("rev-1", "symbol", "symbol:abc def");
    await fetchHistoricalReviewMap("rev-1", 3, "symbol", "symbol:abc def");

    expect((spy.mock.calls[0] as unknown as [string])[0]).toBe("/api/reviews/rev-1/map?level=architecture");
    expect((spy.mock.calls[1] as unknown as [string])[0]).toBe("/api/reviews/rev-1/map?level=symbol&node_id=symbol%3Aabc+def");
    expect((spy.mock.calls[2] as unknown as [string])[0]).toBe("/api/reviews/rev-1/revisions/3/map?level=symbol&node_id=symbol%3Aabc+def");
  });

  it("reads, resolves and detaches a hosted attachment on the Review's own routes", async () => {
    const spy = stubFetch(200, { attached: true, review_id: "rev 1" });

    await fetchHostedAttachment("rev 1");
    await resolveHostedAction("rev 1", "rha/2", "requeue");
    await detachHostedAttachment("rev 1", false);
    await detachHostedAttachment("rev 1", true);

    const calls = spy.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([url, init]) => [url, init.method ?? "GET"])).toEqual([
      ["/api/reviews/rev%201/hosted", "GET"],
      ["/api/reviews/rev%201/hosted/actions/rha%2F2", "POST"],
      ["/api/reviews/rev%201/hosted", "DELETE"],
      ["/api/reviews/rev%201/hosted?discard_unresolved=true", "DELETE"],
    ]);
    expect(JSON.parse(String(calls[1][1].body))).toEqual({ op: "requeue" });
  });

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
    expect(spy.mock.calls[0]?.[0]).toBe("/v1/auth/local-browser/claim");
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

  it("renews an ageing local capability before use without a CLI pairing", async () => {
    const ageing = `v1.${Math.floor(Date.now() / 1000) + 3 * 24 * 60 * 60}.nonce.sig`;
    localStorage.setItem("lemoncrow.review.browserSession", ageing);
    __setBootstrapForTest({ token: "", reviewId: "rev-1" });
    navigateTo("/reviews");
    const spy = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: "renewed-capability" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ session: {} }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", spy);

    await fetchOverview("rev-1");

    expect(spy.mock.calls[0]?.[0]).toBe("/v1/auth/local-browser/claim");
    expect(new Headers(spy.mock.calls[0]?.[1]?.headers).get("X-LemonCrow-Browser-Session")).toBe(ageing);
    expect(new Headers(spy.mock.calls[1]?.[1]?.headers).get("X-LemonCrow-Browser-Session")).toBe("renewed-capability");
    expect(localStorage.getItem("lemoncrow.review.browserSession")).toBe("renewed-capability");
  });

  it("turns an unpaired local claim into a reconnect instruction", async () => {
    navigateTo("/r/12345678");
    const spy = vi.fn(async () => new Response(JSON.stringify({ error: { message: "not paired" } }), { status: 401 }));
    vi.stubGlobal("fetch", spy);

    await expect(fetchOverview("rev-1")).rejects.toThrow(LOCAL_BROWSER_RECONNECT_MESSAGE);
    expect(LOCAL_BROWSER_RECONNECT_MESSAGE).toContain("lc review --open");
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

  it("keeps the reviewer in place with a retryable error when the sign-in service is down", async () => {
    __setBootstrapForTest({ token: "", reviewId: "rev-1" });
    const spy = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "not found" }), { status: 404 }))
      .mockResolvedValueOnce(new Response("upstream down", { status: 503 }));
    vi.stubGlobal("fetch", spy);

    const failure = await fetchOverview("rev-1").catch((reason: unknown) => reason);

    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toBe("Sign-in service is temporarily unavailable. Try again in a moment.");
    expect((failure as { status?: number }).status).toBe(503);
    expect(spy).toHaveBeenCalledTimes(3);
    expect(window.location.pathname).toBe("/review");
  });

  it("routes a refused credential refresh (403) to the access-denied sign-in page", async () => {
    __setBootstrapForTest({ token: "", reviewId: "rev-1" });
    const spy = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "not found" }), { status: 404 }))
      .mockResolvedValueOnce(new Response("denied", { status: 403 }));
    vi.stubGlobal("fetch", spy);

    const failure = await fetchOverview("rev-1").catch((reason: unknown) => reason);

    expect((failure as Error).message).toBe("This account does not have access to this organization.");
    expect((failure as { status?: number }).status).toBe(403);
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
    expect(url).toBe("/api/reviews/rev-1/files/src/session.py/patch?include_web_preview=false");
  });

  it("escapes each path segment without eating the separators", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, {});
    await fetchFile("rev-1", "src/a b/c#d.py");
    const [url] = spy.mock.calls[0] as unknown as [string];
    expect(url).toBe("/api/reviews/rev-1/files/src/a%20b/c%23d.py/patch?include_web_preview=false");
  });

  it("pins rich preview requests to the file projection's exact revision", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { url: "http://127.0.0.1:9999/preview" });

    await fetchMarkdownImage("rev-1", "README.md", "new", "docs/demo.gif", "rrv-7");
    await fetchMediaFile("rev-1", "docs/demo.gif", "new", "rrv-7");
    await fetchLiveWebPreview("rev-1", "frontend/src/main.tsx", "new", "/", "rrv-7");
    await fetchSurfaceImpact("rev-1", "storybook", "web:button--primary", "rrv-7");

    const urls = (spy.mock.calls as unknown as Array<[string, RequestInit?]>).map(([url]) => String(url));
    expect(urls[0]).toContain("revision_id=rrv-7");
    expect(urls[1]).toContain("revision_id=rrv-7");
    expect(urls[2]).toContain("revision_id=rrv-7");
    expect(urls[3]).toContain("/surface-impact?");
    expect(urls[3]).toContain("provider=storybook");
    expect(urls[3]).toContain("surface_id=web%3Abutton--primary");
    expect(urls[3]).toContain("revision_id=rrv-7");
  });

  it("retries transient surface-impact failures but not contract refusals", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const success = {
      revision_id: "rrv-7",
      provider: "web",
      surface_id: "/",
      source: "web",
      comparator: "image",
      changed: true,
      magnitude: 0.4,
      label: "Δ 40.0% pixels",
      reasons: ["visible pixels changed"],
      metrics: {},
    };
    const spy = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "preview worker warming up" }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(success), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", spy);

    await expect(fetchSurfaceImpactWithRetry("rev-1", "web", "/", "rrv-7", 2)).resolves.toEqual(success);
    expect(spy).toHaveBeenCalledTimes(2);

    const refusal = vi.fn(async () => new Response(JSON.stringify({ detail: "not auto-ranked" }), {
      status: 422,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", refusal);
    await expect(fetchSurfaceImpactWithRetry("rev-1", "bruno", "mutate", "rrv-7", 5)).rejects.toThrow("not auto-ranked");
    expect(refusal).toHaveBeenCalledTimes(1);
  });

  it("surfaces the server's refusal text rather than a generic failure", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    stubFetch(403, { detail: "Origin 'http://localhost:9999' is not this workspace" });
    await expect(fetchOverview("rev-1")).rejects.toThrow("is not this workspace");
  });

  it("loads Ask, posts revision-scoped questions, and sends only a preview-bound handoff", async () => {
    const spy = stubFetch(200, { review_id: "rev-1", latest_revision_number: 5, messages: [] });

    await fetchReviewAsk("rev-1");
    let [url, init] = spy.mock.calls.at(-1) as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/ask");
    expect(init.method).toBeUndefined();

    await postReviewAsk("rev-1", {
      revision_number: 5,
      body: "Why is this class needed?",
      path: "src/api.py",
      target_unit_key: "symbol:Authenticator",
      side: "new",
      start_line: 84,
      end_line: 131,
    });
    [url, init] = spy.mock.calls.at(-1) as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/ask");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      revision_number: 5,
      body: "Why is this class needed?",
      path: "src/api.py",
      target_unit_key: "symbol:Authenticator",
      side: "new",
      start_line: 84,
      end_line: 131,
    });

    await sendReviewAskHandoff("rev-1", "ram-1", {
      operation_id: "aop-1",
      instruction_hash: "hash-1",
      expected_revision_id: "rrv-5",
      expected_tree_fingerprint: "tree-5",
      expected_source_fingerprint: "source-5",
    });
    [url, init] = spy.mock.calls.at(-1) as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/ask/ram-1/handoff");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      operation_id: "aop-1",
      instruction_hash: "hash-1",
      expected_revision_id: "rrv-5",
      expected_tree_fingerprint: "tree-5",
      expected_source_fingerprint: "source-5",
    });
  });

  it("hands the Ask stream request the caller's abort signal so Stop cancels the fetch", async () => {
    const result = {
      review_id: "rev-1",
      latest_revision_number: 5,
      human: { id: "ram-1" },
      assistant: { id: "ram-2" },
    };
    const spy = vi.fn(async () => new Response(
      `${JSON.stringify({ type: "accepted", driver: "configured", model: "" })}\n${JSON.stringify({ type: "result", result })}\n`,
      { status: 200, headers: { "Content-Type": "application/x-ndjson" } },
    ));
    vi.stubGlobal("fetch", spy);
    const controller = new AbortController();

    await expect(streamReviewAsk("rev-1", { revision_number: 5, body: "Why?" }, () => {}, controller.signal))
      .resolves.toEqual(result);

    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/ask/stream");
    expect(init.signal).toBe(controller.signal);
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

  it("fetches a protected execution artifact by Review, Snapshot and evidence without a machine token", async () => {
    const spy = stubFetch(200, { role: "log", expired: false, text: "redacted", truncated: false, bytes: 8 });
    const output = await fetchReviewExecutionArtifact("r/review 1", "rev 2", "evidence/3", "log");
    expect(output.text).toBe("redacted");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(
      "/api/reviews/r%2Freview%201/execution-artifacts?revision_id=rev+2&evidence_id=evidence%2F3&role=log",
    );
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
  });

  it("lists revision-bound review evidence through the authenticated review route", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { revision_id: "rrv-1", evidence: [] });
    await fetchReviewEvidence("rev-1", "rrv-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/evidence?revision_id=rrv-1");
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
  });

  it("uses explicit compare-and-swap values when arming provider landing", async () => {
    const spy = stubFetch(200, { snapshot_id: "rrv-9", conditions: [], landing: {} });
    await fetchReviewReadiness("rev-1");
    await armReviewLanding("rev-1", {
      expected_revision_id: "rrv-9",
      expected_tree_fingerprint: "tree-9",
      expected_conditions_hash: "conditions-9",
    });
    await cancelReviewLanding("rev-1", "rrv-9");
    expect((spy.mock.calls[0] as unknown as [string, RequestInit])[0]).toBe("/api/reviews/rev-1/readiness");
    const [armUrl, armInit] = spy.mock.calls[1] as unknown as [string, RequestInit];
    expect(armUrl).toBe("/api/reviews/rev-1/landing/arm");
    expect(JSON.parse(String(armInit.body))).toEqual({
      armed: true, expected_revision_id: "rrv-9", expected_tree_fingerprint: "tree-9", expected_conditions_hash: "conditions-9",
    });
    const [cancelUrl, cancelInit] = spy.mock.calls[2] as unknown as [string, RequestInit];
    expect(cancelUrl).toBe("/api/reviews/rev-1/landing/cancel");
    expect(JSON.parse(String(cancelInit.body))).toEqual({ expected_revision_id: "rrv-9" });
  });

  it("binds verification and dependency projections to an explicit Snapshot", async () => {
    const spy = stubFetch(200, { snapshot_id: "rrv-9", checks: [], changes: [] });
    await fetchReviewVerification("rev-1", "rrv-9");
    await fetchReviewDependencies("rev-1", "rrv-9");
    expect((spy.mock.calls[0] as unknown as [string, RequestInit])[0]).toBe(
      "/api/reviews/rev-1/verification?revision_id=rrv-9",
    );
    expect((spy.mock.calls[1] as unknown as [string, RequestInit])[0]).toBe(
      "/api/reviews/rev-1/dependencies?revision_id=rrv-9",
    );
  });

  it("binds surface discovery and execution to an explicit Snapshot", async () => {
    const spy = stubFetch(200, { revision_id: "rrv-4", surfaces: [], result: { revision_id: "rrv-4" } });
    await fetchReviewSurfaces("rev-1", "rrv-4");
    await runReviewSurface("rev-1", "bruno", "api/get", "old", "rrv-4");
    const [surfaceUrl] = spy.mock.calls[0] as unknown as [string, RequestInit];
    const [runUrl, runInit] = spy.mock.calls[1] as unknown as [string, RequestInit];
    expect(surfaceUrl).toBe("/api/reviews/rev-1/surfaces?revision_id=rrv-4");
    expect(runUrl).toBe("/api/reviews/rev-1/surface-run?provider=bruno&surface_id=api%2Fget&side=old&revision_id=rrv-4");
    expect(runInit.method).toBe("POST");
  });

  it("binds runtime validation run, status, and cancellation to the exact Snapshot and scenario", async () => {
    const spy = stubFetch(200, {
      request: { revision_id: "rrv-4" },
      runtime_validation: { status: "NOT_RUN" },
      verification: { checks: [], summary: {} },
      reused: false,
    });
    await runRuntimeValidation("rev-1", "service", "checks:tests", "rrv-4");
    await fetchRuntimeValidationJob("rev-1", "job-1", "service", "checks:tests", "rrv-4");
    await cancelRuntimeValidationJob("rev-1", "job-1", "service", "checks:tests", "rrv-4");

    const [runUrl, runInit] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(runUrl).toBe("/api/reviews/rev-1/runtime-validation");
    expect(runInit.method).toBe("POST");
    expect(JSON.parse(String(runInit.body))).toEqual({
      revision_id: "rrv-4",
      provider: "service",
      surface_id: "checks:tests",
      side: "new",
    });

    const [statusUrl] = spy.mock.calls[1] as unknown as [string, RequestInit];
    expect(statusUrl).toBe(
      "/api/reviews/rev-1/runtime-validation/jobs/job-1?revision_id=rrv-4&provider=service&surface_id=checks%3Atests&side=new",
    );

    const [cancelUrl, cancelInit] = spy.mock.calls[2] as unknown as [string, RequestInit];
    expect(cancelUrl).toBe("/api/reviews/rev-1/runtime-validation/jobs/job-1/cancel");
    expect(cancelInit.method).toBe("POST");
    expect(JSON.parse(String(cancelInit.body))).toEqual({
      revision_id: "rrv-4",
      provider: "service",
      surface_id: "checks:tests",
      side: "new",
    });
  });
  it("fetches plugin preview artifacts pinned to one Snapshot side and surfaces plugin reasons", async () => {
    const spy = vi.fn(async () =>
      new Response("<p>new</p>", {
        status: 200,
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          "Content-Disposition": 'inline; filename="prices-new.html"',
          "X-LemonCrow-Revision-Id": "rrv-4",
        },
      }),
    );
    vi.stubGlobal("fetch", spy);
    const artifact = await fetchSurfacePreview("rev-1", "csv-table", "data/prices.csv", "new", "rrv-4");
    expect((spy.mock.calls[0] as unknown as [string])[0]).toBe(
      "/api/reviews/rev-1/surface-preview?provider=csv-table&surface_id=data%2Fprices.csv&side=new&revision_id=rrv-4",
    );
    expect(artifact.mediaType).toBe("text/html");
    expect(artifact.filename).toBe("prices-new.html");
    expect(artifact.revisionId).toBe("rrv-4");
    expect(artifact.blob.size).toBe("<p>new</p>".length);

    stubFetch(409, { detail: "CSV table is unavailable on the old side: data/prices.csv does not exist on the old side" });
    await expect(fetchSurfacePreview("rev-1", "csv-table", "data/prices.csv", "old", "rrv-4")).rejects.toThrow(
      "CSV table is unavailable on the old side: data/prices.csv does not exist on the old side",
    );
  });

  it("classifies a Playwright trace upload without wrapping it in multipart", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(201, { evidence: { id: "evd-1" } });
    const file = new File(["trace"], "checkout-trace.zip", { type: "application/zip" });
    await uploadReviewEvidence("rev-1", file, "src/checkout.tsx", "rrv-1");
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
    expect(reviewCompareHref("r/aaaaaaaa", 2, 5)).toBe("/r/aaaaaaaa?base=2&target=5");
    expect(reviewCompareHref("r/aaaaaaaa", "head", 5)).toBe("/r/aaaaaaaa?base=head&target=5");
    expect(sourceCompareHref("git/feature/foo", "worktree")).toBe(
      "/r/x?from=git%2Ffeature%2Ffoo&to=worktree",
    );
    expect(sourceCompareHref("rr/11111111", "rr/22222222")).toBe(
      "/r/x?from=rr%2F11111111&to=rr%2F22222222",
    );
    expect(sourceCompareHref("git/main..feature", "git/HEAD")).toBe(
      "/r/x?from=git%2Fmain..feature&to=git%2FHEAD",
    );
    expect(sourceCompareHref("rr/11111111", "rr/22222222", "r/aaaaaaaa")).toBe(
      "/r/aaaaaaaa/compare/11111111..22222222",
    );
    expect(sourceCompareHref("git/main", "git/HEAD", "r/aaaaaaaa")).toBe(
      "/r/x?from=git%2Fmain&to=git%2FHEAD&scope=r%2Faaaaaaaa",
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
    await finishReview("rev-1", "finished", "rrv-1");
    await finishReview("rev-1", "open");
    const calls = spy.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map((call) => JSON.parse(String(call[1].body)))).toEqual([
      { status: "finished", expected_revision_id: "rrv-1" },
      { status: "open", expected_revision_id: "" },
    ]);
    expect(calls[0][0]).toBe("/api/reviews/rev-1/finish");
  });

  it("posts a mark as JSON", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { recorded: "reviewed" });
    await postMark("rev-1", "fil:abc", "reviewed", "rrv-1");
    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      unit_key: "fil:abc",
      state: "reviewed",
      expected_revision_id: "rrv-1",
    });
  });

  it("posts chapter close-out keys without a disposition override", async () => {
    __setBootstrapForTest({ token: "t", reviewId: "rev-1" });
    const spy = stubFetch(200, { marked: [], skipped: [], group_counts: {} });
    await postBulkReviewed("rev-1", ["fil:a", "fil:b"], "rrv-1");
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/reviews/rev-1/marks/bulk");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      unit_keys: ["fil:a", "fil:b"],
      expected_revision_id: "rrv-1",
    });
  });
});
