/**
 * The workspace's typed fetch layer, and the only place the token lives.
 *
 * The credential arrives in the URL **fragment** — never a query string, so it
 * is not sent in the HTTP request or `Referer`. The fragment deliberately stays
 * in the address bar so ordinary browser copy/paste produces a working reopen
 * URL. The token is also retained in memory plus tab-scoped `sessionStorage`.
 *
 * `sessionStorage` is origin/port scoped and dies with the tab. The token is
 * also per workspace process, so a stale value cannot authenticate to a future
 * process with a new random token. It is never written to `localStorage` or a
 * cookie.
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
  ReviewClosure,
  ReviewEvidenceList,
  ReviewEvidenceResult,
  ReviewOverview,
  ReviewTargetList,
  SourceState,
} from "./types";

let token = "";
let reviewId = "";

const TOKEN_KEY = "lemoncrow.review.token";
const REVIEW_KEY = "lemoncrow.review.id";

export interface Bootstrap {
  token: string;
  reviewId: string;
}

/**
 * Read `#t=<token>&r=<review-id>` and keep both in memory/tab storage. The
 * fragment remains visible so copying the browser URL also copies everything
 * needed to reopen the workspace. With no fragment, restore the same process
 * credential from this tab's `sessionStorage`.
 *
 * Idempotent: React 18's double-invoked effects and browser reloads both keep
 * the credential rather than turning a review into a trip back to the shell.
 */
export function adoptBootstrapFragment(): Bootstrap {
  if (typeof window === "undefined") return { token, reviewId };
  const raw = window.location.hash.replace(/^#/, "");
  if (raw) {
    const params = new URLSearchParams(raw);
    const nextToken = params.get("t") ?? "";
    const nextReview = params.get("r") ?? "";
    if (nextToken) {
      token = nextToken;
      window.sessionStorage.setItem(TOKEN_KEY, nextToken);
    }
    if (nextReview) {
      reviewId = nextReview;
      window.sessionStorage.setItem(REVIEW_KEY, nextReview);
    }
  } else {
    token ||= window.sessionStorage.getItem(TOKEN_KEY) ?? "";
    reviewId ||= window.sessionStorage.getItem(REVIEW_KEY) ?? "";
    // Older workspace builds stripped the fragment after bootstrapping. Repair
    // those still-open tabs from their tab-scoped credential so a refresh moves
    // them onto the new copyable-URL contract without another terminal trip.
    if (token && reviewId) {
      const fragment = new URLSearchParams({ t: token, r: reviewId }).toString();
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${fragment}`);
    }
  }
  return { token, reviewId };
}

export function currentReviewId(): string {
  return reviewId;
}

export function hasToken(): boolean {
  return token !== "";
}
/** Test seam. Production code only ever gets the token from the fragment. */
export function __setBootstrapForTest(next: Bootstrap): void {
  token = next.token;
  reviewId = next.reviewId;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers, credentials: "omit" });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // A non-JSON error body is still an error; do not invent a reason for it.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export function fetchOverview(id: string): Promise<ReviewOverview> {
  return request<ReviewOverview>(`/api/reviews/${encodeURIComponent(id)}`);
}

export function fetchTargets(id: string, order: "recommended" | "file" = "recommended"): Promise<ReviewTargetList> {
  return request<ReviewTargetList>(
    `/api/reviews/${encodeURIComponent(id)}/targets?order=${encodeURIComponent(order)}`,
  );
}

export function fetchFile(id: string, path: string): Promise<FileDetail> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  return request<FileDetail>(`/api/reviews/${encodeURIComponent(id)}/files/${segments}/patch`);
}

export function fetchRelatedSource(id: string, path: string, line: number): Promise<RelatedSource> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  return request<RelatedSource>(
    `/api/reviews/${encodeURIComponent(id)}/related/${segments}?line=${encodeURIComponent(String(line || 1))}`,
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

export function postRefresh(id: string): Promise<ReviewOverview> {
  return request<ReviewOverview>(`/api/reviews/${encodeURIComponent(id)}/refresh`, { method: "POST" });
}

export function listReviews(): Promise<{ reviews: { id: string; title: string }[] }> {
  return request("/api/reviews");
}

export function fetchAnnotations(id: string): Promise<AnnotationList> {
  return request<AnnotationList>(`/api/reviews/${encodeURIComponent(id)}/annotations`);
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
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  headers.set("Content-Type", file.type || "application/octet-stream");
  const response = await fetch(`/api/reviews/${encodeURIComponent(id)}/evidence/upload?${params.toString()}`, {
    method: "POST",
    body: file,
    headers,
    credentials: "omit",
  });
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
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`/api/evidence/${encodeURIComponent(evidenceId)}/content`, {
    headers,
    credentials: "omit",
  });
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

/** Deliver open human feedback to the exact Claude session that authored this revision. */
export function deliverFeedbackToClaude(id: string): Promise<FeedbackDelivery> {
  return request<FeedbackDelivery>(`/api/reviews/${encodeURIComponent(id)}/feedback/claude`, { method: "POST" });
}

/** Read the exact target-based finish sheet without changing review status. */
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
