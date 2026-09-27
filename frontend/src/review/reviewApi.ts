/**
 * The Review fetch layer. The machine credential never enters JavaScript.
 *
 * Local Review uses a server-minted, Review-only browser capability claimed
 * by the same-origin local UI. The derived capability
 * is stored in this exact browser origin (scheme + host + port). Hosted Review
 * keeps Authward-issued access/refresh credentials only in LemonCrow's HttpOnly
 * BFF cookies; JavaScript never receives either credential. Legacy `#t=`
 * machine-bearer links are scrubbed immediately and never retained or replayed.
 */

import type {
  AnnotationDraft,
  AnnotationKind,
  AnnotationList,
  AnnotationResult,
  AnnotationState,
  BulkMarkResult,
  FeedbackDelivery,
  FeedbackExport,
  FileDetail,
  GitHubPublishPreview,
  GitHubPublishResult,
  MarkResult,
  MarkState,
  RelatedSource,
  ReviewAskConversation,
  ReviewAskHandoffResult,
  ReviewAskInput,
  ReviewAskResult,
  ReviewActivityEvent,
  ReviewChangeProposalApplyResult,
  ReviewChangeProposalList,
  ReviewChangeProposalResult,
  ReviewClosure,
  ReviewEvidenceList,
  ReviewEvidenceResult,
  ReviewDependencyProjection,
  ReviewLandingReadiness,
  ReviewVerificationProjection,
  ReviewVerificationWaiverResult,
  ReviewMarkEvent,
  ReviewJournal,
  ReviewDevelopment,
  DevelopmentTimelinePage,
  DevelopmentTimelineScope,
  DevelopmentTimelineView,
  ReviewMapLevel,
  ReviewMapPayload,
  ReviewOutcomeKind,
  ReviewOutcomeProjection,
  ReviewOverview,
  ReviewProposalSelection,
  ReviewRevisionComparison,
  ReviewRevisionHistory,
  ReviewSessionInfo,
  RevisionInfo,
  ReviewSurfaceList,
  ReviewSurfaceRunResponse,
  ReviewTargetList,
  AnnotationVersion,
  SourceComparison,
  SourceState,
  ReviewDirectoryPage,
  ReviewDirectoryStatus,
} from "./types";
let reviewId = "";
let browserSession = "";
let localBrowserClaim: Promise<Response> | null = null;

const BROWSER_SESSION_KEY = "lemoncrow.review.browserSession";
const BROWSER_SESSION_HEADER = "X-LemonCrow-Browser-Session";
const REVIEW_KEY = "lemoncrow.review.id";
const PREPARATION_KEY = "lemoncrow.review.preparation";

export interface Bootstrap {
  /** Deprecated compatibility field. The machine bearer is never browser-visible; always empty. */
  token: string;
  reviewId: string;
  preparationId?: string;
  sourceReady?: boolean;
}

export interface ReviewLiveState {
  cursor: string;
  changed: boolean;
  revision_id: string;
  packet_cursor: string;
  annotations_cursor: string;
  journal_cursor: string;
  /** Last appended Development sequence; absent on older servers. */
  development_cursor?: string;
}

export interface ReviewPreparationStatus {
  state: "diff" | "source" | "impact" | "provenance" | "ranking" | "persisting" | "ready" | "failed" | string;
  detail: string;
}

/**
 * Adopt non-secret Review bootstrap state from the URL fragment.
 *
 * Older local builds emitted `#t=<bearer>`. That value is never used now; if
 * encountered it is removed from the address bar before the page does any
 * work. Review/preparation identifiers remain tab-scoped for reloads.
 */
export function adoptBootstrapFragment(): Bootstrap {
  let preparationId = "";
  let sourceReady = false;
  if (typeof window === "undefined") return { token: "", reviewId };

  const raw = window.location.hash.replace(/^#/, "");
  if (raw) {
    const params = new URLSearchParams(raw);
    const nextReview = params.get("r") ?? "";
    const nextPreparation = params.get("p") ?? "";
    const nextSourceReady = params.get("sr") === "1";
    if (nextReview) {
      reviewId = nextReview;
      window.sessionStorage.setItem(REVIEW_KEY, nextReview);
    }
    if (nextPreparation) {
      preparationId = nextPreparation;
      window.sessionStorage.setItem(PREPARATION_KEY, nextPreparation);
      sourceReady = nextSourceReady;
    }
    if (params.has("t")) {
      params.delete("t");
      const safeHash = params.toString();
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname}${window.location.search}${safeHash ? `#${safeHash}` : ""}`,
      );
    }
  } else {
    reviewId ||= window.sessionStorage.getItem(REVIEW_KEY) ?? "";
    preparationId = window.sessionStorage.getItem(PREPARATION_KEY) ?? "";
  }
  const bootstrap = preparationId ? { token: "", reviewId, preparationId } : { token: "", reviewId };
  return sourceReady ? { ...bootstrap, sourceReady: true } : bootstrap;
}

export function currentReviewId(): string {
  return reviewId;
}

export function selectReviewId(id: string): void {
  reviewId = id;
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(REVIEW_KEY, id);
}

function stripRefNamespace(value: string, prefix: "r/" | "rr/"): string {
  return value.startsWith(prefix) ? value.slice(prefix.length) : value;
}

function encodeCompareRef(value: string): string {
  return value.split("/").map((part) => encodeURIComponent(part)).join("/");
}

export function reviewHref(idOrRef: string): string {
  const id = stripRefNamespace(idOrRef, "r/");
  return `/r/${encodeURIComponent(id)}`;
}

export function historicalReviewHref(reviewIdOrRef: string, revisionNumber?: number): string {
  if (revisionNumber !== undefined) {
    return reviewCompareHref(reviewIdOrRef, revisionNumber, revisionNumber);
  }
  const id = stripRefNamespace(reviewIdOrRef, "rr/");
  return `/rr/${encodeURIComponent(id)}`;
}

export function reviewCompareHref(
  reviewIdOrRef: string,
  baseRevision: number | "head",
  targetRevision: number,
): string {
  const params = new URLSearchParams({
    base: String(baseRevision),
    target: String(targetRevision),
  });
  return `${reviewHref(reviewIdOrRef)}?${params.toString()}`;
}

export function sourceCompareHref(fromRef: string, toRef: string, scope = ""): string {
  if (scope.startsWith("r/") && fromRef.startsWith("rr/") && toRef.startsWith("rr/")) {
    const reviewId = stripRefNamespace(scope, "r/");
    const fromId = stripRefNamespace(fromRef, "rr/");
    const toId = stripRefNamespace(toRef, "rr/");
    return `/r/${encodeURIComponent(reviewId)}/compare/${encodeURIComponent(fromId)}..${encodeURIComponent(toId)}`;
  }
  const path = `/r/x/${encodeCompareRef(fromRef)}..${encodeCompareRef(toRef)}`;
  return scope ? `${path}?scope=${encodeURIComponent(scope)}` : path;
}


export function reviewDirectoryHref(): string {
  return "/reviews";
}

export function hasToken(): boolean {
  return false;
}

/** Test seam: reset module state between browser tests; token input stays ignored. */
export function __setBootstrapForTest(next: Bootstrap): void {
  reviewId = next.reviewId;
  browserSession = "";
  localBrowserClaim = null;
}

function isLiteralLoopbackOrigin(): boolean {
  if (typeof window === "undefined") return false;
  const host = window.location.hostname.toLowerCase();
  return host === "127.0.0.1" || host === "localhost" || host === "::1";
}

function currentBrowserSession(): string {
  if (browserSession || typeof window === "undefined") return browserSession;
  try {
    browserSession = window.localStorage.getItem(BROWSER_SESSION_KEY) ?? "";
  } catch {
    browserSession = "";
  }
  return browserSession;
}

function rememberBrowserSession(value: string): void {
  browserSession = value;
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(BROWSER_SESSION_KEY, value);
  } catch {
    // Memory-only still keeps the current tab usable when storage is disabled.
  }
}

function forgetBrowserSession(): void {
  browserSession = "";
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(BROWSER_SESSION_KEY);
  } catch {
    // Nothing else to clear.
  }
}

function claimLocalBrowserSession(path: string): Promise<Response> {
  if (!localBrowserClaim) {
    localBrowserClaim = (async () => {
      const response = await fetch("/v1/auth/local-browser/claim", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
        credentials: "same-origin",
      });
      if (!response.ok) return response;
      try {
        const payload = (await response.clone().json()) as { token?: unknown };
        if (typeof payload.token === "string" && payload.token) {
          rememberBrowserSession(payload.token);
          return response;
        }
      } catch {
        // Replace malformed success responses with the useful authentication error below.
      }
      return new Response(JSON.stringify({ detail: "local Review browser pairing returned no capability" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    })().finally(() => {
      localBrowserClaim = null;
    });
  }
  return localBrowserClaim;
}

let activeAuthenticatedRequests = 0;

function reportAuthenticatedRequest(delta: 1 | -1): void {
  if (typeof window === "undefined") return;
  activeAuthenticatedRequests = Math.max(0, activeAuthenticatedRequests + delta);
  window.dispatchEvent(
    new CustomEvent("lemoncrow:request-activity", {
      detail: { active: activeAuthenticatedRequests },
    }),
  );
}

export async function authenticatedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  reportAuthenticatedRequest(1);
  try {
    const local = isLiteralLoopbackOrigin();
    const localReviewRoute = typeof window !== "undefined"
      && (/^\/r\//.test(window.location.pathname) || /^\/rr\//.test(window.location.pathname));
    let initialLocalClaim: Response | null = null;

    // Claim a local Review capability before any Reader loader fires so startup
    // does not spray predictable 401s and then retry them. Concurrent loaders
    // share claimLocalBrowserSession's single in-flight promise.
    if (local && localReviewRoute && !currentBrowserSession()) {
      initialLocalClaim = await claimLocalBrowserSession(window.location.pathname);
      if (!initialLocalClaim.ok && initialLocalClaim.status !== 404) {
        return initialLocalClaim.clone();
      }
    }

    const send = () => {
      const capability = local ? currentBrowserSession() : "";
      const hasInit = Object.keys(init).length > 0;
      if (!capability && !hasInit) {
        // Fetch already defaults credentials to same-origin. Preserve the simple
        // GET call shape for existing callers/tests until a browser capability
        // actually needs to be attached.
        return fetch(path);
      }
      const headers = new Headers(init.headers);
      if (capability) headers.set(BROWSER_SESSION_HEADER, capability);
      return fetch(path, { ...init, headers, credentials: "same-origin" });
    };

    let response = await send();
    if (response.status !== 401 || typeof window === "undefined") return response;

    if (local) {
      // A stale capability is never kept alive by accident. The local UI
      // claims a fresh one on the current same-origin page.
      forgetBrowserSession();
      const claimed = initialLocalClaim ?? await claimLocalBrowserSession(window.location.pathname);
      if (claimed.ok) return send();
      // A hosted test/dev origin may itself be loopback. Absence of the local
      // pairing endpoint means this is not the local topology; continue with the
      // hosted Authward BFF flow.
      if (claimed.status !== 404) return claimed.clone();
    }

    // Hosted mode refreshes the HttpOnly Authward credential at Authward; it does
    // not mint a replacement LemonCrow user session token.
    const refreshed = await fetch("/v1/auth/browser/refresh", {
      method: "POST",
      credentials: "same-origin",
    });
    if (refreshed.ok) return send();

    const returnTo = `${window.location.pathname}${window.location.search}`;
    window.location.assign(
      `/v1/auth/browser/start?return_to=${encodeURIComponent(returnTo)}`,
    );
    throw new Error("authentication required");
  } finally {
    reportAuthenticatedRequest(-1);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  // Adopt non-secret review/preparation state before the first request.
  if (typeof window !== "undefined") adoptBootstrapFragment();
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await authenticatedFetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown; error?: { message?: unknown } };
      if (typeof body.detail === "string") detail = body.detail;
      else if (typeof body.error?.message === "string") detail = body.error.message;
    } catch {
      // A non-JSON error body is still an error; do not invent a reason for it.
    }
    throw Object.assign(new Error(detail), { status: response.status });
  }
  return (await response.json()) as T;
}

/** Shared browser-authenticated JSON request for the local control-center surfaces. */
export function requestBrowserJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  return request<T>(path, init);
}

export interface ReviewDirectoryQuery {
  status: ReviewDirectoryStatus;
  query: string;
  cursor: string;
  limit: number;
}

export function fetchReviewDirectory(query: ReviewDirectoryQuery): Promise<ReviewDirectoryPage> {
  const params = new URLSearchParams({
    status: query.status,
    q: query.query,
    limit: String(query.limit),
  });
  if (query.cursor) params.set("cursor", query.cursor);
  return request<ReviewDirectoryPage>(`/v1/reviews?${params.toString()}`);
}

export function fetchReviewPreparation(id: string, preparationId: string): Promise<ReviewPreparationStatus> {
  const params = new URLSearchParams({ preparation_id: preparationId });
  return request<ReviewPreparationStatus>(
    `/api/reviews/${encodeURIComponent(id)}/preparation?${params.toString()}`,
  );
}

export function fetchOverview(id: string): Promise<ReviewOverview> {
  return request<ReviewOverview>(`/api/reviews/${encodeURIComponent(id)}`);
}

export function fetchReviewJournal(id: string): Promise<ReviewJournal> {
  return request<ReviewJournal>(`/api/reviews/${encodeURIComponent(id)}/journal`);
}

export function fetchHistoricalReviewJournal(id: string, revisionNumber: number): Promise<ReviewJournal> {
  return request<ReviewJournal>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/journal`,
  );
}

export function fetchReviewDevelopment(id: string): Promise<ReviewDevelopment> {
  return request<ReviewDevelopment>(`/api/reviews/${encodeURIComponent(id)}/development`);
}

export function fetchHistoricalReviewDevelopment(id: string, revisionNumber: number): Promise<ReviewDevelopment> {
  return request<ReviewDevelopment>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/development`,
  );
}

export function fetchReviewDevelopmentEvents(
  id: string,
  options: {
    revisionNumber?: number;
    scope?: DevelopmentTimelineScope;
    view?: DevelopmentTimelineView;
    before?: number | null;
    limit?: number;
    path?: string;
  } = {},
): Promise<DevelopmentTimelinePage> {
  const params = new URLSearchParams({
    scope: options.scope ?? "snapshot",
    view: options.view ?? "all",
    limit: String(options.limit ?? 50),
  });
  if (options.revisionNumber !== undefined) params.set("revision", String(options.revisionNumber));
  if (options.before !== undefined && options.before !== null) params.set("before", String(options.before));
  if (options.path) params.set("path", options.path);
  return request<DevelopmentTimelinePage>(
    `/api/reviews/${encodeURIComponent(id)}/development/events?${params.toString()}`,
  );
}

export function fetchHistoricalOverview(id: string, revisionNumber: number): Promise<ReviewOverview> {
  return request<ReviewOverview>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}`,
  );
}

export function fetchReviewMap(
  id: string,
  level: ReviewMapLevel = "architecture",
  nodeId = "",
): Promise<ReviewMapPayload> {
  const params = new URLSearchParams({ level });
  if (nodeId) params.set("node_id", nodeId);
  return request<ReviewMapPayload>(
    `/api/reviews/${encodeURIComponent(id)}/map?${params.toString()}`,
  );
}

export function fetchHistoricalReviewMap(
  id: string,
  revisionNumber: number,
  level: ReviewMapLevel = "architecture",
  nodeId = "",
): Promise<ReviewMapPayload> {
  const params = new URLSearchParams({ level });
  if (nodeId) params.set("node_id", nodeId);
  return request<ReviewMapPayload>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/map?${params.toString()}`,
  );
}

export function fetchTargets(id: string, order: "recommended" | "file" = "recommended"): Promise<ReviewTargetList> {
  return request<ReviewTargetList>(
    `/api/reviews/${encodeURIComponent(id)}/targets?order=${encodeURIComponent(order)}`,
  );
}

export function fetchHistoricalTargets(
  id: string,
  revisionNumber: number,
  order: "recommended" | "file" = "recommended",
): Promise<ReviewTargetList> {
  return request<ReviewTargetList>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/targets?order=${encodeURIComponent(order)}`,
  );
}

export function fetchReviewSurfaces(id: string, revisionId = ""): Promise<ReviewSurfaceList> {
  const params = new URLSearchParams();
  if (revisionId) params.set("revision_id", revisionId);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return request<ReviewSurfaceList>(`/api/reviews/${encodeURIComponent(id)}/surfaces${query}`);
}

export interface SurfaceImpactScore {
  revision_id: string;
  provider: string;
  surface_id: string;
  source: string;
  comparator: string;
  changed: boolean;
  magnitude: number;
  label: string;
  reasons: string[];
  metrics: Record<string, unknown>;
}

export function fetchSurfaceImpact(
  id: string,
  provider: string,
  surfaceId: string,
  revisionId = "",
): Promise<SurfaceImpactScore> {
  const params = new URLSearchParams({ provider, surface_id: surfaceId });
  if (revisionId) params.set("revision_id", revisionId);
  return request<SurfaceImpactScore>(
    `/api/reviews/${encodeURIComponent(id)}/surface-impact?${params.toString()}`,
  );
}

function impactErrorStatus(error: unknown): number | null {
  const status = Number((error as { status?: unknown } | null)?.status);
  return Number.isFinite(status) ? status : null;
}

export function surfaceImpactRetryable(error: unknown): boolean {
  const status = impactErrorStatus(error);
  return status === null || status === 409 || status === 429 || status >= 500;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

/**
 * Surface impact work can involve a browser/build/runtime and transient startup
 * failures are expected under load. Retry only transient classes; contract and
 * safety refusals (4xx except 409/429) are permanent for this revision.
 */
export async function fetchSurfaceImpactWithRetry(
  id: string,
  provider: string,
  surfaceId: string,
  revisionId = "",
  attempts = 3,
): Promise<SurfaceImpactScore> {
  let lastError: unknown = new Error("surface impact failed");
  const boundedAttempts = Math.max(1, Math.min(attempts, 5));
  for (let attempt = 0; attempt < boundedAttempts; attempt += 1) {
    try {
      return await fetchSurfaceImpact(id, provider, surfaceId, revisionId);
    } catch (error) {
      lastError = error;
      if (!surfaceImpactRetryable(error) || attempt + 1 >= boundedAttempts) throw error;
      await delay(attempt === 0 ? 150 : attempt === 1 ? 600 : 1500);
    }
  }
  throw lastError;
}

export interface SurfaceResponseDiffScore {
  revision_id: string;
  provider: string;
  surface_id: string;
  old_status_code: number;
  new_status_code: number;
  status_changed: boolean;
  body_changed_ratio: number;
  header_changed_ratio: number;
}

export function fetchSurfaceResponseDiffScore(
  id: string,
  provider: string,
  surfaceId: string,
  revisionId = "",
): Promise<SurfaceResponseDiffScore> {
  const params = new URLSearchParams({ provider, surface_id: surfaceId });
  if (revisionId) params.set("revision_id", revisionId);
  return request<SurfaceResponseDiffScore>(
    `/api/reviews/${encodeURIComponent(id)}/surface-response-diff?${params.toString()}`,
  );
}

export function runReviewSurface(
  id: string,
  provider: string,
  surfaceId: string,
  side: "old" | "new",
  revisionId = "",
): Promise<ReviewSurfaceRunResponse> {
  const params = new URLSearchParams({ provider, surface_id: surfaceId, side });
  if (revisionId) params.set("revision_id", revisionId);
  return request<ReviewSurfaceRunResponse>(
    `/api/reviews/${encodeURIComponent(id)}/surface-run?${params.toString()}`,
    { method: "POST" },
  );
}
export interface SurfacePreviewArtifact {
  blob: Blob;
  mediaType: string;
  filename: string;
  revisionId: string;
}

/** Fetch one plugin-rendered artifact pinned to one side of one exact Snapshot. */
export async function fetchSurfacePreview(
  id: string,
  provider: string,
  surfaceId: string,
  side: "old" | "new",
  revisionId = "",
): Promise<SurfacePreviewArtifact> {
  const params = new URLSearchParams({ provider, surface_id: surfaceId, side });
  if (revisionId) params.set("revision_id", revisionId);
  const response = await authenticatedFetch(
    `/api/reviews/${encodeURIComponent(id)}/surface-preview?${params.toString()}`,
  );
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Keep the HTTP status when a preview failure is not JSON.
    }
    throw new Error(detail);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `${surfaceId}-${side}`;
  return {
    blob: await response.blob(),
    mediaType: (response.headers.get("Content-Type") ?? "").split(";")[0]?.trim() ?? "",
    filename,
    revisionId: response.headers.get("X-LemonCrow-Revision-Id") ?? revisionId,
  };
}

export function fetchFile(id: string, path: string): Promise<FileDetail> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  return request<FileDetail>(`/api/reviews/${encodeURIComponent(id)}/files/${segments}/patch?include_web_preview=false`);
}

export function fetchHistoricalFile(id: string, revisionNumber: number, path: string): Promise<FileDetail> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  return request<FileDetail>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/files/${segments}/patch?include_web_preview=false`,
  );
}

export function fetchReviewPatch(id: string, revisionNumber?: number): Promise<Blob> {
  const base = `/api/reviews/${encodeURIComponent(id)}`;
  return fetchReviewBlob(
    revisionNumber !== undefined
      ? `${base}/revisions/${revisionNumber}/patch`
      : `${base}/patch`,
  );
}

async function fetchReviewBlob(url: string): Promise<Blob> {
  const response = await authenticatedFetch(url);
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Keep the HTTP status for non-JSON binary failures.
    }
    throw new Error(detail);
  }
  return response.blob();
}

export async function fetchMarkdownImage(
  id: string,
  documentPath: string,
  side: "old" | "new",
  src: string,
  revisionId = "",
): Promise<Blob> {
  const params = new URLSearchParams({ document_path: documentPath, side, src });
  if (revisionId) params.set("revision_id", revisionId);
  return fetchReviewBlob(`/api/reviews/${encodeURIComponent(id)}/markdown-image?${params.toString()}`);
}

export async function fetchMediaFile(
  id: string,
  path: string,
  side: "old" | "new",
  revisionId = "",
): Promise<Blob> {
  const params = new URLSearchParams({ path, side });
  if (revisionId) params.set("revision_id", revisionId);
  return fetchReviewBlob(`/api/reviews/${encodeURIComponent(id)}/media-file?${params.toString()}`);
}

export async function fetchLiveWebPreview(
  id: string,
  documentPath: string,
  side: "old" | "new",
  route: string,
  revisionId = "",
): Promise<{ url: string }> {
  const params = new URLSearchParams({ document_path: documentPath, side, route });
  if (revisionId) params.set("revision_id", revisionId);
  return request<{ url: string }>(
    `/api/reviews/${encodeURIComponent(id)}/web-live-preview?${params.toString()}`,
  );
}

export interface WebPreviewDiffScore {
  revision_id: string;
  route: string;
  changed_ratio: number;
  mean_delta: number;
  width: number;
  height: number;
}

export async function fetchWebPreview(
  id: string,
  documentPath: string,
  side: "old" | "new",
  route: string,
  width = 1440,
  height = 5000,
  revisionId = "",
): Promise<Blob> {
  const params = new URLSearchParams({
    document_path: documentPath,
    side,
    route,
    width: String(width),
    height: String(height),
  });
  if (revisionId) params.set("revision_id", revisionId);
  const response = await authenticatedFetch(
    `/api/reviews/${encodeURIComponent(id)}/web-preview?${params.toString()}`,
  );
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Keep the HTTP status when a preview failure is not JSON.
    }
    throw new Error(detail);
  }
  return response.blob();
}

export function fetchWebPreviewDiffScore(
  id: string,
  documentPath: string,
  route: string,
  revisionId = "",
  width = 1440,
  height = 5000,
): Promise<WebPreviewDiffScore> {
  const params = new URLSearchParams({
    document_path: documentPath,
    route,
    width: String(width),
    height: String(height),
  });
  if (revisionId) params.set("revision_id", revisionId);
  return request<WebPreviewDiffScore>(
    `/api/reviews/${encodeURIComponent(id)}/web-preview-diff?${params.toString()}`,
  );
}

export function fetchRelatedSource(id: string, path: string, line: number): Promise<RelatedSource> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  return request<RelatedSource>(
    `/api/reviews/${encodeURIComponent(id)}/related/${segments}?line=${encodeURIComponent(String(line || 1))}`,
  );
}

export function fetchHistoricalRelatedSource(
  id: string,
  revisionNumber: number,
  path: string,
  line: number,
): Promise<RelatedSource> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  return request<RelatedSource>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/related/${segments}?line=${encodeURIComponent(String(line || 1))}`,
  );
}

export function fetchReviewAsk(id: string): Promise<ReviewAskConversation> {
  return request<ReviewAskConversation>(`/api/reviews/${encodeURIComponent(id)}/ask`);
}

export function postReviewAsk(id: string, input: ReviewAskInput): Promise<ReviewAskResult> {
  return request<ReviewAskResult>(`/api/reviews/${encodeURIComponent(id)}/ask`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export type ReviewAskStreamEvent =
  | { type: "accepted"; driver: string; model: string }
  | { type: "working"; elapsed_ms: number; driver: string; model: string }
  | { type: "result"; result: ReviewAskResult }
  | { type: "error"; status: number; detail: string };

export async function streamReviewAsk(
  id: string,
  input: ReviewAskInput,
  onEvent: (event: ReviewAskStreamEvent) => void,
): Promise<ReviewAskResult> {
  if (typeof window !== "undefined") adoptBootstrapFragment();
  const response = await authenticatedFetch(`/api/reviews/${encodeURIComponent(id)}/ask/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // Keep the status fallback for a non-JSON response.
    }
    throw Object.assign(new Error(detail), { status: response.status });
  }
  if (!response.body) return postReviewAsk(id, input);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: ReviewAskResult | null = null;
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const raw of lines) {
      const line = raw.trim();
      if (!line) continue;
      const event = JSON.parse(line) as ReviewAskStreamEvent;
      onEvent(event);
      if (event.type === "error") {
        throw Object.assign(new Error(event.detail), { status: event.status });
      }
      if (event.type === "result") finalResult = event.result;
    }
    if (done) break;
  }
  if (buffer.trim()) {
    const event = JSON.parse(buffer) as ReviewAskStreamEvent;
    onEvent(event);
    if (event.type === "error") throw Object.assign(new Error(event.detail), { status: event.status });
    if (event.type === "result") finalResult = event.result;
  }
  if (!finalResult) throw new Error("Review Ask stream ended before a response was ready");
  return finalResult;
}

export function sendReviewAskHandoff(
  id: string,
  messageId: string,
  input: {
    operation_id: string;
    instruction_hash: string;
    expected_revision_id: string;
    expected_tree_fingerprint: string;
    expected_source_fingerprint: string;
  },
): Promise<ReviewAskHandoffResult> {
  return request<ReviewAskHandoffResult>(
    `/api/reviews/${encodeURIComponent(id)}/ask/${encodeURIComponent(messageId)}/handoff`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function postMark(id: string, unitKey: string, state: MarkState, expectedRevisionId: string): Promise<MarkResult> {
  return request<MarkResult>(`/api/reviews/${encodeURIComponent(id)}/marks`, {
    method: "POST",
    body: JSON.stringify({ unit_key: unitKey, state, expected_revision_id: expectedRevisionId }),
  });
}

export function postBulkReviewed(id: string, unitKeys: string[], expectedRevisionId: string): Promise<BulkMarkResult> {
  return request<BulkMarkResult>(`/api/reviews/${encodeURIComponent(id)}/marks/bulk`, {
    method: "POST",
    body: JSON.stringify({ unit_keys: unitKeys, expected_revision_id: expectedRevisionId }),
  });
}

export function fetchSourceState(id: string): Promise<SourceState> {
  return request<SourceState>(`/api/reviews/${encodeURIComponent(id)}/source-state`);
}

export function fetchReviewLiveState(
  id: string,
  cursor: string,
  waitMs: number,
  signal?: AbortSignal,
): Promise<ReviewLiveState> {
  const params = new URLSearchParams({ cursor, wait_ms: String(Math.max(0, Math.min(waitMs, 15_000))) });
  return request<ReviewLiveState>(
    `/api/reviews/${encodeURIComponent(id)}/live-state?${params.toString()}`,
    signal ? { signal } : {},
  );
}

export function fetchProposalSelection(
  id: string,
  path: string,
  startLine: number,
  endLine: number,
  side: "additions" | "deletions",
): Promise<ReviewProposalSelection> {
  const params = new URLSearchParams({
    path,
    start_line: String(startLine),
    end_line: String(endLine),
    side,
  });
  return request<ReviewProposalSelection>(`/api/reviews/${encodeURIComponent(id)}/proposal-selection?${params.toString()}`);
}

export function fetchChangeProposals(id: string): Promise<ReviewChangeProposalList> {
  return request<ReviewChangeProposalList>(`/api/reviews/${encodeURIComponent(id)}/proposals`);
}

export function createChangeProposal(
  id: string,
  input: {
    expected_revision_id: string;
    path: string;
    start_line: number;
    end_line: number;
    side: "additions";
    replacement_text: string;
    target_unit_key?: string;
    intent?: string;
    annotation_id?: string;
  },
): Promise<ReviewChangeProposalResult> {
  return request<ReviewChangeProposalResult>(`/api/reviews/${encodeURIComponent(id)}/proposals`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function applyChangeProposal(proposalId: string): Promise<ReviewChangeProposalApplyResult> {
  return request<ReviewChangeProposalApplyResult>(`/api/proposals/${encodeURIComponent(proposalId)}/apply`, {
    method: "POST",
  });
}

export function postRefresh(id: string): Promise<ReviewOverview> {
  return request<ReviewOverview>(`/api/reviews/${encodeURIComponent(id)}/refresh`, { method: "POST" });
}

export function listReviews(
  statusFilter: "open" | "finished" | "archived" | "all" = "open",
): Promise<{ reviews: ReviewSessionInfo[]; repo_root: string; status_filter: string }> {
  return request(`/api/reviews?status_filter=${encodeURIComponent(statusFilter)}`);
}

export function fetchRevisions(id: string): Promise<ReviewRevisionHistory> {
  return request(`/api/reviews/${encodeURIComponent(id)}/revisions`);
}

export function fetchRevisionComparison(
  id: string,
  fromRevision?: number,
  toRevision?: number,
): Promise<ReviewRevisionComparison> {
  const params = new URLSearchParams();
  if (fromRevision !== undefined) params.set("from_revision", String(fromRevision));
  if (toRevision !== undefined) params.set("to_revision", String(toRevision));
  const query = params.toString();
  return request(
    `/api/reviews/${encodeURIComponent(id)}/compare${query ? `?${query}` : ""}`,
  );
}

export function fetchRevisionLocator(reference: string): Promise<{ review: ReviewSessionInfo; revision: RevisionInfo }> {
  return request(`/api/revisions/${encodeURIComponent(reference)}`);
}

export function fetchSourceComparison(fromRef: string, toRef: string, scope = ""): Promise<SourceComparison> {
  const params = new URLSearchParams({ from_ref: fromRef, to_ref: toRef });
  if (scope) params.set("scope", scope);
  return request<SourceComparison>(`/api/compare?${params.toString()}`);
}

export function fetchReviewActivity(id: string): Promise<{ review_id: string; events: ReviewActivityEvent[] }> {
  return request(`/api/reviews/${encodeURIComponent(id)}/activity`);
}

export function fetchTargetHistory(
  id: string,
  unitKey: string,
): Promise<{ review_id: string; reviewer_id: string; unit_key: string; events: ReviewMarkEvent[] }> {
  const encodedUnitKey = unitKey.split("/").map(encodeURIComponent).join("/");
  return request(`/api/reviews/${encodeURIComponent(id)}/targets/${encodedUnitKey}/history`);
}

export function fetchAnnotationVersions(
  annotationId: string,
): Promise<{ annotation_id: string; versions: AnnotationVersion[] }> {
  return request(`/api/annotations/${encodeURIComponent(annotationId)}/versions`);
}

export function fetchAnnotations(id: string): Promise<AnnotationList> {
  return request<AnnotationList>(`/api/reviews/${encodeURIComponent(id)}/annotations`);
}

export function fetchHistoricalAnnotations(id: string, revisionNumber: number): Promise<AnnotationList> {
  return request<AnnotationList>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/annotations`,
  );
}

export function fetchReviewEvidence(id: string, revisionId = ""): Promise<ReviewEvidenceList> {
  const params = new URLSearchParams();
  if (revisionId) params.set("revision_id", revisionId);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return request<ReviewEvidenceList>(`/api/reviews/${encodeURIComponent(id)}/evidence${query}`);
}

export function fetchReviewVerification(id: string, revisionId = ""): Promise<ReviewVerificationProjection> {
  const params = new URLSearchParams();
  if (revisionId) params.set("revision_id", revisionId);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return request<ReviewVerificationProjection>(`/api/reviews/${encodeURIComponent(id)}/verification${query}`);
}

export function waiveReviewVerificationCheck(
  id: string,
  payload: { revision_id: string; name: string; reason: string },
): Promise<ReviewVerificationWaiverResult> {
  return request<ReviewVerificationWaiverResult>(`/api/reviews/${encodeURIComponent(id)}/verification/waivers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function fetchReviewDependencies(id: string, revisionId = ""): Promise<ReviewDependencyProjection> {
  const params = new URLSearchParams();
  if (revisionId) params.set("revision_id", revisionId);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return request<ReviewDependencyProjection>(`/api/reviews/${encodeURIComponent(id)}/dependencies${query}`);
}

export function fetchReviewReadiness(id: string): Promise<ReviewLandingReadiness> {
  return request<ReviewLandingReadiness>(`/api/reviews/${encodeURIComponent(id)}/readiness`);
}

export function armReviewLanding(
  id: string,
  payload: {
    expected_revision_id: string;
    expected_tree_fingerprint: string;
    expected_conditions_hash: string;
  },
): Promise<ReviewLandingReadiness> {
  return request<ReviewLandingReadiness>(`/api/reviews/${encodeURIComponent(id)}/landing/arm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ armed: true, ...payload }),
  });
}

export function cancelReviewLanding(id: string, expectedRevisionId: string): Promise<ReviewLandingReadiness> {
  return request<ReviewLandingReadiness>(`/api/reviews/${encodeURIComponent(id)}/landing/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision_id: expectedRevisionId }),
  });
}

function evidenceKind(file: File): "screenshot" | "video" | "playwright_trace" | "document" {
  if (file.name.toLowerCase().endsWith("trace.zip")) return "playwright_trace";
  if (file.type.startsWith("image/")) return "screenshot";
  if (file.type.startsWith("video/")) return "video";
  return "document";
}

/** Upload one browser-selected artifact directly; no multipart parser is needed server-side. */
export async function uploadReviewEvidence(
  id: string,
  file: File,
  path = "",
  expectedRevisionId = "",
): Promise<ReviewEvidenceResult> {
  const params = new URLSearchParams({
    kind: evidenceKind(file),
    title: file.name,
    filename: file.name,
    mime_type: file.type || "application/octet-stream",
  });
  if (path) params.set("path", path);
  if (expectedRevisionId) params.set("expected_revision_id", expectedRevisionId);
  const headers = new Headers({ "Content-Type": file.type || "application/octet-stream" });
  const response = await authenticatedFetch(
    `/api/reviews/${encodeURIComponent(id)}/evidence/upload?${params.toString()}`,
    { method: "POST", body: file, headers },
  );
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Keep the status when a non-JSON response cannot explain itself.
    }
    throw new Error(detail);
  }
  return (await response.json()) as ReviewEvidenceResult;
}

export function linkReviewEvidence(
  id: string,
  url: string,
  title: string,
  path = "",
  expectedRevisionId = "",
): Promise<ReviewEvidenceResult> {
  return request<ReviewEvidenceResult>(`/api/reviews/${encodeURIComponent(id)}/evidence/link`, {
    method: "POST",
    body: JSON.stringify({ url, title, path, expected_revision_id: expectedRevisionId }),
  });
}

export async function fetchReviewEvidenceContent(evidenceId: string): Promise<Blob> {
  const response = await authenticatedFetch(`/api/evidence/${encodeURIComponent(evidenceId)}/content`);
  if (!response.ok) throw new Error(`${response.status}`);
  return response.blob();
}

/**
 * Create a comment from a selected line range.
 *
 * The body carries the path, the range and the text — and nothing that looks
 * like an anchor. Every hash, the surrounding context and the owning definition
 * are computed server-side from the file's own bytes, because a viewer's line
 * number is a rendering detail of one revision (spec §5.4).
 */
export function postAnnotation(
  id: string,
  draft: AnnotationDraft,
  expectedRevisionId: string,
): Promise<AnnotationResult> {
  return request<AnnotationResult>(`/api/reviews/${encodeURIComponent(id)}/annotations`, {
    method: "POST",
    body: JSON.stringify({ ...draft, expected_revision_id: expectedRevisionId }),
  });
}

/** Edit a comment's text, disposition or state. Never its position. */
export function patchAnnotation(
  annotationId: string,
  fields: { body?: string; kind?: AnnotationKind; state?: AnnotationState },
  expectedRevisionId = "",
): Promise<AnnotationResult> {
  return request<AnnotationResult>(`/api/annotations/${encodeURIComponent(annotationId)}`, {
    method: "PATCH",
    body: JSON.stringify(
      fields.state ? { ...fields, expected_revision_id: expectedRevisionId } : fields,
    ),
  });
}

/** Render the review's comments as Markdown. Renders; delivers nothing. */
export function exportFeedback(id: string): Promise<FeedbackExport> {
  return request<FeedbackExport>(`/api/reviews/${encodeURIComponent(id)}/feedback/export`, { method: "POST" });
}

/** Deliver exactly the prepared feedback preview to its exact coding-agent session. */
export function deliverFeedback(id: string, feedback: FeedbackExport): Promise<FeedbackDelivery> {
  return request<FeedbackDelivery>(`/api/reviews/${encodeURIComponent(id)}/feedback/deliver`, {
    method: "POST",
    body: JSON.stringify({
      operation_id: feedback.operation_id,
      expected_revision_id: feedback.revision_id,
      expected_feedback_hash: feedback.feedback_hash,
    }),
  });
}

/** Plan the GitHub review for a pull-request Review; posts nothing. */
export function previewGitHubPublish(id: string): Promise<GitHubPublishPreview> {
  return request<GitHubPublishPreview>(`/api/reviews/${encodeURIComponent(id)}/github/preview`, { method: "POST" });
}

/** Post exactly the previewed plan to the pull request as one GitHub review. */
export function publishGitHubReview(id: string, preview: GitHubPublishPreview): Promise<GitHubPublishResult> {
  return request<GitHubPublishResult>(`/api/reviews/${encodeURIComponent(id)}/github/publish`, {
    method: "POST",
    body: JSON.stringify({
      operation_id: preview.operation_id,
      expected_revision_id: preview.revision_id,
      expected_plan_hash: preview.plan_hash,
    }),
  });
}

/** Read the exact target-based finish sheet without changing review status. */
export function fetchReviewOutcome(id: string): Promise<ReviewOutcomeProjection> {
  return request<ReviewOutcomeProjection>(`/api/reviews/${encodeURIComponent(id)}/outcome`);
}

export function postReviewOutcome(
  id: string,
  outcome: ReviewOutcomeKind,
  summary = "",
  expectedRevisionId = "",
): Promise<ReviewOutcomeProjection> {
  return request<ReviewOutcomeProjection>(`/api/reviews/${encodeURIComponent(id)}/outcome`, {
    method: "POST",
    body: JSON.stringify({ outcome, summary, expected_revision_id: expectedRevisionId }),
  });
}

export function fetchFinishReview(id: string): Promise<ReviewClosure> {
  return request<ReviewClosure>(`/api/reviews/${encodeURIComponent(id)}/finish`);
}

/** Record the human's status choice; this call never infers an approval verdict. */
export function finishReview(
  id: string,
  status: "open" | "finished" | "archived" = "finished",
  expectedRevisionId = "",
): Promise<ReviewClosure> {
  return request<ReviewClosure>(`/api/reviews/${encodeURIComponent(id)}/finish`, {
    method: "POST",
    body: JSON.stringify({ status, expected_revision_id: expectedRevisionId }),
  });
}
