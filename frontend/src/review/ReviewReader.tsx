import { Button as UiButton } from "../components/ui/button";
import { isDialogOpen } from "../components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "../components/ui/popover";
import { escapeDismissedTooltip } from "../components/ui/tooltip";
import { focusReturnTarget } from "../lib/focus";
import { classifyWriteFailure, isStaleSnapshotError } from "./staleSnapshot";
import { readReviewPage, reviewPages } from "./reviewPagination";
import { lazyPanel } from "./lazyPanel";
import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type SetStateAction } from "react";
import { useParams } from "react-router-dom";
import "./reviewUi.css";

import type { ContextDrawerTab } from "./ContextDrawer";
import type { ReviewAskSelection } from "./ReviewAskDrawer";
import { codeHref, currentReturnPath, RETURN_PARAM, returnPathFrom, type CodeTarget } from "../lib/codeLinks";
import ReaderHeader from "./ReaderHeader";
import ReviewFrontierNotes from "./ReviewFrontierNotes";
import ReviewSnapshotControl from "./ReviewSnapshotControl";
import { Clock3, Cloud, FileDiff, ListTree, LoaderCircle, Lock, PencilLine, Puzzle, RefreshCw, RotateCcw, TriangleAlert } from "lucide-react";
import type { ReaderDevelopmentSummary, ReviewPersistentView, ReviewView } from "./ReaderHeader";
import ReviewBrief from "./ReviewBrief";
import InteractivePreviewPanel from "./InteractivePreview";
import { AgentApprovalPanel } from "./AgentActions";
import ReviewTour from "./ReviewTour";
import { PreparationProgress } from "./ReviewPreparation";
import { useReviewPreparation, type PreparationStep } from "./reviewPreparationContext";
import { attentionState, readinessControl, ROUTINE_ATTENTION_LABELS } from "./ReviewAttentionBar";
import ReviewContextStrip, { type ContextSignal } from "./ReviewContextStrip";
import { HostedQueuePanel, useHostedQueue } from "./ReviewHostedQueue";
import { isQueuedWrite, queuedWriteNotice } from "./hostedQueue";
import { reviewBlockingNoticeKind, reviewContextSignalPriority } from "./reviewNoticeArbitration";
import { useReviewFixOf } from "./ReviewFixOfBanner";
import { feedbackRoute } from "./ReviewFeedbackPanel";
import ReviewSendSheet from "./ReviewSendSheet";
import { ReviewCorrectionsProvider, useReviewCorrections } from "./reviewCorrections";
import { FeedbackSyncProvider, useFeedbackSync } from "./feedbackSync";
import ReviewAutomationSection from "./ReviewAutomationSection";
import ReviewDeploymentSection from "./ReviewDeploymentSection";
import ReviewProductionSection from "./ReviewProductionSection";
import { ReviewDestinationPicker, ReviewHandoffControls } from "./ReviewDestinationPicker";
import type { FinishOptions } from "./FinishSheet";
import ReviewRecordExport from "./ReviewRecordExport";
import ReviewReadinessSheet, {
  deriveReviewReadiness,
  scmRequirementBlockers,
  type ReviewReadinessBlocker,
} from "./ReviewReadinessSheet";
import type { ReviewPaletteAction } from "./ReviewCommandPalette";
import type { CommentsFilter } from "./ReviewCommentsSheet";
import type { Theme } from "../lib/theme";
import { applyReviewCodeTheme, getInitialReviewCodeTheme, reviewCodeThemeDefinition, type ReviewCodeTheme } from "./reviewCodeTheme";
import RevisionDeltaBar from "./RevisionDeltaBar";
import ReviewerSuggestionsPanel from "./ReviewerSuggestions";
import RevisionCompareBar, { type ReviewCompareBase } from "./RevisionCompareBar";
import ReviewOutline from "./ReviewOutline";
import ReviewStream, { type ReviewStreamHandle, type ReviewStreamFileSemantics } from "./ReviewStream";
import type { ReviewFileRole } from "./ReviewFileSemantics";
import {
  backtrailQuery,
  pushReviewBacktrail,
  readReviewBacktrail,
  REVIEW_BACKTRAIL_PARAM,
  writeReviewBacktrail,
  type ReviewBacktrailEntry,
} from "./reviewBacktrail";
import { useProductCapabilitiesResolution, useResolvedProductCapabilities } from "../lib/RuntimeCapabilities";
import { ReviewLockReason } from "./ReviewTargetHeader";
import { DEDICATED_REVIEW_ITEM_PARAMS, REVIEW_RETURN_PARAMS, dedicatedReviewItemUrl, fullReviewUrlFromDedicatedItem, readDedicatedReviewItem, readReviewReturnPath, readReviewReturnSurface } from "./reviewItemNavigation";
import { loadCommentDraft, saveCommentDraft } from "./reviewDraftPersistence";
import { isCompactReviewViewport, useCompactReviewLayout } from "./reviewLayout";
import {
  ReviewFrictionProbe,
  ReviewFrictionRecorder,
  activeReviewFriction,
  setActiveReviewFriction,
} from "./reviewFriction";
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
import { selectedNewSideTextFromPatch } from "./selectedNewSideText";
import { getInitialDiffStylePreference, nextDiffStylePreference, persistDiffStylePreference, type DiffOverflow, type DiffStylePreference } from "./diffModel";
import {
  bestTargetAfterRevision,
  fileAttention,
  firstActionable,
  filterTargets,
  isActionable,
  type ReaderOrder,
  orderedPaths,
  progressFromTargets,
  nextReviewGuideTarget,
  reviewAttentionPath,
  stepQueueTarget,
  stepFile,
  stepTarget,
  targetScrollLocation,
} from "./readerModel";
import { ManualChecklist, type BehaviorJudgmentState } from "./BehaviorChanges";
import { requestScopeTargetIds } from "./requestScope";
import {
  adaptiveReviewLanding,
  cohortForTarget,
  guidedReviewUseful,
  isTinyMechanicalReview,
  LARGE_REVIEW_TARGET_THRESHOLD,
  persistentReviewViews,
  projectionSearchTextByTarget,
  reviewCohorts,
} from "./reviewSurfaceModel";
import {
  deriveReviewPrimaryAction,
  finishAvailableInMenu,
  handoffCounts,
  handoffDestinationLabel,
  handoffToCodingAgent,
  primaryActionDestination,
} from "./reviewPrimaryAction";
import {
  adoptBootstrapFragment,
  armReviewLanding,
  applyChangeProposal,
  createChangeProposal,
  deliverFeedback,
  exportFeedback,
  fetchAnnotations,
  fetchFile,
  fetchFinishReview,
  fetchHistoricalAnnotations,
  fetchHistoricalFile,
  fetchHistoricalOverview,
  fetchHistoricalProjection,
  fetchHistoricalRelatedSource,
  fetchHistoricalTargets,
  fetchOverview,
  fetchStartupOverview,
  fetchStartupTargets,
  fetchProjection,
  fetchBriefPreparation,
  type BriefPreparationProgress,
  postAiReview,
  cancelAiReview,
  postAiReviewFresh,
  postBehaviorJudgment,
  postBehaviorNarrative,
  postDecisionEvent,
  postManualCheckResult,
  postSynthesis,
  postTourSynthesis,
  fetchBehaviorTarget,
  fetchProposalSelection,
  fetchChangeProposals,
  fetchRelatedSource,
  fetchReviewEvidence,
  fetchReviewJournal,
  fetchReviewReadiness,
  waiveReviewPremergeCheck,
  rebindReviewIntent,
  evaluateReviewPremergeChecks,
  subscribeLiveState,
  fetchHistoricalReviewJournal,
  fetchHistoricalReviewDevelopment,
  fetchReviewDevelopment,
  fetchReviewOutcome,
  fetchReviewPreparation,
  fetchReviewPatch,
  fetchRevisionLocator,
  fetchRevisionComparison,
  fetchRevisions,
  fetchReviewSurfaces,
  runReviewSurface,
  runRuntimeValidation,
  fetchRuntimeValidationJob,
  cancelRuntimeValidationJob,
  fetchSourceComparison,
  subscribeSourceState,
  fetchTargets,
  fetchTargetsForPath,
  finishReview,
  linkReviewEvidence,
  cancelReviewLanding,
  patchAnnotation,
  postAnnotation,
  postBulkReviewed,
  undoBulkReviewed,
  postMark,
  postRefresh,
  publishReviewSnapshot,
  postReviewOutcome,
  historicalReviewHref,
  reviewCompareHref,
  reviewDirectoryHref,
  reviewHref,
  sourceCompareHref,
  selectReviewId,
  uploadReviewEvidence,
  fetchHistoricalReviewMap,
  fetchReviewDependencies,
  fetchReviewProjectContext,
} from "./reviewApi";
import type {
  Annotation,
  AnnotationDraft,
  AnnotationState,
  FeedbackDelivery,
  FeedbackDeliveryCapability,
  FeedbackExport,
  FeedbackRouteKind,
  FeedbackStatus,
  FileDetail,
  FindingDismissalReason,
  FindingDisposition,
  MarkResult,
  MarkState,
  RefreshInfo,
  RelatedSource,
  ReviewAiReviewRunResult,
  ReviewChangeProposal,
  ReviewClosure,
  ReviewEvidence,
  ReviewJournal,
  ReviewDevelopment,
  ReviewLandingReadiness,
  ReviewDecisionEventInput,
  ReviewProjection,
  ReviewMapSelection,
  ReviewOutlineItem,
  ReviewOutcome,
  ReviewOutcomeKind,
  ReviewOverview,
  ReviewProposalSelection,
  ReviewRequestScope,
  ReviewAskReference,
  ReviewArchitectureDelta,
  ReviewProjectContext,
  ReviewProjectContextSelection,
  ReviewDependencyProjection,
  ReviewRevisionComparison,
  ReviewRevisionHistory,
  ReviewSurfaceInfo,
  ReviewSurfacePluginIssue,
  ReviewSurfaceRunResult,
  ReviewRuntimeValidationResponse,
  ReviewTarget,
  ReviewTargetPriority,
  ReviewTargetList,
  SourceComparison,
  SourceState,
} from "./types";

const ContextDrawer = lazyPanel("Context", () => import("./ContextDrawer"));
const ReviewAskDrawer = lazyPanel("Ask", () => import("./ReviewAskDrawer"));
const FinishSheet = lazyPanel("Finish", () => import("./FinishSheet"));
const GitHubPublishSheet = lazyPanel("GitHub publish", () => import("./GitHubPublishSheet"));
const ReviewCommandPalette = lazyPanel("Command palette", () => import("./ReviewCommandPalette"));

const READER_VIEW_KEY = "lc.review.view.";
const SYNTHESIS_POLL_MS = 2500;
// Browser lifecycle never originates Auto work. Explicit reviewer actions may
// choose an Auto-like effort elsewhere, but their durable trigger is manual.
const BROWSER_AI_REVIEW_TRIGGER = "manual" as const;
const BRIEF_KEYS: ReadonlySet<string> = new Set(["p", "?", "/", "j", "k", "J", "K", "]", "["]);

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
const ReviewMapSheet = lazyPanel("Map", () => import("./ReviewMapSheet"));
/** URL params for the Brief/Guided mode; the Map is named by its own location params. */
const REVIEW_VIEW_PARAM = "view";
const GUIDED_COHORT_PARAM = "guided";
const ReviewCommentsSheet = lazyPanel("Comments", () => import("./ReviewCommentsSheet"));
const ReviewHistorySheet = lazyPanel("History", () => import("./ReviewHistorySheet"));
const ReviewDevelopmentSheet = lazyPanel("Activity", () => import("./ReviewDevelopmentSheet"));

const INITIAL_CHANGE_BUDGET = 400;
const INITIAL_MAX_FILES = 5;
const LOAD_MORE_CHANGE_BUDGET = 800;
const LOAD_MORE_MAX_FILES = 30;
const PREFETCH_AHEAD_FILES = 2;
const BACKGROUND_PREFETCH_FILES = 10;
const CONTEXT_PIN_KEY = "lemoncrow.review.reader.contextPinned";
const DIFF_OVERFLOW_KEY = "lemoncrow.review.reader.diffOverflow";

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
        ? `Changed between Source and Snapshot ${targetRevision}`
        : `Changed between Snapshot ${baseRevision} and Snapshot ${targetRevision}`,
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
    source: "Capturing source snapshot",
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

/** Apply the server's fresh queue to every target; the client never recomputes it. */
function withPriorities(
  targets: ReviewTarget[],
  priorities: Record<string, ReviewTargetPriority> | null | undefined,
): ReviewTarget[] {
  if (!priorities) return targets;
  return targets.map((target) => {
    const priority = priorities[target.target_id];
    return priority ? { ...target, priority } : target;
  });
}

function mergeOutline(
  current: ReviewOutlineItem[],
  updates: ReviewOutlineItem[] | undefined,
): ReviewOutlineItem[] {
  if (!updates?.length) return current;
  const byPath = new Map(updates.map((item) => [item.path, item]));
  const seen = new Set<string>();
  const out = current.map((item) => {
    const replacement = byPath.get(item.path);
    if (replacement) seen.add(item.path);
    return replacement ?? item;
  });
  for (const update of updates) if (!seen.has(update.path)) out.push(update);
  return out;
}

type RelatedPanel = RelatedSource | { path: string; line: number; pending: true };

/** The element whose scroll offset is a view's place: the Code stream, or the Brief/Guided page. */
function reviewScrollRoot(view: ReviewView): HTMLElement | null {
  if (typeof document === "undefined" || view === "map") return null;
  return document.querySelector<HTMLElement>(
    view === "implementation" ? '[data-testid="review-stream"]' : `[data-review-scroll-root="${view}"]`,
  );
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

function primaryBusyLabel(busy: string, sessionStatus: string): string | undefined {
  switch (busy) {
    case "refresh": return "Updating review…";
    case "reload": return "Reloading…";
    case "order": return "Reordering…";
    case "finish-preview": return "Preparing finish…";
    case "feedback": return "Preparing feedback…";
    case "finish":
      return sessionStatus === "archived" ? "Restoring…" : sessionStatus === "finished" ? "Reopening…" : "Finishing…";
    default: return undefined;
  }
}

// Moves one target between state buckets in its file's outline row, so a mark
// shown before the server answers keeps the outline counts in step with it.
function shiftOutlineState(
  outline: ReviewOutlineItem[],
  path: string,
  from: MarkState,
  to: MarkState,
): ReviewOutlineItem[] {
  if (from === to) return outline;
  return outline.map((row) => row.path === path
    ? { ...row, [from]: Math.max(0, row[from] - 1), [to]: row[to] + 1 }
    : row);
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
function targetForPath(targets: ReviewTarget[], path: string): ReviewTarget | null {
  const rows = targets.filter((target) => target.path === path);
  return rows.find((target) => target.state !== "reviewed") ?? rows[0] ?? null;
}
function targetForAnnotation(targets: readonly ReviewTarget[], annotation: Annotation): ReviewTarget | null {
  const exact = annotation.unit_key
    ? targets.find((target) => target.unit_key === annotation.unit_key)
    : undefined;
  if (exact) return exact;
  if (!annotation.path || annotation.start_line < 1) {
    return targets.find((target) => target.path === annotation.path) ?? null;
  }
  const side = annotation.side === "old" ? "old" : "new";
  return targets.find((target) =>
    target.path === annotation.path
    && target.spans.some((span) =>
      span.side === side
      && span.start_line <= annotation.start_line
      && annotation.start_line <= span.end_line,
    ),
  ) ?? targets.find((target) => target.path === annotation.path) ?? null;
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

/** One reviewer-facing line naming every preview/surface plugin that failed. */
function surfacePluginIssueMessage(issues: readonly ReviewSurfacePluginIssue[] | undefined): string | undefined {
  if (!issues?.length) return undefined;
  return `Review plugins unavailable (source review is unaffected): ${issues
    .map((issue) => `${issue.kind} ${issue.plugin}: ${issue.reason}`)
    .join("; ")}`;
}

function automaticSurfaceRunKey(surface: ReviewSurfaceInfo, reviewPaths: ReadonlySet<string>): string {
  if (!surface.runtime || !surface.capabilities.includes("execute")) return "";
  const runner = String(surface.metadata.runner ?? "");
  if (surface.kind === "service" && runner === "docker-compose") {
    return `surface:${surface.provider}:${surface.id}`;
  }
  if (surface.kind !== "api.request" || runner !== "http-service") return "";
  if (!surface.affected_paths.some((path) => reviewPaths.has(path))) return "";
  const method = String(surface.metadata.method ?? "").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) return "";
  const url = String(surface.metadata.url ?? surface.locator ?? "").replace("{{baseUrl}}", "");
  if (url.includes("{{") || /\/:([^/?]+)/.test(url)) return "";
  return `surface:${surface.provider}:${surface.id}`;
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
  hidePrimaryAction?: boolean;
  readOnlyReason?: string;
  outcome?: ReviewOutcome | null;
  /**
   * Record the verdict for the Snapshot on screen. A stale `expectedRevisionId`
   * rejects with the 409 "review advanced" error `isStaleSnapshotError` matches.
   */
  recordOutcome?: (outcome: ReviewOutcomeKind, summary: string, expectedRevisionId: string) => Promise<ReviewOutcome>;
  authorship?: { prefix: string; label: string };
  context?: { label: string; title?: string };
  readinessBlockers?: readonly ReviewReadinessBlocker[];
  readinessSections?: ReactNode;
  /** Where reviewer suggestions live: Review Status (default) or a host surface ("external"). */
  reviewerSuggestions?: "reader" | "external";
  /** The reviewer's scoped request: its targets are the default queue and Brief. */
  requestScope?: ReviewRequestScope | null;
  renderHeaderActions?: (context: ReviewReaderExtensionContext) => ReactNode;
  renderOverlays?: (context: ReviewReaderExtensionContext) => ReactNode;
}

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

const ACCESS_LOST_NOTICE = {
  forbidden: "You no longer have access to this review, so it is read-only. Ask your administrator to restore access, or go back to All reviews.",
  gone: "This review was deleted or archived elsewhere, so it is read-only. Go back to All reviews to find its current version.",
} as const;

/** What a live stream's terminal status means for the open Review. */
function streamAccessLoss(status: number): "forbidden" | "gone" {
  return status === 404 || status === 410 ? "gone" : "forbidden";
}

/** A load refusal no retry can fix: the review is gone, or this account may not open it. */
function missingReviewState(reason: unknown): "" | "not_found" | "no_access" {
  const status = reviewErrorStatus(reason);
  if (status === 404) return "not_found";
  // 401 here is what remains after the request's own sign-in recovery.
  if (status === 403 || status === 401) return "no_access";
  return "";
}

export default function ReviewReader({ sourceProbeIntervalMs = SOURCE_PROBE_INTERVAL_MS, extensions }: ReviewReaderProps = {}) {
  const reviewEnvironment = useReviewEnvironment(extensions?.environmentFallbackMode ?? "local");
  // Settled manifest only: null while product discovery is pending.
  const productCapabilities = useResolvedProductCapabilities();
  const productCapabilitiesResolution = useProductCapabilitiesResolution();
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
    const href = `${window.location.pathname}${query ? `?${query}` : ""}`;
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
  const fixOf = useReviewFixOf(reviewId, reviewEnvironment.review.local_workspace);
  const [locatedRevision, setLocatedRevision] = useState<{ reviewId: string; revisionNumber: number } | null>(null);
  const historicalRevisionNumber = (
    comparisonQueryValid ? requestedTargetRevision : null
  ) ?? legacyHistoricalRevisionNumber ?? locatedRevision?.revisionNumber ?? null;
  const historicalRoute = historicalRevisionNumber !== null;
  const legacyHistoricalRoute = legacyHistoricalRevisionNumber !== null || locatedRevision !== null;
  const selectedHistoricalRevisionNumber = historicalRevisionNumber ?? 0;
  const [authed, setAuthed] = useState(true);
  const [preparationId, setPreparationId] = useState("");
  const [reviewMissing, setReviewMissing] = useState<"" | "none" | "not_found" | "no_access">("");
  // The live stream reported that this open Review was revoked or removed.
  const [accessLost, setAccessLost] = useState<"" | "forbidden" | "gone">("");
  const [preparationStage, setPreparationStage] = useState("");
  const [startupReady, setStartupReady] = useState(false);
  const [firstReviewableReady, setFirstReviewableReady] = useState(false);
  const [preparationRefresh, setPreparationRefresh] = useState(0);
  const firstContentPaintedRef = useRef(false);
  const markFirstContentPainted = useCallback(() => {
    firstContentPaintedRef.current = true;
  }, []);
  const [overview, setOverview] = useState<ReviewOverview | null>(null);
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
  const [targetList, setTargetList] = useState<ReviewTargetList | null>(null);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [commentsState, setCommentsState] = useState<"loading" | "ready" | "failed">("loading");
  const [revisionHistory, setRevisionHistory] = useState<ReviewRevisionHistory | null>(null);
  const [revisionComparison, setRevisionComparison] = useState<ReviewRevisionComparison | null>(null);
  const [sourceComparison, setSourceComparison] = useState<SourceComparison | null>(null);
  const [revisionComparisonLoading, setRevisionComparisonLoading] = useState(false);
  const [revisionComparisonError, setRevisionComparisonError] = useState("");

  useEffect(() => {
    const titleReviewId = overview?.session.ref || routeReviewId || routeReviewRef || overview?.session.id;
    if (!titleReviewId) return;
    document.title = `Review: ${titleReviewId}`;
  }, [overview?.session.ref, overview?.session.id, routeReviewId, routeReviewRef]);

  useEffect(() => {
    if (!overview || typeof window === "undefined") return;
    const reviewRef = overview.session.ref || overview.session.id;
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
    const currentParams = new URLSearchParams(window.location.search);
    const comparisonMode = currentParams.get("compare");
    if (comparisonMode === "auto" || comparisonMode === "full") {
      next.searchParams.set("compare", comparisonMode);
    }
    for (const key of [...REVIEW_MAP_PARAMS, ...DEDICATED_REVIEW_ITEM_PARAMS, ...REVIEW_RETURN_PARAMS, REVIEW_VIEW_PARAM, GUIDED_COHORT_PARAM, REVIEW_BACKTRAIL_PARAM, RETURN_PARAM, "p"]) {
      const value = currentParams.get(key);
      if (value !== null) next.searchParams.set(key, value);
    }
    const nextHref = `${next.pathname}${next.search}`;
    const current = `${window.location.pathname}${window.location.search}`;
    if (current !== nextHref) {
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
  ]);

  const [contextFailures, setContextFailures] = useState<Partial<Record<"comments" | "evidence" | "journal" | "development" | "outcome" | "surfaces" | "plugins", string>>>({});
  const [evidence, setEvidence] = useState<ReviewEvidence[]>([]);
  const [journal, setJournal] = useState<ReviewJournal | null>(null);
  const [development, setDevelopment] = useState<ReviewDevelopment | null>(null);
  const [surfaces, setSurfaces] = useState<ReviewSurfaceInfo[]>([]);
  const [surfaceRuns, setSurfaceRuns] = useState<Record<string, { old?: ReviewSurfaceRunResult; new?: ReviewSurfaceRunResult; error?: string }>>({});
  const [surfaceBusy, setSurfaceBusy] = useState<ReadonlySet<string>>(() => new Set());
  const [runtimeValidations, setRuntimeValidations] = useState<Record<string, { response?: ReviewRuntimeValidationResponse; evidence?: ReviewEvidence; error?: string }>>({});
  const [runtimeValidationBusy, setRuntimeValidationBusy] = useState<ReadonlySet<string>>(() => new Set());
  const [codeTheme, setCodeTheme] = useState<ReviewCodeTheme>(() => getInitialReviewCodeTheme());
  const [theme, setTheme] = useState<Theme>(() => reviewCodeThemeDefinition(getInitialReviewCodeTheme()).chromeTheme);
  const [details, setDetails] = useState<Record<string, FileDetail | undefined>>({});
  const [fileErrors, setFileErrors] = useState<Record<string, string | undefined>>({});
  const fileErrorsRef = useRef(fileErrors);
  fileErrorsRef.current = fileErrors;
  const [revealedPaths, setRevealedPaths] = useState<Set<string>>(() => new Set());
  const [activeTargetId, setActiveTargetIdState] = useState("");
  const activeTargetIdRef = useRef("");
  const setActiveTargetId = useCallback((next: SetStateAction<string>) => {
    const value = typeof next === "function"
      ? next(activeTargetIdRef.current)
      : next;
    activeTargetIdRef.current = value;
    setActiveTargetIdState(value);
  }, []);
  const [query, setQuery] = useState("");
  const [outlineCollapsed, setOutlineCollapsed] = useState(() => Boolean(dedicatedItem) || isCompactReviewViewport());
  const [focusMode, setFocusMode] = useState(Boolean(dedicatedItem));
  const [contextOpen, setContextOpen] = useState(false);
  const [askOpen, setAskOpen] = useState(false);
  // Phone composition: one sticky bottom dock replaces the footer and the
  // header's tool/action rows; navigation, Context and Ask are mutually
  // exclusive full-height panels.
  const compactLayout = useCompactReviewLayout();
  const [compactDock, setCompactDock] = useState<HTMLDivElement | null>(null);
  useEffect(() => {
    if (compactLayout && (contextOpen || askOpen)) setOutlineCollapsed(true);
  }, [compactLayout, contextOpen, askOpen]);
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
  const [diffStyle, setDiffStyle] = useState<DiffStylePreference>(() => getInitialDiffStylePreference());
  const [diffOverflow, setDiffOverflow] = useState<DiffOverflow>(() =>
    typeof window !== "undefined" && window.localStorage.getItem(DIFF_OVERFLOW_KEY) === "wrap" ? "wrap" : "scroll",
  );
  const [draft, setDraft] = useState<Draft | null>(null);
  const [bulkUndo, setBulkUndo] = useState<{
    path: string;
    marks: Array<{ unit_key: string; event_id: string }>;
  } | null>(null);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  // The Snapshot Reload review left: when the revision-loading effect moves off
  // it, the in-memory draft is carried over instead of reading the new
  // Snapshot's empty slot.
  const reloadCarriesDraftRef = useRef("");
  const draftRevisionRef = useRef("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState<FeedbackExport | null>(null);
  const [feedbackStatus, setFeedbackStatus] = useState<FeedbackStatus>(EMPTY_FEEDBACK_STATUS);
  const [feedbackDelivery, setFeedbackDelivery] = useState<FeedbackDeliveryCapability | null>(null);
  const [delivery, setDelivery] = useState<FeedbackDelivery | null>(null);
  const [activeFeedbackHandoff, setActiveFeedbackHandoff] = useState<ActiveFeedbackHandoff | null>(null);
  const [finishSummary, setFinishSummary] = useState<ReviewClosure | null>(null);
  const [finishSheetOpen, setFinishSheetOpen] = useState(false);
  const [githubPublishOpen, setGithubPublishOpen] = useState(false);
  const [sendSheetOpen, setSendSheetOpen] = useState(false);
  const [finishSendError, setFinishSendError] = useState("");
  // A failed verdict or finish write, said in the sheet the reviewer is looking at.
  const [finishError, setFinishError] = useState("");
  // An LGTM or Comment whose comments go to the pull request finishes once GitHub has them.
  const [finishAfterPublish, setFinishAfterPublish] = useState(false);
  const [localOutcome, setLocalOutcome] = useState<ReviewOutcome | null>(null);
  const [outcomeOverride, setOutcomeOverride] = useState<ReviewOutcome | null>(null);
  const [readinessOpen, setReadinessOpen] = useState(false);
  const [landingReadiness, setLandingReadiness] = useState<ReviewLandingReadiness | null>(null);
  const [landingReadinessState, setLandingReadinessState] = useState<"idle" | "loading" | "ready" | "failed">("idle");
  // Bumped when a mark commits. A mark shows at once, so the progress change
  // that re-reads readiness can reach the server before the mark does.
  const [committedMarks, setCommittedMarks] = useState(0);
  const [landingBusy, setLandingBusy] = useState(false);
  const [landingError, setLandingError] = useState("");
  const [premergeBusy, setPremergeBusy] = useState(false);
  const [premergeError, setPremergeError] = useState("");
  const [sourceState, setSourceState] = useState<SourceState | null>(null);
  const [proposals, setProposals] = useState<ReviewChangeProposal[]>([]);
  const [sourceMutationSupported, setSourceMutationSupported] = useState(false);
  const [proposalSelection, setProposalSelection] = useState<ReviewProposalSelection | null>(null);
  const [activeProposal, setActiveProposal] = useState<ReviewChangeProposal | null>(null);
  const [dismissedSourceFingerprint, setDismissedSourceFingerprint] = useState("");
  const [revisionInfo, setRevisionInfo] = useState<RefreshInfo | null>(null);
  const [deltaFocus, setDeltaFocus] = useState(false);
  // A scoped request focuses the queue by default; the rest stays one click away.
  const [requestScopeFocus, setRequestScopeFocus] = useState(true);
  const [related, setRelated] = useState<RelatedPanel | null>(null);
  const [openingPath, setOpeningPath] = useState("");
  const navigationRef = useRef(0);
  const [commandMode, setCommandMode] = useState<"actions" | "shortcuts" | null>(null);
  const [capabilityNoticeDismissed, setCapabilityNoticeDismissed] = useState(false);
  const [unsupportedNoticeDismissed, setUnsupportedNoticeDismissed] = useState(false);
  // The evidence-backed Brief (plan, decisions, questions, evidence) fills
  // the shell's Brief view when the capability is on.
  const briefCapable = reviewEnvironment.review.brief;
  const askCapable = reviewEnvironment.review.ask;
  const [projection, setProjection] = useState<ReviewProjection | null>(null);
  const [aiReviewRun, setAiReviewRun] = useState<ReviewAiReviewRunResult | null>(null);
  const [projectionState, setProjectionState] = useState<"idle" | "loading" | "ready" | "failed">("idle");
  const viewChosenRef = useRef(false);
  // Set once the first view is settled: by the Brief landing below, by a
  // failed plan load, or by the reviewer. Until then a Reader that will land on
  // the Brief keeps its startup screen, so it never opens on an empty Brief or
  // flips from Implementation to the Brief a moment after appearing.
  const [landingDecided, setLandingDecided] = useState(false);
  const projectionRequestRef = useRef(0);
  const [briefPreparation, setBriefPreparation] = useState<BriefPreparationProgress | null>(null);
  const [briefStartedAt, setBriefStartedAt] = useState<number | undefined>();
  // Brief · Guided Review · Code · Map are projections of one authoritative Change.
  const mapLocation = useMemo(() => readReviewMapLocation(searchParams), [searchParams]);
  const mapOpen = mapLocation !== null;
  const requestedReviewView = searchParams.get(REVIEW_VIEW_PARAM);
  const guidedCohortId = searchParams.get(GUIDED_COHORT_PARAM) ?? "";
  const briefOpen = !mapOpen && requestedReviewView === "brief";
  const tourOpen = !mapOpen && requestedReviewView === "tour";
  const view: ReviewView = mapOpen ? "map" : briefOpen ? "brief" : tourOpen ? "tour" : "implementation";
  const briefShown = briefCapable && view === "brief" && projection !== null;
  const tourShown = briefCapable && view === "tour" && projection !== null;
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
    || readinessOpen;
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
  const changeViewRef = useRef<(next: ReviewView) => void>(() => undefined);
  const openPreparedCode = useCallback(() => changeViewRef.current("implementation"), []);
  const revealImplementation = useCallback(() => {
    if (view !== "implementation") changeViewRef.current("implementation");
  }, [view]);
  // Where Back returns when another surface (Code) opened this Review.
  const returnTo = useMemo(() => returnPathFrom(searchParams), [searchParams]);
  const setMapLocation = useCallback((location: ReviewMapLocation | null, mode: "push" | "replace" = "push") => {
    setSearchParams((current) => writeReviewMapLocation(current, location), { replace: mode === "replace" });
  }, [setSearchParams]);
  const [commentsOpen, setCommentsOpen] = useState(false);
  const [commentsFilter, setCommentsFilter] = useState<CommentsFilter | undefined>(undefined);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [developmentOpen, setDevelopmentOpen] = useState(false);
  const [revisionAdvanced, setRevisionAdvanced] = useState(false);
  const currentOutcome = outcomeOverride ?? extensions?.outcome ?? localOutcome;
  const changeDiffStyle = useCallback((preference: DiffStylePreference) => {
    setDiffStyle(preference);
    persistDiffStylePreference(preference);
  }, []);
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
  const exitRevisionComparison = useCallback(() => {
    comparisonSelectionPinnedRef.current = true;
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      params.delete("base");
      params.delete("target");
      params.set("compare", "full");
      return params;
    });
    setRevisionComparison(null);
    setSourceComparison(null);
    setRevisionComparisonError("");
    setActiveTargetId("");
    setRelated(null);
  }, [setSearchParams]);
  const toggleDiffOverflow = useCallback(() => {
    setDiffOverflow((current) => {
      const next: DiffOverflow = current === "scroll" ? "wrap" : "scroll";
      window.localStorage.setItem(DIFF_OVERFLOW_KEY, next);
      return next;
    });
  }, []);
  const streamRef = useRef<ReviewStreamHandle>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const finishButtonRef = useRef<HTMLButtonElement>(null);
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
  const automaticSurfaceRunsRef = useRef(new Set<string>());
  // Bumped whenever the displayed Snapshot changes; a surface run started under
  // an older generation may not write its result or error.
  const surfaceGenerationRef = useRef(0);
  const surfaceIdentityRef = useRef("");
  // Surfaces with a run in flight on the displayed Snapshot: one surface's run
  // never blocks another's.
  const surfaceBusyRef = useRef(new Set<string>());
  const runtimeValidationBusyRef = useRef(new Set<string>());
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
      setDraft({
        ...carried,
        startLine: 0,
        endLine: 0,
        fileLevel: false,
        targetUnitKey: undefined,
        recovered: true,
      });
      return;
    }
    setDraft(loadCommentDraft(reviewId, revisionId));
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
    setRevisionComparisonError(malformedComparisonQuery ? "Comparison URL requires valid non-negative Snapshot numbers." : "");

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
      setRevisionComparisonError(`No such Snapshot ${requestedTargetRevision}.`);
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
          setError(status.detail || "review preparation failed before the first Snapshot became ready");
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
    request = withTransientRetry(() => (historical ? fetchHistoricalFile(id, historicalRevisionKey, path) : fetchFile(id, path)))
      .then((detail) => {
        if (loadGenerationRef.current !== generation) return null;
        const cacheRevision = fileCacheRevisionRef.current;
        if (detail.revision_id && cacheRevision && detail.revision_id !== cacheRevision) {
          // Answered for another Snapshot than the one on screen: never shown
          // as this one's code.
          setFileErrors((current) => ({ ...current, [path]: "This file came back for a different Snapshot than the one shown. Retry, or reload the review." }));
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

  const retryPath = useCallback(async (path: string) => (
    reviewId ? Boolean(await loadPath(reviewId, path)) : false
  ), [reviewId, loadPath]);

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
    const byId = new Map(next.targets.map((target) => [target.target_id, target]));
    const ordered: ReviewTarget[] = [];
    const seen = new Set<string>();
    for (const targetId of stableTargetOrderRef.current) {
      const target = byId.get(targetId);
      if (!target) continue;
      ordered.push(target);
      seen.add(targetId);
    }
    for (const target of next.targets) {
      if (seen.has(target.target_id)) continue;
      ordered.push(target);
      stableTargetOrderRef.current.push(target.target_id);
    }
    return { ...next, targets: ordered };
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
        journal: `Agent context unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
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
        development: `Development unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
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
    void fetchReviewSurfaces(id, revisionId).then((next) => {
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
        surfaces: `Rendered review surfaces unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
      }));
    });
  }, [queueBackgroundPaths]);
  const loadReader = useCallback(async (id: string): Promise<boolean> => {
    // A newer load, or a capture, supersedes this one: nothing it requested
    // may land after that (G0.7's rule for surface runs, for all context).
    const load = ++readerLoadRef.current;
    const isCurrentLoad = () => readerLoadRef.current === load;
    setCommentsState("loading");
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

      setOverview(nextOverview);
      setTargetList(nextTargets);
      setError("");
      const first = firstActionable(nextTargets.targets);
      setActiveTargetId((current) => current && nextTargets.targets.some((target: ReviewTarget) => target.target_id === current)
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
      const uniquePaths = [...new Set(pathsToShow)];
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
        setAnnotations(next.annotations);
        setFeedbackStatus(next.feedback ?? fallbackFeedbackStatus(next.annotations));
        setFeedbackDelivery(next.delivery ?? null);
        setCommentsState("ready");
        setContextFailures((current) => ({ ...current, comments: undefined }));
      }).catch((reason: unknown) => {
        if (!isCurrentLoad()) return;
        setCommentsState("failed");
        setContextFailures((current) => ({
          ...current,
          comments: `Review discussions unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
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
          evidence: `Review evidence unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
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
          outcome: `Review outcome unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
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
  }, [dedicatedItem, historical, historicalRevisionKey, loadPath, loadSnapshotContext, loadSurfaces, resetFileCache, revealPaths, stabilizeTargetOrder]);

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
  // The feedback panel's own handoff, judged from the comments themselves (they
  // outlive the Snapshot it was sent on): once each carries the agent's
  // addressed claim or was resolved, the count still awaiting re-review.
  const feedbackHandoffRereview = useMemo(() => {
    const ids = feedback && delivery ? Object.keys(feedback.annotation_versions) : [];
    if (ids.length === 0) return null;
    const byId = new Map(annotations.map((item) => [item.id, item]));
    let pending = 0;
    for (const id of ids) {
      const item = byId.get(id);
      if (item?.state === "resolved") continue;
      if (item?.author_response !== "addressed") return null;
      pending += 1;
    }
    return pending;
  }, [feedback, delivery, annotations]);
  const activeHandoffCurrentRevision = Boolean(
    activeFeedbackHandoff && overview && activeFeedbackHandoff.revisionId === overview.revision.id,
  );
  const activeHandoffWorking = Boolean(
    activeHandoffCurrentRevision && !activeHandoffAddressed,
  );

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

    // The event stream reports a newer Snapshot. The Reader's own capture wakes it
    // too, with the Snapshot the Reader is moving to: not one captured
    // elsewhere. True when it is the Reader's own (this loop then ends; the new
    // Snapshot's loop takes over).
    const ownCapture = async (revisionId: string): Promise<boolean> => {
      const own = ownCaptureRef.current;
      if (own && (await own) === revisionId) return true;
      if (live) setRevisionAdvanced(true);
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
        setTargetList((current) => (current === before ? ordered : current));
        return true;
      } catch {
        return false;
      }
    };

    const refreshAnnotations = async () => {
      try {
        const result = await fetchAnnotations(reviewId);
        if (!live || result.revision_id !== currentRevisionId) return;
        setAnnotations((current) => {
          const currentById = new Map(current.map((item) => [item.id, item]));
          return result.annotations.map((item) => {
            const previous = currentById.get(item.id);
            return previous && previous.updated_at > item.updated_at ? previous : item;
          });
        });
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
          packetCursor = next.packet_cursor;
          annotationsCursor = next.annotations_cursor;
          journalCursor = next.journal_cursor;
          developmentCursor = next.development_cursor ?? "";
          marksCursor = next.marks_cursor ?? "";
          // The initial event may arrive after a background checkpoint won
          // the race with Reader startup. Priming must still compare revision
          // identity or the UI can miss that newer revision forever.
          if (next.revision_id !== currentRevisionId && await ownCapture(next.revision_id)) return;
        } else {
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
        }
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
    && revisionComparison
    && revisionComparison.code_available
    && revisionComparison.from_revision.revision_number !== revisionComparison.to_revision.revision_number,
  );
  const headComparisonActive = Boolean(requestedHeadBase && sourceComparison);
  const comparisonActive = revisionComparisonActive || headComparisonActive;
  const comparisonRequested = Boolean(
    comparisonQueryValid
    && (requestedHeadBase || requestedBaseRevision !== requestedTargetRevision),
  );
  const automaticComparisonExpected = Boolean(
    overview
    && !comparisonSelectionPinnedRef.current
    && !legacyHistoricalRoute
    && !dedicatedItem
    && (overview.default_base_revision_number ?? overview.revision.revision_number)
      !== (overview.default_target_revision_number ?? overview.revision.revision_number),
  );
  // Never render the Full-review patch under a Snapshot-comparison header.
  // Until the exact pair is available (or if it fails), Implementation stays
  // neutral instead of falling through to Review base → Snapshot.
  const comparisonPlaceholderActive = Boolean(
    !comparisonActive && (comparisonRequested || automaticComparisonExpected),
  );
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

  const fullModelHydrationRef = useRef("");
  useEffect(() => {
    if (!reviewId || historical || !overview || targetList?.complete !== false) return;
    const needsFullModel = view !== "implementation" || Boolean(query.trim());
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
      setOverview(hydratedOverview);
      setTargetList(stableTargets);
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
  }, [historical, overview, query, reviewId, stabilizeTargetOrder, targetList?.complete, targetList?.order, view]);

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
  const streamSurfaces = useMemo(() => {
    if (comparisonActive) return [];
    if (!dedicatedItem) return surfaces;
    if (dedicatedItem.kind === "file") return [];
    return surfaces.filter((item) => item.provider === dedicatedItem.provider && item.id === dedicatedItem.id);
  }, [dedicatedItem, comparisonActive, surfaces]);
  const dedicatedItemAvailable = dedicatedItem?.kind === "surface"
    ? streamSurfaces.length > 0
    : dedicatedItem?.kind === "file"
      ? Boolean(
          comparisonActive
            ? comparisonDetails[dedicatedItem.path]
            : details[dedicatedItem.path] || fileErrors[dedicatedItem.path],
        )
      : false;
  const annotationSearchText = useMemo(
    () => annotationSearchTextByTarget(scopedTargets, displayAnnotations),
    [scopedTargets, displayAnnotations],
  );
  // Lists fold below-threshold findings behind a disclosure; counts and readiness keep reading displayAnnotations.
  const surfacedAnnotations = useMemo(
    () => markBelowThreshold(displayAnnotations, projection?.triage?.hidden_finding_ids),
    [displayAnnotations, projection?.triage?.hidden_finding_ids],
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
  const deltaRemainingCount = useMemo(
    () => targets.filter((target) => deltaActiveIds.has(target.target_id) && targetStillNeedsAttention(target)).length,
    [targets, deltaActiveIds],
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
  const pageChangedLines = pagePaths.reduce((sum, path) => sum + (fileChangeLines.get(path) ?? 0), 0);
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
  // reason the counter directly above it does: a file the stream only carries
  // because it holds a draft matched nothing, so listing it here would answer
  // "1 target · 1 file" with two rows, the second reading "0 matches".
  const outlinePaths = useMemo(() => new Set(filteredTargets.map((target) => target.path)), [filteredTargets]);
  const comparisonOutline = useMemo<ReviewOutlineItem[]>(() => {
    const byPath = new Map<string, ReviewTarget[]>();
    for (const target of comparisonTargets) {
      const rows = byPath.get(target.path) ?? [];
      rows.push(target);
      byPath.set(target.path, rows);
    }
    return [...byPath.entries()].map(([path, rows]) => ({
      path,
      target_count: rows.length,
      reviewed: rows.filter((target) => target.state === "reviewed").length,
      changed_since_review: rows.filter((target) => target.state === "changed_since_review").length,
      needs_changes: rows.filter((target) => target.state === "needs_changes").length,
      unreviewed: rows.filter((target) => target.state === "unreviewed").length,
      unknown: rows.filter((target) => target.state === "unknown").length,
      mechanical: 0,
      attention_rank: Math.max(0, ...rows.map((target) => target.attention_rank)),
      reasons: [...new Set(rows.flatMap((target) => target.reasons))],
    }));
  }, [comparisonTargets]);
  const startupOutlineUnfiltered = targetList?.complete === false
    && !query.trim()
    && !deltaFocus
    && !requestScopeFiltering;
  const visibleOutline = useMemo(() => {
    const rows = comparisonActive ? comparisonOutline : targetList?.outline ?? [];
    return startupOutlineUnfiltered ? rows : rows.filter((row) => outlinePaths.has(row.path));
  }, [comparisonActive, comparisonOutline, targetList?.outline, outlinePaths, startupOutlineUnfiltered]);
  // Over every target, so a search never changes a file's section or reason.
  const outlineAttention = useMemo(() => fileAttention(targets), [targets]);
  // Counted over the filtered set, never the streamed one: a file pinned into
  // the stream only because it holds a draft has not matched the search.
  const searchMatches = useMemo(() => {
    const byPath = new Map<string, number>();
    if (!query.trim()) return { byPath, targetCount: 0, fileCount: 0 };
    for (const target of filteredTargets) byPath.set(target.path, (byPath.get(target.path) ?? 0) + 1);
    return { byPath, targetCount: filteredTargets.length, fileCount: byPath.size };
  }, [query, filteredTargets]);
  const outstandingByPath = useMemo(() => {
    const counts = new Map<string, number>();
    for (const target of targets) {
      if (target.state === "reviewed") continue;
      counts.set(target.path, (counts.get(target.path) ?? 0) + 1);
    }
    return counts;
  }, [targets]);
  const activeTarget = useMemo(
    () => visibleTargets.find((target) => target.target_id === activeTargetId) ?? firstActionable(visibleTargets),
    [visibleTargets, activeTargetId],
  );
  const activeDetail = activeTarget
    ? (comparisonActive ? comparisonDetails[activeTarget.path] : details[activeTarget.path]) ?? null
    : null;
  const progress = targetList?.progress ?? progressFromTargets(targets);
  const rootCommentCount = displayAnnotations.filter((item) => !item.parent_id).length;
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
    readinessOpen,
    reviewId,
    startupEnrichmentReady,
    targetList,
    targetList?.progress.changed_since_review,
    targetList?.progress.needs_changes,
    targetList?.progress.reviewed,
    targetList?.progress.unknown,
    targetList?.progress.unreviewed,
  ]);

  const armLanding = useCallback(async () => {
    if (!reviewId || !landingReadiness || landingReadinessState !== "ready" || landingBusy) return;
    setLandingBusy(true);
    setLandingError("");
    try {
      const next = await armReviewLanding(reviewId, {
        expected_revision_id: landingReadiness.snapshot_id,
        expected_tree_fingerprint: landingReadiness.tree_fingerprint,
        expected_conditions_hash: landingReadiness.conditions_hash,
      });
      setLandingReadiness(next);
    } catch (reason) {
      setLandingError(`Could not arm landing: ${String(reason instanceof Error ? reason.message : reason)}`);
      try {
        setLandingReadiness(await fetchReviewReadiness(reviewId));
        setLandingReadinessState("ready");
      } catch {
        // Do not keep a previously armable projection authoritative after an
        // ambiguous provider/server failure. The exact conditions must reload.
        setLandingReadinessState("failed");
      }
    } finally {
      setLandingBusy(false);
    }
  }, [landingBusy, landingReadiness, landingReadinessState, reviewId]);

  const cancelLanding = useCallback(async () => {
    if (!reviewId || !landingReadiness || landingBusy) return;
    setLandingBusy(true);
    setLandingError("");
    try {
      setLandingReadiness(await cancelReviewLanding(reviewId, landingReadiness.snapshot_id));
    } catch (reason) {
      setLandingError(`Could not cancel landing: ${String(reason instanceof Error ? reason.message : reason)}`);
      try {
        setLandingReadiness(await fetchReviewReadiness(reviewId));
        setLandingReadinessState("ready");
      } catch {
        setLandingReadinessState("failed");
      }
    } finally {
      setLandingBusy(false);
    }
  }, [landingBusy, landingReadiness, reviewId]);

  /** One pre-merge action on the readiness Snapshot, then the authoritative readiness is re-read. */
  const runPremergeAction = useCallback(async (
    failure: string,
    action: (id: string, snapshotId: string) => Promise<unknown>,
  ) => {
    if (!reviewId || !landingReadiness || premergeBusy) return;
    setPremergeBusy(true);
    setPremergeError("");
    try {
      await action(reviewId, landingReadiness.snapshot_id);
    } catch (reason) {
      setPremergeError(`${failure}: ${String(reason instanceof Error ? reason.message : reason)}`);
    }
    try {
      setLandingReadiness(await fetchReviewReadiness(reviewId));
      setLandingReadinessState("ready");
    } catch {
      setLandingReadinessState("failed");
    } finally {
      setPremergeBusy(false);
    }
  }, [landingReadiness, premergeBusy, reviewId]);

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
  const readiness = landingReadinessState === "ready" && landingReadiness
    ? {
        ...localReadiness,
        ready: landingReadiness.ready,
        label: landingReadiness.ready
          ? landingReadiness.landing.supported
            ? "Ready to merge"
            : "Review ready"
          : `${landingReadiness.blocker_count} merge requirement${landingReadiness.blocker_count === 1 ? "" : "s"} unresolved`,
        detail: landingReadiness.ready
          ? landingReadiness.landing.supported
            ? "All visible merge requirements are satisfied."
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
            : "Loading merge requirements for this Snapshot.",
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
  const globalNextTarget = nextReviewGuideTarget(targets);
  const runningDraft = overview?.revision.capture_kind === "draft" && !overview.revision.is_published_snapshot;
  const forcedReadOnly = Boolean(extensions?.forceReadOnly);
  const primaryAction = runningDraft && !historical ? {
    kind: "create_snapshot" as const,
    label: "Create Snapshot",
    detail: forcedReadOnly
      ? (extensions?.readOnlyReason || "This Review is read-only.")
      : "Save this draft as a frozen Snapshot to enable comments and review decisions.",
    disabled: forcedReadOnly || revisionAdvanced || overview?.session.status !== "open",
  } : deriveReviewPrimaryAction({
    historical,
    sessionStatus: overview?.session.status ?? "open",
    revisionAdvanced,
    sourceChanged: Boolean(sourceState?.changed),
    commentsState,
    feedbackStatus,
    feedbackDelivery,
    progress,
    readiness,
    landing: landingReadinessState === "ready" ? landingReadiness : null,
    nextTarget: globalNextTarget,
  });
  const sessionLocked = overview?.session.status === "archived" || overview?.session.status === "finished";
  // A finished review takes no new judgments until it is reopened (the primary
  // action says so); feedback handoff stays available after finishing.
  const readOnly = forcedReadOnly || historical || runningDraft || sessionLocked || revisionAdvanced || Boolean(accessLost);
  const handoffLocked = forcedReadOnly || historical || runningDraft || overview?.session.status === "archived" || revisionAdvanced || Boolean(accessLost);
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
    window.requestAnimationFrame(() => { if (current()) streamRef.current?.scrollToFile(path, behavior); });
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
        if (current()) streamRef.current?.scrollToTarget(target, effectiveBehavior);
      }));
    }
  }, [reviewId, allPathOrder, fileChangeLines, loadPath, navigationPathOrder, queueBackgroundPaths, queueNextPage, revealNavigationWindow, startNavigation, openPathPage, pages, targetList?.complete]);

  const loadProjection = useCallback(async (background = false) => {
    if (!reviewId || !briefCapable) return;
    const request = ++projectionRequestRef.current;
    if (!background) {
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
      if (request !== projectionRequestRef.current) return;
      // A background completion belongs to the Snapshot that triggered it.
      // The Reader may have advanced while this request was in flight.
      if (background && next.revision_id !== overview?.revision.id) return;
      setProjection(next);
      setProjectionState("ready");
    } catch {
      if (request !== projectionRequestRef.current || background) return;
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
      if (request !== projectionRequestRef.current) return;
      setProjectionState("failed");
      setLandingDecided(true);
    }
  }, [reviewId, briefCapable, historical, historicalRevisionKey, overview?.revision.id]);

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

  // Adaptive Auto landing is deterministic from already-loaded Review metadata.
  // Explicit URL/deep-link intent, a dedicated item, return state, comparison,
  // or a remembered reviewer choice wins. No model call chooses navigation.
  useEffect(() => {
    if (!projection || viewChosenRef.current) return;
    viewChosenRef.current = true;
    setLandingDecided(true);
    if (view !== "implementation" || !defaultLanding) return;
    const landing = readStoredView(reviewId)
      ?? (targets.length > LARGE_REVIEW_TARGET_THRESHOLD ? "implementation" : adaptiveReviewLanding(projection, targets, briefCapable));
    if (landing === "implementation") return;
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      params.set(REVIEW_VIEW_PARAM, "brief");
      params.delete(GUIDED_COHORT_PARAM);
      return params;
    }, { replace: true });
  }, [projection, targets, briefCapable, view, defaultLanding, reviewId, setSearchParams]);

  // One path for every failed human write: a stale Snapshot moves the reader to
  // the reload state with an explicit notice instead of a generic error string.
  const reportWriteFailure = useCallback((reason: unknown) => {
    const failure = classifyWriteFailure(reason);
    if (failure.stale) {
      setRevisionAdvanced(true);
      setFinishSheetOpen(false);
    }
    setError(failure.message);
  }, []);

  // The Snapshot on screen. Refetches after a write return the latest
  // revision's state; only state for this Snapshot may replace what is shown.
  const shownRevisionId = overview?.revision.id ?? "";

  // Decisions are human-only writes bound to the displayed Snapshot and the
  // newest event the reviewer saw; the server refuses stale ones. Request
  // change first files ordinary human feedback (never a target mark), then
  // links it, so the request is the queue action.
  const decideOnBrief = useCallback(async (key: string, input: ReviewDecisionEventInput) => {
    if (!reviewId || !projection) return;
    const decision = projection.decisions.find((item) => item.key === key);
    let feedbackPath: string | undefined;
    if (input.action === "request_change") {
      const target = targets.find((item) => item.target_id === decision?.target_ids[0]);
      if (!target) throw new Error("This decision has no implementation target to attach the request to.");
      feedbackPath = target.path;
    }
    try {
      // One write, bound to the Snapshot, event and scope this card showed.
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
      // A newer Snapshot moves the whole Reader to its reload state; the card
      // still shows its own refusal.
      if (isStaleSnapshotError(reason)) reportWriteFailure(reason);
      throw reason;
    } finally {
      // Refused or not, show what is true now; a refresh failure never
      // replaces the write's own error. A newer Snapshot's targets and
      // comments never render under this Snapshot's header and code.
      try {
        const [nextTargets, nextAnnotations] = await Promise.all([
          fetchTargets(reviewId, targetList?.order ?? "recommended"),
          fetchAnnotations(reviewId),
        ]);
        if (nextTargets.revision_id === shownRevisionId) setTargetList(stabilizeTargetOrder(nextTargets));
        if (nextAnnotations.revision_id === shownRevisionId) {
          setAnnotations(nextAnnotations.annotations);
          setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
        }
        await loadProjection();
      } catch {
        // the next poll or navigation refreshes
      }
    }
  }, [reviewId, projection, targets, targetList?.order, stabilizeTargetOrder, loadProjection, shownRevisionId, reportWriteFailure]);

  const runAiReviewOnBrief = useCallback(async (effort: "thorough" | "deep") => {
    if (!reviewId || !projection) throw new Error("Review is not ready for AI review.");
    try {
      const actionId = globalThis.crypto.randomUUID();
      const response = await postAiReview(
        reviewId,
        projection.revision_id,
        effort,
        BROWSER_AI_REVIEW_TRIGGER,
        actionId,
      );
      setAiReviewRun(response.ai_review);
      return response.ai_review;
    } finally {
      try {
        const [nextTargets, nextAnnotations] = await Promise.all([
          fetchTargets(reviewId, targetList?.order ?? "recommended"),
          fetchAnnotations(reviewId),
        ]);
        if (nextTargets.revision_id === shownRevisionId) setTargetList(stabilizeTargetOrder(nextTargets));
        if (nextAnnotations.revision_id === shownRevisionId) {
          setAnnotations(nextAnnotations.annotations);
          setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
        }
        await loadProjection();
      } catch {
        // live state / the next navigation refreshes
      }
    }
  }, [reviewId, projection, targetList?.order, stabilizeTargetOrder, loadProjection, shownRevisionId]);

  const cancelAiReviewOnBrief = useCallback(async (jobId: string) => {
    if (!reviewId || !projection || projection.revision_id !== overview?.revision.id) {
      throw new Error("The AI Review Snapshot changed. Refresh before cancelling.");
    }
    const result = await cancelAiReview(reviewId, projection.revision_id, jobId);
    if (result.ai_review.status === "unavailable") {
      throw new Error("This AI job could not be stopped; its status may have changed.");
    }
    await loadProjection(true);
  }, [reviewId, projection, overview?.revision.id, loadProjection]);

  const rerunAiReviewFreshOnBrief = useCallback(async (actionId: string) => {
    if (!reviewId || !projection) throw new Error("Review is not ready for AI review.");
    try {
      const response = await postAiReviewFresh(reviewId, projection.revision_id, actionId);
      setAiReviewRun(response.ai_review);
      return response.ai_review;
    } finally {
      try {
        const nextAnnotations = await fetchAnnotations(reviewId);
        if (nextAnnotations.revision_id === shownRevisionId) {
          setAnnotations(nextAnnotations.annotations);
          setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
        }
        await loadProjection();
      } catch {
        // live state / the next navigation refreshes
      }
    }
  }, [reviewId, projection, loadProjection, shownRevisionId]);

  const synthesizeOnBrief = useCallback(async () => {
    if (!reviewId || !projection) return;
    try {
      await postSynthesis(reviewId, projection.revision_id);
    } finally {
      try {
        const nextTargets = await fetchTargets(reviewId, targetList?.order ?? "recommended");
        if (nextTargets.revision_id === shownRevisionId) setTargetList(stabilizeTargetOrder(nextTargets));
        await loadProjection();
      } catch {
        // the next navigation refreshes
      }
    }
  }, [reviewId, projection, targetList?.order, stabilizeTargetOrder, loadProjection, shownRevisionId]);

  const synthesizeOnTour = useCallback(async () => {
    if (!reviewId || !projection) return;
    try {
      await postTourSynthesis(reviewId, projection.revision_id);
    } finally {
      try {
        await loadProjection();
      } catch {
        // the next navigation refreshes
      }
    }
  }, [reviewId, projection, loadProjection]);

  const loadBehaviorTarget = useCallback(async (behaviorKey: string) => {
    if (!reviewId) throw new Error("No review is open.");
    return (await fetchBehaviorTarget(reviewId, behaviorKey)).target;
  }, [reviewId]);

  // The digest is the one of the evidence block the Brief rendered; the server refuses any other.
  const judgeBehavior = useCallback(async (behaviorKey: string, state: BehaviorJudgmentState, evidenceDigest: string) => {
    if (!reviewId || !projection) return;
    await postBehaviorJudgment(reviewId, projection.revision_id, {
      behavior_key: behaviorKey,
      state,
      evidence_digest: evidenceDigest,
    });
    activeReviewFriction()?.judgment();
    await loadProjection();
  }, [reviewId, projection, loadProjection]);

  const narrateBehavior = useCallback(async () => {
    if (!reviewId || !projection) return;
    try {
      await postBehaviorNarrative(reviewId, projection.revision_id);
    } finally {
      try {
        await loadProjection();
      } catch {
        // the next navigation refreshes
      }
    }
  }, [reviewId, projection, loadProjection]);

  const recordManualCheck = useCallback(async (itemId: string, status: "PASS" | "FAIL" | "UNKNOWN", note: string) => {
    if (!reviewId || !projection) return;
    await postManualCheckResult(reviewId, projection.revision_id, { item_id: itemId, status, note });
    await loadProjection();
  }, [reviewId, projection, loadProjection]);

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
      setAnnotations(result.annotations);
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

  // ----- In-Review backtrail ------------------------------------------- //
  // Brief/Guided targets, Map nodes, Ask citations, Affected code and Code
  // Peek jump inside the Review and record where they left from; Back in the
  // header returns there exactly. The trail rides in the URL (reload-safe).
  const backtrail = useMemo(() => readReviewBacktrail(searchParams), [searchParams]);
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
  const [scrollRestoreTick, setScrollRestoreTick] = useState(0);
  const goBack = useCallback(() => {
    const trail = readReviewBacktrail(new URLSearchParams(window.location.search));
    const entry = trail[trail.length - 1];
    if (!entry) return;
    activeReviewFriction()?.back(entry.view);
    viewChosenRef.current = true;
    setLandingDecided(true);
    setSearchParams(() => writeReviewBacktrail(new URLSearchParams(entry.query), trail.slice(0, -1)));
    if (entry.related) void showRelatedSource(entry.related.path, entry.related.line);
    else setRelated(null);
    const target = entry.target ? targets.find((item) => item.target_id === entry.target) : undefined;
    const restoreScroll = () => {
      pendingScrollRef.current = entry.view === "map" ? null : entry;
      setScrollRestoreTick((tick) => tick + 1);
    };
    if (target && entry.view === "implementation") {
      // Aim at the target first; the exact offset is applied once it settled.
      pendingScrollRef.current = null;
      void scrollToTarget(target, "instant").then(restoreScroll);
      return;
    }
    if (target) setActiveTargetId(target.target_id);
    restoreScroll();
  }, [scrollToTarget, setActiveTargetId, setSearchParams, showRelatedSource, targets]);
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

  const selectPath = useCallback((path: string) => {
    if (isCompactReviewViewport()) setOutlineCollapsed(true);
    const visible = targetForPath(visibleTargets, path);
    if (visible) {
      void scrollToTarget(visible);
      return;
    }
    const anywhere = targetForPath(targets, path);
    if (anywhere) {
      setQuery("");
      setDeltaFocus(false);
      void scrollToTarget(anywhere);
      return;
    }
    if (!reviewId || targetList?.complete !== false || !overview) return;
    setQuery("");
    setDeltaFocus(false);
    revealImplementation();
    // Target metadata and source are independent reads. Start both together so
    // a far-file jump pays the slower one, not their sum. loadPath is
    // Snapshot-bound and deduplicated, so scrollToTarget's later load is free.
    void Promise.all([
      fetchTargetsForPath(reviewId, path, targetList.order),
      loadPath(reviewId, path),
    ]).then(([slice]) => {
      if (slice.revision_id !== overview.revision.id) return;
      const fetched = targetForPath(slice.targets, path);
      if (!fetched) return;
      setTargetList((current) => {
        if (!current || current.revision_id !== slice.revision_id) return current;
        const seen = new Set(current.targets.map((target) => target.target_id));
        const mergedTargets = [...current.targets, ...slice.targets.filter((target) => !seen.has(target.target_id))];
        const stabilized = stabilizeTargetOrder({ ...current, targets: mergedTargets });
        return {
          ...stabilized,
          outline: current.outline,
          progress: current.progress,
          movement: current.movement,
          complete: current.complete,
        };
      });
      window.requestAnimationFrame(() => void scrollToTarget(fetched, "instant"));
    });
  }, [loadPath, overview, reviewId, revealImplementation, scrollToTarget, stabilizeTargetOrder, targetList, targets, visibleTargets]);

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

  const navigateSearchMatch = useCallback((delta: number) => {
    // Steps the match set, not the stream. A file the stream only carries
    // because it holds an open draft matched nothing, and the counter this
    // control sits beside does not count it either.
    if (!query.trim() || filteredTargets.length === 0) return;
    const next = stepTarget(filteredTargets, activeTarget?.target_id ?? "", delta, { wrap: true });
    if (next) void scrollToTarget(next, "instant");
  }, [query, filteredTargets, activeTarget?.target_id, scrollToTarget]);

  const dismissSearch = useCallback(() => {
    setQuery("");
    searchInputRef.current?.blur();
    window.requestAnimationFrame(() => document.querySelector<HTMLElement>('[aria-current="location"]')?.focus());
  }, []);

  const changeOrder = useCallback(async (nextOrder: ReaderOrder) => {
    if (!reviewId || busy || targetList?.order === nextOrder) return;
    setBusy("order");
    try {
      const currentTargetId = activeTarget?.target_id ?? activeTargetId;
      const fetchedTargets = historical
        ? await fetchHistoricalTargets(reviewId, historicalRevisionKey, nextOrder)
        : await fetchTargets(reviewId, nextOrder);
      // An explicit order change is the one time the list order must move:
      // forget the stabilized order so the requested one takes effect.
      stableTargetOrderRef.current = [];
      const nextTargets = historical ? fetchedTargets : stabilizeTargetOrder(fetchedTargets);
      setTargetList(nextTargets);
      const survivor = nextTargets.targets.find((target) => target.target_id === currentTargetId)
        ?? firstActionable(nextTargets.targets);
      setActiveTargetId(survivor?.target_id ?? "");
      // Any navigation the reviewer starts after this claim (e.g. j while paths
      // load) owns the scroll; a late re-aim at the survivor would yank it back.
      const navigation = navigationRef.current;

      const queueTargets = deltaFocus
        ? nextTargets.targets.filter((target) => deltaActiveIds.has(target.target_id) && targetStillNeedsAttention(target))
        : nextTargets.targets;
      const paths = initialPaths(queueTargets);
      if (survivor && !paths.includes(survivor.path)) paths.unshift(survivor.path);
      const uniquePaths = [...new Set(paths)];
      revealPaths(uniquePaths);
      await loadPaths(reviewId, uniquePaths);
      queueNextPage(reviewId, orderedPaths(nextTargets.targets), changeLinesByPath(nextTargets.targets));
      if (survivor) {
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
          if (navigationRef.current === navigation) streamRef.current?.scrollToTarget(survivor, "instant");
        }));
      }
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, busy, targetList?.order, activeTarget?.target_id, activeTargetId, deltaFocus, deltaActiveIds, loadPaths, queueNextPage, revealPaths]);

  const updateTargetListAfterMark = useCallback((
    target: ReviewTarget,
    result: MarkResult,
    othersInFlight = false,
  ) => {
    setTargetList((current) => {
      if (!current) return current;
      const replacement = result.target ?? { ...target, state: result.recorded, changed_since_mark: false };
      const shown = current.targets.find((item) => item.target_id === target.target_id)?.state ?? target.state;
      const nextTargets = current.targets.map((item) => item.target_id === target.target_id ? replacement : item);
      // With other marks still in flight, the server's totals predate them;
      // taking them would briefly undo marks the reviewer already sees.
      if (othersInFlight) {
        return {
          ...current,
          targets: nextTargets,
          progress: progressFromTargets(nextTargets),
          outline: shiftOutlineState(current.outline, target.path, shown, replacement.state),
        };
      }
      return {
        ...current,
        targets: withPriorities(nextTargets, result.priorities),
        progress: result.progress ?? progressFromTargets(nextTargets),
        outline: result.outline ?? mergeOutline(current.outline, result.outline_updates),
      };
    });
  }, []);

  // Swaps targets in place while a judgment is in flight: shown at once, then
  // reconciled with the server's answer or put back when the write fails.
  // `expected` guards each swap, so a newer local change is never overwritten.
  const swapTargets = useCallback((
    swaps: ReadonlyArray<{ targetId: string; expected: MarkState; next: ReviewTarget }>,
  ) => {
    setTargetList((current) => {
      if (!current) return current;
      const byId = new Map(swaps.map((swap) => [swap.targetId, swap]));
      let outline = current.outline;
      let changed = false;
      const nextTargets = current.targets.map((item) => {
        const swap = byId.get(item.target_id);
        if (!swap || item.state !== swap.expected) return item;
        changed = true;
        outline = shiftOutlineState(outline, item.path, item.state, swap.next.state);
        return swap.next;
      });
      return changed
        ? { ...current, targets: nextTargets, progress: progressFromTargets(nextTargets), outline }
        : current;
    });
  }, []);

  const bulkReviewFile = useCallback(async (path: string) => {
    if (!reviewId || readOnly || busy) return;
    const pending = targets.filter((target) => target.path === path && target.state !== "reviewed");
    const unitKeys = pending.map((target) => target.unit_key);
    if (unitKeys.length === 0) return;
    // Not optimistic: the server owns eligibility, so nothing is painted
    // Reviewed until it answers. `busy` is the pending state meanwhile.
    setBusy(`bulk:${path}`);
    try {
      const result = await postBulkReviewed(reviewId, unitKeys, overview?.revision.id ?? "");
      if (result.marked.length > 0) activeReviewFriction()?.judgment();
      const marked = new Set(result.marked.map((item) => item.unit_key));
      setTargetList((current) => {
        if (!current) return current;
        const nextTargets = withPriorities(current.targets.map((target) => marked.has(target.unit_key)
          ? { ...target, state: "reviewed" as const, changed_since_mark: false, reviewed_revision_id: current.revision_id }
          : target), result.priorities);
        return {
          ...current,
          targets: nextTargets,
          progress: result.progress ?? progressFromTargets(nextTargets),
          outline: result.outline ?? mergeOutline(current.outline, result.outline_updates),
        };
      });
      setBulkUndo(result.marked.length > 0 ? {
        path,
        marks: result.marked.map((item) => ({ unit_key: item.unit_key, event_id: item.event_id })),
      } : null);
      if (result.skipped.length > 0) {
        const reasons = [...new Set(result.skipped.map((item) => item.reason))];
        setError(`${result.marked.length} marked reviewed · ${result.skipped.length} kept for individual review · ${reasons.join(" · ")}`);
      } else {
        setError("");
      }
    } catch (reason) {
      reportWriteFailure(reason);
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, targets, overview?.revision.id, reportWriteFailure]);

  const undoBulkReview = useCallback(async () => {
    if (!reviewId || !bulkUndo || readOnly || busy) return;
    setBusy("bulk-undo");
    try {
      const result = await undoBulkReviewed(reviewId, bulkUndo.marks, overview?.revision.id ?? "");
      const undone = new Set(result.undone.map((item) => item.unit_key));
      setTargetList((current) => {
        if (!current) return current;
        const nextTargets = withPriorities(current.targets.map((target) => undone.has(target.unit_key)
          ? { ...target, state: "unreviewed" as const, changed_since_mark: false, reviewed_revision_id: "" }
          : target), result.priorities);
        return {
          ...current,
          targets: nextTargets,
          progress: result.progress ?? progressFromTargets(nextTargets),
          outline: result.outline ?? mergeOutline(current.outline, result.outline_updates),
        };
      });
      setBulkUndo(null);
      if (result.skipped.length > 0) {
        setError(`Undo kept ${result.skipped.length} newer reviewer judgment${result.skipped.length === 1 ? "" : "s"} intact.`);
      } else {
        setError("");
      }
    } catch (reason) {
      reportWriteFailure(reason);
    } finally {
      setBusy("");
    }
  }, [reviewId, bulkUndo, readOnly, busy, overview?.revision.id, reportWriteFailure]);

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
  const acknowledgeQueued = useCallback((notice: string) => {
    setHostedNotice(notice);
    setHostedVersion((value) => value + 1);
    setError("");
  }, []);
  // Bumped on each failed mark so a concurrent mark's success cannot wipe its error.
  const markFailureSeqRef = useRef(0);
  const markTarget = useCallback(async (target: ReviewTarget, state: MarkState, advance = false) => {
    if (!reviewId || readOnly || busy || target.fingerprint_method === "revision_compare") return;
    if (markingRef.current.has(target.target_id)) return;
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
        return;
      }
      updateTargetListAfterMark(target, result, markingRef.current.size > 1);
      setCommittedMarks((count) => count + 1);
      if (result.downgraded) setError(result.note);
      else if (markFailureSeqRef.current === failureSeq) setError("");
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
    } finally {
      markingRef.current.delete(target.target_id);
    }
  }, [reviewId, readOnly, busy, swapTargets, updateTargetListAfterMark, visibleTargets, scrollToTarget, overview?.revision.id, reportWriteFailure, acknowledgeQueued]);
  const readOnlyNotice = accessLost
    ? ACCESS_LOST_NOTICE[accessLost]
    : forcedReadOnly
    ? (extensions?.readOnlyReason || "This Review is read-only.")
    : runningDraft
    ? "Create a Snapshot first. Comments and review decisions need frozen bytes; the Snapshot also unlocks Brief, Map, and architecture analysis."
    : historical
    ? "Historical Snapshots are read-only. Go back to the latest Snapshot to comment."
    : overview?.session.status === "finished"
      ? "This review is finished. Reopen it to comment or change judgments."
      : overview?.session.status === "archived"
        ? "This review is archived. Restore it to comment."
        : "Reload the review before commenting on the new Snapshot.";
  // Opening a composer that can never submit is a silent dead end: refuse new
  // drafts on a read-only reader and say why. An existing draft stays editable.
  const requestDraft = useCallback((next: Draft | null) => {
    if (next && readOnly && !draft) {
      setError(readOnlyNotice);
      return;
    }
    setDraft(next);
  }, [readOnly, draft, readOnlyNotice]);

  const openComments = useCallback(() => {
    if (runningDraft) {
      setError(readOnlyNotice);
      return;
    }
    setCommentsFilter(undefined);
    setCommentsOpen(true);
  }, [readOnlyNotice, runningDraft]);
  const commentTarget = useCallback((target: ReviewTarget) => {
    const range = targetCommentRange(target);
    setActiveTargetId(target.target_id);
    if (readOnly) {
      setError(readOnlyNotice);
      return;
    }
    if (range) {
      setDraft({
        path: target.path,
        ...range,
        targetUnitKey: target.fingerprint_method === "revision_compare" ? undefined : target.unit_key,
      });
    } else {
      setDraft({
        path: target.path,
        startLine: 0,
        endLine: 0,
        side: "additions",
        fileLevel: true,
        targetUnitKey: target.fingerprint_method === "revision_compare" ? undefined : target.unit_key,
      });
    }
  }, [readOnly, readOnlyNotice]);

  const createAnnotation = useCallback(async (input: AnnotationDraft) => {
    if (!reviewId || readOnly || busy) return;
    setBusy("comment");
    try {
      const result = await postAnnotation(reviewId, input, overview?.revision.id ?? "");
      activeReviewFriction()?.judgment();
      // The write has succeeded. Clear the composer before any secondary read
      // so a failed refetch can never leave the same text ready to submit twice.
      setDraft(null);
      if (isQueuedWrite(result)) {
        acknowledgeQueued(queuedWriteNotice(result));
        return;
      }
      if (result.target && result.progress) {
        setTargetList((current) => current ? {
          ...current,
          targets: withPriorities(
            current.targets.map((target) => target.unit_key === result.target!.unit_key ? result.target! : target),
            result.priorities,
          ),
          progress: result.progress!,
          outline: result.outline ?? mergeOutline(current.outline, result.outline_updates),
        } : current);
      }
      try {
        const nextAnnotations = await fetchAnnotations(reviewId);
        // A newer Snapshot's discussion never replaces this Snapshot's.
        if (nextAnnotations.revision_id === (overview?.revision.id ?? "")) {
          setAnnotations(nextAnnotations.annotations);
          setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
          setFeedbackDelivery(nextAnnotations.delivery ?? null);
        }
        setError("");
      } catch (reason) {
        setError(`Comment saved, but the discussion list is out of date: ${String(reason instanceof Error ? reason.message : reason)}`);
      }
    } catch (reason) {
      reportWriteFailure(reason);
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, overview?.revision.id, reportWriteFailure, acknowledgeQueued]);

  const mergeProposal = useCallback((proposal: ReviewChangeProposal) => {
    setProposals((current) => {
      const exists = current.some((item) => item.id === proposal.id);
      return exists
        ? current.map((item) => item.id === proposal.id ? proposal : item)
        : [...current, proposal];
    });
  }, []);

  const beginChangeProposal = useCallback(async (selectionDraft: Draft, sourceAction: "suggest" | "edit" | "quick" = "suggest") => {
    if (!reviewId || readOnly || busy || selectionDraft.fileLevel || selectionDraft.side !== "additions") return;
    const detail = detailsRef.current[selectionDraft.path];
    const localText = selectionDraft.selectedRevisionId === overview?.revision.id && selectionDraft.selectedText !== undefined
      ? selectionDraft.selectedText
      : detail && detail.revision_id === overview?.revision.id && !staleDetailPathsRef.current.has(selectionDraft.path)
        ? selectedNewSideTextFromPatch(detail.patch, selectionDraft.startLine, selectionDraft.endLine)
        : null;
    const localSelection = localText === null || !overview ? null : {
      revision_id: overview.revision.id,
      path: selectionDraft.path,
      start_line: selectionDraft.startLine,
      end_line: selectionDraft.endLine,
      side: "additions" as const,
      text: localText,
    };
    const pending: Draft = {
      ...selectionDraft,
      mode: "proposal",
      sourceAction,
      replacementText: localSelection?.text,
      proposalIntent: "",
      proposalId: undefined,
    };
    const isPending = (current: Draft | null): current is Draft => Boolean(current
      && current.mode === "proposal"
      && current.path === pending.path
      && current.startLine === pending.startLine
      && current.endLine === pending.endLine);
    setProposalSelection(localSelection);
    setActiveProposal(null);
    setDraft(pending);
    draftRef.current = pending;
    if (localSelection) {
      setError("");
      return;
    }
    setBusy("proposal-selection");
    try {
      const selection = await fetchProposalSelection(
        reviewId,
        selectionDraft.path,
        selectionDraft.startLine,
        selectionDraft.endLine,
        selectionDraft.side,
      );
      if (!isPending(draftRef.current)) return;
      setProposalSelection(selection);
      setDraft((current) => (isPending(current) ? { ...current, replacementText: current.replacementText ?? selection.text } : current));
      setError("");
    } catch (reason) {
      if (isPending(draftRef.current)) setDraft(null);
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, overview]);

  const createProposal = useCallback(async () => {
    if (!reviewId || !overview || !draft || draft.mode !== "proposal" || !proposalSelection || readOnly || busy) return;
    setBusy("proposal-create");
    try {
      const result = await createChangeProposal(reviewId, {
        expected_revision_id: proposalSelection.revision_id,
        path: draft.path,
        start_line: draft.startLine,
        end_line: draft.endLine,
        side: "additions",
        replacement_text: draft.replacementText ?? "",
        target_unit_key: draft.targetUnitKey,
        intent: draft.proposalIntent ?? "",
      });
      mergeProposal(result.proposal);
      setActiveProposal(result.proposal);
      setDraft((current) => current ? { ...current, proposalId: result.proposal.id } : current);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, overview, draft, proposalSelection, readOnly, busy, mergeProposal]);

  const createAndApplyProposal = useCallback(async () => {
    if (!reviewId || !draft || draft.mode !== "proposal" || !proposalSelection || readOnly || busy) return;
    setBusy("proposal-apply");
    let createdProposal: ReviewChangeProposal | null = null;
    try {
      const created = await createChangeProposal(reviewId, {
        expected_revision_id: proposalSelection.revision_id,
        path: draft.path,
        start_line: draft.startLine,
        end_line: draft.endLine,
        side: "additions",
        replacement_text: draft.replacementText ?? "",
        target_unit_key: draft.targetUnitKey,
        intent: draft.proposalIntent ?? "",
      });
      createdProposal = created.proposal;
      mergeProposal(created.proposal);
      const result = await applyChangeProposal(created.proposal.id);
      mergeProposal(result.proposal);
      setSourceState(result.source_state);
      setDismissedSourceFingerprint("");
      setDraft(null);
      setProposalSelection(null);
      setActiveProposal(null);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
      // Once the proposal exists, hold the composer on it (as Apply does): left on
      // the editable form, the next Apply would create a second durable proposal.
      const createdId = createdProposal?.id ?? "";
      if (createdProposal) {
        setActiveProposal(createdProposal);
        setDraft((current) => current ? { ...current, proposalId: createdId } : current);
      }
      try {
        const next = await fetchChangeProposals(reviewId);
        setProposals(next.proposals);
        const refreshed = next.proposals.find((item) => item.id === createdId);
        if (refreshed) setActiveProposal(refreshed);
      } catch {
        // Preserve the source mutation error; proposal refresh is best effort.
      }
    } finally {
      setBusy("");
    }
  }, [reviewId, draft, proposalSelection, readOnly, busy, mergeProposal]);

  const applyProposal = useCallback(async () => {
    if (!activeProposal || readOnly || busy) return;
    setBusy("proposal-apply");
    try {
      const result = await applyChangeProposal(activeProposal.id);
      mergeProposal(result.proposal);
      setActiveProposal(result.proposal);
      setSourceState(result.source_state);
      setDismissedSourceFingerprint("");
      setDraft(null);
      setProposalSelection(null);
      setActiveProposal(null);
      setError("");
    } catch (reason) {
      const message = String(reason instanceof Error ? reason.message : reason);
      setError(message);
      try {
        if (reviewId) {
          const next = await fetchChangeProposals(reviewId);
          setProposals(next.proposals);
          const conflicted = next.proposals.find((item) => item.id === activeProposal.id) ?? null;
          setActiveProposal(conflicted);
        }
      } catch {
        // Preserve the apply error; proposal refresh is best-effort recovery.
      }
    } finally {
      setBusy("");
    }
  }, [activeProposal, readOnly, busy, mergeProposal, reviewId]);

  const closeProposalComposer = useCallback(() => {
    setProposalSelection(null);
    setActiveProposal(null);
    setDraft(null);
  }, []);

  const refreshFeedbackState = useCallback(async (done = "Published") => {
    if (!reviewId) return;
    try {
      const nextAnnotations = await fetchAnnotations(reviewId);
      setAnnotations(nextAnnotations.annotations);
      setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
      setFeedbackDelivery(nextAnnotations.delivery ?? null);
    } catch (reason) {
      setError(`${done}, but the comment list is out of date: ${String(reason instanceof Error ? reason.message : reason)}`);
    }
  }, [reviewId]);

  const setAnnotationState = useCallback(async (id: string, state: AnnotationState) => {
    if (!reviewId || readOnly || busy) return;
    const previous = annotations.find((item) => item.id === id)?.state;
    // Show the resolve or reopen at once; the refetch below reconciles it.
    const showState = (from: AnnotationState | undefined, to: AnnotationState) => setAnnotations((current) => current.map(
      (item) => item.id === id && item.state === from ? { ...item, state: to } : item,
    ));
    setBusy("comment");
    showState(previous, state);
    try {
      try {
        await patchAnnotation(id, { state }, overview?.revision.id ?? "");
      } catch (reason) {
        if (previous) showState(state, previous);
        throw reason;
      }
      const nextAnnotations = await fetchAnnotations(reviewId);
      setAnnotations(nextAnnotations.annotations);
      setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
      setFeedbackDelivery(nextAnnotations.delivery ?? null);
      setError("");
    } catch (reason) {
      reportWriteFailure(reason);
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, annotations, overview?.revision.id, reportWriteFailure]);

  const setFindingDisposition = useCallback(async (
    id: string,
    disposition: FindingDisposition,
    reason: FindingDismissalReason = "",
  ) => {
    if (!reviewId || readOnly || busy) return;
    const previous = annotations.find((item) => item.id === id);
    if (!previous || previous.source !== "ai_review") return;
    setBusy("finding");
    try {
      const result = await patchAnnotation(id, {
        finding_disposition: disposition,
        finding_disposition_reason: reason,
      }, overview?.revision.id ?? "");
      activeReviewFriction()?.judgment();
      setAnnotations((current) => current.map((item) => item.id === id ? result.annotation : item));
      // Finding disposition is deliberately orthogonal to human Review marks.
      setError("");
    } catch (failure) {
      reportWriteFailure(failure);
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, annotations, overview?.revision.id, reportWriteFailure]);

  const openContext = useCallback((target: ReviewTarget, tab: ContextDrawerTab = "context") => {
    contextReturnFocusRef.current = focusReturnTarget();
    setAskOpen(false);
    setActiveTargetId(target.target_id);
    setContextTab(tab);
    setContextOpen(true);
    if (reviewId) void loadPath(reviewId, target.path);
  }, [reviewId, loadPath]);

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

  const askSelection = useCallback((selection: Draft) => {
    const target = selection.targetUnitKey
      ? targets.find((item) => item.unit_key === selection.targetUnitKey)
      : null;
    setDraft(null);
    openAsk({
      path: selection.path,
      symbol: target?.symbol || target?.label || "",
      target_unit_key: target?.fingerprint_method === "revision_compare"
        ? undefined
        : selection.targetUnitKey,
      side: selection.side,
      start_line: selection.startLine,
      end_line: selection.endLine,
    });
  }, [openAsk, targets]);

  const toggleContextPin = useCallback(() => {
    setContextPinned((current) => {
      const next = !current;
      window.sessionStorage.setItem(CONTEXT_PIN_KEY, next ? "1" : "0");
      return next;
    });
  }, []);

  const loadMore = useCallback((automatic = false) => {
    if (!reviewId) return;
    let readyHidden = pagePaths.filter(
      (path) => !revealedPathsRef.current.has(path) && Boolean(detailsRef.current[path] || fileErrors[path]),
    );
    if (automatic) {
      // Infinite-scroll may only append below the mounted window. After an
      // explicit jump to a hidden file, revealing ready predecessors above it
      // would move the reviewer's settled destination without another action.
      let lastRevealedIndex = -1;
      for (let index = 0; index < pagePaths.length; index += 1) {
        if (revealedPathsRef.current.has(pagePaths[index])) lastRevealedIndex = index;
      }
      readyHidden = readyHidden.filter((path) => pagePaths.indexOf(path) > lastRevealedIndex);
    }
    const batch = pathsForChangeBudget(readyHidden, fileChangeLines, LOAD_MORE_CHANGE_BUDGET, LOAD_MORE_MAX_FILES);
    if (batch.length === 0) return;
    revealPaths(batch);
    queueNextPage(reviewId, allPathOrder, fileChangeLines);
  }, [reviewId, allPathOrder, pagePaths, fileChangeLines, fileErrors, queueNextPage, revealPaths]);
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
      setOverview(nextOverview);
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
        setDraft({
          ...draft,
          startLine: 0,
          endLine: 0,
          fileLevel: false,
          targetUnitKey: undefined,
          recovered: true,
        });
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
      setTargetList(nextTargets);
      setAnnotations(nextAnnotations.annotations);
      setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
      setEvidence(nextEvidence.evidence);
      loadSurfaces(reviewId, nextOverview.revision.id, nextTargets.targets, isCurrentCapture);
      stranded = false;
      const nextInfo = nextOverview.refreshed ?? null;
      setRevisionInfo(nextInfo?.created ? nextInfo : null);
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
      if (stranded) setRevisionAdvanced(true);
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
      if (await loadReader(reviewId)) setRevisionAdvanced(false);
    } finally {
      setBusy("");
    }
  }, [reviewId, busy, loadReader, overview?.revision.id]);

  useEffect(() => {
    setOutcomeOverride(null);
  }, [overview?.revision.id]);

  const closeFinishSheet = useCallback(() => {
    setFinishSheetOpen(false);
    setFinishSendError("");
    setFinishError("");
  }, []);

  // Closing the outcome sheet always lands on the primary review action, whatever opened it.
  const focusPrimaryAfterFinish = useCallback((event: Event) => {
    event.preventDefault();
    finishButtonRef.current?.focus();
  }, []);

  const openFinishSheet = useCallback(async () => {
    // Finishing is a review-level judgment, so it is blocked while the reader is
    // stranded on a superseded revision for the same reason marking is. It is
    // *not* blocked by `readOnly` at large: on an archived session this button
    // is "Restore review", the one control that has to keep working.
    if (!reviewId || busy || revisionAdvanced || historical) return;
    if (overview?.session.status === "finished" || overview?.session.status === "archived") {
      setBusy("finish");
      try {
        const next = await finishReview(reviewId, "open");
        setFinishSummary(next);
        setFinishSheetOpen(false);
        setOverview((current) => current ? { ...current, session: { ...current.session, status: next.status } } : current);
        setError("");
      } catch (reason) {
        reportWriteFailure(reason);
      } finally {
        setBusy("");
      }
      return;
    }
    setBusy("finish-preview");
    try {
      const summary = await fetchFinishReview(reviewId);
      // The preview has completed before the sheet appears. Never render the
      // verdict action as "Recording…" while nothing is being recorded yet.
      setBusy("");
      setFinishSummary(summary);
      setError("");
      // Do not mount the verdict action under the pointer/key event that opened
      // it from a dropdown. Let that input sequence finish first so it cannot
      // click through and accidentally submit the default verdict.
      window.requestAnimationFrame(() => setFinishSheetOpen(true));
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
      setBusy("");
    }
  }, [reviewId, busy, revisionAdvanced, overview?.session.status, historical, reportWriteFailure]);

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
      setAnnotations(nextAnnotations.annotations);
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
    try {
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
        const prepared = await exportFeedback(reviewId);
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
        setFinishSheetOpen(false);
        setError("");
        return;
      }
      stage = "finish";
      const next = await finishReview(reviewId, "finished", overview?.revision.id ?? "");
      setFinishSummary(next);
      setFinishSheetOpen(false);
      setOverview((current) => current ? { ...current, session: { ...current.session, status: next.status } } : current);
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
      setOverview((current) => current ? { ...current, session: { ...current.session, status: next.status } } : current);
    } catch (reason) {
      reportWriteFailure(reason);
    }
  }, [reviewId, overview?.revision.id, reportWriteFailure]);

  const prepareFeedback = useCallback(async (): Promise<FeedbackExport | null> => {
    if (!reviewId || historical) return null;
    setBusy("feedback");
    try {
      const nextFeedback = await exportFeedback(reviewId);
      setFeedback(nextFeedback);
      if (nextFeedback.status) setFeedbackStatus(nextFeedback.status);
      setFeedbackDelivery(nextFeedback.delivery);
      setDelivery(null);
      setError("");
      return nextFeedback;
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
      return null;
    } finally {
      setBusy("");
    }
  }, [reviewId, historical]);

  /** "Send now" and the waiting chip: the Send sheet on freshly prepared feedback. */
  const openSendSheet = useCallback(async () => {
    if (busy) return;
    if (await prepareFeedback()) setSendSheetOpen(true);
  }, [busy, prepareFeedback]);

  const sendFeedbackToAgent = useCallback(async (options: { copied?: boolean } = {}) => {
    if (!reviewId || !feedback || busy) return;
    setBusy("delivery");
    try {
      await handOff(feedback, options);
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, feedback, busy, handOff]);

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

  const openMapSelection = useCallback(async (selection: ReviewMapSelection) => {
    if (selection.exactness !== "snapshot" || !reviewId) return;
    const target = (selection.target_ids ?? [])
      .map((targetId) => targets.find((item) => item.target_id === targetId))
      .find(Boolean);
    const path = selection.path ?? "";
    const inPatch = target ?? (path ? targets.find((item) => item.path === path) : undefined);
    if (!inPatch && !path) return;
    // The panel opens under the map, so close the map first or the loading
    // state would be hidden behind it. The Map location rides the backtrail,
    // so Back returns to the map where the reviewer left it.
    withBacktrail(() => {
      setMapLocation(null);
      if (inPatch) void scrollToTarget(inPatch);
      else void showRelatedSource(path, selection.line || 1);
    });
  }, [reviewId, targets, scrollToTarget, setMapLocation, showRelatedSource, withBacktrail]);

  // Map selections in the patch open the file's dedicated diff in a new tab,
  // landing on the mapped target, so the map keeps its place in this one.
  const mapSelectionHref = useCallback((selection: ReviewMapSelection): string | null => {
    if (selection.exactness !== "snapshot" || !reviewId) return null;
    const target = (selection.target_ids ?? [])
      .map((targetId) => targets.find((item) => item.target_id === targetId))
      .find(Boolean);
    if (target) return dedicatedReviewItemUrl({ kind: "file", path: target.path, targetId: target.target_id });
    const path = selection.path ?? "";
    return path && targets.some((item) => item.path === path)
      ? dedicatedReviewItemUrl({ kind: "file", path })
      : null;
  }, [reviewId, targets]);

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

  // Projection items are the one canonical cohort identity. Brief renders them
  // directly; Guided Review uses step.semantic_key; Code shows the active cohort;
  // Map chapters reuse the same id/title/path set.
  const cohorts = reviewCohorts(projection, targets);
  const activeCohort = cohortForTarget(cohorts, activeTarget?.target_id);
  const mapChapters = cohorts.map((cohort) => ({
    id: cohort.id,
    label: cohort.title,
    paths: cohort.paths,
  }));
  const earnedPersistentViews = persistentReviewViews(projection, targets, briefCapable);
  const availableViews: ReviewPersistentView[] = (["brief", "implementation", "map"] as const).filter((candidate) => (
    earnedPersistentViews.includes(candidate)
    || (candidate === "brief" && briefCapable && (largeStartupIncomplete || autoLargeReview || view === "brief" || view === "tour"))
    || (candidate === "map" && view === "map")
  ));

  // The palette is a Radix dialog: it returns focus to its opener, and a dialog
  // or view opened from it takes focus after that return.
  const openCommands = useCallback((mode: "actions" | "shortcuts") => {
    setCommandMode(mode);
  }, []);

  const closeCommands = useCallback(() => {
    setCommandMode(null);
  }, []);

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

  const changeGuidedCohort = useCallback((cohortId: string) => {
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      params.set(REVIEW_VIEW_PARAM, "tour");
      params.set(GUIDED_COHORT_PARAM, cohortId);
      return params;
    });
  }, [setSearchParams]);

  const openMap = useCallback(() => changeView("map"), [changeView]);

  const askMapSelection = useCallback((selection: ReviewMapSelection, label: string) => {
    openAsk({ context: selection, label });
  }, [openAsk]);

  const closeMap = useCallback(() => changeView("implementation"), [changeView]);

  const openOverview = useCallback(() => changeView("brief"), [changeView]);

  const focusBriefAfterCommand = useCallback(() => {
    let attempts = 0;
    const focusWhenMounted = () => {
      const root = document.querySelector<HTMLElement>('[data-review-view-root="brief"]');
      if (root) {
        root.focus({ preventScroll: true });
        return;
      }
      attempts += 1;
      if (attempts < 60) window.requestAnimationFrame(focusWhenMounted);
    };
    window.requestAnimationFrame(focusWhenMounted);
  }, []);

  const closeOverview = useCallback(() => changeView("implementation"), [changeView]);

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

  // PK2 landed history: one plain GET per Snapshot, never AI or background work.
  // The Map offers selection history only once the server reports Project
  // Knowledge configured for this repository, so without it the Map is unchanged.
  const projectContextCacheRef = useRef(new Map<string, Promise<ReviewProjectContext | null>>());
  const [projectKnowledgeReview, setProjectKnowledgeReview] = useState("");
  const loadProjectContext = useCallback((): Promise<ReviewProjectContext | null> => {
    if (!reviewId || architectureRevisionNumber < 0) return Promise.resolve(null);
    const cache = projectContextCacheRef.current;
    const key = `${reviewId}:${architectureRevisionNumber}`;
    const cached = cache.get(key);
    if (cached) return cached;
    const loadedFor = reviewId;
    const request = fetchReviewProjectContext(reviewId, architectureRevisionNumber, "brief").then((payload) => {
      const context = payload.project_context;
      if (context.available) setProjectKnowledgeReview(loadedFor);
      return context;
    });
    request.catch(() => { if (cache.get(key) === request) cache.delete(key); });
    cache.set(key, request);
    while (cache.size > ARCHITECTURE_DELTA_CACHE_LIMIT) {
      const oldest = cache.keys().next().value;
      if (oldest === undefined) break;
      cache.delete(oldest);
    }
    return request;
  }, [architectureRevisionNumber, reviewId]);
  useEffect(() => {
    if (view === "map") loadProjectContext().catch(() => undefined);
  }, [loadProjectContext, view]);
  const loadProjectHistory = useCallback(
    (selection: ReviewProjectContextSelection): Promise<ReviewProjectContext | null> =>
      fetchReviewProjectContext(reviewId, architectureRevisionNumber, "map", selection).then((payload) => payload.project_context),
    [architectureRevisionNumber, reviewId],
  );
  const projectHistoryAvailable = Boolean(reviewId) && projectKnowledgeReview === reviewId;

  // The dependency projection: package semantics in the Brief, and before raw
  // lockfile lines in Code. Unavailable means no semantics, never "no change".
  const [dependencyReview, setDependencyReview] = useState<ReviewDependencyProjection | null>(null);
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
  const projectionFiles = projection?.material?.files;
  const streamFileSemantics = useMemo<ReviewStreamFileSemantics>(() => ({
    roles: new Map<string, ReviewFileRole>((projectionFiles ?? []).map((file) => [
      file.path,
      { role: file.role, source: file.source, collapsedDefault: file.collapsed_default },
    ])),
    dependencies: dependencyReview,
  }), [dependencyReview, projectionFiles]);
  const openArchitecture = useCallback((nodeId: string) => {
    withBacktrail(() => {
      setMapLocation({ level: "architecture", projection: "architecture_delta", focus: "", node: nodeId, edge: "", path: "" });
    });
  }, [setMapLocation, withBacktrail]);

  const validateSurfaceRuntime = useCallback(async (surface: ReviewSurfaceInfo) => {
    if (!reviewId || !surface.runtime) return;
    const runRevisionId = overview?.revision.id ?? "";
    if (!runRevisionId) return;
    const key = `${surface.provider}:${surface.id}`;
    if (runtimeValidationBusyRef.current.has(key)) return;

    const runGeneration = surfaceGenerationRef.current;
    const stillCurrent = () => surfaceGenerationRef.current === runGeneration
      && surfaceIdentityRef.current === `${reviewId}:${runRevisionId}`;

    runtimeValidationBusyRef.current.add(key);
    setRuntimeValidationBusy(new Set(runtimeValidationBusyRef.current));
    setRuntimeValidations((current) => ({
      ...current,
      [key]: { ...current[key], error: undefined },
    }));

    try {
      let response = await runRuntimeValidation(
        reviewId,
        surface.provider,
        surface.id,
        runRevisionId,
        "new",
      );
      if (!stillCurrent()) return;
      setRuntimeValidations((current) => ({
        ...current,
        [key]: { response, error: undefined },
      }));

      let status = String(response.runtime_validation.status ?? "").toUpperCase();
      let jobId = response.runtime_validation.job?.job_id ?? "";
      while (
        jobId
        && ["QUEUED", "RUNNING", "CANCEL_REQUESTED"].includes(status)
        && stillCurrent()
      ) {
        await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 800));
        if (!stillCurrent()) return;
        response = await fetchRuntimeValidationJob(
          reviewId,
          jobId,
          surface.provider,
          surface.id,
          runRevisionId,
          "new",
        );
        if (!stillCurrent()) return;
        setRuntimeValidations((current) => ({
          ...current,
          [key]: { response, error: undefined },
        }));
        status = String(response.runtime_validation.status ?? "").toUpperCase();
        jobId = response.runtime_validation.job?.job_id ?? jobId;
      }

      if (response.evidence && stillCurrent()) {
        try {
          const evidenceResult = await fetchReviewEvidence(reviewId, runRevisionId);
          if (stillCurrent()) setEvidence(evidenceResult.evidence);
        } catch {
          // The validation result is still authoritative; the evidence drawer can
          // retry its normal load if this opportunistic refresh fails.
        }
        if (stillCurrent()) void loadProjection();
      }
    } catch (reason) {
      if (!stillCurrent()) return;
      setRuntimeValidations((current) => ({
        ...current,
        [key]: {
          ...current[key],
          error: String(reason instanceof Error ? reason.message : reason),
        },
      }));
    } finally {
      if (stillCurrent()) {
        runtimeValidationBusyRef.current.delete(key);
        setRuntimeValidationBusy(new Set(runtimeValidationBusyRef.current));
      }
    }
  }, [loadProjection, overview?.revision.id, reviewId]);

  const cancelSurfaceRuntimeValidation = useCallback(async (
    surface: ReviewSurfaceInfo,
    jobId: string,
  ) => {
    if (!reviewId || !jobId) return;
    const runRevisionId = overview?.revision.id ?? "";
    if (!runRevisionId) return;
    const key = `${surface.provider}:${surface.id}`;
    try {
      const response = await cancelRuntimeValidationJob(
        reviewId,
        jobId,
        surface.provider,
        surface.id,
        runRevisionId,
        "new",
      );
      if (surfaceIdentityRef.current !== `${reviewId}:${runRevisionId}`) return;
      setRuntimeValidations((current) => ({
        ...current,
        [key]: { response, error: undefined },
      }));
    } catch (reason) {
      if (surfaceIdentityRef.current !== `${reviewId}:${runRevisionId}`) return;
      setRuntimeValidations((current) => ({
        ...current,
        [key]: {
          ...current[key],
          error: String(reason instanceof Error ? reason.message : reason),
        },
      }));
    }
  }, [overview?.revision.id, reviewId]);

  const executeSurface = useCallback(async (surface: ReviewSurfaceInfo, compare: boolean) => {
    if (!reviewId || !surface.runtime) return;
    const key = `${surface.provider}:${surface.id}`;
    // Busy per surface: a run of another surface (the automatic API run in the
    // background) never swallows this one's "Run check".
    if (surfaceBusyRef.current.has(key)) return;
    // A run belongs to the Snapshot it started on. Its result (or error) is
    // committed only while that Snapshot and request generation are still shown.
    const runRevisionId = overview?.revision.id ?? "";
    const runGeneration = surfaceGenerationRef.current;
    const stillCurrent = () => surfaceGenerationRef.current === runGeneration
      && surfaceIdentityRef.current === `${reviewId}:${runRevisionId}`;
    surfaceBusyRef.current.add(key);
    setSurfaceBusy(new Set(surfaceBusyRef.current));
    setSurfaceRuns((current) => ({ ...current, [key]: { ...current[key], error: undefined } }));
    try {
      if (compare) {
        const [oldResponse, newResponse] = await Promise.all([
          runReviewSurface(reviewId, surface.provider, surface.id, "old", runRevisionId),
          runReviewSurface(reviewId, surface.provider, surface.id, "new", runRevisionId),
        ]);
        if (!stillCurrent()) return;
        // Both sides commit together or not at all.
        setSurfaceRuns((current) => ({
          ...current,
          [key]: { old: oldResponse.result, new: newResponse.result },
        }));
      } else {
        const response = await runReviewSurface(
          reviewId, surface.provider, surface.id, "new", runRevisionId,
        );
        if (!stillCurrent()) return;
        setSurfaceRuns((current) => ({
          ...current,
          [key]: { ...current[key], new: response.result, error: undefined },
        }));
      }
    } catch (reason) {
      if (!stillCurrent()) return;
      setSurfaceRuns((current) => ({
        ...current,
        [key]: {
          ...current[key],
          error: String(reason instanceof Error ? reason.message : reason),
        },
      }));
    } finally {
      // An older Snapshot's run no longer owns a busy state (see below).
      if (stillCurrent()) {
        surfaceBusyRef.current.delete(key);
        setSurfaceBusy(new Set(surfaceBusyRef.current));
      }
    }
  }, [overview?.revision.id, reviewId]);
  useEffect(() => {
    // Automatic runs go one at a time.
    if (!reviewId || !firstReviewableReady || surfaceBusy.size > 0 || surfaces.length === 0) return;
    // Not before the surface generation below has moved to this Snapshot: a
    // run started earlier in the same commit would be discarded and repeated.
    if (surfaceIdentityRef.current !== `${reviewId}:${overview?.revision.id ?? ""}`) return;
    const reviewPaths = new Set(targets.map((target) => target.path));
    const candidate = surfaces.find((surface) => {
      const runKey = automaticSurfaceRunKey(surface, reviewPaths);
      if (!runKey || automaticSurfaceRunsRef.current.has(runKey)) return false;
      const key = `${surface.provider}:${surface.id}`;
      return !surfaceRuns[key]?.new && !surfaceRuns[key]?.error;
    });
    if (!candidate) return;
    const runKey = automaticSurfaceRunKey(candidate, reviewPaths);
    if (!runKey) return;
    automaticSurfaceRunsRef.current.add(runKey);
    void executeSurface(candidate, false);
  }, [executeSurface, firstReviewableReady, overview?.revision.id, reviewId, surfaceBusy, surfaceRuns, surfaces, targets]);

  useEffect(() => {
    surfaceGenerationRef.current += 1;
    surfaceIdentityRef.current = `${reviewId}:${overview?.revision.id ?? ""}`;
    automaticSurfaceRunsRef.current.clear();
    setSurfaceRuns({});
    surfaceBusyRef.current = new Set();
    setSurfaceBusy(new Set());
    setRuntimeValidations({});
    runtimeValidationBusyRef.current = new Set();
    setRuntimeValidationBusy(new Set());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overview?.revision.id, reviewId]);

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


  const changeCodeTheme = useCallback((next: ReviewCodeTheme) => {
    const nextChrome = applyReviewCodeTheme(next);
    setTheme(nextChrome);
    setCodeTheme(next);
  }, []);

  const downloadPatch = useCallback(async () => {
    if (!reviewId || !overview) return;
    try {
      const blob = await fetchReviewPatch(
        reviewId,
        historical ? overview.revision.revision_number : undefined,
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      const reviewName = (overview.session.ref || reviewId)
        .replace(/[^A-Za-z0-9._-]+/g, "-")
        .replace(/^-+|-+$/g, "") || "lemoncrow-review";
      anchor.href = url;
      anchor.download = `${reviewName}-rev-${overview.revision.revision_number}.patch`;
      anchor.style.display = "none";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setError("");
    } catch (reason) {
      setError(`Patch download failed: ${String(reason instanceof Error ? reason.message : reason)}`);
    }
  }, [reviewId, overview, historical]);
  const createSnapshot = useCallback(async () => {
    if (!reviewId || !overview || !runningDraft || historical || forcedReadOnly || sessionLocked || revisionAdvanced || busy) return;
    setBusy("snapshot");
    try {
      await publishReviewSnapshot(reviewId, overview.revision.revision_number);
      revisionHistoryCacheRef.current = null;
      await loadReader(reviewId);
    } catch (reason) {
      reportWriteFailure(reason);
    } finally {
      setBusy("");
    }
  }, [reviewId, overview, runningDraft, historical, forcedReadOnly, sessionLocked, revisionAdvanced, busy, loadReader, reportWriteFailure]);

  const runPrimaryAction = useCallback(() => {
    switch (primaryActionDestination(primaryAction)) {
      case "snapshot":
        void createSnapshot();
        return;
      case "latest_snapshot":
        window.location.href = reviewHref(reviewId);
        return;
      case "finish_sheet":
        void openFinishSheet();
        return;
      case "reload":
        void reloadReader();
        return;
      case "comments":
        // "Re-review N" opens on the comments it counts. An addressed comment
        // whose lines the fix rewrote is orphaned, so the Open tab never lists it.
        setCommentsFilter(primaryAction.kind === "rereview_comments" ? "rereview" : undefined);
        setCommentsOpen(true);
        return;
      case "target": {
        const target = targets.find((item) => item.target_id === primaryAction.targetId);
        if (!target) return;
        setQuery("");
        setDeltaFocus(false);
        revealImplementation();
        void scrollToTarget(target, "instant");
        return;
      }
      case "readiness":
        setReadinessOpen(true);
        return;
      case "none":
        return;
    }
  }, [
    createSnapshot,
    openFinishSheet,
    primaryAction,
    refreshReview,
    reloadReader,
    revealImplementation,
    reviewId,
    scrollToTarget,
    targets,
  ]);

  // Phase 12 controlled actions: Fix/Fix all/explicit corrections (palette + More), finding provenance.
  const corrections = useReviewCorrections({
    reviewId: reviewId ?? "",
    revisionId: overview?.revision.id ?? "",
    enabled: Boolean(reviewId && overview && startupEnrichmentReady) && !historical,
    readOnly: Boolean(readOnly),
  });
  const correctionPalette = corrections.palette;
  const reloadAfterProviderSync = useCallback(() => {
    if (!reviewId) return;
    void fetchAnnotations(reviewId).then((result) => setAnnotations(result.annotations)).catch(() => undefined);
  }, [reviewId]);
  const feedbackSync = useFeedbackSync({
    reviewId: reviewId ?? "",
    enabled: Boolean(reviewId && overview && startupEnrichmentReady) && !historical,
    onChanged: reloadAfterProviderSync,
  });

  const reviewActions = useMemo<ReviewPaletteAction[]>(() => [
    {
      id: "search",
      label: "Search review",
      detail: "Find review areas by path, symbol, state, reason, or loaded comment text",
      shortcut: "/",
      run: () => {
        setFocusMode(false);
        setOutlineCollapsed(false);
        window.requestAnimationFrame(() => searchInputRef.current?.focus());
      },
    },
    {
      id: "reviewed",
      label: "Mark current review area reviewed and advance",
      shortcut: "r",
      disabled: !activeTarget || Boolean(readOnly) || Boolean(busy),
      run: () => { if (activeTarget) void markTarget(activeTarget, "reviewed", true); },
    },
    {
      id: "reopen",
      label: "Reopen current review area",
      shortcut: "u",
      disabled: !activeTarget || Boolean(readOnly) || Boolean(busy),
      run: () => { if (activeTarget) void markTarget(activeTarget, "unreviewed"); },
    },
    {
      id: "needs-changes",
      label: "Mark current review area needs changes",
      shortcut: "x",
      disabled: !activeTarget || Boolean(readOnly) || Boolean(busy),
      run: () => { if (activeTarget) void markTarget(activeTarget, "needs_changes"); },
    },
    {
      id: "comment",
      label: "Comment on current review area",
      shortcut: "c",
      disabled: !activeTarget || Boolean(readOnly) || Boolean(busy),
      run: () => { if (activeTarget) commentTarget(activeTarget); },
    },
    {
      id: "context",
      label: contextOpen ? "Close Context" : "Open Context",
      detail: "Impact, checks, evidence, author provenance, and discussion",
      shortcut: "e",
      disabled: !activeTarget,
      run: () => {
        if (contextOpen) closeContext();
        else if (activeTarget) openContext(activeTarget, contextTab);
      },
    },
    {
      id: "focus",
      label: focusMode ? "Exit focus mode" : "Enter focus mode",
      shortcut: "f",
      run: () => setFocusMode((current) => {
        const next = !current;
        setOutlineCollapsed(next);
        if (next) setContextOpen(false);
        return next;
      }),
    },
    {
      id: "diff-style",
      label: `Diff view: ${diffStyle === "auto" ? "Auto" : diffStyle === "split" ? "Split" : "Unified"}`,
      detail: diffStyle === "auto" ? "Responsive: unified when space is tight, split when the diff viewport is wide enough" : "Explicit preference; overrides responsive layout",
      shortcut: "s",
      run: () => changeDiffStyle(nextDiffStylePreference(diffStyle)),
    },
    {
      id: "line-wrap",
      label: diffOverflow === "wrap" ? "Keep code on one line" : "Wrap long code lines",
      detail: diffOverflow === "wrap" ? "Use horizontal scrolling for long lines" : "Fit long lines into the visible diff width",
      shortcut: "w",
      run: toggleDiffOverflow,
    },
    {
      id: "map",
      label: "Show Map",
      detail: "Bounded system impact with Snapshot-exact changes and clearly labeled current graph context",
      run: openMap,
    },
    {
      id: "overview",
      label: "Show Brief",
      detail: "Change overview: major changes, review-area counts, verification, provenance, and evidence",
      managesFocus: true,
      run: () => {
        openOverview();
        focusBriefAfterCommand();
      },
    },
    ...(briefCapable && projection && guidedReviewUseful(projection, targets) ? [{
      id: "guided-review",
      label: "Start guided review",
      detail: "Step through the change cohort by cohort; your place is kept",
      managesFocus: true,
      run: () => changeView("tour"),
    } satisfies ReviewPaletteAction] : []),
    ...(!historical && !runningDraft ? [{
      id: "status",
      label: "Open Review Status",
      detail: "Progress, blockers, checks and merge readiness in one place",
      managesFocus: true,
      run: () => setReadinessOpen(true),
    } satisfies ReviewPaletteAction] : []),
    {
      id: "comments",
      label: "Open comments",
      detail: runningDraft ? "Create a Snapshot first; comments anchor to frozen bytes" : "Every unresolved and resolved thread in this Review",
      managesFocus: true,
      run: openComments,
    },
    ...(askCapable ? [{
      id: "ask",
      label: askOpen ? "Close Ask" : "Ask about this change",
      run: () => askOpen ? closeAsk() : openAsk(null),
    } satisfies ReviewPaletteAction] : []),
    {
      id: "history",
      label: "Snapshot history",
      detail: "Earlier Snapshots and Snapshot comparison",
      managesFocus: true,
      run: () => setHistoryOpen(true),
    },
    {
      id: "development",
      label: "Open Activity",
      detail: "Contributors, intent, decisions, checks, and changed files frozen with this Snapshot, plus newer work",
      disabled: !development,
      managesFocus: true,
      run: () => setDevelopmentOpen(true),
    },
    ...(reviewEnvironment.review.workspace_refresh ? [{
      id: "refresh",
      label: "Update from current source",
      detail: "Capture the latest local source snapshot and reconcile prior judgments",
      disabled: Boolean(readOnly) || Boolean(busy),
      run: () => void refreshReview(),
    } satisfies ReviewPaletteAction] : []),
    ...(primaryAction.kind !== "review_target" ? [{
      id: "next-action",
      label: primaryAction.label,
      detail: primaryAction.detail,
      disabled: Boolean(busy) || primaryAction.disabled,
      run: runPrimaryAction,
    } satisfies ReviewPaletteAction] : []),
    ...(handoff.unsent > 0 && !handoffLocked && overview?.session.status === "open" ? [{
      id: "send-now",
      label: handoffRoute === "export"
        ? `Copy ${handoff.unsent} for the author now…`
        : `Send ${handoff.unsent} to ${handoffDestinationLabel(feedbackDelivery)} now…`,
      detail: "Preview the unsent comments and hand them off now; the verdict can come later",
      disabled: Boolean(busy),
      run: () => void openSendSheet(),
    } satisfies ReviewPaletteAction] : []),
    ...(overview && finishAvailableInMenu({
      sessionStatus: overview.session.status,
      historical,
      revisionAdvanced,
      action: primaryAction,
    })
      ? [{
          id: "finish",
          label: "Finish review…",
          detail: "Explicitly inspect blockers and record a verdict before finishing early",
          disabled: Boolean(busy),
          run: () => void openFinishSheet(),
        } satisfies ReviewPaletteAction]
      : []),
    ...correctionPalette,
    {
      id: "order-recommended",
      label: "Order: Recommended",
      disabled: Boolean(busy) || targetList?.order === "recommended",
      run: () => void changeOrder("recommended"),
    },
    {
      id: "order-file",
      label: "Order: File order",
      disabled: Boolean(busy) || targetList?.order === "file",
      run: () => void changeOrder("file"),
    },
    {
      id: "shortcuts",
      label: "Keyboard shortcuts",
      shortcut: "?",
      run: () => setCommandMode("shortcuts"),
    },
  ], [activeTarget, readOnly, revisionAdvanced, busy, contextOpen, contextTab, focusMode, diffStyle, diffOverflow, overview?.session.status, targetList?.order, historical, primaryAction, runPrimaryAction, handoff.unsent, handoffLocked, handoffRoute, feedbackDelivery, openSendSheet, markTarget, commentTarget, closeContext, openContext, openMap, openOverview, focusBriefAfterCommand, development, refreshReview, openFinishSheet, changeOrder, changeDiffStyle, toggleDiffOverflow, reviewEnvironment.review.workspace_refresh, correctionPalette, briefCapable, projection, targets, changeView, runningDraft, openComments, askCapable, askOpen, closeAsk, openAsk]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      // A surface that owns the key already acted on it: the line-selection
      // menu's c/r/x/s/e must not also comment on, mark, restyle or open
      // context for the active target. A tooltip that took an Escape only to
      // close itself does not own it: one Esc still closes Context.
      if (event.defaultPrevented && !(event.key === "Escape" && escapeDismissedTooltip(event))) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      // Reader shortcuts pause while a modal is open. The modal owns Escape (Radix listens
      // at the document level and keeps focus inside), so there is nothing to close here --
      // and a Reader Escape would drop the comment draft behind it. A modal a hosting
      // composition adds is only known by its open dialog. They also pause on the Map,
      // Map owns its keys. Brief keeps the existing Reader navigation shortcuts;
      // Tour owns step navigation itself and never acts on the hidden Implementation target.
      if (
        commandMode || view === "map" || commentsOpen || historyOpen || developmentOpen || finishSheetOpen
        || readinessOpen || githubPublishOpen || isDialogOpen()
      ) return;
      const onBrief = view === "brief";
      const onTour = view === "tour";
      if (onBrief && !BRIEF_KEYS.has(event.key)) return;
      if (onTour && !new Set(["p", "?", "/", "Escape"]).has(event.key)) return;
      if (event.key === "p" || event.key === "?") {
        openCommands(event.key === "p" ? "actions" : "shortcuts");
        event.preventDefault();
        return;
      }
      if (event.key === "Escape") {
        // Escape has to stay reachable when a zero-match search has emptied the
        // stream: there is no active target then, and the guard below would
        // strand the reviewer inside a filter they can no longer clear.
        setDraft(null);
        if (contextOpen) closeContext();
        else setRelated(null);
        setFeedback(null);
        if (query) setQuery("");
        event.preventDefault();
        return;
      }
      if (event.key === "/") {
        if (onBrief) changeView("implementation");
        setFocusMode(false);
        setOutlineCollapsed(false);
        window.requestAnimationFrame(() => searchInputRef.current?.focus());
        event.preventDefault();
        return;
      }
      if (!activeTarget) return;
      let next: ReviewTarget | null = null;
      switch (event.key) {
        case "j":
          next = stepTarget(visibleTargets, activeTarget.target_id, 1);
          break;
        case "k":
          next = stepTarget(visibleTargets, activeTarget.target_id, -1);
          break;
        case "J":
          next = stepFile(visibleTargets, activeTarget.target_id, 1);
          break;
        case "K":
          next = stepFile(visibleTargets, activeTarget.target_id, -1);
          break;
        case "]":
          // The Brief has no current item of its own: from it, Next review
          // item opens the queue's resume point (the item the Next card names)
          // rather than skipping past it.
          next = onBrief && isActionable(activeTarget)
            ? activeTarget
            : stepQueueTarget(visibleTargets, activeTarget.target_id, 1);
          break;
        case "[":
          next = stepQueueTarget(visibleTargets, activeTarget.target_id, -1);
          break;
        case "r":
          void markTarget(activeTarget, "reviewed", true);
          break;
        case "u":
          void markTarget(activeTarget, "unreviewed");
          break;
        case "x":
          void markTarget(activeTarget, "needs_changes");
          break;
        case "c":
          commentTarget(activeTarget);
          break;
        case "s":
          changeDiffStyle(nextDiffStylePreference(diffStyle));
          break;
        case "w":
          toggleDiffOverflow();
          break;
        case "e":
          if (contextOpen) closeContext();
          else openContext(activeTarget, contextTab);
          break;
        case "f":
          setFocusMode((current) => {
            const nextFocus = !current;
            setOutlineCollapsed(nextFocus);
            if (nextFocus) setContextOpen(false);
            return nextFocus;
          });
          break;
        default:
          return;
      }
      event.preventDefault();
      if (next && (next.target_id !== activeTarget.target_id || (onBrief && event.key === "]"))) {
        // Moving to code leaves the Brief first, as its own target links do.
        if (onBrief) changeView("implementation");
        void scrollToTarget(next);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeTarget, visibleTargets, markTarget, commentTarget, scrollToTarget, contextOpen, contextTab, openContext, closeContext, finishSheetOpen, closeFinishSheet, query, commandMode, closeCommands, view, changeView, commentsOpen, historyOpen, developmentOpen, readinessOpen, githubPublishOpen, openCommands, diffStyle, changeDiffStyle, toggleDiffOverflow]);

  const sharedPreparation = useReviewPreparation();
  const readerPending = !startupReady || !overview || !targetList || !firstReviewableReady || briefLandingPending;
  const readerSteps = useMemo<PreparationStep[]>(() => [
    { key: "reader_source", label: startupReady ? "Source snapshot ready" : preparationStage ? preparationLabel(preparationStage) : "Opening Review Reader", state: startupReady ? "done" : "running", elapsed: null },
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
      pending: authed && !reviewMissing && !(error && !overview) && readerPending,
      steps: readerSteps,
      onOpenCode: briefLandingPending && startupReady && overview && targetList && firstReviewableReady
        ? openPreparedCode : undefined,
    });
  }, [setSharedPreparation, authed, reviewMissing, error, overview, readerPending, readerSteps, briefLandingPending, startupReady, targetList, firstReviewableReady, openPreparedCode]);
  useLayoutEffect(() => () => setSharedPreparation?.(null), [setSharedPreparation]);

  if (!authed) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6 text-neutral-300">
        <div className="max-w-md">
          <div className="text-heading font-semibold text-foreground">This review link is incomplete</div>
          <p className="mt-1 text-caption leading-5 text-muted-foreground">For a local review, reopen it from the terminal with <code className="text-neutral-300">lc review --open</code>.</p>
        </div>
      </div>
    );
  }
  if (reviewMissing && !overview) {
    return (
      <div className="flex h-screen items-center justify-center bg-background px-6 text-neutral-300">
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
      <div className="flex h-screen items-center justify-center bg-background px-6 text-neutral-300">
        <div className="w-full max-w-md">
          <div className="text-heading font-semibold text-foreground">Review couldn't load</div>
          <div className="mt-1 text-caption leading-5 text-muted-foreground">The review is still available; the Reader could not fetch its current state.</div>
          <details className="mt-3 text-caption text-muted-foreground">
            <summary className="cursor-pointer hover:text-neutral-50">Error details</summary>
            <div className="mt-1 whitespace-pre-wrap font-mono text-rose-300/80">{error}</div>
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
  if (readerPending) {
    return sharedPreparation ? null : <PreparationProgress fullPage steps={readerSteps}
      onOpenCode={briefLandingPending && startupReady && overview && targetList && firstReviewableReady
        ? openPreparedCode : undefined} />;
  }

  const streamPaths = visiblePathOrder.filter((path) => pagePathSet.has(path));
  const hiddenStreamPaths = streamPaths.filter((path) => !revealedPaths.has(path));
  const remainingFileCount = comparisonActive ? 0 : hiddenStreamPaths.length;
  const pendingVisibleFileCount = comparisonActive
    ? 0
    : streamPaths.filter(
        (path) => revealedPaths.has(path) && !details[path] && !fileErrors[path],
      ).length;
  const readyHiddenPaths = hiddenStreamPaths.filter((path) => Boolean(details[path] || fileErrors[path]));
  const readyHiddenBatch = pathsForChangeBudget(
    readyHiddenPaths,
    fileChangeLines,
    LOAD_MORE_CHANGE_BUDGET,
    LOAD_MORE_MAX_FILES,
  );
  const nextPreparationPaths = pathsForChangeBudget(
    hiddenStreamPaths,
    fileChangeLines,
    LOAD_MORE_CHANGE_BUDGET,
    BACKGROUND_PREFETCH_FILES,
  );
  const preparingHiddenFileCount = comparisonActive ? 0 : nextPreparationPaths.filter(
    (path) => !details[path] && !fileErrors[path],
  ).length;
  const mountedPathSet = new Set(streamPaths.filter((path) => comparisonActive || revealedPaths.has(path)));
  if (draftPath) mountedPathSet.add(draftPath);
  const streamDetails = Object.fromEntries(
    Object.entries(comparisonActive ? comparisonDetails : details).filter(([path]) => mountedPathSet.has(path)),
  ) as Record<string, FileDetail | undefined>;
  const streamErrors = comparisonActive ? {} : Object.fromEntries(
    Object.entries(fileErrors).filter(([path]) => mountedPathSet.has(path)),
  ) as Record<string, string | undefined>;
  const addressedNeedsRereview = targets.reduce(
    (sum, target) => sum + target.annotation_counts.addressed_needs_rereview,
    0,
  );
  // A Snapshot comparison's left rail is a delta navigator, not another
  // conceptual story. Cohort identity comes only from the Review projection.
  const attentionPath = reviewAttentionPath(visibleTargets);
  const activeAttentionIndex = activeTarget
    ? attentionPath.findIndex((item) => item.targetId === activeTarget.target_id)
    : -1;
  const attentionPosition = attentionPath.length === 0 ? 0 : Math.max(0, activeAttentionIndex) + 1;
  const nextAttention = attentionPath[activeAttentionIndex >= 0 ? activeAttentionIndex : 0] ?? null;
  const activeProposals = proposals.filter(
    (proposal) => !proposal.result_revision_id
      && (proposal.state === "proposed" || proposal.state === "applied" || proposal.state === "conflicted"),
  );
  const staleProposalCount = activeProposals.filter(
    (proposal) => proposal.state === "proposed" && proposal.base_revision_id !== overview.revision.id,
  ).length;
  const readyProposalCount = activeProposals.filter(
    (proposal) => proposal.state === "proposed" && proposal.can_apply && proposal.base_revision_id === overview.revision.id,
  ).length;
  const savedProposalCount = activeProposals.filter(
    (proposal) => proposal.state === "proposed" && !proposal.can_apply && proposal.base_revision_id === overview.revision.id,
  ).length;
  const awaitingCaptureCount = activeProposals.filter((proposal) => proposal.state === "applied").length;
  const conflictedProposalCount = activeProposals.filter((proposal) => proposal.state === "conflicted").length;
  const proposalBarTone = conflictedProposalCount + staleProposalCount > 0
    ? "border-amber-900/45 bg-amber-950/10"
    : awaitingCaptureCount > 0
      ? "border-sky-900/45 bg-sky-950/10"
      : "border-violet-900/40 bg-violet-950/10";
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

  // ----- Reader chrome: one header, at most one context row --------------- //
  const attention = attentionState({
    revisionNumber: overview.revision.revision_number,
    progress,
    openCommentCount: openRootCommentCount,
    commentsState,
    feedbackStatus,
    feedbackDelivery,
    addressedNeedsRereview,
    historical,
    delivery,
    readiness,
  });
  const readinessHeader = historical || runningDraft
    ? null
    : {
        label: readiness.label,
        detail: readiness.detail,
        ...readinessControl(readiness, progress, true),
        shortText: readinessControl(readiness, progress).text,
      };
  const developmentSummary: ReaderDevelopmentSummary | null = development
    ? (() => {
        const contributors = development.snapshot.contributors.length;
        const newerEvents = development.since?.event_count ?? 0;
        const newerFiles = development.since?.newer_paths?.length ?? 0;
        const active = Boolean(development.since?.active);
        // Frozen Snapshot authorship/history belongs under Context → Activity.
        // The header only promotes Activity when it changes the reviewer's
        // next action: work is live, or something happened after this Snapshot.
        if (!active && newerEvents === 0 && newerFiles === 0) return null;
        const parts = [
          `Activity: ${contributors} contributor${contributors === 1 ? "" : "s"} in Snapshot ${development.revision_number}`,
        ];
        if (active) parts.push("agents working now");
        else if (newerEvents > 0) parts.push(`${newerEvents} newer event${newerEvents === 1 ? "" : "s"}`);
        if (newerFiles > 0) parts.push(`${newerFiles} newer file${newerFiles === 1 ? "" : "s"}`);
        return { contributors, newerEvents, newerFiles, active, label: parts.join(" · ") };
      })()
    : null;
  const contextSignals: ContextSignal[] = [];
  if (accessLost) {
    const label = accessLost === "gone" ? "This review is no longer available" : "Access to this review was removed";
    contextSignals.push({
      id: "access-lost",
      tone: "rose",
      icon: Lock,
      role: "alert",
      label,
      chipLabel: label,
      detail: ACCESS_LOST_NOTICE[accessLost],
      actions: (
        <a href="/reviews" className="review-context-action border-rose-800/60 text-rose-200 hover:text-rose-50">
          All reviews
        </a>
      ),
    });
  }
  if (revisionAdvanced) {
    contextSignals.push({
      id: "revision-advanced",
      tone: "rose",
      icon: RefreshCw,
      role: "alert",
      label: "Snapshot superseded elsewhere",
      chipLabel: "Snapshot superseded elsewhere",
      detail: "A newer Snapshot exists (the working tree changed while you reviewed), so this one no longer accepts changes. Use Reload review to work on the new Snapshot, or keep reading this one read-only.",
      actions: (
        <>
          <a
            href={historicalReviewHref(overview.session.ref || overview.session.id, overview.revision.revision_number)}
            className="review-context-action border-rose-800/60 text-rose-200 hover:text-rose-50"
          >
            Keep reading this Snapshot
          </a>
        </>
      ),
    });
  }
  if (runningDraft) {
    contextSignals.push({
      id: "running-draft",
      tone: "neutral",
      icon: Clock3,
      label: "Running draft",
      chipLabel: "Running draft · read-only",
      detail: forcedReadOnly
        ? (extensions?.readOnlyReason || "This Review is read-only. Snapshot creation is unavailable.")
        : "Create a Snapshot to freeze the current bytes. That enables comments and review decisions, and unlocks Snapshot-exact Brief, Map, and architecture analysis.",
      actions: !forcedReadOnly && !historical && !sessionLocked && !revisionAdvanced ? (
        <UiButton size="compact" disabled={Boolean(busy)} onClick={() => void createSnapshot()}>Create Snapshot</UiButton>
      ) : undefined,
    });
  }
  if (historical && !runningDraft) {
    const latestNumber = overview.latest_revision_number ?? overview.revision.revision_number;
    contextSignals.push({
      id: "historical",
      tone: "amber",
      icon: Clock3,
      testId: "historical-revision-banner",
      label: <span className="uppercase tracking-[0.12em] text-amber-300">Historical</span>,
      chipLabel: `Historical · Snapshot ${overview.revision.revision_number}`,
      detail: (
        <>
          <span className="text-amber-100">Snapshot {overview.revision.revision_number}</span>
          <span aria-hidden="true">·</span>
          <span>latest Snapshot {latestNumber}</span>
          <span aria-hidden="true">·</span>
          <span>read-only</span>
        </>
      ),
      actions: (
        <>
          {overview.latest_revision && overview.latest_revision.id !== overview.revision.id && (
            <a
              href={reviewCompareHref(overview.session.ref || overview.session.id, overview.revision.revision_number, latestNumber)}
              className="review-context-action border-amber-800/60 text-amber-200 hover:text-amber-50"
            >
              Compare with latest
            </a>
          )}
          <UiButton unstyled
            type="button"
            onClick={() => { window.location.href = reviewHref(reviewId); }}
            className="review-context-action-primary border-amber-600/70 bg-amber-800/45 text-amber-50 hover:bg-amber-700/55"
          >
            Back to latest · Snapshot {latestNumber}
          </UiButton>
        </>
      ),
    });
  }
  if (fixOf) {
    contextSignals.push({
      id: "fix-of",
      tone: "sky",
      icon: FileDiff,
      label: `Fix of Snapshot ${fixOf.snapshot_number}`,
      chipLabel: `Fix of Snapshot ${fixOf.snapshot_number}`,
      detail: fixOf.worktree_missing
        ? "Fix worktree missing"
        : fixOf.status === "applied"
          ? "Applied to main"
          : fixOf.status === "applied_with_conflicts"
            ? "Applied with conflicts"
            : fixOf.status === "abandoned"
              ? "Abandoned"
              : `Fixing · ${fixOf.snapshots.length} snapshot${fixOf.snapshots.length === 1 ? "" : "s"}`,
      actions: (
        <a
          href={reviewHref(fixOf.review_id)}
          className="review-context-action border-sky-800/60 text-sky-200 hover:text-sky-50"
        >
          Back to the Review
        </a>
      ),
    });
  }
  if (hostedInfo) {
    const pending = hostedQueue.status?.counts?.pending ?? 0;
    const conflicts = hostedQueue.status?.counts?.conflict ?? 0;
    const queueLabel = [
      pending > 0 ? `${pending} queued` : "",
      conflicts > 0 ? `${conflicts} conflict${conflicts === 1 ? "" : "s"}` : "",
    ].filter(Boolean).join(" · ");
    const hostedPanel = (trigger: ReactNode) => (
      <Popover key="hosted">
        <PopoverTrigger asChild>{trigger}</PopoverTrigger>
        <PopoverContent align="end" aria-label="Hosted queue" className="w-[460px] max-w-[92vw] rounded-md border-neutral-700 p-2.5">
          <HostedQueuePanel
            reviewId={reviewId}
            hosted={hostedInfo}
            status={hostedQueue.status}
            error={hostedQueue.error}
            onStatus={(next) => {
              hostedQueue.setStatus(next);
              setHostedNotice("");
            }}
            onDetached={(localReviewId) => { window.location.href = reviewHref(localReviewId); }}
          />
        </PopoverContent>
      </Popover>
    );
    contextSignals.push({
      id: "hosted",
      tone: conflicts > 0 || hostedQueue.error ? "amber" : "sky",
      icon: Cloud,
      testId: "hosted-attachment-signal",
      label: `Attached to hosted ${hostedInfo.reference}`,
      chipLabel: `Hosted ${hostedInfo.reference}${queueLabel ? ` · ${queueLabel}` : ""}`,
      detail: hostedNotice || hostedQueue.error || (queueLabel
        ? `${queueLabel} for hosted · applies on \`lc review hosted sync\``
        : "Hosted owns comments, judgments and outcomes; your changes here queue for it"),
      actions: hostedPanel(
        <UiButton size="compact" type="button" data-testid="hosted-queue-open">
          {queueLabel ? `Queue · ${queueLabel}` : "Queue"}
        </UiButton>,
      ),
      chip: hostedPanel(
        <UiButton unstyled type="button" className={`review-context-chip ${conflicts > 0 ? "border-amber-800/60 text-amber-200 hover:bg-amber-950/30" : "border-sky-800/60 text-sky-200 hover:bg-sky-950/30"}`}>
          <Cloud size={11} aria-hidden="true" />
          <span className="truncate">{`Hosted ${hostedInfo.reference}${queueLabel ? ` · ${queueLabel}` : ""}`}</span>
        </UiButton>,
      ),
    });
  }
  const snapshotControls = !readOnly && sourceState ? (
    <ReviewSnapshotControl
      files={sourceState.path_count}
      detail={activeHandoffWorking
        ? `${sourceState.path_count} files changed while ${activeFeedbackHandoff?.destination} is working. LemonCrow will capture a read-only draft after every submitted comment is marked addressed.`
        : activeHandoffAddressed && activeHandoffCurrentRevision
          ? `${sourceState.path_count} files changed. Agent fix ready; capturing the settled fix as a read-only draft automatically.`
          : awaitingCaptureCount > 0
            ? `${awaitingCaptureCount} reviewer edit${awaitingCaptureCount === 1 ? "" : "s"} applied · ${sourceState.path_count} files changed. Keep reviewing Snapshot ${overview.revision.revision_number}, or capture the newer source when ready.`
            : `${sourceState.path_count} file${sourceState.path_count === 1 ? "" : "s"} changed after this frozen Snapshot. You can keep reviewing Snapshot ${overview.revision.revision_number}.`}
      compareHref={sourceCompareHref(
        overview.revision.ref || `rr/${overview.revision.id}`,
        "worktree",
        overview.session.ref || `r/${overview.session.id}`,
      )}
      busy={Boolean(busy)}
      onCapture={() => void refreshReview()}
      onHistory={() => setHistoryOpen(true)}
      onHide={awaitingCaptureCount === 0 ? () => {
        setDismissedSourceFingerprint(sourceState.fingerprint);
        setSourceState(null);
      } : undefined}
    />
  ) : null;
  // Routine progress and readiness blockers already live on the header's
  // readiness control; the strip only carries conditions that change the
  // reviewer's next move (failed delivery, requested changes, ...).
  if (!historical && !runningDraft && !ROUTINE_ATTENTION_LABELS.has(attention.label)) {
    contextSignals.push({
      id: "attention",
      tone: attention.tone === "emerald" ? "neutral" : attention.tone,
      icon: attention.icon,
      label: attention.label,
      chipLabel: attention.label,
      detail: attention.detail,
      onActivate: () => setReadinessOpen(true),
    });
  }
  const capabilityDiscoveryDegraded = productCapabilitiesResolution === "degraded";
  const contextFailureMessages = Object.values(contextFailures).filter(Boolean) as string[];
  if (contextFailureMessages.length > 0) {
    contextSignals.push({
      id: "context-failures",
      tone: "amber",
      icon: TriangleAlert,
      label: "Some review context is unavailable",
      chipLabel: "Some context unavailable",
      detail: <span title={contextFailureMessages.join("\n")}>{contextFailureMessages.join(" · ")}</span>,
      chip: (
        <Popover key="context-failures">
          <PopoverTrigger asChild>
            <UiButton unstyled type="button" className="review-context-chip border-amber-800/60 text-amber-200 hover:bg-amber-950/30">
              <TriangleAlert size={11} aria-hidden="true" />
              <span className="truncate">Some context unavailable</span>
            </UiButton>
          </PopoverTrigger>
          <PopoverContent align="end" aria-label="Unavailable review context" className="w-[420px] max-w-[88vw] space-y-1 rounded-md border-neutral-700 p-2.5 text-label leading-5 text-neutral-400">
            {contextFailureMessages.map((message) => <div key={message}>{message}</div>)}
          </PopoverContent>
        </Popover>
      ),
    });
  }
  if (bulkUndo) {
    contextSignals.push({
      id: "bulk-undo",
      tone: "neutral",
      icon: RotateCcw,
      label: `${bulkUndo.marks.length} targets marked reviewed`,
      chipLabel: `${bulkUndo.marks.length} reviewed`,
      detail: bulkUndo.path,
      actions: (
        <UiButton
          unstyled
          type="button"
          disabled={Boolean(busy)}
          onClick={() => void undoBulkReview()}
          className="px-1 text-label text-sky-300 hover:text-sky-200"
        >
          Undo
        </UiButton>
      ),
    });
  }
  if (draft?.recovered) {
    contextSignals.push({
      id: "draft",
      tone: "amber",
      icon: PencilLine,
      label: "Draft preserved",
      chipLabel: "Draft preserved",
      detail: `${draft.body?.trim() || "Unsaved comment"} · select a new line to reattach`,
      actions: (
        <UiButton unstyled type="button" onClick={() => setDraft(null)} className="px-1 text-label text-amber-400/80 hover:text-amber-200">Discard draft</UiButton>
      ),
    });
  }
  if (preparationStage && startupReady) {
    const label = `${preparationLabel(preparationStage)} · source is ready to review`;
    contextSignals.push({ id: "preparation", tone: "sky", icon: LoaderCircle, label, chipLabel: preparationLabel(preparationStage) });
  }
  if (capabilityDiscoveryDegraded && !capabilityNoticeDismissed && contextSignals.length === 0) {
    contextSignals.push({
      id: "capabilities-fallback",
      tone: "neutral",
      icon: Puzzle,
      label: "Using safe Review defaults",
      chipLabel: "Review capabilities unconfirmed",
      detail: "The server did not publish capability details, so LemonCrow is using safe defaults instead of hiding Review controls.",
      actions: (
        <UiButton
          unstyled
          type="button"
          onClick={() => setCapabilityNoticeDismissed(true)}
          className="px-1 text-label text-neutral-400 hover:text-neutral-200"
        >
          Dismiss
        </UiButton>
      ),
    });
  }

  // A feature the server declares off is hidden and named, so a missing
  // control reads as "this server does not support X", never as a bug.
  if (reviewEnvironment.unsupported.length > 0 && !unsupportedNoticeDismissed) {
    const features = reviewEnvironment.unsupported.join(", ");
    contextSignals.push({
      id: "capabilities-unsupported",
      tone: "neutral",
      icon: Puzzle,
      label: `This server does not support ${features}`,
      chipLabel: `Server does not support ${features}`,
      detail: "The server's Review capability manifest turns this off, so the control is hidden. Upgrade or reconfigure the server to use it.",
      actions: (
        <UiButton
          unstyled
          type="button"
          onClick={() => setUnsupportedNoticeDismissed(true)}
          className="px-1 text-label text-neutral-400 hover:text-neutral-200"
        >
          Dismiss
        </UiButton>
      ),
    });
  }

  // Secondary context always occupies one compact strip. This order is product
  // authority, not insertion accident: state that invalidates the current
  // review comes first, then read-only/workflow context, then recoverable
  // degradation and ephemeral acknowledgements.
  const orderedContextSignals = [...contextSignals].sort(
    (left, right) => reviewContextSignalPriority(left.id) - reviewContextSignalPriority(right.id),
  );

  // Blocking notice arbitration: never stack comparison, refresh delta, and
  // request errors into multiple full-width rows. Everything else is secondary
  // context and collapses into ReviewContextStrip below.
  const blockingNoticeKind = reviewBlockingNoticeKind({
    hasError: Boolean(error && overview),
    comparisonActive: Boolean(comparisonQueryValid && (requestedHeadBase || requestedBaseRevision !== requestedTargetRevision)),
    revisionDeltaActive: Boolean(!comparisonActive && (revisionInfo?.target_delta || overview.discarded.length > 0)),
  });
  const blockingNotice = blockingNoticeKind === "error" ? (
    <div role="alert" className="flex shrink-0 items-start gap-2 border-b border-rose-900/50 bg-rose-950/20 px-3 py-1.5 text-caption text-rose-200" data-testid="review-blocking-notice" data-notice="error">
      <span className="min-w-0 flex-1 break-words">{error}</span>
      <UiButton unstyled type="button" onClick={() => setError("")} className="shrink-0 text-rose-300/80 hover:text-rose-100">Dismiss</UiButton>
    </div>
  ) : blockingNoticeKind === "comparison" ? (
    <div data-testid="review-blocking-notice" data-notice="comparison">
      <RevisionCompareBar
        baseRevision={compareBaseRevision}
        targetRevision={compareTargetRevision}
        loading={revisionComparisonLoading}
        error={revisionComparisonError}
        onExit={exitRevisionComparison}
      />
    </div>
  ) : blockingNoticeKind === "revision-delta" ? (
    <div data-testid="review-blocking-notice" data-notice="revision-delta">
      <RevisionDeltaBar
        info={revisionInfo}
        discarded={overview.discarded}
        focused={deltaFocus}
        remainingActive={deltaRemainingCount}
        onShowAll={() => setDeltaFocus(false)}
        onSelectTarget={(targetId) => {
          const target = targets.find((item) => item.target_id === targetId);
          if (!target) return;
          revealImplementation();
          void scrollToTarget(target);
        }}
        onDismiss={() => {
          setRevisionInfo(null);
          setDeltaFocus(false);
        }}
      />
    </div>
  ) : null;

  return (
    <ReviewCorrectionsProvider value={corrections.context}>
    <FeedbackSyncProvider value={feedbackSync}>
    <div
      className="review-app relative flex h-dvh min-h-0 flex-col bg-background text-neutral-200"
      data-testid="review-reader"
      data-review-targets-complete={targetList?.complete === false ? "false" : "true"}
    >
      <ReviewFrictionProbe
        view={view}
        onTarget={view === "implementation" && Boolean(activeTarget)}
        targetCount={progress.target_count}
        rail={askOpen && askCapable ? "ask" : contextOpen ? "context" : null}
        notices={(blockingNoticeKind ? 1 : 0) + orderedContextSignals.length + (!historical && activeProposals.length > 0 ? 1 : 0)}
        revisionNumber={overview.revision.revision_number}
        reopened={progress.changed_since_review}
      />
      <ReaderHeader
        correctionActions={correctionPalette}
        overview={overview}
        progress={progress}
        diffStyle={diffStyle}
        diffOverflow={diffOverflow}
        focusMode={focusMode}
        contextOpen={contextOpen}
        busy={Boolean(busy)}
        busyLabel={primaryBusyLabel(busy, overview.session.status)}
        readOnly={Boolean(readOnly)}
        revisionAdvanced={revisionAdvanced}
        finishButtonRef={finishButtonRef}
        codeTheme={codeTheme}
        commentCount={rootCommentCount}
        openCommentCount={openRootCommentCount}
        commentState={commentsState}
        askOpen={askOpen}
        view={view}
        availableViews={availableViews}
        primaryAction={primaryAction}
        hidePrimaryAction={Boolean(extensions?.hidePrimaryAction)}
        authorshipOverride={extensions?.authorship}
        contextOverride={extensions?.context}
        environment={reviewEnvironment}
        allowRefresh={reviewEnvironment.review.workspace_refresh}
        snapshotControls={snapshotControls}
        extensionActions={extensions?.renderHeaderActions?.({ reviewId, overview, annotations, readOnly: Boolean(readOnly), historical })}
        handoffControls={reviewId && overview.session.status === "open" && !handoffLocked && commentsState === "ready" ? (
          <ReviewHandoffControls
            reviewId={reviewId}
            delivery={feedbackDelivery}
            lastDelivery={delivery}
            unsent={handoff.unsent}
            waiting={handoff.published + handoff.inFlight > 0}
            busy={Boolean(busy)}
            buttonRef={finishButtonRef}
            busyLabel={primaryBusyLabel(busy, overview.session.status)}
            onSendNow={() => void openSendSheet()}
            onShowHandoff={() => void openSendSheet()}
            onDestinationChanged={() => void refreshFeedbackState("Changed")}
          />
        ) : undefined}
        historical={historical}
        onCodeThemeChange={changeCodeTheme}
        onShowDirectory={() => { window.location.href = reviewDirectoryHref(); }}
        backToReviewHref={dedicatedItem ? fullReviewUrlFromDedicatedItem() : undefined}
        returnTo={returnTo ? { href: returnTo, label: returnTo.startsWith("/code") ? "Back to Code" : "Back" } : undefined}
        backtrail={backtrail.length > 0 ? { label: backtrail[backtrail.length - 1].label, onBack: goBack } : null}
        onShowComments={openComments}
        onShowAsk={askCapable ? () => askOpen ? closeAsk() : openAsk(null) : undefined}
        onViewChange={changeView}
        onShowHistory={() => setHistoryOpen(true)}
        onPrimaryAction={runPrimaryAction}
        onDownloadPatch={() => void downloadPatch()}
        onFinish={() => void openFinishSheet()}
        onPublishGitHub={isGitHubPullRequest && !historical ? () => setGithubPublishOpen(true) : undefined}
        onRefresh={() => void refreshReview()}
        onDiffStyleChange={changeDiffStyle}
        onToggleDiffOverflow={toggleDiffOverflow}
        onToggleContext={() => contextOpen ? closeContext() : activeTarget && openContext(activeTarget, contextTab)}
        onToggleFocus={() => setFocusMode((current) => {
          const next = !current;
          setOutlineCollapsed(next);
          if (next) {
            closeContext();
            closeAsk();
          }
          return next;
        })}
        onShowShortcuts={() => openCommands("shortcuts")}
        onShowOverview={openOverview}
        development={developmentSummary}
        developmentOpen={developmentOpen}
        onShowDevelopment={() => setDevelopmentOpen((value) => !value)}
        readiness={readinessHeader}
        landing={landingReadinessState === "ready" || landingReadinessState === "loading" ? landingReadiness : null}
        onShowReadiness={() => setReadinessOpen(true)}
        uncertainSignals={overview.degraded.length}
        compactDock={compactLayout ? {
          target: compactDock,
          navigation: view === "implementation" ? (
            <UiButton
              unstyled
              type="button"
              className="review-mobile-change-nav"
              onClick={() => {
                if (outlineCollapsed) {
                  // One full-height panel at a time on a phone.
                  setContextOpen(false);
                  setAskOpen(false);
                }
                setOutlineCollapsed((current) => !current);
              }}
              aria-expanded={!outlineCollapsed}
              aria-label={`${outlineCollapsed ? "Open" : "Close"} change navigation · ${Math.max(0, progress.target_count - progress.reviewed)} left`}
            >
              <ListTree size={13} aria-hidden="true" />
              <span>Changes · {Math.max(0, progress.target_count - progress.reviewed)}</span>
            </UiButton>
          ) : undefined,
        } : undefined}
      />
      {blockingNotice}
      <ReviewContextStrip signals={orderedContextSignals} />
      <ReviewFrontierNotes
        reviewId={reviewId}
        revisionId={overview.revision.id}
        rangeMode={overview.session.range_mode}
        historical={historical}
      />
      {readinessOpen && (
        <ReviewReadinessSheet
          model={localReadiness}
          landing={landingReadinessState === "ready" ? landingReadiness : null}
          landingBusy={landingBusy}
          landingError={landingError}
          readOnly={historical || revisionAdvanced || overview?.session.status === "archived"}
          extraSections={extensions?.reviewerSuggestions === "external" || !overview ? extensions?.readinessSections : (
            <>
              {extensions?.readinessSections}
              <ReviewerSuggestionsPanel reviewId={overview.session.id} revisionId={overview.revision.id} />
            </>
          )}
          verificationExtras={projection?.behavior?.checklist ? (
            <ManualChecklist
              checklist={projection.behavior.checklist}
              onRecord={historical || readOnly || revisionAdvanced || projection.historical ? undefined : recordManualCheck}
            />
          ) : undefined}
          automation={reviewId ? (
            <ReviewAutomationSection
              reviewId={reviewId}
              readOnly={Boolean(historical || readOnly || overview?.session.status === "archived")}
            />
          ) : undefined}
          deployments={reviewId ? (
            <>
              <ReviewDeploymentSection reviewId={reviewId} />
              <ReviewProductionSection reviewId={reviewId} variant="status" />
            </>
          ) : undefined}
          premergeActions={historical || readOnly || revisionAdvanced ? undefined : {
            busy: premergeBusy,
            error: premergeError,
            onWaive: (input) => void runPremergeAction("Could not waive the check", (id, snapshotId) =>
              waiveReviewPremergeCheck(id, { expected_revision_id: snapshotId, ...input })),
            onRebind: (ref) => void runPremergeAction("Could not re-bind the intent", (id, snapshotId) =>
              rebindReviewIntent(id, { expected_revision_id: snapshotId, ref })),
            onEvaluate: () => void runPremergeAction("Could not evaluate the checks", (id, snapshotId) =>
              evaluateReviewPremergeChecks(id, { expected_revision_id: snapshotId })),
          }}
          recordSection={reviewId && overview && !historical ? (
            <ReviewRecordExport
              reviewId={reviewId}
              snapshotId={overview.revision.id}
              readOnly={readOnly || revisionAdvanced || overview.session.status === "archived"}
            />
          ) : undefined}
          onArmLanding={() => void armLanding()}
          onCancelLanding={() => void cancelLanding()}
          onClose={() => setReadinessOpen(false)}
        />
      )}
      {commandMode && (
        <Suspense fallback={null}>
          <ReviewCommandPalette
            mode={commandMode}
            actions={reviewActions}
            onClose={closeCommands}
          />
        </Suspense>
      )}
      {commentsOpen && (
        <Suspense fallback={null}>
          <ReviewCommentsSheet
            annotations={surfacedAnnotations}
            feedbackStatus={feedbackStatus}
            feedbackDelivery={feedbackDelivery}
            initialFilter={commentsFilter}
            onClose={() => setCommentsOpen(false)}
            onSetAnnotationState={readOnly ? undefined : (id, state) => void setAnnotationState(id, state)}
            onJump={(annotation) => {
              const target = targetForAnnotation(targets, annotation);
              setCommentsOpen(false);
              if (target || annotation.path) revealImplementation();
              if (target) {
                void scrollToTarget(target);
              } else if (annotation.path) {
                void scrollToFile(annotation.path);
              }
            }}
          />
        </Suspense>
      )}
      {historyOpen && (
        <Suspense fallback={null}>
          <ReviewHistorySheet
            reviewId={reviewId}
            selectedRevisionNumber={overview.revision.revision_number}
            historical={historical}
            rangeMode={overview.session.range_mode}
            reviewBaseSha={overview.session.review_base_sha}
            initialTab="revisions"
            canFix={Boolean(codeRoot) && overview.session.status === "open"}
            onClose={() => setHistoryOpen(false)}
          />
        </Suspense>
      )}
      {developmentOpen && development && (
        <Suspense fallback={null}>
          <ReviewDevelopmentSheet
            reviewId={reviewId}
            development={development}
            historical={historical}
            onClose={() => setDevelopmentOpen(false)}
            onOpenPath={selectPath}
          />
        </Suspense>
      )}
      {extensions?.renderOverlays?.({ reviewId, overview, annotations, readOnly: Boolean(readOnly), historical })}

      {!historical && activeProposals.length > 0 && (
        <div className={`shrink-0 border-b px-3 py-1.5 text-caption ${proposalBarTone}`} data-testid="review-proposals">
          <div className="flex items-center gap-2">
            <span className="font-medium text-neutral-200">Source edits</span>
            {readyProposalCount > 0 && <span className="text-violet-300/75">{readyProposalCount} ready</span>}
            {savedProposalCount > 0 && <span className="text-muted-foreground">{savedProposalCount} saved</span>}
            {awaitingCaptureCount > 0 && <span className="text-sky-300/75">{awaitingCaptureCount} awaiting update</span>}
            {staleProposalCount > 0 && <span className="text-amber-300/80">{staleProposalCount} from older Snapshot</span>}
            {conflictedProposalCount > 0 && <span className="text-amber-300/80">{conflictedProposalCount} need attention</span>}
            <span className="flex-1" />
            <Popover>
              <PopoverTrigger asChild>
                <UiButton variant="link" className="text-caption text-muted-foreground hover:text-neutral-200">Details</UiButton>
              </PopoverTrigger>
              <PopoverContent align="end" aria-label="Source edit details" className="w-[480px] max-w-[88vw] rounded-md border-neutral-700 p-1.5 text-caption">
                {activeProposals.map((proposal) => {
                  const status = proposal.state === "conflicted"
                    ? { label: "Source moved", tone: "text-amber-300" }
                    : proposal.state === "applied"
                      ? { label: "Awaiting review update", tone: "text-sky-300" }
                      : proposal.base_revision_id !== overview.revision.id
                        ? { label: "Review advanced", tone: "text-amber-300" }
                        : proposal.can_apply
                          ? { label: "Ready to apply", tone: "text-violet-300" }
                          : { label: "Saved proposal", tone: "text-muted-foreground" };
                  return (
                    <div key={proposal.id} className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-x-2 border-b border-border px-2 py-2 last:border-b-0">
                      <div className="min-w-0">
                        <div className="truncate font-mono text-neutral-300" title={proposal.path}>
                          {proposal.path}:{proposal.start_line}{proposal.end_line !== proposal.start_line ? `-${proposal.end_line}` : ""}
                        </div>
                        {proposal.intent && <div className="mt-0.5 truncate text-muted-foreground" title={proposal.intent}>{proposal.intent}</div>}
                      </div>
                      <span className={status.tone}>{status.label}</span>
                      {(proposal.state === "proposed" || proposal.state === "conflicted") ? (
                        <UiButton
                          variant="ghost"
                          size="compact"
                          type="button"
                          className="h-6 text-caption"
                          onClick={() => {
                            setProposalSelection({
                              revision_id: proposal.base_revision_id,
                              path: proposal.path,
                              start_line: proposal.start_line,
                              end_line: proposal.end_line,
                              side: "additions",
                              text: proposal.original_text,
                            });
                            setActiveProposal(proposal);
                            setDraft({
                              path: proposal.path,
                              startLine: proposal.start_line,
                              endLine: proposal.end_line,
                              side: "additions",
                              targetUnitKey: proposal.target_unit_key || undefined,
                              mode: "proposal",
                              sourceAction: "suggest",
                              replacementText: proposal.replacement_text,
                              proposalIntent: proposal.intent,
                              proposalId: proposal.id,
                            });
                          }}
                        >
                          Open
                        </UiButton>
                      ) : (
                        <span className="w-8" aria-hidden="true" />
                      )}
                    </div>
                  );
                })}
              </PopoverContent>
            </Popover>
          </div>
        </div>
      )}
      {sendSheetOpen && feedback && reviewId && (
        <ReviewSendSheet
          reviewId={reviewId}
          feedback={feedback}
          delivery={delivery}
          handoffRereview={feedbackHandoffRereview}
          busy={Boolean(busy)}
          canSend={!handoffLocked}
          onCopy={() => copyToClipboard(feedback.markdown)}
          onSend={(options) => sendFeedbackToAgent(options)}
          onPublishGitHub={() => { setSendSheetOpen(false); setGithubPublishOpen(true); }}
          onDestinationChanged={() => void prepareFeedback()}
          onReviewOutcome={() => { setSendSheetOpen(false); void openFinishSheet(); }}
          onClose={() => { setSendSheetOpen(false); setFeedback(null); setDelivery(null); }}
        />
      )}
      {corrections.sheet}

      {/* Projected surfaces cover Implementation, which stays mounted but invisible
          (out of the accessibility tree and tab order) with its exact position intact. */}
      <div className={"relative flex min-h-0 flex-1 " + (view === "implementation" ? "" : "invisible")}>
        {view !== "implementation" && (
          <div className="visible absolute inset-0 z-20 flex min-h-0 bg-background" data-testid={"review-" + view + "-view"}>
            <Suspense fallback={<div className="p-4 text-label text-muted-foreground">{"Loading " + (view === "map" ? "Map" : view === "tour" ? "Guided review" : "Brief") + "…"}</div>}>
              {view === "map" ? (
                <ReviewMapSheet
                  reviewId={reviewId}
                  revisionNumber={overview.revision.revision_number}
                  historical={historical}
                  location={mapLocation}
                  onLocationChange={setMapLocation}
                  selectionHref={mapSelectionHref}
                  onClose={closeMap}
                  onOpenSelection={(selection) => void openMapSelection(selection)}
                  onAskSelection={askCapable ? askMapSelection : undefined}
                  askPanel={askCapable && askOpen ? drawer : null}
                  onCloseAsk={askCapable ? closeAsk : undefined}
                  onOpenInCode={runtimeCodeRoot ? openInCode : undefined}
                  chapters={mapChapters}
                  onSelectionChange={(selection) => { mapSelectionRef.current = selection; }}
                  loadProjectHistory={projectHistoryAvailable ? loadProjectHistory : undefined}
                />
              ) : view === "tour" && briefCapable && projection?.tour ? (
                <div className="flex min-h-0 flex-1">
                  <div className="min-h-0 flex-1 overflow-y-auto" data-review-scroll-root="tour">
                    <ReviewTour
                      reviewId={reviewId}
                      revisionId={projection.revision_id}
                      tour={projection.tour}
                      targets={targets}
                      activeCohortId={guidedCohortId}
                      onCohortChange={changeGuidedCohort}
                      onExit={() => changeView("implementation")}
                      onBackToBrief={() => changeView("brief")}
                      onOpenTarget={openBriefTarget}
                      onInspectEvidence={(targetId) => {
                        const target = targetId ? targets.find((item) => item.target_id === targetId) : undefined;
                        if (!target) {
                          setReadinessOpen(true);
                          return;
                        }
                        changeView("implementation");
                        openContext(target, "checks");
                      }}
                      onAsk={askCapable ? (targetId) => {
                        const target = targetId ? targets.find((item) => item.target_id === targetId) : undefined;
                        if (target) askTarget(target);
                        else openAsk(null);
                      } : undefined}
                      onSynthesize={historical || readOnly ? undefined : synthesizeOnTour}
                    />
                  </div>
                  {askOpen && drawer && (
                    <div data-review-rail="ask" className="review-secondary-rail w-[min(32vw,400px)] shrink-0 border-l border-border bg-background">{drawer}</div>
                  )}
                </div>
              ) : briefCapable && projection ? (
                <div className="flex min-h-0 flex-1">
                  <div className="min-h-0 flex-1 overflow-y-auto" data-review-scroll-root="brief">
                    <ReviewBrief
                      projection={projection}
                      loading={projectionState === "loading"}
                      error={projectionState === "failed" ? "The brief could not refresh; showing the last loaded plan." : ""}
                      targets={targets}
                      requestScope={requestScopeApplies && requestScopeIds ? {
                        inScopeTargetIds: requestScopeIds,
                        showOutside: !requestScopeFocus,
                        onToggleOutside: () => setRequestScopeFocus((current) => !current),
                      } : undefined}
                      onOpenTarget={openBriefTarget}
                      onOpenFiles={closeOverview}
                      onOpenTour={guidedReviewUseful(projection, targets) ? () => changeView("tour") : undefined}
                      onInspectEvidence={(targetId) => {
                        const target = targetId ? targets.find((item) => item.target_id === targetId) : undefined;
                        if (!target) {
                          setReadinessOpen(true);
                          return;
                        }
                        closeOverview();
                        openContext(target, "checks");
                      }}
                      onAsk={askCapable ? (targetId) => {
                        const target = targetId ? targets.find((item) => item.target_id === targetId) : undefined;
                        if (target) askTarget(target);
                        else openAsk(null);
                      } : undefined}
                      onDecision={historical || readOnly ? undefined : decideOnBrief}
                      onSynthesize={historical || readOnly ? undefined : synthesizeOnBrief}
                      aiReviewRun={aiReviewRun}
                      onRunAiReview={historical || readOnly ? undefined : runAiReviewOnBrief}
                      onRerunAiReviewFresh={historical || readOnly ? undefined : rerunAiReviewFreshOnBrief}
                      onCancelAiReview={historical || readOnly ? undefined : cancelAiReviewOnBrief}
                      onLoadBehaviorTarget={historical ? undefined : loadBehaviorTarget}
                      onJudgeBehavior={historical || readOnly ? undefined : judgeBehavior}
                      onNarrateBehavior={historical || readOnly ? undefined : narrateBehavior}
                      dependencies={dependencyReview}
                      loadArchitectureDelta={architectureRelevant ? loadArchitectureDelta : undefined}
                      onOpenArchitecture={openArchitecture}
                      loadProjectContext={reviewId ? loadProjectContext : undefined}
                      productionOutcomes={historical || !reviewId ? undefined : (
                        <ReviewProductionSection reviewId={reviewId} variant="brief" />
                      )}
                      agentApprovals={historical ? undefined : (
                        <AgentApprovalPanel
                          reviewId={reviewId}
                          initial={projection.behavior?.agent_approvals}
                          readOnly={readOnly}
                        />
                      )}
                      interactivePreview={historical ? undefined : (
                        <InteractivePreviewPanel
                          reviewId={reviewId}
                          revisionId={projection.revision_id}
                          readOnly={readOnly}
                          openedFrom="behavior_changes"
                        />
                      )}
                    />
                  </div>
                  {askOpen && drawer && (
                    <div data-review-rail="ask" className="review-secondary-rail w-[min(32vw,400px)] shrink-0 border-l border-border bg-background">{drawer}</div>
                  )}
                </div>
              ) : briefCapable && projectionState !== "failed" ? (
                <div className="p-4 text-label text-muted-foreground">{"Loading " + (view === "tour" ? "Guided review" : "Brief") + "…"}</div>
              ) : (
                <div className="flex min-h-0 flex-1 items-center justify-center p-6" data-testid="brief-degraded-fallback">
                  <div className="max-w-md rounded-lg border border-amber-900/40 bg-amber-950/10 p-4">
                    <div className="text-body font-medium text-amber-200">Brief unavailable</div>
                    <p className="mt-1 text-caption leading-5 text-muted-foreground">
                      Structured Brief data could not be loaded for this Snapshot. The exact Code review remains available; unavailable analysis is not treated as reviewed or passed.
                    </p>
                    <UiButton type="button" variant="outline" size="compact" className="mt-3" onClick={closeOverview}>
                      Open code
                    </UiButton>
                  </div>
                </div>
              )}
            </Suspense>
          </div>
        )}
        <ReviewOutline
          rows={visibleOutline}
          pageByPath={!dedicatedItem && pageCount > 1 ? pageByPath : undefined}
          attentionByPath={outlineAttention}
          activePath={activeTarget?.path ?? ""}
          query={query}
          searchInputRef={searchInputRef}
          matchTargetCount={searchMatches.targetCount}
          matchFileCount={searchMatches.fileCount}
          matchCountsByPath={searchMatches.byPath}
          currentCohort={comparisonActive || !activeCohort ? null : {
            id: activeCohort.id,
            label: activeCohort.title,
            path: activeCohort.paths[0] ?? "",
          }}
          nextAttention={comparisonActive ? null : nextAttention}
          attentionCount={comparisonActive ? 0 : attentionPath.length}
          attentionPosition={comparisonActive ? 0 : attentionPosition}
          onQuery={setQuery}
          onNavigateMatch={navigateSearchMatch}
          onDismissSearch={dismissSearch}
          onSelectPath={selectPath}
          onSelectTarget={(targetId) => {
            const target = visibleTargets.find((item) => item.target_id === targetId)
              ?? targets.find((item) => item.target_id === targetId);
            if (target) {
              revealImplementation();
              void scrollToTarget(target);
            }
          }}
          onNavigateAttention={(delta) => {
            const target = stepQueueTarget(visibleTargets, activeTarget?.target_id ?? "", delta);
            if (target) {
              revealImplementation();
              void scrollToTarget(target, "instant");
            }
          }}
          onOpenCohort={(cohortId) => {
            changeView("brief");
            window.requestAnimationFrame(() => {
              document.querySelector<HTMLElement>(`[data-cohort-id="${CSS.escape(cohortId)}"]`)?.scrollIntoView({ block: "center" });
            });
          }}
          order={comparisonActive ? "file" : targetList.order}
          orderDisabled={comparisonActive || Boolean(busy)}
          onOrderChange={(nextOrder) => void changeOrder(nextOrder)}
          reviewActions={comparisonActive ? undefined : briefCapable ? projection?.actions : undefined}
          onReviewAction={(id) => {
            if (id.startsWith("behavior:")) {
              changeView("brief");
              const behaviorKey = id.slice("behavior:".length);
              window.requestAnimationFrame(() => {
                document.querySelector<HTMLElement>(`[data-behavior-key="${CSS.escape(behaviorKey)}"]`)?.scrollIntoView({ block: "center" });
              });
              return;
            }
            if (!id.startsWith("decision:")) {
              setReadinessOpen(true);
              return;
            }
            changeView("brief");
            const key = id.slice("decision:".length);
            window.requestAnimationFrame(() => {
              const node = [...document.querySelectorAll<HTMLElement>("[data-testid^='brief-decision-']")]
                .find((item) => item.dataset.testid === `brief-decision-${key}`);
              node?.scrollIntoView({ block: "center" });
            });
          }}
          collapsed={outlineCollapsed}
          onToggleCollapsed={() => setOutlineCollapsed((current) => !current)}
        />

        <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
          {dedicatedItem && (
            <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-info/30 bg-info/10 px-4 py-2 text-label" data-testid="dedicated-review-scope">
              <div className="flex min-w-0 flex-1 items-center gap-2">
                {dedicatedItem.kind === "file"
                  ? <FileDiff size={15} className="shrink-0 text-info" aria-hidden="true" />
                  : <Puzzle size={15} className="shrink-0 text-info" aria-hidden="true" />}
                <span className="shrink-0 font-semibold text-foreground">
                  {dedicatedItem.kind === "file" ? "File view" : "Component view"}
                </span>
                <span className="shrink-0 text-muted-foreground" aria-hidden="true">·</span>
                <span className="min-w-0 truncate font-mono text-foreground" title={dedicatedItem.kind === "file" ? dedicatedItem.path : undefined}>
                  {dedicatedItem.kind === "file"
                    ? dedicatedItem.path
                    : streamSurfaces[0]?.title || dedicatedItem.id}
                </span>
              </div>
              <span className="text-muted-foreground">
                {dedicatedItem.kind === "file"
                  ? `Showing 1 of ${allPathOrder.length} changed files`
                  : "Showing this component from the change"}
              </span>
              <UiButton asChild variant="outline" size="xs" className="shrink-0">
                <a href={fullReviewUrlFromDedicatedItem()}>View full change</a>
              </UiButton>
            </div>
          )}
          {requestScopeApplies && requestScopeIds && (
            <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border bg-muted/30 px-4 py-1.5 text-caption" data-testid="request-scope-bar">
              <span className="min-w-0 flex-1 truncate text-muted-foreground">
                {requestScopeFocus
                  ? `Your request · ${requestScopeIds.size} of ${targets.length} targets`
                  : `All ${targets.length} targets · ${requestScopeIds.size} in your request`}
                {requestScope?.requestedBy ? ` · requested by ${requestScope.requestedBy}` : ""}
              </span>
              <UiButton
                variant="outline"
                size="xs"
                type="button"
                aria-pressed={!requestScopeFocus}
                onClick={() => setRequestScopeFocus((current) => !current)}
              >
                {requestScopeFocus
                  ? `Show ${targets.length - requestScopeIds.size} outside your request`
                  : "Only your request"}
              </UiButton>
            </div>
          )}
          {openingPath && (
            <div
              role="status"
              aria-live="polite"
              className="pointer-events-none absolute left-1/2 top-3 z-30 flex max-w-[calc(100%-2rem)] -translate-x-1/2 items-center gap-2 rounded-md border border-border bg-background/95 px-2.5 py-1 text-label text-neutral-400 shadow-sm"
            >
              <LoaderCircle size={13} className="shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              <span className="truncate">Opening <span className="font-mono text-neutral-300">{openingPath.split("/").pop()}</span>…</span>
            </div>
          )}
          {related && (
            <div className="absolute inset-4 z-40 flex flex-col border border-neutral-700 bg-background shadow-2xl">
              <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-caption">
                <span className="font-mono text-neutral-300">{related.path}</span>
                <span className="text-muted-foreground">reviewed Snapshot</span>
                <span className="flex-1" />
                <UiButton unstyled type="button" onClick={() => setRelated(null)} className="text-neutral-400">Close</UiButton>
              </div>
              {"pending" in related ? (
                <div role="status" className="flex items-center gap-2 p-3 text-label text-muted-foreground">
                  <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  Loading source…
                </div>
              ) : (
                <pre className="min-h-0 flex-1 overflow-auto p-3 font-mono text-label leading-5 text-neutral-300">{related.text}</pre>
              )}
            </div>
          )}
          {comparisonPlaceholderActive ? (
            <div
              className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-label text-muted-foreground"
              role="status"
              data-testid="review-comparison-placeholder"
            >
              {revisionComparisonLoading && (
                <LoaderCircle size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              )}
              <span>
                {revisionComparisonLoading
                  ? (compareBaseRevision === "head"
                    ? `Preparing Source → Snapshot ${compareTargetRevision} diff…`
                    : `Preparing Snapshot ${compareBaseRevision} → Snapshot ${compareTargetRevision} diff…`)
                  : revisionComparisonError
                    || revisionComparison?.code_unavailable_reason
                    || "This comparison is unavailable."}
              </span>
            </div>
          ) : visibleTargets.length === 0 && !dedicatedItemAvailable ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 text-label text-muted-foreground">
              <span>{dedicatedItem
                ? "Not available in this Snapshot."
                : deltaFocus
                  ? "All changed work has been reviewed."
                  : "No matches."}</span>
              {deltaFocus && <UiButton type="button" onClick={() => setDeltaFocus(false)} className="h-7">Show all</UiButton>}
              {!deltaFocus && query.trim() && <UiButton type="button" onClick={dismissSearch} className="h-7">Clear search</UiButton>}
            </div>
          ) : (
            <ReviewLockReason.Provider value={readOnly ? readOnlyNotice : ""}>
            <ReviewStream
              ref={streamRef}
              targets={draftPath ? visibleTargets.filter((target) => pagePathSet.has(target.path) || target.path === draftPath) : pageTargets}
              details={streamDetails}
              fileSemantics={streamFileSemantics}
              surfaces={streamSurfaces}
              showReviewPreviews={Boolean(dedicatedItem) || pageNumber === 1}
              onOpenPreviews={() => {
                const surface = streamSurfaces[0];
                if (surface) void selectSurface(`${surface.provider}:${surface.id}`);
              }}
              dedicatedItem={dedicatedItem}
              surfaceRuns={surfaceRuns}
              surfaceBusy={surfaceBusy}
              runtimeValidations={runtimeValidations}
              runtimeValidationBusy={runtimeValidationBusy}
              revisionId={overview?.revision.id ?? ""}
              errors={streamErrors}
              annotations={surfacedAnnotations}
              draft={readOnly ? null : draft}
              comparisonMode={headComparisonActive}
              snapshotComparison={revisionComparisonActive && revisionComparison ? {
                fromRevision: revisionComparison.from_revision.revision_number,
                toRevision: revisionComparison.to_revision.revision_number,
                fromRevisionId: revisionComparison.from_revision.id,
                toRevisionId: revisionComparison.to_revision.id,
              } : null}
              proposalSelection={proposalSelection}
              activeProposal={activeProposal}
              proposalBusy={busy.startsWith("proposal")}
              sourceMutationSupported={sourceMutationSupported}
              diffStyle={diffStyle}
              diffOverflow={diffOverflow}
              codeTheme={codeTheme}
              chromeTheme={theme}
              activeTargetId={activeTarget?.target_id ?? ""}
              busy={Boolean(busy) || Boolean(readOnly)}
              loadingPaused={Boolean(busy)}
              pagination={!dedicatedItem && pageCount > 1 ? {
                page: pageNumber,
                pageCount,
                changedLines: pageChangedLines,
                blocked: Boolean(draft),
                onChange: changePage,
              } : undefined}
              outstandingByPath={outstandingByPath}
              remainingFileCount={remainingFileCount}
              autoLoadMore={(targetList?.progress.target_count ?? targets.length) <= LARGE_REVIEW_TARGET_THRESHOLD}
              pendingVisibleFileCount={pendingVisibleFileCount}
              readyHiddenFileCount={pendingVisibleFileCount === 0 ? readyHiddenBatch.length : 0}
              preparingHiddenFileCount={preparingHiddenFileCount}
              onActiveTarget={setActiveTargetId}
              onDraft={requestDraft}
              onSuggestEdit={(selectionDraft) => void beginChangeProposal(selectionDraft, "suggest")}
              onEditSource={(selectionDraft) => void beginChangeProposal(selectionDraft, "edit")}
              onQuickEdit={(selectionDraft) => void beginChangeProposal(selectionDraft, "quick")}
              onCreateProposal={() => void createProposal()}
              onCreateAndApplyProposal={() => void createAndApplyProposal()}
              onApplyProposal={() => void applyProposal()}
              onCloseProposal={closeProposalComposer}
              onCreate={(input) => void createAnnotation(input)}
              onSetAnnotationState={(id, state) => void setAnnotationState(id, state)}
              onFindingDisposition={(id, disposition, reason) => void setFindingDisposition(id, disposition, reason)}
              onMark={(target, state, advance) => void markTarget(target, state, advance)}
              onComment={commentTarget}
              onAsk={askCapable ? askTarget : undefined}
              onAskSelection={askCapable ? askSelection : undefined}
              onContext={openContext}
              onBulkReview={(path) => void bulkReviewFile(path)}
              onLoadMore={loadMore}
              onFirstContentReady={markFirstContentPainted}
              onRunSurface={(surface, compare) => void executeSurface(surface, compare)}
              onValidateSurface={(surface) => void validateSurfaceRuntime(surface)}
              onCancelRuntimeValidation={(surface, jobId) => void cancelSurfaceRuntimeValidation(surface, jobId)}
              onRetryPath={retryPath}
              onOpenInCode={runtimeCodeRoot ? openInCode : undefined}
            />
            </ReviewLockReason.Provider>
          )}
        </main>

        {(contextOpen || (askOpen && view === "implementation")) && drawer && (
          <div data-review-rail={askOpen ? "ask" : "context"} className={"review-secondary-rail " + ((askOpen || contextPinned)
            ? "absolute inset-y-0 right-0 z-50 w-[min(420px,calc(100%-40px))] border-l border-neutral-700 bg-background shadow-[-18px_0_40px_rgba(0,0,0,0.35)] min-[960px]:static min-[960px]:z-auto min-[960px]:w-[min(32vw,400px)] min-[960px]:shrink-0 min-[960px]:border-border min-[960px]:shadow-none"
            : "absolute inset-y-0 right-0 z-50 w-[min(420px,calc(100%-40px))] border-l border-neutral-700 bg-background shadow-[-18px_0_40px_rgba(0,0,0,0.35)]")}>
            {drawer}
          </div>
        )}
      </div>

      {githubPublishOpen && reviewId && (
        <Suspense fallback={<div className="absolute inset-0 z-50 flex items-center justify-center bg-black/45 text-label text-neutral-400">Loading GitHub preview…</div>}>
          <GitHubPublishSheet
            reviewId={reviewId}
            onClose={() => { setGithubPublishOpen(false); setFinishAfterPublish(false); }}
            onPublished={() => {
              void refreshFeedbackState();
              if (finishAfterPublish) void finishPublished();
            }}
          />
        </Suspense>
      )}

      {finishSheetOpen && finishSummary && (
        <Suspense fallback={<div className="absolute inset-0 z-50 flex items-center justify-center bg-black/45 text-label text-neutral-400">Loading finish summary…</div>}>
          <FinishSheet
            reviewId={reviewId}
            summary={finishSummary}
            degraded={finishSummary.degraded ?? overview.degraded}
            evidenceFacts={readiness.evidenceFacts}
            busy={busy === "finish"}
            currentOutcome={currentOutcome}
            handoff={{
              unsent: handoff.unsent,
              label: handoffRoute === "export"
                ? "the author"
                : handoffRoute === "github" && !feedbackDelivery?.label ? "the pull request" : handoffDestinationLabel(feedbackDelivery),
              kind: handoffRoute,
              available: Boolean(feedbackDelivery?.supported) || handoffRoute === "export"
                || (handoffRoute === "github" && isGitHubPullRequest && !feedbackDelivery),
              reason: feedbackDelivery?.reason ?? "",
            }}
            destinationControl={reviewId ? (
              <ReviewDestinationPicker reviewId={reviewId} disabled={busy === "finish"} onChanged={() => void refreshFeedbackState("Changed")}>
                <UiButton variant="ghost" size="compact" type="button">change…</UiButton>
              </ReviewDestinationPicker>
            ) : undefined}
            sendError={finishSendError}
            finishError={finishError}
            onOpenStatus={() => {
              setFinishSheetOpen(false);
              setReadinessOpen(true);
            }}
            onClose={closeFinishSheet}
            onCloseAutoFocus={focusPrimaryAfterFinish}
            onFinish={(outcome, summary, options) => void confirmFinish(outcome, summary, options)}
          />
        </Suspense>
      )}

      {/* Phase 14 item 9: the desktop footer is gone. Its progress counter and
          uncertainty count duplicated the Status control (the one status
          authority), and its shortcut line duplicated `?` / More → Keyboard
          shortcuts. Phones dock the tools and primary action here instead. */}
      {compactLayout && (
        <div ref={setCompactDock} className="review-mobile-dock" data-testid="review-mobile-dock" />
      )}
    </div>
    </FeedbackSyncProvider>
    </ReviewCorrectionsProvider>
  );
}
