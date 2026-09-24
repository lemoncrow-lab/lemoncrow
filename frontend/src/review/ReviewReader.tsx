import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import "./reviewUi.css";

import type { ContextDrawerTab } from "./ContextDrawer";
import ReaderHeader from "./ReaderHeader";
import ReviewAttentionBar from "./ReviewAttentionBar";
import ReviewFeedbackPanel from "./ReviewFeedbackPanel";
import ReviewReadinessSheet, { deriveReviewReadiness, type ReviewReadinessBlocker } from "./ReviewReadinessSheet";
import type { ReviewPaletteAction } from "./ReviewCommandPalette";
import type { Theme } from "../lib/theme";
import { applyReviewCodeTheme, getInitialReviewCodeTheme, reviewCodeThemeDefinition, type ReviewCodeTheme } from "./reviewCodeTheme";
import RevisionDeltaBar from "./RevisionDeltaBar";
import ReviewOutline from "./ReviewOutline";
import ReviewStream, { type ReviewStreamHandle } from "./ReviewStream";
import { readDedicatedReviewItem } from "./reviewItemNavigation";
import { useReviewDropdownDismissal } from "./useReviewDropdownDismissal";
import { useReviewEnvironment, type ReviewDeploymentMode } from "./reviewEnvironment";
import type { Draft } from "./annotationModel";
import { getInitialDiffStylePreference, nextDiffStylePreference, persistDiffStylePreference, type DiffOverflow, type DiffStylePreference } from "./diffModel";
import {
  bestTargetAfterRevision,
  firstActionable,
  filterTargets,
  type ReaderOrder,
  orderedPaths,
  progressFromTargets,
  nextReviewGuideTarget,
  reviewAttentionPath,
  reviewStorySteps,
  stepAttentionTarget,
  stepFile,
  stepTarget,
  targetScrollLocation,
} from "./readerModel";
import { deriveReviewPrimaryAction } from "./reviewPrimaryAction";
import {
  adoptBootstrapFragment,
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
  fetchHistoricalRelatedSource,
  fetchHistoricalTargets,
  fetchOverview,
  fetchProposalSelection,
  fetchChangeProposals,
  fetchRelatedSource,
  fetchReviewEvidence,
  fetchReviewOutcome,
  fetchReviewPreparation,
  fetchReviewPatch,
  fetchRevisionLocator,
  fetchReviewSurfaces,
  runReviewSurface,
  fetchSourceState,
  fetchTargets,
  finishReview,
  linkReviewEvidence,
  patchAnnotation,
  postAnnotation,
  postBulkReviewed,
  postMark,
  postRefresh,
  postReviewOutcome,
  historicalReviewHref,
  reviewDirectoryHref,
  reviewHref,
  sourceCompareHref,
  selectReviewId,
  uploadReviewEvidence,
} from "./reviewApi";
import type {
  Annotation,
  AnnotationDraft,
  AnnotationState,
  FeedbackDelivery,
  FeedbackDeliveryCapability,
  FeedbackExport,
  FeedbackStatus,
  FileDetail,
  MarkState,
  RefreshInfo,
  RelatedSource,
  ReviewChangeProposal,
  ReviewClosure,
  ReviewEvidence,
  ReviewOutlineItem,
  ReviewOutcome,
  ReviewOutcomeKind,
  ReviewOverview,
  ReviewProposalSelection,
  ReviewSurfaceInfo,
  ReviewSurfaceRunResult,
  ReviewTarget,
  ReviewTargetList,
  SourceState,
} from "./types";

const ContextDrawer = lazy(() => import("./ContextDrawer"));
const FinishSheet = lazy(() => import("./FinishSheet"));
const ReviewCommandPalette = lazy(() => import("./ReviewCommandPalette"));
const ReviewOverviewSheet = lazy(() => import("./ReviewOverviewSheet"));
const ReviewCommentsSheet = lazy(() => import("./ReviewCommentsSheet"));
const ReviewHistorySheet = lazy(() => import("./ReviewHistorySheet"));

const INITIAL_CHANGE_BUDGET = 400;
const INITIAL_MAX_FILES = 5;
const LOAD_MORE_CHANGE_BUDGET = 800;
const LOAD_MORE_MAX_FILES = 30;
const PREFETCH_AHEAD_FILES = 2;
const COMPLETE_SMALL_REVIEW_TARGETS = 30;
const BACKGROUND_PREFETCH_FILES = 30;
const CONTEXT_PIN_KEY = "lemoncrow.review.reader.contextPinned";
const DIFF_OVERFLOW_KEY = "lemoncrow.review.reader.diffOverflow";

const SOURCE_PROBE_INTERVAL_MS = 2000;
const PREPARATION_POLL_MS = 180;
const EMPTY_FEEDBACK_STATUS: FeedbackStatus = {
  open_total: 0,
  unpublished: 0,
  published: 0,
  in_flight: 0,
  addressed: 0,
};

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
  if (targets.length <= COMPLETE_SMALL_REVIEW_TARGETS) return paths;
  return pathsForChangeBudget(
    paths,
    changeLinesByPath(targets),
    INITIAL_CHANGE_BUDGET,
    INITIAL_MAX_FILES,
  );
}

function surfacePlacementPaths(surfaces: readonly ReviewSurfaceInfo[], targets: readonly ReviewTarget[]): string[] {
  const reviewPaths = new Set(targets.map((target) => target.path));
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
  outcome?: ReviewOutcome | null;
  recordOutcome?: (outcome: ReviewOutcomeKind, summary: string) => Promise<ReviewOutcome>;
  authorship?: { prefix: string; label: string };
  context?: { label: string; title?: string };
  readinessBlockers?: readonly ReviewReadinessBlocker[];
  readinessSections?: ReactNode;
  renderHeaderActions?: (context: ReviewReaderExtensionContext) => ReactNode;
  renderOverlays?: (context: ReviewReaderExtensionContext) => ReactNode;
}

export interface ReviewReaderProps {
  /** Working-tree probe cadence; tests may override the shipped value. */
  sourceProbeIntervalMs?: number;
  /** Neutral extension points used by non-local compositions. */
  extensions?: ReviewReaderExtensions;
}

export default function ReviewReader({ sourceProbeIntervalMs = SOURCE_PROBE_INTERVAL_MS, extensions }: ReviewReaderProps = {}) {
  useReviewDropdownDismissal();
  const reviewEnvironment = useReviewEnvironment(extensions?.environmentFallbackMode ?? "local");
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
  const legacyHistoricalRevisionNumber = /^\d+$/.test(routeRevisionNumber) && Number(routeRevisionNumber) > 0
    ? Number(routeRevisionNumber)
    : 0;
  const dedicatedItemRef = useRef(readDedicatedReviewItem());
  const dedicatedItem = dedicatedItemRef.current;
  const [reviewId, setReviewId] = useState("");
  const [locatedRevision, setLocatedRevision] = useState<{ reviewId: string; revisionNumber: number } | null>(null);
  const historicalRevisionNumber = legacyHistoricalRevisionNumber || locatedRevision?.revisionNumber || 0;
  const historicalRoute = historicalRevisionNumber > 0;
  const [authed, setAuthed] = useState(true);
  const [preparationId, setPreparationId] = useState("");
  const [preparationStage, setPreparationStage] = useState("");
  const [startupReady, setStartupReady] = useState(false);
  const [firstReviewableReady, setFirstReviewableReady] = useState(false);
  const [initialSourcePaths, setInitialSourcePaths] = useState<string[]>([]);
  const [preparationRefresh, setPreparationRefresh] = useState(0);
  const [overview, setOverview] = useState<ReviewOverview | null>(null);
  // A revision URL is only historical when it points behind the Review's
  // current head. Opening /rr/<latest> should behave exactly like /r/<review>:
  // no historical ribbon, writable latest-state actions, and source probing.
  const historical = historicalRoute && (
    !overview
    || overview.revision.revision_number !== (overview.latest_revision_number ?? overview.revision.revision_number)
  );
  const [targetList, setTargetList] = useState<ReviewTargetList | null>(null);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [commentsState, setCommentsState] = useState<"loading" | "ready" | "failed">("loading");

  useEffect(() => {
    const titleReviewId = overview?.session.ref || routeReviewId || routeReviewRef || overview?.session.id;
    if (!titleReviewId) return;
    document.title = `Review: ${titleReviewId}`;
  }, [overview?.session.ref, overview?.session.id, routeReviewId, routeReviewRef]);

  useEffect(() => {
    if (!overview || typeof window === "undefined") return;
    const canonical = historical
      ? historicalReviewHref(overview.revision.ref || overview.revision.id)
      : reviewHref(overview.session.ref || overview.session.id);
    if (canonical && window.location.pathname !== canonical) {
      window.history.replaceState(null, "", canonical);
    }
  }, [overview, historical, historicalRevisionNumber]);

  const [contextFailures, setContextFailures] = useState<Partial<Record<"comments" | "evidence" | "outcome" | "surfaces", string>>>({});
  const [evidence, setEvidence] = useState<ReviewEvidence[]>([]);
  const [surfaces, setSurfaces] = useState<ReviewSurfaceInfo[]>([]);
  const [surfaceRuns, setSurfaceRuns] = useState<Record<string, { old?: ReviewSurfaceRunResult; new?: ReviewSurfaceRunResult; error?: string }>>({});
  const [surfaceBusy, setSurfaceBusy] = useState("");
  const [codeTheme, setCodeTheme] = useState<ReviewCodeTheme>(() => getInitialReviewCodeTheme());
  const [theme, setTheme] = useState<Theme>(() => reviewCodeThemeDefinition(getInitialReviewCodeTheme()).chromeTheme);
  const [details, setDetails] = useState<Record<string, FileDetail | undefined>>({});
  const [fileErrors, setFileErrors] = useState<Record<string, string | undefined>>({});
  const [revealedPaths, setRevealedPaths] = useState<Set<string>>(() => new Set());
  const [activeTargetId, setActiveTargetId] = useState("");
  const [query, setQuery] = useState("");
  const [outlineCollapsed, setOutlineCollapsed] = useState(() => Boolean(dedicatedItem) || (typeof window.matchMedia === "function" && window.matchMedia("(max-width: 639px)").matches));
  const [focusMode, setFocusMode] = useState(Boolean(dedicatedItem));
  const [contextOpen, setContextOpen] = useState(false);
  const [contextTab, setContextTab] = useState<ContextDrawerTab>("impact");
  const [contextPinned, setContextPinned] = useState(() =>
    typeof window !== "undefined" && window.sessionStorage.getItem(CONTEXT_PIN_KEY) === "1",
  );
  const [diffStyle, setDiffStyle] = useState<DiffStylePreference>(() => getInitialDiffStylePreference());
  const [diffOverflow, setDiffOverflow] = useState<DiffOverflow>(() =>
    typeof window !== "undefined" && window.localStorage.getItem(DIFF_OVERFLOW_KEY) === "wrap" ? "wrap" : "scroll",
  );
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState<FeedbackExport | null>(null);
  const [feedbackStatus, setFeedbackStatus] = useState<FeedbackStatus>(EMPTY_FEEDBACK_STATUS);
  const [feedbackDelivery, setFeedbackDelivery] = useState<FeedbackDeliveryCapability | null>(null);
  const [delivery, setDelivery] = useState<FeedbackDelivery | null>(null);
  const [finishSummary, setFinishSummary] = useState<ReviewClosure | null>(null);
  const [finishSheetOpen, setFinishSheetOpen] = useState(false);
  const [localOutcome, setLocalOutcome] = useState<ReviewOutcome | null>(null);
  const [outcomeOverride, setOutcomeOverride] = useState<ReviewOutcome | null>(null);
  const [readinessOpen, setReadinessOpen] = useState(false);
  const [sourceState, setSourceState] = useState<SourceState | null>(null);
  const [proposals, setProposals] = useState<ReviewChangeProposal[]>([]);
  const [sourceMutationSupported, setSourceMutationSupported] = useState(false);
  const [proposalSelection, setProposalSelection] = useState<ReviewProposalSelection | null>(null);
  const [activeProposal, setActiveProposal] = useState<ReviewChangeProposal | null>(null);
  const [dismissedSourceFingerprint, setDismissedSourceFingerprint] = useState("");
  const [revisionInfo, setRevisionInfo] = useState<RefreshInfo | null>(null);
  const [deltaFocus, setDeltaFocus] = useState(false);
  const [related, setRelated] = useState<RelatedSource | null>(null);
  const [commandMode, setCommandMode] = useState<"actions" | "shortcuts" | null>(null);
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [commentsOpen, setCommentsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [revisionAdvanced, setRevisionAdvanced] = useState(false);
  const currentOutcome = outcomeOverride ?? extensions?.outcome ?? localOutcome;
  const changeDiffStyle = useCallback((preference: DiffStylePreference) => {
    setDiffStyle(preference);
    persistDiffStylePreference(preference);
  }, []);
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
  const contextReturnFocusRef = useRef<HTMLElement | null>(null);
  const commandReturnFocusRef = useRef<HTMLElement | null>(null);
  const overviewReturnFocusRef = useRef<HTMLElement | null>(null);
  const inFlightPaths = useRef(new Map<string, Promise<FileDetail | null>>());
  const detailsRef = useRef<Record<string, FileDetail | undefined>>({});
  const staleDetailPathsRef = useRef(new Set<string>());
  const revealedPathsRef = useRef(new Set<string>());
  const backgroundLoadQueueRef = useRef<Array<{ reviewId: string; path: string }>>([]);
  const backgroundLoadActiveRef = useRef(false);
  const loadGenerationRef = useRef(0);
  const sourceProbeRef = useRef({ fingerprint: "", count: 0 });
  const stableTargetRevisionRef = useRef("");
  const stableTargetOrderRef = useRef<string[]>([]);
  const automaticSurfaceRunsRef = useRef(new Set<string>());

  useLayoutEffect(() => {
    const applied = applyReviewCodeTheme(codeTheme);
    if (applied !== theme) setTheme(applied);
    // Theme selection owns both source syntax and Reader chrome. Rendered web
    // previews keep their own Auto/Light/Dark preference.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [codeTheme]);

  useEffect(() => {
    let live = true;
    const bootstrap = adoptBootstrapFragment();
    setAuthed(true);
    setError("");
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
          setError(err instanceof Error ? err.message : String(err));
        });
      return () => {
        live = false;
      };
    }

    const resolvedReviewId = routeReviewId || routeReviewRef || bootstrap.reviewId;
    if (resolvedReviewId) selectReviewId(resolvedReviewId);
    setReviewId(resolvedReviewId);
    setPreparationId(bootstrap.preparationId ?? "");
    setStartupReady(!bootstrap.preparationId);
    return () => {
      live = false;
    };
  }, [routeReviewId, routeReviewRef, routeRevisionRef]);

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
    request = (historical ? fetchHistoricalFile(id, historicalRevisionNumber, path) : fetchFile(id, path))
      .then((detail) => {
        if (loadGenerationRef.current !== generation) return null;
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
  }, [historical, historicalRevisionNumber]);

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
    const remaining = allPaths.filter((path) => !revealedPathsRef.current.has(path));
    const next = pathsForChangeBudget(remaining, weights, LOAD_MORE_CHANGE_BUDGET, BACKGROUND_PREFETCH_FILES);
    queueBackgroundPaths(id, next);
  }, [queueBackgroundPaths]);

  const stabilizeTargetOrder = useCallback((next: ReviewTargetList): ReviewTargetList => {
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

  const loadReader = useCallback(async (id: string): Promise<boolean> => {
    setCommentsState("loading");
    setContextFailures({});
    try {
      // Source review is the critical path. Historical routes use the same
      // progressive source-first pipeline, but resolve immutable projections.
      const [nextOverview, fetchedTargets] = await Promise.all([
        historical ? fetchHistoricalOverview(id, historicalRevisionNumber) : fetchOverview(id),
        historical ? fetchHistoricalTargets(id, historicalRevisionNumber) : fetchTargets(id),
      ]);
      const nextTargets = historical ? fetchedTargets : stabilizeTargetOrder(fetchedTargets);
      const annotationsPromise = historical
        ? fetchHistoricalAnnotations(id, historicalRevisionNumber)
        : fetchAnnotations(id);
      const evidencePromise = historical
        ? Promise.resolve({ evidence: [] as ReviewEvidence[] })
        : fetchReviewEvidence(id);
      const outcomePromise = historical
        ? Promise.resolve(null)
        : fetchReviewOutcome(id).then((next) => next.current);

      let dedicatedSurfaces: ReviewSurfaceInfo[] = [];
      if (!historical && dedicatedItem?.kind === "surface") {
        // A dedicated surface was the human's explicit action, so its discovery
        // is part of the foreground path rather than speculative work.
        dedicatedSurfaces = (await fetchReviewSurfaces(id)).surfaces;
        setSurfaces(dedicatedSurfaces);
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

      const allPaths = orderedPaths(nextTargets.targets);
      const weights = changeLinesByPath(nextTargets.targets);
      const pathsToShow = dedicatedItem?.kind === "file"
        ? [dedicatedItem.path]
        : dedicatedItem?.kind === "surface"
          ? dedicatedSurfaces.find(
            (surface) => surface.provider === dedicatedItem.provider && surface.id === dedicatedItem.id,
          )?.affected_paths ?? []
          : initialPaths(nextTargets.targets);
      const uniquePaths = [...new Set(pathsToShow)];
      setInitialSourcePaths(uniquePaths);
      revealPaths(uniquePaths, true);

      // Prepare the whole first window concurrently, but do not expose the full
      // Reader shell until at least one real source diff is readable. Remaining
      // initial files append themselves as their requests finish.
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
        setAnnotations(next.annotations);
        setFeedbackStatus(next.feedback ?? fallbackFeedbackStatus(next.annotations));
        setFeedbackDelivery(next.delivery ?? null);
        setCommentsState("ready");
        setContextFailures((current) => ({ ...current, comments: undefined }));
      }).catch((reason: unknown) => {
        setCommentsState("failed");
        setContextFailures((current) => ({
          ...current,
          comments: `Review discussions unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
        }));
      });
      void evidencePromise.then((next) => {
        setEvidence(next.evidence);
        setContextFailures((current) => ({ ...current, evidence: undefined }));
      }).catch((reason: unknown) => {
        setContextFailures((current) => ({
          ...current,
          evidence: `Review evidence unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
        }));
      });
      void outcomePromise.then((next) => {
        setLocalOutcome(next);
        setContextFailures((current) => ({ ...current, outcome: undefined }));
      }).catch((reason: unknown) => {
        setContextFailures((current) => ({
          ...current,
          outcome: `Review outcome unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
        }));
      });

      // Normal rendered-surface discovery starts only after the first source
      // diff is readable. Expensive route/runtime work cannot compete with time
      // to first reviewable content.
      await firstSourceReady;
      setFirstReviewableReady(true);
      if (!historical && dedicatedItem?.kind !== "surface") {
        void fetchReviewSurfaces(id).then((next) => {
          setSurfaces(next.surfaces);
          setContextFailures((current) => ({ ...current, surfaces: undefined }));
          queueBackgroundPaths(
            id,
            surfacePlacementPaths(next.surfaces, nextTargets.targets).filter((path) => !revealedPathsRef.current.has(path)),
          );
        }).catch((reason: unknown) => {
          setContextFailures((current) => ({
            ...current,
            surfaces: `Rendered review surfaces unavailable: ${String(reason instanceof Error ? reason.message : reason)}`,
          }));
        });
      }

      // The rest of the initial window is already in flight. Separately prepare
      // the next pagination chunk, but never reveal unfinished files just
      // because the reviewer clicked a pagination control.
      if (!dedicatedItem) queueNextPage(id, allPaths, weights);
      await firstPageLoad;
      return true;
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
      return false;
    }
  }, [dedicatedItem, historical, historicalRevisionNumber, loadPath, queueBackgroundPaths, queueNextPage, revealPaths, stabilizeTargetOrder]);

  useEffect(() => {
    if (!reviewId || !authed || !startupReady) return;
    backgroundLoadQueueRef.current = [];
    void loadReader(reviewId);
  }, [reviewId, authed, startupReady, preparationRefresh, loadReader]);

  useEffect(() => {
    if (!reviewId || !authed || !overview || historical) return;
    if (!reviewEnvironment.review.workspace_refresh) {
      setSourceState(null);
      return;
    }
    if (overview.session.range_mode !== "working_tree" && overview.session.range_mode !== "staged") return;
    // A reader that cannot accept a new revision must not advertise one. The
    // banner's only action is a refresh that an archived session — or a reader
    // already stranded on a superseded revision — refuses, so the button could
    // never do anything but sit there.
    if (overview.session.status === "archived" || revisionAdvanced) {
      setSourceState(null);
      return;
    }
    let live = true;
    let inFlight = false;
    const check = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const state = await fetchSourceState(reviewId);
        if (live) {
          if (!state.supported || !state.changed) {
            sourceProbeRef.current = { fingerprint: "", count: 0 };
            setSourceState(null);
          } else {
            // An agent that is still writing moves the working tree under every
            // probe. Interrupting on the first observation raises the banner for
            // a half-written commit, so the same fingerprint has to be seen
            // twice before the reviewer hears about it.
            const seen = sourceProbeRef.current;
            const count = seen.fingerprint === state.fingerprint ? seen.count + 1 : 1;
            sourceProbeRef.current = { fingerprint: state.fingerprint, count };
            const settled = count >= 2 && state.fingerprint !== dismissedSourceFingerprint;
            setSourceState(settled ? state : null);
          }
        }
      } catch {
        // Source probing is convenience. Never interrupt a frozen revision for
        // a transient probe failure.
      } finally {
        inFlight = false;
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), sourceProbeIntervalMs);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [reviewId, authed, overview?.revision.id, overview?.session.range_mode, overview?.session.status, revisionAdvanced, dismissedSourceFingerprint, sourceProbeIntervalMs, historical, reviewEnvironment.review.workspace_refresh]);

  useEffect(() => {
    if (!reviewId || !authed || historical) {
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
  }, [reviewId, authed, historical, overview?.revision.id]);

  const hasOpenHumanFeedback = annotations.some(
    (item) => item.source === "human" && !item.parent_id && item.state !== "resolved" && item.state !== "obsolete",
  );
  useEffect(() => {
    if (!reviewId || !authed || historical || !hasOpenHumanFeedback || !overview?.revision.id) return;
    let live = true;
    let inFlight = false;
    const refreshMetadata = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const result = await fetchAnnotations(reviewId);
        if (!live || result.revision_id !== overview.revision.id) return;
        setAnnotations((current) => {
          const currentById = new Map(current.map((item) => [item.id, item]));
          return result.annotations.map((item) => {
            const previous = currentById.get(item.id);
            return previous && previous.updated_at > item.updated_at ? previous : item;
          });
        });
        setFeedbackStatus(result.feedback ?? fallbackFeedbackStatus(result.annotations));
      } catch {
        // Metadata refresh is convenience. The durable review remains authoritative.
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => void refreshMetadata(), Math.max(1500, sourceProbeIntervalMs));
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [reviewId, authed, historical, hasOpenHumanFeedback, overview?.revision.id, sourceProbeIntervalMs]);

  const targets = targetList?.targets ?? [];
  const scopedTargets = useMemo(() => {
    if (!dedicatedItem) return targets;
    if (dedicatedItem.kind === "file") return targets.filter((target) => target.path === dedicatedItem.path);
    const surface = surfaces.find((item) => item.provider === dedicatedItem.provider && item.id === dedicatedItem.id);
    if (!surface) return [];
    const paths = new Set(surface.affected_paths);
    return targets.filter((target) => paths.has(target.path));
  }, [dedicatedItem, surfaces, targets]);
  const streamSurfaces = useMemo(() => {
    if (!dedicatedItem) return surfaces;
    if (dedicatedItem.kind === "file") return [];
    return surfaces.filter((item) => item.provider === dedicatedItem.provider && item.id === dedicatedItem.id);
  }, [dedicatedItem, surfaces]);
  const dedicatedItemAvailable = dedicatedItem?.kind === "surface"
    ? streamSurfaces.length > 0
    : dedicatedItem?.kind === "file"
      ? Boolean(details[dedicatedItem.path] || fileErrors[dedicatedItem.path])
      : false;
  const annotationSearchText = useMemo(
    () => annotationSearchTextByTarget(scopedTargets, annotations),
    [scopedTargets, annotations],
  );
  const searchedTargets = useMemo(
    () => filterTargets(scopedTargets, query, annotationSearchText),
    [scopedTargets, query, annotationSearchText],
  );
  const deltaActiveIds = useMemo(
    () => new Set(revisionInfo?.target_delta?.active.map((target) => target.target_id) ?? []),
    [revisionInfo],
  );
  const deltaRemainingCount = useMemo(
    () => targets.filter((target) => deltaActiveIds.has(target.target_id) && targetStillNeedsAttention(target)).length,
    [targets, deltaActiveIds],
  );
  const filteredTargets = useMemo(
    () => deltaFocus
      ? searchedTargets.filter((target) => deltaActiveIds.has(target.target_id) && targetStillNeedsAttention(target))
      : searchedTargets,
    [searchedTargets, deltaFocus, deltaActiveIds],
  );
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
  const allPathOrder = useMemo(() => orderedPaths(targets), [targets]);
  const fileChangeLines = useMemo(() => changeLinesByPath(targets), [targets]);
  const visiblePathOrder = useMemo(() => orderedPaths(visibleTargets), [visibleTargets]);
  const navigationPathOrder = visiblePathOrder.length > 0 ? visiblePathOrder : allPathOrder;
  // reason the counter directly above it does: a file the stream only carries
  // because it holds a draft matched nothing, so listing it here would answer
  // "1 target · 1 file" with two rows, the second reading "0 matches".
  const outlinePaths = useMemo(() => new Set(filteredTargets.map((target) => target.path)), [filteredTargets]);
  const visibleOutline = useMemo(
    () => (targetList?.outline ?? []).filter((row) => outlinePaths.has(row.path)),
    [targetList?.outline, outlinePaths],
  );
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
  const activeDetail = activeTarget ? details[activeTarget.path] ?? null : null;
  const progress = targetList?.progress ?? progressFromTargets(targets);
  const rootCommentCount = annotations.filter((item) => !item.parent_id).length;
  const openRootCommentCount = annotations.filter(
    (item) => !item.parent_id && item.state !== "resolved" && item.state !== "obsolete",
  ).length;
  const readiness = deriveReviewReadiness({
    progress,
    openComments: openRootCommentCount,
    commentsState,
    verification: overview?.brief?.verification ?? { pass: 0, fail: 0, not_run: 0, unknown: 0 },
    evidence: overview?.evidence ?? [],
    staleArtifacts: overview?.brief?.artifacts.stale ?? 0,
    additionalBlockers: extensions?.readinessBlockers,
    outcome: currentOutcome,
  });
  const globalNextTarget = nextReviewGuideTarget(targets);
  const primaryAction = deriveReviewPrimaryAction({
    historical,
    sessionStatus: overview?.session.status ?? "open",
    revisionAdvanced,
    sourceChanged: Boolean(sourceState?.changed),
    commentsState,
    feedbackStatus,
    feedbackDelivery,
    progress,
    readiness,
    nextTarget: globalNextTarget,
  });
  const readOnly = historical || overview?.session.status === "archived" || revisionAdvanced;

  useEffect(() => {
    if (!activeTarget && visibleTargets.length > 0) setActiveTargetId(firstActionable(visibleTargets)?.target_id ?? "");
  }, [activeTarget, visibleTargets]);

  const scrollToTarget = useCallback(async (target: ReviewTarget, behavior: "instant" | "smooth" = "smooth") => {
    if (!reviewId) return;
    setActiveTargetId(target.target_id);
    revealPaths([target.path]);
    await loadPath(reviewId, target.path);
    const pathIndex = navigationPathOrder.indexOf(target.path);
    if (pathIndex >= 0) {
      // Navigation hints are speculative. Keep them behind the explicit file
      // the reviewer chose, and never make them visible until Load more or a
      // later direct navigation reveals them.
      queueBackgroundPaths(
        reviewId,
        navigationPathOrder.slice(pathIndex + 1, pathIndex + 1 + PREFETCH_AHEAD_FILES),
      );
    }
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => streamRef.current?.scrollToTarget(target, behavior)));
  }, [reviewId, loadPath, navigationPathOrder, queueBackgroundPaths, revealPaths]);

  const selectPath = useCallback((path: string) => {
    if (typeof window.matchMedia === "function" && window.matchMedia("(max-width: 639px)").matches) setOutlineCollapsed(true);
    const visible = targetForPath(visibleTargets, path);
    if (visible) {
      void scrollToTarget(visible);
      return;
    }
    const anywhere = targetForPath(targets, path);
    if (!anywhere) return;
    // Explicit navigation from the change overview must not become a silent
    // no-op merely because the destination is outside a transient filter.
    setQuery("");
    setDeltaFocus(false);
    void scrollToTarget(anywhere);
  }, [visibleTargets, targets, scrollToTarget]);

  const selectSurface = useCallback(async (surfaceKey: string) => {
    if (!reviewId) return;
    const surface = surfaces.find((candidate) => `${candidate.provider}:${candidate.id}` === surfaceKey);
    if (!surface) return;
    setQuery("");
    setDeltaFocus(false);
    const paths = surface.affected_paths.slice(0, 4);
    revealPaths(paths);
    await Promise.all(paths.map((path) => loadPath(reviewId, path)));
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => streamRef.current?.scrollToSurface(surfaceKey)));
  }, [loadPath, revealPaths, reviewId, surfaces]);

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
        ? await fetchHistoricalTargets(reviewId, historicalRevisionNumber, nextOrder)
        : await fetchTargets(reviewId, nextOrder);
      const nextTargets = historical ? fetchedTargets : stabilizeTargetOrder(fetchedTargets);
      setTargetList(nextTargets);
      const survivor = nextTargets.targets.find((target) => target.target_id === currentTargetId)
        ?? firstActionable(nextTargets.targets);
      setActiveTargetId(survivor?.target_id ?? "");

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
        window.requestAnimationFrame(() =>
          window.requestAnimationFrame(() => streamRef.current?.scrollToTarget(survivor, "instant"))
        );
      }
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, busy, targetList?.order, activeTarget?.target_id, activeTargetId, deltaFocus, deltaActiveIds, loadPaths, queueNextPage, revealPaths]);

  const updateTargetListAfterMark = useCallback((target: ReviewTarget, result: Awaited<ReturnType<typeof postMark>>) => {
    setTargetList((current) => {
      if (!current) return current;
      const replacement = result.target ?? { ...target, state: result.recorded, changed_since_mark: false };
      const nextTargets = current.targets.map((item) => item.target_id === target.target_id ? replacement : item);
      return {
        ...current,
        targets: nextTargets,
        progress: result.progress ?? progressFromTargets(nextTargets),
        outline: mergeOutline(current.outline, result.outline_updates),
      };
    });
  }, []);

  const bulkReviewFile = useCallback(async (path: string) => {
    if (!reviewId || readOnly || busy) return;
    const unitKeys = targets.filter((target) => target.path === path && target.state !== "reviewed").map((target) => target.unit_key);
    if (unitKeys.length === 0) return;
    setBusy(`bulk:${path}`);
    try {
      const result = await postBulkReviewed(reviewId, unitKeys);
      const marked = new Set(result.marked.map((item) => item.unit_key));
      setTargetList((current) => {
        if (!current) return current;
        const nextTargets = current.targets.map((target) => marked.has(target.unit_key)
          ? { ...target, state: "reviewed" as const, changed_since_mark: false, reviewed_revision_id: current.revision_id }
          : target);
        return {
          ...current,
          targets: nextTargets,
          progress: result.progress ?? progressFromTargets(nextTargets),
          outline: mergeOutline(current.outline, result.outline_updates),
        };
      });
      if (result.skipped.length > 0) {
        const reasons = [...new Set(result.skipped.map((item) => item.reason))];
        setError(`${result.marked.length} marked reviewed · ${result.skipped.length} kept for individual review · ${reasons.join(" · ")}`);
      } else {
        setError("");
      }
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, targets]);

  const markTarget = useCallback(async (target: ReviewTarget, state: MarkState, advance = false) => {
    if (!reviewId || readOnly || busy) return;
    setBusy(state);
    try {
      const result = await postMark(reviewId, target.unit_key, state);
      updateTargetListAfterMark(target, result);
      if (result.downgraded) {
        setError(result.note);
        return;
      }
      setError("");
      if (advance) {
        const projected = visibleTargets.map((item) => item.target_id === target.target_id
          ? (result.target ?? { ...item, state: result.recorded })
          : item);
        const next = nextActionableTargetInFile(projected, target)
          ?? stepTarget(projected, target.target_id, 1, { actionableOnly: true });
        if (next && next.target_id !== target.target_id) await scrollToTarget(next);
      }
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, updateTargetListAfterMark, visibleTargets, scrollToTarget]);

  const commentTarget = useCallback((target: ReviewTarget) => {
    const range = targetCommentRange(target);
    setActiveTargetId(target.target_id);
    if (range) {
      setDraft({
        path: target.path,
        ...range,
        targetUnitKey: target.unit_key,
      });
    } else {
      setDraft({
        path: target.path,
        startLine: 0,
        endLine: 0,
        side: "additions",
        fileLevel: true,
        targetUnitKey: target.unit_key,
      });
    }
  }, []);

  const createAnnotation = useCallback(async (input: AnnotationDraft) => {
    if (!reviewId || readOnly || busy) return;
    setBusy("comment");
    try {
      const result = await postAnnotation(reviewId, input);
      // The write has succeeded. Clear the composer before any secondary read
      // so a failed refetch can never leave the same text ready to submit twice.
      setDraft(null);
      if (result.target && result.progress) {
        setTargetList((current) => current ? {
          ...current,
          targets: current.targets.map((target) => target.unit_key === result.target!.unit_key ? result.target! : target),
          progress: result.progress!,
          outline: mergeOutline(current.outline, result.outline_updates),
        } : current);
      }
      try {
        const nextAnnotations = await fetchAnnotations(reviewId);
        setAnnotations(nextAnnotations.annotations);
        setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
        setFeedbackDelivery(nextAnnotations.delivery ?? null);
        setError("");
      } catch (reason) {
        setError(`Comment saved, but the discussion list is out of date: ${String(reason instanceof Error ? reason.message : reason)}`);
      }
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy]);

  const mergeProposal = useCallback((proposal: ReviewChangeProposal) => {
    setProposals((current) => {
      const exists = current.some((item) => item.id === proposal.id);
      return exists
        ? current.map((item) => item.id === proposal.id ? proposal : item)
        : [...current, proposal];
    });
  }, []);

  const beginChangeProposal = useCallback(async (selectionDraft: Draft, sourceAction: "suggest" | "edit" = "suggest") => {
    if (!reviewId || readOnly || busy || selectionDraft.fileLevel || selectionDraft.side !== "additions") return;
    setBusy("proposal-selection");
    try {
      const selection = await fetchProposalSelection(
        reviewId,
        selectionDraft.path,
        selectionDraft.startLine,
        selectionDraft.endLine,
        selectionDraft.side,
      );
      setProposalSelection(selection);
      setActiveProposal(null);
      setDraft({
        ...selectionDraft,
        mode: "proposal",
        sourceAction,
        replacementText: selection.text,
        proposalIntent: "",
        proposalId: undefined,
      });
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy]);

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
      try {
        const next = await fetchChangeProposals(reviewId);
        setProposals(next.proposals);
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

  const setAnnotationState = useCallback(async (id: string, state: AnnotationState) => {
    if (!reviewId || readOnly || busy) return;
    setBusy("comment");
    try {
      await patchAnnotation(id, { state });
      const nextAnnotations = await fetchAnnotations(reviewId);
      setAnnotations(nextAnnotations.annotations);
      setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
      setFeedbackDelivery(nextAnnotations.delivery ?? null);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy]);

  const openContext = useCallback((target: ReviewTarget, tab: ContextDrawerTab = "impact") => {
    contextReturnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
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

  const toggleContextPin = useCallback(() => {
    setContextPinned((current) => {
      const next = !current;
      window.sessionStorage.setItem(CONTEXT_PIN_KEY, next ? "1" : "0");
      return next;
    });
  }, []);

  const loadMore = useCallback(() => {
    if (!reviewId) return;
    const readyHidden = navigationPathOrder.filter(
      (path) => !revealedPathsRef.current.has(path) && Boolean(detailsRef.current[path] || fileErrors[path]),
    );
    const batch = pathsForChangeBudget(
      readyHidden,
      fileChangeLines,
      LOAD_MORE_CHANGE_BUDGET,
      LOAD_MORE_MAX_FILES,
    );
    if (batch.length === 0) return;
    // Pagination is now purely a visibility operation. If a path is not ready,
    // it is not eligible for this button and stays in background preparation.
    revealPaths(batch);
    queueNextPage(reviewId, navigationPathOrder, fileChangeLines);
  }, [reviewId, navigationPathOrder, fileChangeLines, fileErrors, queueNextPage, revealPaths]);
  const refreshReview = useCallback(async () => {
    if (!reviewEnvironment.review.workspace_refresh || !reviewId || readOnly || busy) return;
    setBusy("refresh");
    // Between committing the new overview and landing its dependent reads the
    // reader presents a revision it has not loaded. `busy` already blocks every
    // judgment for that window, so nothing is announced while the refresh is
    // still running — only a refresh that dies inside the window leaves the
    // reader stranded and has to say so.
    let stranded = false;
    try {
      const previousTarget = activeTarget ?? null;
      const nextOverview = await postRefresh(reviewId);
      // The server is on the new revision the moment postRefresh resolves, so
      // the new revision is committed here, ahead of the dependent reads that
      // can still fail.
      setOverview(nextOverview);
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
        fetchReviewEvidence(reviewId),
      ]);
      stableTargetRevisionRef.current = nextTargets.revision_id;
      stableTargetOrderRef.current = nextTargets.targets.map((target) => target.target_id);
      setTargetList(nextTargets);
      setAnnotations(nextAnnotations.annotations);
      setFeedbackStatus(nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations));
      setEvidence(nextEvidence.evidence);
      stranded = false;
      const nextInfo = nextOverview.refreshed ?? null;
      setRevisionInfo(nextInfo?.created ? nextInfo : null);
      const nextDelta = nextInfo?.target_delta;
      const focusDelta = Boolean(nextInfo?.created && nextDelta && nextDelta.active.length > 0);
      setDeltaFocus(focusDelta);
      const survivor = bestTargetAfterRevision(previousTarget, nextTargets.targets, nextDelta);
      setActiveTargetId(survivor?.target_id ?? "");
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
      if (survivor) window.requestAnimationFrame(() => streamRef.current?.scrollToTarget(survivor, "instant"));
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
      setBusy("");
    }
  }, [reviewEnvironment.review.workspace_refresh, reviewId, readOnly, busy, activeTarget, draft, draftPath, loadPaths, queueNextPage, revealPaths, targetList?.order]);

  const reloadReader = useCallback(async () => {
    if (!reviewId || busy) return;
    setBusy("reload");
    try {
      // Only a load that actually describes the current revision may hand the
      // reviewer their judgments back.
      if (await loadReader(reviewId)) setRevisionAdvanced(false);
    } finally {
      setBusy("");
    }
  }, [reviewId, busy, loadReader]);

  useEffect(() => {
    setOutcomeOverride(null);
  }, [overview?.revision.id]);

  const closeFinishSheet = useCallback(() => {
    setFinishSheetOpen(false);
    window.requestAnimationFrame(() => finishButtonRef.current?.focus());
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
        setError(String(reason instanceof Error ? reason.message : reason));
      } finally {
        setBusy("");
      }
      return;
    }
    setBusy("finish-preview");
    try {
      setFinishSummary(await fetchFinishReview(reviewId));
      setFinishSheetOpen(true);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, busy, revisionAdvanced, overview?.session.status, historical]);

  const confirmFinish = useCallback(async (outcome: ReviewOutcomeKind, summary: string) => {
    if (!reviewId || busy || revisionAdvanced || historical) return;
    setBusy("finish");
    try {
      const sameCurrentOutcome = currentOutcome
        && !currentOutcome.stale
        && currentOutcome.outcome === outcome
        && currentOutcome.summary === summary;
      if (!sameCurrentOutcome) {
        if (extensions?.recordOutcome) {
          const recorded = await extensions.recordOutcome(outcome, summary);
          setOutcomeOverride(recorded);
        } else {
          const recorded = await postReviewOutcome(reviewId, outcome, summary);
          setLocalOutcome(recorded.current);
          setOutcomeOverride(recorded.current);
        }
      }
      const next = await finishReview(reviewId, "finished");
      setFinishSummary(next);
      setFinishSheetOpen(false);
      setOverview((current) => current ? { ...current, session: { ...current.session, status: next.status } } : current);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, busy, revisionAdvanced, historical, currentOutcome, extensions]);

  const prepareFeedback = useCallback(async () => {
    if (!reviewId || historical) return;
    setBusy("feedback");
    try {
      const nextFeedback = await exportFeedback(reviewId);
      setFeedback(nextFeedback);
      if (nextFeedback.status) setFeedbackStatus(nextFeedback.status);
      setFeedbackDelivery(nextFeedback.delivery);
      setDelivery(null);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, historical]);

  const sendFeedbackToAgent = useCallback(async () => {
    if (!reviewId || !feedback || busy) return;
    setBusy("delivery");
    try {
      const nextDelivery = await deliverFeedback(reviewId, feedback);
      setDelivery(nextDelivery);

      if (nextDelivery.state === "sent" || nextDelivery.state === "queued") {
        const handedOffCount = nextDelivery.annotation_count;
        const optimisticStatus = (current: FeedbackStatus): FeedbackStatus => ({
          ...current,
          unpublished: Math.max(0, current.unpublished - handedOffCount),
          published: current.published + (nextDelivery.state === "sent" ? handedOffCount : 0),
          in_flight: current.in_flight + (nextDelivery.state === "queued" ? handedOffCount : 0),
        });
        setFeedbackStatus(optimisticStatus);
        setFeedback((current) => current
          ? { ...current, status: optimisticStatus(current.status ?? feedbackStatus) }
          : current);
      }

      try {
        const nextAnnotations = await fetchAnnotations(reviewId);
        const nextFeedbackStatus = nextAnnotations.feedback ?? fallbackFeedbackStatus(nextAnnotations.annotations);
        const nextFeedbackDelivery = nextAnnotations.delivery ?? feedback.delivery;
        setAnnotations(nextAnnotations.annotations);
        setFeedbackStatus(nextFeedbackStatus);
        setFeedbackDelivery(nextFeedbackDelivery);
        setFeedback((current) => current
          ? { ...current, status: nextFeedbackStatus, delivery: nextFeedbackDelivery }
          : current);
        setError("");
      } catch (reason) {
        setError(
          nextDelivery.state === "sent"
            ? `Feedback was sent, but the review state could not refresh: ${String(reason instanceof Error ? reason.message : reason)}`
            : nextDelivery.state === "queued"
              ? `Feedback was queued, but the review state could not refresh: ${String(reason instanceof Error ? reason.message : reason)}`
              : String(reason instanceof Error ? reason.message : reason),
        );
      }
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, feedback, feedbackStatus, busy]);

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
          await uploadReviewEvidence(reviewId, file, activeTarget.path);
        } catch (reason) {
          failures.push(`${file.name}: ${String(reason instanceof Error ? reason.message : reason)}`);
        }
      }
      setEvidence((await fetchReviewEvidence(reviewId)).evidence);
    } catch (reason) {
      failures.push(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
    // A clean upload has nothing to say about the rest of the reader, and the
    // banner it would clear is usually a message the reviewer is still acting
    // on, so only failures speak here.
    if (failures.length > 0) setError(failures.join(" · "));
  }, [reviewId, activeTarget, readOnly, busy]);

  const addPreview = useCallback(async (url: string, title: string) => {
    // The sibling of the drop zone in the same evidence tab, and a write like
    // it: it joins the shared busy channel on the same terms rather than
    // clobbering a mark or a bulk review still in flight.
    if (!reviewId || !activeTarget || readOnly || busy) return;
    setBusy("evidence");
    try {
      await linkReviewEvidence(reviewId, url, title, activeTarget.path);
      setEvidence((await fetchReviewEvidence(reviewId)).evidence);
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, activeTarget, readOnly, busy]);

  const openImpact = useCallback(async (site: import("./types").ImpactSite) => {
    if (!reviewId) return;
    const match = /^(.*):L(\d+)$/.exec(site.path);
    const path = match?.[1] ?? site.path;
    const line = Number(match?.[2] ?? 1);
    const inPatch = targets.find((target) => target.path === path);
    if (site.in_patch && inPatch) {
      await scrollToTarget(inPatch);
      setContextOpen(false);
      return;
    }
    try {
      setRelated(await (historical
        ? fetchHistoricalRelatedSource(reviewId, historicalRevisionNumber, path, line)
        : fetchRelatedSource(reviewId, path, line)));
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    }
  }, [reviewId, targets, scrollToTarget, historical, historicalRevisionNumber]);

  const openCommands = useCallback((mode: "actions" | "shortcuts") => {
    commandReturnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setCommandMode(mode);
  }, []);

  const closeCommands = useCallback(() => {
    setCommandMode(null);
    const restore = commandReturnFocusRef.current;
    window.requestAnimationFrame(() => {
      // A palette action can open another dialog. Restoring the pre-palette
      // opener then drops focus behind that dialog's overlay, where Tab walks
      // the page underneath and the dialog's own Escape handler never fires.
      // A modal that was already open before the palette is the exception: the
      // opener lives inside it, so focus belongs back there.
      const modal = document.querySelector('[aria-modal="true"]');
      if (modal && !(restore && modal.contains(restore))) return;
      if (restore && document.contains(restore)) restore.focus();
    });
  }, []);

  const openOverview = useCallback(() => {
    overviewReturnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setOverviewOpen(true);
  }, []);

  const closeOverview = useCallback(() => {
    setOverviewOpen(false);
    const restore = overviewReturnFocusRef.current;
    window.requestAnimationFrame(() => {
      if (restore && document.contains(restore)) restore.focus();
    });
  }, []);

  const executeSurface = useCallback(async (surface: ReviewSurfaceInfo, compare: boolean) => {
    if (!reviewId || !surface.runtime || surfaceBusy) return;
    const key = `${surface.provider}:${surface.id}`;
    setSurfaceBusy(key);
    setSurfaceRuns((current) => ({ ...current, [key]: { ...current[key], error: undefined } }));
    try {
      if (compare) {
        const [oldResponse, newResponse] = await Promise.all([
          runReviewSurface(reviewId, surface.provider, surface.id, "old"),
          runReviewSurface(reviewId, surface.provider, surface.id, "new"),
        ]);
        setSurfaceRuns((current) => ({
          ...current,
          [key]: { old: oldResponse.result, new: newResponse.result },
        }));
      } else {
        const response = await runReviewSurface(reviewId, surface.provider, surface.id, "new");
        setSurfaceRuns((current) => ({
          ...current,
          [key]: { ...current[key], new: response.result, error: undefined },
        }));
      }
    } catch (reason) {
      setSurfaceRuns((current) => ({
        ...current,
        [key]: {
          ...current[key],
          error: String(reason instanceof Error ? reason.message : reason),
        },
      }));
    } finally {
      setSurfaceBusy("");
    }
  }, [reviewId, surfaceBusy]);
  useEffect(() => {
    if (!reviewId || !firstReviewableReady || surfaceBusy || surfaces.length === 0) return;
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
  }, [executeSurface, firstReviewableReady, reviewId, surfaceBusy, surfaceRuns, surfaces, targets]);

  useEffect(() => {
    automaticSurfaceRunsRef.current.clear();
    setSurfaceRuns({});
  }, [overview?.revision.id]);

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

  const runPrimaryAction = useCallback(() => {
    if (primaryAction.disabled) return;
    switch (primaryAction.kind) {
      case "go_latest":
        window.location.href = reviewHref(reviewId);
        return;
      case "reopen":
      case "restore":
      case "finish":
        void openFinishSheet();
        return;
      case "reload_state":
        void reloadReader();
        return;
      case "send_feedback":
        void prepareFeedback();
        return;
      case "rereview_comments":
      case "review_comments":
        setCommentsOpen(true);
        return;
      case "review_changed":
      case "review_target":
      case "review_requested_changes": {
        const target = targets.find((item) => item.target_id === primaryAction.targetId);
        if (!target) return;
        setQuery("");
        setDeltaFocus(false);
        void scrollToTarget(target, "instant");
        return;
      }
      case "review_readiness":
        setReadinessOpen(true);
        return;
      case "checking_comments":
      case "delivery_pending":
      case "waiting_agent":
        return;
    }
  }, [
    openFinishSheet,
    prepareFeedback,
    primaryAction,
    refreshReview,
    reloadReader,
    reviewId,
    scrollToTarget,
    targets,
  ]);

  const reviewActions = useMemo<ReviewPaletteAction[]>(() => [
    {
      id: "search",
      label: "Search review",
      detail: "Find targets by path, symbol, state, reason, or loaded comment text",
      shortcut: "/",
      run: () => {
        setFocusMode(false);
        setOutlineCollapsed(false);
        window.requestAnimationFrame(() => searchInputRef.current?.focus());
      },
    },
    {
      id: "reviewed",
      label: "Mark current target reviewed and advance",
      shortcut: "r",
      disabled: !activeTarget || Boolean(readOnly) || Boolean(busy),
      run: () => { if (activeTarget) void markTarget(activeTarget, "reviewed", true); },
    },
    {
      id: "reopen",
      label: "Reopen current target",
      shortcut: "u",
      disabled: !activeTarget || Boolean(readOnly) || Boolean(busy),
      run: () => { if (activeTarget) void markTarget(activeTarget, "unreviewed"); },
    },
    {
      id: "needs-changes",
      label: "Mark current target needs changes",
      shortcut: "x",
      disabled: !activeTarget || Boolean(readOnly) || Boolean(busy),
      run: () => { if (activeTarget) void markTarget(activeTarget, "needs_changes"); },
    },
    {
      id: "comment",
      label: "Comment on current target",
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
      id: "overview",
      label: "Open change overview",
      detail: "Major changes, target counts, verification, provenance, and evidence",
      run: openOverview,
    },
    ...(reviewEnvironment.review.workspace_refresh ? [{
      id: "refresh",
      label: "Refresh local revision",
      detail: "Capture the latest local source snapshot and reconcile prior judgments",
      disabled: Boolean(readOnly) || Boolean(busy),
      run: () => void refreshReview(),
    } satisfies ReviewPaletteAction] : []),
    {
      id: "next-action",
      label: primaryAction.label,
      detail: primaryAction.detail,
      disabled: Boolean(busy) || primaryAction.disabled,
      run: runPrimaryAction,
    },
    ...(overview?.session.status === "open"
      && primaryAction.kind !== "finish"
      && !historical
      && !revisionAdvanced
      ? [{
          id: "finish",
          label: "Finish review…",
          detail: "Explicitly inspect blockers and record a verdict before finishing early",
          disabled: Boolean(busy),
          run: () => void openFinishSheet(),
        } satisfies ReviewPaletteAction]
      : []),
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
  ], [activeTarget, readOnly, revisionAdvanced, busy, contextOpen, contextTab, focusMode, diffStyle, diffOverflow, overview?.session.status, targetList?.order, historical, primaryAction, runPrimaryAction, markTarget, commentTarget, closeContext, openContext, openOverview, refreshReview, openFinishSheet, changeOrder, changeDiffStyle, toggleDiffOverflow, reviewEnvironment.review.workspace_refresh]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (commandMode || overviewOpen || commentsOpen || historyOpen || finishSheetOpen) {
        // A dialog whose own Escape handler is out of reach — because focus
        // escaped its overlay — would otherwise be closable only with the
        // mouse, so the window handler closes the topmost dialog instead of
        // bailing out into a dead end.
        if (event.key !== "Escape") return;
        if (commandMode) closeCommands();
        else if (overviewOpen) closeOverview();
        else if (commentsOpen) setCommentsOpen(false);
        else if (historyOpen) setHistoryOpen(false);
        else closeFinishSheet();
        event.preventDefault();
        return;
      }
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
          next = stepAttentionTarget(visibleTargets, activeTarget.target_id, 1);
          break;
        case "[":
          next = stepAttentionTarget(visibleTargets, activeTarget.target_id, -1);
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
      if (next && next.target_id !== activeTarget.target_id) void scrollToTarget(next);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeTarget, visibleTargets, markTarget, commentTarget, scrollToTarget, contextOpen, contextTab, openContext, closeContext, finishSheetOpen, closeFinishSheet, query, commandMode, closeCommands, overviewOpen, closeOverview, commentsOpen, historyOpen, openCommands, diffStyle, changeDiffStyle, toggleDiffOverflow]);

  if (!authed) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-neutral-950 px-6 text-neutral-300">
        <div className="max-w-md">
          <div className="text-[13px] font-semibold text-neutral-100">This review link is incomplete</div>
          <p className="mt-1 text-[10px] leading-5 text-neutral-500">For a local review, reopen it from the terminal with <code className="text-neutral-300">lc review --open</code>.</p>
        </div>
      </div>
    );
  }
  if (error && !overview) {
    return (
      <div className="flex h-screen items-center justify-center bg-neutral-950 px-6 text-neutral-300">
        <div className="w-full max-w-md">
          <div className="text-[13px] font-semibold text-neutral-100">Review couldn't load</div>
          <div className="mt-1 text-[10px] leading-5 text-neutral-500">The review is still available; the Reader could not fetch its current state.</div>
          <details className="mt-3 text-[10px] text-neutral-600">
            <summary className="cursor-pointer hover:text-neutral-50">Error details</summary>
            <div className="mt-1 whitespace-pre-wrap font-mono text-rose-300/80">{error}</div>
          </details>
          <button type="button" disabled={Boolean(busy)} onClick={() => void reloadReader()} className="review-toolbar-button mt-4">
            {busy === "reload" ? "Retrying…" : "Retry"}
          </button>
        </div>
      </div>
    );
  }
  if (!startupReady || !overview || !targetList || !firstReviewableReady) {
    const structureReady = Boolean(overview && targetList);
    const preparingSource = startupReady && structureReady;
    const readyInitial = initialSourcePaths.filter((path) => Boolean(details[path] || fileErrors[path])).length;
    const totalInitial = initialSourcePaths.length;
    const label = !startupReady
      ? preparationLabel(preparationStage)
      : preparingSource
        ? "Preparing first visual diff"
        : "Reading review structure";
    const detail = preparingSource && totalInitial > 0
      ? `${readyInitial} of ${totalInitial} priority files prepared. Review opens on the first usable diff.`
      : !startupReady
        ? "Freezing the revision first so everything you review stays pinned to the same source."
        : "Building the review outline and selecting the first changed source.";
    return (
      <div
        data-testid="review-loading-screen"
        className="fixed inset-0 z-50 flex h-dvh w-screen items-center justify-center overflow-hidden overscroll-none bg-neutral-950 px-5 text-neutral-300"
      >
        <div className="w-full max-w-[430px] rounded-lg border border-neutral-800/80 bg-neutral-900/40 p-5 shadow-2xl shadow-black/30">
          <div className="flex items-center justify-between gap-4">
            <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-neutral-500">LemonCrow Review</div>
            <div className="flex items-center gap-1.5 text-[10px] text-neutral-600" role="status" aria-live="polite">
              <span className="h-1.5 w-1.5 rounded-full bg-neutral-500" aria-hidden="true" />
              Preparing
            </div>
          </div>
          <div className="mt-4 text-[14px] font-semibold text-neutral-100">{label}</div>
          <div className="mt-1.5 min-h-8 text-[10px] leading-4 text-neutral-500">{detail}</div>
          <div className="mt-4 grid grid-cols-3 gap-2 border-t border-neutral-800/80 pt-3 text-[9px]">
            <div>
              <div className="font-medium text-neutral-400">Revision</div>
              <div className={startupReady ? "mt-0.5 text-neutral-500" : "mt-0.5 text-neutral-300"}>
                {startupReady ? "Ready" : "Capturing"}
              </div>
            </div>
            <div>
              <div className="font-medium text-neutral-400">Structure</div>
              <div className={structureReady ? "mt-0.5 text-neutral-500" : startupReady ? "mt-0.5 text-neutral-300" : "mt-0.5 text-neutral-700"}>
                {structureReady ? "Ready" : startupReady ? "Reading" : "Waiting"}
              </div>
            </div>
            <div>
              <div className="font-medium text-neutral-400">First diff</div>
              <div className={readyInitial > 0 ? "mt-0.5 text-neutral-500" : structureReady ? "mt-0.5 text-neutral-300" : "mt-0.5 text-neutral-700"}>
                {readyInitial > 0 ? "Ready" : structureReady ? "Preparing" : "Waiting"}
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const streamPaths = visiblePathOrder;
  const hiddenStreamPaths = streamPaths.filter((path) => !revealedPaths.has(path));
  const remainingFileCount = hiddenStreamPaths.length;
  const pendingVisibleFileCount = streamPaths.filter(
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
  const preparingHiddenFileCount = nextPreparationPaths.filter(
    (path) => !details[path] && !fileErrors[path],
  ).length;
  const streamDetails = Object.fromEntries(
    Object.entries(details).filter(([path]) => revealedPaths.has(path)),
  ) as Record<string, FileDetail | undefined>;
  const streamErrors = Object.fromEntries(
    Object.entries(fileErrors).filter(([path]) => revealedPaths.has(path)),
  ) as Record<string, string | undefined>;
  const addressedNeedsRereview = targets.reduce(
    (sum, target) => sum + target.annotation_counts.addressed_needs_rereview,
    0,
  );
  const storySteps = reviewStorySteps(overview);
  const attentionPath = reviewAttentionPath(visibleTargets);
  const activeAttentionIndex = activeTarget
    ? attentionPath.findIndex((item) => item.targetId === activeTarget.target_id)
    : -1;
  const attentionPosition = attentionPath.length === 0 ? 0 : Math.max(0, activeAttentionIndex) + 1;
  const nextAttention = attentionPath[activeAttentionIndex >= 0 ? activeAttentionIndex : 0] ?? null;
  const canDirectSend = !readOnly && Boolean(feedback?.delivery?.supported);
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
  const drawer = activeTarget ? (
    <Suspense fallback={<div className="p-3 text-[10px] text-neutral-600">Loading context…</div>}>
      <ContextDrawer
        reviewId={reviewId}
        target={activeTarget}
        detail={activeDetail}
        degraded={overview.degraded}
        annotations={annotations}
        evidence={evidence}
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
      />
    </Suspense>
  ) : null;

  return (
    <div className="review-app relative flex h-dvh min-h-0 flex-col bg-neutral-950 text-neutral-200" data-testid="review-reader">
      {historical && (
        <div
          className="flex min-h-8 shrink-0 items-center gap-2 border-b border-amber-700/50 bg-amber-950/25 px-3 text-[10px] text-amber-200"
          role="status"
          aria-live="polite"
          data-testid="historical-revision-banner"
        >
          <span className="font-semibold uppercase tracking-[0.12em] text-amber-300">Historical</span>
          <span className="text-amber-100">rev {overview.revision.revision_number}</span>
          <span className="text-amber-500/70">·</span>
          <span className="text-amber-300/70">latest rev {overview.latest_revision_number ?? overview.revision.revision_number}</span>
          <span className="text-amber-500/70">·</span>
          <span className="text-amber-300/70">read-only</span>
          <span className="flex-1" />
          {overview.latest_revision && overview.latest_revision.id !== overview.revision.id && (
            <a
              href={sourceCompareHref(
                overview.revision.ref || `rr/${overview.revision.id}`,
                overview.latest_revision.ref || `rr/${overview.latest_revision.id}`,
                overview.session.ref || `r/${overview.session.id}`,
              )}
              className="review-compact-action h-6 border-amber-800/60 px-2 text-[10px] text-amber-200 hover:border-amber-600 hover:text-amber-50"
            >
              Compare with latest
            </a>
          )}
          <button
            type="button"
            onClick={() => { window.location.href = reviewHref(reviewId); }}
            className="inline-flex h-6 items-center rounded-md border border-amber-600/70 bg-amber-800/45 px-2.5 text-[10px] font-semibold text-amber-50 transition-colors hover:border-amber-500 hover:bg-amber-700/55"
          >
            Back to latest · rev {overview.latest_revision_number ?? overview.revision.revision_number}
          </button>
        </div>
      )}
      <ReaderHeader
        overview={overview}
        progress={progress}
        diffStyle={diffStyle}
        diffOverflow={diffOverflow}
        order={targetList.order}
        focusMode={focusMode}
        contextOpen={contextOpen}
        busy={Boolean(busy)}
        busyLabel={busy === "refresh" ? "Updating review…" : undefined}
        readOnly={Boolean(readOnly)}
        revisionAdvanced={revisionAdvanced}
        finishButtonRef={finishButtonRef}
        codeTheme={codeTheme}
        commentCount={rootCommentCount}
        openCommentCount={openRootCommentCount}
        commentState={commentsState}
        primaryAction={primaryAction}
        authorshipOverride={extensions?.authorship}
        contextOverride={extensions?.context}
        environment={reviewEnvironment}
        allowRefresh={reviewEnvironment.review.workspace_refresh}
        extensionActions={extensions?.renderHeaderActions?.({ reviewId, overview, annotations, readOnly: Boolean(readOnly), historical })}
        historical={historical}
        onCodeThemeChange={changeCodeTheme}
        onShowDirectory={() => { window.location.href = reviewDirectoryHref(); }}
        onShowComments={() => setCommentsOpen(true)}
        onShowHistory={() => setHistoryOpen(true)}
        onPrimaryAction={runPrimaryAction}
        onDownloadPatch={() => void downloadPatch()}
        onFinish={() => void openFinishSheet()}
        onRefresh={() => void refreshReview()}
        onOrderChange={(nextOrder) => void changeOrder(nextOrder)}
        onDiffStyleChange={changeDiffStyle}
        onToggleDiffOverflow={toggleDiffOverflow}
        onToggleContext={() => contextOpen ? closeContext() : activeTarget && openContext(activeTarget, contextTab)}
        onToggleFocus={() => setFocusMode((current) => {
          const next = !current;
          setOutlineCollapsed(next);
          if (next) closeContext();
          return next;
        })}
        onShowShortcuts={() => openCommands("shortcuts")}
        onShowOverview={openOverview}
      />
      {!historical && (
        <ReviewAttentionBar
          revisionNumber={overview.revision.revision_number}
          progress={progress}
          openCommentCount={openRootCommentCount}
          commentsState={commentsState}
          feedbackStatus={feedbackStatus}
          feedbackDelivery={feedbackDelivery}
          addressedNeedsRereview={addressedNeedsRereview}
          historical={historical}
          delivery={delivery}
          readiness={readiness}
          onShowComments={() => setCommentsOpen(true)}
          onShowReadiness={() => setReadinessOpen(true)}
        />
      )}
      {readinessOpen && (
        <ReviewReadinessSheet
          model={readiness}
          extraSections={extensions?.readinessSections}
          onClose={() => setReadinessOpen(false)}
        />
      )}
      {preparationStage && startupReady && (
        <div className="flex shrink-0 items-center gap-2 border-b border-sky-950/70 bg-sky-950/10 px-3 py-1 text-[10px] text-sky-300" role="status" aria-live="polite">
          <span>{preparationLabel(preparationStage)} · source is ready to review</span>
        </div>
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
      {overviewOpen && (
        <Suspense fallback={null}>
          <ReviewOverviewSheet
            overview={overview}
            surfaces={surfaces}
            onClose={closeOverview}
            onSelectPath={selectPath}
            onSelectSurface={(surfaceKey) => void selectSurface(surfaceKey)}
          />
        </Suspense>
      )}
      {commentsOpen && (
        <Suspense fallback={null}>
          <ReviewCommentsSheet
            annotations={annotations}
            feedbackStatus={feedbackStatus}
            feedbackDelivery={feedbackDelivery}
            onClose={() => setCommentsOpen(false)}
            onJump={(annotation) => {
              const target = targetForAnnotation(targets, annotation);
              setCommentsOpen(false);
              if (target) {
                void scrollToTarget(target);
              } else if (annotation.path) {
                void loadPath(reviewId, annotation.path).then(() => {
                  window.requestAnimationFrame(() => streamRef.current?.scrollToFile(annotation.path));
                });
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
            initialTab="revisions"
            onClose={() => setHistoryOpen(false)}
          />
        </Suspense>
      )}
      {extensions?.renderOverlays?.({ reviewId, overview, annotations, readOnly: Boolean(readOnly), historical })}

      {revisionAdvanced && (
        <div className="flex shrink-0 items-center gap-2 border-b border-rose-900/60 bg-rose-950/25 px-3 py-1.5 text-[10px] text-rose-200" role="alert">
          <span className="font-medium">Review updated elsewhere</span>
          <span className="min-w-0 flex-1 truncate text-rose-300/70">The newest revision did not finish loading. Use Reload review above before making another judgment.</span>
        </div>
      )}
      {sourceState && (
        <div className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-sky-900/60 bg-sky-950/20 px-3 py-1.5 text-[10px] text-sky-200" role="status" aria-live="polite">
          <span className="font-medium">New revision available</span>
          <span className="min-w-0 flex-1 truncate text-sky-400/70">
            {awaitingCaptureCount > 0
              ? `${awaitingCaptureCount} reviewer edit${awaitingCaptureCount === 1 ? "" : "s"} applied · ${sourceState.path_count} file${sourceState.path_count === 1 ? "" : "s"} changed. You can keep reviewing rev ${overview.revision.revision_number} or capture the newer source when ready.`
              : `${sourceState.path_count} file${sourceState.path_count === 1 ? "" : "s"} changed after this frozen revision. You can keep reviewing rev ${overview.revision.revision_number}.`}
          </span>
          <button
            type="button"
            disabled={Boolean(busy)}
            onClick={() => void refreshReview()}
            className="inline-flex h-7 items-center rounded-md border border-sky-700/70 bg-sky-900/35 px-2.5 font-medium text-sky-100 transition-colors hover:border-sky-600 hover:bg-sky-800/45 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy === "refresh" ? "Capturing…" : "Capture new revision"}
          </button>
          <a
            href={sourceCompareHref(
              overview.revision.ref || `rr/${overview.revision.id}`,
              "worktree",
              overview.session.ref || `r/${overview.session.id}`,
            )}
            className="review-compact-action inline-flex h-7 items-center border-sky-900/50 px-2 text-sky-300/80 hover:text-sky-100"
          >
            Compare source
          </a>
          <button
            type="button"
            onClick={() => setHistoryOpen(true)}
            className="review-compact-action h-7 border-sky-900/50 text-sky-300/70 hover:text-sky-100"
          >
            Revisions
          </button>
          {awaitingCaptureCount === 0 && (
            <button
              type="button"
              onClick={() => {
                setDismissedSourceFingerprint(sourceState.fingerprint);
                setSourceState(null);
              }}
              className="px-1 text-sky-400/60 hover:text-sky-200"
            >
              Hide
            </button>
          )}
        </div>
      )}
      {draft?.recovered && (
        <div className="flex shrink-0 items-center gap-2 border-b border-amber-900/60 bg-amber-950/20 px-3 py-2 text-[10px] text-amber-200" role="status">
          <span className="font-medium">Draft preserved</span>
          <span className="min-w-0 flex-1 truncate text-amber-300/70">{draft.body?.trim() || "Unsaved comment"} · select a new line to reattach</span>
          <button type="button" onClick={() => setDraft(null)} className="text-amber-400/70 hover:text-amber-200">Discard draft</button>
        </div>
      )}
      {!historical && activeProposals.length > 0 && (
        <div className={`shrink-0 border-b px-3 py-1.5 text-[10px] ${proposalBarTone}`} data-testid="review-proposals">
          <div className="flex items-center gap-2">
            <span className="font-medium text-neutral-200">Source edits</span>
            {readyProposalCount > 0 && <span className="text-violet-300/75">{readyProposalCount} ready</span>}
            {savedProposalCount > 0 && <span className="text-neutral-500">{savedProposalCount} saved</span>}
            {awaitingCaptureCount > 0 && <span className="text-sky-300/75">{awaitingCaptureCount} awaiting update</span>}
            {staleProposalCount > 0 && <span className="text-amber-300/80">{staleProposalCount} from older revision</span>}
            {conflictedProposalCount > 0 && <span className="text-amber-300/80">{conflictedProposalCount} need attention</span>}
            <span className="flex-1" />
            <details data-review-dropdown className="relative">
              <summary className="cursor-pointer list-none text-neutral-500 hover:text-neutral-200">Details</summary>
              <div className="absolute right-0 top-5 z-[70] w-[480px] max-w-[88vw] border border-neutral-700 bg-neutral-950 p-1.5 shadow-2xl">
                {activeProposals.map((proposal) => {
                  const status = proposal.state === "conflicted"
                    ? { label: "Source moved", tone: "text-amber-300" }
                    : proposal.state === "applied"
                      ? { label: "Awaiting review update", tone: "text-sky-300" }
                      : proposal.base_revision_id !== overview.revision.id
                        ? { label: "Review advanced", tone: "text-amber-300" }
                        : proposal.can_apply
                          ? { label: "Ready to apply", tone: "text-violet-300" }
                          : { label: "Saved proposal", tone: "text-neutral-500" };
                  return (
                    <div key={proposal.id} className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-x-2 border-b border-neutral-900 px-2 py-2 last:border-b-0">
                      <div className="min-w-0">
                        <div className="truncate font-mono text-neutral-300" title={proposal.path}>
                          {proposal.path}:{proposal.start_line}{proposal.end_line !== proposal.start_line ? `-${proposal.end_line}` : ""}
                        </div>
                        {proposal.intent && <div className="mt-0.5 truncate text-neutral-600" title={proposal.intent}>{proposal.intent}</div>}
                      </div>
                      <span className={status.tone}>{status.label}</span>
                      {(proposal.state === "proposed" || proposal.state === "conflicted") ? (
                        <button
                          type="button"
                          className="review-compact-action h-6 px-1.5 text-[10px]"
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
                        </button>
                      ) : (
                        <span className="w-8" aria-hidden="true" />
                      )}
                    </div>
                  );
                })}
              </div>
            </details>
          </div>
        </div>
      )}
      <RevisionDeltaBar
        info={revisionInfo}
        discarded={overview.discarded}
        focused={deltaFocus}
        remainingActive={deltaRemainingCount}
        onShowAll={() => setDeltaFocus(false)}
        onSelectTarget={(targetId) => {
          const target = targets.find((item) => item.target_id === targetId);
          if (target) void scrollToTarget(target);
        }}
        onDismiss={() => {
          setRevisionInfo(null);
          setDeltaFocus(false);
        }}
      />
      {(error || Object.values(contextFailures).some(Boolean)) && overview && (
        <details className="shrink-0 border-b border-amber-900/50 bg-amber-950/15 px-3 py-1.5 text-[10px] text-amber-300">
          <summary className="cursor-pointer">Some review context is unavailable</summary>
          <div className="mt-1.5 space-y-1 break-words pb-1 text-neutral-500">
            {error && <div>{error}</div>}
            {Object.values(contextFailures).filter(Boolean).map((message) => <div key={message}>{message}</div>)}
          </div>
        </details>
      )}
      {feedback && (
        <ReviewFeedbackPanel
          feedback={feedback}
          delivery={delivery}
          busy={Boolean(busy)}
          canDirectSend={canDirectSend}
          onCopy={async () => {
            if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
            await navigator.clipboard.writeText(feedback.markdown);
          }}
          onSend={() => void sendFeedbackToAgent()}
          onClose={() => { setFeedback(null); setDelivery(null); }}
        />
      )}

      <div className="relative flex min-h-0 flex-1">
        <ReviewOutline
          rows={visibleOutline}
          activePath={activeTarget?.path ?? ""}
          query={query}
          searchInputRef={searchInputRef}
          matchTargetCount={searchMatches.targetCount}
          matchFileCount={searchMatches.fileCount}
          matchCountsByPath={searchMatches.byPath}
          storySteps={storySteps}
          storyMore={overview.change_story_more ?? 0}
          nextAttention={nextAttention}
          attentionCount={attentionPath.length}
          attentionPosition={attentionPosition}
          onQuery={setQuery}
          onNavigateMatch={navigateSearchMatch}
          onDismissSearch={dismissSearch}
          onSelectPath={selectPath}
          onSelectTarget={(targetId) => {
            const target = visibleTargets.find((item) => item.target_id === targetId)
              ?? targets.find((item) => item.target_id === targetId);
            if (target) void scrollToTarget(target);
          }}
          onNavigateAttention={(delta) => {
            const target = stepAttentionTarget(visibleTargets, activeTarget?.target_id ?? "", delta);
            if (target) void scrollToTarget(target, "instant");
          }}
          onShowOverview={openOverview}
          collapsed={outlineCollapsed}
          onToggleCollapsed={() => setOutlineCollapsed((current) => !current)}
        />

        <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
          {related && (
            <div className="absolute inset-4 z-40 flex flex-col border border-neutral-700 bg-neutral-950 shadow-2xl">
              <div className="flex items-center gap-2 border-b border-neutral-800 px-3 py-2 text-[10px]">
                <span className="font-mono text-neutral-300">{related.path}</span>
                <span className="text-neutral-600">reviewed revision</span>
                <span className="flex-1" />
                <button type="button" onClick={() => setRelated(null)} className="text-neutral-400">Close</button>
              </div>
              <pre className="min-h-0 flex-1 overflow-auto p-3 font-mono text-[11px] leading-5 text-neutral-300">{related.text}</pre>
            </div>
          )}
          {visibleTargets.length === 0 && !dedicatedItemAvailable ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 text-[11px] text-neutral-600">
              <span>{dedicatedItem
                ? "Not available in this revision."
                : deltaFocus
                  ? "All changed work has been reviewed."
                  : "No matches."}</span>
              {deltaFocus && <button type="button" onClick={() => setDeltaFocus(false)} className="review-toolbar-button h-7">Show all</button>}
              {!deltaFocus && query.trim() && <button type="button" onClick={dismissSearch} className="review-toolbar-button h-7">Clear search</button>}
            </div>
          ) : (
            <ReviewStream
              ref={streamRef}
              targets={visibleTargets}
              details={streamDetails}
              surfaces={streamSurfaces}
              dedicatedItem={dedicatedItem}
              surfaceRuns={surfaceRuns}
              surfaceBusy={surfaceBusy}
              errors={streamErrors}
              annotations={annotations}
              draft={draft}
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
              remainingFileCount={remainingFileCount}
              pendingVisibleFileCount={pendingVisibleFileCount}
              readyHiddenFileCount={pendingVisibleFileCount === 0 ? readyHiddenBatch.length : 0}
              preparingHiddenFileCount={preparingHiddenFileCount}
              outstandingByPath={outstandingByPath}
              onActiveTarget={setActiveTargetId}
              onDraft={setDraft}
              onSuggestEdit={(selectionDraft) => void beginChangeProposal(selectionDraft, "suggest")}
              onEditSource={(selectionDraft) => void beginChangeProposal(selectionDraft, "edit")}
              onCreateProposal={() => void createProposal()}
              onCreateAndApplyProposal={() => void createAndApplyProposal()}
              onApplyProposal={() => void applyProposal()}
              onCloseProposal={closeProposalComposer}
              onCreate={(input) => void createAnnotation(input)}
              onSetAnnotationState={(id, state) => void setAnnotationState(id, state)}
              onMark={(target, state, advance) => void markTarget(target, state, advance)}
              onComment={commentTarget}
              onContext={openContext}
              onBulkReview={(path) => void bulkReviewFile(path)}
              onLoadMore={loadMore}
              onRunSurface={(surface, compare) => void executeSurface(surface, compare)}
            />
          )}
        </main>

        {contextOpen && drawer && (
          <div className={contextPinned
            ? "absolute inset-y-0 right-0 z-50 w-[min(420px,calc(100%-40px))] border-l border-neutral-700 bg-neutral-950 shadow-[-18px_0_40px_rgba(0,0,0,0.35)] xl:static xl:z-auto xl:w-[400px] xl:shrink-0 xl:border-neutral-800 xl:shadow-none"
            : "absolute inset-y-0 right-0 z-50 w-[min(420px,calc(100%-40px))] border-l border-neutral-700 bg-neutral-950 shadow-[-18px_0_40px_rgba(0,0,0,0.35)]"}>
            {drawer}
          </div>
        )}
      </div>

      {finishSheetOpen && finishSummary && (
        <Suspense fallback={<div className="absolute inset-0 z-[70] flex items-center justify-center bg-black/45 text-[11px] text-neutral-400">Loading finish summary…</div>}>
          <FinishSheet
            summary={finishSummary}
            degraded={overview.degraded}
            evidenceFacts={readiness.evidenceFacts}
            busy={busy === "finish"}
            currentOutcome={currentOutcome}
            onClose={closeFinishSheet}
            onFinish={(outcome, summary) => void confirmFinish(outcome, summary)}
          />
        </Suspense>
      )}

      <footer className="flex shrink-0 items-center gap-3 border-t border-neutral-900 px-3 py-1 text-[10px] text-neutral-600">
        <span className="hidden xl:inline">j/k target · J/K file · r reviewed · x changes · c comment · e context · f focus · / find · Enter next match</span>
        <span className="flex-1" />
        {overview.degraded.length > 0 && <span className="text-amber-400">{overview.degraded.length} uncertain signal{overview.degraded.length === 1 ? "" : "s"}</span>}
        <span>{progress.reviewed}/{progress.target_count}</span>
      </footer>
    </div>
  );
}
