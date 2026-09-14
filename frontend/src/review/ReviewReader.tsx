import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ContextDrawerTab } from "./ContextDrawer";
import ReaderHeader from "./ReaderHeader";
import type { ReviewPaletteAction } from "./ReviewCommandPalette";
import RevisionDeltaBar from "./RevisionDeltaBar";
import ReviewOutline from "./ReviewOutline";
import ReviewStream, { type ReviewStreamHandle } from "./ReviewStream";
import type { DiffStyle } from "./diffModel";
import type { Draft } from "./annotationModel";
import {
  bestTargetAfterRevision,
  firstActionable,
  filterTargets,
  type ReaderOrder,
  orderedPaths,
  progressFromTargets,
  stepFile,
  stepTarget,
  targetScrollLocation,
} from "./readerModel";
import {
  adoptBootstrapFragment,
  deliverFeedbackToClaude,
  exportFeedback,
  fetchAnnotations,
  fetchFile,
  fetchFinishReview,
  fetchOverview,
  fetchRelatedSource,
  fetchReviewEvidence,
  fetchSourceState,
  fetchTargets,
  finishReview,
  linkReviewEvidence,
  patchAnnotation,
  postAnnotation,
  postBulkReviewed,
  postMark,
  postRefresh,
  uploadReviewEvidence,
} from "./reviewApi";
import type {
  Annotation,
  AnnotationDraft,
  AnnotationState,
  FeedbackDelivery,
  FeedbackExport,
  FileDetail,
  MarkState,
  RefreshInfo,
  RelatedSource,
  ReviewClosure,
  ReviewEvidence,
  ReviewOutlineItem,
  ReviewOverview,
  ReviewTarget,
  ReviewTargetList,
  SourceState,
} from "./types";

const ContextDrawer = lazy(() => import("./ContextDrawer"));
const FinishSheet = lazy(() => import("./FinishSheet"));
const ReviewCommandPalette = lazy(() => import("./ReviewCommandPalette"));
const ReviewOverviewSheet = lazy(() => import("./ReviewOverviewSheet"));

const INITIAL_FILE_BATCH = 5;
const LOAD_MORE_BATCH = 5;
const PREFETCH_AHEAD_FILES = 2;
const COMPLETE_SMALL_REVIEW_TARGETS = 30;
const CONTEXT_PIN_KEY = "lemoncrow.review.reader.contextPinned";

const SOURCE_PROBE_INTERVAL_MS = 2000;

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

function initialPaths(targets: ReviewTarget[]): string[] {
  const paths = orderedPaths(targets);
  return targets.length <= COMPLETE_SMALL_REVIEW_TARGETS ? paths : paths.slice(0, INITIAL_FILE_BATCH);
}

function targetForPath(targets: ReviewTarget[], path: string): ReviewTarget | null {
  const rows = targets.filter((target) => target.path === path);
  return rows.find((target) => target.state !== "reviewed") ?? rows[0] ?? null;
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

interface ReviewReaderProps {
  /**
   * Working-tree probe cadence. Production mounts the reader with no props and
   * gets the shipped cadence; only a test passes this, so the knob stays a
   * per-instance argument instead of a module global any importer could retune.
   */
  sourceProbeIntervalMs?: number;
}

export default function ReviewReader({ sourceProbeIntervalMs = SOURCE_PROBE_INTERVAL_MS }: ReviewReaderProps = {}) {
  const [reviewId, setReviewId] = useState("");
  const [authed, setAuthed] = useState(true);
  const [overview, setOverview] = useState<ReviewOverview | null>(null);
  const [targetList, setTargetList] = useState<ReviewTargetList | null>(null);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [evidence, setEvidence] = useState<ReviewEvidence[]>([]);
  const [details, setDetails] = useState<Record<string, FileDetail | undefined>>({});
  const [fileErrors, setFileErrors] = useState<Record<string, string | undefined>>({});
  const [activeTargetId, setActiveTargetId] = useState("");
  const [query, setQuery] = useState("");
  const [outlineCollapsed, setOutlineCollapsed] = useState(false);
  const [focusMode, setFocusMode] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [contextTab, setContextTab] = useState<ContextDrawerTab>("impact");
  const [contextPinned, setContextPinned] = useState(() =>
    typeof window !== "undefined" && window.sessionStorage.getItem(CONTEXT_PIN_KEY) === "1",
  );
  const [diffStyle, setDiffStyle] = useState<DiffStyle>("split");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState<FeedbackExport | null>(null);
  const [delivery, setDelivery] = useState<FeedbackDelivery | null>(null);
  const [finishSummary, setFinishSummary] = useState<ReviewClosure | null>(null);
  const [finishSheetOpen, setFinishSheetOpen] = useState(false);
  const [sourceState, setSourceState] = useState<SourceState | null>(null);
  const [dismissedSourceFingerprint, setDismissedSourceFingerprint] = useState("");
  const [revisionInfo, setRevisionInfo] = useState<RefreshInfo | null>(null);
  const [deltaFocus, setDeltaFocus] = useState(false);
  const [related, setRelated] = useState<RelatedSource | null>(null);
  const [commandMode, setCommandMode] = useState<"actions" | "shortcuts" | null>(null);
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [revisionAdvanced, setRevisionAdvanced] = useState(false);
  const streamRef = useRef<ReviewStreamHandle>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const finishButtonRef = useRef<HTMLButtonElement>(null);
  const contextReturnFocusRef = useRef<HTMLElement | null>(null);
  const commandReturnFocusRef = useRef<HTMLElement | null>(null);
  const overviewReturnFocusRef = useRef<HTMLElement | null>(null);
  const inFlightPaths = useRef(new Map<string, Promise<FileDetail | null>>());
  const detailsRef = useRef<Record<string, FileDetail | undefined>>({});
  const loadGenerationRef = useRef(0);
  const sourceProbeRef = useRef({ fingerprint: "", count: 0 });

  useEffect(() => {
    const bootstrap = adoptBootstrapFragment();
    setAuthed(Boolean(bootstrap.token));
    setReviewId(bootstrap.reviewId);
  }, []);

  const loadPath = useCallback(async (id: string, path: string): Promise<FileDetail | null> => {
    if (!path) return null;
    if (detailsRef.current[path]) return detailsRef.current[path] ?? null;
    const existing = inFlightPaths.current.get(path);
    if (existing) return existing;
    const generation = loadGenerationRef.current;
    let request: Promise<FileDetail | null>;
    request = fetchFile(id, path)
      .then((detail) => {
        if (loadGenerationRef.current !== generation) return null;
        setDetails((current) => {
          const next = { ...current, [path]: detail };
          detailsRef.current = next;
          return next;
        });
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
  }, []);

  const loadPaths = useCallback(async (id: string, paths: string[]) => {
    await Promise.all(paths.map((path) => loadPath(id, path)));
  }, [loadPath]);

  const loadReader = useCallback(async (id: string): Promise<boolean> => {
    try {
      const [nextOverview, nextTargets, nextAnnotations, nextEvidence] = await Promise.all([
        fetchOverview(id),
        fetchTargets(id),
        fetchAnnotations(id),
        fetchReviewEvidence(id),
      ]);
      setOverview(nextOverview);
      setTargetList(nextTargets);
      setAnnotations(nextAnnotations.annotations);
      setEvidence(nextEvidence.evidence);
      setError("");
      const first = firstActionable(nextTargets.targets);
      setActiveTargetId((current) => current && nextTargets.targets.some((target) => target.target_id === current)
        ? current
        : first?.target_id ?? "");
      await loadPaths(id, initialPaths(nextTargets.targets));
      return true;
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
      return false;
    }
  }, [loadPaths]);

  useEffect(() => {
    if (!reviewId || !authed) return;
    void loadReader(reviewId);
  }, [reviewId, authed, loadReader]);

  useEffect(() => {
    if (!reviewId || !authed || !overview) return;
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
  }, [reviewId, authed, overview?.revision.id, overview?.session.range_mode, overview?.session.status, revisionAdvanced, dismissedSourceFingerprint, sourceProbeIntervalMs]);

  const targets = targetList?.targets ?? [];
  const annotationSearchText = useMemo(
    () => annotationSearchTextByTarget(targets, annotations),
    [targets, annotations],
  );
  const searchedTargets = useMemo(
    () => filterTargets(targets, query, annotationSearchText),
    [targets, query, annotationSearchText],
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
  const visiblePathOrder = useMemo(() => orderedPaths(visibleTargets), [visibleTargets]);
  const navigationPathOrder = visiblePathOrder.length > 0 ? visiblePathOrder : allPathOrder;
  // The outline indexes the filtered set, never the streamed one, for the same
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
  const readOnly = overview?.session.status === "archived" || revisionAdvanced;

  useEffect(() => {
    if (!activeTarget && visibleTargets.length > 0) setActiveTargetId(firstActionable(visibleTargets)?.target_id ?? "");
  }, [activeTarget, visibleTargets]);

  const scrollToTarget = useCallback(async (target: ReviewTarget, behavior: "instant" | "smooth" = "smooth") => {
    if (!reviewId) return;
    setActiveTargetId(target.target_id);
    await loadPath(reviewId, target.path);
    const pathIndex = navigationPathOrder.indexOf(target.path);
    if (pathIndex >= 0) {
      // Navigation prefetches only the next small window. Large reviews never
      // turn one keypress into an eager read of the remaining patch corpus.
      void loadPaths(reviewId, navigationPathOrder.slice(pathIndex + 1, pathIndex + 1 + PREFETCH_AHEAD_FILES));
    }
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => streamRef.current?.scrollToTarget(target, behavior)));
  }, [reviewId, loadPath, loadPaths, navigationPathOrder]);

  const selectPath = useCallback((path: string) => {
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
      const nextTargets = await fetchTargets(reviewId, nextOrder);
      setTargetList(nextTargets);
      const survivor = nextTargets.targets.find((target) => target.target_id === currentTargetId)
        ?? firstActionable(nextTargets.targets);
      setActiveTargetId(survivor?.target_id ?? "");

      const queueTargets = deltaFocus
        ? nextTargets.targets.filter((target) => deltaActiveIds.has(target.target_id) && targetStillNeedsAttention(target))
        : nextTargets.targets;
      const paths = initialPaths(queueTargets);
      if (survivor && !paths.includes(survivor.path)) paths.unshift(survivor.path);
      await loadPaths(reviewId, [...new Set(paths)]);
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
  }, [reviewId, busy, targetList?.order, activeTarget?.target_id, activeTargetId, deltaFocus, deltaActiveIds, loadPaths]);

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
        const next = stepTarget(projected, target.target_id, 1, { actionableOnly: true, wrap: true });
        if (next && next.target_id !== target.target_id) await scrollToTarget(next);
      }
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, readOnly, busy, updateTargetListAfterMark, visibleTargets, scrollToTarget]);

  const commentTarget = useCallback((target: ReviewTarget) => {
    const location = targetScrollLocation(target);
    setActiveTargetId(target.target_id);
    if (location) {
      setDraft({
        path: target.path,
        startLine: location.lineNumber,
        endLine: location.lineNumber,
        side: location.side,
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
        setAnnotations((await fetchAnnotations(reviewId)).annotations);
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

  const setAnnotationState = useCallback(async (id: string, state: AnnotationState) => {
    if (!reviewId || readOnly || busy) return;
    setBusy("comment");
    try {
      await patchAnnotation(id, { state });
      setAnnotations((await fetchAnnotations(reviewId)).annotations);
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
    const unloaded = navigationPathOrder.filter((path) => !details[path] && !fileErrors[path] && !inFlightPaths.current.has(path));
    void loadPaths(reviewId, unloaded.slice(0, LOAD_MORE_BATCH));
  }, [reviewId, navigationPathOrder, details, fileErrors, loadPaths]);

  const refreshReview = useCallback(async () => {
    if (!reviewId || readOnly || busy) return;
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
      loadGenerationRef.current += 1;
      detailsRef.current = {};
      setDetails({});
      setFileErrors({});
      inFlightPaths.current.clear();
      // An open composer anchors to a path, side and line in the diff that was
      // just discarded, so it cannot be saved against the new revision and must
      // not linger — the pin that keeps it in the stream would otherwise drag a
      // file the new revision never touched into the delta queue. It goes with
      // the diff it belonged to, and the reviewer is told below.
      if (draftPath) setDraft(null);
      setSourceState(null);
      setDismissedSourceFingerprint("");
      stranded = true;
      const [nextTargets, nextAnnotations, nextEvidence] = await Promise.all([
        fetchTargets(reviewId, targetList?.order ?? "recommended"),
        fetchAnnotations(reviewId),
        fetchReviewEvidence(reviewId),
      ]);
      setTargetList(nextTargets);
      setAnnotations(nextAnnotations.annotations);
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
        ? nextTargets.targets.filter((target) => activeIds.has(target.target_id))
        : nextTargets.targets;
      const paths = initialPaths(queueTargets);
      if (survivor && !paths.includes(survivor.path)) paths.unshift(survivor.path);
      await loadPaths(reviewId, [...new Set(paths)]);
      if (survivor) window.requestAnimationFrame(() => streamRef.current?.scrollToTarget(survivor, "instant"));
      setError(draftPath
        ? `Unsaved comment on ${draftPath} was discarded — this refresh replaced the diff it was anchored to.`
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
  }, [reviewId, readOnly, busy, activeTarget, draftPath, loadPaths, targetList?.order]);

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

  const closeFinishSheet = useCallback(() => {
    setFinishSheetOpen(false);
    window.requestAnimationFrame(() => finishButtonRef.current?.focus());
  }, []);

  const openFinishSheet = useCallback(async () => {
    // Finishing is a review-level judgment, so it is blocked while the reader is
    // stranded on a superseded revision for the same reason marking is. It is
    // *not* blocked by `readOnly` at large: on an archived session this button
    // is "Restore review", the one control that has to keep working.
    if (!reviewId || busy || revisionAdvanced) return;
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
  }, [reviewId, busy, revisionAdvanced, overview?.session.status]);

  const confirmFinish = useCallback(async () => {
    if (!reviewId || busy || revisionAdvanced) return;
    setBusy("finish");
    try {
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
  }, [reviewId, busy, revisionAdvanced]);

  const prepareFeedback = useCallback(async () => {
    if (!reviewId) return;
    setBusy("feedback");
    try {
      setFeedback(await exportFeedback(reviewId));
      setDelivery(null);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId]);

  const sendFeedbackToAuthor = useCallback(async () => {
    if (!reviewId || !feedback || busy) return;
    setBusy("delivery");
    try {
      setDelivery(await deliverFeedbackToClaude(reviewId));
      setAnnotations((await fetchAnnotations(reviewId)).annotations);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId, feedback, busy]);

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
      setRelated(await fetchRelatedSource(reviewId, path, line));
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    }
  }, [reviewId, targets, scrollToTarget]);

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
      label: `Switch to ${diffStyle === "split" ? "unified" : "split"} diff`,
      shortcut: "s",
      run: () => setDiffStyle((current) => current === "split" ? "unified" : "split"),
    },
    {
      id: "overview",
      label: "Open change overview",
      detail: "Major changes, target counts, verification, provenance, and evidence",
      run: openOverview,
    },
    {
      id: "refresh",
      label: "Refresh revision",
      detail: "Accept the latest source snapshot and reconcile prior judgments",
      disabled: Boolean(readOnly) || Boolean(busy),
      run: () => void refreshReview(),
    },
    {
      id: "feedback",
      label: "Prepare feedback",
      detail: "Preview structured human feedback before copying or exact-session delivery",
      disabled: Boolean(busy),
      run: () => void prepareFeedback(),
    },
    {
      id: "finish",
      label: overview?.session.status === "finished" ? "Reopen review" : overview?.session.status === "archived" ? "Restore review" : "Finish review",
      detail: "Preview target and risk counts before changing review status",
      disabled: Boolean(busy) || revisionAdvanced,
      run: () => void openFinishSheet(),
    },
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
  ], [activeTarget, readOnly, revisionAdvanced, busy, contextOpen, contextTab, focusMode, diffStyle, overview?.session.status, targetList?.order, markTarget, commentTarget, closeContext, openContext, openOverview, refreshReview, prepareFeedback, openFinishSheet, changeOrder]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (commandMode || overviewOpen || finishSheetOpen) {
        // A dialog whose own Escape handler is out of reach — because focus
        // escaped its overlay — would otherwise be closable only with the
        // mouse, so the window handler closes the topmost dialog instead of
        // bailing out into a dead end.
        if (event.key !== "Escape") return;
        if (commandMode) closeCommands();
        else if (overviewOpen) closeOverview();
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
          next = stepTarget(visibleTargets, activeTarget.target_id, 1, { highPriorityOnly: true, wrap: true });
          break;
        case "[":
          next = stepTarget(visibleTargets, activeTarget.target_id, -1, { highPriorityOnly: true, wrap: true });
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
          setDiffStyle((current) => current === "split" ? "unified" : "split");
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
  }, [activeTarget, visibleTargets, markTarget, commentTarget, scrollToTarget, contextOpen, contextTab, openContext, closeContext, finishSheetOpen, closeFinishSheet, query, commandMode, closeCommands, overviewOpen, closeOverview, openCommands]);

  if (!authed) {
    return (
      <div className="p-8 text-sm text-neutral-300">
        <div className="pb-2 text-lg text-amber-300">No workspace token</div>
        <p className="max-w-xl text-neutral-500">Reopen this local review from the terminal with <code className="text-neutral-300">lc review --open</code>.</p>
      </div>
    );
  }
  if (error && !overview) return <div className="p-8 text-sm text-rose-300">Could not load this review: {error}</div>;
  if (!overview || !targetList) return <div className="p-8 text-sm text-neutral-600">Loading review reader…</div>;

  const loadedPaths = new Set([...Object.keys(details), ...Object.keys(fileErrors)]);
  const streamPaths = visiblePathOrder;
  const remainingFileCount = streamPaths.filter((path) => !loadedPaths.has(path)).length;
  const canSendToClaude =
    !readOnly &&
    overview.revision.provenance_host === "claude" &&
    overview.revision.provenance_certainty === "exact" &&
    Boolean(overview.revision.provenance_session_id);
  const drawer = activeTarget ? (
    <Suspense fallback={<div className="p-3 text-[10px] text-neutral-600">Loading context…</div>}>
      <ContextDrawer
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
    <div className="relative flex h-screen min-h-0 flex-col bg-neutral-950 text-neutral-200" data-testid="review-reader">
      <ReaderHeader
        overview={overview}
        progress={progress}
        diffStyle={diffStyle}
        order={targetList.order}
        focusMode={focusMode}
        contextOpen={contextOpen}
        busy={Boolean(busy)}
        readOnly={Boolean(readOnly)}
        revisionAdvanced={revisionAdvanced}
        finishButtonRef={finishButtonRef}
        onFinish={() => void openFinishSheet()}
        onRefresh={() => void refreshReview()}
        onPrepareFeedback={() => void prepareFeedback()}
        onOrderChange={(nextOrder) => void changeOrder(nextOrder)}
        onToggleDiffStyle={() => setDiffStyle((current) => current === "split" ? "unified" : "split")}
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
            onClose={closeOverview}
            onSelectPath={selectPath}
          />
        </Suspense>
      )}
      {revisionAdvanced && (
        <div className="flex shrink-0 items-center gap-2 border-b border-rose-900/60 bg-rose-950/25 px-3 py-1.5 text-[10px] text-rose-200" role="alert">
          <span>Revision advanced — reload required</span>
          <span className="text-rose-300/70">This review moved to a newer revision that never finished loading, so judgments stay blocked until the reader catches up.</span>
          <span className="flex-1" />
          <button
            type="button"
            disabled={Boolean(busy)}
            onClick={() => void reloadReader()}
            className="border border-rose-800 px-2 py-0.5 disabled:opacity-40"
          >
            Reload review
          </button>
        </div>
      )}
      {sourceState && (
        <div className="flex shrink-0 items-center gap-2 border-b border-sky-900/60 bg-sky-950/20 px-3 py-1.5 text-[10px] text-sky-200" role="status" aria-live="polite">
          <span>New revision available</span>
          <span className="text-sky-400/70">
            {sourceState.path_count} changed file{sourceState.path_count === 1 ? "" : "s"} · current diff stays frozen
          </span>
          <span className="flex-1" />
          <button type="button" onClick={() => void refreshReview()} className="border border-sky-800 px-2 py-0.5">Review update</button>
          <button
            type="button"
            onClick={() => {
              setDismissedSourceFingerprint(sourceState.fingerprint);
              setSourceState(null);
            }}
            className="text-sky-400/70"
          >
            Later
          </button>
        </div>
      )}
      <RevisionDeltaBar
        info={revisionInfo}
        discarded={overview.discarded}
        focused={deltaFocus}
        remainingActive={deltaRemainingCount}
        onFocusDelta={() => setDeltaFocus(true)}
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
      {error && overview && <div className="shrink-0 border-b border-amber-900/50 bg-amber-950/20 px-3 py-1 text-[10px] text-amber-300">{error}</div>}
      {feedback && (
        <div className="max-h-48 shrink-0 overflow-auto border-b border-violet-900/50 bg-neutral-950 px-3 py-2">
          <div className="flex items-center gap-2 text-[10px] text-violet-300">
            <span>Feedback · {feedback.open} open · {feedback.orphaned} orphaned</span>
            <span className="flex-1" />
            <button type="button" onClick={() => void navigator.clipboard?.writeText(feedback.markdown)} className="border border-violet-900 px-2 py-0.5">Copy</button>
            {canSendToClaude && (
              <button
                type="button"
                disabled={busy !== ""}
                onClick={() => void sendFeedbackToAuthor()}
                className="border border-violet-700 px-2 py-0.5 text-violet-200 disabled:opacity-40"
              >
                Send to Claude
              </button>
            )}
            <button type="button" onClick={() => { setFeedback(null); setDelivery(null); }} className="text-neutral-500">Close</button>
          </div>
          {delivery && (
            <div className={`mt-1 text-[10px] ${delivery.state === "sent" ? "text-emerald-300" : "text-amber-300"}`}>
              {delivery.state === "sent" ? "Feedback sent" : `Feedback ${delivery.state}`} · {delivery.annotation_count} comment{delivery.annotation_count === 1 ? "" : "s"} · {delivery.target_ref}
            </div>
          )}
          <pre className="mt-2 whitespace-pre-wrap text-[10px] text-neutral-400">{feedback.markdown}</pre>
        </div>
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
          onQuery={setQuery}
          onNavigateMatch={navigateSearchMatch}
          onDismissSearch={dismissSearch}
          onSelectPath={selectPath}
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
          {visibleTargets.length === 0 ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 text-[11px] text-neutral-600">
              <span>{deltaFocus ? "Revision delta complete — no target needs another judgment." : "No review targets match this search."}</span>
              {deltaFocus && <button type="button" onClick={() => setDeltaFocus(false)} className="border border-neutral-800 px-2 py-1 text-neutral-400">Show all targets</button>}
              {!deltaFocus && query.trim() && <button type="button" onClick={dismissSearch} className="border border-neutral-800 px-2 py-1 text-neutral-400">Clear search</button>}
            </div>
          ) : (
            <ReviewStream
              ref={streamRef}
              targets={visibleTargets}
              details={details}
              errors={fileErrors}
              annotations={annotations}
              draft={draft}
              diffStyle={diffStyle}
              activeTargetId={activeTarget?.target_id ?? ""}
              busy={Boolean(busy) || Boolean(readOnly)}
              remainingFileCount={remainingFileCount}
              outstandingByPath={outstandingByPath}
              onActiveTarget={setActiveTargetId}
              onDraft={setDraft}
              onCreate={(input) => void createAnnotation(input)}
              onSetAnnotationState={(id, state) => void setAnnotationState(id, state)}
              onMark={(target, state, advance) => void markTarget(target, state, advance)}
              onComment={commentTarget}
              onContext={openContext}
              onBulkReview={(path) => void bulkReviewFile(path)}
              onLoadMore={loadMore}
            />
          )}
        </main>

        {contextOpen && drawer && (
          <div className={contextPinned
            ? "w-[420px] shrink-0 border-l border-neutral-800 bg-neutral-950"
            : "absolute inset-y-0 right-0 z-50 w-[min(440px,calc(100%-40px))] border-l border-neutral-700 bg-neutral-950 shadow-[-18px_0_40px_rgba(0,0,0,0.35)]"}>
            {drawer}
          </div>
        )}
      </div>

      {finishSheetOpen && finishSummary && (
        <Suspense fallback={<div className="absolute inset-0 z-[70] flex items-center justify-center bg-black/45 text-[11px] text-neutral-400">Loading finish summary…</div>}>
          <FinishSheet
            summary={finishSummary}
            degraded={overview.degraded}
            busy={busy === "finish"}
            onClose={closeFinishSheet}
            onFinish={() => void confirmFinish()}
          />
        </Suspense>
      )}

      <footer className="flex shrink-0 items-center gap-3 border-t border-neutral-900 px-3 py-1 text-[9px] text-neutral-600">
        <span>j/k target · J/K file · r reviewed · x changes · c comment · e context · f focus · / find · Enter next match</span>
        <span className="flex-1" />
        {overview.degraded.length > 0 && <span className="text-amber-400">{overview.degraded.length} uncertain signal{overview.degraded.length === 1 ? "" : "s"}</span>}
        <span>{progress.reviewed}/{progress.target_count}</span>
      </footer>
    </div>
  );
}
