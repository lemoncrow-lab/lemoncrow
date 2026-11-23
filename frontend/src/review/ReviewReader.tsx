import { Button as UiButton } from "../components/ui/button";
import { focusReturnTarget } from "../lib/focus";
import { classifyWriteFailure, isStaleSnapshotError } from "./staleSnapshot";
import { readReviewPage, reviewPages } from "./reviewPagination";
import { retainTargetsOnSameRevision } from "./reviewTargetHydration";
import { ReviewStoreProvider, useReviewStoreDispatch, useReviewStoreState } from "./reviewStoreContext";
import { applyStableOrder } from "./reviewStore";
import { PrincipalNamesContext, ViewerContext, type PrincipalNames } from "./plainWords";
import { lazyPanel } from "./lazyPanel";
import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type SetStateAction } from "react";
import { useNavigate, useParams } from "react-router-dom";
import "./reviewUi.css";

import type { ContextDrawerTab } from "./ContextDrawer";
import { revealBriefDecision } from "./investigate/revealDecision";
import type { BehaviorJudgmentState, ManualCheckStatus } from "./brief/BehaviorChanges";
import type { ReviewAskSelection } from "./ReviewAskDrawer";
import { codeHref, currentReturnPath, RETURN_PARAM, type CodeTarget } from "../lib/codeLinks";
import { PreparationProgress } from "./ReviewPreparation";
import { useReviewPreparation, type PreparationStep } from "./reviewPreparationContext";
import { useHostedQueue } from "./ReviewHostedQueue";
import { isQueuedWrite, queuedWriteNotice, unresolvedHostedActions } from "./hostedQueue";
import { feedbackRoute } from "./ReviewFeedbackPanel";
import { ReviewCorrectionsProvider, useReviewCorrections } from "./reviewCorrections";
import { FeedbackSyncProvider, useFeedbackSync } from "./feedbackSync";
import { ReviewDestinationPicker } from "./ReviewDestinationPicker";
import type { FinishOptions } from "./FinishSheet";
import { finishRecordFacts } from "./finishRecord";
import { downloadDecisionRecord } from "./ReviewRecordExport";
import { deriveReviewReadiness, scmRequirementBlockers, type ReviewReadinessBlocker } from "./ReviewReadinessSheet";
import type { Theme } from "../lib/theme";
import { applyReviewCodeTheme, getInitialReviewCodeTheme, reviewCodeThemeDefinition } from "./reviewCodeTheme";
import { type ReviewCompareBase } from "./RevisionCompareBar";
import type { ReviewStreamHandle } from "./ReviewStream";
import {
  backtrailQuery,
  type ReviewView,
  pushReviewBacktrail,
  readReviewBacktrail,
  REVIEW_BACKTRAIL_PARAM,
  writeReviewBacktrail,
  type ReviewBacktrailEntry,
} from "./reviewBacktrail";
import { useResolvedProductCapabilities } from "../lib/RuntimeCapabilities";
import { flowDocumentTitle } from "./flowDocumentTitle";
import { onFlowScreen, keepFlowScreen, parseReviewFlowLocation, readerSearchHref } from "./reviewRoutes";
import { ReviewOverviewScreen } from "./overview/ReviewOverviewScreen";
import { ReviewFocusScreen } from "./focus/ReviewFocusScreen";
import type { LineComment } from "./focus/FocusCode";
import { CodeViewProvider, useCodeView } from "./codeView";
import { ReviewAllFilesScreen } from "./allFiles/ReviewAllFilesScreen";

import { SeenCoverageProvider } from "./seen/SeenCoverageProvider";
import { ReviewQuickCheckScreen } from "./quickCheck/ReviewQuickCheckScreen";
import type { QuickConfirmActions } from "./quickCheck/ConfirmAllQuickChecks";
import { rounds, type QuickConfirmResult } from "./quickCheck/confirmAllModel";
import { ReviewWrapScreen } from "./wrap/ReviewWrapScreen";
import { ReviewDoneScreen } from "./done/ReviewDoneScreen";
import { SecondSignatureScreen } from "./second/SecondSignatureScreen";
import { TeamNoticeAnchor, useReviewPresence } from "./team/useReviewPresence";
import { FlowToastHost } from "./flowToast";
import { ReviewWelcomeBackScreen } from "./back/ReviewWelcomeBackScreen";
import { targetAnswer } from "./answerModel";
import { partSlug } from "./overview/overviewModel";
import { overviewReady } from "./overview/overviewModel";
import { DEDICATED_REVIEW_ITEM_PARAMS, REVIEW_RETURN_PARAMS, readDedicatedReviewItem, readReviewReturnPath, readReviewReturnSurface } from "./reviewItemNavigation";
import { loadCommentDraft, saveCommentDraft } from "./reviewDraftPersistence";
import { isCompactReviewViewport, isNarrowReviewViewport, useCompactReviewLayout, useNarrowReviewLayout } from "./reviewLayout";
import { ReviewFrictionProbe, ReviewFrictionRecorder, activeReviewFriction, setActiveReviewFriction } from "./reviewFriction";
import { countMapOpen, countWaiting } from "./reviewFrictionCounts";
import {
  REVIEW_MAP_OVERVIEW,
  REVIEW_MAP_PARAMS,
  readReviewMapLocation,
  reviewMapLocationFromSelection,
  writeReviewMapLocation,
  type ReviewMapLocation,
} from "./reviewMapLocation";
import { withTransientRetry } from "./transientRetry";
import { useReviewEnvironment, type ReviewDeploymentMode } from "./reviewEnvironment";
import type { Draft } from "./annotationModel";
import { markBelowThreshold } from "./findingThreshold";
import { bestTargetAfterRevision, firstActionable, filterTargets, orderedPaths, progressFromTargets, stepTarget, targetScrollLocation } from "./readerModel";
import { shouldApplyProjection } from "./projectionRequests";
import { requestScopeTargetIds } from "./requestScope";
import { adaptiveReviewLanding, guidedReviewUseful, isTinyMechanicalReview, LARGE_REVIEW_TARGET_THRESHOLD, projectionSearchTextByTarget } from "./reviewSurfaceModel";
import { handoffCounts, handoffDestinationLabel, handoffToCodingAgent } from "./reviewPrimaryAction";
import { adoptBootstrapFragment, fetchBehaviorTarget, postBehaviorJudgment, postBehaviorNarrative, postManualCheckResult, postDecisionEvent, deliverFeedback, exportFeedback, queueReviewFlowSend, fetchAnnotations, fetchFile, fetchHistoricalAnnotations, fetchHistoricalFile, fetchHistoricalOverview, fetchHistoricalProjection, fetchHistoricalRelatedSource, fetchHistoricalTargets, fetchOverview, fetchStartupOverview, fetchStartupTargets, fetchProjection, fetchBriefPreparation, type BriefPreparationProgress, fetchChangeProposals, fetchRelatedSource, fetchReviewEvidence, fetchReviewJournal, fetchReviewReadiness, subscribeLiveState, fetchHistoricalReviewJournal, fetchHistoricalReviewDevelopment, fetchReviewDevelopment, fetchReviewOutcome, fetchReviewPreparation, fetchRevisionLocator, fetchRevisionComparison, fetchRevisions, fetchReviewSurfaces, fetchSourceComparison, subscribeSourceState, fetchTargets, fetchTargetsForPath, finishReview, linkReviewEvidence, patchAnnotation, postAnnotation, postBulkReviewed, postMark, postRefresh, postReviewOutcome, postTourSynthesis, historicalReviewHref, reviewCompareHref, reviewHref, selectReviewId, undoBulkReviewed, uploadReviewEvidence, fetchHistoricalReviewMap, fetchReviewDependencies } from "./reviewApi";
import { isLocalBrowserUnpaired } from "./localBrowser";
import type { ReviewDecisionEventInput, Annotation, AnnotationDraft, AnnotationState, FeedbackDelivery, FeedbackDeliveryCapability, FeedbackExport, FeedbackRouteKind, FeedbackStatus, FileDetail, MarkState, RelatedSource, ReviewChangeProposal, ReviewClosure, ReviewEvidence, ReviewJournal, ReviewDevelopment, ReviewLandingReadiness, ReviewProjection, ReviewMapSelection, ReviewOutcome, ReviewOutcomeKind, ReviewOverview, ReviewRequestScope, ReviewAskReference, ReviewArchitectureDelta, ReviewDependencyProjection, ReviewRevisionComparison, ReviewRevisionHistory, ReviewSurfaceInfo, ReviewSurfacePluginIssue, ReviewRuntimeValidationResponse, ReviewTarget, ReviewTargetList, SourceComparison, SourceState } from "./types";
import { revisionName } from "./revisionLabel";
const ContextDrawer = lazyPanel("Context", () => import("./ContextDrawer"));
const ReviewAskDrawer = lazyPanel("Ask", () => import("./ReviewAskDrawer"));
const GitHubPublishSheet = lazyPanel("GitHub publish", () => import("./GitHubPublishSheet"));

const READER_VIEW_KEY = "lc.review.view.";
const SYNTHESIS_POLL_MS = 2500;

/** The reviewer's last explicit persistent surface choice for one Review. */
type StoredView = "brief" | "implementation";

function readStoredView(reviewId: string): StoredView | null {
  try {
    const value = window.localStorage.getItem(READER_VIEW_KEY + reviewId);
    if (value === "tour") return "brief";
    return value === "brief" || value === "implementation" ? value : null;
  } catch {
    return null;
  }
}

function storeView(reviewId: string, view: StoredView): void {
  try {
    window.localStorage.setItem(READER_VIEW_KEY + reviewId, view);
  } catch {
    // Storage can be blocked; the choice then lasts for this page only.
  }
}
/** Landing condition states that do not block; mirrors the server's SATISFIED_CONDITION_STATES. */
const SATISFIED_LANDING_STATES: ReadonlySet<string> = new Set(["pass", "not_applicable", "waived"]);
const ReviewFlowMapScreen = lazyPanel("Review map", () => import("./mapFlow/ReviewFlowMapScreen"));
const ReviewFlowPreviewScreen = lazyPanel("Review preview", () => import("./previewFlow/ReviewFlowPreviewScreen"));
const ReviewTourScreen = lazyPanel("Walkthrough", () => import("./ReviewTourScreen"));
const ReviewBriefScreen = lazyPanel("Brief", () => import("./brief/ReviewBriefScreen"));
const ReviewXRayScreen = lazyPanel("X-Ray", () => import("./xray/ReviewXRayScreen"));
/** URL params for the Brief/Guided mode; the Map is named by its own location params. */
const REVIEW_VIEW_PARAM = "view";
const GUIDED_COHORT_PARAM = "guided";

const INITIAL_CHANGE_BUDGET = 400;
const INITIAL_MAX_FILES = 5;
const LOAD_MORE_CHANGE_BUDGET = 800;
const PREFETCH_AHEAD_FILES = 2;
const BACKGROUND_PREFETCH_FILES = 10;
const CONTEXT_PIN_KEY = "lemoncrow.review.reader.contextPinned";

/**
 * An inline comment written on a frozen line or range keeps exactly that anchor. It joins (and may mark) the part
 * only when the whole range lies inside one of the part's spans on the same side; whole-function context lines
 * outside them stay plain line comments rather than being moved onto the part's range.
 */
function lineCommentDraft(target: ReviewTarget, body: string, line: LineComment, reconsidered: boolean): AnnotationDraft {
  const inside = target.spans.some((span) => span.start_line <= line.start && line.end <= span.end_line
    && (span.side === "old" ? line.side === "deletions" : line.side === "additions"));
  const linked = inside && !line.parentId && target.fingerprint_method !== "revision_compare";
  return {
    path: target.path,
    start_line: line.start,
    end_line: line.end,
    side: line.side,
    body,
    kind: line.kind,
    ...(line.parentId ? { parent_id: line.parentId } : {}),
    target_unit_key: linked ? target.unit_key : undefined,
    mark_target: linked && line.kind === "request_change" && line.markTarget,
    ...(reconsidered ? { reconsidered_after_structure: true } : {}),
  };
}

function queryRevisionNumber(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

function comparisonTarget(
  file: ReviewRevisionComparison["code_files"][number] | SourceComparison["files"][number],
  baseRevision: ReviewCompareBase,
  targetRevision: number,
): ReviewTarget {
  const additions = "added_lines" in file ? file.added_lines : file.additions;
  const deletions = "removed_lines" in file ? file.removed_lines : file.deletions;
  return {
    target_id: `revision-compare:${file.path}`,
    unit_key: `revision-compare:${file.path}`,
    kind: "file",
    path: file.path,
    label: file.path,
    symbol: "",
    start_line: 0,
    end_line: 0,
    hunk_ordinals: [],
    spans: [],
    state: "unknown",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: [
      baseRevision === "head"
        ? `Changed between Source and ${revisionName(targetRevision)}`
        : `Changed between ${revisionName(baseRevision)} and ${revisionName(targetRevision)}`,
    ],
    additions,
    deletions,
    fingerprint_method: "revision_compare",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

function comparisonDetail(
  file: ReviewRevisionComparison["code_files"][number] | SourceComparison["files"][number],
): FileDetail {
  if ("additions" in file) {
    return {
      path: file.path,
      old_path: file.old_path || file.path,
      status: file.status,
      additions: file.additions,
      deletions: file.deletions,
      patch: file.patch,
      renderable: file.renderable,
      refusal: file.refusal,
      detail: file.detail,
      degraded: [],
    };
  }
  return {
    path: file.path,
    old_path: file.path,
    status: file.status,
    additions: file.added_lines,
    deletions: file.removed_lines,
    patch: file.patch,
    renderable: true,
    refusal: "",
    detail: "",
    degraded: [],
  };
}

const SOURCE_PROBE_INTERVAL_MS = 2000;
const PREPARATION_POLL_MS = 180;
const EMPTY_FEEDBACK_STATUS: FeedbackStatus = {
  open_total: 0,
  unpublished: 0,
  published: 0,
  in_flight: 0,
  addressed: 0,
};

interface ActiveFeedbackHandoff {
  revisionId: string;
  operationId: string;
  annotationIds: string[];
  destination: string;
}

/** The fields of a delivery route the Reader shows and sends by; a fresh copy with the same ones changes nothing. */
function sameDeliveryRoute(current: FeedbackDeliveryCapability | null, next: FeedbackDeliveryCapability): boolean {
  return current !== null && current.kind === next.kind && current.supported === next.supported && current.host === next.host
    && current.session_id === next.session_id && current.target_ref === next.target_ref && current.label === next.label
    && current.reason === next.reason;
}

function fallbackFeedbackStatus(annotations: readonly Annotation[]): FeedbackStatus {
  const roots = annotations.filter(
    (item) => item.source === "human" && !item.parent_id && item.state !== "resolved" && item.state !== "obsolete",
  );
  const addressed = roots.filter((item) => item.author_response === "addressed").length;
  return {
    open_total: roots.length,
    unpublished: roots.length - addressed,
    published: 0,
    in_flight: 0,
    addressed,
  };
}

function preparationLabel(stage: string): string {
  return {
    diff: "Reading changed files",
    source: "Capturing source revision",
    impact: "Analyzing change impact",
    provenance: "Reading agent provenance",
    ranking: "Prioritizing review",
    persisting: "Preparing first diff",
    impact_background: "Source ready · analyzing impact in background",
    finalizing_background: "Source ready · applying review context",
    enrichment_failed: "Source ready · impact enrichment unavailable",
    connecting: "Connecting to review preparation",
  }[stage] ?? "Preparing review";
}

type RelatedPanel = RelatedSource | { path: string; line: number; pending: true };

/** The element whose scroll offset is a view's place: the Code stream, or the document (flow screens scroll the window). */
function reviewScrollRoot(view: ReviewView): Element | null {
  if (typeof document === "undefined" || view === "map") return null;
  return view === "implementation" ? document.querySelector('[data-testid="review-stream"]') : document.scrollingElement;
}

/** Architecture deltas kept per Reader; one per Snapshot is enough. */
const ARCHITECTURE_DELTA_CACHE_LIMIT = 4;

// The primary button stays put while its action waits on the server, so it
// names the wait itself; otherwise a slow reply reads as a dead click.
/** Delivery states in which the feedback left for its destination. */
const HANDED_OFF_STATES = new Set(["sent", "consumed", "queued", "working"]);

async function copyToClipboard(text: string): Promise<void> {
  if (!navigator.clipboard?.writeText) throw new Error("the clipboard is unavailable in this browser");
  await navigator.clipboard.writeText(text);
}

/**
 * A clipboard write started inside the reviewer's click and filled once the
 * text exists. Safari and Firefox refuse a write issued after the verdict and
 * bundle round trips; a pending ClipboardItem keeps the click's permission.
 * Null where the browser has no ClipboardItem (the plain write is used then).
 */
function clipboardWriteInGesture(): { written: Promise<void>; fill(text: string | null): void } | null {
  if (typeof ClipboardItem === "undefined" || !navigator.clipboard?.write) return null;
  let fill: (text: string | null) => void = () => undefined;
  const blob = new Promise<Blob>((resolve, reject) => {
    fill = (text) => text === null
      ? reject(new Error("nothing to copy"))
      : resolve(new Blob([text], { type: "text/plain" }));
  });
  const written = navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
  // Observed by the caller when it copies; an abandoned write must not surface.
  written.catch(() => undefined);
  return { written, fill };
}

function frozenPatchDetail(detail: FileDetail): FileDetail {
  // Only the patch-local fields are safe to carry across revisions. Symbols,
  // provenance, verification and rich previews can change because another file
  // moved even when this file's exact bytes did not. Keep the rendered diff hot
  // while those revision-scoped fields refresh lazily.
  return {
    path: detail.path,
    old_path: detail.old_path,
    status: detail.status,
    language: detail.language,
    additions: detail.additions,
    deletions: detail.deletions,
    patch: detail.patch,
    renderable: detail.renderable,
    refusal: detail.refusal,
    detail: detail.detail,
    degraded: [],
  };
}

function initialPaths(targets: ReviewTarget[]): string[] {
  const paths = orderedPaths(targets);
  return pathsForChangeBudget(
    paths,
    changeLinesByPath(targets),
    INITIAL_CHANGE_BUDGET,
    INITIAL_MAX_FILES,
  );
}

function surfacePlacementPaths(surfaces: readonly ReviewSurfaceInfo[], reviewPaths: ReadonlySet<string>): string[] {
  const paths: string[] = [];
  for (const surface of surfaces) {
    const placement = surface.affected_paths.find((path) => reviewPaths.has(path));
    if (placement && !paths.includes(placement)) paths.push(placement);
  }
  return paths;
}

/**
 * Pick a meaningful next review chunk by changed lines, not file count.
 * Review targets are non-overlapping within a revision, so summing their
 * additions/deletions gives a stable per-file approximation of rendered work.
 * The file cap prevents a corpus of tiny one-line files from mounting hundreds
 * of diff surfaces in one click.
 */
function changeLinesByPath(targets: readonly ReviewTarget[]): ReadonlyMap<string, number> {
  const weights = new Map<string, number>();
  for (const target of targets) {
    const changed = Math.max(0, target.additions) + Math.max(0, target.deletions);
    weights.set(target.path, (weights.get(target.path) ?? 0) + changed);
  }
  return weights;
}

function pathsForChangeBudget(
  paths: readonly string[],
  weights: ReadonlyMap<string, number>,
  budget: number,
  maxFiles: number,
): string[] {
  const selected: string[] = [];
  let changedLines = 0;
  for (const path of paths) {
    if (selected.length >= maxFiles) break;
    selected.push(path);
    changedLines += Math.max(1, weights.get(path) ?? 1);
    if (changedLines >= budget) break;
  }
  return selected;
}

function targetStillNeedsAttention(target: ReviewTarget): boolean {
  return (
    target.state !== "reviewed"
    || target.annotation_counts.open > 0
    || target.annotation_counts.orphaned > 0
    || target.annotation_counts.addressed_needs_rereview > 0
    || target.verification.fail > 0
    || target.verification.unknown > 0
  );
}

function nextActionableTargetInFile(
  targets: readonly ReviewTarget[],
  current: ReviewTarget,
): ReviewTarget | null {
  const currentLocation = targetScrollLocation(current);
  const currentLine = currentLocation?.lineNumber ?? current.start_line;
  const currentIndex = targets.findIndex((item) => item.target_id === current.target_id);
  const candidates = targets
    .map((target, index) => ({ target, index, location: targetScrollLocation(target) }))
    .filter(({ target }) => (
      target.target_id !== current.target_id
      && target.path === current.path
      && targetStillNeedsAttention(target)
    ))
    .sort((left, right) => {
      const leftLine = left.location?.lineNumber ?? left.target.start_line;
      const rightLine = right.location?.lineNumber ?? right.target.start_line;
      return leftLine - rightLine || left.index - right.index;
    });
  return candidates.find(({ target, index, location }) => {
    const line = location?.lineNumber ?? target.start_line;
    return line > currentLine || (line === currentLine && index > currentIndex);
  })?.target ?? null;
}

function targetCommentRange(target: ReviewTarget): {
  startLine: number;
  endLine: number;
  side: Draft["side"];
} | null {
  const location = targetScrollLocation(target);
  if (!location) return null;
  const side: Draft["side"] = location.side;
  const spanSide = side === "deletions" ? "old" : "new";

  // Definition/section targets are deliberately block-level judgments. A
  // comment started from their header should therefore describe the same block,
  // not collapse to the first changed line used only as a scroll anchor.
  if (target.kind === "symbol" && target.start_line > 0) {
    return {
      startLine: target.start_line,
      endLine: Math.max(target.start_line, target.end_line),
      side,
    };
  }

  // Hunk fallback targets can contain disjoint regions. Keep one contiguous
  // anchor instead of claiming that untouched code between two regions was
  // selected.
  const sideSpans = target.spans.filter((span) => span.side === spanSide);
  const span = sideSpans.find((item) => (
    item.start_line <= location.lineNumber && location.lineNumber <= item.end_line
  )) ?? sideSpans[0];
  if (span) return { startLine: span.start_line, endLine: span.end_line, side };
  if (target.start_line > 0) {
    return {
      startLine: target.start_line,
      endLine: Math.max(target.start_line, target.end_line),
      side,
    };
  }
  return null;
}

/** One loss of review context: what the reviewer cannot see, and the technical cause for hover. */
interface ContextFailure {
  text: string;
  cause: string;
}

function contextFailure(text: string, reason: unknown): ContextFailure {
  return { text, cause: String(reason instanceof Error ? reason.message : reason) };
}

/** One reviewer-facing line naming every preview/surface plugin that failed. */
function surfacePluginIssueMessage(issues: readonly ReviewSurfacePluginIssue[] | undefined): ContextFailure | undefined {
  if (!issues?.length) return undefined;
  return {
    text: `${issues.length === 1 ? "A preview plugin" : `${issues.length} preview plugins`} couldn't load. The code review is unaffected.`,
    cause: issues.map((issue) => `${issue.kind} ${issue.plugin}: ${issue.reason}`).join("; "),
  };
}

function annotationSearchTextByTarget(
  targets: readonly ReviewTarget[],
  annotations: readonly Annotation[],
): ReadonlyMap<string, string> {
  const byUnit = new Map(targets.map((target) => [target.unit_key, target]));
  const byPath = new Map<string, ReviewTarget[]>();
  for (const target of targets) {
    const rows = byPath.get(target.path) ?? [];
    rows.push(target);
    byPath.set(target.path, rows);
  }

  const textByTarget = new Map<string, string>();
  for (const annotation of annotations) {
    const text = [annotation.title ?? "", annotation.body].join(" ").trim();
    if (!text) continue;
    let target = byUnit.get(annotation.unit_key);
    if (!target) {
      const rows = byPath.get(annotation.path) ?? [];
      const side = annotation.side === "old" || annotation.side === "new" ? annotation.side : "";
      target = rows.find((candidate) =>
        annotation.start_line > 0
        && side
        && candidate.spans.some((span) =>
          span.side === side
          && span.start_line <= annotation.start_line
          && annotation.start_line <= span.end_line,
        ),
      ) ?? rows.find((candidate) => candidate.state !== "reviewed") ?? rows[0];
    }
    if (!target) continue;
    textByTarget.set(target.target_id, `${textByTarget.get(target.target_id) ?? ""} ${text}`.trim());
  }
  return textByTarget;
}

export interface ReviewReaderExtensionContext {
  reviewId: string;
  overview: ReviewOverview;
  annotations: Annotation[];
  readOnly: boolean;
  historical: boolean;
}

export interface ReviewReaderExtensions {
  environmentFallbackMode?: ReviewDeploymentMode;
  forceReadOnly?: boolean;
  readOnlyReason?: string;
  outcome?: ReviewOutcome | null;
  /**
   * Record the verdict for the Snapshot on screen. A stale `expectedRevisionId`
   * rejects with the 409 "review advanced" error `isStaleSnapshotError` matches.
   */
  recordOutcome?: (outcome: ReviewOutcomeKind, summary: string, expectedRevisionId: string) => Promise<ReviewOutcome>;
  authorship?: { prefix: string; label: string };
  /** Stored display names by principal id (people and agents); anyone missing reads as today, from the id. */
  principalNames?: PrincipalNames;
  context?: { label: string; title?: string };
  readinessBlockers?: readonly ReviewReadinessBlocker[];
  /** A readiness panel drawn on Wrap up above its footer (the hosted policy and progress). */
  readinessSections?: ReactNode;
  /** The reviewer's scoped request: its targets are the default queue and Brief. */
  requestScope?: ReviewRequestScope | null;
  renderHeaderActions?: (context: ReviewReaderExtensionContext) => ReactNode;
  renderOverlays?: (context: ReviewReaderExtensionContext) => ReactNode;
}

const NO_PRINCIPAL_NAMES: PrincipalNames = {};

export interface ReviewReaderProps {
  /** Stream reconnect delay; tests may override the shipped value. */
  sourceProbeIntervalMs?: number;
  /** Neutral extension points used by non-local compositions. */
  extensions?: ReviewReaderExtensions;
}

/** HTTP status carried by a failed review API request (set by reviewApi request), or 0 for network/other errors. */
function reviewErrorStatus(reason: unknown): number {
  const status = (reason as { status?: unknown } | null)?.status;
  return typeof status === "number" ? status : 0;
}

/** What a live stream's terminal status means for the open Review. */
function streamAccessLoss(status: number): "forbidden" | "gone" {
  return status === 404 || status === 410 ? "gone" : "forbidden";
}

/** A load refusal no retry can fix: the review is gone, or this account may not open it. */
function missingReviewState(reason: unknown): "" | "not_found" | "no_access" | "unpaired" {
  const status = reviewErrorStatus(reason);
  if (status === 404) return "not_found";
  // A browser the terminal never paired is not an account that lost access: no administrator is involved.
  if (isLocalBrowserUnpaired(reason)) return "unpaired";
  // 401 here is what remains after the request's own sign-in recovery.
  if (status === 403 || status === 401) return "no_access";
  return "";
}

function ReviewReaderBody({ sourceProbeIntervalMs = SOURCE_PROBE_INTERVAL_MS, extensions }: ReviewReaderProps = {}) {
  const navigateTo = useNavigate();
  // Kept in a ref: `useNavigate` returns a new function on every location change, and the address effect must not re-run for that.
  const navigateRef = useRef(navigateTo);
  navigateRef.current = navigateTo;
  const reviewStore = useReviewStoreState();
  const reviewDispatch = useReviewStoreDispatch();
  const reviewEnvironment = useReviewEnvironment(extensions?.environmentFallbackMode ?? "local");
  // Settled manifest only: null while product discovery is pending.
  const productCapabilities = useResolvedProductCapabilities();
  const {
    reviewId: routeReviewId = "",
    reviewRef: routeReviewRef = "",
    revisionNumber: routeRevisionNumber = "",
    revisionRef: routeRevisionRef = "",
  } = useParams<{
    reviewId: string;
    reviewRef: string;
    revisionNumber: string;
    revisionRef: string;
  }>();
  const [locationSearch, setLocationSearch] = useState(() =>
    typeof window === "undefined" ? "" : window.location.search,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const sync = () => setLocationSearch(window.location.search);
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const searchParams = useMemo(() => new URLSearchParams(locationSearch), [locationSearch]);
  const setSearchParams = useCallback((
    update: (current: URLSearchParams) => URLSearchParams,
    options: { replace?: boolean } = {},
  ) => {
    if (typeof window === "undefined") return;
    const next = update(new URLSearchParams(window.location.search));
    const query = next.toString();
    const href = readerSearchHref(window.location, query, onFlowScreen());
    if (options.replace) window.history.replaceState(null, "", href);
    else window.history.pushState(null, "", href);
    setLocationSearch(window.location.search);
  }, []);
  const requestedPage = readReviewPage(searchParams.get("p"));
  const requestedPageRef = useRef(requestedPage);
  requestedPageRef.current = requestedPage;
  const baseRevisionParam = searchParams.get("base");
  const targetRevisionParam = searchParams.get("target");
  const requestedHeadBase = baseRevisionParam === "head";
  const requestedBaseRevision = queryRevisionNumber(baseRevisionParam);
  const requestedTargetRevision = queryRevisionNumber(targetRevisionParam);
  const comparisonQueryPresent = baseRevisionParam !== null || targetRevisionParam !== null;
  const comparisonQueryValid = baseRevisionParam !== null
    && targetRevisionParam !== null
    && (requestedHeadBase || requestedBaseRevision !== null)
    && requestedTargetRevision !== null;
  const malformedComparisonQuery = comparisonQueryPresent && !comparisonQueryValid;
  const fullReviewRequested = searchParams.get("compare") === "full";
  const automaticComparison = searchParams.get("compare") === "auto";
  const comparisonSelectionPinnedRef = useRef(fullReviewRequested || (comparisonQueryPresent && !automaticComparison));
  useEffect(() => {
    comparisonSelectionPinnedRef.current = fullReviewRequested || (comparisonQueryPresent && !automaticComparison);
  }, [fullReviewRequested, comparisonQueryPresent, automaticComparison]);
  const legacyHistoricalRevisionNumber = /^\d+$/.test(routeRevisionNumber)
    ? Number(routeRevisionNumber)
    : null;
  const dedicatedItemRef = useRef(readDedicatedReviewItem());
  const dedicatedItem = dedicatedItemRef.current;
  const returnPathRef = useRef(readReviewReturnPath());
  const returnSurfaceRef = useRef(readReviewReturnSurface());
  const [reviewId, setReviewId] = useState("");
  const [locatedRevision, setLocatedRevision] = useState<{ reviewId: string; revisionNumber: number } | null>(null);
  const historicalRevisionNumber = (
    comparisonQueryValid ? requestedTargetRevision : null
  ) ?? legacyHistoricalRevisionNumber ?? locatedRevision?.revisionNumber ?? null;
  const historicalRoute = historicalRevisionNumber !== null;
  const legacyHistoricalRoute = legacyHistoricalRevisionNumber !== null || locatedRevision !== null;
  const selectedHistoricalRevisionNumber = historicalRevisionNumber ?? 0;
  const [authed, setAuthed] = useState(true);
  const [preparationId, setPreparationId] = useState("");
  const [reviewMissing, setReviewMissing] = useState<"" | "none" | "not_found" | "no_access" | "unpaired">("");
  // The live stream reported that this open Review was revoked or removed.
  const [accessLost, setAccessLost] = useState<"" | "forbidden" | "gone">("");
  const [preparationStage, setPreparationStage] = useState("");
  const [startupReady, setStartupReady] = useState(false);
  const [firstReviewableReady, setFirstReviewableReady] = useState(false);
  const [preparationRefresh, setPreparationRefresh] = useState(0);
  const firstContentPaintedRef = useRef(false);
  const { overview } = reviewStore;
  // A revision URL is only historical when it points behind the Review's
  // current head. Opening /rr/<latest> should behave exactly like /r/<review>:
  // no historical ribbon, writable latest-state actions, and source probing.
  const historical = historicalRoute && (
    (legacyHistoricalRoute && !overview)
    || Boolean(
      overview
      && selectedHistoricalRevisionNumber
        !== (overview.latest_revision_number ?? overview.revision.revision_number),
    )
  );
  // `lc review --pr` names its Review `github:owner/repo#N`; only those can publish to GitHub.
  const isGitHubPullRequest = Boolean(overview?.session.source_ref?.startsWith("github:"));
  // Comparison URLs always name a target revision, including latest. Only let
  // that number invalidate source/context loaders when it actually selects a
  // historical checkpoint; otherwise writing the default ?base=&target= query
  // would reload the already-rendered latest Reader for no reason.
  const historicalRevisionKey = historical ? selectedHistoricalRevisionNumber : -1;
  const { targetList } = reviewStore;
  const { annotations, commentsState } = reviewStore;
  const [revisionHistory, setRevisionHistory] = useState<ReviewRevisionHistory | null>(null);
  const [revisionComparison, setRevisionComparison] = useState<ReviewRevisionComparison | null>(null);
  const [sourceComparison, setSourceComparison] = useState<SourceComparison | null>(null);
  const [, setRevisionComparisonLoading] = useState(false);
  const [, setRevisionComparisonError] = useState("");

  useEffect(() => {
    const titleReviewId = overview?.session.ref || routeReviewId || routeReviewRef || overview?.session.id;
    if (!titleReviewId || flowDocumentTitle()) return;
    document.title = `Review: ${titleReviewId}`;
  }, [overview?.session.ref, overview?.session.id, routeReviewId, routeReviewRef]);

  useEffect(() => {
    if (!overview || typeof window === "undefined") return;
    const reviewRef = overview.session.ref || overview.session.id;
    // On a review's own route the store may still hold the previous review's overview for a moment (A to B without a remount):
    // that overview is stale for this address and must not rewrite it.
    const named = routeReviewRef;
    if (named && ![overview.session.id, overview.session.ref, reviewRef.replace(/^r\//, "")].some((value) => value && value.replace(/^r\//, "") === named)) return;
    const canonical = comparisonQueryValid
      ? reviewCompareHref(
          reviewRef,
          requestedHeadBase ? "head" : requestedBaseRevision!,
          requestedTargetRevision!,
        )
      : historical
        ? historicalReviewHref(reviewRef, overview.revision.revision_number)
        : reviewHref(reviewRef);
    // The open view (Brief or the Map), a dedicated-tab item, and where Back
    // returns are view state, not identity: keep them so a reload lands on the
    // same view.
    const next = new URL(canonical, window.location.origin);
    // The redesigned flow's screens live under the review's address
    // (`/r/<review>/one/<part>`): renaming the review part must not drop them.
    const flowSuffix = keepFlowScreen(next, window.location);
    const currentParams = new URLSearchParams(window.location.search);
    // A flow navigation drops the query from the address before this runs: the mode the Reader holds must survive it, or
    // its own automatic comparison would come back looking like one the reviewer pinned.
    const comparisonMode = currentParams.get("compare") ?? (automaticComparison ? "auto" : fullReviewRequested ? "full" : null);
    if (comparisonMode === "auto" || comparisonMode === "full") {
      next.searchParams.set("compare", comparisonMode);
    }
    for (const key of [...REVIEW_MAP_PARAMS, ...DEDICATED_REVIEW_ITEM_PARAMS, ...REVIEW_RETURN_PARAMS, REVIEW_VIEW_PARAM, GUIDED_COHORT_PARAM, REVIEW_BACKTRAIL_PARAM, RETURN_PARAM, "p"]) {
      const value = currentParams.get(key);
      if (value !== null) next.searchParams.set(key, value);
    }
    const nextHref = `${next.pathname}${next.search}${next.hash}`;
    const current = `${window.location.pathname}${window.location.search}${flowSuffix ? window.location.hash : ""}`;
    if (current !== nextHref) {
      if (!routeReviewRef && next.pathname !== window.location.pathname) {
        // An old address (/rr/<revision>, /reviews/<id>, /review) moves to the review's own address through the router, so the
        // route layer mounts the redesigned flow there; a bare history write would leave the router on the old route.
        navigateRef.current(nextHref, { replace: true });
        return;
      }
      window.history.replaceState(null, "", nextHref);
      setLocationSearch(window.location.search);
    }
  }, [
    overview,
    historical,
    comparisonQueryValid,
    requestedHeadBase,
    requestedBaseRevision,
    requestedTargetRevision,
    automaticComparison,
    fullReviewRequested,
    routeReviewRef,
  ]);

  // Each value is what the reviewer lost, in plain words; the transport error stays in `contextFailureCause` for hover.
  const [, setContextFailures] = useState<Partial<Record<"comments" | "evidence" | "journal" | "development" | "outcome" | "surfaces" | "plugins", ContextFailure>>>({});
  const [, setRetryingSurfaceIdentity] = useState("");
  const [evidence, setEvidence] = useState<ReviewEvidence[]>([]);
  const [journal, setJournal] = useState<ReviewJournal | null>(null);
  const [development, setDevelopment] = useState<ReviewDevelopment | null>(null);
  const [surfaces, setSurfaces] = useState<ReviewSurfaceInfo[]>([]);
  const [, setRuntimeValidations] = useState<Record<string, { response?: ReviewRuntimeValidationResponse; evidence?: ReviewEvidence; error?: string }>>({});
  const { codeTheme } = useCodeView();
  const [theme, setTheme] = useState<Theme>(() => reviewCodeThemeDefinition(getInitialReviewCodeTheme()).chromeTheme);
  const [details, setDetails] = useState<Record<string, FileDetail | undefined>>({});
  const [fileErrors, setFileErrors] = useState<Record<string, string | undefined>>({});
  const fileErrorsRef = useRef(fileErrors);
  fileErrorsRef.current = fileErrors;
  const [revealedPaths, setRevealedPaths] = useState<Set<string>>(() => new Set());
  const { activeTargetId } = reviewStore;
  const activeTargetIdRef = useRef("");
  const setActiveTargetId = useCallback((next: SetStateAction<string>) => {
    const value = typeof next === "function"
      ? next(activeTargetIdRef.current)
      : next;
    activeTargetIdRef.current = value;
    reviewDispatch({ type: "part/selected", targetId: value });
  }, [reviewDispatch]);
  const [query, setQuery] = useState("");
  const [, setOutlineCollapsed] = useState(() => Boolean(dedicatedItem) || isNarrowReviewViewport());
  const [contextOpen, setContextOpen] = useState(false);
  const [askOpen, setAskOpen] = useState(false);
  // Phone composition: one sticky bottom dock replaces the footer and the
  // header's tool/action rows; navigation, Context and Ask are mutually
  // exclusive full-height panels.
  const compactLayout = useCompactReviewLayout();
  useEffect(() => {
    if (compactLayout && (contextOpen || askOpen)) setOutlineCollapsed(true);
  }, [compactLayout, contextOpen, askOpen]);
  // Below md the file drawer starts closed, and closes again when the window
  // narrows into that range; the rail or the dock button reopens it.
  const narrowLayout = useNarrowReviewLayout();
  useEffect(() => {
    if (narrowLayout) setOutlineCollapsed(true);
  }, [narrowLayout]);
  // Local-only friction metrics for this page-load of this Review.
  useEffect(() => {
    if (!reviewId) return;
    const recorder = new ReviewFrictionRecorder(reviewId, { compact: isCompactReviewViewport() });
    setActiveReviewFriction(recorder);
    return () => {
      if (activeReviewFriction() === recorder) setActiveReviewFriction(null);
    };
  }, [reviewId]);
  const [askScope, setAskScope] = useState<ReviewAskSelection | null>(null);
  const [contextTab, setContextTab] = useState<ContextDrawerTab>("context");
  const [contextPinned, setContextPinned] = useState(() =>
    typeof window === "undefined" || window.sessionStorage.getItem(CONTEXT_PIN_KEY) !== "0",
  );
  const { draft } = reviewStore;
  const draftRef = useRef(draft);
  draftRef.current = draft;
  // The Snapshot Reload review left: when the revision-loading effect moves off
  // it, the in-memory draft is carried over instead of reading the new
  // Snapshot's empty slot.
  const reloadCarriesDraftRef = useRef("");
  const draftRevisionRef = useRef("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [, setFeedback] = useState<FeedbackExport | null>(null);
  const [feedbackStatus, setFeedbackStatus] = useState<FeedbackStatus>(EMPTY_FEEDBACK_STATUS);
  const [feedbackDelivery, setFeedbackDelivery] = useState<FeedbackDeliveryCapability | null>(null);
  const [, setDelivery] = useState<FeedbackDelivery | null>(null);
  const [activeFeedbackHandoff, setActiveFeedbackHandoff] = useState<ActiveFeedbackHandoff | null>(null);
  const [, setFinishSummary] = useState<ReviewClosure | null>(null);
  const [, setFinishSheetOpen] = useState(false);
  const [githubPublishOpen, setGithubPublishOpen] = useState(false);
  const [finishSendError, setFinishSendError] = useState("");
  // A failed verdict or finish write, said in the sheet the reviewer is looking at.
  const [finishError, setFinishError] = useState("");
  // The verdict just recorded: the sheet stays open to confirm it and offer its record.
  const [finishRecorded, setFinishRecorded] = useState<ReviewOutcomeKind | null>(null);
  // An LGTM or Comment whose comments go to the pull request finishes once GitHub has them.
  const [finishAfterPublish, setFinishAfterPublish] = useState(false);
  const [localOutcome, setLocalOutcome] = useState<ReviewOutcome | null>(null);
  const [outcomeOverride, setOutcomeOverride] = useState<ReviewOutcome | null>(null);
  const [readinessOpen] = useState(false);
  const [landingReadiness, setLandingReadiness] = useState<ReviewLandingReadiness | null>(null);
  const [landingReadinessState, setLandingReadinessState] = useState<"idle" | "loading" | "ready" | "failed">("idle");
  // Bumped when a mark commits. A mark shows at once, so the progress change
  // that re-reads readiness can reach the server before the mark does.
  const { committedMarks } = reviewStore;
  const [, setLandingError] = useState("");
  const [sourceState, setSourceState] = useState<SourceState | null>(null);
  const [proposals, setProposals] = useState<ReviewChangeProposal[]>([]);
  const [, setSourceMutationSupported] = useState(false);
  const [dismissedSourceFingerprint, setDismissedSourceFingerprint] = useState("");
  const { revisionInfo } = reviewStore;
  const [deltaFocus, setDeltaFocus] = useState(false);
  // A scoped request focuses the queue by default; the rest stays one click away.
  const [requestScopeFocus] = useState(true);
  const [related, setRelated] = useState<RelatedPanel | null>(null);
  const [, setOpeningPath] = useState("");
  const navigationRef = useRef(0);
  // The evidence-backed Brief (plan, decisions, questions, evidence) fills
  // the shell's Brief view when the capability is on.
  const briefCapable = reviewEnvironment.review.brief;
  const askCapable = reviewEnvironment.review.ask;
  const [projection, setProjection] = useState<ReviewProjection | null>(null);
  const [projectionState, setProjectionState] = useState<"idle" | "loading" | "ready" | "failed">("idle");
  const viewChosenRef = useRef(false);
  // Set once the first view is settled: by the Brief landing below, by a
  // failed plan load, or by the reviewer. Until then a Reader that will land on
  // the Brief keeps its startup screen, so it never opens on an empty Brief or
  // flips from Implementation to the Brief a moment after appearing.
  const [landingDecided, setLandingDecided] = useState(false);
  const projectionRequestRef = useRef(0);
  // The newest foreground load (a screen waiting on it) and the newest request whose plan is on screen. A plan that a
  // background refresh overtook still counts while nothing newer has landed: waiting for the refresh instead meant
  // two heavy /projection calls before ready (P9-6).
  const projectionForegroundRef = useRef(0);
  const projectionAppliedRef = useRef(0);
  const [briefPreparation, setBriefPreparation] = useState<BriefPreparationProgress | null>(null);
  const [briefStartedAt, setBriefStartedAt] = useState<number | undefined>();
  // The Classic Brief, Guided review and Map views are retired (P8-3): the Reader holds the one view, and an old `?view=`
  // or Map query on an address is ignored. The flow's own screens (`/brief`, `/xray`, `/walkthrough`, `/map`) replace them.
  const view = "implementation" as ReviewView;
  const mapLocation = null as ReturnType<typeof readReviewMapLocation>;
  // Only the /r/:reviewRef/* route branch renders the flow's screens (SPEC 3.1). An old address (/rr/<revision>,
  // /reviews/<id>[/revisions/<n>], /review) mounts the Reader bare, and the canonical-address effect below moves it
  // through the router to /r/<review>, where this branch takes over; until then it draws nothing.
  const flowRoute = Boolean(routeReviewRef)
    ? parseReviewFlowLocation(window.location) : null;
  const flowOverview = Boolean(flowRoute?.known && flowRoute.route.kind === "overview");
  const flowAll = Boolean(flowRoute?.known && flowRoute.route.kind === "all");
  const flowMap = Boolean(flowRoute?.known && flowRoute.route.kind === "map");
  const flowPreview = Boolean(flowRoute?.known && flowRoute.route.kind === "preview");
  const flowFocus = Boolean(flowRoute?.known && flowRoute.route.kind === "one");
  const flowTour = Boolean(flowRoute?.known && flowRoute.route.kind === "walkthrough");
  const flowBrief = Boolean(flowRoute?.known && flowRoute.route.kind === "brief");
  const flowXRay = Boolean(flowRoute?.known && flowRoute.route.kind === "xray");
  const flowWrap = Boolean(flowRoute?.known && flowRoute.route.kind === "wrap");
  const flowDone = Boolean(flowRoute?.known && flowRoute.route.kind === "done");
  const flowBack = Boolean(flowRoute?.known && flowRoute.route.kind === "rev");
  const flowSecond = Boolean(flowRoute?.known && flowRoute.route.kind === "second");
  // The flow screen shown before this one: Wrap up reads it for its back control ("All files" after All files).
  const flowKindNow = flowRoute?.known ? flowRoute.route.kind : "";
  const flowKindBefore = useRef({ previous: "", current: "" });
  if (flowKindBefore.current.current !== flowKindNow) flowKindBefore.current = { previous: flowKindBefore.current.current, current: flowKindNow };
  const wrapOpenedFromAll = flowKindBefore.current.previous === "all";
  // True only while a redesigned screen is actually shown.
  const flowScreenShown = Boolean(flowRoute?.known);
  // P7-2 (hosted only): tell the team I am in this review. Nothing is asked of a local install (SPEC 7.1 P0-2).
  useReviewPresence(reviewId, flowScreenShown);
  const briefShown = briefCapable && view === "brief" && projection !== null;
  const tourShown = briefCapable && (view === "tour" || flowTour) && projection !== null;
  // Deep links, return-to-file/surface, comparisons and remembered explicit choices outrank Auto landing.
  const defaultLanding = !dedicatedItem && !returnPathRef.current && !returnSurfaceRef.current && !comparisonQueryPresent;
  const storedLanding = readStoredView(reviewId);
  const autoLargeReview = defaultLanding
    && !storedLanding
    && (targetList?.progress.target_count ?? 0) > LARGE_REVIEW_TARGET_THRESHOLD;
  const largeStartupIncomplete = targetList?.complete === false;
  const deferLargeStartupEnrichment = largeStartupIncomplete || autoLargeReview;
  // A compact Code session stays demand-driven even after first paint. Rich
  // Review projections hydrate only when the reviewer asks for them.
  const startupEnrichmentReady = !deferLargeStartupEnrichment
    || view !== "implementation"
    || readinessOpen
    || flowOverview || flowFocus || flowTour || flowBrief || flowXRay;
  const startupEnrichmentReadyRef = useRef(startupEnrichmentReady);
  startupEnrichmentReadyRef.current = startupEnrichmentReady;
  useEffect(() => {
    firstContentPaintedRef.current = false;
  }, [reviewId]);
  const briefLandingPending = briefCapable && !landingDecided && (
    view === "brief"
    || view === "tour"
    || (view === "implementation" && defaultLanding && storedLanding !== "implementation" && !autoLargeReview)
  );
  // The Code surface normally stays mounted when hidden to preserve scroll
  // position. Do not mount it at all until the reviewer first opens Code;
  // otherwise its lazy bundle still downloads behind the initial Brief.
  const [, setCodeWasShown] = useState(false);
  // Initial routes temporarily say Code while their Brief landing is decided.
  // Hold its chunk only for that case. Explicit Code, Review-only installs,
  // large/partial Code-first reviews, and remembered Code must work immediately.
  const codeNeeded = view === "implementation" && (
    landingDecided || (Boolean(overview) && Boolean(targetList) && !briefCapable) || !defaultLanding
    || storedLanding === "implementation" || autoLargeReview
  );
  useEffect(() => {
    if (codeNeeded) setCodeWasShown(true);
  }, [codeNeeded]);
  const changeViewRef = useRef<(next: ReviewView) => void>(() => undefined);
  const openPreparedCode = useCallback(() => changeViewRef.current("implementation"), []);
  const revealImplementation = useCallback(() => {
    if (view !== "implementation") changeViewRef.current("implementation");
  }, [view]);
  const setMapLocation = useCallback((location: ReviewMapLocation | null, mode: "push" | "replace" = "push") => {
    setSearchParams((current) => writeReviewMapLocation(current, location), { replace: mode === "replace" });
  }, [setSearchParams]);
  const [, setDevelopmentOpen] = useState(false);
  const { revisionAdvanced } = reviewStore;
  const currentOutcome = outcomeOverride ?? extensions?.outcome ?? localOutcome;
  const changeRevisionComparison = useCallback((baseRevision: ReviewCompareBase, targetRevision: number) => {
    comparisonSelectionPinnedRef.current = true;
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      params.delete("compare");
      params.set("base", String(baseRevision));
      params.set("target", String(targetRevision));
      return params;
    });
    setActiveTargetId("");
    setRelated(null);
  }, [setSearchParams]);
  const streamRef = useRef<ReviewStreamHandle | null>(null);
  // Navigations can arrive while the first Code chunk is still downloading.
  // Preserve the latest requested jump until the lazy stream exposes its handle.
  const pendingCodeJumpRef = useRef<(
    | { kind: "target"; target: ReviewTarget; behavior: "instant" | "smooth"; current: () => boolean }
    | { kind: "file"; path: string; behavior?: "instant" | "smooth"; current: () => boolean }
  ) | null>(null);
  // Context and Ask are non-modal panels, so they restore focus themselves; modal
  // Dialog/Sheet surfaces return focus to their opener on their own.
  const contextReturnFocusRef = useRef<HTMLElement | null>(null);
  const inFlightPaths = useRef(new Map<string, Promise<FileDetail | null>>());
  const detailsRef = useRef<Record<string, FileDetail | undefined>>({});
  const staleDetailPathsRef = useRef(new Set<string>());
  const revealedPathsRef = useRef(new Set<string>());
  const backgroundLoadQueueRef = useRef<Array<{ reviewId: string; path: string }>>([]);
  const backgroundLoadActiveRef = useRef(false);
  const loadGenerationRef = useRef(0);
  // The Snapshot the per-path file cache above describes. The cache is keyed
  // by path only, so a load that lands on another Snapshot starts it over.
  const fileCacheRevisionRef = useRef("");
  // Bumped by every loadReader and by a capture: context a superseded load
  // requested (comments, evidence, journal, Development, surfaces) is dropped.
  const readerLoadRef = useRef(0);
  // While the Reader's own capture is in flight: resolves to the Snapshot it
  // moved to ("" if it failed), so its own live-state SSE event is not news.
  const ownCaptureRef = useRef<Promise<string> | null>(null);
  const targetListRef = useRef<ReviewTargetList | null>(null);
  targetListRef.current = targetList;
  const sourceProbeRef = useRef({ fingerprint: "", count: 0 });
  const feedbackAutoRefreshFingerprintRef = useRef("");
  const stableTargetRevisionRef = useRef("");
  const stableTargetOrderRef = useRef<string[]>([]);
  const revisionComparisonCacheRef = useRef(new Map<string, ReviewRevisionComparison>());
  const revisionHistoryCacheRef = useRef<{ reviewId: string; history: ReviewRevisionHistory } | null>(null);

  useEffect(() => {
    const revisionId = overview?.revision.id ?? "";
    if (!reviewId || !revisionId || draftRevisionRef.current === revisionId) return;
    const previousRevisionId = draftRevisionRef.current;
    draftRevisionRef.current = revisionId;
    const carried = reloadCarriesDraftRef.current === previousRevisionId ? draftRef.current : null;
    reloadCarriesDraftRef.current = "";
    if (previousRevisionId && carried && carried.mode !== "proposal") {
      // Reload onto a Snapshot captured elsewhere: like the Reader's own
      // capture, the unsent text moves to the new Snapshot and its anchor does
      // not; only a new human selection may reattach it.
      saveCommentDraft(reviewId, previousRevisionId, null);
      reviewDispatch({ type: "draft/set", draft: {
        ...carried,
        startLine: 0,
        endLine: 0,
        fileLevel: false,
        targetUnitKey: undefined,
        recovered: true,
      } });
      return;
    }
    reviewDispatch({ type: "draft/set", draft: loadCommentDraft(reviewId, revisionId) });
  }, [reviewId, overview?.revision.id]);

  useEffect(() => {
    const revisionId = overview?.revision.id ?? "";
    if (!reviewId || !revisionId || draftRevisionRef.current !== revisionId) return;
    // Persist only unsent human annotation text. Proposal/source-edit internals
    // never cross a reload through this channel.
    if (draft?.mode === "proposal") return;
    saveCommentDraft(reviewId, revisionId, draft);
  }, [reviewId, overview?.revision.id, draft]);

  useLayoutEffect(() => {
    const applied = applyReviewCodeTheme(codeTheme);
    if (applied !== theme) setTheme(applied);
    // Theme selection owns both source syntax and Reader chrome. Rendered web
    // previews keep their own Auto/Light/Dark preference.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [codeTheme]);

  useEffect(() => {
    let live = true;
    const bootstrap = adoptBootstrapFragment({
      restorePreparation: !routeReviewId && !routeReviewRef && !routeRevisionRef,
    });
    setAuthed(true);
    setError("");
    setReviewMissing("");
    setAccessLost("");
    setLocatedRevision(null);

    if (routeRevisionRef) {
      const reference = routeRevisionRef;
      setReviewId("");
      setPreparationId("");
      setStartupReady(false);
      void fetchRevisionLocator(reference)
        .then(({ review, revision }) => {
          if (!live) return;
          selectReviewId(review.id);
          setReviewId(review.id);
          setLocatedRevision({ reviewId: review.id, revisionNumber: revision.revision_number });
          setStartupReady(true);
        })
        .catch((err: unknown) => {
          if (!live) return;
          const missing = missingReviewState(err);
          if (missing) setReviewMissing(missing);
          setError(err instanceof Error ? err.message : String(err));
        });
      return () => {
        live = false;
      };
    }

    const resolvedReviewId = routeReviewId || routeReviewRef || bootstrap.reviewId;
    if (resolvedReviewId) selectReviewId(resolvedReviewId);
    else setReviewMissing("none");
    setReviewId(resolvedReviewId);
    setPreparationId(bootstrap.preparationId ?? "");
    setStartupReady(Boolean(bootstrap.sourceReady) || !bootstrap.preparationId);
    return () => {
      live = false;
    };
  }, [routeReviewId, routeReviewRef, routeRevisionRef]);

  useEffect(() => {
    if (!reviewId || !authed || !startupReady || !overview || !targetList) return;
    if (targetList.complete === false && !startupEnrichmentReady && !comparisonQueryPresent && historicalRevisionNumber === null) {
      setRevisionComparisonLoading(false);
      return;
    }
    let live = true;
    setRevisionComparisonLoading(true);
    setRevisionComparisonError(malformedComparisonQuery ? "Comparison URL requires valid non-negative revision numbers." : "");

    const cachedHistory = revisionHistoryCacheRef.current;
    const historyPromise = cachedHistory?.reviewId === reviewId
      && (
        overview?.latest_revision_number == null
        || cachedHistory.history.latest_revision_number === overview.latest_revision_number
      )
      ? Promise.resolve(cachedHistory.history)
      : fetchRevisions(reviewId);
    const requestedComparisonKey = comparisonQueryValid && !requestedHeadBase
      ? `${reviewId}:${requestedBaseRevision}:${requestedTargetRevision}`
      : historicalRevisionNumber !== null && !requestedHeadBase
        ? `${reviewId}:${historicalRevisionNumber}:${historicalRevisionNumber}`
        : "";
    const cachedComparison = requestedComparisonKey
      ? revisionComparisonCacheRef.current.get(requestedComparisonKey)
      : undefined;
    const comparisonPromise = malformedComparisonQuery || requestedHeadBase
      ? Promise.resolve<ReviewRevisionComparison | null>(null)
      : cachedComparison
        ? Promise.resolve(cachedComparison)
        : comparisonQueryValid
          ? fetchRevisionComparison(reviewId, requestedBaseRevision!, requestedTargetRevision!)
          : historicalRevisionNumber !== null
            ? fetchRevisionComparison(reviewId, historicalRevisionNumber, historicalRevisionNumber)
            : Promise.resolve<ReviewRevisionComparison | null>(null);

    void Promise.allSettled([historyPromise, comparisonPromise]).then(([historyResult, comparisonResult]) => {
      if (!live) return;

      if (historyResult.status === "fulfilled") {
        revisionHistoryCacheRef.current = { reviewId, history: historyResult.value };
        setRevisionHistory(historyResult.value);
      } else {
        setRevisionHistory(null);
        setRevisionComparisonError(String(
          historyResult.reason instanceof Error ? historyResult.reason.message : historyResult.reason,
        ));
      }

      if (comparisonResult.status === "fulfilled" && comparisonResult.value) {
        const next = comparisonResult.value;
        revisionComparisonCacheRef.current.set(
          `${reviewId}:${next.from_revision.revision_number}:${next.to_revision.revision_number}`,
          next,
        );
        setRevisionComparison(next);
      } else if (comparisonResult.status === "rejected") {
        setRevisionComparison(null);
        setRevisionComparisonError(String(
          comparisonResult.reason instanceof Error ? comparisonResult.reason.message : comparisonResult.reason,
        ));
      } else if (malformedComparisonQuery || requestedHeadBase) {
        setRevisionComparison(null);
      }

      setRevisionComparisonLoading(false);
    });

    return () => {
      live = false;
    };
  }, [
    reviewId,
    authed,
    startupReady,
    comparisonQueryPresent,
    comparisonQueryValid,
    malformedComparisonQuery,
    requestedHeadBase,
    requestedBaseRevision,
    requestedTargetRevision,
    historicalRevisionNumber,
    overview?.latest_revision_number,
    targetList,
    startupEnrichmentReady,
    setSearchParams,
  ]);

  useEffect(() => {
    if (!requestedHeadBase) {
      setSourceComparison(null);
      return;
    }
    if (
      !reviewId
      || !authed
      || !startupReady
      || requestedTargetRevision === null
      || !revisionHistory
    ) return;

    const target = revisionHistory.revisions.find(
      (item) => item.revision_number === requestedTargetRevision,
    );
    if (!target) {
      setSourceComparison(null);
      setRevisionComparisonError(`No such ${revisionName(requestedTargetRevision)}.`);
      return;
    }

    let live = true;
    setRevisionComparisonLoading(true);
    setRevisionComparisonError("");
    const scope = overview?.session.ref || `r/${reviewId}`;
    void fetchSourceComparison("git/HEAD", target.ref, scope)
      .then((result) => {
        if (!live) return;
        setSourceComparison(result);
      })
      .catch((reason: unknown) => {
        if (!live) return;
        setSourceComparison(null);
        setRevisionComparisonError(String(reason instanceof Error ? reason.message : reason));
      })
      .finally(() => {
        if (live) setRevisionComparisonLoading(false);
      });

    return () => {
      live = false;
    };
  }, [
    requestedHeadBase,
    requestedTargetRevision,
    reviewId,
    authed,
    startupReady,
    revisionHistory,
    overview?.session.ref,
  ]);

  useEffect(() => {
    if (!reviewId || !authed || !preparationId) return;
    let live = true;
    let timer = 0;
    const check = async () => {
      try {
        const status = await fetchReviewPreparation(reviewId, preparationId);
        if (!live) return;
        if (status.state === "ready") {
          setPreparationStage("");
          setStartupReady(true);
          setPreparationRefresh((value) => value + 1);
          return;
        }
        if (status.state === "failed") {
          setPreparationStage("failed");
          setError(status.detail || "review preparation failed before the first revision became ready");
          return;
        }
        if (status.state === "impact_background" || status.state === "finalizing_background" || status.state === "enrichment_failed") {
          setPreparationStage(status.state);
          setStartupReady(true);
          if (status.state === "enrichment_failed" && status.detail) setError(status.detail);
        } else {
          setPreparationStage(status.state);
        }
      } catch {
        if (live) setPreparationStage("connecting");
      }
      if (live) timer = window.setTimeout(() => void check(), PREPARATION_POLL_MS);
    };
    void check();
    return () => {
      live = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [reviewId, authed, preparationId]);

  const loadPath = useCallback(async (id: string, path: string): Promise<FileDetail | null> => {
    if (!path) return null;
    // Human navigation always jumps ahead of speculative preparation. Removing a
    // queued path does not cancel a request that is already running, but the
    // background worker is deliberately single-flight so an explicit click can
    // start immediately in its own request slot instead of sitting behind the
    // rest of the queue.
    backgroundLoadQueueRef.current = backgroundLoadQueueRef.current.filter(
      (item) => !(item.reviewId === id && item.path === path),
    );
    const cached = detailsRef.current[path];
    if (cached && !staleDetailPathsRef.current.has(path)) return cached;
    const existing = inFlightPaths.current.get(path);
    if (existing) return existing;
    const generation = loadGenerationRef.current;
    let request: Promise<FileDetail | null>;
    // A dropped connection or server restart must not strand the file on an
    // error card; retry transient failures before reporting one.
    request = withTransientRetry(() => (historical ? fetchHistoricalFile(id, historicalRevisionKey, path) : fetchFile(id, path, fileCacheRevisionRef.current)))
      .then((detail) => {
        if (loadGenerationRef.current !== generation) return null;
        const cacheRevision = fileCacheRevisionRef.current;
        if (detail.revision_id && cacheRevision && detail.revision_id !== cacheRevision) {
          // Answered for another Snapshot than the one on screen: never shown
          // as this one's code.
          setFileErrors((current) => ({ ...current, [path]: "This file came back for a different revision than the one shown. Retry, or reload the review." }));
          return null;
        }
        setDetails((current) => {
          const next = { ...current, [path]: detail };
          detailsRef.current = next;
          return next;
        });
        staleDetailPathsRef.current.delete(path);
        setFileErrors((current) => ({ ...current, [path]: undefined }));
        return detail;
      })
      .catch((reason: unknown) => {
        if (loadGenerationRef.current === generation) {
          setFileErrors((current) => ({
            ...current,
            [path]: String(reason instanceof Error ? reason.message : reason),
          }));
        }
        return null;
      })
      .finally(() => {
        if (inFlightPaths.current.get(path) === request) inFlightPaths.current.delete(path);
      });
    inFlightPaths.current.set(path, request);
    return request;
  }, [historical, historicalRevisionKey]);

  const loadPaths = useCallback(async (id: string, paths: string[]) => {
    await Promise.all(paths.map((path) => loadPath(id, path)));
  }, [loadPath]);

  const revealPaths = useCallback((paths: readonly string[], reset = false) => {
    const next = reset ? new Set<string>() : new Set(revealedPathsRef.current);
    for (const path of paths) if (path) next.add(path);
    revealedPathsRef.current = next;
    setRevealedPaths(next);
  }, []);

  const pumpBackgroundLoads = useCallback(() => {
    if (backgroundLoadActiveRef.current) return;
    const next = backgroundLoadQueueRef.current.shift();
    if (!next) return;
    backgroundLoadActiveRef.current = true;
    void loadPath(next.reviewId, next.path).finally(() => {
      backgroundLoadActiveRef.current = false;
      pumpBackgroundLoads();
    });
  }, [loadPath]);

  const queueBackgroundPaths = useCallback((id: string, paths: readonly string[]) => {
    const queued = new Set(backgroundLoadQueueRef.current.map((item) => `${item.reviewId}\u0000${item.path}`));
    for (const path of paths) {
      if (!path || detailsRef.current[path] || inFlightPaths.current.has(path)) continue;
      const key = `${id}\u0000${path}`;
      if (queued.has(key)) continue;
      backgroundLoadQueueRef.current.push({ reviewId: id, path });
      queued.add(key);
    }
    pumpBackgroundLoads();
  }, [pumpBackgroundLoads]);

  const queueNextPage = useCallback((id: string, allPaths: readonly string[], weights: ReadonlyMap<string, number>) => {
    const pages = reviewPages(allPaths, weights);
    const pagePaths = pages[Math.min(requestedPageRef.current, pages.length) - 1] ?? [];
    const remaining = pagePaths.filter((path) => !revealedPathsRef.current.has(path));
    const next = pathsForChangeBudget(remaining, weights, LOAD_MORE_CHANGE_BUDGET, BACKGROUND_PREFETCH_FILES);
    queueBackgroundPaths(id, next);
  }, [queueBackgroundPaths]);

  const stabilizeTargetOrder = useCallback((next: ReviewTargetList): ReviewTargetList => {
    // A bounded startup/path slice is not the authoritative queue. Let it drive
    // immediate Code navigation, but never let unseen targets acquire stable
    // slots from the order in which they happened to be fetched.
    if (next.complete === false) return next;
    if (stableTargetRevisionRef.current !== next.revision_id || stableTargetOrderRef.current.length === 0) {
      stableTargetRevisionRef.current = next.revision_id;
      stableTargetOrderRef.current = next.targets.map((target) => target.target_id);
      return next;
    }
    const known = new Set(stableTargetOrderRef.current);
    const ordered = applyStableOrder(next, stableTargetOrderRef.current);
    for (const target of next.targets) {
      if (!known.has(target.target_id)) stableTargetOrderRef.current.push(target.target_id);
    }
    return ordered;
  }, []);

  // Forget every file loaded, or still loading, for another Snapshot.
  const resetFileCache = useCallback(() => {
    loadGenerationRef.current += 1;
    backgroundLoadQueueRef.current = [];
    inFlightPaths.current.clear();
    detailsRef.current = {};
    staleDetailPathsRef.current = new Set();
    setDetails({});
    setFileErrors({});
  }, []);

  // Agent context and Development for one Snapshot, committed only while the
  // load (or capture) that asked for them is still current.
  const loadSnapshotContext = useCallback((
    journalPromise: Promise<ReviewJournal>,
    developmentPromise: Promise<ReviewDevelopment | null | undefined>,
    isCurrent: () => boolean,
  ) => {
    void journalPromise.then((next) => {
      if (!isCurrent()) return;
      setJournal(next);
      setContextFailures((current) => ({ ...current, journal: undefined }));
    }).catch((reason: unknown) => {
      if (!isCurrent()) return;
      setJournal(null);
      setContextFailures((current) => ({
        ...current,
        journal: contextFailure("Couldn't load the agent's notes.", reason),
      }));
    });
    void developmentPromise.then((next) => {
      if (!isCurrent()) return;
      setDevelopment(next ?? null);
      setContextFailures((current) => ({ ...current, development: undefined }));
    }).catch((reason: unknown) => {
      if (!isCurrent()) return;
      setDevelopment(null);
      setContextFailures((current) => ({
        ...current,
        development: contextFailure("Couldn't load the development session.", reason),
      }));
    });
  }, []);

  // The rendered surfaces discovered on one Snapshot (each Snapshot lists its own).
  const loadSurfaces = useCallback((
    id: string,
    revisionId: string,
    targets: ReviewTarget[],
    isCurrent: () => boolean,
    indexedPaths?: readonly string[],
    indexedWeights?: ReadonlyMap<string, number>,
    prefetchPlacements = true,
  ) => {
    return fetchReviewSurfaces(id, revisionId).then((next) => {
      if (!isCurrent()) return;
      setSurfaces(next.surfaces);
      setContextFailures((current) => ({
        ...current,
        surfaces: undefined,
        plugins: surfacePluginIssueMessage(next.issues),
      }));
      if (!prefetchPlacements) return;
      const allPaths = indexedPaths ?? orderedPaths(targets);
      const weights = indexedWeights ?? changeLinesByPath(targets);
      const reviewPaths = new Set(allPaths);
      const pages = reviewPages(allPaths, weights);
      const pagePaths = new Set(pages[Math.min(requestedPageRef.current, pages.length) - 1] ?? []);
      queueBackgroundPaths(
        id,
        surfacePlacementPaths(next.surfaces, reviewPaths)
          .filter((path) => pagePaths.has(path) && !revealedPathsRef.current.has(path)),
      );
    }).catch((reason: unknown) => {
      if (!isCurrent()) return;
      setContextFailures((current) => ({
        ...current,
        surfaces: contextFailure("Couldn't load rendered previews.", reason),
      }));
    });
  }, [queueBackgroundPaths]);

  const loadReader = useCallback(async (id: string): Promise<boolean> => {
    // A newer load, or a capture, supersedes this one: nothing it requested
    // may land after that (G0.7's rule for surface runs, for all context).
    const load = ++readerLoadRef.current;
    const isCurrentLoad = () => readerLoadRef.current === load;
    // A retry from an earlier Snapshot no longer owns this Reader.
    setRetryingSurfaceIdentity("");
    reviewDispatch({ type: "notes/loadStarted" });
    setContextFailures({});
    try {
      // Source review is the critical path. Current large Reviews use the
      // bounded startup projection; historical routes stay fully immutable.
      const [nextOverview, fetchedTargets] = await Promise.all([
        historical ? fetchHistoricalOverview(id, historicalRevisionKey) : fetchStartupOverview(id),
        historical ? fetchHistoricalTargets(id, historicalRevisionKey) : fetchStartupTargets(id),
      ]);
      if (!isCurrentLoad()) return false;

      let startupTargets = fetchedTargets;
      if (
        !historical
        && startupTargets.complete === false
        && dedicatedItem?.kind === "file"
        && !startupTargets.targets.some((target) => target.path === dedicatedItem.path)
      ) {
        const slice = await fetchTargetsForPath(id, dedicatedItem.path, startupTargets.order);
        if (!isCurrentLoad()) return false;
        if (slice.revision_id === startupTargets.revision_id) {
          const seen = new Set(startupTargets.targets.map((target) => target.target_id));
          startupTargets = {
            ...startupTargets,
            targets: [...startupTargets.targets, ...slice.targets.filter((target) => !seen.has(target.target_id))],
          };
        }
      }

      // The file cache is keyed by path only. Files cached for another Snapshot
      // are never reusable as this Snapshot's code.
      const sameSnapshot = fileCacheRevisionRef.current === nextOverview.revision.id;
      if (!sameSnapshot) {
        resetFileCache();
        fileCacheRevisionRef.current = nextOverview.revision.id;
      }
      const nextTargets = historical ? startupTargets : stabilizeTargetOrder(startupTargets);
      const deferStartupContext = !dedicatedItem
        && !historical
        && nextTargets.progress.target_count > LARGE_REVIEW_TARGET_THRESHOLD;
      // loadReader runs before React commits the compact target list. Close the
      // gate synchronously so a stale ref from the previous render cannot start
      // rich enrichment on the first-useful-Code critical path.
      if (deferStartupContext) startupEnrichmentReadyRef.current = false;

      function afterStartupDemand<T>(loadContext: () => Promise<T>): Promise<T> {
        const run = () => Promise.resolve().then(loadContext);
        if (!deferStartupContext) return run();
        return new Promise<T>((resolve, reject) => {
          const wait = () => {
            if (!isCurrentLoad()) {
              reject(new Error("review load superseded"));
              return;
            }
            if (startupEnrichmentReadyRef.current) {
              void run().then(resolve, reject);
              return;
            }
            window.setTimeout(wait, 50);
          };
          wait();
        });
      }

      // Human discussion state stays foreground because it changes what Code
      // means. Rich evidence/development/outcome context waits for explicit
      // reviewer demand on very large Reviews.
      const annotationsPromise = Promise.resolve().then(() => (
        historical ? fetchHistoricalAnnotations(id, historicalRevisionKey) : fetchAnnotations(id)
      ));
      const evidencePromise = afterStartupDemand(() => fetchReviewEvidence(id, nextOverview.revision.id));
      const journalPromise = afterStartupDemand(() => (
        historical ? fetchHistoricalReviewJournal(id, historicalRevisionKey) : fetchReviewJournal(id)
      ));
      const developmentPromise = afterStartupDemand(() => (
        historical ? fetchHistoricalReviewDevelopment(id, historicalRevisionKey) : fetchReviewDevelopment(id)
      ));
      const outcomePromise = historical
        ? Promise.resolve(null)
        : afterStartupDemand(() => fetchReviewOutcome(id).then((next) => next.current));

      let dedicatedSurfaces: ReviewSurfaceInfo[] = [];
      if (dedicatedItem?.kind === "surface") {
        const dedicatedList = await fetchReviewSurfaces(id, nextOverview.revision.id);
        if (!isCurrentLoad()) return false;
        dedicatedSurfaces = dedicatedList.surfaces;
        setSurfaces(dedicatedSurfaces);
        setContextFailures((current) => ({ ...current, plugins: surfacePluginIssueMessage(dedicatedList.issues) }));
      } else {
        setSurfaces([]);
      }

      reviewDispatch({ type: "overview/set", overview: nextOverview });
      // A later compact startup read may finish after full hydration or a
      // far-file target fetch. Keep those exact same-Snapshot targets instead
      // of leaving the reviewer on a page with no target metadata.
      const displayedTargets = retainTargetsOnSameRevision(targetListRef.current, nextTargets);
      reviewDispatch({ type: "parts/startupRetained", incoming: nextTargets });
      setError("");
      const first = firstActionable(displayedTargets.targets);
      setActiveTargetId((current) => current && displayedTargets.targets.some((target: ReviewTarget) => target.target_id === current)
        ? current
        : first?.target_id ?? "");

      // Large startup carries every file as a compact index, but only a bounded
      // target slice. Page membership remains exact before full hydration.
      const indexedOutline = nextTargets.complete === false ? nextTargets.outline : [];
      const allPaths = indexedOutline.length > 0
        ? indexedOutline.map((row) => row.path)
        : orderedPaths(nextTargets.targets);
      const weights = indexedOutline.length > 0
        ? new Map(indexedOutline.map((row) => [row.path, Math.max(1, row.changed_lines ?? 1)] as const))
        : changeLinesByPath(nextTargets.targets);
      const initialPages = reviewPages(allPaths, weights);
      const initialPagePaths = initialPages[Math.min(requestedPageRef.current, initialPages.length) - 1] ?? [];
      const defaultPathsToShow = dedicatedItem?.kind === "file"
        ? [dedicatedItem.path]
        : dedicatedItem?.kind === "surface"
          ? dedicatedSurfaces.find(
            (surface) => surface.provider === dedicatedItem.provider && surface.id === dedicatedItem.id,
          )?.affected_paths ?? []
          : pathsForChangeBudget(
              initialPagePaths,
              weights,
              INITIAL_CHANGE_BUDGET,
              INITIAL_MAX_FILES,
            );
      const pathsToShow = nextTargets.complete === false && !dedicatedItem
        ? defaultPathsToShow.slice(0, 1)
        : defaultPathsToShow;
      // The flow's screens fetch the code they draw themselves; only a comparison (base and target in the address) needs these patches here.
      const uniquePaths = comparisonQueryValid ? [...new Set(pathsToShow)] : [];
      // A reload of the same Snapshot (background enrichment) keeps every file
      // the reviewer already opened; only a different Snapshot starts over.
      revealPaths(uniquePaths, !sameSnapshot);

      const initialLoads = uniquePaths.map((path) => loadPath(id, path));
      const firstPageLoad = Promise.all(initialLoads);
      const firstSourceReady = uniquePaths.length === 0
        ? Promise.resolve()
        : new Promise<void>((resolve) => {
          let settled = 0;
          for (const request of initialLoads) {
            void request.then((detail) => {
              settled += 1;
              if (detail || settled === initialLoads.length) resolve();
            });
          }
        });

      void annotationsPromise.then((next) => {
        if (!isCurrentLoad()) return;
        reviewDispatch({ type: "notes/loaded", annotations: next.annotations });
        setFeedbackStatus(next.feedback ?? fallbackFeedbackStatus(next.annotations));
        setFeedbackDelivery(next.delivery ?? null);
        setContextFailures((current) => ({ ...current, comments: undefined }));
      }).catch((reason: unknown) => {
        if (!isCurrentLoad()) return;
        reviewDispatch({ type: "notes/loadFailed" });
        setContextFailures((current) => ({
          ...current,
          comments: contextFailure("Couldn't load comments.", reason),
        }));
      });
      void evidencePromise.then((next) => {
        if (!isCurrentLoad()) return;
        setEvidence(next.evidence);
        setContextFailures((current) => ({ ...current, evidence: undefined }));
      }).catch((reason: unknown) => {
        if (!isCurrentLoad()) return;
        setContextFailures((current) => ({
          ...current,
          evidence: contextFailure("Couldn't load check results.", reason),
        }));
      });
      loadSnapshotContext(journalPromise, developmentPromise, isCurrentLoad);
      void outcomePromise.then((next) => {
        if (!isCurrentLoad()) return;
        setLocalOutcome(next);
        setContextFailures((current) => ({ ...current, outcome: undefined }));
      }).catch((reason: unknown) => {
        if (!isCurrentLoad()) return;
        setContextFailures((current) => ({
          ...current,
          outcome: contextFailure("Couldn't load your verdict.", reason),
        }));
      });

      await firstSourceReady;
      if (!isCurrentLoad()) return false;
      setFirstReviewableReady(true);
      if (dedicatedItem?.kind !== "surface") {
        if (deferStartupContext) {
          void afterStartupDemand(async () => {
            loadSurfaces(
              id,
              nextOverview.revision.id,
              nextTargets.targets,
              isCurrentLoad,
              allPaths,
              weights,
              nextTargets.complete !== false,
            );
          }).catch(() => {
            // Superseded by a newer load, which places its own surfaces.
          });
        } else {
          loadSurfaces(
            id,
            nextOverview.revision.id,
            nextTargets.targets,
            isCurrentLoad,
            allPaths,
            weights,
            nextTargets.complete !== false,
          );
        }
      }

      // The rest of the initial visible window is already in flight. Page
      // activation owns background lookahead so startup never duplicates it.
      await firstPageLoad;
      return true;
    } catch (reason) {
      // A newer load owns the Reader now; this one's failure describes nothing on screen.
      if (!isCurrentLoad()) return false;
      const missing = missingReviewState(reason);
      if (missing) setReviewMissing(missing);
      setError(String(reason instanceof Error ? reason.message : reason));
      return false;
    }
  }, [comparisonQueryValid, dedicatedItem, historical, historicalRevisionKey, loadPath, loadSnapshotContext, loadSurfaces, resetFileCache, revealPaths, stabilizeTargetOrder]);

  useEffect(() => {
    if (!reviewId || !authed || !startupReady) return;
    backgroundLoadQueueRef.current = [];
    void loadReader(reviewId);
  }, [reviewId, authed, startupReady, preparationRefresh, loadReader]);
  useEffect(() => {
    if (!overview || comparisonSelectionPinnedRef.current || legacyHistoricalRoute || dedicatedItem) return;
    const target = overview.default_target_revision_number ?? overview.revision.revision_number;
    const base = overview.default_base_revision_number ?? target;
    if (base === target && !automaticComparison) return;
    if (requestedBaseRevision === base && requestedTargetRevision === target) return;
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      params.set("compare", "auto");
      params.set("base", String(base));
      params.set("target", String(target));
      return params;
    }, { replace: true });
  }, [overview, legacyHistoricalRoute, dedicatedItem, automaticComparison, requestedBaseRevision, requestedTargetRevision, setSearchParams]);

  useEffect(() => {
    if (!reviewId || !authed || !overview || historical) return;
    if (!reviewEnvironment.review.workspace_refresh) {
      setSourceState(null);
      return;
    }
    if (overview.session.range_mode !== "working_tree" && overview.session.range_mode !== "staged") return;
    if (overview.session.status === "archived" || revisionAdvanced) {
      setSourceState(null);
      return;
    }
    return subscribeSourceState(reviewId, (state) => {
      if (!state.supported || !state.changed) {
        sourceProbeRef.current = { fingerprint: "", count: 0 };
        setSourceState(null);
      } else {
        const seen = sourceProbeRef.current;
        const settled = state.settled;
        const count = settled === undefined
          ? seen.fingerprint === state.fingerprint ? seen.count + 1 : 1
          : settled ? 2 : 1;
        sourceProbeRef.current = { fingerprint: state.fingerprint, count };
        // Keep the action visible during edits; automatic capture requires quiet source.
        setSourceState((current) => state.fingerprint === dismissedSourceFingerprint
          ? null : count >= 2 || current ? state : null);
      }
    }, sourceProbeIntervalMs, (status) => setAccessLost(streamAccessLoss(status)));
  }, [reviewId, authed, overview?.revision.id, overview?.session.range_mode, overview?.session.status, revisionAdvanced, dismissedSourceFingerprint, sourceProbeIntervalMs, historical, reviewEnvironment.review.workspace_refresh]);

  useEffect(() => {
    if (!reviewId || !authed || historical || !startupEnrichmentReady) {
      setProposals([]);
      setSourceMutationSupported(false);
      return;
    }
    let live = true;
    void fetchChangeProposals(reviewId)
      .then((result) => {
        if (!live) return;
        setProposals(result.proposals);
        setSourceMutationSupported(result.source_mutation_supported);
      })
      .catch(() => {
        // Proposal state is additive authoring context. A transient list failure
        // must not block the frozen Review itself.
      });
    return () => {
      live = false;
    };
  }, [reviewId, authed, historical, overview?.revision.id, startupEnrichmentReady]);

  const activeHandoffAddressed = useMemo(() => {
    if (!activeFeedbackHandoff || activeFeedbackHandoff.annotationIds.length === 0) return false;
    const byId = new Map(annotations.map((item) => [item.id, item]));
    return activeFeedbackHandoff.annotationIds.every(
      (id) => byId.get(id)?.author_response === "addressed",
    );
  }, [activeFeedbackHandoff, annotations]);

  useEffect(() => {
    if (!reviewId || !authed || !overview || historical) return;
    let live = true;
    let packetCursor = "";
    let annotationsCursor = "";
    let journalCursor = "";
    let developmentCursor = "";
    let marksCursor = "";
    let primed = false;
    const currentRevisionId = overview.revision.id;
    const loadedPacketCursor = overview.packet_cursor ?? "";

    // The event stream reports a newer Snapshot. The Reader's own capture wakes it
    // too, with the Snapshot the Reader is moving to: not one captured
    // elsewhere. True when it is the Reader's own (this loop then ends; the new
    // Snapshot's loop takes over).
    const ownCapture = async (revisionId: string): Promise<boolean> => {
      const own = ownCaptureRef.current;
      if (own && (await own) === revisionId) return true;
      if (live) reviewDispatch({ type: "revision/advancedSet", advanced: true });
      return false;
    };

    // Judgments recorded in another tab (a dedicated file tab). This Reader's
    // own marks show before the server has them, so its list is replaced only
    // while none is in flight and nothing changed it meanwhile; false asks the
    // reconnected stream to try again.
    const refreshMarks = async (): Promise<boolean> => {
      const before = targetListRef.current;
      if (!before || markingRef.current.size > 0) return false;
      try {
        const next = await fetchTargets(reviewId, before.order);
        if (!live || next.revision_id !== currentRevisionId) return true;
        if (markingRef.current.size > 0 || targetListRef.current !== before) return false;
        // This Reader's own mark, already shown: nothing to take.
        const shown = new Map(before.targets.map((target) => [target.target_id, target.state]));
        if (next.targets.length === shown.size && next.targets.every((target) => shown.get(target.target_id) === target.state)) return true;
        const ordered = stabilizeTargetOrder(next);
        reviewDispatch({ type: "parts/refreshedIfUnchanged", before, next: ordered });
        return true;
      } catch {
        return false;
      }
    };

    const refreshAnnotations = async () => {
      try {
        const result = await fetchAnnotations(reviewId);
        if (!live || result.revision_id !== currentRevisionId) return;
        reviewDispatch({ type: "notes/merged", annotations: result.annotations });
        setFeedbackStatus(result.feedback ?? fallbackFeedbackStatus(result.annotations));
        setFeedbackDelivery(result.delivery ?? null);
      } catch (reason) {
        // Reconnect with a fresh cursor so a failed projection is retried.
        throw reason;
      }
    };

    const refreshJournal = async () => {
      try {
        const next = await fetchReviewJournal(reviewId);
        if (!live || (next.revision_id && next.revision_id !== currentRevisionId)) return;
        setJournal(next);
        setContextFailures((current) => ({ ...current, journal: undefined }));
      } catch (reason) {
        throw reason;
      }
    };

    const refreshDevelopment = async () => {
      try {
        const next = await fetchReviewDevelopment(reviewId);
        if (!live || (next.revision_id && next.revision_id !== currentRevisionId)) return;
        setDevelopment(next);
        setContextFailures((current) => ({ ...current, development: undefined }));
      } catch (reason) {
        throw reason;
      }
    };

    const stopStream = subscribeLiveState(reviewId, async (next) => {
        if (!live) return;
        if (!primed) {
          primed = true;
          // The packet's baseline is the Snapshot the Reader loaded, not the stream's first event: background
          // enrichment (the agent's name, impact) that lands between the two is still taken below.
          packetCursor = loadedPacketCursor || next.packet_cursor;
          annotationsCursor = next.annotations_cursor;
          journalCursor = next.journal_cursor;
          developmentCursor = next.development_cursor ?? "";
          marksCursor = next.marks_cursor ?? "";
        }
        // The initial event may arrive after a background checkpoint won
        // the race with Reader startup: it still compares revision identity,
        // or the UI can miss that newer revision forever.
        const sameRevision = next.revision_id === currentRevisionId;
        const packetChanged = sameRevision && next.packet_cursor !== packetCursor;
        const annotationsChanged = sameRevision && next.annotations_cursor !== annotationsCursor;
        const journalChanged = sameRevision && next.journal_cursor !== journalCursor;
        const developmentChanged = sameRevision && (next.development_cursor ?? "") !== developmentCursor;
        const marksChanged = sameRevision && (next.marks_cursor ?? "") !== marksCursor;

        if (!sameRevision) {
          if (await ownCapture(next.revision_id)) return;
        } else {
          const deferPacketReload = packetChanged
            && targetListRef.current?.complete === false
            && !startupEnrichmentReadyRef.current;
          if (packetChanged && !deferPacketReload) {
            // Background impact/provenance enrichment rewrites only metadata
            // on this exact revision. Rich/full Readers reload it immediately.
            await loadReader(reviewId);
          } else {
            // Compact large-Code mode deliberately keeps its Snapshot-bound
            // source cache stable while packet enrichment advances. The latest
            // full projection is fetched when the reviewer asks for rich
            // context, so packet churn cannot evict source under navigation.
            const [, , , marksTaken] = await Promise.all([
              annotationsChanged ? refreshAnnotations() : Promise.resolve(),
              journalChanged ? refreshJournal() : Promise.resolve(),
              developmentChanged ? refreshDevelopment() : Promise.resolve(),
              marksChanged ? refreshMarks() : Promise.resolve(true),
            ]);
            if (!marksTaken) throw new Error("Review marks are still being saved");
          }
        }
        packetCursor = next.packet_cursor;
        annotationsCursor = next.annotations_cursor;
        journalCursor = next.journal_cursor;
        developmentCursor = next.development_cursor ?? "";
        marksCursor = next.marks_cursor ?? "";
    }, sourceProbeIntervalMs, (status) => {
      if (live) setAccessLost(streamAccessLoss(status));
    });
    return () => {
      live = false;
      stopStream();
    };
  }, [
    reviewId,
    authed,
    historical,
    overview?.revision.id,
    sourceProbeIntervalMs,
    loadReader,
    stabilizeTargetOrder,
  ]);

  const revisionComparisonActive = Boolean(
    comparisonQueryValid
    && !requestedHeadBase
    // `compare=auto` is the Classic Reader's own landing. A flow screen answers for the whole revision: narrowing it to
    // the parts that moved would drop what is already reviewed and what is still unanswered.
    && !(automaticComparison && flowRoute?.known)
    && revisionComparison
    && revisionComparison.code_available
    && revisionComparison.from_revision.revision_number !== revisionComparison.to_revision.revision_number,
  );
  const headComparisonActive = Boolean(requestedHeadBase && sourceComparison);
  const comparisonActive = revisionComparisonActive || headComparisonActive;
  const sourceTargets = useMemo(() => targetList?.targets ?? [], [targetList?.targets]);
  const comparisonFiles = useMemo(
    () => headComparisonActive && sourceComparison
      ? sourceComparison.files
      : revisionComparisonActive && revisionComparison
        ? revisionComparison.code_files
        : [],
    [headComparisonActive, sourceComparison, revisionComparisonActive, revisionComparison],
  );
  const comparisonBase: ReviewCompareBase = headComparisonActive
    ? "head"
    : revisionComparison?.from_revision.revision_number ?? 0;
  const comparisonTargetNumber = headComparisonActive
    ? requestedTargetRevision ?? overview?.revision.revision_number ?? 0
    : revisionComparison?.to_revision.revision_number ?? overview?.revision.revision_number ?? 0;
  // A Snapshot comparison is the delta between two Snapshots: only the review
  // areas whose content differs between them (the server lists exactly those
  // units). An untouched area of a changed file is not part of it; a changed
  // file no listed area covers keeps its file row, so nothing vanishes.
  const comparisonUnitKeys = useMemo(
    () => revisionComparisonActive && revisionComparison?.units
      ? new Set(revisionComparison.units.filter((unit) => unit.status !== "removed").map((unit) => unit.unit_key))
      : null,
    [revisionComparisonActive, revisionComparison],
  );
  const comparisonTargets = useMemo(() => {
    if (!comparisonActive) return [];
    const targetsByPath = new Map<string, ReviewTarget[]>();
    for (const target of sourceTargets) {
      if (comparisonUnitKeys && !comparisonUnitKeys.has(target.unit_key)) continue;
      const rows = targetsByPath.get(target.path) ?? [];
      rows.push(target);
      targetsByPath.set(target.path, rows);
    }
    return comparisonFiles.flatMap((file) => {
      const rows = targetsByPath.get(file.path) ?? [];
      return rows.length > 0
        ? rows
        : [comparisonTarget(file, comparisonBase, comparisonTargetNumber)];
    });
  }, [comparisonActive, comparisonFiles, comparisonBase, comparisonTargetNumber, comparisonUnitKeys, sourceTargets]);
  const comparisonDetails = useMemo(() => {
    const next: Record<string, FileDetail> = {};
    if (!comparisonActive) return next;
    for (const file of comparisonFiles) next[file.path] = comparisonDetail(file);
    return next;
  }, [comparisonActive, comparisonFiles]);
  const targets = comparisonActive ? comparisonTargets : sourceTargets;
  const flowQuick = flowFocus && flowRoute?.route.kind === "one" && (() => {
    const quick = targets.filter((target) => targetAnswer(target).category === "quick_check");
    const requested = flowRoute.route.part;
    if (!requested) return quick.length > 0 && !targets.some((target) => targetAnswer(target).category === "decision");
    return quick.some((target) => partSlug(target, targets) === requested);
  })();

  const fullModelHydrationRef = useRef("");
  useEffect(() => {
    if (!reviewId || historical || !overview || targetList?.complete !== false) return;
    const needsFullModel = view !== "implementation" || Boolean(query.trim()) || flowOverview || flowFocus || flowWrap || flowDone;
    if (!needsFullModel) return;
    const key = `${reviewId}:${overview.revision.id}:${targetList.order}`;
    if (fullModelHydrationRef.current === key) return;
    fullModelHydrationRef.current = key;
    let cancelled = false;
    void Promise.all([
      fetchOverview(reviewId),
      fetchTargets(reviewId, targetList.order),
    ]).then(([hydratedOverview, hydratedTargets]) => {
      if (cancelled) return;
      if (hydratedOverview.revision.id !== overview.revision.id || hydratedTargets.revision_id !== overview.revision.id) return;
      const stableTargets = stabilizeTargetOrder(hydratedTargets);
      reviewDispatch({ type: "overview/set", overview: hydratedOverview });
      reviewDispatch({ type: "parts/set", targetList: stableTargets });
      setActiveTargetId((current) => current && stableTargets.targets.some((target) => target.target_id === current)
        ? current
        : firstActionable(stableTargets.targets)?.target_id ?? "");
    }).catch(() => {
      if (!cancelled && fullModelHydrationRef.current === key) fullModelHydrationRef.current = "";
    });
    return () => {
      cancelled = true;
      if (fullModelHydrationRef.current === key) fullModelHydrationRef.current = "";
    };
  }, [historical, overview, query, reviewId, stabilizeTargetOrder, targetList?.complete, targetList?.order, view, flowOverview, flowFocus, flowWrap]);

  const displayAnnotations = useMemo(() => {
    if (!revisionComparisonActive || !revisionComparison) return annotations;
    const contextual = new Map(revisionComparison.annotations.map((item) => [item.id, item]));
    const targetNumber = revisionComparison.to_revision.revision_number;
    const targetId = revisionComparison.to_revision.id;
    // The comparison is fetched once and cached. Compared up to the latest
    // Snapshot, its comments are only an older copy of the live list, which a
    // resolve or reply refetches, so the live comment wins. Compared up to an
    // earlier Snapshot, its copy is the comment as it stood there.
    const targetIsLatest = targetId === overview?.revision.id;
    const merged = annotations.map((item) => {
      const context = contextual.get(item.id);
      if (context) return targetIsLatest ? { ...context, ...item } : { ...item, ...context };
      if (item.revision_id === targetId) {
        return {
          ...item,
          origin_revision_number: targetNumber,
          relation_to_revision: "originating" as const,
        };
      }
      return item;
    });
    const seen = new Set(merged.map((item) => item.id));
    for (const item of revisionComparison.annotations) {
      if (!seen.has(item.id)) merged.push(item);
    }
    return merged;
  }, [annotations, revisionComparisonActive, revisionComparison, overview?.revision.id]);

  useEffect(() => {
    if (!comparisonActive) return;
    setActiveTargetId((current) => (
      current && comparisonTargets.some((target) => target.target_id === current)
        ? current
        : comparisonTargets[0]?.target_id ?? ""
    ));
  }, [comparisonActive, comparisonTargets]);

  const scopedTargets = useMemo(() => {
    if (!dedicatedItem) return targets;
    if (dedicatedItem.kind === "file") return targets.filter((target) => target.path === dedicatedItem.path);
    const surface = surfaces.find((item) => item.provider === dedicatedItem.provider && item.id === dedicatedItem.id);
    if (!surface) return [];
    const paths = new Set(surface.affected_paths);
    return targets.filter((target) => paths.has(target.path));
  }, [dedicatedItem, surfaces, targets]);
  const annotationSearchText = useMemo(
    () => annotationSearchTextByTarget(scopedTargets, displayAnnotations),
    [scopedTargets, displayAnnotations],
  );
  // Lists fold below-threshold findings behind a disclosure; counts and readiness keep reading displayAnnotations.
  const surfacedAnnotations = useMemo(
    () => markBelowThreshold(displayAnnotations, projection?.triage?.hidden_finding_ids),
    [displayAnnotations, projection?.triage],
  );
  const conceptSearchText = useMemo(() => projectionSearchTextByTarget(projection), [projection]);
  const combinedSearchText = useMemo(() => {
    const merged = new Map(annotationSearchText);
    for (const [targetId, text] of conceptSearchText) {
      merged.set(targetId, [merged.get(targetId) ?? "", text].filter(Boolean).join(" "));
    }
    return merged;
  }, [annotationSearchText, conceptSearchText]);
  const searchedTargets = useMemo(
    () => filterTargets(scopedTargets, query, combinedSearchText),
    [scopedTargets, query, combinedSearchText],
  );
  const deltaActiveIds = useMemo(
    () => new Set(revisionInfo?.target_delta?.active.map((target) => target.target_id) ?? []),
    [revisionInfo],
  );
  const requestScope = extensions?.requestScope ?? null;
  const requestScopeIds = useMemo(() => requestScopeTargetIds(targets, requestScope), [targets, requestScope]);
  // A scope no current target carries (renamed or deleted entries) focuses
  // nothing: the whole change shows rather than an empty queue.
  const requestScopeApplies = requestScopeIds !== null
    && requestScopeIds.size > 0
    && requestScopeIds.size < targets.length
    && !comparisonActive
    && !dedicatedItem;
  const requestScopeFiltering = requestScopeApplies && requestScopeFocus && !query.trim();
  const filteredTargets = useMemo(() => {
    const queue = requestScopeFiltering && requestScopeIds
      ? searchedTargets.filter((target) => requestScopeIds.has(target.target_id))
      : searchedTargets;
    return deltaFocus
      ? queue.filter((target) => deltaActiveIds.has(target.target_id) && targetStillNeedsAttention(target))
      : queue;
  }, [searchedTargets, deltaFocus, deltaActiveIds, requestScopeFiltering, requestScopeIds]);
  const draftPath = draft?.path ?? "";
  const visibleTargets = useMemo(() => {
    // An open composer holds its unsaved body in its own subtree, so a filter
    // that drops the drafted file unmounts the composer and silently destroys
    // the paragraph the reviewer just typed. The drafted file therefore stays
    // in the stream even while nothing in it matches.
    if (!draftPath || filteredTargets.some((target) => target.path === draftPath)) return filteredTargets;
    const kept = new Set(filteredTargets.map((target) => target.target_id));
    return targets.filter((target) => kept.has(target.target_id) || target.path === draftPath);
  }, [targets, filteredTargets, draftPath]);
  const startupOutlineIndex = targetList?.complete === false ? targetList.outline : null;
  const allPathOrder = useMemo(
    () => startupOutlineIndex ? startupOutlineIndex.map((row) => row.path) : orderedPaths(targets),
    [startupOutlineIndex, targets],
  );
  const fileChangeLines = useMemo(
    () => startupOutlineIndex
      ? new Map(startupOutlineIndex.map((row) => [row.path, Math.max(1, row.changed_lines ?? 1)] as const))
      : changeLinesByPath(targets),
    [startupOutlineIndex, targets],
  );
  const visiblePathOrder = useMemo(() => orderedPaths(visibleTargets), [visibleTargets]);
  const navigationPathOrder = visiblePathOrder.length > 0 ? visiblePathOrder : allPathOrder;
  // Page membership follows the complete ordered change, so marking a target or
  // searching never silently moves a file between shared page URLs.
  const pages = useMemo(() => reviewPages(allPathOrder, fileChangeLines), [allPathOrder, fileChangeLines]);
  const pageCount = Math.max(1, pages.length);
  const pageByPath = useMemo(() => new Map(pages.flatMap((paths, index) => paths.map((path) => [path, index + 1] as const))), [pages]);
  const pageNumber = Math.min(requestedPage, pageCount);
  const pagePaths = dedicatedItem ? visiblePathOrder : pages[pageNumber - 1] ?? [];
  const pagePathSet = useMemo(() => new Set(pagePaths), [pagePaths]);
  const pageTargets = useMemo(() => dedicatedItem ? visibleTargets : visibleTargets.filter((target) => pagePathSet.has(target.path)), [dedicatedItem, visibleTargets, pagePathSet]);
  const pageIdentity = JSON.stringify([reviewId, overview?.revision.id, comparisonActive, pageNumber, pagePaths]);
  const loadedPageIdentityRef = useRef("");
  const pendingPagePathRef = useRef("");
  const changePage = useCallback((page: number) => {
    if (draftRef.current) {
      setError("Save or cancel your comment before changing pages.");
      return false;
    }
    setSearchParams((params) => { params.set("p", String(page)); return params; });
    return true;
  }, [setSearchParams]);
  const openPathPage = useCallback((path: string) => {
    if (dedicatedItem) return true;
    const destinationPage = pageByPath.get(path);
    if (!destinationPage || destinationPage === pageNumber) return true;
    if (!changePage(destinationPage)) return false;
    pendingPagePathRef.current = path;
    return true;
  }, [changePage, dedicatedItem, pageNumber, pageByPath]);

  useEffect(() => {
    if (!targetList || dedicatedItem) return;
    if (searchParams.get("p") !== String(pageNumber) && (pageCount > 1 || searchParams.has("p"))) {
      setSearchParams((params) => { params.set("p", String(pageNumber)); return params; }, { replace: true });
    }
  }, [dedicatedItem, pageCount, pageNumber, searchParams, setSearchParams, targetList]);

  useEffect(() => {
    if (!reviewId || !firstReviewableReady || dedicatedItem || busy || revisionAdvanced || loadedPageIdentityRef.current === pageIdentity) return;
    loadedPageIdentityRef.current = pageIdentity;
    backgroundLoadQueueRef.current = [];
    const destination = pendingPagePathRef.current;
    pendingPagePathRef.current = "";
    const destinationIndex = destination ? pagePaths.indexOf(destination) : -1;
    // An explicit cross-page jump owns the landing position. Start that page's
    const initialPagePaths = destinationIndex >= 0 ? pagePaths.slice(destinationIndex) : pagePaths;
    const demandDriven = targetList?.complete === false;
    const defaultPaths = pathsForChangeBudget(
      initialPagePaths,
      fileChangeLines,
      INITIAL_CHANGE_BUDGET,
      INITIAL_MAX_FILES,
    );
    const paths = demandDriven
      ? (destination ? [destination] : defaultPaths.slice(0, 1))
      : defaultPaths;
    // Keep an unsaved composer mounted even when browser Back changes the page.
    const draftPath = draftRef.current?.path;
    if (draftPath && !paths.includes(draftPath)) paths.push(draftPath);
    revealPaths(paths, true);
    setActiveTargetId((current) => pageTargets.some((target) => target.target_id === current)
      ? current : firstActionable(pageTargets)?.target_id ?? "");
    if (!comparisonActive) {
      // The first window was already fetched by loadReader. Page activation
      // must not immediately retry a file that just failed, or its error card
      // can disappear before the reviewer sees or explicitly retries it.
      const missing = paths.filter((path) => !detailsRef.current[path] && !fileErrorsRef.current[path]);
      if (missing.length > 0) void loadPaths(reviewId, missing);
      if (!demandDriven) queueNextPage(reviewId, allPathOrder, fileChangeLines);
    }
  }, [allPathOrder, busy, comparisonActive, dedicatedItem, fileChangeLines, firstReviewableReady, loadPaths, pageIdentity, pagePaths, pagePathSet, pageTargets, queueNextPage, revealPaths, reviewId, revisionAdvanced, targetList?.complete]);
  const activeTarget = useMemo(
    () => visibleTargets.find((target) => target.target_id === activeTargetId) ?? firstActionable(visibleTargets),
    [visibleTargets, activeTargetId],
  );
  const activeDetail = activeTarget
    ? (comparisonActive ? comparisonDetails[activeTarget.path] : details[activeTarget.path]) ?? null
    : null;
  const progress = targetList?.progress ?? progressFromTargets(targets);
  const openRootCommentCount = displayAnnotations.filter(
    (item) => !item.parent_id && item.state !== "resolved" && item.state !== "obsolete",
  ).length;
  useEffect(() => {
    if (!reviewId || !overview || !targetList || historical || (!startupEnrichmentReady && !readinessOpen)) {
      setLandingReadiness(null);
      setLandingReadinessState("idle");
      return;
    }
    let cancelled = false;
    setLandingReadinessState("loading");
    void fetchReviewReadiness(reviewId).then(
      (next) => {
        if (!cancelled) {
          setLandingReadiness(next);
          setLandingReadinessState("ready");
          setLandingError("");
        }
      },
      (reason) => {
        if (!cancelled) {
          setLandingReadiness(null);
          setLandingReadinessState("failed");
          setLandingError(`Merge readiness unavailable: ${String(reason instanceof Error ? reason.message : reason)}`);
        }
      },
    );
    return () => { cancelled = true; };
  }, [
    annotations,
    committedMarks,
    currentOutcome?.id,
    evidence,
    historical,
    overview?.brief?.verification.fail,
    overview?.brief?.verification.not_run,
    overview?.brief?.verification.pass,
    overview?.brief?.verification.unknown,
    // Finishing offers Merge when ready; reopening withdraws an armed one on the server.
    overview?.session.status,
    readinessOpen,
    reviewId,
    startupEnrichmentReady,
    targetList,
    targetList?.progress.changed_since_review,
    targetList?.progress.needs_changes,
    targetList?.progress.reviewed,
    targetList?.progress.unknown,
    targetList?.progress.unreviewed,
    targetList?.progress.later,
  ]);

  const connectedScmBlockers = scmRequirementBlockers(overview?.change?.scm_requirements ?? []);
  const localReadiness = deriveReviewReadiness({
    progress,
    openComments: openRootCommentCount,
    commentsState,
    verification: overview?.brief?.verification ?? { pass: 0, fail: 0, not_run: 0, unknown: 0 },
    evidence: overview?.evidence ?? [],
    staleArtifacts: overview?.brief?.artifacts.stale ?? 0,
    additionalBlockers: [...(extensions?.readinessBlockers ?? []), ...connectedScmBlockers],
    outcome: currentOutcome,
    movement: overview?.movement,
  });
  // Merge when ready is offered only after Finish, so an open review that meets every requirement says so.
  const reviewFinished = overview?.session.status === "finished" || Boolean(landingReadiness?.landing.armed);
  const readiness = landingReadinessState === "ready" && landingReadiness
    ? {
        ...localReadiness,
        ready: landingReadiness.ready,
        label: landingReadiness.ready
          ? landingReadiness.landing.supported
            ? reviewFinished ? "Ready to merge" : "Ready to merge once finished"
            : "Review ready"
          : `${landingReadiness.blocker_count} merge requirement${landingReadiness.blocker_count === 1 ? "" : "s"} unresolved`,
        detail: landingReadiness.ready
          ? landingReadiness.landing.supported
            ? reviewFinished
              ? "All visible merge requirements are satisfied."
              : "All visible merge requirements are satisfied. Finish the review to offer Merge when ready."
            : "The review is complete; automatic merge is unavailable for this Change."
          : landingReadiness.conditions
              .filter((condition) => !SATISFIED_LANDING_STATES.has(condition.state))
              .map((condition) => condition.label)
              .join(" · "),
        blockers: landingReadiness.conditions
          .filter((condition) => !SATISFIED_LANDING_STATES.has(condition.state))
          .map((condition) => ({
            id: condition.id,
            label: condition.label,
            detail: condition.detail,
            tone: condition.state === "fail" ? "rose" as const : "amber" as const,
          })),
      }
    : landingReadinessState === "ready" || historical
      ? localReadiness
      : {
          ...localReadiness,
          ready: false,
          label: landingReadinessState === "failed" ? "Merge readiness unavailable" : "Checking merge readiness",
          detail: landingReadinessState === "failed"
            ? "Merge requirements could not be loaded; readiness stays blocked until they are available."
            : "Loading merge requirements for this revision.",
          blockers: [
            ...localReadiness.blockers,
            {
              id: "landing-readiness",
              label: landingReadinessState === "failed" ? "Merge readiness unavailable" : "Checking merge readiness",
              detail: landingReadinessState === "failed"
                ? "Merge requirements could not be loaded. Retry before treating this Change as ready."
                : "Merge requirements are still loading.",
              tone: "amber" as const,
            },
          ],
        };
  const runningDraft = overview?.revision.capture_kind === "draft" && !overview.revision.is_published_snapshot;
  const forcedReadOnly = Boolean(extensions?.forceReadOnly);
  const sessionLocked = overview?.session.status === "archived" || overview?.session.status === "finished";
  // A finished review takes no new judgments until it is reopened (the primary
  // action says so); feedback handoff stays available after finishing.
  const readOnly = forcedReadOnly || historical || runningDraft || sessionLocked || revisionAdvanced || Boolean(accessLost);
  const handoffRoute: FeedbackRouteKind = feedbackDelivery?.kind
    ?? (feedbackDelivery?.supported ? "agent" : isGitHubPullRequest ? "github" : "export");
  const handoff = handoffCounts(feedbackStatus, feedbackDelivery);

  useEffect(() => {
    if (!activeTarget && visibleTargets.length > 0) setActiveTargetId(firstActionable(visibleTargets)?.target_id ?? "");
  }, [activeTarget, visibleTargets]);

  // A file that is not loaded yet can take seconds on a busy server: say so
  // where the reviewer is looking, and let only the latest navigation scroll
  // once its files land, so an earlier slow one cannot yank the view back.
  // The returned check is true only while this navigation is still the latest.
  const startNavigation = useCallback((paths: readonly string[]) => {
    const navigation = ++navigationRef.current;
    setOpeningPath(paths.find((path) => !detailsRef.current[path]) ?? "");
    return () => {
      if (navigationRef.current !== navigation) return false;
      setOpeningPath("");
      return true;
    };
  }, []);

  const revealNavigationWindow = useCallback((path: string): boolean => {
    const alreadyVisible = Boolean(dedicatedItem) || (pagePathSet.has(path) && revealedPathsRef.current.has(path));
    if (alreadyVisible) {
      revealPaths([path]);
      return true;
    }
    const destinationPage = pageByPath.get(path);
    const destinationPaths = destinationPage ? pages[destinationPage - 1] ?? [] : [path];
    const destinationIndex = Math.max(0, destinationPaths.indexOf(path));
    const windowPaths = pathsForChangeBudget(
      destinationPaths.slice(destinationIndex),
      fileChangeLines,
      INITIAL_CHANGE_BUDGET,
      INITIAL_MAX_FILES,
    );
    if (!windowPaths.includes(path)) windowPaths.unshift(path);
    revealPaths(windowPaths, true);
    return false;
  }, [dedicatedItem, fileChangeLines, pageByPath, pagePathSet, pages, revealPaths]);

  const scrollToFile = useCallback(async (path: string, behavior?: "instant" | "smooth") => {
    if (!reviewId) return;
    if (!openPathPage(path)) return;
    const alreadyVisible = revealNavigationWindow(path);
    if (!alreadyVisible && targetList?.complete !== false) queueNextPage(reviewId, allPathOrder, fileChangeLines);
    const current = startNavigation([path]);
    await loadPath(reviewId, path);
    if (!current()) return;
    window.requestAnimationFrame(() => {
      if (!current()) return;
      const stream = streamRef.current;
      if (stream) stream.scrollToFile(path, behavior);
      else pendingCodeJumpRef.current = { kind: "file", path, behavior, current };
    });
  }, [reviewId, allPathOrder, fileChangeLines, loadPath, queueNextPage, revealNavigationWindow, startNavigation, openPathPage, targetList?.complete]);

  const scrollToTarget = useCallback(async (target: ReviewTarget, behavior: "instant" | "smooth" = "smooth") => {
    if (!reviewId) return;
    if (!openPathPage(target.path)) return;
    const alreadyVisible = revealNavigationWindow(target.path);
    if (!alreadyVisible && targetList?.complete !== false) queueNextPage(reviewId, allPathOrder, fileChangeLines);
    const stream = streamRef.current;
    const effectiveBehavior = alreadyVisible ? behavior : "instant";
    const aimedImmediately = Boolean(
      alreadyVisible
      && stream
      && detailsRef.current[target.path]
      && !staleDetailPathsRef.current.has(target.path),
    );
    if (aimedImmediately) stream?.scrollToTarget(target, effectiveBehavior);
    setActiveTargetId(target.target_id);
    const current = startNavigation([target.path]);
    await loadPath(reviewId, target.path);
    if (!current()) return;
    if (targetList?.complete !== false) {
      const pathIndex = navigationPathOrder.indexOf(target.path);
      if (pathIndex >= 0) {
        const destinationPaths = new Set(pages.find((page) => page.includes(target.path)) ?? []);
        queueBackgroundPaths(
          reviewId,
          navigationPathOrder.slice(pathIndex + 1, pathIndex + 1 + PREFETCH_AHEAD_FILES).filter((path) => destinationPaths.has(path)),
        );
      }
    }
    if (!aimedImmediately) {
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        if (!current()) return;
        const handle = streamRef.current;
        if (handle) handle.scrollToTarget(target, effectiveBehavior);
        else pendingCodeJumpRef.current = { kind: "target", target, behavior: effectiveBehavior, current };
      }));
    }
  }, [reviewId, allPathOrder, fileChangeLines, loadPath, navigationPathOrder, queueBackgroundPaths, queueNextPage, revealNavigationWindow, startNavigation, openPathPage, pages, targetList?.complete]);

  const flowOverviewRef = useRef(false);
  flowOverviewRef.current = flowOverview;
  const lagRetriedRef = useRef(false);
  const loadProjectionRef = useRef<(background?: boolean) => Promise<void>>(async () => undefined);
  const loadProjection = useCallback(async (background = false) => {
    if (!reviewId || !briefCapable) return;
    const request = ++projectionRequestRef.current;
    if (!background) {
      projectionForegroundRef.current = request;
      setBriefPreparation(null);
      setBriefStartedAt(Date.now() / 1000);
      setProjectionState("loading");
    }
    try {
      const next = await withTransientRetry(() => (
        historical
          ? fetchHistoricalProjection(reviewId, historicalRevisionKey)
          : fetchProjection(reviewId)
      ));
      if (!shouldApplyProjection(request, projectionForegroundRef.current, projectionAppliedRef.current)) return;
      // A background completion belongs to the Snapshot that triggered it.
      // The Reader may have advanced while this request was in flight.
      if (background && next.revision_id !== overview?.revision.id) return;
      // The redesigned Overview never takes a plan for another revision as its own.
      if (flowOverviewRef.current && !background && next.revision_id !== overview?.revision.id) {
        setProjectionState("failed");
        // One background retry for the current revision; a server that keeps lagging does not loop.
        if (!lagRetriedRef.current) {
          lagRetriedRef.current = true;
          void loadProjectionRef.current(true);
        }
        return;
      }
      if (next.revision_id === overview?.revision.id) lagRetriedRef.current = false;
      projectionAppliedRef.current = request;
      setProjection(next);
      setProjectionState("ready");
    } catch {
      // A failed foreground load fails the screen unless a newer foreground load replaced it or a plan is already
      // on screen; a background refresh begun behind it must not leave the screen loading forever (P9-6).
      const stillWaiting = () => request === projectionForegroundRef.current && request >= projectionAppliedRef.current;
      if (background || !stillWaiting()) return;
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
      if (!stillWaiting()) return;
      setProjectionState("failed");
      setLandingDecided(true);
    }
  }, [reviewId, briefCapable, historical, historicalRevisionKey, overview?.revision.id]);
  loadProjectionRef.current = loadProjection;

  useEffect(() => {
    if (!reviewId || !briefLandingPending || projectionState !== "loading") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const revisionId = overview?.revision.id;
    async function poll() {
      try {
        const status = await fetchBriefPreparation(reviewId!, historical ? historicalRevisionKey : undefined);
        if (!cancelled && status.revision_id === revisionId) setBriefPreparation(status);
      } catch {
        // Older servers still prepare the Brief; feedback cannot prevent opening code.
      }
      if (!cancelled) timer = setTimeout(poll, 1000);
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [reviewId, briefLandingPending, projectionState, overview?.revision.id, historical, historicalRevisionKey]);

  const overviewRevisionId = overview?.revision.id ?? "";
  useEffect(() => {
    if (!overviewRevisionId || !startupEnrichmentReady) return;
    void loadProjection();
  }, [overviewRevisionId, loadProjection, startupEnrichmentReady]);

  // Marks and comments change the plan's queue; refresh it while Brief or Guided Review is on screen.
  const briefInputsRef = useRef<{ targetList: ReviewTargetList | null; annotations: Annotation[] } | null>(null);
  useEffect(() => {
    const previous = briefInputsRef.current;
    briefInputsRef.current = { targetList, annotations };
    if ((view !== "brief" && view !== "tour") || !previous?.targetList) return;
    if (previous.targetList === targetList && previous.annotations === annotations) return;
    void loadProjection();
  }, [view, targetList, annotations, loadProjection]);

  // The redesigned Overview refreshes the plan in the background, so a passive change (a provider sync, a source
  // recovery, full-model hydration) never puts it back to preparing, and marks made on other screens show on return.
  const overviewInputsRef = useRef<{ targetList: ReviewTargetList | null; annotations: Annotation[] } | null>(null);
  useEffect(() => {
    if (!flowOverview) return;
    const previous = overviewInputsRef.current;
    overviewInputsRef.current = { targetList, annotations };
    if (!previous?.targetList) return;
    if (previous.targetList === targetList && previous.annotations === annotations) return;
    void loadProjection(true);
  }, [flowOverview, targetList, annotations, loadProjection]);

  // Adaptive Auto landing is deterministic from already-loaded Review metadata.
  // Explicit URL/deep-link intent, a dedicated item, return state, comparison,
  // or a remembered reviewer choice wins. No model call chooses navigation.
  useEffect(() => {
    if (!projection || viewChosenRef.current) return;
    viewChosenRef.current = true;
    setLandingDecided(true);
    if (view !== "implementation" || !defaultLanding) return;
    // A redesigned flow screen owns its query (the Map's `view=`); the Reader's own landing must not rewrite it.
    if (onFlowScreen() || flowOverview) return;
    const landing = readStoredView(reviewId)
      ?? (targets.length > LARGE_REVIEW_TARGET_THRESHOLD ? "implementation" : adaptiveReviewLanding(projection, targets, briefCapable));
    if (landing === "implementation") return;
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      params.set(REVIEW_VIEW_PARAM, "brief");
      params.delete(GUIDED_COHORT_PARAM);
      return params;
    }, { replace: true });
  }, [projection, targets, briefCapable, view, defaultLanding, reviewId, setSearchParams, flowOverview]);

  // One path for every failed human write: a stale Snapshot moves the reader to
  // the reload state with an explicit notice instead of a generic error string.
  const reportWriteFailure = useCallback((reason: unknown) => {
    const failure = classifyWriteFailure(reason);
    if (failure.stale) {
      reviewDispatch({ type: "revision/advancedSet", advanced: true });
      setFinishSheetOpen(false);
    }
    setError(failure.message);
  }, []);

  const synthesisPending = Boolean(
    (briefShown && projection?.synthesis?.status === "pending")
    || (tourShown && projection?.tour?.synthesis?.status === "pending"),
  );
  // Keep the Snapshot's AI status fresh even when the reviewer works in Code.
  // When the job finishes, hydrate newly published AI annotations without
  // reloading the Reader, clearing existing diffs, or changing navigation.
  const aiWasPendingRef = useRef<{ revisionId: string; pending: boolean }>({ revisionId: "", pending: false });
  useEffect(() => {
    if (!reviewId || historical || !projection || projection.revision_id !== overviewRevisionId) return;
    const pending = projection.ai_review?.status === "pending";
    const previous = aiWasPendingRef.current;
    aiWasPendingRef.current = { revisionId: projection.revision_id, pending };
    if (pending || !previous.pending || previous.revisionId !== projection.revision_id) return;
    let current = true;
    const revisionId = projection.revision_id;
    void fetchAnnotations(reviewId).then((result) => {
      if (!current || projectionRequestRef.current === 0 || overview?.revision.id !== revisionId) return;
      reviewDispatch({ type: "notes/set", annotations: result.annotations });
      setFeedbackStatus(result.feedback ?? fallbackFeedbackStatus(result.annotations));
      setFeedbackDelivery(result.delivery ?? null);
    }).catch(() => {
      // Optional enrichment does not replace already rendered Review content.
    });
    return () => { current = false; };
  }, [reviewId, historical, projection?.revision_id, projection?.ai_review?.status, overviewRevisionId]);
  // Polling never starts a model job, blocks navigation, or blanks the diff.
  const aiReviewPending = !historical && projection?.ai_review?.status === "pending";
  useEffect(() => {
    if (!synthesisPending && !aiReviewPending) return;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      await loadProjection(true);
      // Keep retrying after transient network/provider failures, but never
      // stack competing requests or poll once the Snapshot settles/unmounts.
      if (!cancelled) timer = window.setTimeout(() => void poll(), SYNTHESIS_POLL_MS);
    };
    timer = window.setTimeout(() => void poll(), SYNTHESIS_POLL_MS);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [synthesisPending, aiReviewPending, projection, loadProjection]);


  // Return from a dedicated tab to the file header that opened it.
  const returnPathShownRef = useRef(false);
  useEffect(() => {
    const path = returnPathRef.current;
    if (dedicatedItem || !path || !firstReviewableReady || returnPathShownRef.current) return;
    if (!targets.some((target) => target.path === path)) return;
    returnPathShownRef.current = true;
    void scrollToFile(path, "instant");
  }, [dedicatedItem, firstReviewableReady, scrollToFile, targets]);

  // A dedicated file tab opened from the Review Map lands on the mapped target.
  const dedicatedTargetShownRef = useRef(false);
  useEffect(() => {
    if (dedicatedItem?.kind !== "file" || !dedicatedItem.targetId || !firstReviewableReady || dedicatedTargetShownRef.current) return;
    const target = targets.find((item) => item.target_id === dedicatedItem.targetId);
    if (!target) return;
    dedicatedTargetShownRef.current = true;
    void scrollToTarget(target, "instant");
  }, [dedicatedItem, firstReviewableReady, scrollToTarget, targets]);

  // Opens the related-source panel before the fetch, so a slow reply shows as
  // loading rather than a dead click. A panel closed or replaced meanwhile
  // keeps the late reply out.
  const showRelatedSource = useCallback(async (path: string, line: number) => {
    if (!reviewId) return;
    const pending: RelatedPanel = { path, line, pending: true };
    setRelated(pending);
    try {
      const source = await (historical
        ? fetchHistoricalRelatedSource(reviewId, historicalRevisionKey, path, line)
        : fetchRelatedSource(reviewId, path, line));
      setRelated((current) => (current === pending ? source : current));
    } catch (reason) {
      setRelated((current) => (current === pending ? null : current));
      setError(String(reason instanceof Error ? reason.message : reason));
    }
  }, [reviewId, historical, historicalRevisionKey]);
  const departure = useCallback((): ReviewBacktrailEntry => {
    const relatedAt = related
      ? { path: related.path, line: "pending" in related ? related.line : related.focus_line || related.start_line || 1 }
      : null;
    const label = view === "brief" ? "Brief"
      : view === "tour" ? "Guided review"
      : view === "map" ? "Map"
      : relatedAt ? `${relatedAt.path}:L${relatedAt.line}`
      : activeTarget ? `${activeTarget.path}${activeTarget.start_line > 0 ? `:L${activeTarget.start_line}` : ""}`
      : "Code";
    return {
      query: backtrailQuery(new URLSearchParams(window.location.search)),
      view,
      target: activeTarget?.target_id ?? "",
      scroll: reviewScrollRoot(view)?.scrollTop ?? 0,
      related: relatedAt,
      label,
    };
  }, [activeTarget, related, view]);
  // Records the departure, then navigates; the trail lands on the URL the
  // navigation produced, so the departure never lists itself.
  const withBacktrail = useCallback((navigate: () => unknown) => {
    const entry = departure();
    const trail = readReviewBacktrail(new URLSearchParams(window.location.search));
    navigate();
    setSearchParams((current) => writeReviewBacktrail(current, pushReviewBacktrail(trail, entry)), { replace: true });
  }, [departure, setSearchParams]);
  const pendingScrollRef = useRef<ReviewBacktrailEntry | null>(null);
  const [scrollRestoreTick] = useState(0);
  useEffect(() => {
    const entry = pendingScrollRef.current;
    if (!entry || entry.view !== view) return;
    let attempts = 0;
    let frame = 0;
    const apply = () => {
      const root = reviewScrollRoot(entry.view);
      if (root) {
        root.scrollTop = entry.scroll;
        if (pendingScrollRef.current === entry) pendingScrollRef.current = null;
        return;
      }
      attempts += 1;
      if (attempts < 30) frame = window.requestAnimationFrame(apply);
    };
    frame = window.requestAnimationFrame(apply);
    return () => window.cancelAnimationFrame(frame);
  }, [view, scrollRestoreTick]);

  // Code Peek's definition/usage jump: the covering in-patch target, else the
  // Snapshot source at that line. Code stays the secondary action.
  const openCodeLocation = useCallback((location: CodeTarget) => {
    const line = location.line;
    const covers = (target: ReviewTarget): boolean => {
      if (target.path !== location.path) return false;
      if (line <= 0) return true;
      const spans = target.spans.filter((span) => span.side === "new");
      return spans.length > 0
        ? spans.some((span) => span.start_line <= line && span.end_line >= line)
        : target.start_line > 0 && target.start_line <= line && target.end_line >= line;
    };
    const covering = targets.find(covers);
    withBacktrail(() => {
      revealImplementation();
      if (covering) void scrollToTarget(covering);
      else void showRelatedSource(location.path, Math.max(1, line));
    });
  }, [revealImplementation, scrollToTarget, showRelatedSource, targets, withBacktrail]);

  const openAskReference = useCallback((reference: ReviewAskReference) => {
    if (reference.map_selection) {
      const selection = reference.map_selection;
      withBacktrail(() => {
        setAskOpen(false);
        setMapLocation(reviewMapLocationFromSelection(selection));
      });
      return;
    }
    const path = reference.path ?? "";
    const referenceStart = reference.start_line ?? 0;
    const referenceEnd = reference.end_line ?? referenceStart;
    const referenceSide = reference.side === "old" ? "old" : "new";
    const coversReference = (target: ReviewTarget): boolean => {
      if (target.path !== path) return false;
      if (reference.symbol && target.symbol && target.symbol !== reference.symbol) return false;
      if (referenceStart <= 0) return true;
      const matchingSpans = target.spans.filter((span) => span.side === referenceSide);
      const spanStart = matchingSpans.length > 0
        ? Math.min(...matchingSpans.map((span) => span.start_line))
        : 0;
      const spanEnd = matchingSpans.length > 0
        ? Math.max(...matchingSpans.map((span) => span.end_line))
        : 0;
      const targetStart = matchingSpans.length > 0 ? spanStart : target.start_line;
      const targetEnd = matchingSpans.length > 0 ? spanEnd : target.end_line;
      return targetStart > 0 && targetStart <= referenceEnd && targetEnd >= referenceStart;
    };
    const exact = reference.target_unit_key
      ? targets.find((target) => target.unit_key === reference.target_unit_key && coversReference(target))
      : undefined;
    const bySymbol = exact ?? (
      reference.symbol
        ? targets.find((target) => target.symbol === reference.symbol && coversReference(target))
        : undefined
    );
    const byRange = bySymbol ?? targets.find(coversReference);
    if (byRange) {
      withBacktrail(() => {
        revealImplementation();
        void scrollToTarget(byRange);
      });
      return;
    }
    if (!reviewId || !path) return;
    withBacktrail(() => {
      revealImplementation();
      if (reference.exactness === "snapshot") void showRelatedSource(path, referenceStart || 1);
      else void scrollToFile(path, "smooth");
    });
  }, [reviewId, revealImplementation, scrollToFile, scrollToTarget, setMapLocation, showRelatedSource, targets, withBacktrail]);

  const switchAskToLatest = useCallback(() => {
    const latest = revisionHistory?.latest_revision_number ?? overview?.revision.revision_number ?? 0;
    const base = revisionHistory?.default_base_revision_number ?? latest;
    changeRevisionComparison(base, latest);
  }, [changeRevisionComparison, overview?.revision.revision_number, revisionHistory]);

  const selectSurface = useCallback(async (surfaceKey: string) => {
    if (!reviewId) return;
    const surface = surfaces.find((candidate) => `${candidate.provider}:${candidate.id}` === surfaceKey);
    if (!surface) return;
    if (!dedicatedItem && pageNumber !== 1 && !changePage(1)) return;
    setQuery("");
    setDeltaFocus(false);
    const paths = surface.affected_paths.slice(0, 4);
    revealPaths(paths);
    const current = startNavigation(paths);
    await Promise.all(paths.map((path) => loadPath(reviewId, path)));
    if (!current()) return;
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      if (current()) streamRef.current?.scrollToSurface(surfaceKey);
    }));
  }, [changePage, dedicatedItem, loadPath, pageNumber, revealPaths, reviewId, startNavigation, surfaces]);

  const returnSurfaceShownRef = useRef(false);
  useEffect(() => {
    const surfaceKey = returnSurfaceRef.current;
    if (dedicatedItem || !surfaceKey || !firstReviewableReady || returnSurfaceShownRef.current) return;
    if (!surfaces.some((surface) => `${surface.provider}:${surface.id}` === surfaceKey)) return;
    returnSurfaceShownRef.current = true;
    void selectSurface(surfaceKey);
  }, [dedicatedItem, firstReviewableReady, selectSurface, surfaces]);

  // Swaps targets in place while a judgment is in flight: shown at once, then
  // reconciled with the server's answer or put back when the write fails.
  // `expected` guards each swap, so a newer local change is never overwritten.
  const swapTargets = useCallback((
    swaps: ReadonlyArray<{ targetId: string; expected: MarkState; next: ReviewTarget }>,
  ) => {
    reviewDispatch({ type: "marks/swapped", swaps });
  }, [reviewDispatch]);

  // Targets with a mark in flight. A mark shows, and advances, before the
  // server answers, so it must not hold the shared busy channel: on a slow
  // server that swallowed the reviewer's next `r` on the area it advanced to.
  const markingRef = useRef(new Set<string>());
  // An attached Review queues comments, judgments and outcomes for hosted
  // (HTTP 202); nothing is applied locally, so the Reader acknowledges the
  // queue instead of showing a local result that hosted may never accept.
  const hostedInfo = overview?.session.hosted;
  const [hostedVersion, setHostedVersion] = useState(0);
  const [hostedNotice, setHostedNotice] = useState("");
  const hostedQueue = useHostedQueue(reviewId, Boolean(hostedInfo), hostedVersion);
  // The "queued for hosted" notice belongs to this review and to actions still waiting: it goes when the review changes, and when
  // the queue reports nothing unresolved.
  useEffect(() => { setHostedNotice(""); }, [reviewId]);
  const queueStatus = hostedQueue.status;
  useEffect(() => {
    if (queueStatus && unresolvedHostedActions(queueStatus).length === 0) setHostedNotice("");
  }, [queueStatus]);
  const acknowledgeQueued = useCallback((notice: string) => {
    setHostedNotice(notice);
    setHostedVersion((value) => value + 1);
    setError("");
  }, []);
  // Bumped on each failed mark so a concurrent mark's success cannot wipe its error.
  const markFailureSeqRef = useRef(0);
  const markTarget = useCallback(async (target: ReviewTarget, state: MarkState, advance = false) => {
    if (!reviewId || readOnly || busy || target.fingerprint_method === "revision_compare") return false;
    if (markingRef.current.has(target.target_id)) return false;
    markingRef.current.add(target.target_id);
    const shown: ReviewTarget = { ...target, state, changed_since_mark: false };
    swapTargets([{ targetId: target.target_id, expected: target.state, next: shown }]);
    const failureSeq = markFailureSeqRef.current;
    // Set when this mark auto-advanced: the target we moved to and the
    // navigation token right after, so a failure can tell whether the reviewer
    // has navigated on their own since.
    let advanced: { id: string; token: number } | null = null;
    if (advance) {
      const projected = visibleTargets.map((item) => item.target_id === target.target_id ? shown : item);
      const next = nextActionableTargetInFile(projected, target)
        ?? stepTarget(projected, target.target_id, 1, { actionableOnly: true });
      if (next && next.target_id !== target.target_id) {
        void scrollToTarget(next);
        advanced = { id: next.target_id, token: navigationRef.current };
      }
    }
    try {
      const result = await postMark(reviewId, target.unit_key, state, overview?.revision.id ?? "");
      activeReviewFriction()?.judgment();
      if (isQueuedWrite(result)) {
        // Queued for hosted, not applied: the local target keeps its own state.
        swapTargets([{ targetId: target.target_id, expected: state, next: target }]);
        acknowledgeQueued(queuedWriteNotice(result));
        return "queued" as const;
      }
      reviewDispatch({ type: "marks/committed", target, result, othersInFlight: markingRef.current.size > 1 });
      if (result.downgraded) setError(result.note);
      else if (markFailureSeqRef.current === failureSeq) setError("");
      return !result.downgraded;
    } catch (reason) {
      markFailureSeqRef.current += 1;
      swapTargets([{ targetId: target.target_id, expected: state, next: target }]);
      reportWriteFailure(reason);
      // Return to the failed target only if the reviewer is still where the
      // auto-advance left them; an independent navigation wins.
      const moved = advanced as { id: string; token: number } | null;
      if (moved && navigationRef.current === moved.token && activeTargetIdRef.current === moved.id) {
        void scrollToTarget(target);
      }
      return false;
    } finally {
      markingRef.current.delete(target.target_id);
    }
  }, [reviewId, readOnly, busy, swapTargets, visibleTargets, scrollToTarget, overview?.revision.id, reportWriteFailure, acknowledgeQueued]);

  // "Confirm all N quick checks": parts unanswered on this revision go through the server's bulk mark, in rounds of its own
  // limit; each round lands in the store as it returns, so a failure part way keeps (and can undo) what was recorded. The
  // few others (a routine change since the reviewer's review) take the single mark, as Quick check's batch does. An attached
  // Review queues judgments for hosted one by one, so it is not offered there.
  const sourceTargetsRef = useRef(sourceTargets);
  sourceTargetsRef.current = sourceTargets;
  const confirmQuickChecks = useCallback(async (parts: readonly ReviewTarget[]): Promise<QuickConfirmResult | null> => {
    if (!reviewId || readOnly || busy || hostedInfo) return null;
    const revisionId = overview?.revision.id ?? "";
    const marked: QuickConfirmResult["marked"] = [];
    const single: ReviewTarget[] = [];
    const skipped: QuickConfirmResult["skipped"] = [];
    const recorded = () => marked.length + single.length > 0;
    for (const round of rounds(parts.filter((part) => part.state === "unreviewed").map((part) => part.unit_key))) {
      try {
        const result = await postBulkReviewed(reviewId, round, revisionId);
        reviewDispatch({ type: "marks/bulkApplied", path: "", result });
        marked.push(...result.marked.map((item) => ({ unit_key: item.unit_key, event_id: item.event_id })));
        skipped.push(...result.skipped);
      } catch (reason) {
        reportWriteFailure(reason);
        return recorded() ? { marked, single, skipped, failed: true } : null;
      }
    }
    for (const part of parts.filter((item) => item.state !== "unreviewed")) {
      const result = await markTarget(part, "reviewed");
      if (result === true) single.push({ ...part, state: "reviewed", changed_since_mark: false });
      else skipped.push({ unit_key: part.unit_key, path: part.path, reason: result === "queued" ? "queued, not applied yet" : "not saved" });
    }
    activeReviewFriction()?.judgment();
    return recorded() || skipped.length > 0 ? { marked, single, skipped } : null;
  }, [reviewId, readOnly, busy, hostedInfo, overview?.revision.id, reviewDispatch, reportWriteFailure, markTarget]);
  const undoQuickChecks = useCallback(async (done: QuickConfirmResult) => {
    if (!reviewId || readOnly || hostedInfo) return false;
    const revisionId = overview?.revision.id ?? "";
    for (const round of rounds(done.marked)) {
      try {
        const result = await undoBulkReviewed(reviewId, round, revisionId);
        reviewDispatch({ type: "marks/bulkUndone", result });
      } catch (reason) {
        reportWriteFailure(reason);
        return false;
      }
    }
    // A part answered by hand since keeps that answer: only one still at the Looks good this click recorded goes back.
    const now = new Map(sourceTargetsRef.current.map((target) => [target.target_id, target]));
    let ok = true;
    for (const part of done.single) {
      const current = now.get(part.target_id);
      if (current?.state === "reviewed" && await markTarget(current, "unreviewed") !== true) ok = false;
    }
    return ok;
  }, [reviewId, readOnly, hostedInfo, overview?.revision.id, reviewDispatch, reportWriteFailure, markTarget]);
  const quickConfirm = useMemo<QuickConfirmActions | undefined>(
    () => (readOnly || hostedInfo ? undefined : { confirm: confirmQuickChecks, undo: undoQuickChecks }),
    [readOnly, hostedInfo, confirmQuickChecks, undoQuickChecks],
  );

  const createAnnotation = useCallback(async (input: AnnotationDraft) => {
    if (!reviewId || readOnly || busy) return false;
    setBusy("comment");
    try {
      const result = await postAnnotation(reviewId, input, overview?.revision.id ?? "");
      activeReviewFriction()?.judgment();
      // The write has succeeded. Clear the composer before any secondary read
      // so a failed refetch can never leave the same text ready to submit twice.
      reviewDispatch({ type: "draft/set", draft: null });
      if (isQueuedWrite(result)) {
        acknowledgeQueued(queuedWriteNotice(result));
        return "queued" as const;
      }
      if (result.target && result.progress) {
        reviewDispatch({
          type: "parts/commentTargetApplied",
          target: result.target,
          progress: result.progress,
          priorities: result.priorities,
          outline: result.outline,
          outlineUpdates: result.outline_updates,
        });
      }
      try {
        const nextAnnotations = await fetchAnnotations(reviewId);
        // A newer Snapshot's discussion never replaces this Snapshot's.
        if (nextAnnotations.revision_id === (overview?.revision.id ?? "")) {
          reviewDispatch({ type: "notes/set", annotations: nextAnnotations.annotations });
          setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
          setFeedbackDelivery(nextAnnotations.delivery ?? null);
        }
        setError("");
      } catch (reason) {
        setError(`Comment saved, but the discussion list is out of date: ${String(reason instanceof Error ? reason.message : reason)}`);
      }
      return true;
    } catch (reason) {
      reportWriteFailure(reason);
      return false;
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, overview?.revision.id, reportWriteFailure, acknowledgeQueued]);

  const refreshFeedbackState = useCallback(async (done = "Published") => {
    if (!reviewId) return;
    try {
      const nextAnnotations = await fetchAnnotations(reviewId);
      reviewDispatch({ type: "notes/set", annotations: nextAnnotations.annotations });
      setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
      setFeedbackDelivery(nextAnnotations.delivery ?? null);
    } catch (reason) {
      setError(`${done}, but the comment list is out of date: ${String(reason instanceof Error ? reason.message : reason)}`);
    }
  }, [reviewId]);

  // Resolving several comments in a row queues each change: a click made while an
  // earlier one is still saving is applied after it, never dropped.
  const annotationStateQueueRef = useRef<Promise<void>>(Promise.resolve());
  const annotationStatePendingRef = useRef(0);
  const setAnnotationState = useCallback((id: string, state: AnnotationState): Promise<void> => {
    if (!reviewId || readOnly || (busy && busy !== "comment-state")) return Promise.resolve();
    const previous = annotations.find((item) => item.id === id)?.state;
    // Show the resolve or reopen at once; the refetch after the last queued change reconciles it.
    const showState = (from: AnnotationState | undefined, to: AnnotationState) => reviewDispatch({ type: "notes/stateShown", id, from, to });
    setBusy("comment-state");
    showState(previous, state);
    annotationStatePendingRef.current += 1;
    const revisionId = overview?.revision.id ?? "";
    const run = async () => {
      let saved = false;
      try {
        await patchAnnotation(id, { state }, revisionId);
        saved = true;
      } catch (reason) {
        if (previous) showState(state, previous);
        reportWriteFailure(reason);
      }
      try {
        // A refetch now would still list the queued comments as open; the last change refetches.
        if (annotationStatePendingRef.current > 1) return;
        const nextAnnotations = await fetchAnnotations(reviewId);
        reviewDispatch({ type: "notes/set", annotations: nextAnnotations.annotations });
        setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
        setFeedbackDelivery(nextAnnotations.delivery ?? null);
        if (saved) setError("");
      } catch (reason) {
        reportWriteFailure(reason);
      } finally {
        annotationStatePendingRef.current -= 1;
        if (annotationStatePendingRef.current === 0) setBusy("");
      }
    };
    const queued = annotationStateQueueRef.current.then(run);
    annotationStateQueueRef.current = queued;
    return queued;
  }, [reviewId, readOnly, busy, annotations, overview?.revision.id, reportWriteFailure]);

  const closeContext = useCallback(() => {
    setContextOpen(false);
    setRelated(null);
    const restore = contextReturnFocusRef.current;
    window.requestAnimationFrame(() => {
      const meaningfulRestore = restore && restore !== document.body && restore !== document.documentElement;
      if (meaningfulRestore && document.contains(restore)) {
        restore.focus();
        return;
      }
      const targetNode = [...document.querySelectorAll<HTMLElement>("[data-review-target-id]")]
        .find((node) => node.dataset.reviewTargetId === activeTargetId);
      const targetControl = targetNode?.querySelector<HTMLButtonElement>("button");
      if (targetControl) {
        targetControl.focus();
        return;
      }
      document.querySelector<HTMLElement>('[aria-current="location"]')?.focus();
    });
  }, [activeTargetId]);

  const openAsk = useCallback((scope: ReviewAskSelection | null = null) => {
    if (!askCapable) return;
    contextReturnFocusRef.current = focusReturnTarget();
    setContextOpen(false);
    setRelated(null);
    setAskScope(scope);
    setAskOpen(true);
  }, [askCapable]);

  const closeAsk = useCallback(() => {
    setAskOpen(false);
    const restore = contextReturnFocusRef.current;
    window.requestAnimationFrame(() => {
      if (restore && restore !== document.body && restore !== document.documentElement && document.contains(restore)) {
        restore.focus();
      }
    });
  }, []);

  const askTarget = useCallback((target: ReviewTarget) => {
    const newSpans = target.spans.filter((span) => span.side === "new");
    const startLine = newSpans.length > 0
      ? Math.min(...newSpans.map((span) => span.start_line))
      : Math.max(0, target.start_line);
    const endLine = newSpans.length > 0
      ? Math.max(...newSpans.map((span) => span.end_line))
      : Math.max(startLine, target.end_line);
    openAsk({
      path: target.path,
      symbol: target.kind === "document_section" ? "" : target.symbol || target.label,
      target_unit_key: target.fingerprint_method === "revision_compare" ? undefined : target.unit_key,
      side: "new",
      start_line: startLine,
      end_line: endLine,
    });
  }, [openAsk]);

  const toggleContextPin = useCallback(() => {
    setContextPinned((current) => {
      const next = !current;
      window.sessionStorage.setItem(CONTEXT_PIN_KEY, next ? "1" : "0");
      return next;
    });
  }, []);
  const refreshReview = useCallback(async (publish = true) => {
    if (!reviewEnvironment.review.workspace_refresh || !reviewId || readOnly || busy) return;
    setBusy("refresh");
    // Between committing the new overview and landing its dependent reads the
    // reader presents a revision it has not loaded. `busy` already blocks every
    // judgment for that window, so nothing is announced while the refresh is
    // still running — only a refresh that dies inside the window leaves the
    // reader stranded and has to say so.
    let stranded = false;
    // The server commits the new Snapshot before this request returns, and the
    // Reader's live-state SSE can hear of it first: it waits for this.
    let settleCapture: (revisionId: string) => void = () => undefined;
    ownCaptureRef.current = new Promise<string>((resolve) => {
      settleCapture = resolve;
    });
    try {
      const previousTarget = activeTarget ?? null;
      const nextOverview = await (publish ? postRefresh(reviewId) : postRefresh(reviewId, false));
      // An unsent human draft is reconciled explicitly below. Transfer its
      // persistence ownership before the new Snapshot renders, otherwise the
      // revision-loading effect would read an empty new-Snapshot slot and wipe
      // the active composer before we can decide whether its anchor survived.
      if (draft && draft.mode !== "proposal") {
        const previousDraftRevision = draftRevisionRef.current;
        if (previousDraftRevision && previousDraftRevision !== nextOverview.revision.id) {
          saveCommentDraft(reviewId, previousDraftRevision, null);
        }
        draftRevisionRef.current = nextOverview.revision.id;
      }
      // The server is on the new revision the moment postRefresh resolves, so
      // the new revision is committed here, ahead of the dependent reads that
      // can still fail.
      reviewDispatch({ type: "overview/set", overview: nextOverview });
      settleCapture(nextOverview.revision.id);
      // Nothing an earlier load requested for the old Snapshot may land now;
      // this Snapshot's own agent context, Development and surfaces follow.
      const capture = ++readerLoadRef.current;
      const isCurrentCapture = () => readerLoadRef.current === capture;
      fileCacheRevisionRef.current = nextOverview.revision.id;
      setSurfaces([]);
      loadSnapshotContext(fetchReviewJournal(reviewId), fetchReviewDevelopment(reviewId), isCurrentCapture);
      if (!comparisonSelectionPinnedRef.current) {
        const targetRevision = nextOverview.default_target_revision_number
          ?? nextOverview.latest_revision_number
          ?? nextOverview.revision.revision_number;
        const baseRevision = nextOverview.default_base_revision_number ?? targetRevision;
        setSearchParams((current) => {
          const params = new URLSearchParams(current);
          params.set("compare", "auto");
          params.set("base", String(baseRevision));
          params.set("target", String(targetRevision));
          return params;
        }, { replace: true });
      }
      // A refresh advances the request generation globally so an in-flight N
      // response can never win after N+1. Speculative work from the old target
      // order is discarded too; user-triggered reads for the new revision may
      // start immediately while an already-running old request winds down.
      backgroundLoadQueueRef.current = [];
      loadGenerationRef.current += 1;
      const fileDelta = nextOverview.refreshed;
      const hasFileDelta = Array.isArray(fileDelta?.preserved_paths);
      const preservedPaths = new Set(fileDelta?.preserved_paths ?? []);
      const retainedDetails: Record<string, FileDetail | undefined> = {};
      const staleDetailPaths = new Set<string>();
      if (hasFileDelta) {
        for (const [path, detail] of Object.entries(detailsRef.current)) {
          if (!detail || !preservedPaths.has(path)) continue;
          retainedDetails[path] = frozenPatchDetail(detail);
          staleDetailPaths.add(path);
        }
      }
      detailsRef.current = retainedDetails;
      staleDetailPathsRef.current = staleDetailPaths;
      setDetails(retainedDetails);
      // Fetch errors are transient rather than frozen review evidence; retry
      // them on the new revision even when the underlying patch was preserved.
      setFileErrors({});
      inFlightPaths.current.clear();
      // A draft is safe to keep attached only when the exact file bytes it
      // anchors to survive. Otherwise retain the human's text but detach the
      // anchor: only a new human selection may attach it to the new revision.
      const draftSurvives = Boolean(draftPath && hasFileDelta && preservedPaths.has(draftPath));
      if (draft && !draftSurvives) {
        reviewDispatch({ type: "draft/set", draft: {
          ...draft,
          startLine: 0,
          endLine: 0,
          fileLevel: false,
          targetUnitKey: undefined,
          recovered: true,
        } });
      }
      setSourceState(null);
      setDismissedSourceFingerprint("");
      stranded = true;
      const [nextTargets, nextAnnotations, nextEvidence] = await Promise.all([
        fetchTargets(reviewId, targetList?.order ?? "recommended"),
        fetchAnnotations(reviewId),
        fetchReviewEvidence(reviewId, nextOverview.revision.id),
      ]);
      stableTargetRevisionRef.current = nextTargets.revision_id;
      stableTargetOrderRef.current = nextTargets.targets.map((target) => target.target_id);
      reviewDispatch({ type: "parts/set", targetList: nextTargets });
      reviewDispatch({ type: "notes/set", annotations: nextAnnotations.annotations });
      setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
      setEvidence(nextEvidence.evidence);
      loadSurfaces(reviewId, nextOverview.revision.id, nextTargets.targets, isCurrentCapture);
      stranded = false;
      const nextInfo = nextOverview.refreshed ?? null;
      reviewDispatch({ type: "revision/infoSet", info: nextInfo?.created ? nextInfo : null });
      const nextDelta = nextInfo?.target_delta;
      const focusDelta = Boolean(nextInfo?.created && nextDelta && nextDelta.active.length > 0);
      setDeltaFocus(focusDelta);
      const survivor = bestTargetAfterRevision(previousTarget, nextTargets.targets, nextDelta);
      setActiveTargetId(survivor?.target_id ?? "");
      const navigation = navigationRef.current;
      const activeIds = new Set(nextDelta?.active.map((target) => target.target_id) ?? []);
      const queueTargets = focusDelta
        ? nextTargets.targets.filter((target: ReviewTarget) => activeIds.has(target.target_id))
        : nextTargets.targets;
      const paths = initialPaths(queueTargets);
      if (survivor && !paths.includes(survivor.path)) paths.unshift(survivor.path);
      const uniquePaths = [...new Set(paths)];
      revealPaths(uniquePaths);
      // Changed/new files must land before judgments unlock. Preserved files
      // already have their exact frozen patch mounted, so revision-scoped
      // metadata can refresh in the background without blanking the reader.
      await loadPaths(reviewId, uniquePaths.filter((path) => !preservedPaths.has(path)));
      void loadPaths(reviewId, uniquePaths.filter((path) => preservedPaths.has(path)));
      queueNextPage(reviewId, orderedPaths(nextTargets.targets), changeLinesByPath(nextTargets.targets));
      if (survivor) {
        window.requestAnimationFrame(() => {
          if (navigationRef.current === navigation) streamRef.current?.scrollToTarget(survivor, "instant");
        });
      }
      setError(draftPath && !draftSurvives
        ? `Unsaved comment from ${draftPath} was preserved, but its old anchor changed. Select a new line to reattach it.`
        : "");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
      // A reader that keeps presenting the superseded revision as current lets
      // the reviewer approve code they never saw: the mark is recorded against
      // the new revision's units either way.
      if (stranded) reviewDispatch({ type: "revision/advancedSet", advanced: true });
    } finally {
      settleCapture("");
      ownCaptureRef.current = null;
      setBusy("");
    }
  }, [reviewEnvironment.review.workspace_refresh, reviewId, readOnly, busy, activeTarget, draft, draftPath, loadPaths, loadSnapshotContext, loadSurfaces, queueNextPage, revealPaths, targetList?.order]);

  useEffect(() => {
    if (!activeFeedbackHandoff || !overview) return;
    if (activeFeedbackHandoff.revisionId !== overview.revision.id) {
      // The exact handoff belonged to the revision we just advanced from. A
      // later unrelated edit must not inherit its auto-refresh permission.
      setActiveFeedbackHandoff(null);
      feedbackAutoRefreshFingerprintRef.current = "";
      return;
    }
    if (
      !activeHandoffAddressed
      || !sourceState?.changed
      || sourceProbeRef.current.count < 2
      || sourceProbeRef.current.fingerprint !== sourceState.fingerprint
      || historical
      || readOnly
      || busy
      || !reviewEnvironment.review.workspace_refresh
    ) return;
    if (feedbackAutoRefreshFingerprintRef.current === sourceState.fingerprint) return;

    // The source probe already required the same fingerprint twice. Waiting for
    // every annotation in this exact handoff to carry an author `addressed`
    // claim adds the second safety gate: do not checkpoint a half-written fix.
    feedbackAutoRefreshFingerprintRef.current = sourceState.fingerprint;
    void refreshReview(false);
  }, [
    activeFeedbackHandoff,
    activeHandoffAddressed,
    overview?.revision.id,
    sourceState,
    historical,
    readOnly,
    busy,
    reviewEnvironment.review.workspace_refresh,
    refreshReview,
  ]);

  const reloadReader = useCallback(async () => {
    if (!reviewId || busy) return;
    setBusy("reload");
    // An unsent comment follows the reviewer onto the Snapshot they reload to.
    // The revision-loading effect consumes this once the new overview commits,
    // which can be after loadReader settles.
    reloadCarriesDraftRef.current = overview?.revision.id ?? "";
    try {
      // Only a load that actually describes the current revision may hand the
      // reviewer their judgments back.
      if (await loadReader(reviewId)) reviewDispatch({ type: "revision/advancedSet", advanced: false });
    } finally {
      setBusy("");
    }
  }, [reviewId, busy, loadReader, overview?.revision.id]);

  useEffect(() => {
    setOutcomeOverride(null);
  }, [overview?.revision.id]);

  /**
   * Hand prepared feedback to its destination and reflect the result. Throws
   * when the server refuses; a delivery that did not leave comes back as its
   * state for the caller to judge.
   */
  const handOff = useCallback(async (prepared: FeedbackExport, options: { copied?: boolean } = {}): Promise<FeedbackDelivery> => {
    const nextDelivery = options.copied
      ? await deliverFeedback(reviewId, prepared, { copied: true })
      : await deliverFeedback(reviewId, prepared);
    setDelivery(nextDelivery);

    if (HANDED_OFF_STATES.has(nextDelivery.state)) {
      const handedOffCount = nextDelivery.annotation_count;
      const consumed = nextDelivery.state === "sent" || nextDelivery.state === "consumed";
      const optimisticStatus = (current: FeedbackStatus): FeedbackStatus => ({
        ...current,
        unpublished: Math.max(0, current.unpublished - handedOffCount),
        published: current.published + (consumed ? handedOffCount : 0),
        in_flight: current.in_flight + (consumed ? 0 : handedOffCount),
      });
      setFeedbackStatus(optimisticStatus);
      setFeedback((current) => current
        ? { ...current, status: optimisticStatus(current.status ?? feedbackStatus) }
        : current);
      // Only an exact agent session earns the auto-capture hold; feedback
      // copied for a person leaves the next Snapshot to the reviewer.
      if (handoffToCodingAgent(prepared.delivery)) setActiveFeedbackHandoff({
        revisionId: prepared.revision_id,
        operationId: nextDelivery.operation_id || prepared.operation_id,
        annotationIds: Object.keys(prepared.annotation_versions),
        destination: handoffDestinationLabel(prepared.delivery),
      });
    }

    try {
      const nextAnnotations = await fetchAnnotations(reviewId);
      const nextFeedbackStatus = nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations);
      const nextFeedbackDelivery = nextAnnotations.delivery ?? prepared.delivery;
      reviewDispatch({ type: "notes/set", annotations: nextAnnotations.annotations });
      setFeedbackStatus(nextFeedbackStatus);
      setFeedbackDelivery(nextFeedbackDelivery);
      setFeedback((current) => current
        ? { ...current, status: nextFeedbackStatus, delivery: nextFeedbackDelivery }
        : current);
      setError("");
    } catch (reason) {
      setError(
        nextDelivery.state === "consumed"
          ? `Feedback was consumed, but the review state could not refresh: ${String(reason instanceof Error ? reason.message : reason)}`
          : nextDelivery.state === "sent"
          ? `Feedback was sent, but the review state could not refresh: ${String(reason instanceof Error ? reason.message : reason)}`
          : nextDelivery.state === "queued" || nextDelivery.state === "working"
            ? `Feedback handoff is in progress, but the review state could not refresh: ${String(reason instanceof Error ? reason.message : reason)}`
            : String(reason instanceof Error ? reason.message : reason),
      );
    }
    return nextDelivery;
  }, [reviewId, feedbackStatus]);

  const confirmFinish = useCallback(async (
    outcome: ReviewOutcomeKind,
    summary: string,
    options: FinishOptions = { send: false },
  ) => {
    if (!reviewId || busy || revisionAdvanced || historical) return;
    // Before the first await, while the click still counts as the reviewer's gesture.
    const pendingCopy = options.send && handoffRoute === "export" && handoff.unsent > 0 ? clipboardWriteInGesture() : null;
    setBusy("finish");
    setFinishSendError("");
    setFinishError("");
    let stage: "outcome" | "send" | "finish" = "outcome";
    let verified: Awaited<ReturnType<typeof exportFeedback>> | null = null;
    try {
      // For Wrap up, verify the exact human-approved message before recording any verdict.
      // A stale preview must not leave a new verdict behind when delivery is refused.
      if (options.send && options.presentation && options.expectedFeedbackHash && handoffRoute !== "github") {
        const current = await exportFeedback(reviewId, {
          expectedRevisionId: overview?.revision.id,
          presentation: options.presentation,
        });
        if (current.feedback_hash !== options.expectedFeedbackHash) {
          setFinishError("Feedback changed since the Wrap up preview. Nothing recorded or sent; review the updated message.");
          return;
        }
        verified = current;
      }
      // Attached, the verdict that counts is hosted's (its projection), never a
      // local one recorded before attaching: only hosted's current verdict on
      // its current Snapshot, with the same summary, needs no new write.
      const hostedProjection = hostedInfo && !Array.isArray(hostedQueue.status?.projection)
        ? hostedQueue.status?.projection
        : undefined;
      const hostedMine = hostedProjection?.my_outcome;
      const sameCurrentOutcome = hostedInfo
        ? Boolean(hostedMine
          && hostedMine.revision_id === hostedProjection?.snapshot?.id
          && hostedMine.outcome === outcome
          && typeof hostedMine.summary === "string"
          && hostedMine.summary === summary)
        : Boolean(currentOutcome
          && !currentOutcome.stale
          && currentOutcome.outcome === outcome
          && currentOutcome.summary === summary);
      if (sameCurrentOutcome && hostedInfo) {
        setFinishError(
          `Your verdict is already on hosted ${hostedQueue.status?.reference ?? hostedInfo.reference}. Finishing is online-only: finish on the hosted Review.`,
        );
        return;
      }
      if (!sameCurrentOutcome) {
        if (extensions?.recordOutcome) {
          const recorded = await extensions.recordOutcome(outcome, summary, overview?.revision.id ?? "");
          setOutcomeOverride(recorded);
        } else {
          const recorded = await postReviewOutcome(reviewId, outcome, summary, overview?.revision.id ?? "");
          if (isQueuedWrite(recorded)) {
            // Finishing is online-only on an attached Review: hosted owns status.
            acknowledgeQueued(queuedWriteNotice(recorded));
            setFinishError(
              `Your verdict is queued for hosted ${recorded.reference}. Finishing is online-only: run \`lc review hosted sync\`, then finish on the hosted Review.`,
            );
            return;
          }
          setLocalOutcome(recorded.current);
          setOutcomeOverride(recorded.current);
        }
        activeReviewFriction()?.judgment();
      }
      const requestChanges = outcome === "changes_requested";
      if (handoffRoute === "github" && (options.send || requestChanges)) {
        // GitHub has its own preview. Request changes stays open waiting on the
        // PR; LGTM and Comment finish once the publish lands.
        setFinishAfterPublish(!requestChanges);
        setFinishSheetOpen(false);
        setGithubPublishOpen(true);
        setError("");
        return;
      }
      if (options.send) {
        stage = "send";
        const prepared = verified ?? await exportFeedback(reviewId, options.presentation ? { expectedRevisionId: overview?.revision.id, presentation: options.presentation } : undefined);
        if (options.expectedFeedbackHash && prepared.feedback_hash !== options.expectedFeedbackHash) {
          setFinishSendError("Feedback changed since the preview. Review the updated message before sending.");
          return;
        }
        setFeedback(prepared);
        if (prepared.status) setFeedbackStatus(prepared.status);
        setFeedbackDelivery(prepared.delivery);
        setDelivery(null);
        if ((prepared.status?.unpublished ?? prepared.open + prepared.orphaned) > 0) {
          const copied = feedbackRoute(prepared) === "export";
          if (copied && pendingCopy) {
            pendingCopy.fill(prepared.markdown);
            await pendingCopy.written;
          } else if (copied) {
            await copyToClipboard(prepared.markdown);
          }
          const result = await handOff(prepared, { copied });
          if (!HANDED_OFF_STATES.has(result.state)) {
            setFinishSendError(result.message || `delivery ${result.state}`);
            return;
          }
        }
      }
      if (requestChanges) {
        // Request changes never finishes: the Review waits on the fix, and the
        // next Snapshot comes back to it for re-review.
        setFinishRecorded(outcome);
        setError("");
        return;
      }
      stage = "finish";
      const next = await finishReview(reviewId, "finished", overview?.revision.id ?? "");
      setFinishSummary(next);
      setFinishRecorded(outcome);
      reviewDispatch({ type: "overview/sessionStatusSet", status: next.status });
      setError("");
    } catch (reason) {
      const failure = classifyWriteFailure(reason);
      if (stage === "send") setFinishSendError(String(reason instanceof Error ? reason.message : reason));
      // A newer Snapshot closes the sheet on the page's reload notice. Any
      // other failure stays in the open sheet, whose overlay covers the page
      // strip; its record button is the retry, which reuses a recorded verdict.
      else if (failure.stale) reportWriteFailure(reason);
      else setFinishError(stage === "finish"
        ? `Your verdict is recorded, but the Review was not finished: ${failure.message}`
        : `Nothing was recorded: ${failure.message}`);
    } finally {
      pendingCopy?.fill(null);
      setBusy("");
    }
  }, [reviewId, busy, revisionAdvanced, historical, currentOutcome, extensions, handoffRoute, handoff.unsent, handOff, overview?.revision.id, reportWriteFailure, acknowledgeQueued, hostedInfo, hostedQueue.status]);

  /** Finish an LGTM or Comment once its GitHub publish landed. */
  const finishPublished = useCallback(async () => {
    setFinishAfterPublish(false);
    if (!reviewId) return;
    try {
      const next = await finishReview(reviewId, "finished", overview?.revision.id ?? "");
      setFinishSummary(next);
      reviewDispatch({ type: "overview/sessionStatusSet", status: next.status });
    } catch (reason) {
      reportWriteFailure(reason);
    }
  }, [reviewId, overview?.revision.id, reportWriteFailure]);

  const uploadEvidence = useCallback(async (files: File[]) => {
    // Joins the shared busy channel on the same terms as every other action:
    // taking the lock without checking it first would clobber a mark or a bulk
    // review that is still in flight and then release their lock early.
    if (!reviewId || !activeTarget || readOnly || busy) return;
    setBusy("evidence");
    const failures: string[] = [];
    try {
      for (const file of files) {
        try {
          await uploadReviewEvidence(reviewId, file, activeTarget.path, overview?.revision.id ?? "");
        } catch (reason) {
          failures.push(`${file.name}: ${String(reason instanceof Error ? reason.message : reason)}`);
        }
      }
      setEvidence((await fetchReviewEvidence(reviewId, overview?.revision.id ?? "")).evidence);
    } catch (reason) {
      failures.push(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
    // A clean upload has nothing to say about the rest of the reader, and the
    // banner it would clear is usually a message the reviewer is still acting
    // on, so only failures speak here.
    if (failures.length > 0) setError(failures.join(" · "));
  }, [reviewId, activeTarget, readOnly, busy, overview?.revision.id]);

  const addPreview = useCallback(async (url: string, title: string) => {
    // The sibling of the drop zone in the same evidence tab, and a write like
    // it: it joins the shared busy channel on the same terms rather than
    // clobbering a mark or a bulk review still in flight.
    if (!reviewId || !activeTarget || readOnly || busy) return;
    setBusy("evidence");
    try {
      await linkReviewEvidence(reviewId, url, title, activeTarget.path, overview?.revision.id ?? "");
      setEvidence((await fetchReviewEvidence(reviewId, overview?.revision.id ?? "")).evidence);
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, activeTarget, readOnly, busy, overview?.revision.id]);

  const openImpact = useCallback(async (site: import("./types").ImpactSite) => {
    if (!reviewId) return;
    const match = /^(.*):L(\d+)$/.exec(site.path);
    const path = match?.[1] ?? site.path;
    const line = Number(match?.[2] ?? 1);
    const inPatch = targets.find((target) => target.path === path);
    if (site.in_patch && inPatch) {
      withBacktrail(() => {
        void scrollToTarget(inPatch).then(() => setContextOpen(false));
      });
      return;
    }
    withBacktrail(() => { void showRelatedSource(path, line); });
  }, [reviewId, targets, scrollToTarget, showRelatedSource, withBacktrail]);

  // Code shows the working tree, so the link exists only with a trusted local
  // checkout. Back from Code returns to this exact Review location: the Map
  // where it stood, or the opened file in the stream.
  const codeRoot = reviewEnvironment.review.local_workspace === false ? "" : overview?.workspace_root ?? "";
  // Code and Code Peek belong to the Runtime product: a Review-only install,
  // or one whose discovery is still pending, offers neither and never calls
  // /v1/code-map (isolation §25). Open in Code needs only the deterministic
  // map, which a local Dashboard serves; Code Peek needs precise references
  // and stays gated on runtime.code inside ContextDrawer.
  const runtimeCodeRoot = productCapabilities?.features["runtime.code_map"] ? codeRoot : "";
  const openInCode = useCallback((target: CodeTarget) => {
    const back = new URL(currentReturnPath(), window.location.origin);
    if (!readReviewMapLocation(back.searchParams)) back.searchParams.set("review-return-path", target.path);
    window.location.href = codeHref(codeRoot, target, `${back.pathname}${back.search}`);
  }, [codeRoot]);

  // The patch target a Map selection stands for: its own targets, else its file's first.
  const mapSelectionTarget = useCallback((selection: ReviewMapSelection): ReviewTarget | null => {
    if (selection.exactness !== "snapshot") return null;
    const own = (selection.target_ids ?? [])
      .map((targetId) => targets.find((item) => item.target_id === targetId))
      .find(Boolean);
    if (own) return own;
    const path = selection.path ?? "";
    return path ? targets.find((item) => item.path === path) ?? null : null;
  }, [targets]);

  // Selection carries across views. The Map reports what it has selected; the
  // last Map location is kept with the stream file it was left at, so coming
  // back from the same file restores it and coming back from another file
  // opens the Map on the change with that file selected.
  const mapSelectionRef = useRef<ReviewMapSelection | null>(null);
  const lastMapRef = useRef<{ location: ReviewMapLocation; path: string } | null>(null);
  const changeView = useCallback((next: ReviewView) => {
    viewChosenRef.current = true;
    setLandingDecided(true);
    if (reviewId && (next === "brief" || next === "implementation")) storeView(reviewId, next);
    // Small/fully hydrated Reviews can retry a failed projection immediately.
    // Deferred large Reviews cross the startup-demand gate by changing view;
    // that effect owns the single projection request.
    if ((next === "brief" || next === "tour") && startupEnrichmentReady) void loadProjection();
    if (next === view) return;
    const activePath = activeTarget?.path ?? "";
    let landing: ReviewTarget | null = null;
    if (view === "map") {
      const selection = mapSelectionRef.current;
      landing = next === "implementation" && selection ? mapSelectionTarget(selection) : null;
      if (mapLocation) lastMapRef.current = { location: mapLocation, path: landing?.path ?? activePath };
    }
    const last = lastMapRef.current;
    const mapNext = next !== "map"
      ? null
      : last && last.path === activePath ? last.location : { ...REVIEW_MAP_OVERVIEW, path: activePath };
    setSearchParams((current) => {
      const params = writeReviewMapLocation(new URLSearchParams(current), mapNext);
      if (next === "brief" || next === "tour") params.set(REVIEW_VIEW_PARAM, next);
      else params.delete(REVIEW_VIEW_PARAM);
      if (next !== "tour") params.delete(GUIDED_COHORT_PARAM);
      return params;
    });
    if (landing && landing.path !== activePath) void scrollToTarget(landing);
  }, [activeTarget?.path, loadProjection, mapLocation, mapSelectionTarget, reviewId, scrollToTarget, setSearchParams, startupEnrichmentReady, view]);
  changeViewRef.current = changeView;

  // The Brief leaves for Implementation before navigating, like the Map.
  const openBriefTarget = useCallback((targetId: string) => {
    const target = targets.find((item) => item.target_id === targetId);
    withBacktrail(() => {
      changeView("implementation");
      if (target) void scrollToTarget(target);
    });
  }, [changeView, scrollToTarget, targets, withBacktrail]);

  // Brief §5.1 Architecture Delta: loaded once per Snapshot, never for a tiny
  // mechanical change, and opened in the Map at the moved component.
  const architectureCacheRef = useRef(new Map<string, Promise<ReviewArchitectureDelta | null>>());
  const architectureRevisionNumber = overview?.revision.revision_number ?? -1;
  const loadArchitectureDelta = useCallback((): Promise<ReviewArchitectureDelta | null> => {
    if (!reviewId || architectureRevisionNumber < 0) return Promise.resolve(null);
    const cache = architectureCacheRef.current;
    const key = `${reviewId}:${architectureRevisionNumber}`;
    const cached = cache.get(key);
    if (cached) return cached;
    const request = fetchHistoricalReviewMap(reviewId, architectureRevisionNumber, "architecture", "", "architecture_delta")
      .then((payload) => payload.architecture_delta ?? null);
    // A failed load is retried on the next Brief, not remembered.
    request.catch(() => { if (cache.get(key) === request) cache.delete(key); });
    cache.set(key, request);
    while (cache.size > ARCHITECTURE_DELTA_CACHE_LIMIT) {
      const oldest = cache.keys().next().value;
      if (oldest === undefined) break;
      cache.delete(oldest);
    }
    return request;
  }, [architectureRevisionNumber, reviewId]);
  const architectureRelevant = projection !== null && !isTinyMechanicalReview(projection, targets);
  // Friction counts: at most one Map open and one wait per Review opened in this page.
  const mapOpenCounted = useRef("");
  useEffect(() => {
    if (!reviewId || !projection || !(flowMap || flowXRay) || mapOpenCounted.current === reviewId) return;
    mapOpenCounted.current = reviewId;
    countMapOpen(architectureRelevant);
  }, [architectureRelevant, flowMap, flowXRay, projection, reviewId]);
  const waitCounted = useRef("");
  const reviewLoaded = Boolean(overview);
  useEffect(() => {
    if (!reviewId || !reviewLoaded || historical || waitCounted.current === reviewId) return;
    waitCounted.current = reviewId;
    countWaiting(reviewId);
  }, [historical, reviewId, reviewLoaded]);

  // Decisions are human-only writes bound to the displayed Snapshot and the newest event the reviewer saw; the server
  // refuses stale ones. Request change files ordinary feedback first, so the request is the queue action.
  const decideOnBrief = useCallback(async (key: string, input: ReviewDecisionEventInput) => {
    if (!reviewId || !projection) return;
    const decision = projection.decisions.find((item) => item.key === key);
    let feedbackPath: string | undefined;
    if (input.action === "request_change") {
      const target = targets.find((item) => item.target_id === decision?.target_ids[0]);
      if (!target) throw new Error("This decision has no part to attach the request to.");
      feedbackPath = target.path;
    }
    try {
      await postDecisionEvent(reviewId, key, {
        action: input.action,
        option_key: input.option_key,
        reason: input.reason,
        trigger: input.trigger,
        feedback_body: input.action === "request_change" ? input.feedback_body ?? "" : undefined,
        feedback_path: feedbackPath,
        expected_revision_id: projection.revision_id,
        expected_event_id: decision?.state?.latest_event_id ?? "",
        expected_semantic_fingerprint: decision?.state?.semantic_fingerprint ?? "",
      });
      activeReviewFriction()?.judgment();
    } catch (reason) {
      // A newer Snapshot moves the whole Reader to its reload state; the card still shows its own refusal.
      if (isStaleSnapshotError(reason)) reportWriteFailure(reason);
      throw reason;
    } finally {
      // Refused or not, show what is true now; a refresh failure never replaces the write's own error.
      await loadProjection().catch(() => undefined);
    }
  }, [reviewId, projection, targets, loadProjection, reportWriteFailure]);

  const loadBehaviorTarget = useCallback(async (behaviorKey: string) => {
    if (!reviewId || !projection?.revision_id || projection.revision_id !== overviewRevisionId) {
      throw new Error("Behavior evidence is unavailable until the current revision is loaded.");
    }
    const result = await fetchBehaviorTarget(reviewId, behaviorKey);
    if (result.revision_id !== projection.revision_id || result.target.behavior_key !== behaviorKey) {
      throw new Error("Behavior evidence belongs to a different revision or part. Refresh the Review before judging.");
    }
    return result.target;
  }, [reviewId, projection?.revision_id, overviewRevisionId]);

  // The digest is the one of the evidence block on screen; the server refuses any other.
  const judgeBehavior = useCallback(async (behaviorKey: string, state: BehaviorJudgmentState, evidenceDigest: string) => {
    if (!reviewId || !projection) return;
    await postBehaviorJudgment(reviewId, projection.revision_id, { behavior_key: behaviorKey, state, evidence_digest: evidenceDigest });
    activeReviewFriction()?.judgment();
    await loadProjection();
  }, [reviewId, projection, loadProjection]);

  const narrateBehavior = useCallback(async () => {
    if (!reviewId || !projection) return;
    try {
      await postBehaviorNarrative(reviewId, projection.revision_id);
    } finally {
      await loadProjection().catch(() => undefined);
    }
  }, [reviewId, projection, loadProjection]);

  const recordManualCheck = useCallback(async (itemId: string, status: ManualCheckStatus, note: string) => {
    if (!reviewId || !projection) return;
    await postManualCheckResult(reviewId, projection.revision_id, { item_id: itemId, status, note });
    await loadProjection();
  }, [reviewId, projection, loadProjection]);

  // The dependency projection: package semantics in the Brief, and before raw
  // lockfile lines in Code. Unavailable means no semantics, never "no change".
  const [, setDependencyReview] = useState<ReviewDependencyProjection | null>(null);
  const dependencyRevisionId = overview?.revision.id ?? "";
  useEffect(() => {
    setDependencyReview(null);
    if (!reviewId || !dependencyRevisionId || !startupEnrichmentReady) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => fetchReviewDependencies(reviewId, dependencyRevisionId))
      .then((review) => { if (!cancelled) setDependencyReview(review ?? null); })
      .catch(() => { if (!cancelled) setDependencyReview(null); });
    return () => { cancelled = true; };
  }, [dependencyRevisionId, reviewId, startupEnrichmentReady]);


  useEffect(() => {
    if (surfaces.length === 0 || evidence.length === 0) return;
    setRuntimeValidations((current) => {
      let changed = false;
      const next = { ...current };
      for (const surface of surfaces) {
        const key = `${surface.provider}:${surface.id}`;
        if (current[key]?.response) continue;
        const prefix = `runtime-validation:surface:${surface.provider}:${surface.id}:`;
        const persisted = evidence.find(
          (item) => item.status === "current"
            && Boolean(item.verification_status)
            && item.source_ref.startsWith(prefix),
        );
        if (!persisted || current[key]?.evidence?.id === persisted.id) continue;
        next[key] = { ...current[key], evidence: persisted };
        changed = true;
      }
      return changed ? next : current;
    });
  }, [evidence, surfaces]);

  // Phase 12 controlled actions: Fix/Fix all/explicit corrections (palette + More), finding provenance.
  const corrections = useReviewCorrections({
    reviewId: reviewId ?? "",
    revisionId: overview?.revision.id ?? "",
    enabled: Boolean(reviewId && overview && startupEnrichmentReady) && !historical,
    readOnly: Boolean(readOnly),
  });
  const reloadAfterProviderSync = useCallback(() => {
    if (!reviewId) return;
    void fetchAnnotations(reviewId).then((result) => reviewDispatch({ type: "notes/set", annotations: result.annotations })).catch(() => undefined);
  }, [reviewId]);
  const feedbackSync = useFeedbackSync({
    reviewId: reviewId ?? "",
    enabled: Boolean(reviewId && overview && startupEnrichmentReady) && !historical,
    onChanged: reloadAfterProviderSync,
  });


  const sharedPreparation = useReviewPreparation();
  const readerPending = !startupReady || !overview || !targetList || !firstReviewableReady || briefLandingPending;
  // The redesigned Overview draws its own preparing state from the structure alone (SPEC 4.2 Preparing, OV-8).
  const overviewDrawsPreparing = flowOverview && Boolean(overview) && Boolean(targetList);
  // The Overview is ready when its facts have arrived, failed or are not offered.
  const partsPartial = targetList?.complete === false;
  // Only a plan for the revision on screen is ever drawn on the Overview; another revision's facts are not its facts.
  const currentProjection = projection && overview && projection.revision_id === overview.revision.id ? projection : null;
  const flowOverviewReady = overviewReady({
    structure: overviewDrawsPreparing,
    partsComplete: !partsPartial,
    briefCapable,
    projectionState,
    projectionCurrent: currentProjection !== null,
  });
  const readerSteps = useMemo<PreparationStep[]>(() => [
    { key: "reader_source", label: startupReady ? "Source revision ready" : preparationStage ? preparationLabel(preparationStage) : "Opening Review Reader", state: startupReady ? "done" : "running", elapsed: null },
    { key: "reader_structure", label: "Reading review structure", state: overview && targetList ? "done" : startupReady ? "running" : "pending", elapsed: null },
    { key: "reader_diff", label: "Preparing first visual diff", state: firstReviewableReady ? "done" : overview && targetList ? "running" : "pending", elapsed: null },
    { key: "reader_brief", label: briefPreparation?.state === "running"
      ? `Preparing Brief · ${briefPreparation.detail} · ${briefPreparation.completed}/${briefPreparation.total} Brief stages`
      : "Preparing Brief", state: !briefLandingPending ? "skipped" : firstReviewableReady ? "running" : "pending",
      elapsed: null, started_at: briefStartedAt },
  ], [startupReady, overview, targetList, firstReviewableReady, briefLandingPending, preparationStage, briefPreparation, briefStartedAt]);
  const setSharedPreparation = sharedPreparation?.setReader;
  useLayoutEffect(() => {
    setSharedPreparation?.({
      pending: authed && !reviewMissing && !(error && !overview) && readerPending && !overviewDrawsPreparing,
      steps: readerSteps,
      onOpenCode: briefLandingPending && startupReady && overview && targetList && firstReviewableReady
        ? openPreparedCode : undefined,
    });
  }, [setSharedPreparation, authed, reviewMissing, error, overview, readerPending, overviewDrawsPreparing, readerSteps, briefLandingPending, startupReady, targetList, firstReviewableReady, openPreparedCode]);
  useLayoutEffect(() => () => setSharedPreparation?.(null), [setSharedPreparation]);

  if (!authed) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6 text-subtle">
        <div className="max-w-md">
          <div className="text-heading font-semibold text-foreground">This review link is incomplete</div>
          <p className="mt-1 text-caption leading-5 text-muted-foreground">For a local review, reopen it from the terminal with <code className="text-subtle">lc review --open</code>.</p>
        </div>
      </div>
    );
  }
  if (reviewMissing === "unpaired" && !overview) {
    // The refusal a local browser gets before the terminal has connected it. Retry works once `lc review` has run.
    return (
      <div className="flex h-screen items-center justify-center bg-background px-6 text-subtle">
        <div className="w-full max-w-md" role="alert">
          <div className="text-heading font-semibold text-foreground">This browser isn't connected to your local review</div>
          <div className="mt-1 text-caption leading-5 text-muted-foreground">
            {routeReviewRef ? (
              // `lc review --open` would capture a new review; `lc review open` connects this browser to this one.
              <>
                Run <code className="text-subtle">lc review open r/{routeReviewRef}</code> in your project and this review opens already
                connected.
              </>
            ) : (
              <>
                Run <code className="text-subtle">lc review --open</code> in your terminal and this page opens already connected, or run{" "}
                <code className="text-subtle">lc review</code> again in your project. Then retry here.
              </>
            )}
          </div>
          <UiButton type="button" disabled={Boolean(busy)} onClick={() => void reloadReader()} className="mt-4">
            {busy === "reload" ? "Retrying…" : "Retry"}
          </UiButton>
        </div>
      </div>
    );
  }
  if (reviewMissing && !overview) {
    return (
      <div className="flex h-screen items-center justify-center bg-background px-6 text-subtle">
        <div className="w-full max-w-md" role="alert">
          <div className="text-heading font-semibold text-foreground">
            {reviewMissing === "none"
              ? "No review selected"
              : reviewMissing === "no_access"
                ? "You no longer have access to this review"
                : "Review not found"}
          </div>
          <div className="mt-1 text-caption leading-5 text-muted-foreground">
            {reviewMissing === "none"
              ? "Open a review from the list, or start one from the terminal with lc review."
              : reviewMissing === "no_access"
                ? "Ask your administrator to give this account access, then open the review again."
                : "This link does not match a review on this server. It may have been deleted or captured elsewhere."}
          </div>
          <UiButton asChild className="mt-4"><a href="/reviews">All reviews</a></UiButton>
        </div>
      </div>
    );
  }
  if (error && !overview) {
    return (
      <div className="flex h-screen items-center justify-center bg-background px-6 text-subtle">
        <div className="w-full max-w-md">
          <div className="text-heading font-semibold text-foreground">Review couldn't load</div>
          <div className="mt-1 text-caption leading-5 text-muted-foreground">The review is still available; the Reader could not fetch its current state.</div>
          <details className="mt-3 text-meta text-muted-foreground">
            <summary className="cursor-pointer hover:text-foreground">Error details</summary>
            <div className="mt-1 whitespace-pre-wrap font-mono text-red-300/80">{error}</div>
          </details>
          <UiButton type="button" disabled={Boolean(busy)} onClick={() => void reloadReader()} className="mt-4">
            {busy === "reload" ? "Retrying…" : "Retry"}
          </UiButton>
        </div>
      </div>
    );
  }

  // The application renders the shared progress display; standalone embeds
  // use the same surface, without creating a second loading screen.
  // The Overview sits in one provider chain and one root element, so moving between
  // the Overview and another flow screen swaps children and never remounts the Reader root.
  // What a hosting composition adds around the flow: its repository and author line, the read-only reason, header actions and overlays.
  const hostContext = overview && reviewId ? { reviewId, overview, annotations, readOnly: Boolean(readOnly), historical: Boolean(historical) } : null;
  const hostHeaderActions = hostContext ? extensions?.renderHeaderActions?.(hostContext) : null;
  const hostOverlays = hostContext ? extensions?.renderOverlays?.(hostContext) : null;
  const flowOverviewRoot = (screen: ReactNode, ready: boolean) => (
    <ViewerContext.Provider value={overview?.session.reviewer_id ?? ""}>
    <PrincipalNamesContext.Provider value={extensions?.principalNames ?? NO_PRINCIPAL_NAMES}>
    <ReviewCorrectionsProvider value={corrections.context}>
    <FeedbackSyncProvider value={feedbackSync}>
    <SeenCoverageProvider reviewRef={flowRoute?.reviewRef ?? ""} apiReviewId={reviewId} reviewerId={overview?.session.reviewer_id ?? ""} revisionId={overview?.revision.id ?? ""} targets={targets} recordable={!historical && !revisionAdvanced}>
    <div
      // The flow screens scroll the window under a sticky top bar; none of them is a fixed-height shell with an inner scroll box.
      className="review-app relative flex min-h-dvh flex-col bg-background text-foreground"
      data-testid="review-reader"
      data-review-targets-complete={targetList?.complete === false ? "false" : "true"}
      data-review-view-root={flowPreview ? "preview" : flowMap ? "map" : flowTour ? "walkthrough" : flowBrief ? "brief" : flowXRay ? "xray" : flowBack ? "welcome-back" : flowDone ? "done" : flowWrap ? "wrap" : flowQuick ? "quick-check" : flowFocus ? "focus" : flowAll ? "all-files" : "overview"}
      data-overview-state={ready ? "ready" : "preparing"}
    >
      {hostContext && (extensions?.readOnlyReason || extensions?.context || extensions?.authorship || hostHeaderActions) ? (
        <div data-testid="host-chrome" className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-1 text-meta text-muted-foreground">
          <p className="min-w-0">
            {extensions?.context ? <span title={extensions.context.title}>{extensions.context.label}</span> : null}
            {extensions?.authorship ? <span>{extensions.context ? " · " : ""}{extensions.authorship.prefix} {extensions.authorship.label}</span> : null}
            {extensions?.readOnlyReason && readOnly ? <span role="status" data-testid="host-read-only-reason">{extensions.context || extensions.authorship ? " · " : ""}{extensions.readOnlyReason}</span> : null}
          </p>
          {hostHeaderActions ? <div className="flex items-center gap-2">{hostHeaderActions}</div> : null}
        </div>
      ) : null}
      <ReviewFrictionProbe
        view={flowBrief ? "brief" : flowTour ? "tour" : flowMap || flowXRay ? "map" : "implementation"}
        onTarget={flowFocus}
        targetCount={targets.length}
        rail={askScope ? "ask" : null}
        notices={hostedNotice ? 1 : 0}
        revisionNumber={overview?.revision.revision_number ?? 0}
        reopened={targetList?.progress.changed_since_review ?? 0}
      />
      {screen}
      {hostOverlays}
      {hostedNotice ? <p role="status" data-testid="hosted-queued-notice" className="border-t border-border px-4 py-1 text-meta text-muted-foreground">{hostedNotice}</p> : null}
      {/* P7-2: where a hosted notice ("Sam took this review") shows on a screen that has no toast of its own; the first host owns it. */}
      <FlowToastHost />
      <TeamNoticeAnchor />
    </div>
    </SeenCoverageProvider>
    </FeedbackSyncProvider>
    </ReviewCorrectionsProvider>
    </PrincipalNamesContext.Provider>
    </ViewerContext.Provider>
  );
  // The agent the Overview names when this revision recorded no host: the earliest revision that did, else the agent the notes go to.
  const reviewAgentHost = [...(revisionHistory?.revisions ?? [])].sort((a, b) => a.revision_number - b.revision_number)
    .find((item) => item.provenance_host)?.provenance_host || (handoffToCodingAgent(feedbackDelivery) ? feedbackDelivery?.host ?? "" : "");
  if ((readerPending || !flowOverviewReady) && flowOverview && flowRoute && overview && targetList) {
    return flowOverviewRoot(
      <ReviewOverviewScreen
        reviewRef={flowRoute.reviewRef}
        overview={overview}
        agentHost={reviewAgentHost}
        targets={targets}
        annotations={annotations}
        projection={currentProjection}
        partial={partsPartial}
        partialTotals={{ parts: targetList?.progress.target_count ?? null, files: overview?.stats?.files ?? null }}
        ready={flowOverviewReady}
        progress={briefPreparation}
        aiAvailable={askCapable}
      />,
      flowOverviewReady,
    );
  }
  if (readerPending) {
    return sharedPreparation ? null : <PreparationProgress fullPage steps={readerSteps}
      onOpenCode={briefLandingPending && startupReady && overview && targetList && firstReviewableReady
        ? openPreparedCode : undefined} />;
  }

  const streamPaths = visiblePathOrder.filter((path) => pagePathSet.has(path));
  const mountedPathSet = new Set(streamPaths.filter((path) => comparisonActive || revealedPaths.has(path)));
  if (draftPath) mountedPathSet.add(draftPath);
  const compareBaseRevision: ReviewCompareBase = comparisonQueryValid
    ? requestedHeadBase ? "head" : requestedBaseRevision!
    : revisionComparison?.from_revision.revision_number
      ?? revisionHistory?.default_base_revision_number
      ?? overview.revision.revision_number;
  const compareTargetRevision = comparisonQueryValid
    ? requestedTargetRevision!
    : revisionComparison?.to_revision.revision_number
      ?? revisionHistory?.default_target_revision_number
      ?? overview.revision.revision_number;

  const contextDrawer = activeTarget ? (
    <Suspense fallback={<div className="p-3 text-caption text-muted-foreground">Loading context…</div>}>
      <ContextDrawer
        reviewId={reviewId}
        revisionNumber={overview.revision.revision_number}
        historical={historical}
        codeRoot={runtimeCodeRoot}
        target={activeTarget}
        detail={activeDetail}
        degraded={overview.degraded}
        annotations={surfacedAnnotations}
        evidence={evidence}
        journal={journal}
        development={development}
        tab={contextTab}
        pinned={contextPinned}
        uploading={busy === "evidence"}
        readOnly={Boolean(readOnly)}
        onTab={setContextTab}
        onClose={closeContext}
        onTogglePin={toggleContextPin}
        onOpenImpact={(site) => void openImpact(site)}
        onUploadEvidence={(files) => void uploadEvidence(files)}
        onAddPreview={(url, title) => void addPreview(url, title)}
        onOpenDevelopment={development ? () => setDevelopmentOpen(true) : undefined}
        onOpenInCode={runtimeCodeRoot ? openInCode : undefined}
        onJumpToCode={openCodeLocation}
        onSetAnnotationState={(id, state) => void setAnnotationState(id, state)}
        hiddenFindingIds={projection?.triage?.hidden_finding_ids}
        // N06: the Investigate tab reads what the Reader already holds; Walk and Decide go through the backtrail.
        investigate={briefCapable ? {
          projection,
          targets,
          checks: historical ? undefined : overview.evidence,
          revision: overview.revision,
          loadMapGraph: architectureRelevant ? () => loadArchitectureDelta().then((delta) => delta?.graph ?? null) : undefined,
          onWalk: (location) => withBacktrail(() => setMapLocation(location)),
          onOpenTarget: openBriefTarget,
          onOpenCodeLocation: openCodeLocation,
          onOpenDecision: projection ? (key) => { withBacktrail(() => changeView("brief")); revealBriefDecision(key); } : undefined,
          canRun: !readOnly && !historical,
        } : undefined}
      />
    </Suspense>
  ) : null;
  const latestAskRevision = revisionHistory?.latest_revision_number ?? overview.revision.revision_number;
  const askRevisionId = revisionHistory?.revisions.find((item) => item.revision_number === compareTargetRevision)?.id
    ?? (overview.revision.revision_number === compareTargetRevision ? overview.revision.id : "");
  const drawer = askOpen && askCapable ? (
    <Suspense fallback={<div className="p-3 text-caption text-muted-foreground">Loading Ask…</div>}>
      <ReviewAskDrawer
        reviewId={reviewId}
        revisionNumber={compareTargetRevision}
        baseRevisionNumber={typeof compareBaseRevision === "number" ? compareBaseRevision : undefined}
        latestRevisionNumber={latestAskRevision}
        revisionId={askRevisionId}
        filePaths={allPathOrder}
        scope={askScope}
        onClearScope={() => setAskScope(null)}
        onClose={closeAsk}
        onSwitchLatest={switchAskToLatest}
        onOpenReference={openAskReference}
      />
    </Suspense>
  ) : contextOpen ? contextDrawer : null;

  if (flowOverview && flowRoute) {
    return flowOverviewRoot(
      <ReviewOverviewScreen
        reviewRef={flowRoute.reviewRef}
        overview={overview}
        agentHost={reviewAgentHost}
        targets={targets}
        annotations={annotations}
        projection={currentProjection}
        ready
        progress={briefPreparation}
        aiAvailable={askCapable}
        onAsk={askCapable ? () => openAsk(null) : undefined}
        askPanel={askOpen && askCapable ? drawer : null}
        // The walkthrough reads the plan the Brief capability serves; it is offered only where it has steps to walk.
        quickConfirm={quickConfirm}
        tourAvailable={briefCapable && guidedReviewUseful(currentProjection, targets)}
        briefAvailable={briefCapable && currentProjection !== null}
        xrayAvailable={briefCapable && architectureRelevant}
      />,
      true,
    );
  }

  if (flowBrief && flowRoute && reviewId) {
    return flowOverviewRoot(
      <Suspense fallback={<div className="p-6 text-label text-muted-foreground" role="status">Opening the Brief…</div>}>
        <ReviewBriefScreen
          reviewRef={flowRoute.reviewRef}
          apiReviewId={reviewId}
          overview={overview}
          targets={targets}
          complete={!partsPartial}
          projection={briefCapable ? currentProjection : null}
          loading={briefCapable && !currentProjection && projectionState !== "failed"}
          checks={historical ? undefined : overview.evidence}
          revision={overview.revision}
          onDecision={historical || readOnly ? undefined : decideOnBrief}
          onLoadTarget={historical ? undefined : loadBehaviorTarget}
          onJudge={historical || readOnly ? undefined : judgeBehavior}
          onNarrate={historical || readOnly ? undefined : narrateBehavior}
          onManualCheck={historical || readOnly ? undefined : recordManualCheck}
          xrayAvailable={briefCapable && architectureRelevant}
          onAsk={askCapable ? askTarget : undefined}
          onAskChange={askCapable ? () => openAsk(null) : undefined}
          askPanel={askOpen && askCapable ? drawer : null}
        />
      </Suspense>,
      true,
    );
  }

  if (flowXRay && flowRoute?.route.kind === "xray" && reviewId) {
    return flowOverviewRoot(
      <Suspense fallback={<div className="p-6 text-label text-muted-foreground" role="status">Opening the X-Ray…</div>}>
        <ReviewXRayScreen
          reviewRef={flowRoute.reviewRef}
          apiReviewId={reviewId}
          at={flowRoute.route.at}
          overview={overview}
          targets={targets}
          complete={!partsPartial}
          load={briefCapable && architectureRelevant ? loadArchitectureDelta : undefined}
          loading={briefCapable && !currentProjection && projectionState !== "failed"}
          behavior={currentProjection?.behavior ?? null}
          revisionId={currentProjection?.revision_id}
          onLoadTarget={historical ? undefined : loadBehaviorTarget}
          onJudge={historical || readOnly ? undefined : judgeBehavior}
          onAskChange={askCapable ? () => openAsk(null) : undefined}
          askPanel={askOpen && askCapable ? drawer : null}
        />
      </Suspense>,
      true,
    );
  }

  if (flowTour && flowRoute?.route.kind === "walkthrough" && reviewId) {
    return flowOverviewRoot(
      <Suspense fallback={<div className="p-6 text-label text-muted-foreground" role="status">Opening the walkthrough…</div>}>
        <ReviewTourScreen
          reviewRef={flowRoute.reviewRef}
          apiReviewId={reviewId}
          step={flowRoute.route.step}
          view={flowRoute.route.view}
          overview={overview}
          targets={targets}
          complete={!partsPartial}
          tour={briefCapable ? currentProjection?.tour ?? null : null}
          loading={briefCapable && !currentProjection && projectionState !== "failed"}
          onSynthesize={historical || readOnly || !currentProjection ? undefined : async () => {
            try {
              await postTourSynthesis(reviewId, currentProjection.revision_id);
            } finally {
              await loadProjection().catch(() => undefined);
            }
          }}
          // The Walkthrough's map walks the Reader's cached architecture read, as the Brief's did; never for a tiny mechanical change.
          loadStory={briefCapable && architectureRelevant ? loadArchitectureDelta : undefined}
          checks={historical ? undefined : overview.evidence}
          annotations={annotations}
          onAsk={askCapable ? askTarget : undefined}
          onAskChange={askCapable ? () => openAsk(null) : undefined}
          askPanel={askOpen && askCapable ? drawer : null}
        />
      </Suspense>,
      true,
    );
  }

  if (flowMap && flowRoute?.route.kind === "map" && reviewId) {
    return flowOverviewRoot(
      <Suspense fallback={<div className="p-6 text-label text-muted-foreground" role="status">Opening the code map…</div>}>
        <ReviewFlowMapScreen reviewRef={flowRoute.reviewRef} apiReviewId={reviewId}
          overview={overview} targets={targets} lens={flowRoute.route.lens} view={flowRoute.route.view}
          complete={!partsPartial} canRun={!readOnly && !historical}
          onAskChange={askCapable ? () => openAsk(null) : undefined} askPanel={askOpen && askCapable ? drawer : null}/>
      </Suspense>,
      true,
    );
  }

  if (flowPreview && flowRoute?.route.kind === "preview" && reviewId) {
    return flowOverviewRoot(
      <Suspense fallback={<div role="status" className="p-6 text-label text-muted-foreground">Opening previews…</div>}>
        <ReviewFlowPreviewScreen reviewRef={flowRoute.reviewRef} apiReviewId={reviewId}
          overview={overview} targets={targets} surface={flowRoute.route.surface}
          complete={!partsPartial} onAskChange={askCapable ? () => openAsk(null) : undefined} askPanel={askOpen && askCapable ? drawer : null}/>
      </Suspense>, true,
    );
  }

  if (flowBack && flowRoute?.route.kind === "rev" && reviewId) {
    return flowOverviewRoot(
      <ReviewWelcomeBackScreen
        reviewRef={flowRoute.reviewRef}
        apiReviewId={reviewId}
        revision={flowRoute.route.revision}
        overview={overview}
        targets={sourceTargets}
        readOnly={readOnly}
        onDownloadRecord={() => downloadDecisionRecord(reviewId, overview.revision.id)}
        annotations={annotations}
        complete={!partsPartial}
        canCompare={!extensions}
        handoff={{
          kind: handoffRoute,
          label: handoffRoute === "export" ? "the author" : handoffRoute === "github" && !feedbackDelivery?.label ? "the pull request" : handoffDestinationLabel(feedbackDelivery),
          available: Boolean(feedbackDelivery?.supported) || handoffRoute === "export" || (handoffRoute === "github" && isGitHubPullRequest && !feedbackDelivery),
          reason: feedbackDelivery?.reason ?? "",
        }}
        onQueue={!hostedInfo && !extensions?.recordOutcome ? (outcome, summary, prepared, operationId) => queueReviewFlowSend(reviewId, {
          expectedRevisionId: overview.revision.id, outcome, summary, feedback: prepared, clientOperationId: operationId,
        }) : undefined}
        onAskChange={askCapable ? () => openAsk(null) : undefined}
        askPanel={askOpen && askCapable ? drawer : null}
        onMark={(target) => markTarget(target, "reviewed")}
        onKeepOpen={(target, body) => createAnnotation({
          path: target.path, start_line: 0, end_line: 0, file_level: true,
          side: "additions", body, kind: "request_change", target_unit_key: target.unit_key, mark_target: true,
        })}
      />,
      true,
    );
  }

  if (flowSecond && flowRoute && reviewId) {
    return flowOverviewRoot(<SecondSignatureScreen apiReviewId={reviewId} overview={overview} />, true);
  }

  if (flowDone && flowRoute && reviewId) {
    return flowOverviewRoot(
      <ReviewDoneScreen
        reviewRef={flowRoute.reviewRef}
        apiReviewId={reviewId}
        overview={overview}
        targets={sourceTargets}
        destinationLabel={handoffRoute === "export" ? "the author" : handoffDestinationLabel(feedbackDelivery)}
        onDownloadRecord={() => downloadDecisionRecord(reviewId, overview.revision.id)}
        onAskChange={askCapable ? () => openAsk(null) : undefined}
        askPanel={askOpen && askCapable ? drawer : null}
      />,
      true,
    );
  }

  if (flowWrap && flowRoute && reviewId) {
    return flowOverviewRoot(
      <>
      <ReviewWrapScreen
        hostBlockers={(extensions?.readinessBlockers ?? []).map((blocker) => blocker.label)}
        hostSections={extensions?.readinessSections}
        reviewRef={flowRoute.reviewRef}
        apiReviewId={reviewId}
        overview={overview}
        targets={sourceTargets}
        annotations={annotations}
        complete={!partsPartial}
        readOnly={readOnly}
        busy={busy === "finish"}
        recorded={finishRecorded}
        currentOutcome={currentOutcome}
        record={{
          ...finishRecordFacts({
            revision: overview.revision,
            targets: sourceTargets,
            readiness,
            reviewer: currentOutcome?.reviewer_id || overview.session.reviewer_id,
            notMeasured: overview.degraded.length,
          }),
          reconsideredNotes: annotations.filter((note) => note.source === "human" && note.kind === "request_change" && note.reconsidered_after_structure && note.structure_revision_id === overview.revision.id).map((note) => ({ id: note.id, path: note.path, body: note.body })),
        }}
        handoff={{
          unsent: handoff.unsent,
          label: handoffRoute === "export" ? "the author" : handoffRoute === "github" && !feedbackDelivery?.label ? "the pull request" : handoffDestinationLabel(feedbackDelivery),
          kind: handoffRoute,
          available: Boolean(feedbackDelivery?.supported) || handoffRoute === "export" || (handoffRoute === "github" && isGitHubPullRequest && !feedbackDelivery),
          reason: feedbackDelivery?.reason ?? "",
        }}
        destinationControl={(
          <ReviewDestinationPicker reviewId={reviewId} disabled={busy === "finish"} onChanged={() => void refreshFeedbackState("Changed")}>
            <UiButton variant="ghost" size="compact" type="button">Change destination</UiButton>
          </ReviewDestinationPicker>
        )}
        sendError={finishSendError}
        finishError={finishError}
        openedFrom={wrapOpenedFromAll ? "all" : "overview"}
        onAskChange={askCapable ? () => openAsk(null) : undefined}
        askPanel={askOpen && askCapable ? drawer : null}
        onFinish={(outcome, summary, options) => void confirmFinish(outcome, summary, options)}
        onQueue={!hostedInfo && !extensions?.recordOutcome ? (outcome, summary, prepared, operationId) => queueReviewFlowSend(reviewId, {
          expectedRevisionId: overview.revision.id, outcome, summary, feedback: prepared, clientOperationId: operationId,
        }) : undefined}
        onFeedbackPrepared={(prepared) => setFeedbackDelivery((current) => sameDeliveryRoute(current, prepared.delivery) ? current : prepared.delivery)}
        onDownloadRecord={() => downloadDecisionRecord(reviewId, overview.revision.id)}
      />
      {githubPublishOpen && <Suspense fallback={<p role="status" className="p-4 text-label">Loading GitHub confirmation…</p>}>
        <GitHubPublishSheet
          reviewId={reviewId}
          onClose={() => { setGithubPublishOpen(false); setFinishAfterPublish(false); }}
          onPublished={() => { void refreshFeedbackState(); if (finishAfterPublish) void finishPublished(); }}
        />
      </Suspense>}
      </>,
      true,
    );
  }

  if (flowQuick && flowRoute?.route.kind === "one") {
    return flowOverviewRoot(
      <ReviewQuickCheckScreen
        reviewRef={flowRoute.reviewRef}
        apiReviewId={reviewId}
        part={flowRoute.route.part}
        overview={overview}
        targets={targets}
        annotations={annotations}
        complete={!partsPartial}
        aiAvailable={askCapable}
        readOnly={readOnly}
        onMark={(target, state) => markTarget(target, state)}
        onNote={(target, body, reconsideredAfterStructure, line) => {
          if (line) return createAnnotation(lineCommentDraft(target, body, line, reconsideredAfterStructure));
          const range = targetCommentRange(target);
          return createAnnotation({
            path: target.path,
            start_line: range?.startLine ?? 0,
            end_line: range?.endLine ?? 0,
            side: range?.side ?? "additions",
            body,
            kind: "request_change",
            reconsidered_after_structure: reconsideredAfterStructure,
            file_level: range === null,
            target_unit_key: target.fingerprint_method === "revision_compare" ? undefined : target.unit_key,
            mark_target: true,
          });
        }}
        onAnnotationState={(id, state) => void setAnnotationState(id, state)}
        onAsk={askCapable ? askTarget : undefined}
        onAskChange={askCapable ? () => openAsk(null) : undefined}
        askPanel={askOpen && askCapable ? drawer : null}
      />,
      true,
    );
  }

  if (flowAll && flowRoute?.route.kind === "all" && reviewId) {
    return flowOverviewRoot(
      <ReviewAllFilesScreen
        reviewRef={flowRoute.reviewRef}
        apiReviewId={reviewId}
        overview={overview}
        targets={targets}
        annotations={annotations}
        projection={currentProjection}
        complete={!partsPartial}
        readOnly={readOnly}
        aiAvailable={askCapable}
        onMark={(target, state) => markTarget(target, state)}
        quickConfirm={quickConfirm}
        onNote={(target, body, line) => {
          if (line) return createAnnotation(lineCommentDraft(target, body, line, false));
          const position = targetCommentRange(target);
          return createAnnotation({
            path: target.path,
            start_line: position?.startLine ?? 0,
            end_line: position?.endLine ?? 0,
            side: position?.side ?? "additions",
            body,
            kind: "request_change",
            file_level: position === null,
            target_unit_key: target.fingerprint_method === "revision_compare" ? undefined : target.unit_key,
            mark_target: true,
          });
        }}
        onAnnotationState={(id, state) => void setAnnotationState(id, state)}
        onAsk={askCapable ? askTarget : undefined}
        onAskChange={askCapable ? () => openAsk(null) : undefined}
        askPanel={askOpen && askCapable ? drawer : null}
      />,
      true,
    );
  }

  if (flowFocus && flowRoute?.route.kind === "one") {
    return flowOverviewRoot(
      <ReviewFocusScreen
        reviewRef={flowRoute.reviewRef}
        apiReviewId={reviewId}
        part={flowRoute.route.part}
        overview={overview}
        targets={targets}
        annotations={annotations}
        proposals={proposals}
        projection={currentProjection}
        complete={!partsPartial}
        aiAvailable={askCapable}
        readOnly={readOnly}
        onMark={(target, state) => markTarget(target, state)}
        onNote={(target, body, reconsideredAfterStructure, line) => {
          if (line) return createAnnotation(lineCommentDraft(target, body, line, reconsideredAfterStructure));
          // Reuse the Reader's verified old/new-side anchoring, including removed-only parts.
          const range = targetCommentRange(target);
          return createAnnotation({
            path: target.path,
            start_line: range?.startLine ?? 0,
            end_line: range?.endLine ?? 0,
            side: range?.side ?? "additions",
            body,
            kind: "request_change",
            reconsidered_after_structure: reconsideredAfterStructure,
            file_level: range === null,
            target_unit_key: target.fingerprint_method === "revision_compare" ? undefined : target.unit_key,
            mark_target: true,
          });
        }}
        onAnnotationState={(id, state) => void setAnnotationState(id, state)}
        onAsk={askCapable ? askTarget : undefined}
        onAskChange={askCapable ? () => openAsk(null) : undefined}
        askPanel={askOpen && askCapable ? drawer : null}
        // Investigate reads what the Reader already holds, as the Classic Context drawer's tab did.
        investigate={briefCapable ? {
          projection: currentProjection,
          targets,
          checks: historical ? undefined : overview.evidence,
          revision: overview.revision,
          loadMapGraph: architectureRelevant ? () => loadArchitectureDelta().then((delta) => delta?.graph ?? null) : undefined,
          canRun: !readOnly && !historical,
        } : undefined}
      />,
      true,
    );
  }

  // The redesigned flow is the only Reader (SPEC 7.1 OWNER-9). An address that names no flow screen yet (an old link
  // that the address effect above is about to move to /r/<review>) draws nothing until it does.
  return null;
}

export default function ReviewReader(props: ReviewReaderProps = {}) {
  return (
    <ReviewStoreProvider>
      <CodeViewProvider>
        <ReviewReaderBody {...props} />
      </CodeViewProvider>
    </ReviewStoreProvider>
  );
}
