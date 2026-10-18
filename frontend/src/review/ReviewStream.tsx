import { Button as UiButton } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  useContext,
} from "react";
import { parsePatchFiles } from "@pierre/diffs";
import { Boxes, CheckCircle2, ChevronDown, ChevronUp, Code2, Download, EllipsisVertical, ExternalLink, FilePenLine, MessageSquareText, Moon, PanelRight, Pencil, Puzzle, RotateCw, Sparkles, Sun, SunMoon, X } from "lucide-react";
import type { CodeTarget } from "../lib/codeLinks";
import {
  File,
  FileDiff,
  type CodeViewDiffItem,
  type CodeViewFileItem,
  type SelectedLineRange,
} from "@pierre/diffs/react";
import AnnotationCard from "./AnnotationCard";
import ApiReviewPreview from "./ApiReviewPreview";
import ReviewChangeProposalComposer from "./ReviewChangeProposalComposer";
import type { Theme } from "../lib/theme";
import type { ContextDrawerTab } from "./ContextDrawer";
import FileTypeIcon from "./FileTypeIcon";
import CsvReviewPreview from "./CsvReviewPreview";
import MarkdownReviewPreview, { type MarkdownPreviewTheme, type MarkdownRenderMode } from "./MarkdownReviewPreview";
import MediaReviewPreview, { type MediaRenderMode } from "./MediaReviewPreview";
import ServiceReviewPreview from "./ServiceReviewPreview";
import { isUnavailableRunnerError } from "./surfaceRunPresentation";
import SurfacePicker, { type SurfacePickerOption } from "./SurfacePicker";
import SurfacePluginPreview, { surfaceComparable, type SurfacePreviewMode } from "./SurfacePluginPreview";
import WebReviewPreview, { type WebRouteOption } from "./WebReviewPreview";
import { motionSafeScrollBehavior, resolveDiffStyle, type DiffOverflow, type DiffStyle, type DiffStylePreference } from "./diffModel";
import { reviewCodeThemeDefinition, type ReviewCodeTheme } from "./reviewCodeTheme";
import { openDedicatedReviewItem, type DedicatedReviewItem } from "./reviewItemNavigation";
import {
  currentReviewId,
  fetchFileContents,
  fetchRevisionComparisonFileContents,
  fetchSurfaceImpactWithRetry,
  type SurfaceImpactScore,
} from "./reviewApi";
import ReviewTargetHeader, { ReviewLockReason } from "./ReviewTargetHeader";
import type { Draft, MarkerDisplayLine } from "./annotationModel";
import {
  commentsAt,
  draftMarkerKey,
  fileComments,
  locationLabel,
  markerLines,
  markerSide,
  markerSignature,
  partition,
} from "./annotationModel";
import { groupReaderFiles, orderedPaths, targetScrollLocation } from "./readerModel";
import { decorateReviewScopeBoxes, decorateReviewTargetFolds, isFoldHiddenRow, renderedDiffLine, targetHasContinuousScope } from "./reviewScopeOverlay";
import { decorateCssColorSwatches } from "./cssColorSwatches";
import { decorateDiffAccessibility } from "./reviewDiffA11y";
import {
  DependencyFileSemantics,
  GeneratedFileSemantics,
  SpecialGitChange,
  dependencyFileRelevant,
  generatedSource,
  type ReviewFileRole,
} from "./ReviewFileSemantics";
import type {
  Annotation,
  AnnotationDraft,
  AnnotationKind,
  FileDetail,
  MarkState,
  ReviewDependencyProjection,
  ReviewSpecialChange,
  ReviewChangeProposal,
  ReviewEvidence,
  FindingDismissalReason,
  FindingDisposition,
  ReviewProposalSelection,
  ReviewRuntimeValidationResponse,
  ReviewSurfaceInfo,
  ReviewSurfaceRunResult,
  ReviewTarget,
  WebPreview,
} from "./types";

const DIFF_CONTEXT_EXPANSION_LINES = 20;

interface StreamMarkerMeta {
  key: string;
  targetIds: string[];
  targetEndIds: string[];
  /** Anchor keys of the comments (and draft) whose card this row draws. */
  commentKeys: string[];
}

interface SelectionActionState {
  path: string;
  range: SelectedLineRange;
  draft: Draft;
  clientX: number;
  clientY: number;
}

type ParsedFileDiff = CodeViewDiffItem<StreamMarkerMeta>["fileDiff"];
type StreamMarkers = NonNullable<CodeViewDiffItem<StreamMarkerMeta>["annotations"]>;

interface StreamItemBase {
  id: string;
  version: number;
  estimatedHeight: number;
  collapsed: boolean;
}

interface MarkdownStreamItem extends StreamItemBase {
  type: "markdown";
  mode: MarkdownRenderMode;
}

interface CsvStreamItem extends StreamItemBase {
  type: "csv";
  mode: MarkdownRenderMode;
}

interface MediaStreamItem extends StreamItemBase {
  type: "media";
  mode: MediaRenderMode;
}

interface WebSurfaceStreamItem extends StreamItemBase {
  type: "web-surface";
  surface: ReviewSurfaceInfo;
  documentPath: string;
  preview: NonNullable<FileDetail["preview"]> & { kind: "web" };
}

interface ApiSurfaceStreamItem extends StreamItemBase {
  type: "api-surface";
  surface: ReviewSurfaceInfo;
  mode: MarkdownRenderMode;
}

interface ServiceSurfaceStreamItem extends StreamItemBase {
  type: "service-surface";
  surface: ReviewSurfaceInfo;
  mode: MarkdownRenderMode;
}

/** Any descriptor-bearing or unknown-kind surface, grouped per provider. */
interface PluginSurfaceStreamItem extends StreamItemBase {
  type: "plugin-surface";
  surface: ReviewSurfaceInfo;
  options: SurfacePickerOption[];
  mode: SurfacePreviewMode;
}

interface LinkedSurfaceStreamItem extends StreamItemBase {
  type: "linked-surface";
  surface: ReviewSurfaceInfo;
  options: SurfacePickerOption[];
  mode: SurfacePreviewMode;
}

interface DiffStreamItem extends StreamItemBase {
  type: "diff";
  fileDiff: ParsedFileDiff;
  /** Changes only when the patch text changes; forces Pierre to drop hydrated state from another diff. */
  patchVersion: number;
  annotations: StreamMarkers;
}

interface FileStreamItem extends StreamItemBase {
  type: "file";
  file: CodeViewFileItem<StreamMarkerMeta>["file"];
  /** The file's source request failed; the card offers a retry. */
  failed: boolean;
}

/** A rename/mode/symlink/submodule change the packet models; never an empty or raw text diff. */
interface SpecialStreamItem extends StreamItemBase {
  type: "special";
  change: ReviewSpecialChange;
  patch: string;
}

/** Semantic context rendered before a file's raw diff (plan 8F). */
export interface ReviewStreamFileSemantics {
  /** Review roles from the Brief projection's material; generated files lead with their provenance. */
  roles?: ReadonlyMap<string, ReviewFileRole>;
  /** Existing package-level dependency projection for this Snapshot. */
  dependencies?: ReviewDependencyProjection | null;
}

type StreamItem = MarkdownStreamItem | CsvStreamItem | MediaStreamItem | WebSurfaceStreamItem | ApiSurfaceStreamItem | ServiceSurfaceStreamItem | PluginSurfaceStreamItem | LinkedSurfaceStreamItem | DiffStreamItem | FileStreamItem | SpecialStreamItem;

type SurfaceView = "web" | "api" | "service" | "plugin";

/**
 * Which view presents a surface. A versioned preview descriptor always wins, and
 * every kind without a dedicated view falls through to the generic plugin view,
 * so basic preview selection never depends on a list of known kinds. The three
 * named kinds only select richer built-in presenters.
 */
function surfaceView(surface: ReviewSurfaceInfo): SurfaceView {
  if (surface.preview) return "plugin";
  if (surface.kind === "web.route") return "web";
  if (surface.kind === "api.request") return "api";
  if (surface.kind === "service") return "service";
  return "plugin";
}

function pluginGroupId(surface: ReviewSurfaceInfo): string {
  return `surface-group:plugin:${surface.provider}`;
}

interface ParsedDiffCacheEntry {
  patch: string;
  fileDiff: ParsedFileDiff | null;
  version: number;
}

function diffCodeScrollers(node: HTMLElement): HTMLElement[] {
  const roots: ParentNode[] = [node];
  if (node.shadowRoot) roots.push(node.shadowRoot);
  for (const host of node.querySelectorAll<HTMLElement>("diffs-container")) {
    roots.push(host);
    if (host.shadowRoot) roots.push(host.shadowRoot);
  }
  const seen = new Set<HTMLElement>();
  const rows: HTMLElement[] = [];
  for (const root of roots) {
    for (const scroller of root.querySelectorAll<HTMLElement>("[data-code]")) {
      if (seen.has(scroller)) continue;
      seen.add(scroller);
      rows.push(scroller);
    }
  }
  return rows;
}

function hideInnerScrollbarChrome(scroller: HTMLElement): void {
  scroller.dataset.lcReviewInnerScroll = "true";
  scroller.style.setProperty("scrollbar-width", "none");
  const root = scroller.getRootNode();
  if (!(root instanceof ShadowRoot)) return;
  if (root.querySelector("style[data-lc-review-scrollbar-style]")) return;
  const style = document.createElement("style");
  style.dataset.lcReviewScrollbarStyle = "true";
  style.textContent = `
    [data-lc-review-inner-scroll="true"] { scrollbar-width: none !important; }
    [data-lc-review-inner-scroll="true"]::-webkit-scrollbar { height: 0 !important; width: 0 !important; }
  `;
  root.appendChild(style);
}

function restoreInnerScrollbarChrome(scroller: HTMLElement): void {
  delete scroller.dataset.lcReviewInnerScroll;
  scroller.style.removeProperty("scrollbar-width");
}

interface ItemRevisionEntry {
  version: number;
  type: "diff" | "file" | "markdown" | "csv" | "media" | "linked-surface" | "special";
  source: string;
  markers: string;
  collapsed: boolean;
}

export interface ReviewStreamHandle {
  scrollToTarget(target: ReviewTarget, behavior?: "instant" | "smooth"): void;
  scrollToFile(path: string, behavior?: "instant" | "smooth"): void;
  scrollToSurface(surfaceKey: string, behavior?: "instant" | "smooth"): void;
}

interface ReviewStreamProps {
  targets: ReviewTarget[];
  details: Record<string, FileDetail | undefined>;
  surfaces?: ReviewSurfaceInfo[];
  /** Show full review previews on the first page; retain file-level previews elsewhere. */
  showReviewPreviews?: boolean;
  onOpenPreviews?(): void;
  dedicatedItem?: DedicatedReviewItem | null;
  surfaceRuns?: Record<string, { old?: ReviewSurfaceRunResult; new?: ReviewSurfaceRunResult; error?: string }>;
  /** Surfaces with a preview/run in flight. */
  surfaceBusy?: ReadonlySet<string>;
  runtimeValidations?: Record<string, { response?: ReviewRuntimeValidationResponse; evidence?: ReviewEvidence; error?: string }>;
  runtimeValidationBusy?: ReadonlySet<string>;
  /** Snapshot every plugin preview is pinned to. */
  revisionId?: string;
  errors: Record<string, string | undefined>;
  annotations: Annotation[];
  draft: Draft | null;
  proposalSelection?: ReviewProposalSelection | null;
  activeProposal?: ReviewChangeProposal | null;
  proposalBusy?: boolean;
  sourceMutationSupported?: boolean;
  /** Read-only arbitrary-source comparison (Git/worktree/etc.). */
  comparisonMode?: boolean;
  /** Exact Snapshot pair while the normal Review remains interactive. */
  snapshotComparison?: {
    fromRevision: number;
    toRevision: number;
    fromRevisionId: string;
    toRevisionId: string;
  } | null;
  diffStyle: DiffStylePreference;
  diffOverflow?: DiffOverflow;
  codeTheme?: ReviewCodeTheme;
  chromeTheme?: Theme;
  activeTargetId: string;
  busy: boolean;
  /** Pause pagination for an in-flight action, independently of read-only controls. */
  loadingPaused?: boolean;
  remainingFileCount: number;
  /** Automatic footer-driven reveal is disabled for very large Reviews so an
   * unmeasured first batch cannot recursively materialize a whole page. */
  autoLoadMore?: boolean;
  pendingVisibleFileCount?: number;
  readyHiddenFileCount?: number;
  preparingHiddenFileCount?: number;
  pagination?: {
    page: number;
    pageCount: number;
    changedLines: number;
    blocked: boolean;
    onChange(page: number): void;
  };
  /**
   * How many targets each file still owes a judgment, counted over the whole
   * review and not over `targets`. The caller already keeps it for the bulk
   * "review eligible remainder" action, and it is the only unfiltered per-file
   * tally this component is handed.
   */
  outstandingByPath: ReadonlyMap<string, number>;
  onActiveTarget(targetId: string): void;
  onDraft(next: Draft | null): void;
  onSuggestEdit?(draft: Draft): void;
  onEditSource?(draft: Draft): void;
  onQuickEdit?(draft: Draft): void;
  onCreateProposal?(): void;
  onCreateAndApplyProposal?(): void;
  onApplyProposal?(): void;
  onCloseProposal?(): void;
  onCreate(draft: AnnotationDraft): void;
  onSetAnnotationState(id: string, state: "open" | "resolved"): void;
  onFindingDisposition?(id: string, disposition: FindingDisposition, reason?: FindingDismissalReason): void;
  onMark(target: ReviewTarget, state: MarkState, advance?: boolean): void;
  onComment(target: ReviewTarget): void;
  onAsk?(target: ReviewTarget): void;
  onAskSelection?(draft: Draft): void;
  onContext(target: ReviewTarget, tab?: ContextDrawerTab): void;
  onBulkReview(path: string): void;
  onLoadMore(automatic?: boolean): void;
  /** Fires once the first mounted Review item has real reviewer-visible content. */
  onFirstContentReady?(): void;
  onRunSurface?(surface: ReviewSurfaceInfo, compare: boolean): void;
  onValidateSurface?(surface: ReviewSurfaceInfo): void;
  onCancelRuntimeValidation?(surface: ReviewSurfaceInfo, jobId: string): void;
  /** Reloads a file whose source request failed; resolves true once it loads. */
  onRetryPath?(path: string): Promise<boolean>;
  onOpenInCode?(target: CodeTarget): void;
  fileSemantics?: ReviewStreamFileSemantics;
}

/**
 * Owns its own busy state: stream items re-render only when their content
 * version changes, and a retry that fails again leaves the same error text.
 */
function FileLoadRetry({ path, onRetry }: { path: string; onRetry(path: string): Promise<boolean> }) {
  const [state, setState] = useState<"idle" | "retrying" | "failed">("idle");
  return (
    <div className="flex items-center justify-end gap-2 border-t border-border/70 px-3 py-2 text-label text-muted-foreground">
      {state === "failed" && <span role="status">Still unavailable.</span>}
      <UiButton
        type="button"
        disabled={state === "retrying"}
        onClick={(event) => {
          event.stopPropagation();
          setState("retrying");
          void onRetry(path).then((loaded) => setState(loaded ? "idle" : "failed"), () => setState("failed"));
        }}
        aria-label={`Retry loading ${path}`}
        className="h-7"
      >
        <RotateCw size={11} aria-hidden="true" className={state === "retrying" ? "animate-spin motion-reduce:animate-none" : ""} />
        {state === "retrying" ? "Retrying…" : "Retry"}
      </UiButton>
    </div>
  );
}

function RuntimeValidationControl({
  state,
  busy,
  target,
  onRun,
  onCancel,
  onInspect,
  onAsk,
}: {
  state?: { response?: ReviewRuntimeValidationResponse; evidence?: ReviewEvidence; error?: string };
  busy: boolean;
  target?: ReviewTarget;
  onRun(): void;
  onCancel?(jobId: string): void;
  onInspect?(target: ReviewTarget): void;
  onAsk?(target: ReviewTarget): void;
}) {
  const record = state?.response?.runtime_validation;
  const evidence = state?.response?.evidence ?? state?.evidence;
  const status = String(record?.status ?? evidence?.verification_status ?? "").toUpperCase();
  const job = record?.job;
  const historical = Boolean(state?.response?.historical || job?.historical || evidence?.execution_intent === "explicit_historical");
  const active = status === "QUEUED" || status === "RUNNING" || status === "CANCEL_REQUESTED";
  const tone = status === "PASS"
    ? "text-emerald-300"
    : status === "FAIL" || status === "FAILED"
      ? "text-rose-300"
      : status
        ? "text-amber-300"
        : "text-muted-foreground";
  const detail = state?.error
    || record?.status_reasons?.[0]
    || record?.summary
    || job?.detail
    || job?.last_error
    || evidence?.detail
    || "";
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-caption" data-testid="runtime-validation-control">
      <span className="text-muted-foreground">Runtime validation</span>
      {status && <span className={tone}>{status.replaceAll("_", " ")}</span>}
      {historical && <span className="text-muted-foreground">historical</span>}
      {detail && <span className="max-w-80 truncate text-muted-foreground" title={detail}>{detail}</span>}
      <UiButton
        type="button"
        variant="outline"
        size="compact"
        disabled={busy || active}
        onClick={onRun}
      >
        {busy || active ? "Validating…" : status ? "Run again" : "Validate"}
      </UiButton>
      {active && job?.job_id && onCancel && (
        <UiButton type="button" variant="ghost" size="compact" onClick={() => onCancel(job.job_id!)}>
          Cancel
        </UiButton>
      )}
      {evidence && target && onInspect && (
        <UiButton type="button" variant="ghost" size="compact" onClick={() => onInspect(target)}>
          Inspect evidence
        </UiButton>
      )}
      {target && onAsk && status === "FAIL" && (
        <UiButton type="button" variant="ghost" size="compact" onClick={() => onAsk(target)}>
          Ask
        </UiButton>
      )}
    </div>
  );
}

function markerKey(side: "additions" | "deletions", line: number): string {
  return `${side}:${line}`;
}

function downloadTextPatch(filename: string, patch: string): void {
  const blob = new Blob([patch], { type: "text/x-diff;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/**
 * Whether a comment belongs on the diff at all.
 *
 * LemonCrow's per-file attention note explains why the file is in the queue; it
 * is machine reasoning about the review, not something a person left for the
 * author. Drawn at line 0 as an ordinary card it is one more comment thread in
 * a column of comment threads, and the reader loses the ability to tell what a
 * human asked for from what the tool observed. The context drawer still lists
 * it, under a source label that says where it came from.
 */
function showInlineAnnotation(annotation: Annotation): boolean {
  return !(
    annotation.source === "lemoncrow"
    && annotation.file_level
    && annotation.title === "Why this file deserves attention"
  );
}

function targetAtLine(
  targets: readonly ReviewTarget[],
  path: string,
  side: "additions" | "deletions",
  line: number,
  activeTargetId: string,
): ReviewTarget | null {
  const rows = targets.filter((target) => target.path === path);
  if (rows.length === 0) return null;
  const active = rows.find((target) => target.target_id === activeTargetId);
  if (line === 0) return active ?? rows.find((target) => target.state !== "reviewed") ?? rows[0];
  const targetSide = side === "deletions" ? "old" : "new";
  const ownsChangedLine = (target: ReviewTarget) => target.spans.some(
    (span) => span.side === targetSide && span.start_line <= line && span.end_line >= line,
  );
  if (active && (ownsChangedLine(active) || (active.start_line <= line && active.end_line >= line))) return active;
  const exact = rows.find(ownsChangedLine);
  if (exact) return exact;
  const containing = rows
    .filter((target) => target.start_line > 0 && target.start_line <= line && target.end_line >= line)
    .sort((left, right) => (left.end_line - left.start_line) - (right.end_line - right.start_line));
  return containing[0] ?? active ?? null;
}

function targetHeaderLocation(target: ReviewTarget): {
  side: "additions" | "deletions";
  lineNumber: number;
} {
  const location = targetScrollLocation(target) ?? { side: "additions" as const, lineNumber: 0 };
  return {
    side: location.side,
    lineNumber: location.lineNumber > 1 ? location.lineNumber - 1 : 0,
  };
}

// Where the change under a target's header ends, most specific first. A symbol
// owns its whole scope, falling back to its last changed line (always rendered
// even when scope context is collapsed). Other targets can scatter regions
// across the file, so only the region the header sits on counts.
function targetEndLocations(target: ReviewTarget): Array<{
  side: "additions" | "deletions";
  lineNumber: number;
}> {
  const start = targetScrollLocation(target);
  if (!start) return [];
  const spanSide = start.side === "additions" ? "new" : "old";
  const sideSpans = target.spans.filter((span) => span.side === spanSide);
  const lines = target.kind === "symbol"
    ? [target.end_line, Math.max(0, ...sideSpans.map((span) => span.end_line))]
    : [sideSpans.find((span) => span.start_line === start.lineNumber)?.end_line ?? 0];
  return [...new Set([...lines, start.lineNumber])]
    .filter((line) => line >= start.lineNumber)
    .map((lineNumber) => ({ side: start.side, lineNumber }));
}

// ReviewTargetHeader's `my-0.5` margin.
const TARGET_HEADER_MARGIN = 2;
/** A navigation holds its destination at least this long after it starts... */
const NAVIGATION_HOLD_MS = 1200;
/** ...and this long after its last (re-)aim, while the stream settles. */
const NAVIGATION_SETTLE_MS = 700;
/** How long a navigation waits for a destination the stream has not rendered yet. */
const NAVIGATION_WAIT_MS = 8000;
const STREAM_SCROLL_KEYS: ReadonlySet<string> = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);
const NO_TARGETS: readonly ReviewTarget[] = [];
const NO_BUSY_SURFACES: ReadonlySet<string> = new Set();

type StickyTrack = { targetId: string; top: number; height: number; left: number; width: number };

function sameStickyTracks(left: readonly StickyTrack[], right: readonly StickyTrack[]): boolean {
  return left.length === right.length && left.every((track, index) => {
    const other = right[index];
    return track.targetId === other.targetId
      && Math.abs(track.top - other.top) < 0.5
      && Math.abs(track.height - other.height) < 0.5
      && Math.abs(track.left - other.left) < 0.5
      && Math.abs(track.width - other.width) < 0.5;
  });
}

// One track per inline target header, spanning from that header to the end of
// its change, in coordinates relative to `box`.
function measureStickyTracks(
  box: HTMLElement,
  item: HTMLElement,
  targets: readonly ReviewTarget[],
): { tracks: StickyTrack[]; headers: HTMLElement[] } {
  const boxRect = box.getBoundingClientRect();
  const itemRect = item.getBoundingClientRect();
  const tracks: StickyTrack[] = [];
  const headers: HTMLElement[] = [];
  for (const target of targets) {
    const header = [...item.querySelectorAll<HTMLElement>(
      `.review-target-header[data-review-target-id="${CSS.escape(target.target_id)}"]`,
    )].find((node) => !node.closest("[data-review-sticky-copy]"));
    const rect = header?.getBoundingClientRect();
    if (!header || !rect || rect.height <= 0) continue;
    const endRow = targetEndLocations(target)
      .map((location) => renderedDiffLine(item, location.side, location.lineNumber))
      .find((row): row is HTMLElement => Boolean(row));
    const endBottom = endRow ? endRow.getBoundingClientRect().bottom : itemRect.bottom;
    const top = rect.top - boxRect.top - TARGET_HEADER_MARGIN;
    const height = endBottom - boxRect.top - top;
    // A change shorter than its header has nothing to stick over.
    if (height < 2 * (rect.height + 2 * TARGET_HEADER_MARGIN)) continue;
    headers.push(header);
    tracks.push({ targetId: target.target_id, top, height, left: rect.left - boxRect.left, width: rect.width });
  }
  return { tracks, headers };
}

/**
 * Keeps a review area's header in view while its change is on screen. The
 * inline header lives in a Pierre annotation row, whose height bounds CSS
 * `sticky`, so each header gets a copy in an absolute track spanning its
 * change. The copy is `sticky` within that track, so the browser scrolls it
 * natively and it leaves with the change's last line. It stays invisible over
 * the inline original until that original starts scrolling off the top.
 */
function StickyTargetHeaders({
  targets,
  scrollRef,
  layoutVersion,
  renderHeader,
}: {
  targets: readonly ReviewTarget[];
  scrollRef: { readonly current: HTMLDivElement | null };
  layoutVersion: number;
  renderHeader(targetId: string): ReactNode;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [tracks, setTracks] = useState<readonly StickyTrack[]>([]);
  const [stuck, setStuck] = useState<ReadonlySet<string>>(() => new Set());

  useEffect(() => {
    const box = boxRef.current;
    const item = box?.parentElement;
    const root = scrollRef.current;
    if (!box || !item || !root || targets.length === 0) {
      setTracks((current) => (current.length === 0 ? current : []));
      return;
    }
    const intersections = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver((entries) => {
      setStuck((current) => {
        let next: Set<string> | null = null;
        for (const entry of entries) {
          const id = entry.target instanceof HTMLElement ? entry.target.dataset.reviewTargetId ?? "" : "";
          const isStuck = Boolean(entry.rootBounds && entry.boundingClientRect.top < entry.rootBounds.top);
          if (!id || isStuck === current.has(id)) continue;
          next ??= new Set(current);
          if (isStuck) next.add(id);
          else next.delete(id);
        }
        return next ?? current;
      });
    }, { root, threshold: [0, 1] });
    let frame: number | null = null;
    const measure = () => {
      frame = null;
      const next = measureStickyTracks(box, item, targets);
      setTracks((current) => (sameStickyTracks(current, next.tracks) ? current : next.tracks));
      intersections?.disconnect();
      for (const header of next.headers) intersections?.observe(header);
    };
    const schedule = () => {
      if (frame === null) frame = window.requestAnimationFrame(measure);
    };
    schedule();
    // Layout only moves with the item's size (annotations, composers, hunk
    // expansion); scrolling needs no JavaScript.
    const resizes = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    resizes?.observe(item);
    window.addEventListener("resize", schedule);
    return () => {
      window.removeEventListener("resize", schedule);
      resizes?.disconnect();
      intersections?.disconnect();
      if (frame !== null) window.cancelAnimationFrame(frame);
    };
  }, [layoutVersion, scrollRef, targets]);

  return (
    <div ref={boxRef} className="pointer-events-none relative h-0">
      {tracks.map((track) => {
        const header = renderHeader(track.targetId);
        if (!header) return null;
        const isStuck = stuck.has(track.targetId);
        return (
          <div key={track.targetId} className="absolute inset-x-0 z-10" style={{ top: track.top, height: track.height }}>
            <div
              data-review-sticky-copy=""
              data-stuck={isStuck ? "true" : "false"}
              data-testid={`review-sticky-target:${track.targetId}`}
              className={`sticky top-0 flex flex-col border-b border-border bg-background ${isStuck ? "pointer-events-auto" : "invisible"}`}
            >
              <div style={{ marginLeft: track.left, width: track.width }}>{header}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ReviewTargetEndActions({
  target,
  busy,
  onMark,
  onComment,
  onAsk,
  onContext,
}: {
  target: ReviewTarget;
  busy: boolean;
  onMark(state: MarkState, advance?: boolean): void;
  onComment(): void;
  onAsk?(): void;
  onContext(): void;
}) {
  const lock = useContext(ReviewLockReason);
  const label = target.symbol || target.label || target.unit_key;
  return (
    <div className="review-target-end-actions" role="group" aria-label={"End actions for " + label}>
      <span className="review-target-end-label">End</span>
      <UiButton
        variant="ghost"
        size="icon"
        type="button"
        aria-label={"Mark " + label + " reviewed from end"}
        tooltip={lock || "Mark reviewed"}
        disabled={busy || target.state === "reviewed"}
        onClick={() => onMark("reviewed", true)}
        className="h-6 w-6 hover:text-emerald-300"
      >
        <CheckCircle2 size={12} aria-hidden="true" />
      </UiButton>
      <UiButton
        variant="ghost"
        size="icon"
        type="button"
        aria-label={"Mark " + label + " needs changes from end"}
        tooltip={lock || "Needs changes"}
        disabled={busy}
        onClick={() => onMark("needs_changes")}
        className="h-6 w-6 hover:text-rose-300"
      >
        <X size={12} aria-hidden="true" />
      </UiButton>
      <UiButton
        variant="ghost"
        size="icon"
        type="button"
        aria-label={"Comment on " + label + " from end"}
        tooltip={lock || "Add comment"}
        disabled={busy}
        onClick={onComment}
        className="h-6 w-6 hover:text-sky-300"
      >
        <MessageSquareText size={12} aria-hidden="true" />
      </UiButton>
      {onAsk && (
        <UiButton
          variant="ghost"
          size="icon"
          type="button"
          aria-label={"Ask about " + label + " from end"}
          tooltip={lock || "Ask about this code"}
          disabled={busy}
          onClick={onAsk}
          className="h-6 w-6 hover:text-violet-300"
        >
          <Sparkles size={11} aria-hidden="true" />
        </UiButton>
      )}
      <UiButton
        variant="ghost"
        size="icon"
        type="button"
        aria-label={"Open context for " + label + " from end"}
        tooltip="Open context"
        onClick={onContext}
        className="h-6 w-6"
      >
        <PanelRight size={11} aria-hidden="true" />
      </UiButton>
    </div>
  );
}

/**
 * The diff's line selection mirrors the open draft: the selected lines stay
 * highlighted while the reviewer writes, and clear once the draft is sent or
 * cancelled instead of lingering over an already-saved comment.
 */
function draftSelection(draft: Draft | null, path: string): SelectedLineRange | null {
  if (!draft || draft.path !== path || draft.fileLevel || draft.startLine < 1) return null;
  return {
    start: draft.startLine,
    end: Math.max(draft.startLine, draft.endLine),
    side: draft.side,
    endSide: draft.side,
  };
}

type CodeTextPoint = {
  itemPath: string;
  host: Element | null;
  lineNumber: number;
  side: "additions" | "deletions";
};

/**
 * Pierre starts line selection only from the line-number gutter, so a drag
 * across code text is a native text selection it never reports. Resolve the
 * code row under a pointer event so that gesture can reuse the range actions.
 * Like Pierre, an annotation row maps to its line only for the end point.
 */
function codeTextPoint(eventPath: readonly EventTarget[], allowAnnotation: boolean): CodeTextPoint | null {
  let row: HTMLElement | null = null;
  let host: Element | null = null;
  for (const node of eventPath) {
    if (!(node instanceof HTMLElement)) continue;
    if (!row) {
      if (node.hasAttribute("data-column-number")) return null;
      if (node.hasAttribute("data-line")) {
        row = node;
      } else if (allowAnnotation && node.hasAttribute("data-line-annotation")) {
        const previous = node.previousElementSibling;
        if (previous instanceof HTMLElement && previous.hasAttribute("data-line")) row = previous;
      }
      continue;
    }
    if (!host && node.localName === "diffs-container") host = node;
    const itemPath = node.getAttribute("data-review-stream-item");
    if (itemPath == null) continue;
    const lineNumber = Number.parseInt(row.getAttribute("data-line") ?? "", 10);
    if (!Number.isFinite(lineNumber) || lineNumber < 1) return null;
    const lineType = row.getAttribute("data-line-type") ?? "";
    const side = lineType === "change-deletion"
      ? "deletions"
      : lineType === "change-addition"
        ? "additions"
        : row.closest("[data-code]")?.hasAttribute("data-deletions") ? "deletions" : "additions";
    return { itemPath, host, lineNumber, side };
  }
  return null;
}

type ContextLoadState = "loading" | "error";

/**
 * The first expand of a file's unchanged context fetches the whole file, which
 * can take seconds on a busy server, and Pierre only logs a failure. Say both
 * on the file's collapsed-context rows; clicking one again retries the fetch.
 */
function markContextLoad(node: HTMLElement, state: ContextLoadState | undefined): void {
  const root: ParentNode = node.shadowRoot ?? node;
  for (const separator of root.querySelectorAll<HTMLElement>("[data-separator][data-expand-index]")) {
    separator.querySelector("[data-review-context-status]")?.remove();
    separator.removeAttribute("aria-busy");
    const label = separator.querySelector<HTMLElement>("[data-unmodified-lines]");
    if (!state || !label) continue;
    const status = document.createElement("span");
    status.dataset.reviewContextStatus = state;
    status.setAttribute("role", state === "error" ? "alert" : "status");
    status.textContent = state === "loading" ? "Loading context…" : "Couldn't load context · click to retry";
    status.style.marginLeft = "0.75rem";
    status.style.opacity = state === "loading" ? "0.75" : "1";
    if (state === "error") status.style.color = "#f43f5e";
    label.after(status);
    if (state === "loading") separator.setAttribute("aria-busy", "true");
  }
}

function hasCodeTextSelection(host: Element | null): boolean {
  // Chromium exposes shadow-scoped selections only through ShadowRoot.
  const root = host?.shadowRoot as (ShadowRoot & { getSelection?: () => Selection | null }) | null | undefined;
  const selection = root?.getSelection?.() ?? document.getSelection();
  return Boolean(selection?.toString().trim());
}

// Review targets render as compact actionable headers at their source anchor;
// real discussion/proposal content shares the same annotation row when needed.
// Scope overlays remain complementary coverage context rather than replacing
// the controls needed to judge, comment on, or ask about a target.
/**
 * The last line of start..end that the diff renders without expanding context
 * (its hunks), or 0 when the whole range is collapsed unchanged context. A
 * symbol-level comment ends on the symbol's last line, which is often hidden.
 */
function renderedHunkLine(fileDiff: ParsedFileDiff): MarkerDisplayLine {
  return (side, startLine, endLine) => {
    let best = 0;
    for (const hunk of fileDiff.hunks ?? []) {
      const first = side === "additions" ? hunk.additionStart : hunk.deletionStart;
      const count = side === "additions" ? hunk.additionCount : hunk.deletionCount;
      const last = Math.min(endLine, first + count - 1);
      if (count > 0 && last >= Math.max(startLine, first)) best = Math.max(best, last);
    }
    return best;
  };
}

function buildMarkers(
  path: string,
  targets: readonly ReviewTarget[],
  annotations: Annotation[],
  draft: Draft | null,
  displayLine?: MarkerDisplayLine,
): { side: "additions" | "deletions"; lineNumber: number; metadata: StreamMarkerMeta }[] {
  const markers = new Map<string, { side: "additions" | "deletions"; lineNumber: number; metadata: StreamMarkerMeta }>();

  const ensure = (side: "additions" | "deletions", lineNumber: number) => {
    const key = markerKey(side, lineNumber);
    let row = markers.get(key);
    if (!row) {
      row = { side, lineNumber, metadata: { key, targetIds: [], targetEndIds: [], commentKeys: [] } };
      markers.set(key, row);
    }
    return row;
  };

  for (const target of targets) {
    const location = targetHeaderLocation(target);
    ensure(location.side, location.lineNumber).metadata.targetIds.push(target.target_id);
    if (target.start_line > 0 && target.end_line > target.start_line) {
      ensure(location.side, target.end_line).metadata.targetEndIds.push(target.target_id);
    }
  }

  for (const marker of markerLines(annotations, path, draft, displayLine)) {
    ensure(marker.side, marker.lineNumber).metadata.commentKeys.push(marker.metadata.key);
  }

  // A below-threshold finding is not surfaced, so it never makes the file-level row exist on its own.
  const fileLevel = fileComments(annotations, path).filter((item) => !item.below_threshold);
  const fileDraft = draft?.path === path && draft.fileLevel;
  if (fileLevel.length > 0 || fileDraft) ensure("additions", 0);

  return [...markers.values()].sort((a, b) => a.lineNumber - b.lineNumber || a.side.localeCompare(b.side));
}

function refusalText(detail: FileDetail | undefined, error: string | undefined, path: string): string {
  if (error) return `Could not load this file.\n\n${error}\n\nOther files in the review remain available.`;
  if (!detail) return `LemonCrow has not loaded ${path} yet.`;
  const title = detail.refusal ? detail.refusal.replaceAll("_", " ") : "diff unavailable";
  return `Diff unavailable: ${title}\n\n${detail.detail || "This recorded Snapshot does not contain enough text to render a reliable diff."}`;
}

function targetScopeSignature(targets: readonly ReviewTarget[]): string {
  return targets.map((target) => {
    const location = targetScrollLocation(target) ?? { side: "additions" as const, lineNumber: 0 };
    const spans = target.spans
      .map((span) => `${span.side}:${span.start_line}-${span.end_line}:${span.hunk_ordinal}`)
      .join(",");
    return `${target.target_id}@${location.side}:${location.lineNumber}[${spans}]`;
  }).join("|");
}

function nextItemRevision(
  cache: Map<string, ItemRevisionEntry>,
  path: string,
  type: "diff" | "file" | "markdown" | "csv" | "media" | "linked-surface" | "special",
  source: string,
  markers: string,
  collapsed: boolean,
): number {
  const previous = cache.get(path);
  if (
    previous
    && previous.type === type
    && previous.source === source
    && previous.markers === markers
    && previous.collapsed === collapsed
  ) return previous.version;
  const version = (previous?.version ?? 0) + 1;
  cache.set(path, { version, type, source, markers, collapsed });
  return version;
}

function parsedFileDiff(
  cache: Map<string, ParsedDiffCacheEntry>,
  path: string,
  patch: string,
): ParsedFileDiff | null {
  const previous = cache.get(path);
  if (previous?.patch === patch) return previous.fileDiff;
  const version = (previous?.version ?? 0) + 1;
  try {
    const fileDiff = parsePatchFiles(patch, `lc:${path}`, false)[0]?.files[0] ?? null;
    cache.set(path, { patch, fileDiff, version });
    return fileDiff;
  } catch {
    cache.set(path, { patch, fileDiff: null, version });
    return null;
  }
}

function markdownEstimatedHeight(detail: FileDetail, mode: MarkdownRenderMode): number {
  const preview = detail.preview;
  if (preview?.kind !== "markdown") return 480;
  const lineCount = mode === "compare"
    ? Math.max(preview.old_content.split("\n").length, preview.new_content.split("\n").length)
    : (detail.status === "deleted" ? preview.old_content : preview.new_content).split("\n").length;
  // Only a first-layout estimate. CodeView measures the mounted custom block
  // immediately and corrects this without dropping it out of the stream.
  return Math.max(320, Math.min(30_000, 220 + lineCount * 24));
}

function itemFor(
  path: string,
  detail: FileDetail | undefined,
  error: string | undefined,
  targets: ReviewTarget[],
  annotations: Annotation[],
  draft: Draft | null,
  comparisonMode: boolean,
  collapsed: boolean,
  markdownMode: MarkdownRenderMode | "source",
  diffCache: Map<string, ParsedDiffCacheEntry>,
  revisionCache: Map<string, ItemRevisionEntry>,
): StreamItem | null {
  if (!detail && !error) return null;
  if (detail?.special_change && !comparisonMode) {
    const change = detail.special_change;
    return {
      id: path,
      type: "special",
      change,
      patch: detail.patch,
      version: nextItemRevision(revisionCache, path, "special", `${JSON.stringify(change)}#${detail.patch}`, "", collapsed),
      estimatedHeight: collapsed ? 44 : 180,
      collapsed,
    };
  }
  if (detail?.preview?.kind === "markdown" && markdownMode !== "source") {
    const signature = `${markerSignature(annotations, path, draft)}#${targetScopeSignature(targets)}#markdown-${markdownMode}`;
    return {
      id: path,
      type: "markdown",
      version: nextItemRevision(
        revisionCache,
        path,
        "markdown",
        `${detail.patch}#${markdownMode}`,
        signature,
        collapsed,
      ),
      estimatedHeight: collapsed ? 44 : markdownEstimatedHeight(detail, markdownMode),
      mode: markdownMode,
      collapsed,
    };
  }
  if (detail?.preview?.kind === "csv" && markdownMode !== "source") {
    const signature = `${markerSignature(annotations, path, draft)}#${targetScopeSignature(targets)}#csv-${markdownMode}`;
    return {
      id: path,
      type: "csv",
      version: nextItemRevision(
        revisionCache,
        path,
        "csv",
        `${detail.patch}#${markdownMode}`,
        signature,
        collapsed,
      ),
      estimatedHeight: collapsed ? 44 : 620,
      mode: markdownMode,
      collapsed,
    };
  }
  if (detail?.preview?.kind === "media") {
    const signature = `${markerSignature(annotations, path, draft)}#${targetScopeSignature(targets)}#media-${markdownMode}`;
    return {
      id: path,
      type: "media",
      version: nextItemRevision(
        revisionCache,
        path,
        "media",
        `${detail.status}#${detail.preview.media_type}#${markdownMode}`,
        signature,
        collapsed,
      ),
      estimatedHeight: collapsed ? 44 : 680,
      mode: markdownMode,
      collapsed,
    };
  }
  if (detail?.renderable && detail.patch) {
    const fileDiff = parsedFileDiff(diffCache, path, detail.patch);
    if (fileDiff) {
      const markers = comparisonMode ? [] : buildMarkers(path, targets, annotations, draft, renderedHunkLine(fileDiff));
      const signature = `${markerSignature(annotations, path, draft)}#${targetScopeSignature(targets)}`;
      const item: DiffStreamItem = {
        id: path,
        type: "diff",
        fileDiff,
        patchVersion: diffCache.get(path)?.version ?? 0,
        version: nextItemRevision(revisionCache, path, "diff", detail.patch, signature, collapsed),
        annotations: markers,
        collapsed,
        estimatedHeight: collapsed
          ? 44
          : Math.max(160, Math.min(18_000, 76 + detail.patch.split("\n").length * 21)),
      };
      return item;
    }
  }
  const contents = refusalText(detail, error, path);
  const item: FileStreamItem = {
    id: path,
    type: "file",
    file: { name: path, contents },
    failed: Boolean(error),
    version: nextItemRevision(revisionCache, path, "file", contents, "", collapsed),
    collapsed,
    estimatedHeight: collapsed ? 44 : Math.max(140, 76 + contents.split("\n").length * 21),
  };
  return item;
}
function browserScrollBehavior(behavior: "instant" | "smooth"): ScrollBehavior {
  return motionSafeScrollBehavior(behavior) === "smooth" ? "smooth" : "auto";
}

function configuredSurfaceName(surface: ReviewSurfaceInfo): string {
  const configured = surface.metadata.surface_id;
  if (typeof configured === "string" && configured) return configured;
  const service = surface.metadata.service;
  if (typeof service === "string" && service) return service;
  return surface.runtime || surface.provider;
}

const STORYBOOK_EXPORT_DECL_RE = /^\s*export\s+(?:const|let|var|function\*?)\s+([A-Za-z_$][\w$]*)/;

function changedStorybookExports(patch: string): ReadonlySet<string> {
  const changed = new Set<string>();
  let currentExport = "";
  for (const row of patch.split("\n")) {
    if (row.startsWith("@@")) {
      currentExport = "";
      continue;
    }
    if (row.startsWith("+++") || row.startsWith("---")) continue;
    const prefix = row[0];
    if (prefix !== "+" && prefix !== " ") continue;
    const line = row.slice(1);
    const declaration = STORYBOOK_EXPORT_DECL_RE.exec(line);
    if (declaration) currentExport = declaration[1];
    // Ignore separator-only additions between stories. Without this, adding a
    // blank line immediately before a new export would incorrectly mark the
    // preceding story as changed and alphabetical fallback could win again.
    if (prefix === "+" && line.trim() && currentExport) changed.add(currentExport);
  }
  return changed;
}

function surfaceAutoRankable(surface: ReviewSurfaceInfo): boolean {
  if (surface.affected_paths.length === 0) return false;
  if (surface.comparison) {
    return surface.comparison.status !== "unavailable" && surface.comparison.auto;
  }
  // Backward-compatible inference for tests and older servers: a two-sided
  // preview is already sandbox-renderable and therefore safe to compare.
  if (surface.preview) {
    return surface.preview.status !== "unavailable"
      && surface.preview.sides.includes("old")
      && surface.preview.sides.includes("new");
  }
  // Older servers did not advertise comparison metadata for Bruno. Preserve
  // safe read-only inference while never auto-running mutation methods.
  if (surface.kind !== "api.request" || !surface.runtime || !surface.capabilities.includes("execute")) return false;
  const method = String(surface.metadata.method ?? "").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) return false;
  const url = String(surface.metadata.url ?? surface.locator ?? "").replace("{{baseUrl}}", "");
  return !url.includes("{{") && !/\/:([^/?]+)/.test(url);
}

const diffSurfaceStyle = {
  width: "100%",
  "--diffs-font-size": "13px",
  "--diffs-line-height": "21px",
} as CSSProperties;

const ReviewStream = forwardRef<ReviewStreamHandle, ReviewStreamProps>(function ReviewStream(
  {
    targets,
    details,
    surfaces = [],
    showReviewPreviews = true,
    onOpenPreviews,
    dedicatedItem = null,
    surfaceRuns = {},
    surfaceBusy = NO_BUSY_SURFACES,
    revisionId = "",
    errors,
    annotations,
    draft,
    proposalSelection = null,
    activeProposal = null,
    proposalBusy = false,
    sourceMutationSupported = false,
    comparisonMode = false,
    snapshotComparison = null,
    diffStyle,
    diffOverflow = "scroll",
    codeTheme = "lemoncrow",
    chromeTheme = "light",
    activeTargetId,
    busy,
    loadingPaused = busy,
    remainingFileCount,
    autoLoadMore = true,
    pendingVisibleFileCount = 0,
    readyHiddenFileCount = 0,
    preparingHiddenFileCount = 0,
    pagination,
    outstandingByPath,
    onActiveTarget,
    onDraft,
    onSuggestEdit = () => {},
    onEditSource = () => {},
    onQuickEdit = () => {},
    onCreateProposal = () => {},
    onCreateAndApplyProposal = () => {},
    onApplyProposal = () => {},
    onCloseProposal = () => {},
    onCreate,
    onSetAnnotationState,
    onFindingDisposition = () => {},
    onMark,
    onComment,
    onAsk,
    onContext,
    onAskSelection,
    onBulkReview,
    onLoadMore,
    onFirstContentReady,
    onRunSurface,
    onValidateSurface,
    onCancelRuntimeValidation,
    runtimeValidations = {},
    runtimeValidationBusy = new Set(),
    onRetryPath,
    onOpenInCode,
    fileSemantics,
  },
  ref,
) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const checkLoadMoreRef = useRef<(() => void) | null>(null);
  const horizontalDockRef = useRef<HTMLDivElement>(null);
  const firstContentRevisionRef = useRef("");
  const selectionMenuRef = useRef<HTMLDivElement>(null);
  const lastPointerRef = useRef({ clientX: 0, clientY: 0 });
  const suppressNextSelectionEndRef = useRef(false);
  const suppressNextSelectionCommitRef = useRef(false);
  const lastSelectionEndKeyRef = useRef("");
  const pendingSingleLineActionRef = useRef<number | null>(null);
  const codeTextSelectionStartRef = useRef<CodeTextPoint | null>(null);
  const scrollFocusFrameRef = useRef<number | null>(null);
  const scopeFrameByPathRef = useRef(new Map<string, number>());
  const targetLineAnchorsRef = useRef(new Map<string, HTMLElement>());
  const activeScopePathRef = useRef("");
  const activeHorizontalScrollersRef = useRef<HTMLElement[]>([]);
  const diffNodesRef = useRef(new Map<string, HTMLElement>());
  // Per file: a context fetch in flight, or a failed one awaiting a retry.
  // Cleared on success and whenever the file's renderer remounts.
  const contextLoadRef = useRef(new Map<string, ContextLoadState>());
  const expandedSourceRef = useRef(new Map<string, { revisionId: string; text: string }>());
  const itemElementsRef = useRef(new Map<string, HTMLElement>());
  const diffOptionsCacheRef = useRef(new Map<string, { diffStyle: DiffStyle; diffOverflow: DiffOverflow; codeTheme: ReviewCodeTheme; chromeTheme: Theme; revisionKey: string; options: any }>());
  const parsedDiffCacheRef = useRef(new Map<string, ParsedDiffCacheEntry>());
  const itemRevisionCacheRef = useRef(new Map<string, ItemRevisionEntry>());
  const targetsRef = useRef(targets);
  const activeTargetIdRef = useRef(activeTargetId);
  const onActiveTargetRef = useRef(onActiveTarget);
  const onDraftRef = useRef(onDraft);
  const onQuickEditRef = useRef(onQuickEdit);
  const draftRef = useRef(draft);
  const committedDraftPathRef = useRef("");
  targetsRef.current = targets;
  activeTargetIdRef.current = activeTargetId;
  onActiveTargetRef.current = onActiveTarget;
  onDraftRef.current = onDraft;
  onQuickEditRef.current = onQuickEdit;
  draftRef.current = draft;

  const [collapsedPaths, setCollapsedPaths] = useState<ReadonlySet<string>>(new Set());
  // Generated/vendor diffs start collapsed behind their provenance card, once
  // per path, exactly where the projection collapses them by default; the
  // reviewer's own expand/collapse wins from then on.
  const seededSecondaryPathsRef = useRef(new Set<string>());
  const fileRoles = fileSemantics?.roles;
  useEffect(() => {
    if (!fileRoles || comparisonMode || dedicatedItem?.kind === "file") return;
    const fresh = [...fileRoles]
      .filter(([path, role]) => role.role === "generated" && role.collapsedDefault && !seededSecondaryPathsRef.current.has(path))
      .map(([path]) => path);
    if (fresh.length === 0) return;
    for (const path of fresh) seededSecondaryPathsRef.current.add(path);
    setCollapsedPaths((current) => new Set([...current, ...fresh]));
  }, [comparisonMode, dedicatedItem?.kind, fileRoles]);
  const [peekedTargetIds, setPeekedTargetIds] = useState<ReadonlySet<string>>(new Set());
  const [expandedJudgedPaths, setExpandedJudgedPaths] = useState<ReadonlySet<string>>(new Set());
  const previousActiveTargetIdRef = useRef(activeTargetId);
  const [diffNodeVersion, setDiffNodeVersion] = useState(0);
  const [horizontalScrollRange, setHorizontalScrollRange] = useState(0);
  const [viewportWidth, setViewportWidth] = useState(0);
  const [lineSelections, setLineSelections] = useState<Record<string, SelectedLineRange | null>>({});
  const [selectionAction, setSelectionAction] = useState<SelectionActionState | null>(null);
  const [selectionMenuSize, setSelectionMenuSize] = useState({ width: 0, height: 0 });
  const [markdownModes, setMarkdownModes] = useState<Record<string, MarkdownRenderMode | "source">>({});
  const [markdownPreviewThemes, setMarkdownPreviewThemes] = useState<Record<string, MarkdownPreviewTheme>>({});
  const [surfaceModes, setSurfaceModes] = useState<Record<string, MarkdownRenderMode>>({});
  const [fileSurfaceModes, setFileSurfaceModes] = useState<Record<string, "source" | SurfacePreviewMode>>({});
  const [selectedFileSurfaceKeys, setSelectedFileSurfaceKeys] = useState<Record<string, string>>({});
  const initialDedicatedSurfaceKey = dedicatedItem?.kind === "surface" ? `${dedicatedItem.provider}:${dedicatedItem.id}` : "";
  const [selectedWebSurfaceKey, setSelectedWebSurfaceKey] = useState(initialDedicatedSurfaceKey);
  const [selectedApiSurfaceKey, setSelectedApiSurfaceKey] = useState(initialDedicatedSurfaceKey);
  const [surfaceImpactScores, setSurfaceImpactScores] = useState<Record<string, SurfaceImpactScore>>({});
  const [surfaceImpactBusy, setSurfaceImpactBusy] = useState(false);
  const [selectedPluginSurfaceKeys, setSelectedPluginSurfaceKeys] = useState<Record<string, string>>({});
  const [activeSurfaceKey, setActiveSurfaceKey] = useState(initialDedicatedSurfaceKey);
  const surfaceImpactInflightRef = useRef(new Map<string, Promise<SurfaceImpactScore>>());
  const activeSurfaceKeyRef = useRef(activeSurfaceKey);
  activeSurfaceKeyRef.current = activeSurfaceKey;
  const loadSurfaceImpact = useCallback((surface: ReviewSurfaceInfo, attempts = 3): Promise<SurfaceImpactScore> => {
    if (!revisionId) return Promise.reject(new Error("surface impact requires a revision"));
    const surfaceKey = `${surface.provider}:${surface.id}`;
    const inflightKey = `${revisionId}\u0000${surfaceKey}`;
    const pending = surfaceImpactInflightRef.current.get(inflightKey);
    if (pending) return pending;
    const request = fetchSurfaceImpactWithRetry(
      currentReviewId(), surface.provider, surface.id, revisionId, attempts,
    ).then((score) => {
      setSurfaceImpactScores((current) => current[surfaceKey] === score ? current : { ...current, [surfaceKey]: score });
      return score;
    }).finally(() => {
      surfaceImpactInflightRef.current.delete(inflightKey);
    });
    surfaceImpactInflightRef.current.set(inflightKey, request);
    return request;
  }, [revisionId]);
  const activeMarkdownPathRef = useRef("");
  const setLineSelection = useCallback((path: string, range: SelectedLineRange | null) => {
    setLineSelections((current) => {
      const previous = current[path] ?? null;
      const same = previous === range || Boolean(
        previous && range
        && previous.start === range.start
        && previous.end === range.end
        && previous.side === range.side
        && previous.endSide === range.endSide,
      );
      if (same) return current;
      if (range === null) {
        if (!(path in current)) return current;
        const next = { ...current };
        delete next[path];
        return next;
      }
      return { ...current, [path]: range };
    });
  }, []);

  useEffect(() => {
    const previousPath = committedDraftPathRef.current;
    const nextPath = draft && !draft.fileLevel && draft.startLine > 0 ? draft.path : "";
    if (previousPath && previousPath !== nextPath) setLineSelection(previousPath, null);
    committedDraftPathRef.current = nextPath;
  }, [draft?.path, draft?.fileLevel, draft?.startLine, setLineSelection]);

  const cancelPendingSingleLineAction = useCallback(() => {
    if (pendingSingleLineActionRef.current == null) return;
    window.clearTimeout(pendingSingleLineActionRef.current);
    pendingSingleLineActionRef.current = null;
  }, []);

  const dismissSelectionAction = useCallback((clearSelection = true) => {
    cancelPendingSingleLineAction();
    if (clearSelection && selectionAction) setLineSelection(selectionAction.path, null);
    setSelectionAction(null);
  }, [cancelPendingSingleLineAction, selectionAction, setLineSelection]);

  useEffect(() => () => cancelPendingSingleLineAction(), [cancelPendingSingleLineAction]);

  const openDraftPreservingViewport = useCallback((nextDraft: Draft) => {
    const root = scrollRef.current;
    const item = itemElementsRef.current.get(nextDraft.path);
    const beforeLine = item && !nextDraft.fileLevel && nextDraft.startLine > 0
      ? renderedDiffLine(item, nextDraft.side, nextDraft.startLine)
      : null;
    const beforeTop = beforeLine?.getBoundingClientRect().top;
    onDraftRef.current(nextDraft);
    window.requestAnimationFrame(() => {
      if (draftRef.current?.path !== nextDraft.path) {
        setLineSelection(nextDraft.path, null);
        return;
      }
      if (!root || beforeTop == null) return;
      const nextItem = itemElementsRef.current.get(nextDraft.path);
      const afterLine = nextItem ? renderedDiffLine(nextItem, nextDraft.side, nextDraft.startLine) : null;
      if (!afterLine) return;
      const delta = afterLine.getBoundingClientRect().top - beforeTop;
      if (Math.abs(delta) > 0.5) root.scrollTop += delta;
    });
  }, [setLineSelection]);

  useLayoutEffect(() => {
    if (!selectionAction) {
      setSelectionMenuSize((current) => current.width === 0 && current.height === 0 ? current : { width: 0, height: 0 });
      return;
    }
    const menu = selectionMenuRef.current;
    if (!menu) return;
    const rect = menu.getBoundingClientRect();
    setSelectionMenuSize((current) => (
      Math.abs(current.width - rect.width) < 0.5 && Math.abs(current.height - rect.height) < 0.5
        ? current
        : { width: rect.width, height: rect.height }
    ));
  }, [selectionAction]);

  useEffect(() => {
    if (!selectionAction) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && selectionMenuRef.current?.contains(target)) return;
      dismissSelectionAction();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        dismissSelectionAction();
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const eventTarget = event.target;
      if (
        eventTarget instanceof HTMLElement
        && (eventTarget.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(eventTarget.tagName))
      ) return;

      const selected = selectionAction;
      const key = event.key.toLowerCase();
      const selectedTarget = selected.draft.targetUnitKey
        ? targetsRef.current.find((target) => target.unit_key === selected.draft.targetUnitKey)
        : targetAtLine(
            targetsRef.current,
            selected.path,
            selected.draft.side,
            selected.draft.startLine,
            activeTargetIdRef.current,
          );
      const finish = () => {
        setSelectionAction(null);
        setLineSelection(selected.path, null);
      };

      if (key === "c") {
        event.preventDefault();
        setSelectionAction(null);
        openDraftPreservingViewport(selected.draft);
      } else if (key === "a") {
        event.preventDefault();
        finish();
        onAskSelection?.(selected.draft);
      } else if (key === "r" && selectedTarget && !busy && selectedTarget.state !== "reviewed") {
        event.preventDefault();
        finish();
        onMark(selectedTarget, "reviewed", true);
      } else if (key === "x" && selectedTarget && !busy) {
        event.preventDefault();
        finish();
        onMark(selectedTarget, "needs_changes", false);
      } else if (key === "s" && selected.draft.side === "additions") {
        event.preventDefault();
        setSelectionAction(null);
        onSuggestEdit(selected.draft);
      } else if (key === "e" && selected.draft.side === "additions" && sourceMutationSupported) {
        event.preventDefault();
        setSelectionAction(null);
        onEditSource(selected.draft);
      }
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [
    busy,
    dismissSelectionAction,
    onAskSelection,
    onEditSource,
    onMark,
    onSuggestEdit,
    openDraftPreservingViewport,
    selectionAction,
    setLineSelection,
    sourceMutationSupported,
  ]);

  // A drag selection outranks the draft's own range (it must stay the same
  // object for @pierre/diffs), so drop it once the draft moves elsewhere in the
  // same file, e.g. `c` on another target; otherwise the highlight disagrees
  // with the composer.
  useEffect(() => {
    if (!draft || draft.fileLevel || draft.startLine < 1) return;
    const start = draft.startLine;
    const end = Math.max(draft.startLine, draft.endLine);
    setLineSelections((current) => {
      const selection = current[draft.path];
      if (!selection) return current;
      const matches = Math.min(selection.start, selection.end) === start
        && Math.max(selection.start, selection.end) === end
        && markerSide(selection.side ?? "additions") === draft.side;
      if (matches) return current;
      const next = { ...current };
      delete next[draft.path];
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.path, draft?.fileLevel, draft?.startLine, draft?.endLine, draft?.side]);

  const themeDefinition = reviewCodeThemeDefinition(codeTheme);
  const resolvedDiffStyle = resolveDiffStyle(diffStyle, viewportWidth);

  useEffect(() => {
    const node = viewportRef.current;
    if (!node) return;
    const measure = () => {
      const width = node.getBoundingClientRect().width || node.clientWidth;
      setViewportWidth((current) => Math.abs(current - width) > 0.5 ? width : current);
    };
    measure();
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(measure);
      observer.observe(node);
      return () => observer.disconnect();
    }
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);
  const inlineAnnotations = useMemo(() => annotations.filter(showInlineAnnotation), [annotations]);
  const files = useMemo(() => groupReaderFiles(targets), [targets]);
  const fileByPath = useMemo(() => new Map(files.map((file) => [file.path, file])), [files]);
  const targetsById = useMemo(() => new Map(targets.map((target) => [target.target_id, target])), [targets]);
  const activeDiffPath = targetsById.get(activeTargetId)?.path ?? "";

  const findingDraft = useCallback((comment: Annotation): Draft => ({
    path: comment.path,
    startLine: comment.start_line,
    endLine: comment.end_line,
    side: markerSide(comment.side),
    fileLevel: Boolean(comment.file_level),
    targetUnitKey: comment.unit_key || undefined,
    body: comment.title ? `${comment.title}\n\n${comment.body}` : comment.body,
    kind: "request_change",
    markTarget: true,
  }), []);

  const requestFindingChange = useCallback((comment: Annotation) => {
    onFindingDisposition(comment.id, "confirmed_by_reviewer");
    onDraft(findingDraft(comment));
  }, [findingDraft, onDraft, onFindingDisposition]);

  const askAboutFinding = useCallback((comment: Annotation) => {
    const draft = findingDraft(comment);
    onAskSelection?.({ ...draft, kind: "comment", markTarget: false });
  }, [findingDraft, onAskSelection]);

  const fixFinding = useCallback((comment: Annotation) => {
    const draft = findingDraft(comment);
    onFindingDisposition(comment.id, "confirmed_by_reviewer");
    onSuggestEdit({ ...draft, mode: "proposal", sourceAction: "suggest", kind: "comment", markTarget: false });
  }, [findingDraft, onFindingDisposition, onSuggestEdit]);

  const registerDiffNode = useCallback((path: string, node: HTMLElement, phase: "mount" | "update" | "unmount") => {
    for (const target of targetsRef.current) {
      if (target.path === path) targetLineAnchorsRef.current.delete(target.target_id);
    }
    if (phase === "unmount") {
      if (diffNodesRef.current.get(path) === node) {
        diffNodesRef.current.delete(path);
        setDiffNodeVersion((value) => value + 1);
      }
      return;
    }
    if (diffNodesRef.current.get(path) !== node) {
      diffNodesRef.current.set(path, node);
      setDiffNodeVersion((value) => value + 1);
    }
  }, []);

  const onHorizontalDockScroll = useCallback(() => {
    const dock = horizontalDockRef.current;
    if (!dock) return;
    for (const scroller of activeHorizontalScrollersRef.current) {
      if (Math.abs(scroller.scrollLeft - dock.scrollLeft) > 0.5) scroller.scrollLeft = dock.scrollLeft;
    }
  }, []);

  useEffect(() => {
    let frame = 0;
    let attempts = 0;
    let resizeObserver: ResizeObserver | null = null;
    let mutationObserver: MutationObserver | null = null;
    let boundScrollers: HTMLElement[] = [];

    function onCodeScroll(event: Event) {
      const scroller = event.currentTarget as HTMLElement;
      const dock = horizontalDockRef.current;
      if (!dock) return;
      const next = scroller.scrollLeft;
      if (Math.abs(dock.scrollLeft - next) <= 0.5) return;
      dock.scrollLeft = next;
      for (const peer of activeHorizontalScrollersRef.current) {
        if (peer !== scroller && Math.abs(peer.scrollLeft - next) > 0.5) peer.scrollLeft = next;
      }
    }

    const clear = () => {
      for (const scroller of boundScrollers) {
        scroller.removeEventListener("scroll", onCodeScroll);
        restoreInnerScrollbarChrome(scroller);
      }
      boundScrollers = [];
      resizeObserver?.disconnect();
      mutationObserver?.disconnect();
      resizeObserver = null;
      mutationObserver = null;
      activeHorizontalScrollersRef.current = [];
    };

    const measure = () => {
      const overflow = boundScrollers.filter((scroller) => scroller.scrollWidth - scroller.clientWidth > 1);
      activeHorizontalScrollersRef.current = overflow;
      const primary = overflow.reduce<HTMLElement | null>((best, scroller) => {
        if (!best) return scroller;
        return scroller.scrollWidth - scroller.clientWidth > best.scrollWidth - best.clientWidth ? scroller : best;
      }, null);
      const range = primary ? Math.max(0, primary.scrollWidth - primary.clientWidth) : 0;
      setHorizontalScrollRange((current) => Math.abs(current - range) > 0.5 ? range : current);
      if (primary && horizontalDockRef.current && Math.abs(horizontalDockRef.current.scrollLeft - primary.scrollLeft) > 0.5) {
        horizontalDockRef.current.scrollLeft = primary.scrollLeft;
      }
    };

    const bind = () => {
      clear();
      if (!activeDiffPath) {
        setHorizontalScrollRange(0);
        return;
      }
      const node = diffNodesRef.current.get(activeDiffPath);
      const scrollers = node ? diffCodeScrollers(node) : [];
      if (!node || scrollers.length === 0) {
        if (attempts < 8) {
          attempts += 1;
          frame = requestAnimationFrame(bind);
        } else {
          setHorizontalScrollRange(0);
        }
        return;
      }
      boundScrollers = scrollers;
      for (const scroller of scrollers) {
        hideInnerScrollbarChrome(scroller);
        scroller.addEventListener("scroll", onCodeScroll, { passive: true });
      }
      if (typeof ResizeObserver !== "undefined") {
        resizeObserver = new ResizeObserver(measure);
        for (const scroller of scrollers) resizeObserver.observe(scroller);
      }
      const root = node.shadowRoot ?? node;
      if (typeof MutationObserver !== "undefined") {
        mutationObserver = new MutationObserver(measure);
        mutationObserver.observe(root, { childList: true, subtree: true, characterData: true });
      }
      measure();
    };

    frame = requestAnimationFrame(bind);
    return () => {
      cancelAnimationFrame(frame);
      clear();
    };
  }, [activeDiffPath, codeTheme, diffNodeVersion, diffOverflow, resolvedDiffStyle]);
  const targetsByPath = useMemo(() => {
    const map = new Map<string, ReviewTarget[]>();
    for (const target of targets) {
      const rows = map.get(target.path) ?? [];
      rows.push(target);
      map.set(target.path, rows);
    }
    return map;
  }, [targets]);
  const draftTargetId = useMemo(() => {
    if (!draft?.targetUnitKey) return "";
    return targets.find((target) => target.unit_key === draft.targetUnitKey)?.target_id ?? "";
  }, [draft?.targetUnitKey, targets]);
  // An area judged while it is the active one (x, u, its header's buttons) is
  // still what the reviewer is reading: it stays open, header current, until
  // they move on. Folding it (or its whole file) under them took the active
  // area off screen. An area they arrive at already judged folds as before.
  const activeArrivalRef = useRef<{ id: string; judged: boolean } | null>(null);
  if (activeArrivalRef.current?.id !== activeTargetId) {
    const arrivedState = targetsById.get(activeTargetId)?.state;
    activeArrivalRef.current = {
      id: activeTargetId,
      judged: arrivedState === undefined || arrivedState === "reviewed" || arrivedState === "needs_changes",
    };
  }
  const judgedInPlace = activeArrivalRef.current.judged ? undefined : targetsById.get(activeTargetId);
  const judgedInPlaceId = judgedInPlace?.target_id ?? "";
  const judgedInPlacePath = judgedInPlace?.path ?? "";
  const foldedTargetIds = useMemo(() => new Set(
    targets
      .filter((target) => (
        (target.state === "reviewed" || target.state === "needs_changes")
        && targetHasContinuousScope(target, targets)
        && target.target_id !== draftTargetId
        && target.target_id !== judgedInPlaceId
        && !peekedTargetIds.has(target.target_id)
      ))
      .map((target) => target.target_id),
  ), [draftTargetId, judgedInPlaceId, peekedTargetIds, targets]);
  const foldedTargetIdsRef = useRef<ReadonlySet<string>>(foldedTargetIds);
  foldedTargetIdsRef.current = foldedTargetIds;

  const fullyJudgedPaths = useMemo(() => {
    const paths = new Set<string>();
    for (const [path, rows] of targetsByPath) {
      if (rows.length === 0 || !rows.every((target) => target.state === "reviewed" || target.state === "needs_changes")) continue;
      // Search/filtering can leave only a slice of a file in the stream. Never
      // collapse the whole file if the review-wide tally proves hidden work is
      // still outstanding. Visible needs-changes targets are already judged,
      // so discount them from the backend's non-reviewed tally.
      const visibleBackendOutstanding = rows.filter((target) => target.state !== "reviewed").length;
      const wholeOutstanding = outstandingByPath.get(path) ?? visibleBackendOutstanding;
      if (wholeOutstanding > visibleBackendOutstanding) continue;
      paths.add(path);
    }
    return paths;
  }, [outstandingByPath, targetsByPath]);

  useEffect(() => {
    const previous = previousActiveTargetIdRef.current;
    previousActiveTargetIdRef.current = activeTargetId;
    if (!activeTargetId || activeTargetId === previous) return;
    const target = targetsById.get(activeTargetId);
    if (!target || (target.state !== "reviewed" && target.state !== "needs_changes")) return;
    setPeekedTargetIds((current) => {
      if (current.has(activeTargetId)) return current;
      const next = new Set(current);
      next.add(activeTargetId);
      return next;
    });
    setExpandedJudgedPaths((current) => {
      if (current.has(target.path)) return current;
      const next = new Set(current);
      next.add(target.path);
      return next;
    });
  }, [activeTargetId, targetsById]);

  useEffect(() => {
    setPeekedTargetIds((current) => {
      let changed = false;
      const next = new Set(current);
      for (const id of current) {
        const target = targetsById.get(id);
        if (!target || (target.state !== "reviewed" && target.state !== "needs_changes")) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : current;
    });
    setExpandedJudgedPaths((current) => {
      let changed = false;
      const next = new Set(current);
      for (const path of current) {
        if (!fullyJudgedPaths.has(path)) {
          next.delete(path);
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [fullyJudgedPaths, targetsById]);

  const isPathCollapsed = useCallback((path: string) => (
    collapsedPaths.has(path)
    || (
      !comparisonMode
      && dedicatedItem?.kind !== "file"
      && draft?.path !== path
      && judgedInPlacePath !== path
      && fullyJudgedPaths.has(path)
      && !expandedJudgedPaths.has(path)
    )
  ), [collapsedPaths, comparisonMode, dedicatedItem?.kind, draft?.path, expandedJudgedPaths, fullyJudgedPaths, judgedInPlacePath]);

  const setTargetFolded = useCallback((targetId: string, folded: boolean) => {
    setPeekedTargetIds((current) => {
      const next = new Set(current);
      if (folded) next.delete(targetId);
      else next.add(targetId);
      return next;
    });
  }, []);

  // Folding changes heights above the reader's eyes. Capture the first visible
  // row during render (DOM still pre-commit) and restore its screen position
  // after the fold commits. Programmatic scrolls own their own destination.
  const programmaticScrollUntilRef = useRef(0);
  const foldAnchorRef = useRef<{ path: string; line: string; type: string; side: string; header: string; top: number } | null>(null);
  const foldSignatureRef = useRef<{ ids: ReadonlySet<string>; paths: ReadonlySet<string>; collapsed: ReadonlySet<string> } | null>(null);
  {
    const previous = foldSignatureRef.current;
    // By content: these sets are rebuilt on every target update (a mark, a
    // priority refresh), and anchoring then would turn a header that grows a
    // line into a scroll of the stream.
    const sameMembers = (left: ReadonlySet<string>, right: ReadonlySet<string>) => (
      left === right || (left.size === right.size && [...left].every((member) => right.has(member)))
    );
    if (previous && (!sameMembers(previous.ids, foldedTargetIds) || !sameMembers(previous.paths, fullyJudgedPaths) || !sameMembers(previous.collapsed, collapsedPaths))) {
      const root = scrollRef.current;
      foldAnchorRef.current = null;
      if (root && Date.now() >= programmaticScrollUntilRef.current) {
        const rootTop = root.getBoundingClientRect().top;
        // The item at the top of the screen (the map is not in stream order).
        let top: { path: string; item: HTMLElement; top: number } | null = null;
        for (const [path, item] of itemElementsRef.current) {
          if (!item.isConnected) continue;
          const rect = item.getBoundingClientRect();
          if (rect.bottom <= rootTop + 1 || (top && rect.top >= top.top)) continue;
          top = { path, item, top: rect.top };
        }
        if (top) {
          const { path, item } = top;
          // The topmost diff row or area header still on screen. A folded area
          // is only its header: expanding one in view keeps that header, and
          // the reviewer's click, where they are, instead of pinning the rows
          // below it and pushing the opened code up off the screen.
          let anchor: { element: HTMLElement; top: number } | null = null;
          const consider = (element: HTMLElement): boolean => {
            if (element.getClientRects().length === 0) return false;
            const elementTop = element.getBoundingClientRect().top;
            if (elementTop < rootTop) return false;
            if (!anchor || elementTop < anchor.top) anchor = { element, top: elementTop };
            return true;
          };
          for (const host of item.querySelectorAll<HTMLElement>("diffs-container")) {
            // Rows run top to bottom within each column: its first one on screen is its topmost.
            const settledColumns = new Set<Element | null>();
            for (const row of (host.shadowRoot ?? host).querySelectorAll<HTMLElement>("[data-line]")) {
              const column = row.closest("[data-code]");
              if (!settledColumns.has(column) && consider(row)) settledColumns.add(column);
            }
          }
          for (const header of item.querySelectorAll<HTMLElement>(".review-target-header")) {
            if (!header.closest("[data-review-sticky-copy]")) consider(header);
          }
          const found = anchor as { element: HTMLElement; top: number } | null;
          const element = found?.element;
          foldAnchorRef.current = !element
            ? { path, line: "", type: "", side: "", header: "", top: item.getBoundingClientRect().top }
            : element.classList.contains("review-target-header")
              ? { path, line: "", type: "", side: "", header: element.dataset.reviewTargetId ?? "", top: found.top }
              : {
                  path,
                  line: element.dataset.line ?? "",
                  type: element.getAttribute("data-line-type") ?? "",
                  side: element.closest("[data-code]")?.hasAttribute("data-deletions") ? "deletions" : "additions",
                  header: "",
                  top: found.top,
                };
        }
      }
    }
    foldSignatureRef.current = { ids: foldedTargetIds, paths: fullyJudgedPaths, collapsed: collapsedPaths };
  }

  const pathPriority = useMemo(
    () => new Map(orderedPaths(targets).map((path, index) => [path, index])),
    [targets],
  );
  const webSurfaces = useMemo(
    () => surfaces.filter((surface) => surfaceView(surface) === "web"),
    [surfaces],
  );
  const apiSurfaces = useMemo(
    () => surfaces.filter((surface) => surfaceView(surface) === "api"),
    [surfaces],
  );
  const serviceSurfaces = useMemo(
    () => surfaces.filter((surface) => surfaceView(surface) === "service"),
    [surfaces],
  );
  const surfacesByKey = useMemo(
    () => new Map<string, ReviewSurfaceInfo>(surfaces.map((surface) => [`${surface.provider}:${surface.id}`, surface])),
    [surfaces],
  );
  const linkedPreviewSurfacesByPath = useMemo(() => {
    const map = new Map<string, ReviewSurfaceInfo[]>();
    const previewable = surfaces.filter((surface) => {
      if (surfaceView(surface) === "web") return true;
      return Boolean(surface.preview && surface.preview.status !== "unavailable");
    });
    const changedSymbolsByPath = new Map<string, Set<string>>();
    for (const target of targets) {
      const symbols = changedSymbolsByPath.get(target.path) ?? new Set<string>();
      if (target.symbol) symbols.add(target.symbol);
      changedSymbolsByPath.set(target.path, symbols);
    }
    const visualPriority = (surface: ReviewSurfaceInfo) => {
      if (surface.kind === "storybook.story") return 0;
      if (surface.kind === "web.route") return 1;
      return 2;
    };
    const changedStoryExportsByPath = new Map<string, ReadonlySet<string>>();
    for (const [path, detail] of Object.entries(details)) {
      if (detail?.patch) changedStoryExportsByPath.set(path, changedStorybookExports(detail.patch));
    }
    const changeAffinity = (surface: ReviewSurfaceInfo, path: string) => {
      if (surface.kind !== "storybook.story") return 1;
      const storyFile = String(surface.metadata?.story_file ?? "");
      const exportName = String(surface.metadata?.export ?? "");
      if (storyFile === path && exportName && changedStoryExportsByPath.get(path)?.has(exportName)) return 0;
      return exportName && changedSymbolsByPath.get(path)?.has(exportName) ? 0 : 1;
    };
    for (const surface of previewable) {
      for (const path of surface.affected_paths) {
        const rows = map.get(path) ?? [];
        rows.push(surface);
        map.set(path, rows);
      }
    }
    for (const [path, rows] of map) {
      map.set(path, rows.sort((a, b) => changeAffinity(a, path) - changeAffinity(b, path)
        || visualPriority(a) - visualPriority(b)
        || (a.title || a.id).localeCompare(b.title || b.id)));
    }
    return map;
  }, [details, surfaces, targets]);
  const pluginGroups = useMemo(() => {
    const priority = (surface: ReviewSurfaceInfo) => Math.min(
      ...surface.affected_paths.map((path) => pathPriority.get(path) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    const impactFor = (surface: ReviewSurfaceInfo) => surfaceImpactScores[`${surface.provider}:${surface.id}`]?.magnitude;
    const groups = new Map<string, { surfaces: ReviewSurfaceInfo[]; options: SurfacePickerOption[]; priority: number; impact: number }>();
    const ordered = surfaces
      .filter((surface) => surfaceView(surface) === "plugin" && surface.affected_paths.length > 0)
      .map((surface) => ({ surface, priority: priority(surface), impact: impactFor(surface) }))
      .sort((a, b) => {
        if (a.impact !== undefined && b.impact !== undefined) {
          if (a.impact !== b.impact) return b.impact - a.impact;
        } else if (a.impact !== undefined) return -1;
        else if (b.impact !== undefined) return 1;
        return a.priority - b.priority || (a.surface.title || a.surface.id).localeCompare(b.surface.title || b.surface.id);
      });
    for (const { surface, priority: rank, impact } of ordered) {
      const id = pluginGroupId(surface);
      const group = groups.get(id) ?? { surfaces: [], options: [], priority: rank, impact: impact ?? -1 };
      const key = `${surface.provider}:${surface.id}`;
      const score = surfaceImpactScores[key];
      const rankable = surfaceAutoRankable(surface);
      const comparisonLabel = score?.label
        || (rankable ? (surfaceImpactBusy ? "comparison queued…" : "comparison pending") : "not auto-ranked");
      group.surfaces.push(surface);
      group.options.push({
        value: key,
        label: surface.title || surface.locator || surface.id,
        detail: [comparisonLabel, surface.preview?.label || surface.comparison?.label || surface.kind].filter(Boolean).join(" · "),
        searchText: [
          surface.title,
          surface.locator,
          surface.kind,
          surface.provider,
          score?.label,
          ...(score?.reasons ?? []),
          ...surface.affected_paths,
        ].filter(Boolean).join(" "),
      });
      group.priority = Math.min(group.priority, rank);
      group.impact = Math.max(group.impact, impact ?? -1);
      groups.set(id, group);
    }
    return [...groups.entries()].sort((a, b) => {
      if (a[1].impact !== b[1].impact) return b[1].impact - a[1].impact;
      return a[1].priority - b[1].priority || a[0].localeCompare(b[0]);
    });
  }, [pathPriority, surfaceImpactBusy, surfaceImpactScores, surfaces]);
  const webRouteOptions = useMemo<WebRouteOption[]>(() => {
    const routes = webSurfaces.map((surface) => surface.locator || surface.title || "/");
    const counts = new Map<string, number>();
    for (const route of routes) counts.set(route, (counts.get(route) ?? 0) + 1);
    return webSurfaces
      // WebSurfaceProvider emits only routes reachable from changed files, and
      // SurfaceRegistry merges duplicate (root, route) identities. Keep that
      // invariant explicit in the Reader: a route with no changed-file anchor
      // never belongs in the affected-route picker.
      .filter((surface) => surface.affected_paths.length > 0)
      .map((surface) => {
        const route = surface.locator || surface.title || "/";
        const root = String(surface.metadata?.root ?? "");
        const framework = String(surface.metadata?.framework ?? "");
        const serviceName = configuredSurfaceName(surface);
        const disambiguator = serviceName || root || surface.title || surface.provider;
        const affected = [...surface.affected_paths].sort(
          (a, b) => (pathPriority.get(a) ?? Number.MAX_SAFE_INTEGER) - (pathPriority.get(b) ?? Number.MAX_SAFE_INTEGER)
            || a.localeCompare(b),
        );
        const priority = Math.min(
          ...affected.map((path) => pathPriority.get(path) ?? Number.MAX_SAFE_INTEGER),
          Number.MAX_SAFE_INTEGER,
        );
        return {
          route,
          surfaceKey: `${surface.provider}:${surface.id}`,
          documentPath: affected[0] ?? "",
          label: (counts.get(route) ?? 0) > 1 ? `${route} · ${disambiguator}` : route,
          detail: [serviceName, framework, root].filter(Boolean).join(" · ") || surface.provider,
          searchText: [surface.title, surface.provider, surface.runtime, serviceName, framework, root, ...affected].filter(Boolean).join(" "),
          priority,
        };
      })
      .sort((a, b) => {
        if ((a.priority ?? 0) !== (b.priority ?? 0)) return (a.priority ?? 0) - (b.priority ?? 0);
        if (a.route === "/" && b.route !== "/") return -1;
        if (b.route === "/" && a.route !== "/") return 1;
        return a.route.localeCompare(b.route) || (a.label ?? "").localeCompare(b.label ?? "");
      });
  }, [pathPriority, webSurfaces]);
  useEffect(() => {
    let live = true;
    const candidates = surfaces
      .filter((surface) => surfaceView(surface) !== "web" && !surface.preview && surfaceAutoRankable(surface))
      .sort((a, b) => {
        const aKey = `${a.provider}:${a.id}`;
        const bKey = `${b.provider}:${b.id}`;
        if (aKey === activeSurfaceKeyRef.current) return -1;
        if (bKey === activeSurfaceKeyRef.current) return 1;
        const priority = (surface: ReviewSurfaceInfo) => Math.min(
          ...surface.affected_paths.map((path) => pathPriority.get(path) ?? Number.MAX_SAFE_INTEGER),
          Number.MAX_SAFE_INTEGER,
        );
        return priority(a) - priority(b) || (a.title || a.id).localeCompare(b.title || b.id);
      });
    if (!revisionId || candidates.length === 0) {
      setSurfaceImpactScores((current) => Object.keys(current).length === 0 ? current : {});
      setSurfaceImpactBusy((current) => current ? false : current);
      return () => { live = false; };
    }

    setSurfaceImpactScores({});
    setSurfaceImpactBusy(true);
    const run = async () => {
      const pending = new Set(candidates.map((surface) => `${surface.provider}:${surface.id}`));
      for (let pass = 0; pass < 2 && live && pending.size > 0; pass += 1) {
        for (const surface of candidates) {
          if (!live) return;
          const key = `${surface.provider}:${surface.id}`;
          if (!pending.has(key)) continue;
          if (key !== activeSurfaceKeyRef.current || pass > 0) {
            await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0));
          }
          try {
            await loadSurfaceImpact(surface, 3);
            pending.delete(key);
          } catch {
            // Leave the item queued. Selecting it triggers an immediate retry;
            // the second lazy pass catches transient browser/runtime startup.
          }
        }
        if (pending.size > 0 && pass === 0) {
          await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 1500));
        }
      }
      if (live) setSurfaceImpactBusy(false);
    };
    void run();
    return () => { live = false; };
  }, [loadSurfaceImpact, pathPriority, revisionId, surfaces]);

  const apiSurfaceOptions = useMemo(() => apiSurfaces
    .filter((surface) => surface.affected_paths.length > 0)
    .map((surface) => {
      const method = String(surface.metadata.method ?? "").toUpperCase();
      const url = String(surface.metadata.url ?? surface.locator ?? surface.title);
      const priority = Math.min(
        ...surface.affected_paths.map((path) => pathPriority.get(path) ?? Number.MAX_SAFE_INTEGER),
        Number.MAX_SAFE_INTEGER,
      );
      const key = `${surface.provider}:${surface.id}`;
      const score = surfaceImpactScores[key];
      const rankable = surfaceAutoRankable(surface);
      const delta = score?.label
        || (rankable ? (surfaceImpactBusy ? "response diff queued…" : "response diff pending") : "not auto-ranked");
      return {
        value: key,
        label: [method, url].filter(Boolean).join(" "),
        detail: [delta, configuredSurfaceName(surface)].filter(Boolean).join(" · "),
        searchText: [
          surface.title,
          surface.provider,
          surface.runtime,
          configuredSurfaceName(surface),
          method,
          url,
          score?.label,
          ...(score?.reasons ?? []),
          ...surface.affected_paths,
        ].filter(Boolean).join(" "),
        priority,
        score,
      };
    })
    .sort((a, b) => {
      if (a.score && b.score) {
        if (a.score.magnitude !== b.score.magnitude) return b.score.magnitude - a.score.magnitude;
      } else if (a.score) return -1;
      else if (b.score) return 1;
      return a.priority - b.priority || a.label.localeCompare(b.label);
    }), [apiSurfaces, pathPriority, surfaceImpactBusy, surfaceImpactScores]);
  const orderedServiceSurfaces = useMemo(() => [...serviceSurfaces].sort((a, b) => {
    const priority = (surface: ReviewSurfaceInfo) => Math.min(
      ...surface.affected_paths.map((path) => pathPriority.get(path) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    const aImpact = surfaceImpactScores[`${a.provider}:${a.id}`]?.magnitude;
    const bImpact = surfaceImpactScores[`${b.provider}:${b.id}`]?.magnitude;
    if (aImpact !== undefined && bImpact !== undefined) {
      if (aImpact !== bImpact) return bImpact - aImpact;
    } else if (aImpact !== undefined) return -1;
    else if (bImpact !== undefined) return 1;
    return priority(a) - priority(b)
      || String(a.metadata.service ?? a.title ?? a.id).localeCompare(String(b.metadata.service ?? b.title ?? b.id));
  }), [pathPriority, serviceSurfaces, surfaceImpactScores]);
  const selectedWebSurface = useMemo(() => {
    const key = webRouteOptions.some((option) => option.surfaceKey === selectedWebSurfaceKey)
      ? selectedWebSurfaceKey
      : webRouteOptions[0]?.surfaceKey;
    return webSurfaces.find((surface) => `${surface.provider}:${surface.id}` === key) ?? null;
  }, [selectedWebSurfaceKey, webRouteOptions, webSurfaces]);
  const selectedApiSurface = useMemo(() => {
    const key = apiSurfaceOptions.some((option) => option.value === selectedApiSurfaceKey)
      ? selectedApiSurfaceKey
      : apiSurfaceOptions[0]?.value;
    return apiSurfaces.find((surface) => `${surface.provider}:${surface.id}` === key) ?? null;
  }, [apiSurfaceOptions, apiSurfaces, selectedApiSurfaceKey]);
  const previewFromSurface = useCallback((surface: ReviewSurfaceInfo): WebPreview | null => {
    const framework = typeof surface.metadata.framework === "string" ? surface.metadata.framework : "";
    const root = typeof surface.metadata.root === "string" ? surface.metadata.root : undefined;
    const route = surface.locator || surface.title || "/";
    if (!framework || !route) return null;
    return { kind: "web", framework, routes: [route], default_route: route, root };
  }, []);

  const resetScrollPageRef = useRef<number | undefined>(undefined);
  const items = useMemo<StreamItem[]>(() => {
    const rows: StreamItem[] = [];
    const pathOrder = orderedPaths(targets);
    const activePaths = new Set(pathOrder);

    const webItemFor = (surface: ReviewSurfaceInfo): WebSurfaceStreamItem | null => {
      const routeHint = surface.locator || surface.title || "";
      const visibleAffected = surface.affected_paths.filter((path) => activePaths.has(path));
      const documentPath = visibleAffected.find((path) => {
        const preview = details[path]?.preview;
        return preview?.kind === "web" && (!routeHint || preview.routes.includes(routeHint));
      }) ?? surface.affected_paths.find((path) => {
        const preview = details[path]?.preview;
        return preview?.kind === "web" && (!routeHint || preview.routes.includes(routeHint));
      }) ?? visibleAffected[0] ?? surface.affected_paths[0];
      if (!documentPath) return null;
      const detailPreview = details[documentPath]?.preview;
      const preview = detailPreview?.kind === "web" ? detailPreview : previewFromSurface(surface);
      if (!preview) return null;
      const route = surface.locator || preview.default_route || "/";
      return {
        id: "surface-group:web",
        type: "web-surface",
        version: 1,
        estimatedHeight: 840,
        collapsed: false,
        surface,
        documentPath,
        preview: { ...preview, routes: [route], default_route: route },
      };
    };

    const apiItemFor = (surface: ReviewSurfaceInfo): ApiSurfaceStreamItem => {
      const surfaceKey = `${surface.provider}:${surface.id}`;
      const comparable = surface.capabilities.includes("compare")
        && surface.capabilities.includes("execute")
        && Boolean(surface.runtime);
      return {
        id: "surface-group:api",
        type: "api-surface",
        version: 1,
        estimatedHeight: 520,
        collapsed: false,
        surface,
        mode: comparable ? (surfaceModes[surfaceKey] ?? "preview") : "preview",
      };
    };
    const serviceItemFor = (surface: ReviewSurfaceInfo): ServiceSurfaceStreamItem => {
      const surfaceKey = `${surface.provider}:${surface.id}`;
      return {
        id: `surface:${surfaceKey}`,
        type: "service-surface",
        version: 1,
        estimatedHeight: 360,
        collapsed: false,
        surface,
        mode: surfaceModes[surfaceKey] ?? "preview",
      };
    };
    const pluginItemFor = (
      id: string,
      group: { surfaces: ReviewSurfaceInfo[]; options: SurfacePickerOption[] },
    ): PluginSurfaceStreamItem | null => {
      const selectedKey = selectedPluginSurfaceKeys[id];
      const surface = group.surfaces.find((item) => `${item.provider}:${item.id}` === selectedKey) ?? group.surfaces[0];
      if (!surface) return null;
      const surfaceKey = `${surface.provider}:${surface.id}`;
      return {
        id,
        type: "plugin-surface",
        version: 1,
        estimatedHeight: 520,
        collapsed: false,
        surface,
        options: group.options,
        mode: surfaceComparable(surface) && surfaceModes[surfaceKey] === "compare" ? "compare" : "preview",
      };
    };
    const linkedItemFor = (path: string): LinkedSurfaceStreamItem | null => {
      const mode = fileSurfaceModes[path] ?? "source";
      if (mode === "source") return null;
      const candidates = linkedPreviewSurfacesByPath.get(path) ?? [];
      const selectedKey = selectedFileSurfaceKeys[path];
      const surface = candidates.find((item) => `${item.provider}:${item.id}` === selectedKey) ?? candidates[0];
      if (!surface) return null;
      const actualMode: SurfacePreviewMode = mode === "compare"
        && surfaceView(surface) !== "web"
        && !surfaceComparable(surface)
        ? "preview"
        : mode;
      const options = candidates.map((candidate) => ({
        value: `${candidate.provider}:${candidate.id}`,
        label: candidate.title || candidate.locator || candidate.id,
        detail: candidate.kind === "storybook.story" ? "Storybook" : candidate.kind === "web.route" ? "Web" : (candidate.preview?.label || candidate.provider),
        searchText: [candidate.provider, candidate.kind, candidate.title, candidate.locator, ...candidate.affected_paths].filter(Boolean).join(" "),
      }));
      return {
        id: path,
        type: "linked-surface",
        version: nextItemRevision(
          itemRevisionCacheRef.current,
          path,
          "linked-surface",
          `${surface.provider}:${surface.id}:${actualMode}`,
          "",
          isPathCollapsed(path),
        ),
        estimatedHeight: isPathCollapsed(path) ? 44 : (surfaceView(surface) === "web" ? 860 : 620),
        collapsed: isPathCollapsed(path),
        surface,
        options,
        mode: actualMode,
      };
    };

    if (dedicatedItem?.kind === "file") {
      const path = dedicatedItem.path;
      const linkedItem = linkedItemFor(path);
      if (linkedItem) return [linkedItem];
      const detail = details[path];
      const mode = detail?.preview?.kind === "web" ? "source" : (markdownModes[path] ?? "preview");
      const item = itemFor(
        path,
        detail,
        errors[path],
        targetsByPath.get(path) ?? [],
        inlineAnnotations,
        draft,
        comparisonMode,
        isPathCollapsed(path),
        mode,
        parsedDiffCacheRef.current,
        itemRevisionCacheRef.current,
      );
      return item ? [item] : [];
    }

    if (dedicatedItem?.kind === "surface") {
      const dedicatedKey = `${dedicatedItem.provider}:${dedicatedItem.id}`;
      if (webSurfaces.some((surface) => `${surface.provider}:${surface.id}` === dedicatedKey)) {
        const item = selectedWebSurface ? webItemFor(selectedWebSurface) : null;
        return item ? [item] : [];
      }
      if (apiSurfaces.some((surface) => `${surface.provider}:${surface.id}` === dedicatedKey)) {
        return selectedApiSurface ? [apiItemFor(selectedApiSurface)] : [];
      }
      const dedicatedService = serviceSurfaces.find((surface) => `${surface.provider}:${surface.id}` === dedicatedKey);
      if (dedicatedService) return [serviceItemFor(dedicatedService)];
      for (const [id, group] of pluginGroups) {
        const dedicatedPlugin = group.surfaces.find((surface) => `${surface.provider}:${surface.id}` === dedicatedKey);
        if (!dedicatedPlugin) continue;
        const item = pluginItemFor(id, {
          surfaces: [dedicatedPlugin],
          options: group.options.filter((option) => option.value === dedicatedKey),
        });
        return item ? [item] : [];
      }
      return [];
    }

    if (showReviewPreviews) {
      const webItem = selectedWebSurface ? webItemFor(selectedWebSurface) : null;
      if (webItem) rows.push(webItem);
      if (selectedApiSurface) rows.push(apiItemFor(selectedApiSurface));
      for (const service of orderedServiceSurfaces) rows.push(serviceItemFor(service));
      for (const [id, group] of pluginGroups) {
        const standaloneSurfaces = group.surfaces.filter((surface) => (
          !surface.preview || !surface.affected_paths.some((path) => activePaths.has(path))
        ));
        if (standaloneSurfaces.length === 0) continue;
        const allowed = new Set(standaloneSurfaces.map((surface) => `${surface.provider}:${surface.id}`));
        const item = pluginItemFor(id, {
          surfaces: standaloneSurfaces,
          options: group.options.filter((option) => allowed.has(option.value)),
        });
        if (item) rows.push(item);
      }
    }

    for (const path of pathOrder) {
      const linkedItem = linkedItemFor(path);
      if (linkedItem) {
        rows.push(linkedItem);
        continue;
      }
      const detail = details[path];
      const mode = detail?.preview?.kind === "web" ? "source" : (markdownModes[path] ?? "preview");
      const item = itemFor(
        path,
        detail,
        errors[path],
        targetsByPath.get(path) ?? [],
        inlineAnnotations,
        draft,
        comparisonMode,
        isPathCollapsed(path),
        mode,
        parsedDiffCacheRef.current,
        itemRevisionCacheRef.current,
      );
      if (item) rows.push(item);
    }
    for (const path of itemRevisionCacheRef.current.keys()) {
      if (!activePaths.has(path)) itemRevisionCacheRef.current.delete(path);
    }
    for (const path of parsedDiffCacheRef.current.keys()) {
      if (!activePaths.has(path)) parsedDiffCacheRef.current.delete(path);
    }
    return rows;
  }, [
    targets,
    details,
    errors,
    targetsByPath,
    inlineAnnotations,
    draft,
    isPathCollapsed,
    fileSurfaceModes,
    linkedPreviewSurfacesByPath,
    markdownModes,
    selectedFileSurfaceKeys,
    surfaceModes,
    webSurfaces,
    apiSurfaces,
    serviceSurfaces,
    dedicatedItem,
    previewFromSurface,
    selectedWebSurface,
    selectedApiSurface,
    orderedServiceSurfaces,
    pluginGroups,
    selectedPluginSurfaceKeys,
    showReviewPreviews,
  ]);

  useEffect(() => {
    if (!onFirstContentReady || items.length === 0) return;
    const identity = revisionId || "current";
    if (firstContentRevisionRef.current === identity) return;
    let cancelled = false;
    let frame = 0;
    const check = () => {
      if (cancelled) return;
      const root = scrollRef.current;
      if (root) {
        const hasNonDiffContent = items.some((item) => item.type !== "diff")
          && Boolean(root.querySelector("[data-review-stream-item]"));
        const hasDiffContent = [...root.querySelectorAll<HTMLElement>("diffs-container")].some(
          (host) => Boolean((host.shadowRoot ?? host).querySelector("[data-line]")),
        );
        if (hasNonDiffContent || hasDiffContent) {
          firstContentRevisionRef.current = identity;
          onFirstContentReady();
          return;
        }
      }
      frame = window.requestAnimationFrame(check);
    };
    frame = window.requestAnimationFrame(check);
    return () => {
      cancelled = true;
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [items, onFirstContentReady, revisionId]);

  // Programmatic navigation (j/k, outline, file jumps) owns the active target
  // until its scroll settles; otherwise the scroll-sync below reassigns it to
  // whatever file header passes the threshold mid-animation.
  //
  // It also holds its destination while the stream settles. The scroll is
  // aimed once, but what it aims at can still move: the file renders a moment
  // later, a judged area above folds or (a failed mark rolled back) unfolds, a
  // diff re-renders taller. Each frame re-resolves the destination and, when
  // it moved, re-aims the scroll still in flight or re-anchors the one that
  // arrived, so the reviewer ends up on what they asked for. The reviewer's
  // own scrolling, the next navigation or moving to another area ends it.
  const navigationHoldRef = useRef<{ frame: number } | null>(null);
  const releaseNavigation = useCallback(() => {
    programmaticScrollUntilRef.current = 0;
    const hold = navigationHoldRef.current;
    navigationHoldRef.current = null;
    if (hold) window.cancelAnimationFrame(hold.frame);
  }, []);
  useEffect(() => releaseNavigation, [releaseNavigation]);
  const holdNavigation = useCallback((
    activeId: string,
    resolve: (root: HTMLElement) => number | null,
    behavior: "instant" | "smooth",
  ) => {
    const previous = navigationHoldRef.current;
    if (previous) window.cancelAnimationFrame(previous.frame);
    const root = scrollRef.current;
    if (!root) {
      navigationHoldRef.current = null;
      return;
    }
    const started = Date.now();
    const minimumRelease = started + NAVIGATION_HOLD_MS;
    const hardDeadline = started + NAVIGATION_WAIT_MS;
    const activeAtStart = activeTargetIdRef.current;
    const hold = { frame: 0 };
    navigationHoldRef.current = hold;
    let aimed: number | null = null;
    let settledSince: number | null = null;
    const aim = (): boolean => {
      const destination = resolve(root);
      if (destination === null) {
        settledSince = null;
        return false;
      }
      const top = Math.round(Math.min(Math.max(0, root.scrollHeight - root.clientHeight), Math.max(0, destination)));
      const changed = aimed === null || Math.abs(top - aimed) >= 1;
      if (changed) {
        // The first aim animates as asked; a re-aim keeps a scroll in flight
        // animating and moves an arrived one without a second animation.
        const smooth = behavior === "smooth" && (aimed === null || Math.abs(root.scrollTop - aimed) >= 1);
        aimed = top;
        settledSince = null;
        if (typeof root.scrollTo === "function") root.scrollTo({ top, behavior: smooth ? browserScrollBehavior("smooth") : "auto" });
        else root.scrollTop = top;
      }
      const arrived = Math.abs(root.scrollTop - top) < 1;
      if (arrived) settledSince ??= Date.now();
      else settledSince = null;
      return arrived;
    };
    const pin = () => {
      const now = Date.now();
      // Keep scroll-sync and fold anchoring out of the way while the browser is
      // still travelling or the destination's layout is still moving. The hard
      // deadline is only a safety valve for a destination that never renders.
      const settleDeadline = settledSince === null ? now + NAVIGATION_SETTLE_MS : settledSince + NAVIGATION_SETTLE_MS;
      const deadline = Math.min(hardDeadline, Math.max(minimumRelease, settleDeadline));
      programmaticScrollUntilRef.current = Math.max(programmaticScrollUntilRef.current, deadline);
    };
    programmaticScrollUntilRef.current = 0;
    aim();
    pin();
    const schedule = () => {
      // Only real frames: a requestAnimationFrame that runs its callback
      // synchronously (test doubles) would otherwise recurse.
      let scheduled = false;
      hold.frame = window.requestAnimationFrame(() => {
        if (scheduled) tick();
      });
      scheduled = true;
    };
    const tick = () => {
      if (navigationHoldRef.current !== hold) return;
      const current = activeTargetIdRef.current;
      const left = Boolean(activeId) && current !== activeId && current !== activeAtStart;
      const now = Date.now();
      if (left) {
        navigationHoldRef.current = null;
        programmaticScrollUntilRef.current = 0;
        return;
      }
      const arrived = aim();
      pin();
      const settled = arrived
        && settledSince !== null
        && now >= minimumRelease
        && now - settledSince >= NAVIGATION_SETTLE_MS;
      if (settled || now >= hardDeadline) {
        navigationHoldRef.current = null;
        return;
      }
      schedule();
    };
    schedule();
  }, []);
  const focusFromScroll = useCallback(() => {
    const root = scrollRef.current;
    if (!root) return;
    if (navigationHoldRef.current) return;
    if (Date.now() < programmaticScrollUntilRef.current) {
      programmaticScrollUntilRef.current = Math.max(programmaticScrollUntilRef.current, Date.now() + 150);
      return;
    }
    const threshold = root.getBoundingClientRect().top + 115;
    const candidates: Array<{ id: string; distance: number; surfaceKey: string }> = [];
    const consider = (id: string, top: number, surfaceKey = "") => {
      if (!id) return;
      const distance = top <= threshold
        ? Math.abs(threshold - top)
        : Math.abs(top - threshold) + 1000;
      candidates.push({ id, distance, surfaceKey });
    };

    // Review targets are coverage, not annotation DOM. Track ordinary code
    // targets from their rendered source lines so every symbol can become
    // active while scrolling without requiring a fake inline target card.
    const lineAnchoredTargets = new Set<string>();
    for (const target of targets) {
      const item = itemElementsRef.current.get(target.path);
      const renderedMarkdown = details[target.path]?.preview?.kind === "markdown"
        && (markdownModes[target.path] ?? "preview") !== "source";
      const location = targetScrollLocation(target);
      if (!item || !location || renderedMarkdown) continue;
      let line = targetLineAnchorsRef.current.get(target.target_id) ?? null;
      if (!line || !line.isConnected) {
        line = renderedDiffLine(item, location.side, location.lineNumber);
        if (line) targetLineAnchorsRef.current.set(target.target_id, line);
      }
      // A folded area's lines are hidden (no box, top 0): its one-line header
      // below stands for it instead of a phantom line at the top edge.
      if (!line || isFoldHiddenRow(line)) continue;
      lineAnchoredTargets.add(target.target_id);
      consider(target.target_id, line.getBoundingClientRect().top);
    }

    // Surface and rendered-document containers still expose a target id because
    // they do not have source-line geometry in the current view.
    for (const node of root.querySelectorAll<HTMLElement>("[data-review-target-id]")) {
      const id = node.dataset.reviewTargetId ?? "";
      if (!id || lineAnchoredTargets.has(id)) continue;
      consider(id, node.getBoundingClientRect().top, node.dataset.reviewSurfaceKey ?? "");
    }

    const best = candidates.reduce<(typeof candidates)[number] | null>(
      (current, candidate) => (!current || candidate.distance < current.distance ? candidate : current),
      null,
    );
    if (!best) return;
    if (best.id !== activeTargetId) onActiveTarget(best.id);
    setActiveSurfaceKey(best.surfaceKey);
  }, [activeTargetId, details, markdownModes, onActiveTarget, targets]);

  const scheduleFocusFromScroll = useCallback(() => {
    if (scrollFocusFrameRef.current !== null) return;
    scrollFocusFrameRef.current = window.requestAnimationFrame(() => {
      scrollFocusFrameRef.current = null;
      focusFromScroll();
    });
  }, [focusFromScroll]);

  useEffect(() => () => {
    if (scrollFocusFrameRef.current !== null) {
      window.cancelAnimationFrame(scrollFocusFrameRef.current);
      scrollFocusFrameRef.current = null;
    }
    for (const frame of scopeFrameByPathRef.current.values()) window.cancelAnimationFrame(frame);
    scopeFrameByPathRef.current.clear();
  }, []);

  const scrollToTarget = useCallback((target: ReviewTarget, behavior: "instant" | "smooth" = "smooth") => {
    const renderedMarkdown = details[target.path]?.preview?.kind === "markdown"
      && (markdownModes[target.path] ?? "preview") !== "source";
    const location = targetScrollLocation(target);
    holdNavigation(target.target_id, (root) => {
      const item = itemElementsRef.current.get(target.path);
      if (!item?.isConnected) return null;
      const rootTop = root.getBoundingClientRect().top;
      if (location && !renderedMarkdown) {
        const line = renderedDiffLine(item, location.side, location.lineNumber);
        if (line && !isFoldHiddenRow(line)) return root.scrollTop + line.getBoundingClientRect().top - rootTop - 52;
        // Folded, its lines are hidden: the one-line header stands where the change starts.
        const header = line && [...item.querySelectorAll<HTMLElement>(`.review-target-header[data-review-target-id="${CSS.escape(target.target_id)}"]`)]
          .find((node) => !node.closest("[data-review-sticky-copy]"));
        if (header) return root.scrollTop + header.getBoundingClientRect().bottom - rootTop - 52;
        return root.scrollTop + item.getBoundingClientRect().top - rootTop;
      }
      return root.scrollTop + item.getBoundingClientRect().top - rootTop - 36;
    }, behavior);
    onActiveTarget(target.target_id);
  }, [details, holdNavigation, markdownModes, onActiveTarget]);

  const scrollToItem = useCallback((itemId: string, activeId: string, behavior: "instant" | "smooth") => {
    holdNavigation(activeId, (root) => {
      const item = itemElementsRef.current.get(itemId);
      if (!item?.isConnected) return null;
      return root.scrollTop + item.getBoundingClientRect().top - root.getBoundingClientRect().top - 36;
    }, behavior);
  }, [holdNavigation]);

  const scrollToFile = useCallback((path: string, behavior: "instant" | "smooth" = "smooth") => {
    const first = targets.find((target) => target.path === path);
    // A file the stream is still rendering is scrolled to once it is there.
    scrollToItem(path, first?.target_id ?? "", behavior);
    setActiveSurfaceKey("");
    if (first) onActiveTarget(first.target_id);
  }, [onActiveTarget, scrollToItem, targets]);

  const scrollToSurface = useCallback((surfaceKey: string, behavior: "instant" | "smooth" = "smooth") => {
    const surface = surfaces.find((candidate) => `${candidate.provider}:${candidate.id}` === surfaceKey);
    if (!surface) return;
    const view = surfaceView(surface);
    const linkedPath = (view === "web" || (view === "plugin" && Boolean(surface.preview)))
      ? surface.affected_paths.find((path) => targetsByPath.has(path))
      : undefined;
    const itemId = linkedPath
      ?? (view === "api"
        ? "surface-group:api"
        : view === "service"
          ? `surface:${surfaceKey}`
          : pluginGroupId(surface));
    if (linkedPath) {
      setSelectedFileSurfaceKeys((current) => ({ ...current, [linkedPath]: surfaceKey }));
      setFileSurfaceModes((current) => ({ ...current, [linkedPath]: "preview" }));
    } else if (view === "api") setSelectedApiSurfaceKey(surfaceKey);
    else if (view === "plugin") setSelectedPluginSurfaceKeys((current) => ({ ...current, [itemId]: surfaceKey }));
    setActiveSurfaceKey(surfaceKey);

    const first = surface.affected_paths.flatMap((path) => targetsByPath.get(path) ?? [])[0];
    scrollToItem(itemId, first?.target_id ?? "", behavior);
    if (first) onActiveTarget(first.target_id);
  }, [onActiveTarget, scrollToItem, surfaces, targetsByPath]);
  useImperativeHandle(ref, () => ({
    scrollToTarget,
    scrollToFile,
    scrollToSurface,
  }), [scrollToFile, scrollToSurface, scrollToTarget]);
  const submit = useCallback((
    path: string,
    body: string,
    kind: AnnotationKind,
    parentId: string,
    markTarget: boolean,
    targetUnitKey: string | undefined,
  ) => {
    const parent = parentId ? inlineAnnotations.find((item) => item.id === parentId) : undefined;
    const fileLevel = parent ? parent.file_level : Boolean(draft?.fileLevel);
    const start = fileLevel ? 0 : parent ? parent.start_line : (draft?.startLine ?? 0);
    const end = fileLevel ? 0 : parent ? parent.end_line : (draft?.endLine ?? start);
    if (!fileLevel && start < 1) return;
    onCreate({
      path,
      start_line: start,
      end_line: end,
      side: parent ? markerSide(parent.side) : (draft?.side ?? "additions"),
      body,
      kind,
      file_level: fileLevel,
      parent_id: parentId || undefined,
      target_unit_key: targetUnitKey,
      mark_target: markTarget && Boolean(targetUnitKey),
    });
  }, [draft, inlineAnnotations, onCreate]);

  const toggleCollapsed = useCallback((path: string) => {
    const collapsed = isPathCollapsed(path);
    if (collapsed) {
      setCollapsedPaths((current) => {
        if (!current.has(path)) return current;
        const next = new Set(current);
        next.delete(path);
        return next;
      });
      if (fullyJudgedPaths.has(path)) {
        setExpandedJudgedPaths((current) => {
          if (current.has(path)) return current;
          const next = new Set(current);
          next.add(path);
          return next;
        });
      }
      return;
    }

    setCollapsedPaths((current) => {
      if (current.has(path)) return current;
      const next = new Set(current);
      next.add(path);
      return next;
    });
  }, [fullyJudgedPaths, isPathCollapsed]);
  const setMarkdownView = useCallback((path: string, mode: MarkdownRenderMode | "source") => {
    setMarkdownModes((current) => ({ ...current, [path]: mode }));
  }, []);

  const setFileSurfaceView = useCallback((path: string, mode: "source" | SurfacePreviewMode) => {
    const linked = linkedPreviewSurfacesByPath.get(path) ?? [];
    if (mode !== "source" && linked.length === 0) return;
    if (mode !== "source") {
      const selectedKey = selectedFileSurfaceKeys[path];
      const selected = linked.find((surface) => `${surface.provider}:${surface.id}` === selectedKey) ?? linked[0];
      if (!selected) return;
      const key = `${selected.provider}:${selected.id}`;
      setSelectedFileSurfaceKeys((current) => current[path] === key ? current : { ...current, [path]: key });
      setActiveSurfaceKey(key);
      activeMarkdownPathRef.current = "";
      if (mode === "compare" && surfaceView(selected) !== "web" && !surfaceComparable(selected)) mode = "preview";
    }
    setFileSurfaceModes((current) => ({ ...current, [path]: mode }));
  }, [linkedPreviewSurfacesByPath, selectedFileSurfaceKeys]);

  const cycleMarkdownPreviewTheme = useCallback((path: string) => {
    setMarkdownPreviewThemes((current) => {
      const value = current[path] ?? "auto";
      const next: MarkdownPreviewTheme = value === "auto" ? "light" : value === "light" ? "dark" : "auto";
      return { ...current, [path]: next };
    });
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const path = activeMarkdownPathRef.current;
      if (!path) return;
      const target = event.target as HTMLElement | null;
      if (target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable)) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "1") setMarkdownView(path, "preview");
      else if (event.key === "2") setMarkdownView(path, "compare");
      else if (event.key === "3") setMarkdownView(path, "source");
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setMarkdownView]);

  const headerControls = useCallback((path: string) => {
    const file = fileByPath.get(path);
    if (!file) return null;
    if (comparisonMode) {
      const additions = details[path]?.additions ?? 0;
      const deletions = details[path]?.deletions ?? 0;
      return (
        <div className="flex items-center gap-2 pr-2 font-mono text-caption text-muted-foreground">
          {additions > 0 && <span className="text-emerald-400">+{additions}</span>}
          {deletions > 0 && <span className="text-rose-400">-{deletions}</span>}
          <UiButton
            variant="ghost"
            size="icon"
            type="button"
            onClick={() => toggleCollapsed(path)}
            aria-label={isPathCollapsed(path) ? `Expand ${path}` : `Collapse ${path}`}
            tooltip={isPathCollapsed(path) ? "Expand file" : "Collapse file"}
            className="h-6 w-6"
          >
            {isPathCollapsed(path) ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
          </UiButton>
        </div>
      );
    }
    const unreviewedInFile = outstandingByPath.get(path) ?? 0;
    const fileTargets = targetsByPath.get(path) ?? [];
    const reviewedCount = fileTargets.filter((target) => target.state === "reviewed").length;
    const needsChangesCount = fileTargets.filter((target) => target.state === "needs_changes").length;
    const visibleOutstanding = fileTargets.filter((target) => target.state !== "reviewed" && target.state !== "needs_changes").length;
    const visibleBackendOutstanding = fileTargets.filter((target) => target.state !== "reviewed").length;
    const hiddenUnreviewed = Math.max(0, unreviewedInFile - visibleBackendOutstanding);
    const allJudged = fullyJudgedPaths.has(path);
    const hasMarkdownPreview = details[path]?.preview?.kind === "markdown";
    const hasCsvPreview = details[path]?.preview?.kind === "csv";
    const hasMediaPreview = details[path]?.preview?.kind === "media";
    const hasInlinePreview = hasMarkdownPreview || hasCsvPreview || hasMediaPreview;
    const markdownMode: MarkdownRenderMode | "source" = markdownModes[path] ?? "preview";
    const markdownPreviewTheme = markdownPreviewThemes[path] ?? "auto";
    const resolvedMarkdownTheme: Theme = markdownPreviewTheme === "auto" ? chromeTheme : markdownPreviewTheme;
    const nextMarkdownTheme: MarkdownPreviewTheme = markdownPreviewTheme === "auto" ? "light" : markdownPreviewTheme === "light" ? "dark" : "auto";
    const MarkdownThemeIcon = markdownPreviewTheme === "auto" ? SunMoon : markdownPreviewTheme === "light" ? Sun : Moon;
    const linkedSurfaces = hasInlinePreview ? [] : (linkedPreviewSurfacesByPath.get(path) ?? []);
    const selectedLinkedKey = selectedFileSurfaceKeys[path];
    const selectedLinked = linkedSurfaces.find((surface) => `${surface.provider}:${surface.id}` === selectedLinkedKey) ?? linkedSurfaces[0];
    const selectedLinkedSurfaceKey = selectedLinked ? `${selectedLinked.provider}:${selectedLinked.id}` : "";
    const fileSurfaceMode = fileSurfaceModes[path] ?? "source";
    const linkedComparable = selectedLinked
      ? (surfaceView(selectedLinked) === "web" || surfaceComparable(selectedLinked))
      : false;
    const linkedOptions: SurfacePickerOption[] = linkedSurfaces.map((surface) => ({
      value: `${surface.provider}:${surface.id}`,
      label: surface.title || surface.locator || surface.id,
      detail: surface.kind === "storybook.story" ? "Storybook" : surface.kind === "web.route" ? "Web" : (surface.preview?.label || surface.provider),
      searchText: [surface.provider, surface.kind, surface.title, surface.locator, ...surface.affected_paths].filter(Boolean).join(" "),
    }));
    return (
      <div className="flex items-center gap-2 pr-2 font-mono text-caption text-muted-foreground">
        {file.targetCount > 0 && (
          <span className={allJudged
            ? needsChangesCount > 0
              ? "font-medium text-rose-300/85"
              : "font-medium text-emerald-300"
            : reviewedCount > 0 || needsChangesCount > 0
              ? "text-neutral-400"
              : ""}
          >
            {allJudged
              ? needsChangesCount > 0
                ? `${needsChangesCount} need${needsChangesCount === 1 ? "s" : ""} changes${reviewedCount > 0 ? ` · ${reviewedCount} reviewed` : ""}`
                : "✓ reviewed"
              : hiddenUnreviewed > 0
                ? `${visibleOutstanding} shown · ${visibleOutstanding + hiddenUnreviewed} left in file`
                : `${visibleOutstanding} left`}
          </span>
        )}
        {linkedSurfaces.length > 0 && (
          <>
            {linkedOptions.length > 1 ? (
              <SurfacePicker
                ariaLabel={`Preview surface for ${path}`}
                value={selectedLinkedSurfaceKey}
                storageKey={`lemoncrow-review-file-surface:${currentReviewId()}:${path}`}
                placeholder="Search linked stories, routes…"
                options={linkedOptions}
                onChange={(nextKey) => {
                  setSelectedFileSurfaceKeys((current) => ({ ...current, [path]: nextKey }));
                  const next = surfacesByKey.get(nextKey);
                  if (next) {
                    setActiveSurfaceKey(nextKey);
                    if (fileSurfaceMode === "compare" && surfaceView(next) !== "web" && !surfaceComparable(next)) {
                      setFileSurfaceModes((current) => ({ ...current, [path]: "preview" }));
                    }
                  }
                }}
              />
            ) : (
              <span
                className="max-w-48 truncate rounded border border-violet-950 bg-violet-950/20 px-1.5 py-0.5 text-caption text-violet-300/80"
                title={selectedLinked?.title || selectedLinked?.locator || "Linked preview"}
              >
                {selectedLinked?.kind === "storybook.story" ? "Storybook" : selectedLinked?.kind === "web.route" ? "Web" : "Preview"}
                {selectedLinked?.title ? ` · ${selectedLinked.title}` : ""}
              </span>
            )}
            <span className="flex overflow-hidden rounded border border-border">
              <UiButton unstyled
                type="button"
                aria-label={`Source for ${path}`}
                aria-pressed={fileSurfaceMode === "source"}
                onClick={() => setFileSurfaceView(path, "source")}
                className={`px-2 py-0.5 ${fileSurfaceMode === "source" ? "bg-neutral-800 text-foreground" : "text-muted-foreground hover:text-neutral-50"}`}
              >Source</UiButton>
              <UiButton unstyled
                type="button"
                aria-label={`Preview ${path}`}
                aria-pressed={fileSurfaceMode === "preview"}
                onClick={() => setFileSurfaceView(path, "preview")}
                className={`px-2 py-0.5 ${fileSurfaceMode === "preview" ? "bg-neutral-800 text-foreground" : "text-muted-foreground hover:text-neutral-50"}`}
              >Preview</UiButton>
              {linkedComparable && (
                <UiButton unstyled
                  type="button"
                  aria-label={`Compare preview ${path}`}
                  aria-pressed={fileSurfaceMode === "compare"}
                  onClick={() => setFileSurfaceView(path, "compare")}
                  className={`px-2 py-0.5 ${fileSurfaceMode === "compare" ? "bg-neutral-800 text-foreground" : "text-muted-foreground hover:text-neutral-50"}`}
                >Compare</UiButton>
              )}
            </span>
          </>
        )}
        {hasInlinePreview && (
          <span className="flex overflow-hidden rounded border border-border">
            {(["preview", "compare", "source"] as const).map((mode, index) => (
              <UiButton unstyled
                key={mode}
                type="button"
                aria-label={mode}
                aria-pressed={markdownMode === mode}
                tooltip={`${mode[0]?.toUpperCase()}${mode.slice(1)} · ${index + 1}`}
                onClick={() => setMarkdownView(path, mode)}
                className={`px-2 py-0.5 capitalize ${
                  markdownMode === mode
                    ? "bg-neutral-800 text-foreground"
                    : "text-muted-foreground hover:text-neutral-50"
                }`}
              >
                {mode} <span className="ml-1 text-caption text-muted-foreground">{index + 1}</span>
              </UiButton>
            ))}
          </span>
        )}
        {hasMarkdownPreview && markdownMode !== "source" && (
          <UiButton
            variant="ghost"
            size="icon"
            type="button"
            aria-label={`Markdown preview theme: ${markdownPreviewTheme === "auto" ? `Auto · ${resolvedMarkdownTheme}` : markdownPreviewTheme}. Switch to ${nextMarkdownTheme}`}
            tooltip={`Markdown preview theme: ${markdownPreviewTheme === "auto" ? `Auto · ${resolvedMarkdownTheme}` : markdownPreviewTheme}`}
            onClick={() => cycleMarkdownPreviewTheme(path)}
            className="h-6 w-7 rounded border border-border text-violet-200 hover:bg-muted"
          >
            <MarkdownThemeIcon size={12} strokeWidth={1.7} />
          </UiButton>
        )}
        {(unreviewedInFile > 0 || Boolean(details[path]?.patch) || Boolean(onOpenInCode)) && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <UiButton
                variant="ghost"
                size="icon"
                type="button"
                aria-label={`File actions for ${path}`}
                tooltip="File actions"
                className="h-6 w-6"
              >
                <EllipsisVertical size={13} />
              </UiButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {onOpenInCode && details[path]?.status !== "deleted" && (
                <DropdownMenuItem
                  onSelect={() => {
                    const first = fileTargets.find((target) => target.start_line > 0);
                    onOpenInCode({ path, line: first?.start_line ?? 0, endLine: first?.end_line ?? 0 });
                  }}
                  aria-label={`Open ${path} in Code`}
                  title="Open the working tree's current file in Code, labelled as the current index."
                >
                  <Code2 size={12} className="shrink-0 text-muted-foreground" />
                  <span>Open in Code</span>
                </DropdownMenuItem>
              )}
              {details[path]?.patch && (
                <DropdownMenuItem
                  onSelect={() => {
                    const safeName = path.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "change";
                    downloadTextPatch(`${safeName}.patch`, details[path]?.patch ?? "");
                  }}
                  aria-label={`Download patch for ${path}`}
                  title="Download this frozen file patch; apply it with git apply."
                >
                  <Download size={12} className="shrink-0 text-muted-foreground" />
                  <span>Download file patch</span>
                  <span className="ml-auto font-mono text-micro text-muted-foreground">git apply</span>
                </DropdownMenuItem>
              )}
              {unreviewedInFile > 0 && (
                <DropdownMenuItem
                  disabled={busy || hiddenUnreviewed > 0}
                  onSelect={() => onBulkReview(path)}
                  aria-label={hiddenUnreviewed > 0
                    ? `Mark safe items reviewed unavailable in ${path} while review areas are hidden`
                    : `Mark safe items reviewed in ${path}`}
                  title={hiddenUnreviewed > 0
                    ? "Some review areas are hidden. Show the whole file before bulk-marking."
                    : "Marks eligible remaining review areas as reviewed. Anything needing individual attention is skipped."}
                >
                  <CheckCircle2 size={12} className="shrink-0 text-emerald-500/80" />
                  <span>Mark safe items reviewed</span>
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        <UiButton
          variant="ghost"
          size="icon"
          type="button"
          onClick={() => toggleCollapsed(path)}
          aria-label={isPathCollapsed(path) ? `Expand ${path}` : `Collapse ${path}`}
          tooltip={isPathCollapsed(path) ? "Expand file" : "Collapse file"}
          className="h-6 w-6"
        >
          {isPathCollapsed(path) ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
        </UiButton>
      </div>
    );
  }, [
    busy,
    chromeTheme,
    comparisonMode,
    cycleMarkdownPreviewTheme,
    details,
    fileByPath,
    fileSurfaceModes,
    fullyJudgedPaths,
    isPathCollapsed,
    linkedPreviewSurfacesByPath,
    markdownModes,
    markdownPreviewThemes,
    onBulkReview,
    onOpenInCode,
    outstandingByPath,
    selectedFileSurfaceKeys,
    setFileSurfaceView,
    setMarkdownView,
    surfacesByKey,
    targetsByPath,
    toggleCollapsed,
  ]);
  const renderInlineAnnotation = useCallback((path: string, annotation: StreamMarkers[number]) => {
    const metadata = annotation.metadata;
    if (!metadata) return null;
    // Hunk fallback targets describe regions the full Review could not map
    // to a symbol. Their spans come from Review-base → Snapshot, not from the
    // Snapshot A → B patch, so do not present them as if they described this
    // incremental comparison. The actual diff and its comments remain visible.
    const targetRows = metadata.targetIds
      .map((id) => targetsById.get(id))
      .filter((row): row is ReviewTarget => Boolean(row))
      .filter((row) => !(snapshotComparison && row.kind === "hunk" && !row.symbol));
    const endTargetRows = metadata.targetEndIds
      .map((id) => targetsById.get(id))
      .filter((row): row is ReviewTarget => Boolean(row))
      .filter((row) => !(snapshotComparison && row.kind === "hunk" && !row.symbol));
    const comments = metadata.commentKeys.flatMap((key) => commentsAt(inlineAnnotations, path, key));
    const fileLevelAll = annotation.lineNumber === 0 ? fileComments(inlineAnnotations, path) : [];
    // Like line markers: below-threshold file comments ride along only with a surfaced one.
    const fileLevelRows = fileLevelAll.some((item) => !item.below_threshold) ? fileLevelAll : [];
    const shownComments = comments.length > 0 ? comments : fileLevelRows;
    const composing = Boolean(
      draft
      && draft.path === path
      && (draft.fileLevel ? annotation.lineNumber === 0 : metadata.commentKeys.includes(draftMarkerKey(draft))),
    );
    const draftTarget = composing && draft
      ? (targets.find((target) => target.unit_key === draft.targetUnitKey)
        ?? targetAtLine(targets, path, draft.side, draft.startLine, activeTargetId))
      : null;
    if (targetRows.length === 0 && endTargetRows.length === 0 && shownComments.length === 0 && (!composing || draft?.mode === "proposal")) {
      if (!(composing && draft?.mode === "proposal")) return null;
    }
    return (
      <div className="review-inline-annotation-stack space-y-1">
        {(shownComments.length > 0 || (composing && draft?.mode !== "proposal")) && (
          <AnnotationCard
            comments={shownComments}
            all={inlineAnnotations}
            label={draft?.fileLevel
              ? "File comment"
              : composing && draft
                ? `New comment · ${locationLabel(draft.startLine, draft.endLine)}`
                : "New comment"}
            composing={composing && draft?.mode !== "proposal"}
            draftFocusKey={composing && draft ? `${draft.path}:${draftMarkerKey(draft)}:${draft.startLine}` : ""}
            busy={busy}
            targetMarkAvailable={Boolean(draftTarget)}
            draftBody={composing ? draft?.body : ""}
            draftKind={composing ? draft?.kind : undefined}
            draftMarkTarget={composing ? draft?.markTarget : undefined}
            onDraftChange={(fields) => { if (draft) onDraft({ ...draft, ...fields }); }}
            onAsk={onAskSelection && composing && draft ? () => onAskSelection(draft) : undefined}
            onSuggestEdit={composing && draft && !draft.fileLevel && draft.side === "additions"
              ? () => onSuggestEdit(draft)
              : undefined}
            onEditSource={sourceMutationSupported && composing && draft && !draft.fileLevel && draft.side === "additions"
              ? () => onEditSource(draft)
              : undefined}
            onSubmit={(body, kind, parentId, markTarget) => submit(
              path,
              body,
              kind,
              parentId,
              markTarget,
              draftTarget?.unit_key,
            )}
            onCancel={() => onDraft(null)}
            onResolve={(id) => onSetAnnotationState(id, "resolved")}
            onReopen={(id) => onSetAnnotationState(id, "open")}
            onFindingDisposition={onFindingDisposition}
            onFindingRequestChange={requestFindingChange}
            onFindingFix={fixFinding}
            onFindingAsk={onAskSelection ? askAboutFinding : undefined}
          />
        )}
        {composing && draft?.mode === "proposal" && (
          <ReviewChangeProposalComposer
            selection={proposalSelection}
            proposal={activeProposal}
            loading={proposalBusy && !proposalSelection}
            busy={proposalBusy}
            directEdit={draft.sourceAction === "edit"}
            quickEdit={draft.sourceAction === "quick"}
            allowApply={sourceMutationSupported}
            theme={chromeTheme}
            replacementText={draft.replacementText ?? ""}
            intent={draft.proposalIntent ?? ""}
            onReplacementChange={(value) => onDraft({ ...draft, replacementText: value })}
            onIntentChange={(value) => onDraft({ ...draft, proposalIntent: value })}
            onCreate={onCreateProposal}
            onCreateAndApply={onCreateAndApplyProposal}
            onApply={onApplyProposal}
            onClose={onCloseProposal}
          />
        )}
        {endTargetRows.filter((target) => !foldedTargetIds.has(target.target_id)).map((target) => (
          <ReviewTargetEndActions
            key={"end:" + target.target_id}
            target={target}
            busy={busy}
            onMark={(state, advance) => onMark(target, state, advance)}
            onComment={() => onComment(target)}
            onAsk={onAsk ? () => onAsk(target) : undefined}
            onContext={() => onContext(target, "context")}
          />
        ))}
        {targetRows.map((target) => (
          <ReviewTargetHeader
            key={target.target_id}
            target={target}
            active={target.target_id === activeTargetId}
            busy={busy}
            scopeTop={targetHasContinuousScope(target, targetsRef.current)}
            comparisonMode={comparisonMode}
            folded={foldedTargetIds.has(target.target_id)}
            onToggleFold={() => setTargetFolded(target.target_id, !foldedTargetIds.has(target.target_id))}
            onFocus={() => onActiveTarget(target.target_id)}
            onMark={(state, advance) => onMark(target, state, advance)}
            onComment={() => onComment(target)}
            onAsk={onAsk ? () => onAsk(target) : undefined}
            onContext={(tab) => onContext(target, tab)}
          />
        ))}
      </div>
    );
  }, [
    activeTargetId,
    busy,
    chromeTheme,
    draft,
    comparisonMode,
    foldedTargetIds,
    inlineAnnotations,
    onActiveTarget,
    onAsk,
    onAskSelection,
    onComment,
    onContext,
    onCreateAndApplyProposal,
    onCreateProposal,
    onCloseProposal,
    onDraft,
    onEditSource,
    onMark,
    onSetAnnotationState,
    onSuggestEdit,
    proposalBusy,
    proposalSelection,
    activeProposal,
    setTargetFolded,
    sourceMutationSupported,
    submit,
    targets,
    targetsById,
    snapshotComparison,
  ]);

  const decorateDedicatedHeader = useCallback((node: HTMLElement, phase: "mount" | "update" | "unmount") => {
    if (phase === "unmount") return;
    const root: ParentNode = node.shadowRoot ?? node;
    const header = root.querySelector<HTMLElement>("[data-header-content]");
    const title = root.querySelector<HTMLElement>("[data-title]");
    if (!header || !title) return;
    header.style.cursor = "pointer";
    title.style.textDecorationLine = "";
    title.style.textUnderlineOffset = "2px";
    header.onmouseenter = () => { title.style.textDecorationLine = "underline"; };
    header.onmouseleave = () => { title.style.textDecorationLine = ""; };
  }, []);

  const decorateExpandableDiffGaps = useCallback((
    node: HTMLElement,
    instance: { expandHunk?: (hunkIndex: number, direction: "up" | "down" | "both", lineCount?: number) => void } | null,
    phase: "mount" | "update" | "unmount",
  ) => {
    if (phase === "unmount") return;
    const root: ParentNode = node.shadowRoot ?? node;
    for (const separator of root.querySelectorAll<HTMLElement>("[data-separator][data-expand-index]")) {
      const label = separator.querySelector<HTMLElement>("[data-unmodified-lines]");
      if (!label) continue;
      const expandIndex = Number.parseInt(separator.dataset.expandIndex ?? "", 10);
      if (!Number.isFinite(expandIndex)) continue;
      const context = label.textContent?.trim() || "unchanged context";
      const hiddenLineMatch = context.match(/([0-9][0-9,]*)/);
      const hiddenLineCount = hiddenLineMatch
        ? Number.parseInt(hiddenLineMatch[1].replaceAll(",", ""), 10)
        : DIFF_CONTEXT_EXPANSION_LINES;
      const direction = separator.hasAttribute("data-separator-first")
        ? "down"
        : separator.hasAttribute("data-separator-last")
          ? "up"
          : "both";

      // Pierre's built-in hit target is intentionally small. In a review UI the
      // entire collapsed-context row should behave like the affordance users see
      // in GitHub/Gerrit: click anywhere on "N unmodified lines" to reveal
      // context. Call the renderer directly instead of synthesizing a click so
      // this also works through Pierre's shadow DOM/event delegation.
      separator.style.cursor = "pointer";
      separator.onclick = (event) => {
        const clickedExpandButton = event.composedPath().some(
          (target) => target instanceof HTMLElement && target.hasAttribute("data-expand-button"),
        );
        if (clickedExpandButton) return;
        if (!instance?.expandHunk) return;
        event.preventDefault();
        event.stopPropagation();
        const lineCount = event.shiftKey
          ? Math.max(hiddenLineCount, DIFF_CONTEXT_EXPANSION_LINES)
          : hiddenLineCount <= DIFF_CONTEXT_EXPANSION_LINES
            ? hiddenLineCount
            : DIFF_CONTEXT_EXPANSION_LINES;
        instance.expandHunk(expandIndex, direction, lineCount);
      };
      separator.title = hiddenLineCount <= DIFF_CONTEXT_EXPANSION_LINES
        ? `Show all ${context}`
        : `Expand ${context} by ${DIFF_CONTEXT_EXPANSION_LINES} lines. Shift-click to expand all.`;
    }
  }, []);

  const diffOptionsFor = useCallback((path: string) => {
    const status = details[path]?.status;
    const effectiveDiffStyle: DiffStyle = status === "added" || status === "deleted"
      ? "unified"
      : resolvedDiffStyle;
    // In Snapshot A → B mode, line selection and expanded source belong
    // to B even when the surrounding Reader was initially loaded from another
    // revision. This keeps new-side anchors tied to the comparison target.
    const revisionKey = snapshotComparison?.toRevisionId ?? details[path]?.revision_id ?? revisionId ?? "";
    const diffContextKey = snapshotComparison
      ? `${snapshotComparison.fromRevisionId}:${snapshotComparison.toRevisionId}`
      : revisionKey;
    const cached = diffOptionsCacheRef.current.get(path);
    if (
      cached
      && cached.diffStyle === effectiveDiffStyle
      && cached.diffOverflow === diffOverflow
      && cached.codeTheme === codeTheme
      && cached.chromeTheme === chromeTheme
      && cached.revisionKey === diffContextKey
    ) return cached.options;
    const draftForRange = (range: SelectedLineRange | null): Draft | null => {
      if (comparisonMode || !range) return null;
      const side = markerSide(range.side ?? "additions");
      const endSide = markerSide(range.endSide ?? range.side ?? "additions");
      // An anchored action belongs to one side of a diff. Do not silently turn
      // a cross-column drag into a different range than the reviewer selected.
      if (side !== endSide) {
        setSelectionAction(null);
        setLineSelection(path, null);
        return null;
      }
      setLineSelection(path, range);
      const startLine = Math.min(range.start, range.end);
      const endLine = Math.max(range.start, range.end);
      const target = targetAtLine(targetsRef.current, path, side, startLine, activeTargetIdRef.current);
      if (target) onActiveTargetRef.current(target.target_id);
      const recovered = draftRef.current?.recovered ? draftRef.current : null;
      const expanded = expandedSourceRef.current.get(path);
      const sourceLines = expanded?.revisionId === revisionKey ? expanded.text.match(/[^\n]*\n|[^\n]+$/g) : null;
      const selectedText = side === "additions" && sourceLines && startLine >= 1 && endLine <= sourceLines.length
        ? sourceLines.slice(startLine - 1, endLine).join("")
        : undefined;
      return {
        path,
        startLine,
        endLine,
        side,
        targetUnitKey: target?.unit_key,
        ...(selectedText !== undefined ? { selectedText, selectedRevisionId: revisionKey } : {}),
        ...(recovered ? {
          body: recovered.body,
          kind: recovered.kind,
          markTarget: recovered.markTarget,
          recovered: false,
        } : {}),
      };
    };
    const openRangeDraft = (range: SelectedLineRange | null) => {
      const nextDraft = draftForRange(range);
      if (!nextDraft) return;
      setSelectionAction(null);
      openDraftPreservingViewport(nextDraft);
    };
    const openRangeActions = (range: SelectedLineRange | null) => {
      // Opening a composer selects its range, and Pierre reports that
      // controlled selection back through onLineSelected as if the reviewer had
      // just made it. Never cover the composer already open for exactly this
      // range (gutter +, header Comment, `c`) with the line menu.
      const open = draftRef.current;
      if (
        range && open && !open.fileLevel && open.path === path
        && open.startLine === Math.min(range.start, range.end)
        && open.endLine === Math.max(range.start, range.end)
        && open.side === markerSide(range.side ?? "additions")
      ) return;
      const actionDraft = draftForRange(range);
      if (!actionDraft || !range) return;
      const pointer = lastPointerRef.current;
      const diffRect = diffNodesRef.current.get(path)?.getBoundingClientRect();
      setSelectionAction({
        path,
        range,
        draft: actionDraft,
        clientX: pointer.clientX || (diffRect ? diffRect.left + Math.min(Math.max(diffRect.width - 24, 24), 180) : 180),
        clientY: pointer.clientY || (diffRect ? diffRect.top + 56 : 120),
      });
    };
    const openRangeActionsAfterDoubleClickWindow = (range: SelectedLineRange | null) => {
      if (!range || range.start !== range.end) {
        cancelPendingSingleLineAction();
        openRangeActions(range);
        return;
      }
      cancelPendingSingleLineAction();
      pendingSingleLineActionRef.current = window.setTimeout(() => {
        pendingSingleLineActionRef.current = null;
        openRangeActions(range);
      }, 360);
    };
    const openQuickEdit = (lineNumber: number, rawSide: string) => {
      const side = markerSide(rawSide);
      if (comparisonMode || side !== "additions") return;
      const range: SelectedLineRange = {
        start: lineNumber,
        end: lineNumber,
        side: "additions",
        endSide: "additions",
      };
      const quickDraft = draftForRange(range);
      if (!quickDraft) return;
      setSelectionAction(null);
      onQuickEditRef.current(quickDraft);
    };
    const options = {
      diffStyle: effectiveDiffStyle,
      overflow: diffOverflow,
      theme: themeDefinition.pierreTheme,
      themeType: chromeTheme,
      hunkSeparators: "line-info" as const,
      expansionLineCount: DIFF_CONTEXT_EXPANSION_LINES,
      // Arbitrary-source comparisons cannot safely fetch hidden context. A
      // Snapshot comparison can: load Snapshot A's immutable new side as the
      // old file and Snapshot B's immutable new side as the new file. Never
      // splice Review-base -> Snapshot contents into a Snapshot A -> B patch.
      loadDiffFiles: comparisonMode ? undefined : async () => {
        const showContextLoad = (state: ContextLoadState | undefined) => {
          if (state) contextLoadRef.current.set(path, state);
          else contextLoadRef.current.delete(path);
          const node = diffNodesRef.current.get(path);
          if (node) markContextLoad(node, state);
        };
        showContextLoad("loading");
        let contents:
          | Awaited<ReturnType<typeof fetchFileContents>>
          | Awaited<ReturnType<typeof fetchRevisionComparisonFileContents>>;
        try {
          contents = snapshotComparison
            ? await fetchRevisionComparisonFileContents(
                currentReviewId(),
                path,
                snapshotComparison.fromRevision,
                snapshotComparison.toRevision,
              )
            : await fetchFileContents(currentReviewId(), path, revisionKey);
          if (snapshotComparison) {
            const fromRevisionId = "from_revision_id" in contents ? String(contents.from_revision_id) : "";
            const toRevisionId = "to_revision_id" in contents ? String(contents.to_revision_id) : "";
            if (
              fromRevisionId !== snapshotComparison.fromRevisionId
              || toRevisionId !== snapshotComparison.toRevisionId
            ) {
              throw new Error("Snapshot comparison context changed while loading");
            }
          }
          if (contents.new_content == null) {
            throw new Error(`Cannot expand unchanged context for deleted file ${path}`);
          }
        } catch (reason) {
          showContextLoad("error");
          throw reason;
        }
        showContextLoad(undefined);
        const targetRevisionId = snapshotComparison?.toRevisionId ?? contents.revision_id;
        expandedSourceRef.current.set(path, { revisionId: targetRevisionId, text: contents.new_content });
        return {
          oldFile: contents.old_content == null
            ? null
            : {
                name: contents.old_path || path,
                contents: contents.old_content,
                cacheKey: snapshotComparison
                  ? `${snapshotComparison.fromRevisionId}:new:${contents.old_path || path}`
                  : `${contents.revision_id}:old:${contents.old_path || path}`,
              },
          newFile: {
            name: path,
            contents: contents.new_content,
            cacheKey: snapshotComparison
              ? `${snapshotComparison.toRevisionId}:new:${path}`
              : `${contents.revision_id}:new:${path}`,
          },
        };
      },
      enableLineSelection: !comparisonMode,
      enableGutterUtility: !comparisonMode,
      lineHoverHighlight: "both" as const,
      onPostRender: (
        node: HTMLElement,
        instance: { expandHunk?: (hunkIndex: number, direction: "up" | "down" | "both", lineCount?: number) => void } | null,
        phase: "mount" | "update" | "unmount",
      ) => {
        registerDiffNode(path, node, phase);
        decorateDedicatedHeader(node, phase);
        decorateExpandableDiffGaps(node, instance, phase);
        // A new renderer starts with no context fetch of its own; a re-render
        // keeps showing the one in flight or the failure that awaits a retry.
        if (phase === "mount") contextLoadRef.current.delete(path);
        if (phase !== "unmount") markContextLoad(node, contextLoadRef.current.get(path));
        decorateCssColorSwatches(node, path, phase);
        decorateDiffAccessibility(node, path, phase);
        if (comparisonMode) return;
        const pendingScopeFrame = scopeFrameByPathRef.current.get(path);
        if (pendingScopeFrame !== undefined) {
          window.cancelAnimationFrame(pendingScopeFrame);
          scopeFrameByPathRef.current.delete(path);
        }
        if (phase === "unmount") {
          decorateReviewTargetFolds(node, path, targetsRef.current, foldedTargetIdsRef.current, phase);
          decorateReviewScopeBoxes(node, path, targetsRef.current, phase, chromeTheme, effectiveDiffStyle, activeTargetIdRef.current);
          return;
        }
        const applyScope = () => {
          decorateReviewTargetFolds(node, path, targetsRef.current, foldedTargetIdsRef.current, phase);
          decorateReviewScopeBoxes(
            node,
            path,
            targetsRef.current,
            phase,
            chromeTheme,
            effectiveDiffStyle,
            activeTargetIdRef.current,
          );
        };
        // Updates retain the existing overlay until that settling frame instead
        // of repeatedly tearing it down during Pierre's render burst.
        if (phase === "mount") applyScope();
        const frame = window.requestAnimationFrame(() => {
          scopeFrameByPathRef.current.delete(path);
          applyScope();
        });
        scopeFrameByPathRef.current.set(path, frame);
      },
      // The gutter + remains a direct "comment here" affordance. Pierre emits
      // onLineSelectionEnd after onGutterUtilityClick for the same gesture, so
      // suppress exactly that follow-up end event or the popup would appear too.
      onGutterUtilityClick: (range: SelectedLineRange | null) => {
        suppressNextSelectionEndRef.current = true;
        suppressNextSelectionCommitRef.current = true;
        openRangeDraft(range);
      },
      onLineSelectionChange: (range: SelectedLineRange | null) => {
        if (comparisonMode) return;
        setSelectionAction(null);
        setLineSelection(path, range);
      },
      // Double-click anywhere on an editable/new-side line for Quick Edit.
      // This is intentionally line-level: the reviewer can act without hunting
      // for a small gutter target, and the first click still participates in
      // the normal selection model.
      // Pierre names a diff line's side `annotationSide`; a missing side would read as the
      // new side and turn a double-click on a deleted line into an edit of another line.
      onLineClick: ({ lineNumber, annotationSide: rawSide, event }: { lineNumber: number; annotationSide: string; event: MouseEvent }) => {
        // The browser already owns double-click timing and exposes it as
        // MouseEvent.detail. A second LemonCrow click clock can disagree with
        // OS/browser accessibility settings and turn unrelated clicks into an
        // edit, so keep only the single-click deferral below.
        if (event.detail !== 2) return;
        event.preventDefault();
        cancelPendingSingleLineAction();
        openQuickEdit(lineNumber, rawSide);
      },
      // Normal code selection opens the contextual actions automatically on
      // pointer-up. Gutter/comment affordances are handled separately above.
      onLineSelectionEnd: (range: SelectedLineRange | null) => {
        if (suppressNextSelectionEndRef.current) {
          suppressNextSelectionEndRef.current = false;
          return;
        }
        const key = range
          ? `${range.side}:${range.start}:${range.endSide ?? range.side}:${range.end}`
          : "";
        lastSelectionEndKeyRef.current = key;
        // A single line click is also a one-line selection. Delay only that
        // case long enough to let a second click become Quick Edit; range drags
        // still get their action menu immediately.
        openRangeActionsAfterDoubleClickWindow(range);
        window.setTimeout(() => {
          if (lastSelectionEndKeyRef.current === key) lastSelectionEndKeyRef.current = "";
        }, 0);
      },
      // Pierre commits every finished line selection after selection-end. Use
      // this as a fallback because some real drag gestures skip selection-end
      // in the browser even though the committed range is correct. Gutter '+'
      // commits are explicitly suppressed above.
      onLineSelected: (range: SelectedLineRange | null) => {
        if (suppressNextSelectionCommitRef.current) {
          suppressNextSelectionCommitRef.current = false;
          return;
        }
        const key = range
          ? `${range.side}:${range.start}:${range.endSide ?? range.side}:${range.end}`
          : "";
        if (key && lastSelectionEndKeyRef.current === key) return;
        openRangeActionsAfterDoubleClickWindow(range);
      },
    };
    diffOptionsCacheRef.current.set(path, {
      diffStyle: effectiveDiffStyle,
      diffOverflow,
      codeTheme,
      chromeTheme,
      revisionKey: diffContextKey,
      options,
    });
    return options;
  }, [cancelPendingSingleLineAction, chromeTheme, codeTheme, comparisonMode, decorateDedicatedHeader, decorateExpandableDiffGaps, details, diffOverflow, openDraftPreservingViewport, registerDiffNode, resolvedDiffStyle, revisionId, setLineSelection, snapshotComparison, themeDefinition]);
  useLayoutEffect(() => {
    if (comparisonMode) return;
    const currentPath = targetsById.get(activeTargetId)?.path ?? "";
    activeScopePathRef.current = currentPath;
    for (const [path, node] of diffNodesRef.current) {
      const status = details[path]?.status;
      const effectiveDiffStyle: DiffStyle = status === "added" || status === "deleted"
        ? "unified"
        : resolvedDiffStyle;
      decorateReviewTargetFolds(
        node,
        path,
        targets,
        foldedTargetIds,
        "update",
      );
      decorateReviewScopeBoxes(
        node,
        path,
        targets,
        "update",
        chromeTheme,
        effectiveDiffStyle,
        activeTargetId,
      );
    }
  }, [activeTargetId, chromeTheme, comparisonMode, details, foldedTargetIds, resolvedDiffStyle, targets, targetsById]);

  useLayoutEffect(() => {
    const anchor = foldAnchorRef.current;
    foldAnchorRef.current = null;
    const root = scrollRef.current;
    const item = anchor ? itemElementsRef.current.get(anchor.path) : undefined;
    if (!anchor || !root || !item || Date.now() < programmaticScrollUntilRef.current) return;
    let el: HTMLElement | null = null;
    if (anchor.header) {
      el = [...item.querySelectorAll<HTMLElement>(`.review-target-header[data-review-target-id="${CSS.escape(anchor.header)}"]`)]
        .find((node) => !node.closest("[data-review-sticky-copy]") && node.getClientRects().length > 0) ?? null;
    } else if (anchor.line) {
      for (const host of item.querySelectorAll<HTMLElement>("diffs-container")) {
        for (const row of (host.shadowRoot ?? host).querySelectorAll<HTMLElement>(`[data-line="${anchor.line}"]`)) {
          if (row.getAttribute("data-line-type") !== anchor.type || row.getClientRects().length === 0) continue;
          // A split diff's two columns can show the same number and type (context).
          if ((row.closest("[data-code]")?.hasAttribute("data-deletions") ? "deletions" : "additions") !== anchor.side) continue;
          el = row;
          break;
        }
        if (el) break;
      }
    } else {
      el = item;
    }
    if (!el) return;
    const delta = el.getBoundingClientRect().top - anchor.top;
    if (Math.abs(delta) > 0.5) root.scrollTop += delta;
  }, [foldedTargetIds, fullyJudgedPaths, collapsedPaths]);

  const renderCollapsedHeader = useCallback((path: string) => {
    const firstTargetId = targetsByPath.get(path)?.[0]?.target_id;
    return (
      <div
        className="flex min-h-10 items-center gap-3 border-y border-border bg-background px-3 py-1.5"
        data-review-target-id={firstTargetId}
        data-testid={`code-item:${path}`}
        data-collapsed="true"
      >
        {comparisonMode ? (
          <div className="inline-flex min-w-0 flex-1 items-center gap-2 truncate font-mono text-body text-neutral-200">
            <FileTypeIcon path={path} />
            <span className="truncate" title={path}>{path}</span>
          </div>
        ) : (
          <UiButton unstyled
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "file", path })}
            aria-label={`Open ${path} in dedicated tab`}
            tooltip="Open file in dedicated tab"
            className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 truncate text-left font-mono text-body text-neutral-200 underline-offset-2 hover:text-neutral-50 hover:underline"
          >
            <FileTypeIcon path={path} />
            <span className="truncate" title={path}>{path}</span>
            <ExternalLink size={11} className="shrink-0 text-muted-foreground" />
          </UiButton>
        )}
        {headerControls(path)}
      </div>
    );
  }, [comparisonMode, headerControls, targetsByPath]);

  const renderMarkdown = useCallback((item: MarkdownStreamItem) => {
    const path = item.id;
    const detail = details[path];
    if (detail?.preview?.kind !== "markdown") return null;
    if (item.collapsed) return renderCollapsedHeader(path);

    const targetRows = (targetsByPath.get(path) ?? [])
      .filter((target) => !(snapshotComparison && target.kind === "hunk" && !target.symbol));
    const shownComments = partition(inlineAnnotations, path).anchored.filter((comment) => !comment.parent_id);
    const composing = Boolean(draft && draft.path === path);
    const draftTarget = composing && draft
      ? (targets.find((target) => target.unit_key === draft.targetUnitKey)
        ?? targetAtLine(targets, path, draft.side, draft.startLine, activeTargetId))
      : null;
    const firstTargetId = targetRows[0]?.target_id;

    return (
      <section
        className="border-b border-neutral-900 bg-background"
        data-testid={`markdown-stream:${path}`}
        onMouseEnter={() => {
          activeMarkdownPathRef.current = path;
          setActiveSurfaceKey("");
        }}
        onFocusCapture={() => {
          activeMarkdownPathRef.current = path;
          setActiveSurfaceKey("");
        }}
      >
        <div
          className="sticky top-0 z-20 flex min-h-10 items-center gap-3 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur"
          data-review-target-id={firstTargetId}
        >
          <UiButton unstyled
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "file", path })}
            aria-label={`Open ${path} in dedicated tab`}
            tooltip="Open file in dedicated tab"
            className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 truncate text-left font-mono text-body text-neutral-200 underline-offset-2 hover:text-neutral-50 hover:underline"
          >
            <FileTypeIcon path={path} />
            <span className="truncate" title={path}>{path}</span>
            <ExternalLink size={11} className="shrink-0 text-muted-foreground" />
          </UiButton>
          {headerControls(path)}
        </div>
        <MarkdownReviewPreview
          preview={detail.preview}
          patch={detail.patch}
          documentPath={path}
          revisionId={detail.revision_id ?? ""}
          status={detail.status}
          mode={item.mode}
          theme={(markdownPreviewThemes[path] ?? "auto") === "auto" ? chromeTheme : markdownPreviewThemes[path] as Theme}
          onSelect={(side, startLine, endLine) => {
            const target = targetAtLine(targets, path, side, startLine, activeTargetId);
            if (target) onActiveTarget(target.target_id);
            const recovered = draft?.recovered ? draft : null;
            onDraft({
              path,
              startLine,
              endLine,
              side,
              targetUnitKey: target?.unit_key,
              ...(recovered ? {
                body: recovered.body,
                kind: recovered.kind,
                markTarget: recovered.markTarget,
                recovered: false,
              } : {}),
            });
          }}
        />
        {(targetRows.length > 0 || shownComments.length > 0 || composing) && (
          <div className="space-y-1 border-t border-neutral-900 bg-background px-3 py-3">
            {targetRows.map((target) => (
              <ReviewTargetHeader
                key={target.target_id}
                target={target}
                active={target.target_id === activeTargetId}
                busy={busy}
                comparisonMode={comparisonMode}
                onFocus={() => onActiveTarget(target.target_id)}
                onMark={(state, advance) => onMark(target, state, advance)}
                onComment={() => onComment(target)}
                onAsk={onAsk ? () => onAsk(target) : undefined}
                onContext={(tab) => onContext(target, tab)}
              />
            ))}
            {(shownComments.length > 0 || (composing && draft?.mode !== "proposal")) && (
              <AnnotationCard
                comments={shownComments}
                all={inlineAnnotations}
                label={draft?.fileLevel ? "File comment" : "Markdown comment"}
                composing={composing && draft?.mode !== "proposal"}
                busy={busy}
                targetMarkAvailable={Boolean(draftTarget)}
                draftBody={composing ? draft?.body : ""}
                draftKind={composing ? draft?.kind : undefined}
                draftMarkTarget={composing ? draft?.markTarget : undefined}
                onDraftChange={(fields) => { if (draft) onDraft({ ...draft, ...fields }); }}
                onAsk={onAskSelection && composing && draft ? () => onAskSelection(draft) : undefined}
                onSuggestEdit={composing && draft && !draft.fileLevel && draft.side === "additions"
                  ? () => onSuggestEdit(draft)
                  : undefined}
                onEditSource={sourceMutationSupported && composing && draft && !draft.fileLevel && draft.side === "additions"
                  ? () => onEditSource(draft)
                  : undefined}
                onSubmit={(body, kind, parentId, markTarget) => submit(
                  path,
                  body,
                  kind,
                  parentId,
                  markTarget,
                  draftTarget?.unit_key,
                )}
                onCancel={() => onDraft(null)}
                onResolve={(id) => onSetAnnotationState(id, "resolved")}
                onReopen={(id) => onSetAnnotationState(id, "open")}
                onFindingDisposition={onFindingDisposition}
                onFindingRequestChange={requestFindingChange}
                onFindingFix={fixFinding}
                onFindingAsk={onAskSelection ? askAboutFinding : undefined}
              />
            )}
            {composing && draft?.mode === "proposal" && (
              <ReviewChangeProposalComposer
                selection={proposalSelection}
                proposal={activeProposal}
                loading={proposalBusy && !proposalSelection}
                busy={proposalBusy}
                directEdit={draft.sourceAction === "edit"}
                quickEdit={draft.sourceAction === "quick"}
                allowApply={sourceMutationSupported}
                theme={chromeTheme}
                replacementText={draft.replacementText ?? ""}
                intent={draft.proposalIntent ?? ""}
                onReplacementChange={(value) => onDraft({ ...draft, replacementText: value })}
                onIntentChange={(value) => onDraft({ ...draft, proposalIntent: value })}
                onCreate={onCreateProposal}
                onCreateAndApply={onCreateAndApplyProposal}
                onApply={onApplyProposal}
                onClose={onCloseProposal}
              />
            )}
          </div>
        )}
      </section>
    );
  }, [
    activeTargetId,
    busy,
    details,
    draft,
    chromeTheme,
    headerControls,
    comparisonMode,
    inlineAnnotations,
    markdownPreviewThemes,
    onActiveTarget,
    onAsk,
    onAskSelection,
    onComment,
    onContext,
    onDraft,
    onMark,
    onSetAnnotationState,
    renderCollapsedHeader,
    submit,
    targets,
    targetsByPath,
    snapshotComparison,
  ]);
  const renderCsv = useCallback((item: CsvStreamItem) => {
    const path = item.id;
    const detail = details[path];
    if (detail?.preview?.kind !== "csv") return null;
    if (item.collapsed) return renderCollapsedHeader(path);
    const firstTargetId = targetsByPath.get(path)?.[0]?.target_id;

    return (
      <section
        className="border-b border-neutral-900 bg-background"
        data-testid={`csv-stream:${path}`}
        onMouseEnter={() => {
          activeMarkdownPathRef.current = path;
          setActiveSurfaceKey("");
        }}
        onFocusCapture={() => {
          activeMarkdownPathRef.current = path;
          setActiveSurfaceKey("");
        }}
      >
        <div
          className="sticky top-0 z-20 flex min-h-10 items-center gap-3 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur"
          data-review-target-id={firstTargetId}
        >
          <UiButton unstyled
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "file", path })}
            aria-label={`Open ${path} in dedicated tab`}
            tooltip="Open file in dedicated tab"
            className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 truncate text-left font-mono text-body text-neutral-200 underline-offset-2 hover:text-neutral-50 hover:underline"
          >
            <FileTypeIcon path={path} />
            <span className="truncate" title={path}>{path}</span>
            <ExternalLink size={11} className="shrink-0 text-muted-foreground" />
          </UiButton>
          {headerControls(path)}
        </div>
        <CsvReviewPreview
          preview={detail.preview}
          revisionId={detail.revision_id ?? ""}
          status={detail.status}
          mode={item.mode}
        />
      </section>
    );
  }, [details, headerControls, renderCollapsedHeader, targetsByPath]);
  const renderMedia = useCallback((item: MediaStreamItem) => {
    const path = item.id;
    const detail = details[path];
    if (detail?.preview?.kind !== "media") return null;
    if (item.collapsed) return renderCollapsedHeader(path);
    const firstTargetId = targetsByPath.get(path)?.[0]?.target_id;

    return (
      <section
        className="border-b border-neutral-900 bg-background"
        data-testid={`media-stream:${path}`}
        onMouseEnter={() => {
          activeMarkdownPathRef.current = path;
          setActiveSurfaceKey("");
        }}
        onFocusCapture={() => {
          activeMarkdownPathRef.current = path;
          setActiveSurfaceKey("");
        }}
      >
        <div
          className="sticky top-0 z-20 flex min-h-10 items-center gap-3 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur"
          data-review-target-id={firstTargetId}
        >
          <UiButton unstyled
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "file", path })}
            aria-label={`Open ${path} in dedicated tab`}
            tooltip="Open file in dedicated tab"
            className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 truncate text-left font-mono text-body text-neutral-200 underline-offset-2 hover:text-neutral-50 hover:underline"
          >
            <FileTypeIcon path={path} />
            <span className="truncate" title={path}>{path}</span>
            <ExternalLink size={11} className="shrink-0 text-muted-foreground" />
          </UiButton>
          {headerControls(path)}
        </div>
        <MediaReviewPreview
          preview={detail.preview}
          documentPath={path}
          revisionId={detail.revision_id ?? ""}
          status={detail.status}
          mode={item.mode}
        />
      </section>
    );
  }, [details, headerControls, renderCollapsedHeader, targetsByPath]);
  const renderWebSurface = useCallback((item: WebSurfaceStreamItem) => {
    const surfaceKey = `${item.surface.provider}:${item.surface.id}`;
    const firstTargetId = item.surface.affected_paths
      .map((path) => targetsByPath.get(path)?.[0]?.target_id)
      .find((id): id is string => Boolean(id));
    const executable = item.surface.capabilities.includes("execute") && Boolean(item.surface.runtime);
    const validationTarget = firstTargetId ? targetsById.get(firstTargetId) : undefined;
    return (
      <section
        className="border-b border-sky-950/60 bg-background"
        data-testid={`web-surface:${surfaceKey}`}
        data-review-target-id={firstTargetId}
        data-review-surface-key={surfaceKey}
        onMouseEnter={() => {
          setActiveSurfaceKey(surfaceKey);
          activeMarkdownPathRef.current = "";
        }}
      >
        {executable && (
          <div className="border-b border-border bg-surface-sunken px-3 py-2">
            <RuntimeValidationControl
              state={runtimeValidations[surfaceKey]}
              busy={runtimeValidationBusy.has(surfaceKey)}
              target={validationTarget}
              onRun={() => onValidateSurface?.(item.surface)}
              onCancel={onCancelRuntimeValidation ? (jobId) => onCancelRuntimeValidation(item.surface, jobId) : undefined}
              onInspect={(target) => onContext(target, "checks")}
              onAsk={onAsk}
            />
          </div>
        )}
        <WebReviewPreview
          preview={item.preview}
          documentPath={item.documentPath}
          revisionId={details[item.documentPath]?.revision_id ?? ""}
          surfaceKey={surfaceKey}
          surfaceTitle={item.surface.title || item.surface.locator || "Web surface"}
          serviceName={configuredSurfaceName(item.surface)}
          affectedPaths={item.surface.affected_paths}
          framework={item.preview.framework}
          chromeTheme={chromeTheme}
          active={activeSurfaceKey === surfaceKey}
          routeOptions={webRouteOptions}
          onActivate={() => {
            setActiveSurfaceKey(surfaceKey);
            activeMarkdownPathRef.current = "";
          }}
          onOpenPath={scrollToFile}
          onOpenDedicated={() => openDedicatedReviewItem({ kind: "surface", provider: item.surface.provider, id: item.surface.id })}
          onNavigateSurface={(nextSurfaceKey) => {
            setSelectedWebSurfaceKey(nextSurfaceKey);
            setActiveSurfaceKey(nextSurfaceKey);
          }}
        />
      </section>
    );
  }, [activeSurfaceKey, chromeTheme, details, onAsk, onCancelRuntimeValidation, onContext, onValidateSurface, runtimeValidationBusy, runtimeValidations, scrollToFile, targetsById, targetsByPath, webRouteOptions]);

  const renderApiSurface = useCallback((item: ApiSurfaceStreamItem) => {
    const surfaceKey = `${item.surface.provider}:${item.surface.id}`;
    const firstTargetId = item.surface.affected_paths
      .map((path) => targetsByPath.get(path)?.[0]?.target_id)
      .find((id): id is string => Boolean(id));
    const run = surfaceRuns[surfaceKey];
    const executable = item.surface.capabilities.includes("execute") && Boolean(item.surface.runtime);
    const comparable = executable && item.surface.capabilities.includes("compare");
    const statusCode = run?.new?.data?.status_code;
    const unavailableRunner = isUnavailableRunnerError(run?.error);
    const statusLabel = surfaceBusy.has(surfaceKey)
      ? "Checking…"
      : unavailableRunner
        ? "Unavailable"
        : run?.error
          ? "Failed"
          : run?.new
            ? (typeof statusCode === "number" ? `HTTP ${statusCode}` : run.new.status)
            : executable
              ? "Ready"
              : "Not runnable";
    const statusTone = surfaceBusy.has(surfaceKey)
      ? "text-amber-400/70"
      : unavailableRunner
        ? "text-amber-400/80"
        : run?.error || run?.new?.status === "failed"
          ? "text-rose-400/80"
          : run?.new?.status === "passed"
            ? "text-emerald-400/80"
            : "text-muted-foreground";
    return (
      <section
        className="border-b border-cyan-950/60 bg-background"
        data-testid={`api-surface:${surfaceKey}`}
        data-review-target-id={firstTargetId}
        data-review-surface-key={surfaceKey}
        onMouseEnter={() => {
          setActiveSurfaceKey(surfaceKey);
          activeMarkdownPathRef.current = "";
        }}
        onFocusCapture={() => {
          setActiveSurfaceKey(surfaceKey);
          activeMarkdownPathRef.current = "";
        }}
      >
        <div className="review-surface-header sticky top-0 z-20 flex min-h-10 items-center gap-2 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur">
          <UiButton unstyled
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "surface", provider: item.surface.provider, id: item.surface.id })}
            aria-label={`Open API surface ${item.surface.title || item.surface.id} in dedicated tab`}
            tooltip="Open API surface in dedicated tab"
            className="review-surface-kind inline-flex shrink-0 items-center gap-1 text-caption font-medium uppercase tracking-[0.14em] text-cyan-400/70 hover:text-cyan-300"
          >
            API <span className="normal-case tracking-normal text-cyan-300/90">· {configuredSurfaceName(item.surface)}</span> <ExternalLink size={10} />
          </UiButton>
          <div className="review-surface-title min-w-0">
            {apiSurfaceOptions.length > 1 ? (
              <SurfacePicker
                ariaLabel="API endpoint"
                value={surfaceKey}
                storageKey={`lemoncrow-review-surface-filter:${currentReviewId()}:api`}
                placeholder="Search affected endpoints, changed files…"
                options={apiSurfaceOptions}
                onChange={(nextSurfaceKey) => {
                  setSelectedApiSurfaceKey(nextSurfaceKey);
                  setActiveSurfaceKey(nextSurfaceKey);
                  const nextSurface = surfacesByKey.get(nextSurfaceKey);
                  if (nextSurface && surfaceAutoRankable(nextSurface) && !surfaceImpactScores[nextSurfaceKey]) {
                    void loadSurfaceImpact(nextSurface, 4).catch(() => undefined);
                  }
                }}
              />
            ) : (
              <span className="block min-w-0 truncate font-mono text-body text-foreground">{item.surface.locator || item.surface.title}</span>
            )}
          </div>
          <span className={`review-surface-status shrink-0 text-caption ${statusTone}`}>{statusLabel}</span>
          {surfaceImpactBusy && apiSurfaces.some(surfaceAutoRankable) && (
            <span className="review-surface-meta shrink-0 font-mono text-micro text-muted-foreground" title="Ranking affected safe endpoints by immutable old/new response delta">
              Ranking responses…
            </span>
          )}
          <span className="review-surface-spacer flex-1" />
          {comparable && (
            <span className="review-surface-mode flex overflow-hidden rounded border border-border">
              {(["preview", "compare"] as const).map((mode, index) => (
                <UiButton unstyled
                  key={mode}
                  type="button"
                  aria-label={mode}
                  aria-pressed={item.mode === mode}
                  tooltip={`${mode[0]?.toUpperCase()}${mode.slice(1)} · ${index + 1}`}
                  onClick={() => setSurfaceModes((current) => ({ ...current, [surfaceKey]: mode }))}
                  className={`px-2 py-0.5 text-caption capitalize ${item.mode === mode ? "bg-neutral-800 text-foreground" : "text-muted-foreground hover:text-neutral-50"}`}
                >
                  {mode} <span className="ml-1 text-caption text-muted-foreground">{index + 1}</span>
                </UiButton>
              ))}
            </span>
          )}
        </div>
        {executable && (
          <div className="border-b border-border bg-surface-sunken px-3 py-2">
            <RuntimeValidationControl
              state={runtimeValidations[surfaceKey]}
              busy={runtimeValidationBusy.has(surfaceKey)}
              target={firstTargetId ? targetsById.get(firstTargetId) : undefined}
              onRun={() => onValidateSurface?.(item.surface)}
              onCancel={onCancelRuntimeValidation ? (jobId) => onCancelRuntimeValidation(item.surface, jobId) : undefined}
              onInspect={(target) => onContext(target, "checks")}
              onAsk={onAsk}
            />
          </div>
        )}
        <ApiReviewPreview
          surface={item.surface}
          mode={item.mode}
          run={run}
          running={surfaceBusy.has(surfaceKey)}
          active={activeSurfaceKey === surfaceKey}
          onActivate={() => {
            setActiveSurfaceKey(surfaceKey);
            activeMarkdownPathRef.current = "";
          }}
          onModeChange={(mode) => setSurfaceModes((current) => ({ ...current, [surfaceKey]: mode }))}
          onRun={(compare) => onRunSurface?.(item.surface, compare)}
        />
      </section>
    );
  }, [activeSurfaceKey, apiSurfaceOptions, apiSurfaces, loadSurfaceImpact, onAsk, onCancelRuntimeValidation, onContext, onRunSurface, onValidateSurface, runtimeValidationBusy, runtimeValidations, surfaceBusy, surfaceImpactBusy, surfaceImpactScores, surfaceRuns, surfacesByKey, targetsById, targetsByPath]);

  const renderServiceSurface = useCallback((item: ServiceSurfaceStreamItem) => {
    const surfaceKey = `${item.surface.provider}:${item.surface.id}`;
    const runner = String(item.surface.metadata.runner ?? "");
    const runtimeLabel = runner === "docker-compose" ? "Docker" : "Runtime";
    const serviceLabel = String(item.surface.metadata.service ?? item.surface.title ?? item.surface.id);
    const firstTargetId = item.surface.affected_paths
      .map((path) => targetsByPath.get(path)?.[0]?.target_id)
      .find((id): id is string => Boolean(id));
    const run = surfaceRuns[surfaceKey];
    const executable = item.surface.capabilities.includes("execute") && Boolean(item.surface.runtime);
    const unavailableRunner = isUnavailableRunnerError(run?.error);
    const statusLabel = surfaceBusy.has(surfaceKey)
      ? "Checking…"
      : unavailableRunner
        ? "Unavailable"
        : run?.error
          ? "Failed"
          : run?.new
            ? (run.new.status === "passed" ? "Valid" : run.new.status)
            : executable
              ? "Ready"
              : "Not runnable";
    const statusTone = surfaceBusy.has(surfaceKey)
      ? "text-amber-400/70"
      : unavailableRunner
        ? "text-amber-400/80"
        : run?.error || run?.new?.status === "failed"
          ? "text-rose-400/80"
          : run?.new?.status === "passed"
            ? "text-emerald-400/80"
            : "text-muted-foreground";
    return (
      <section
        className="border-b border-violet-950/60 bg-background"
        data-testid={`service-surface:${surfaceKey}`}
        data-review-target-id={firstTargetId}
        data-review-surface-key={surfaceKey}
        onMouseEnter={() => {
          setActiveSurfaceKey(surfaceKey);
          activeMarkdownPathRef.current = "";
        }}
        onFocusCapture={() => {
          setActiveSurfaceKey(surfaceKey);
          activeMarkdownPathRef.current = "";
        }}
      >
        <div className="review-surface-header sticky top-0 z-20 flex min-h-10 items-center gap-2 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur">
          <UiButton unstyled
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "surface", provider: item.surface.provider, id: item.surface.id })}
            aria-label={`Open runtime surface ${item.surface.title || item.surface.id} in dedicated tab`}
            tooltip="Open runtime surface in dedicated tab"
            className="review-surface-kind inline-flex shrink-0 items-center gap-1 text-caption font-medium uppercase tracking-[0.14em] text-violet-400/75 hover:text-violet-300"
          >
            <Boxes size={12} /> {runtimeLabel} <span className="normal-case tracking-normal text-violet-300/90">· {configuredSurfaceName(item.surface)}</span> <ExternalLink size={10} />
          </UiButton>
          <span className="review-surface-title min-w-0 truncate font-mono text-body text-foreground">{serviceLabel}</span>
          <span className={`review-surface-status shrink-0 text-caption ${statusTone}`}>{statusLabel}</span>
          <span className="review-surface-spacer flex-1" />
          <span className="review-surface-mode flex overflow-hidden rounded border border-border">
            {(["preview", "compare"] as const).map((mode, index) => (
              <UiButton unstyled
                key={mode}
                type="button"
                aria-label={`runtime-${mode}`}
                aria-pressed={item.mode === mode}
                tooltip={`${mode[0]?.toUpperCase()}${mode.slice(1)} · ${index + 1}`}
                onClick={() => setSurfaceModes((current) => ({ ...current, [surfaceKey]: mode }))}
                className={`px-2 py-0.5 text-caption capitalize ${item.mode === mode ? "bg-neutral-800 text-foreground" : "text-muted-foreground hover:text-neutral-50"}`}
              >
                {mode} <span className="ml-1 text-caption text-muted-foreground">{index + 1}</span>
              </UiButton>
            ))}
          </span>
        </div>
        {executable && (
          <div className="border-b border-border bg-surface-sunken px-3 py-2">
            <RuntimeValidationControl
              state={runtimeValidations[surfaceKey]}
              busy={runtimeValidationBusy.has(surfaceKey)}
              target={firstTargetId ? targetsById.get(firstTargetId) : undefined}
              onRun={() => onValidateSurface?.(item.surface)}
              onCancel={onCancelRuntimeValidation ? (jobId) => onCancelRuntimeValidation(item.surface, jobId) : undefined}
              onInspect={(target) => onContext(target, "checks")}
              onAsk={onAsk}
            />
          </div>
        )}
        <ServiceReviewPreview
          surface={item.surface}
          mode={item.mode}
          run={run}
          running={surfaceBusy.has(surfaceKey)}
          active={activeSurfaceKey === surfaceKey}
          onActivate={() => {
            setActiveSurfaceKey(surfaceKey);
            activeMarkdownPathRef.current = "";
          }}
          onRun={(compare) => onRunSurface?.(item.surface, compare)}
        />
      </section>
    );
  }, [activeSurfaceKey, onAsk, onCancelRuntimeValidation, onContext, onRunSurface, onValidateSurface, runtimeValidationBusy, runtimeValidations, surfaceBusy, surfaceRuns, targetsById, targetsByPath]);

  const renderPluginSurface = useCallback((item: PluginSurfaceStreamItem) => {
    const surface = item.surface;
    const surfaceKey = `${surface.provider}:${surface.id}`;
    const firstTargetId = surface.affected_paths
      .map((path) => targetsByPath.get(path)?.[0]?.target_id)
      .find((id): id is string => Boolean(id));
    const descriptor = surface.preview;
    const run = surfaceRuns[surfaceKey];
    const running = surfaceBusy.has(surfaceKey);
    const executable = surface.capabilities.includes("execute") && Boolean(surface.runtime);
    const kindLabel = descriptor?.label || surface.kind || "Surface";
    const title = surface.title || surface.locator || surface.id;
    const unavailableRunner = isUnavailableRunnerError(run?.error);
    const statusLabel = descriptor
      ? (descriptor.status === "unavailable" ? "Unavailable" : "Snapshot-pinned")
      : running
        ? "Running…"
        : unavailableRunner
          ? "Unavailable"
          : run?.error
            ? "Failed"
            : run?.new
              ? run.new.status
              : executable
                ? "Ready"
                : "Source only";
    const statusTone = descriptor?.status === "unavailable" || unavailableRunner
      ? "text-amber-400/80"
      : run?.error || run?.new?.status === "failed"
        ? "text-rose-400/80"
        : running
          ? "text-amber-400/70"
          : run?.new?.status === "passed"
            ? "text-emerald-400/80"
            : "text-muted-foreground";
    const activate = () => {
      setActiveSurfaceKey(surfaceKey);
      activeMarkdownPathRef.current = "";
      if (surfaceAutoRankable(surface) && !surfaceImpactScores[surfaceKey]) {
        void loadSurfaceImpact(surface, 4).catch(() => undefined);
      }
    };
    return (
      <section
        className="border-b border-emerald-950/60 bg-background"
        data-testid={`plugin-surface:${surfaceKey}`}
        data-review-target-id={firstTargetId}
        data-review-surface-key={surfaceKey}
        onMouseEnter={activate}
        onFocusCapture={activate}
      >
        <div className="review-surface-header sticky top-0 z-20 flex min-h-10 items-center gap-2 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur">
          <UiButton unstyled
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "surface", provider: surface.provider, id: surface.id })}
            aria-label={`Open ${kindLabel} surface ${title} in dedicated tab`}
            tooltip="Open surface in dedicated tab"
            className="review-surface-kind inline-flex shrink-0 items-center gap-1 text-caption font-medium uppercase tracking-[0.14em] text-emerald-400/75 hover:text-emerald-300"
          >
            <Puzzle size={12} /> {kindLabel} <span className="normal-case tracking-normal text-emerald-300/90">· {surface.provider}</span> <ExternalLink size={10} />
          </UiButton>
          <div className="review-surface-title min-w-0">
            {item.options.length > 1 ? (
              <SurfacePicker
                ariaLabel={`${kindLabel} surface`}
                value={surfaceKey}
                storageKey={`lemoncrow-review-surface-filter:${currentReviewId()}:${item.id}`}
                placeholder="Search surfaces, paths…"
                options={item.options}
                onChange={(nextSurfaceKey) => {
                  setSelectedPluginSurfaceKeys((current) => ({ ...current, [item.id]: nextSurfaceKey }));
                  setActiveSurfaceKey(nextSurfaceKey);
                  const nextSurface = surfacesByKey.get(nextSurfaceKey);
                  if (nextSurface && surfaceAutoRankable(nextSurface) && !surfaceImpactScores[nextSurfaceKey]) {
                    void loadSurfaceImpact(nextSurface, 4).catch(() => undefined);
                  }
                }}
              />
            ) : (
              <span className="block min-w-0 truncate font-mono text-body text-foreground">{title}</span>
            )}
          </div>
          <span className={`review-surface-status shrink-0 text-caption ${statusTone}`}>{statusLabel}</span>
          <span className="review-surface-spacer flex-1" />
          {surfaceComparable(surface) && (
            <span className="review-surface-mode flex overflow-hidden rounded border border-border">
              {(["preview", "compare"] as const).map((mode) => (
                <UiButton unstyled
                  key={mode}
                  type="button"
                  aria-label={`surface-${mode}`}
                  aria-pressed={item.mode === mode}
                  tooltip={`${mode[0]?.toUpperCase()}${mode.slice(1)}`}
                  onClick={() => setSurfaceModes((current) => ({ ...current, [surfaceKey]: mode }))}
                  className={`px-2 py-0.5 text-caption capitalize ${item.mode === mode ? "bg-neutral-800 text-foreground" : "text-muted-foreground hover:text-neutral-50"}`}
                >
                  {mode}
                </UiButton>
              ))}
            </span>
          )}
        </div>
        {executable && (
          <div className="border-b border-border bg-surface-sunken px-3 py-2">
            <RuntimeValidationControl
              state={runtimeValidations[surfaceKey]}
              busy={runtimeValidationBusy.has(surfaceKey)}
              target={firstTargetId ? targetsById.get(firstTargetId) : undefined}
              onRun={() => onValidateSurface?.(surface)}
              onCancel={onCancelRuntimeValidation ? (jobId) => onCancelRuntimeValidation(surface, jobId) : undefined}
              onInspect={(target) => onContext(target, "checks")}
              onAsk={onAsk}
            />
          </div>
        )}
        <SurfacePluginPreview
          surface={surface}
          revisionId={revisionId}
          mode={item.mode}
          run={run}
          running={running}
          onRun={(compare) => onRunSurface?.(surface, compare)}
          onOpenPath={scrollToFile}
        />
      </section>
    );
  }, [loadSurfaceImpact, onAsk, onCancelRuntimeValidation, onContext, onRunSurface, onValidateSurface, revisionId, runtimeValidationBusy, runtimeValidations, scrollToFile, surfaceBusy, surfaceImpactScores, surfaceRuns, surfacesByKey, targetsById, targetsByPath]);

  const renderLinkedSurface = useCallback((item: LinkedSurfaceStreamItem) => {
    const path = item.id;
    const surface = item.surface;
    const surfaceKey = `${surface.provider}:${surface.id}`;
    if (item.collapsed) return renderCollapsedHeader(path);
    const firstTargetId = targetsByPath.get(path)?.[0]?.target_id;
    const executable = surface.capabilities.includes("execute") && Boolean(surface.runtime);
    const validationTarget = firstTargetId ? targetsById.get(firstTargetId) : undefined;
    const activate = () => {
      setActiveSurfaceKey(surfaceKey);
      activeMarkdownPathRef.current = "";
    };
    const fileHeader = (
      <div
        className="sticky top-0 z-40 flex min-h-10 items-center gap-3 border-b border-border bg-background/95 px-3 py-1.5 backdrop-blur"
        data-review-target-id={firstTargetId}
      >
        <UiButton unstyled
          type="button"
          onClick={() => openDedicatedReviewItem({ kind: "file", path })}
          aria-label={`Open ${path} in dedicated tab`}
          tooltip="Open file in dedicated tab"
          className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 truncate text-left font-mono text-body text-neutral-200 underline-offset-2 hover:text-neutral-50 hover:underline"
        >
          <FileTypeIcon path={path} />
          <span className="truncate" title={path}>{path}</span>
          <ExternalLink size={11} className="shrink-0 text-muted-foreground" />
        </UiButton>
        {headerControls(path)}
      </div>
    );

    if (surfaceView(surface) === "web") {
      const detailPreview = details[path]?.preview;
      const preview = detailPreview?.kind === "web" ? detailPreview : previewFromSurface(surface);
      if (!preview) return null;
      const linkedKeys = new Set(item.options.map((option) => option.value));
      const linkedRoutes = webRouteOptions.filter((option) => linkedKeys.has(option.surfaceKey));
      return (
        <section
          className="border-b border-sky-950/60 bg-background"
          data-testid={`linked-surface:${path}`}
          data-review-surface-key={surfaceKey}
          onMouseEnter={activate}
          onFocusCapture={activate}
        >
          {fileHeader}
          {executable && (
            <div className="border-b border-border bg-surface-sunken px-3 py-2">
              <RuntimeValidationControl
                state={runtimeValidations[surfaceKey]}
                busy={runtimeValidationBusy.has(surfaceKey)}
                target={validationTarget}
                onRun={() => onValidateSurface?.(surface)}
                onCancel={onCancelRuntimeValidation ? (jobId) => onCancelRuntimeValidation(surface, jobId) : undefined}
                onInspect={(target) => onContext(target, "checks")}
                onAsk={onAsk}
              />
            </div>
          )}
          <WebReviewPreview
            preview={{ ...preview, routes: [surface.locator || preview.default_route || "/"], default_route: surface.locator || preview.default_route || "/" }}
            documentPath={path}
            revisionId={details[path]?.revision_id ?? revisionId}
            surfaceKey={surfaceKey}
            surfaceTitle={surface.title || surface.locator || "Web surface"}
            serviceName={configuredSurfaceName(surface)}
            affectedPaths={surface.affected_paths}
            framework={preview.framework}
            chromeTheme={chromeTheme}
            active={activeSurfaceKey === surfaceKey}
            routeOptions={linkedRoutes.length > 0 ? linkedRoutes : webRouteOptions.filter((option) => option.surfaceKey === surfaceKey)}
            requestedMode={item.mode}
            onActivate={activate}
            onOpenPath={scrollToFile}
            onOpenDedicated={() => openDedicatedReviewItem({ kind: "surface", provider: surface.provider, id: surface.id })}
            onNavigateSurface={(nextKey) => {
              setSelectedFileSurfaceKeys((current) => ({ ...current, [path]: nextKey }));
              setActiveSurfaceKey(nextKey);
            }}
          />
        </section>
      );
    }

    return (
      <section
        className="border-b border-emerald-950/60 bg-background"
        data-testid={`linked-surface:${path}`}
        data-review-surface-key={surfaceKey}
        onMouseEnter={activate}
        onFocusCapture={activate}
      >
        {fileHeader}
        {executable && (
          <div className="border-b border-border bg-surface-sunken px-3 py-2">
            <RuntimeValidationControl
              state={runtimeValidations[surfaceKey]}
              busy={runtimeValidationBusy.has(surfaceKey)}
              target={validationTarget}
              onRun={() => onValidateSurface?.(surface)}
              onCancel={onCancelRuntimeValidation ? (jobId) => onCancelRuntimeValidation(surface, jobId) : undefined}
              onInspect={(target) => onContext(target, "checks")}
              onAsk={onAsk}
            />
          </div>
        )}
        <SurfacePluginPreview
          surface={surface}
          revisionId={details[path]?.revision_id ?? revisionId}
          mode={item.mode}
          run={surfaceRuns[surfaceKey]}
          running={surfaceBusy.has(surfaceKey)}
          onRun={(compare) => onRunSurface?.(surface, compare)}
          onOpenPath={scrollToFile}
        />
      </section>
    );
  }, [activeSurfaceKey, chromeTheme, details, headerControls, onAsk, onCancelRuntimeValidation, onContext, onRunSurface, onValidateSurface, previewFromSurface, renderCollapsedHeader, revisionId, runtimeValidationBusy, runtimeValidations, scrollToFile, surfaceBusy, surfaceRuns, targetsById, targetsByPath, webRouteOptions]);

  const renderItem = useCallback((item: StreamItem) => {
    if (item.type === "markdown") return renderMarkdown(item);
    if (item.type === "csv") return renderCsv(item);
    if (item.type === "media") return renderMedia(item);
    if (item.type === "web-surface") return renderWebSurface(item);
    if (item.type === "api-surface") return renderApiSurface(item);
    if (item.type === "service-surface") return renderServiceSurface(item);
    if (item.type === "plugin-surface") return renderPluginSurface(item);
    if (item.type === "linked-surface") return renderLinkedSurface(item);
    if (item.collapsed) return renderCollapsedHeader(item.id);
    const firstTargetId = targetsByPath.get(item.id)?.[0]?.target_id;
    if (item.type === "special") {
      return (
        <div data-review-target-id={firstTargetId} data-testid={`code-item:${item.id}`} data-collapsed="false">
          <SpecialGitChange path={item.id} change={item.change} patch={item.patch} headerControls={headerControls(item.id)} />
        </div>
      );
    }
    if (item.type === "diff") {
      return (
        <div
          data-review-target-id={firstTargetId}
          data-testid={`code-item:${item.id}`}
          data-collapsed="false"
          onClick={(event) => {
            if (comparisonMode || event.defaultPrevented) return;
            const clickedIdentity = event.nativeEvent.composedPath().some(
              (node) => node instanceof HTMLElement && node.hasAttribute("data-header-content"),
            );
            if (clickedIdentity) openDedicatedReviewItem({ kind: "file", path: item.id });
          }}
        >
          <FileDiff
            key={`${item.id}:${item.patchVersion}`}
            fileDiff={item.fileDiff}
            options={diffOptionsFor(item.id)}
            lineAnnotations={item.annotations}
            selectedLines={comparisonMode ? undefined : (lineSelections[item.id] ?? draftSelection(draft, item.id))}
            renderHeaderPrefix={() => <FileTypeIcon path={item.id} />}
            renderHeaderFilenameSuffix={() => comparisonMode ? null : (
              <UiButton unstyled
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  openDedicatedReviewItem({ kind: "file", path: item.id });
                }}
                aria-label={`Open ${item.id} in dedicated tab`}
                tooltip="Open file in dedicated tab"
                className="ml-1 inline-flex items-center text-muted-foreground hover:text-neutral-50"
              >
                <ExternalLink size={10} />
              </UiButton>
            )}
            renderHeaderMetadata={() => headerControls(item.id)}
            renderAnnotation={(annotation) => renderInlineAnnotation(item.id, annotation)}
            style={diffSurfaceStyle}
          />
        </div>
      );
    }
    return (
      <div
        data-review-target-id={firstTargetId}
        data-testid={`code-item:${item.id}`}
        data-collapsed="false"
        onClick={(event) => {
          if (event.defaultPrevented) return;
          const clickedIdentity = event.nativeEvent.composedPath().some(
            (node) => node instanceof HTMLElement && node.hasAttribute("data-header-content"),
          );
          if (clickedIdentity) openDedicatedReviewItem({ kind: "file", path: item.id });
        }}
      >
        <File
          file={item.file}
          options={{
            theme: themeDefinition.pierreTheme,
            themeType: chromeTheme,
            onPostRender: (node: HTMLElement, _instance: unknown, phase: "mount" | "update" | "unmount") => {
              decorateDedicatedHeader(node, phase);
              decorateCssColorSwatches(node, item.id, phase);
            },
          }}
          renderHeaderPrefix={() => <FileTypeIcon path={item.id} />}
          renderHeaderFilenameSuffix={() => (
            <UiButton unstyled
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                openDedicatedReviewItem({ kind: "file", path: item.id });
              }}
              aria-label={`Open ${item.id} in dedicated tab`}
              tooltip="Open file in dedicated tab"
              className="ml-1 inline-flex items-center text-muted-foreground hover:text-neutral-50"
            >
              <ExternalLink size={10} />
            </UiButton>
          )}
          renderHeaderMetadata={() => headerControls(item.id)}
          style={diffSurfaceStyle}
        />
        {item.failed && onRetryPath && <FileLoadRetry path={item.id} onRetry={onRetryPath} />}
      </div>
    );
  }, [
    onRetryPath,
    diffOptionsFor,
    headerControls,
    renderCollapsedHeader,
    renderInlineAnnotation,
    renderMarkdown,
    renderCsv,
    renderMedia,
    renderWebSurface,
    renderApiSurface,
    renderServiceSurface,
    renderPluginSurface,
    renderLinkedSurface,
    targetsByPath,
    chromeTheme,
    themeDefinition,
    decorateDedicatedHeader,
    draft,
    lineSelections,
  ]);

  // Package semantics and generated-file provenance lead the file; its raw
  // lines follow (plan 8F). Comparisons show raw Snapshot deltas only.
  const renderFileSemantics = (item: StreamItem): ReactNode => {
    if (comparisonMode || (item.type !== "diff" && item.type !== "file")) return null;
    const path = item.id;
    const dependencies = fileSemantics?.dependencies;
    const role = fileSemantics?.roles?.get(path);
    const dependencyCard = dependencies && dependencyFileRelevant(dependencies, path)
      ? <DependencyFileSemantics review={dependencies} path={path} />
      : null;
    const generatedCard = role?.role === "generated"
      ? (
        <GeneratedFileSemantics
          path={path}
          role={role}
          generatedFrom={generatedSource(dependencies, path)}
          rawShown={!item.collapsed}
          onToggleRaw={() => toggleCollapsed(path)}
        />
      )
      : null;
    if (!dependencyCard && !generatedCard) return null;
    return <>{generatedCard}{dependencyCard}</>;
  };

  const renderStickyTargetHeader = (targetId: string) => {
    const target = targetsById.get(targetId);
    if (!target || (snapshotComparison && target.kind === "hunk" && !target.symbol)) return null;
    return (
      <ReviewTargetHeader
        target={target}
        active={target.target_id === activeTargetId}
        busy={busy}
        scopeTop={targetHasContinuousScope(target, targets)}
        comparisonMode={comparisonMode}
        stickyPath={target.path}
        folded={foldedTargetIds.has(target.target_id)}
        onToggleFold={() => setTargetFolded(target.target_id, !foldedTargetIds.has(target.target_id))}
        onFocus={() => onActiveTarget(target.target_id)}
        onMark={(state, advance) => onMark(target, state, advance)}
        onComment={() => onComment(target)}
        onAsk={onAsk ? () => onAsk(target) : undefined}
        onContext={(tab) => onContext(target, tab)}
      />
    );
  };

  useEffect(() => {
    const root = scrollRef.current;
    const footer = loadMoreRef.current;
    if (!autoLoadMore || !root || !footer || dedicatedItem || loadingPaused || pendingVisibleFileCount > 0
      || readyHiddenFileCount === 0 || remainingFileCount === 0
      || typeof IntersectionObserver === "undefined") return;
    let requested = false;
    let retryTimer: number | undefined;
    const reveal = () => {
      if (requested) return;
      if (navigationHoldRef.current || Date.now() < programmaticScrollUntilRef.current) {
        // An intersection suppressed by navigation may never fire again.
        // Recheck the footer's current position once the hold ends.
        retryTimer ??= window.setTimeout(() => {
          retryTimer = undefined;
          checkLoadMoreRef.current?.();
        }, 100);
        return;
      }
      requested = true;
      onLoadMore(true);
    };
    checkLoadMoreRef.current = () => {
      const bounds = root.getBoundingClientRect();
      const next = footer.getBoundingClientRect();
      if (bounds.height > 0 && next.top <= bounds.bottom + 200 && next.bottom >= bounds.top) reveal();
    };
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.target === footer && entry.isIntersecting)) reveal();
    }, { root, rootMargin: "0px 0px 200px 0px" });
    observer.observe(footer);
    // Re-observe after each batch or preparation update so the browser checks
    // the footer's new position before revealing another batch.
    return () => {
      requested = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      checkLoadMoreRef.current = null;
      observer.disconnect();
    };
  }, [autoLoadMore, loadingPaused, dedicatedItem, items.length, onLoadMore, pendingVisibleFileCount, readyHiddenFileCount, remainingFileCount]);

  useLayoutEffect(() => {
    const root = scrollRef.current;
    if (!root || resetScrollPageRef.current === pagination?.page) return;
    resetScrollPageRef.current = pagination?.page;
    releaseNavigation();
    root.scrollTop = 0;
  }, [pagination?.page, items.length, releaseNavigation]);

  const pageNavigation = pagination && (
    <nav aria-label="Review pages" className="flex min-w-0 items-center justify-center gap-2 border-t border-border px-3 py-2 text-caption">
      {pagination.page > 1 && (
        <UiButton size="compact" className="shrink-0 text-caption" disabled={pagination.blocked} tooltip={pagination.blocked ? "Save or cancel your comment first" : ""}
          onClick={() => pagination.onChange(pagination.page - 1)}>Previous page</UiButton>
      )}
      <span className="min-w-0 truncate text-muted-foreground tabular-nums">
        Page {pagination.page} of {pagination.pageCount} · {pagination.changedLines.toLocaleString()} changed lines
      </span>
      {pagination.page < pagination.pageCount && remainingFileCount === 0 && pendingVisibleFileCount === 0 && (
        <UiButton size="compact" className="shrink-0 text-caption" disabled={pagination.blocked} tooltip={pagination.blocked ? "Save or cancel your comment first" : ""}
          onClick={() => pagination.onChange(pagination.page + 1)}>Next page</UiButton>
      )}
    </nav>
  );

  const previewShortcut = !dedicatedItem && !showReviewPreviews && surfaces.length > 0 && onOpenPreviews && (
    <div className="flex justify-end px-3 py-1">
      <UiButton
        size="compact"
        className="text-caption"
        disabled={pagination?.blocked}
        tooltip={pagination?.blocked ? "Save or cancel your comment first" : "Open review previews on page 1"}
        onClick={onOpenPreviews}
      >Open previews</UiButton>
    </div>
  );

  if (items.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        {previewShortcut}
        <div className="flex flex-1 items-center justify-center text-label text-muted-foreground" role="status">
          {targets.length === 0 ? "No matching targets on this page." : "Preparing review content…"}
        </div>
        {pageNavigation}
      </div>
    );
  }

  return (
    <div
      ref={viewportRef}
      className="relative min-h-0 flex-1 bg-background"
      data-review-diff-preference={diffStyle}
      data-review-diff-style={resolvedDiffStyle}
      onPointerDownCapture={(event) => {
        const primary = event.pointerType !== "mouse" || event.button === 0;
        codeTextSelectionStartRef.current = !comparisonMode && primary && !event.shiftKey
          ? codeTextPoint(event.nativeEvent.composedPath(), false)
          : null;
      }}
      onPointerUpCapture={(event) => {
        lastPointerRef.current = { clientX: event.clientX, clientY: event.clientY };
        // Dragging across code text (not the gutter) is a native selection
        // Pierre never reports; give multi-line drags the same range actions.
        const start = codeTextSelectionStartRef.current;
        codeTextSelectionStartRef.current = null;
        if (!start) return;
        const end = codeTextPoint(event.nativeEvent.composedPath(), true);
        if (
          !end
          || end.itemPath !== start.itemPath
          || end.host !== start.host
          || end.lineNumber === start.lineNumber
          || !diffNodesRef.current.has(start.itemPath)
          || !hasCodeTextSelection(start.host)
        ) return;
        diffOptionsFor(start.itemPath).onLineSelectionEnd({
          start: start.lineNumber,
          end: end.lineNumber,
          side: start.side,
          endSide: end.side,
        });
      }}
    >
      <div
        ref={scrollRef}
        className="absolute inset-0 overflow-y-auto bg-background"
        data-testid="review-stream"
        // The reviewer's own scroll input interrupts a programmatic navigation
        // (the browser cancels its smooth scroll) and owns the active target
        // from here on. Without this, a wheel scroll that starts while the pin
        // is still held keeps extending it, and when it expires no further
        // scroll event arrives to re-sync: the active target stays stale.
        onWheel={() => { releaseNavigation(); checkLoadMoreRef.current?.(); }}
        onTouchMove={() => { releaseNavigation(); checkLoadMoreRef.current?.(); }}
        // Dragging the scrollbar, clicking into the stream or scrolling it
        // with the keyboard is the reviewer taking over as well.
        onPointerDown={releaseNavigation}
        onKeyDown={(event) => {
          if (STREAM_SCROLL_KEYS.has(event.key)) { releaseNavigation(); checkLoadMoreRef.current?.(); }
        }}
        onScroll={() => {
          // Keep range actions open while the reviewer scrolls. A drag selection
          // can itself produce small scroll events (especially near viewport
          // edges); dismissing here made multi-line selection look broken.
          scheduleFocusFromScroll();
          checkLoadMoreRef.current?.();
        }}
      >
        {previewShortcut}
        <div className="w-full">
          {items.map((item) => (
            <div
              key={item.id}
              ref={(node) => {
                if (node) itemElementsRef.current.set(item.id, node);
                else itemElementsRef.current.delete(item.id);
              }}
              data-review-stream-item={item.id}
              className="w-full"
            >
              {renderFileSemantics(item)}
              {renderItem(item)}
              {item.type === "diff" && !item.collapsed && (
                <StickyTargetHeaders
                  targets={targetsByPath.get(item.id) ?? NO_TARGETS}
                  scrollRef={scrollRef}
                  layoutVersion={diffNodeVersion}
                  renderHeader={renderStickyTargetHeader}
                />
              )}
            </div>
          ))}
        </div>
        {/* Reveal prepared files as this footer approaches the viewport. The
            button also supports keyboard users and browsers without observers. */}
        {!dedicatedItem && (pendingVisibleFileCount > 0 || remainingFileCount > 0) ? (
          <div ref={loadMoreRef} data-testid="review-load-more" className="flex items-center justify-center gap-3 border-t border-neutral-900 py-5">
            {pendingVisibleFileCount > 0 ? (
              <span role="status" aria-live="polite" className="text-caption text-muted-foreground">
                Preparing {pendingVisibleFileCount} selected file{pendingVisibleFileCount === 1 ? "" : "s"}…
              </span>
            ) : readyHiddenFileCount > 0 ? (
              <UiButton
                type="button"
                onClick={() => onLoadMore(false)}
                className="px-3 py-1.5 text-caption"
              >
                Show {readyHiddenFileCount} ready file{readyHiddenFileCount === 1 ? "" : "s"} · {remainingFileCount} remaining
              </UiButton>
            ) : remainingFileCount > 0 ? (
              <span role="status" aria-live="polite" className="text-caption text-muted-foreground">
                {preparingHiddenFileCount > 0
                  ? `Preparing next ${preparingHiddenFileCount} file${preparingHiddenFileCount === 1 ? "" : "s"}…`
                  : "Preparing next review chunk…"}
              </span>
            ) : null}
          </div>
        ) : pagination ? null : <div className="h-8" />}
        {pageNavigation}
      </div>
      {selectionAction && (() => {
        const viewportPadding = 8;
        const gap = 8;
        const menuWidth = selectionMenuSize.width || 360;
        const menuHeight = selectionMenuSize.height || 28;
        const rightCandidate = selectionAction.clientX + gap;
        const belowCandidate = selectionAction.clientY + gap;
        const left = rightCandidate + menuWidth <= window.innerWidth - viewportPadding
          ? rightCandidate
          : Math.max(viewportPadding, selectionAction.clientX - menuWidth - gap);
        const top = belowCandidate + menuHeight <= window.innerHeight - viewportPadding
          ? belowCandidate
          : Math.max(viewportPadding, selectionAction.clientY - menuHeight - gap);
        const additions = selectionAction.draft.side === "additions";
        const selectedTarget = selectionAction.draft.targetUnitKey
          ? targets.find((target) => target.unit_key === selectionAction.draft.targetUnitKey)
          : targetAtLine(
              targets,
              selectionAction.path,
              selectionAction.draft.side,
              selectionAction.draft.startLine,
              activeTargetId,
            );
        const finishTargetAction = (state: MarkState) => {
          if (!selectedTarget) return;
          const selected = selectionAction;
          setSelectionAction(null);
          setLineSelection(selected.path, null);
          onMark(selectedTarget, state, state === "reviewed");
        };
        const openDraft = () => {
          const selected = selectionAction;
          setSelectionAction(null);
          openDraftPreservingViewport(selected.draft);
        };
        return (
          <div
            ref={selectionMenuRef}
            role="menu"
            aria-label={`Actions for selected lines ${selectionAction.draft.startLine}–${selectionAction.draft.endLine}`}
            data-testid="review-selection-actions"
            className="fixed z-20 flex min-h-7 w-max max-w-[calc(100vw-16px)] flex-wrap items-center gap-0.5 rounded-md border border-border bg-popover p-px text-popover-foreground shadow-md transition-[opacity,transform] duration-100 motion-reduce:transition-none"
            style={{
              left,
              top,
              opacity: 1,
              transform: "scale(1)",
              transformOrigin: top < selectionAction.clientY ? "bottom right" : "top left",
            }}
            onPointerDown={(event) => event.stopPropagation()}
          >
            <UiButton
              variant="ghost"
              type="button"
              role="menuitem"
              aria-keyshortcuts="c"
              onClick={openDraft}
              className="h-6 gap-1 px-1.5 text-label font-medium text-foreground hover:bg-accent"
            >
              <MessageSquareText size={13} aria-hidden="true" />
              Comment
              <kbd aria-hidden="true" className="ml-1 font-mono text-micro text-muted-foreground">C</kbd>
            </UiButton>
            <UiButton
              variant="ghost"
              type="button"
              role="menuitem"
              aria-keyshortcuts="a"
              onClick={() => {
                const selected = selectionAction;
                setSelectionAction(null);
                setLineSelection(selected.path, null);
                onAskSelection?.(selected.draft);
              }}
              className="h-6 gap-1 px-1.5 text-label text-violet-300 hover:bg-violet-950/45"
            >
              <Sparkles size={13} aria-hidden="true" />
              Ask AI
              <kbd aria-hidden="true" className="ml-1 font-mono text-micro text-violet-500/70">A</kbd>
            </UiButton>
            {selectedTarget && (
              <>
                <span aria-hidden="true" className="mx-0.5 h-4 w-px bg-neutral-700" />
                <UiButton
                  variant="ghost"
                  size="icon"
                  type="button"
                  role="menuitem"
                  aria-keyshortcuts="r"
                  aria-label={`Mark ${selectedTarget.symbol || selectedTarget.label} reviewed`}
                  tooltip="Mark review area reviewed · R"
                  disabled={busy || selectedTarget.state === "reviewed"}
                  onClick={() => finishTargetAction("reviewed")}
                  className="h-6 w-6 text-emerald-300 hover:bg-emerald-950/45 disabled:opacity-40"
                >
                  <CheckCircle2 size={13} aria-hidden="true" />
                </UiButton>
                <UiButton
                  variant="ghost"
                  size="icon"
                  type="button"
                  role="menuitem"
                  aria-keyshortcuts="x"
                  aria-label={`Mark ${selectedTarget.symbol || selectedTarget.label} needs changes`}
                  tooltip="Mark review area needs changes · X"
                  disabled={busy}
                  onClick={() => finishTargetAction("needs_changes")}
                  className="h-6 w-6 text-rose-300 hover:bg-rose-950/45 disabled:opacity-40"
                >
                  <X size={13} aria-hidden="true" />
                </UiButton>
              </>
            )}
            {additions && (
              <UiButton
                variant="ghost"
                type="button"
                role="menuitem"
                aria-keyshortcuts="s"
                onClick={() => {
                  const selected = selectionAction;
                  setSelectionAction(null);
                  onSuggestEdit(selected.draft);
                }}
                className="h-6 gap-1 px-1.5 text-label text-foreground hover:bg-accent"
              >
                <Pencil size={13} aria-hidden="true" />
                Suggest edit
                <kbd aria-hidden="true" className="ml-1 font-mono text-micro text-muted-foreground">S</kbd>
              </UiButton>
            )}
            {additions && sourceMutationSupported && (
              <UiButton
                variant="ghost"
                type="button"
                role="menuitem"
                aria-keyshortcuts="e"
                onClick={() => {
                  const selected = selectionAction;
                  setSelectionAction(null);
                  onEditSource(selected.draft);
                }}
                className="h-6 gap-1 px-1.5 text-label text-sky-300 hover:bg-sky-950/45"
              >
                <FilePenLine size={13} aria-hidden="true" />
                Edit source
                <kbd aria-hidden="true" className="ml-1 font-mono text-micro text-sky-500/70">E</kbd>
              </UiButton>
            )}
          </div>
        );
      })()}
      {horizontalScrollRange > 1 && (
        <div
          ref={horizontalDockRef}
          data-testid="review-horizontal-scrollbar"
          aria-label={`Horizontal scroll for ${activeDiffPath}`}
          onScroll={onHorizontalDockScroll}
          className="review-horizontal-scrollbar absolute inset-x-0 bottom-0 z-40 overflow-x-auto overflow-y-hidden border-t border-border bg-background/95"
        >
          <div aria-hidden="true" style={{ width: `calc(100% + ${horizontalScrollRange}px)`, height: 1 }} />
        </div>
      )}
    </div>
  );
});

export default ReviewStream;
