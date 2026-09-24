/**
 * The Review fetch layer. The machine credential never enters JavaScript.
 *
 * Local Review uses a server-minted, Review-only browser capability after the
 * machine-authenticated CLI arms a short pairing window. The derived capability
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
  MarkResult,
  MarkState,
  RelatedSource,
  ReviewActivityEvent,
  ReviewChangeProposalApplyResult,
  ReviewChangeProposalList,
  ReviewChangeProposalResult,
  ReviewClosure,
  ReviewEvidenceList,
  ReviewEvidenceResult,
  ReviewMarkEvent,
  ReviewOutcomeKind,
  ReviewOutcomeProjection,
  ReviewOverview,
  ReviewProposalSelection,
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
  if (typeof window === "undefined") return { token: "", reviewId };

  const raw = window.location.hash.replace(/^#/, "");
  if (raw) {
    const params = new URLSearchParams(raw);
    const nextReview = params.get("r") ?? "";
    const nextPreparation = params.get("p") ?? "";
    if (nextReview) {
      reviewId = nextReview;
      window.sessionStorage.setItem(REVIEW_KEY, nextReview);
    }
    if (nextPreparation) {
      preparationId = nextPreparation;
      window.sessionStorage.setItem(PREPARATION_KEY, nextPreparation);
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
  return preparationId ? { token: "", reviewId, preparationId } : { token: "", reviewId };
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

export function historicalReviewHref(revisionIdOrRef: string): string {
  const id = stripRefNamespace(revisionIdOrRef, "rr/");
  return `/rr/${encodeURIComponent(id)}`;
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

export async function authenticatedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const local = isLiteralLoopbackOrigin();
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
    // A stale capability is never kept alive by accident. The clean page URL
    // can claim a new one only while `lc review --open` has armed that path.
    forgetBrowserSession();
    const claimed = await claimLocalBrowserSession(window.location.pathname);
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
  window.location.assign(`/v1/auth/browser/start?return_to=${encodeURIComponent(returnTo)}`);
  throw new Error("authentication required");
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
    throw new Error(detail);
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

export function fetchHistoricalOverview(id: string, revisionNumber: number): Promise<ReviewOverview> {
  return request<ReviewOverview>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}`,
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

export function fetchReviewSurfaces(id: string): Promise<ReviewSurfaceList> {
  return request<ReviewSurfaceList>(`/api/reviews/${encodeURIComponent(id)}/surfaces`);
}

export function runReviewSurface(
  id: string,
  provider: string,
  surfaceId: string,
  side: "old" | "new",
): Promise<ReviewSurfaceRunResponse> {
  const params = new URLSearchParams({ provider, surface_id: surfaceId, side });
  return request<ReviewSurfaceRunResponse>(
    `/api/reviews/${encodeURIComponent(id)}/surface-run?${params.toString()}`,
    { method: "POST" },
  );
}
export function fetchFile(id: string, path: string): Promise<FileDetail> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  return request<FileDetail>(`/api/reviews/${encodeURIComponent(id)}/files/${segments}/patch`);
}

export function fetchHistoricalFile(id: string, revisionNumber: number, path: string): Promise<FileDetail> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  return request<FileDetail>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/files/${segments}/patch`,
  );
}

export function fetchReviewPatch(id: string, revisionNumber?: number): Promise<Blob> {
  const base = `/api/reviews/${encodeURIComponent(id)}`;
  return fetchReviewBlob(
    revisionNumber
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

export function postMark(id: string, unitKey: string, state: MarkState): Promise<MarkResult> {
  return request<MarkResult>(`/api/reviews/${encodeURIComponent(id)}/marks`, {
    method: "POST",
    body: JSON.stringify({ unit_key: unitKey, state }),
  });
}

export function postBulkReviewed(id: string, unitKeys: string[]): Promise<BulkMarkResult> {
  return request<BulkMarkResult>(`/api/reviews/${encodeURIComponent(id)}/marks/bulk`, {
    method: "POST",
    body: JSON.stringify({ unit_keys: unitKeys }),
  });
}

export function fetchSourceState(id: string): Promise<SourceState> {
  return request<SourceState>(`/api/reviews/${encodeURIComponent(id)}/source-state`);
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

export function fetchRevisions(id: string): Promise<{ revisions: RevisionInfo[] }> {
  return request(`/api/reviews/${encodeURIComponent(id)}/revisions`);
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

export function fetchReviewEvidence(id: string): Promise<ReviewEvidenceList> {
  return request<ReviewEvidenceList>(`/api/reviews/${encodeURIComponent(id)}/evidence`);
}

function evidenceKind(file: File): "screenshot" | "video" | "playwright_trace" | "document" {
  if (file.name.toLowerCase().endsWith("trace.zip")) return "playwright_trace";
  if (file.type.startsWith("image/")) return "screenshot";
  if (file.type.startsWith("video/")) return "video";
  return "document";
}

/** Upload one browser-selected artifact directly; no multipart parser is needed server-side. */
export async function uploadReviewEvidence(id: string, file: File, path = ""): Promise<ReviewEvidenceResult> {
  const params = new URLSearchParams({
    kind: evidenceKind(file),
    title: file.name,
    filename: file.name,
    mime_type: file.type || "application/octet-stream",
  });
  if (path) params.set("path", path);
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

export function linkReviewEvidence(id: string, url: string, title: string, path = ""): Promise<ReviewEvidenceResult> {
  return request<ReviewEvidenceResult>(`/api/reviews/${encodeURIComponent(id)}/evidence/link`, {
    method: "POST",
    body: JSON.stringify({ url, title, path }),
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
export function postAnnotation(id: string, draft: AnnotationDraft): Promise<AnnotationResult> {
  return request<AnnotationResult>(`/api/reviews/${encodeURIComponent(id)}/annotations`, {
    method: "POST",
    body: JSON.stringify(draft),
  });
}

/** Edit a comment's text, disposition or state. Never its position. */
export function patchAnnotation(
  annotationId: string,
  fields: { body?: string; kind?: AnnotationKind; state?: AnnotationState },
): Promise<AnnotationResult> {
  return request<AnnotationResult>(`/api/annotations/${encodeURIComponent(annotationId)}`, {
    method: "PATCH",
    body: JSON.stringify(fields),
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

/** Read the exact target-based finish sheet without changing review status. */
export function fetchReviewOutcome(id: string): Promise<ReviewOutcomeProjection> {
  return request<ReviewOutcomeProjection>(`/api/reviews/${encodeURIComponent(id)}/outcome`);
}

export function postReviewOutcome(
  id: string,
  outcome: ReviewOutcomeKind,
  summary = "",
): Promise<ReviewOutcomeProjection> {
  return request<ReviewOutcomeProjection>(`/api/reviews/${encodeURIComponent(id)}/outcome`, {
    method: "POST",
    body: JSON.stringify({ outcome, summary }),
  });
}

export function fetchFinishReview(id: string): Promise<ReviewClosure> {
  return request<ReviewClosure>(`/api/reviews/${encodeURIComponent(id)}/finish`);
}

/** Record the human's status choice; this call never infers an approval verdict. */
export function finishReview(id: string, status: "open" | "finished" | "archived" = "finished"): Promise<ReviewClosure> {
  return request<ReviewClosure>(`/api/reviews/${encodeURIComponent(id)}/finish`, {
    method: "POST",
    body: JSON.stringify({ status }),
  });
}
