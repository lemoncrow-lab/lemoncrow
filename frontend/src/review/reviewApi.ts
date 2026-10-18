import { subscribeReviewState } from "./reviewStateEvents";
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

import { transientRequestFailure } from "./transientRetry";
import type {
  ReviewProjectContextResponse,
  ReviewProjectContextSelection,
  ThreadSummaryRead,
  ThreadSummaryRun,
  ReviewAiReviewRunResult,
  ReviewAiExecutionStatus,
  ReviewBehaviorNarrative,
  ReviewBehaviorTarget,
  ReviewManualChecklist,
  ReviewDecisionEventResult,
  ReviewSynthesisStatus,
  ReviewProjection,
  AnnotationDraft,
  AnnotationKind,
  AnnotationList,
  AnnotationResult,
  AnnotationState,
  BulkMarkResult,
  HostedAction,
  HostedAttachmentStatus,
  HostedDetachResult,
  HostedQueuedWrite,
  FeedbackDelivery,
  FeedbackExport,
  FileDetail,
  FixApplyPlan,
  FixLine,
  FixLineApplyResult,
  FixLineCloseResult,
  FixLineList,
  ReviewFileContents,
  ReviewComparisonFileContents,
  GitHubPublishPreview,
  GitHubPublishResult,
  MarkResult,
  MarkState,
  RelatedSource,
  ReviewAskConversation,
  ReviewAskConversationOrigin,
  ReviewAskConversationSummary,
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
  ReviewRecordExportResult,
  ReviewRecordStatus,
  ReviewPremergeProjection,
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
  ReviewMapProjection,
  ReviewArchitectureExplanationResponse,
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
  ReviewRuntimeValidationResponse,
  ReviewTargetList,
  AnnotationVersion,
  SourceComparison,
  SourceState,
  ReviewDirectoryPage,
  ReviewDirectoryStatus,
  ReviewDestinationChoice,
  ReviewDestinationOptions,
  ReviewDestinationState,
  InboxFilterDefinition,
  InboxView,
  InboxWorkflowState,
  SavedInboxFilter,
  SavedInboxFilterList,
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
  /** This reviewer's judgments (another tab's marks); absent on older servers. */
  marks_cursor?: string;
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
export function adoptBootstrapFragment({ restorePreparation = true }: { restorePreparation?: boolean } = {}): Bootstrap {
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
    // A named saved Review already owns its Snapshot. An old tab-scoped job
    // must not hold it behind preparation from an earlier terminal opening.
    if (restorePreparation) preparationId = window.sessionStorage.getItem(PREPARATION_KEY) ?? "";
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
  // Arbitrary source specs can themselves contain ".." (for example a Git
  // range). Keep them in independent URL fields instead of inventing a path
  // delimiter that valid source syntax can collide with. ReviewCompareReader
  // still accepts the legacy /r/x/<from>..<to> form for old links.
  const params = new URLSearchParams({ from: fromRef, to: toRef });
  if (scope) params.set("scope", scope);
  return `/r/x?${params.toString()}`;
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
  browserSessionRenewChecked = false;
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

/** Shown when this browser has no usable local capability and no CLI pairing is armed. */
export const LOCAL_BROWSER_RECONNECT_MESSAGE =
  "This browser is not connected to LemonCrow Review. Run `lc review --open` in your terminal to reconnect.";

/** Renew a capability once it is a day old so active browsers never need re-pairing. */
const BROWSER_SESSION_RENEW_AFTER_S = 24 * 60 * 60;
const BROWSER_SESSION_TTL_S = 30 * 24 * 60 * 60;
let browserSessionRenewChecked = false;

function browserSessionWantsRenewal(capability: string): boolean {
  // ``v1.<expires_at>.<nonce>.<signature>``: the expiry is not secret.
  const [version, rawExpiry] = capability.split(".");
  const expiresAt = Number(rawExpiry);
  if (version !== "v1" || !Number.isFinite(expiresAt)) return false;
  return expiresAt - Date.now() / 1000 < BROWSER_SESSION_TTL_S - BROWSER_SESSION_RENEW_AFTER_S;
}

async function postLocalBrowserClaim(path: string, capability: string): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (capability) headers[BROWSER_SESSION_HEADER] = capability;
  return fetch("/v1/auth/local-browser/claim", {
    method: "POST",
    headers,
    body: JSON.stringify({ path }),
    credentials: "same-origin",
  });
}

/**
 * Obtain a local Review capability: renew a still-valid one first, then fall
 * back to the CLI pairing that `lc review --open` arms. A refused claim becomes
 * one clear reconnect instruction instead of a raw authentication error.
 */
function claimLocalBrowserSession(path: string, renewing = ""): Promise<Response> {
  if (!localBrowserClaim) {
    localBrowserClaim = (async () => {
      let response = renewing ? await postLocalBrowserClaim(path, renewing) : null;
      if (!response || response.status === 401) {
        if (response) forgetBrowserSession();
        response = await postLocalBrowserClaim(path, "");
      }
      if (response.status === 401) {
        return new Response(JSON.stringify({ detail: LOCAL_BROWSER_RECONNECT_MESSAGE }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        });
      }
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
      return new Response(JSON.stringify({ detail: LOCAL_BROWSER_RECONNECT_MESSAGE }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    })().finally(() => {
      localBrowserClaim = null;
    });
  }
  return localBrowserClaim;
}

let hostedCredentialRefresh: Promise<Response> | null = null;

/**
 * Refresh the hosted Authward browser credential once for every concurrent 401.
 * Authward rotates the refresh token, so parallel refreshes would spend the
 * same token and all but one would be refused, forcing a full sign-in.
 * Callers may read only the status of the shared Response, never its body.
 */
export function refreshHostedBrowserCredential(): Promise<Response> {
  if (!hostedCredentialRefresh) {
    hostedCredentialRefresh = fetch("/v1/auth/browser/refresh", {
      method: "POST",
      credentials: "same-origin",
    }).finally(() => {
      hostedCredentialRefresh = null;
    });
  }
  return hostedCredentialRefresh;
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
  // SSE keeps Review live continuously and is background transport, not
  // foreground user work. Counting it here made the global Loading UI
  // effectively permanent whenever a Review was open.
  const reportActivity = !/\/(?:live-state|source-state)(?:\/events)?(?:\?|$)/.test(path) && !path.startsWith("/api/review-preparations/");
  if (reportActivity) reportAuthenticatedRequest(1);
  try {
    const local = isLiteralLoopbackOrigin();
    const localReviewRoute = typeof window !== "undefined"
      && (/^\/r\//.test(window.location.pathname) || /^\/rr\//.test(window.location.pathname)
        || window.location.pathname.startsWith("/reviews/preparing/"));
    let initialLocalClaim: Response | null = null;

    // Claim a local Review capability before any Reader loader fires so startup
    // does not spray predictable 401s and then retry them. Concurrent loaders
    // share claimLocalBrowserSession's single in-flight promise.
    if (local && localReviewRoute && !currentBrowserSession()) {
      initialLocalClaim = await claimLocalBrowserSession(window.location.pathname);
      if (!initialLocalClaim.ok && initialLocalClaim.status !== 404) {
        return initialLocalClaim.clone();
      }
    } else if (local && !browserSessionRenewChecked && currentBrowserSession()) {
      // Once per page load, roll an ageing capability forward so a browser in
      // regular use never reaches expiry and never needs a fresh CLI pairing.
      browserSessionRenewChecked = true;
      const stored = currentBrowserSession();
      if (browserSessionWantsRenewal(stored)) {
        const renewed = await claimLocalBrowserSession(window.location.pathname, stored);
        if (!renewed.ok && renewed.status !== 404) return renewed.clone();
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
    const refreshed = await refreshHostedBrowserCredential();
    if (refreshed.ok) return send();
    // An Authward outage is transient: a sign-in round trip would fail the
    // same way, so keep the reviewer (and their drafts) where they are.
    if (refreshed.status >= 500) {
      throw Object.assign(
        new Error("Sign-in service is temporarily unavailable. Try again in a moment."),
        { status: refreshed.status },
      );
    }

    const returnTo = `${window.location.pathname}${window.location.search}`;
    if (refreshed.status === 403) {
      window.location.assign(
        `/login?error=access_denied&return_to=${encodeURIComponent(returnTo)}`,
      );
      throw Object.assign(
        new Error("This account does not have access to this organization."),
        { status: 403 },
      );
    }
    window.location.assign(
      `/v1/auth/browser/start?return_to=${encodeURIComponent(returnTo)}`,
    );
    throw new Error("authentication required");
  } finally {
    if (reportActivity) reportAuthenticatedRequest(-1);
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
    // A structured refusal (e.g. a discard that needs confirmation) keeps its body for the caller.
    let structured: Record<string, unknown> | undefined;
    try {
      const body = (await response.json()) as { detail?: unknown; error?: { message?: unknown } };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail && typeof body.detail === "object") {
        structured = body.detail as Record<string, unknown>;
        if (typeof structured.message === "string") detail = structured.message;
      } else if (typeof body.error?.message === "string") detail = body.error.message;
    } catch {
      // A non-JSON error body is still an error; do not invent a reason for it.
    }
    throw Object.assign(new Error(detail), { status: response.status, detail: structured });
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
  /** Any of these selects the Inbox view mode (P9-INBOX): per-viewer views, workflow, saved filter. */
  view?: InboxView;
  workflow?: InboxWorkflowState | "";
  filter?: string;
  /** Initial local-only list: no progress, workflow, readiness or Git reads. */
  summary?: boolean;
}

export function reviewDirectoryParams(query: ReviewDirectoryQuery): URLSearchParams {
  const params = new URLSearchParams();
  if (query.view) params.set("view", query.view);
  params.set("status", query.status);
  params.set("q", query.query);
  params.set("limit", String(query.limit));
  if (query.workflow) params.set("workflow", query.workflow);
  if (query.filter) params.set("filter", query.filter);
  if (query.cursor) params.set("cursor", query.cursor);
  if (query.summary) params.set("summary", "1");
  return params;
}

export function fetchReviewDirectory(query: ReviewDirectoryQuery): Promise<ReviewDirectoryPage> {
  return request<ReviewDirectoryPage>(`/v1/reviews?${reviewDirectoryParams(query).toString()}`);
}

/** The local user's saved Inbox filters (a selection, never access). */
export const INBOX_FILTERS_PATH = "/api/review-inbox/filters";

export function fetchInboxFilters(): Promise<SavedInboxFilterList> {
  return request<SavedInboxFilterList>(INBOX_FILTERS_PATH);
}

export function createInboxFilter(name: string, definition: Partial<InboxFilterDefinition>): Promise<{ filter: SavedInboxFilter }> {
  return request(INBOX_FILTERS_PATH, { method: "POST", body: JSON.stringify({ name, definition }) });
}

export function deleteInboxFilter(id: string): Promise<{ deleted: boolean; id: string }> {
  return request(`${INBOX_FILTERS_PATH}/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** Permanent delete of an archived Review: the request must name it exactly (its id or exact title). */
export function deleteReview(id: string, confirmIdentity: string): Promise<{ deleted: boolean; review_id: string }> {
  return request(`/api/reviews/${encodeURIComponent(id)}`, {
    method: "DELETE",
    body: JSON.stringify({ confirm_identity: confirmIdentity }),
  });
}

export function updateReviewTitle(repoId: string, reviewId: string, title: string): Promise<{ title: string; auto_title: string; title_is_custom: boolean }> {
  return request(`/api/repos/${encodeURIComponent(repoId)}/reviews/${encodeURIComponent(reviewId)}/title`, {
    method: "PUT",
    body: JSON.stringify({ title }),
  });
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

export function fetchStartupOverview(id: string): Promise<ReviewOverview> {
  return request<ReviewOverview>(`/api/reviews/${encodeURIComponent(id)}?startup=true`);
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
  projection: ReviewMapProjection = "change_map",
): Promise<ReviewMapPayload> {
  const params = new URLSearchParams({ level });
  if (nodeId) params.set("node_id", nodeId);
  if (projection !== "change_map") params.set("projection", projection);
  return request<ReviewMapPayload>(
    `/api/reviews/${encodeURIComponent(id)}/map?${params.toString()}`,
  );
}

export function fetchHistoricalReviewMap(
  id: string,
  revisionNumber: number,
  level: ReviewMapLevel = "architecture",
  nodeId = "",
  projection: ReviewMapProjection = "change_map",
): Promise<ReviewMapPayload> {
  const params = new URLSearchParams({ level });
  if (nodeId) params.set("node_id", nodeId);
  if (projection !== "change_map") params.set("projection", projection);
  return request<ReviewMapPayload>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/map?${params.toString()}`,
  );
}

/**
 * PK2: bounded landed history for the Brief or one Map selection. A plain GET:
 * it reads derived Project Knowledge and never starts AI or background work.
 */
export function fetchReviewProjectContext(
  id: string,
  revisionNumber: number,
  purpose: "brief" | "map" = "brief",
  selection: ReviewProjectContextSelection = {},
): Promise<ReviewProjectContextResponse> {
  const params = new URLSearchParams({ purpose, revision_number: String(revisionNumber) });
  for (const path of selection.paths ?? []) params.append("path", path);
  for (const component of selection.components ?? []) params.append("component", component);
  for (const edge of selection.edges ?? []) params.append("edge", edge);
  for (const symbol of selection.symbols ?? []) params.append("symbol", symbol);
  return request<ReviewProjectContextResponse>(
    `/api/reviews/${encodeURIComponent(id)}/project-context?${params.toString()}`,
  );
}

export function explainHistoricalReviewArchitecture(
  id: string,
  revisionNumber: number,
): Promise<ReviewArchitectureExplanationResponse> {
  return request<ReviewArchitectureExplanationResponse>(
    `/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/architecture-explain`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function fetchTargets(id: string, order: "recommended" | "file" = "recommended"): Promise<ReviewTargetList> {
  return request<ReviewTargetList>(
    `/api/reviews/${encodeURIComponent(id)}/targets?order=${encodeURIComponent(order)}`,
  );
}

export function fetchStartupTargets(id: string, order: "recommended" | "file" = "recommended"): Promise<ReviewTargetList> {
  return request<ReviewTargetList>(
    `/api/reviews/${encodeURIComponent(id)}/targets?order=${encodeURIComponent(order)}&startup=true`,
  );
}

export function fetchTargetsForPath(
  id: string,
  path: string,
  order: "recommended" | "file" = "recommended",
): Promise<ReviewTargetList> {
  const params = new URLSearchParams({ order, path });
  return request<ReviewTargetList>(
    `/api/reviews/${encodeURIComponent(id)}/targets?${params.toString()}`,
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

export interface BriefPreparationProgress {
  revision_id: string;
  state: "pending" | "running" | "ready" | "failed";
  completed: number;
  total: number;
  detail: string;
}

export function fetchBriefPreparation(id: string, revisionNumber?: number): Promise<BriefPreparationProgress> {
  const scope = revisionNumber === undefined ? "" : `/revisions/${revisionNumber}`;
  return request<BriefPreparationProgress>(`/api/reviews/${encodeURIComponent(id)}${scope}/projection/preparation`);
}

export function fetchProjection(id: string): Promise<ReviewProjection> {
  return request<ReviewProjection>(`/api/reviews/${encodeURIComponent(id)}/projection`);
}

export function fetchHistoricalProjection(id: string, revisionNumber: number): Promise<ReviewProjection> {
  return request<ReviewProjection>(`/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/projection`);
}

/** Record one human decision disposition; bound to the displayed Snapshot and the last event seen. */
export function postDecisionEvent(
  id: string,
  key: string,
  body: {
    action: string;
    option_key?: string;
    reason?: string;
    trigger?: string;
    feedback_annotation_id?: string;
    /** Request change: the server files this as human feedback in the same write. */
    feedback_body?: string;
    feedback_path?: string;
    expected_revision_id: string;
    expected_event_id: string;
    /** The decision scope the reviewer saw; a changed scope refuses the write. */
    expected_semantic_fingerprint: string;
  },
): Promise<ReviewDecisionEventResult> {
  return request<ReviewDecisionEventResult>(
    `/api/reviews/${encodeURIComponent(id)}/decisions/${encodeURIComponent(key)}/events`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** Explain unexplained Brief groups for the exact Snapshot on screen. */
export function postSynthesis(id: string, expectedRevisionId: string): Promise<{ synthesis: ReviewSynthesisStatus }> {
  return request<{ synthesis: ReviewSynthesisStatus }>("/api/reviews/" + encodeURIComponent(id) + "/synthesis", {
    method: "POST",
    body: JSON.stringify({ expected_revision_id: expectedRevisionId }),
  });
}

/** Refine only deterministic Tour steps that still need wording help. */
export function postTourSynthesis(id: string, expectedRevisionId: string): Promise<{ synthesis: ReviewSynthesisStatus }> {
  return request<{ synthesis: ReviewSynthesisStatus }>("/api/reviews/" + encodeURIComponent(id) + "/tour/synthesis", {
    method: "POST",
    body: JSON.stringify({ expected_revision_id: expectedRevisionId }),
  });
}

/** One behavior target with the evidence (and digest) a judgment must echo. */
export function fetchBehaviorTarget(id: string, behaviorKey: string): Promise<{ revision_id: string; target: ReviewBehaviorTarget }> {
  return request<{ revision_id: string; target: ReviewBehaviorTarget }>(
    "/api/reviews/" + encodeURIComponent(id) + "/behavior/targets/" + encodeURIComponent(behaviorKey),
  );
}

/** Judge one behavior target; `evidenceDigest` is the digest of the evidence block the client rendered. */
export function postBehaviorJudgment(
  id: string,
  expectedRevisionId: string,
  judgment: { behavior_key: string; state: "reviewed" | "needs_changes" | "unreviewed"; evidence_digest: string },
): Promise<{ recorded: { behavior_key: string; recorded: string }[]; refused: { behavior_key: string; code: string; reason: string }[] }> {
  return request("/api/reviews/" + encodeURIComponent(id) + "/behavior/marks", {
    method: "POST",
    body: JSON.stringify({ expected_revision_id: expectedRevisionId, judgments: [judgment] }),
  });
}

/** Run the single behavior-narrative synthesis call; the deterministic list is the fallback. */
export function postBehaviorNarrative(id: string, expectedRevisionId: string): Promise<{ narrative: ReviewBehaviorNarrative | null }> {
  return request<{ narrative: ReviewBehaviorNarrative | null }>("/api/reviews/" + encodeURIComponent(id) + "/behavior/narrative", {
    method: "POST",
    body: JSON.stringify({ expected_revision_id: expectedRevisionId }),
  });
}

/** Record what a human observed for one manual verification item (stored as a human observation). */
export function postManualCheckResult(
  id: string,
  expectedRevisionId: string,
  result: { item_id: string; status: "PASS" | "FAIL" | "UNKNOWN"; note: string },
): Promise<{ checklist: ReviewManualChecklist | null }> {
  return request<{ checklist: ReviewManualChecklist | null }>("/api/reviews/" + encodeURIComponent(id) + "/behavior/checklist", {
    method: "POST",
    body: JSON.stringify({ expected_revision_id: expectedRevisionId, ...result }),
  });
}

export function dismissRuleSuggestion(
  id: string,
  suggestionId: string,
  expectedRevisionId: string,
): Promise<{ policy: import("./types").ReviewPolicyProjection }> {
  return request<{ policy: import("./types").ReviewPolicyProjection }>(
    `/api/reviews/${encodeURIComponent(id)}/rule-suggestions/${encodeURIComponent(suggestionId)}/dismiss`,
    { method: "POST", body: JSON.stringify({ expected_revision_id: expectedRevisionId }) },
  );
}

/** Ask for the patch that would adopt a suggestion as a trusted-base rule. Writes nothing; changes no policy. */
export function adoptRuleSuggestion(
  id: string,
  suggestionId: string,
  expectedRevisionId: string,
): Promise<{ proposal: import("./types").ReviewRuleAdoptionProposal }> {
  return request<{ proposal: import("./types").ReviewRuleAdoptionProposal }>(
    `/api/reviews/${encodeURIComponent(id)}/rule-suggestions/${encodeURIComponent(suggestionId)}/adopt`,
    { method: "POST", body: JSON.stringify({ expected_revision_id: expectedRevisionId }) },
  );
}

const threadSummaryAvailability = new Map<string, Promise<{ available: boolean; min_messages: number }>>();

/** Whether this deployment offers thread summaries for a Review (one read per Review per page). */
export function threadSummariesAvailable(id: string): Promise<{ available: boolean; min_messages: number }> {
  let pending = threadSummaryAvailability.get(id);
  if (!pending) {
    pending = request<{ available: boolean; min_messages?: number }>(
      `/api/reviews/${encodeURIComponent(id)}/thread-summaries`,
    ).then(
      (body) => ({ available: Boolean(body.available), min_messages: body.min_messages ?? 3 }),
      () => {
        threadSummaryAvailability.delete(id);
        return { available: false, min_messages: 3 };
      },
    );
    threadSummaryAvailability.set(id, pending);
  }
  return pending;
}

/** Test seam: forget cached availability. */
export function resetThreadSummaryAvailability(): void {
  threadSummaryAvailability.clear();
}

/** A thread's stored summary and its authoritative state. A read: never runs a model. */
export function fetchThreadSummary(id: string, threadId: string): Promise<ThreadSummaryRead> {
  return request<ThreadSummaryRead>(
    `/api/reviews/${encodeURIComponent(id)}/threads/${encodeURIComponent(threadId)}/summary`,
  );
}

/** Summarize a thread now (an explicit model call, cached and metered like Ask). */
export function postThreadSummary(id: string, threadId: string): Promise<ThreadSummaryRun> {
  return request<ThreadSummaryRun>(
    `/api/reviews/${encodeURIComponent(id)}/threads/${encodeURIComponent(threadId)}/summary`,
    { method: "POST" },
  );
}

export function postAiReview(
  id: string,
  expectedRevisionId: string,
  effort: "auto" | "quick" | "thorough" | "deep",
  trigger: "manual" | "auto" = "manual",
  actionId = "",
): Promise<{ ai_review: ReviewAiReviewRunResult }> {
  return request<{ ai_review: ReviewAiReviewRunResult }>(`/api/reviews/${encodeURIComponent(id)}/ai-review`, {
    method: "POST",
    body: JSON.stringify({
      expected_revision_id: expectedRevisionId,
      effort,
      trigger,
      ...(actionId ? { action_id: actionId } : {}),
    }),
  });
}

/** Stop a current-Snapshot AI Review job; never another tenant's run or an author-agent task. */
export function cancelAiReview(
  id: string,
  expectedRevisionId: string,
  jobId: string,
): Promise<{ ai_review: ReviewAiExecutionStatus }> {
  return request<{ ai_review: ReviewAiExecutionStatus }>(
    `/api/reviews/${encodeURIComponent(id)}/ai-review/cancel`,
    { method: "POST", body: JSON.stringify({ expected_revision_id: expectedRevisionId, job_id: jobId }) },
  );
}

/**
 * Reviewer/admin fresh re-run of AI review on the unchanged current Snapshot.
 * ``actionId`` makes it idempotent: a replay never spends twice.
 */
export function postAiReviewFresh(
  id: string,
  expectedRevisionId: string,
  actionId: string,
): Promise<{ ai_review: ReviewAiReviewRunResult; fresh_rerun: { action_id: string } }> {
  return request<{ ai_review: ReviewAiReviewRunResult; fresh_rerun: { action_id: string } }>(
    `/api/reviews/${encodeURIComponent(id)}/ai-review/fresh`,
    {
      method: "POST",
      body: JSON.stringify({ expected_revision_id: expectedRevisionId, action_id: actionId }),
    },
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

export function surfaceImpactRetryable(error: unknown): boolean {
  return transientRequestFailure(error);
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

export function runRuntimeValidation(
  id: string,
  provider: string,
  surfaceId: string,
  revisionId: string,
  side: "old" | "new" = "new",
): Promise<ReviewRuntimeValidationResponse> {
  return request<ReviewRuntimeValidationResponse>(
    `/api/reviews/${encodeURIComponent(id)}/runtime-validation`,
    {
      method: "POST",
      body: JSON.stringify({
        revision_id: revisionId,
        provider,
        surface_id: surfaceId,
        side,
      }),
    },
  );
}

export function fetchRuntimeValidationJob(
  id: string,
  jobId: string,
  provider: string,
  surfaceId: string,
  revisionId: string,
  side: "old" | "new" = "new",
): Promise<ReviewRuntimeValidationResponse> {
  const params = new URLSearchParams({
    revision_id: revisionId,
    provider,
    surface_id: surfaceId,
    side,
  });
  return request<ReviewRuntimeValidationResponse>(
    `/api/reviews/${encodeURIComponent(id)}/runtime-validation/jobs/${encodeURIComponent(jobId)}?${params.toString()}`,
  );
}

export function cancelRuntimeValidationJob(
  id: string,
  jobId: string,
  provider: string,
  surfaceId: string,
  revisionId: string,
  side: "old" | "new" = "new",
): Promise<ReviewRuntimeValidationResponse> {
  return request<ReviewRuntimeValidationResponse>(
    `/api/reviews/${encodeURIComponent(id)}/runtime-validation/jobs/${encodeURIComponent(jobId)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify({
        revision_id: revisionId,
        provider,
        surface_id: surfaceId,
        side,
      }),
    },
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

export function fetchFileContents(id: string, path: string, revisionId = ""): Promise<ReviewFileContents> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  const params = new URLSearchParams();
  if (revisionId) params.set("revision_id", revisionId);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return request<ReviewFileContents>(
    `/api/reviews/${encodeURIComponent(id)}/files/${segments}/contents${query}`,
  );
}

export function fetchRevisionComparisonFileContents(
  id: string,
  path: string,
  fromRevision: number,
  toRevision: number,
): Promise<ReviewComparisonFileContents> {
  const segments = path.split("/").map(encodeURIComponent).join("/");
  const params = new URLSearchParams({
    from_revision: String(fromRevision),
    to_revision: String(toRevision),
  });
  return request<ReviewComparisonFileContents>(
    `/api/reviews/${encodeURIComponent(id)}/compare/files/${segments}/contents?${params.toString()}`,
  );
}

/** The latest Snapshot's decision, who can sign its record here, and the records already exported. */
export function fetchReviewRecordStatus(id: string): Promise<ReviewRecordStatus> {
  return request<ReviewRecordStatus>(`/api/reviews/${encodeURIComponent(id)}/record`);
}

/**
 * Export a signed review record (`auto`: signs or refuses, never silently unsigned)
 * or an explicitly UNSIGNED one. Refusals carry `code` and the current signing status.
 */
export async function exportReviewRecord(
  id: string,
  payload: { signing: "auto" | "unsigned"; expected_revision_id?: string },
): Promise<ReviewRecordExportResult> {
  if (typeof window !== "undefined") adoptBootstrapFragment();
  const response = await authenticatedFetch(`/api/reviews/${encodeURIComponent(id)}/record`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (response.ok) return (await response.json()) as ReviewRecordExportResult;
  let message = `${response.status}`;
  let code = "";
  try {
    const body = (await response.json()) as { detail?: unknown; error?: { message?: unknown } };
    if (typeof body.detail === "string") message = body.detail;
    else if (body.detail && typeof body.detail === "object") {
      const detail = body.detail as { message?: unknown; code?: unknown };
      if (typeof detail.message === "string") message = detail.message;
      if (typeof detail.code === "string") code = detail.code;
    } else if (typeof body.error?.message === "string") message = body.error.message;
  } catch {
    // Keep the HTTP status for a non-JSON refusal.
  }
  throw Object.assign(new Error(message), { status: response.status, code });
}

/** The stored DSSE envelope, exactly as exported (verifiable with `lc review verify-record`). */
export function fetchReviewRecordFile(id: string, recordId: string): Promise<Blob> {
  return fetchReviewBlob(`/api/reviews/${encodeURIComponent(id)}/records/${encodeURIComponent(recordId)}`);
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

export function fetchReviewAsk(id: string, conversationId?: string): Promise<ReviewAskConversation> {
  const query = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : "";
  return request<ReviewAskConversation>(`/api/reviews/${encodeURIComponent(id)}/ask${query}`);
}

export function createReviewAskConversation(
  id: string,
  origin: ReviewAskConversationOrigin,
): Promise<ReviewAskConversationSummary> {
  return request<ReviewAskConversationSummary>(`/api/reviews/${encodeURIComponent(id)}/ask/conversations`, {
    method: "POST",
    body: JSON.stringify({ origin }),
  });
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
  | { type: "delta"; text: string }
  | { type: "result"; result: ReviewAskResult }
  | { type: "error"; status: number; detail: string };

export async function streamReviewAsk(
  id: string,
  input: ReviewAskInput,
  onEvent: (event: ReviewAskStreamEvent) => void,
  signal?: AbortSignal,
): Promise<ReviewAskResult> {
  if (typeof window !== "undefined") adoptBootstrapFragment();
  const response = await authenticatedFetch(`/api/reviews/${encodeURIComponent(id)}/ask/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
    ...(signal ? { signal } : {}),
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

/** On an attached Review the judgment is queued for hosted instead (HTTP 202, `queued: true`). */
export function postMark(
  id: string,
  unitKey: string,
  state: MarkState,
  expectedRevisionId: string,
): Promise<MarkResult | HostedQueuedWrite> {
  return request<MarkResult | HostedQueuedWrite>(`/api/reviews/${encodeURIComponent(id)}/marks`, {
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

export function undoBulkReviewed(
  id: string,
  marks: Array<{ unit_key: string; event_id: string }>,
  expectedRevisionId: string,
): Promise<import("./types").BulkUndoResult> {
  return request<import("./types").BulkUndoResult>(`/api/reviews/${encodeURIComponent(id)}/marks/bulk/undo`, {
    method: "POST",
    body: JSON.stringify({ marks, expected_revision_id: expectedRevisionId }),
  });
}

export function subscribeSourceState(
  id: string,
  onState: (state: SourceState) => void,
  retryMs?: number,
  onTerminal?: (status: number) => void,
): () => void {
  return subscribeReviewState(`/api/reviews/${encodeURIComponent(id)}/source-state/events`, onState, authenticatedFetch, retryMs, onTerminal);
}

export function subscribeLiveState(
  id: string,
  onState: (state: ReviewLiveState) => Promise<void>,
  retryMs?: number,
  onTerminal?: (status: number) => void,
): () => void {
  return subscribeReviewState(`/api/reviews/${encodeURIComponent(id)}/live-state/events`, onState, authenticatedFetch, retryMs, onTerminal);
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

export function postRefresh(id: string, publish = true): Promise<ReviewOverview> {
  return request<ReviewOverview>(`/api/reviews/${encodeURIComponent(id)}/refresh${publish ? "" : "?publish=false"}`, { method: "POST" });
}

export function publishReviewSnapshot(id: string, revisionNumber: number): Promise<{ revision_id: string; revision_number: number }> {
  return request(`/api/reviews/${encodeURIComponent(id)}/revisions/${revisionNumber}/publish`, { method: "POST" });
}

export function listReviews(
  statusFilter: "open" | "finished" | "archived" | "all" = "open",
): Promise<{ reviews: ReviewSessionInfo[]; repo_root: string; status_filter: string }> {
  return request(`/api/reviews?status_filter=${encodeURIComponent(statusFilter)}`);
}

export function fetchRevisions(id: string): Promise<ReviewRevisionHistory> {
  return request(`/api/reviews/${encodeURIComponent(id)}/revisions`);
}

export function fetchFixLines(id: string): Promise<FixLineList> {
  return request(`/api/reviews/${encodeURIComponent(id)}/fix-lines`);
}

export function startFixLine(id: string, revisionId: string): Promise<{ fix_line: FixLine; created: boolean }> {
  return request(
    `/api/reviews/${encodeURIComponent(id)}/snapshots/${encodeURIComponent(revisionId)}/fix-line`,
    { method: "POST", body: "{}" },
  );
}

export function planFixApply(id: string, fixId: string): Promise<FixApplyPlan> {
  return request(
    `/api/reviews/${encodeURIComponent(id)}/fix-lines/${encodeURIComponent(fixId)}/apply-plan`,
    { method: "POST", body: "{}" },
  );
}

export function applyFixLine(id: string, fixId: string, withConflictMarkers = false): Promise<FixLineApplyResult> {
  return request(
    `/api/reviews/${encodeURIComponent(id)}/fix-lines/${encodeURIComponent(fixId)}/apply`,
    { method: "POST", body: JSON.stringify({ with_conflict_markers: withConflictMarkers }) },
  );
}

export function discardFixLine(id: string, fixId: string, force = false): Promise<FixLineCloseResult> {
  return request(
    `/api/reviews/${encodeURIComponent(id)}/fix-lines/${encodeURIComponent(fixId)}/discard`,
    { method: "POST", body: JSON.stringify({ force }) },
  );
}

export function resolveFixLine(id: string, fixId: string, force = false): Promise<FixLineCloseResult> {
  return request(
    `/api/reviews/${encodeURIComponent(id)}/fix-lines/${encodeURIComponent(fixId)}/resolved`,
    { method: "POST", body: JSON.stringify({ force }) },
  );
}

export function recreateFixWorktree(id: string, fixId: string): Promise<{ fix_line: FixLine }> {
  return request(
    `/api/reviews/${encodeURIComponent(id)}/fix-lines/${encodeURIComponent(fixId)}/recreate`,
    { method: "POST", body: "{}" },
  );
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

/** Waive one blocking pre-merge check on this Snapshot; permission is decided by trusted policy on the server. */
export function waiveReviewPremergeCheck(
  id: string,
  payload: { expected_revision_id: string; check_id: string; reason: string; scope: "snapshot" | "review"; hours?: number },
): Promise<{ evidence_id: string; premerge: ReviewPremergeProjection }> {
  return request(`/api/reviews/${encodeURIComponent(id)}/premerge/waivers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** Bind checks to a linked source's newer text; explicit and audited, never automatic. */
export function rebindReviewIntent(
  id: string,
  payload: { expected_revision_id: string; ref: string },
): Promise<{ premerge: ReviewPremergeProjection }> {
  return request(`/api/reviews/${encodeURIComponent(id)}/intent/rebind`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** Run model-evaluated pre-merge checks within the Review budget (AI off or over budget stays NOT_RUN). */
export function evaluateReviewPremergeChecks(
  id: string,
  payload: { expected_revision_id: string; checks?: string[] },
): Promise<{ evaluated: { check: string; evidence_id: string }[]; premerge: ReviewPremergeProjection }> {
  return request(`/api/reviews/${encodeURIComponent(id)}/premerge/evaluate`, {
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
  revisionId = "",
): Promise<ReviewEvidenceResult> {
  const params = new URLSearchParams({
    kind: evidenceKind(file),
    title: file.name,
    filename: file.name,
    mime_type: file.type || "application/octet-stream",
  });
  if (path) params.set("path", path);
  if (expectedRevisionId) params.set("expected_revision_id", expectedRevisionId);
  if (revisionId) params.set("revision_id", revisionId);
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
): Promise<AnnotationResult | HostedQueuedWrite> {
  return request<AnnotationResult | HostedQueuedWrite>(`/api/reviews/${encodeURIComponent(id)}/annotations`, {
    method: "POST",
    body: JSON.stringify({ ...draft, expected_revision_id: expectedRevisionId }),
  });
}

/** Edit a comment's text, disposition or state. Never its position. */
export function patchAnnotation(
  annotationId: string,
  fields: {
    body?: string;
    kind?: AnnotationKind;
    state?: AnnotationState;
    finding_disposition?: import("./types").FindingDisposition;
    finding_disposition_reason?: import("./types").FindingDismissalReason;
    finding_disposition_evidence?: string[];
  },
  expectedRevisionId = "",
): Promise<AnnotationResult> {
  const needsSnapshotCas = Boolean(fields.state || fields.finding_disposition);
  return request<AnnotationResult>(`/api/annotations/${encodeURIComponent(annotationId)}`, {
    method: "PATCH",
    body: JSON.stringify(
      needsSnapshotCas ? { ...fields, expected_revision_id: expectedRevisionId } : fields,
    ),
  });
}

/** Render the review's comments as Markdown. Renders; delivers nothing. */
export function exportFeedback(id: string): Promise<FeedbackExport> {
  return request<FeedbackExport>(`/api/reviews/${encodeURIComponent(id)}/feedback/export`, { method: "POST" });
}

/**
 * Deliver exactly the prepared feedback preview to its destination. A copy for
 * the author is recorded only with `copied`, once the reviewer has copied it.
 */
export function deliverFeedback(
  id: string,
  feedback: FeedbackExport,
  options: { copied?: boolean } = {},
): Promise<FeedbackDelivery> {
  return request<FeedbackDelivery>(`/api/reviews/${encodeURIComponent(id)}/feedback/deliver`, {
    method: "POST",
    body: JSON.stringify({
      operation_id: feedback.operation_id,
      expected_revision_id: feedback.revision_id,
      expected_feedback_hash: feedback.feedback_hash,
      ...(options.copied ? { copied: true } : {}),
    }),
  });
}

/** Every destination the reviewer can pick, with why an unavailable one is not. */
export function fetchReviewDestinationOptions(id: string): Promise<ReviewDestinationOptions> {
  return request<ReviewDestinationOptions>(`/api/reviews/${encodeURIComponent(id)}/destination/options`);
}

/** Pin where this Review's feedback goes; `auto` returns to the author sessions. */
export function putReviewDestination(id: string, choice: ReviewDestinationChoice): Promise<ReviewDestinationState> {
  return request<ReviewDestinationState>(`/api/reviews/${encodeURIComponent(id)}/destination`, {
    method: "PUT",
    body: JSON.stringify(choice),
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
): Promise<ReviewOutcomeProjection | HostedQueuedWrite> {
  return request<ReviewOutcomeProjection | HostedQueuedWrite>(`/api/reviews/${encodeURIComponent(id)}/outcome`, {
    method: "POST",
    body: JSON.stringify({ outcome, summary, expected_revision_id: expectedRevisionId }),
  });
}

/** The attachment, its queue and the counts by state; `attached: false` once detached. */
export function fetchHostedAttachment(id: string): Promise<HostedAttachmentStatus> {
  return request<HostedAttachmentStatus>(`/api/reviews/${encodeURIComponent(id)}/hosted`);
}

/** The reviewer's explicit resolution of one queued action: drop it, or re-apply a conflict. */
export function resolveHostedAction(
  id: string,
  actionId: string,
  op: "discard" | "requeue",
): Promise<HostedAttachmentStatus & { action: HostedAction }> {
  return request<HostedAttachmentStatus & { action: HostedAction }>(
    `/api/reviews/${encodeURIComponent(id)}/hosted/actions/${encodeURIComponent(actionId)}`,
    { method: "POST", body: JSON.stringify({ op }) },
  );
}

/** Explicit detach; unresolved actions are refused unless the reviewer discards them. */
export function detachHostedAttachment(id: string, discardUnresolved: boolean): Promise<HostedDetachResult> {
  const query = discardUnresolved ? "?discard_unresolved=true" : "";
  return request<HostedDetachResult>(`/api/reviews/${encodeURIComponent(id)}/hosted${query}`, { method: "DELETE" });
}

export function fetchFinishReview(id: string): Promise<ReviewClosure> {
  return request<ReviewClosure>(`/api/reviews/${encodeURIComponent(id)}/finish`);
}

/** Record the human's status choice; this call never infers an approval verdict. */
export function finishReview(
  id: string,
  status: "open" | "finished" | "archived" = "finished",
  expectedRevisionId = "",
  /** A discard that names pending feedback / an armed landing must acknowledge each one. */
  confirmation?: { acknowledged_hazards: string[]; hazard_digest: string },
): Promise<ReviewClosure> {
  return request<ReviewClosure>(`/api/reviews/${encodeURIComponent(id)}/finish`, {
    method: "POST",
    body: JSON.stringify({ status, expected_revision_id: expectedRevisionId, ...(confirmation ?? {}) }),
  });
}
