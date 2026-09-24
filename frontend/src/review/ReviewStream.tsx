import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { parsePatchFiles } from "@pierre/diffs";
import { Boxes, CheckCircle2, ChevronDown, ChevronUp, Download, EllipsisVertical, ExternalLink, Moon, Sun, SunMoon } from "lucide-react";
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
import MarkdownReviewPreview, { type MarkdownPreviewTheme, type MarkdownRenderMode } from "./MarkdownReviewPreview";
import MediaReviewPreview, { type MediaRenderMode } from "./MediaReviewPreview";
import ServiceReviewPreview from "./ServiceReviewPreview";
import SurfacePicker from "./SurfacePicker";
import WebReviewPreview, { type WebRouteOption } from "./WebReviewPreview";
import { motionSafeScrollBehavior, resolveDiffStyle, type DiffOverflow, type DiffStyle, type DiffStylePreference } from "./diffModel";
import { reviewCodeThemeDefinition, type ReviewCodeTheme } from "./reviewCodeTheme";
import { openDedicatedReviewItem, type DedicatedReviewItem } from "./reviewItemNavigation";
import { currentReviewId } from "./reviewApi";
import ReviewTargetHeader from "./ReviewTargetHeader";
import type { Draft } from "./annotationModel";
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
import { groupReaderFiles, orderedPaths, reviewScopeTone, targetScrollLocation } from "./readerModel";
import type {
  Annotation,
  AnnotationDraft,
  AnnotationKind,
  FileDetail,
  MarkState,
  ReviewChangeProposal,
  ReviewProposalSelection,
  ReviewSurfaceInfo,
  ReviewSurfaceRunResult,
  ReviewTarget,
  WebPreview,
} from "./types";

interface StreamMarkerMeta {
  key: string;
  targetIds: string[];
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

interface DiffStreamItem extends StreamItemBase {
  type: "diff";
  fileDiff: ParsedFileDiff;
  annotations: StreamMarkers;
}

interface FileStreamItem extends StreamItemBase {
  type: "file";
  file: CodeViewFileItem<StreamMarkerMeta>["file"];
}

type StreamItem = MarkdownStreamItem | MediaStreamItem | WebSurfaceStreamItem | ApiSurfaceStreamItem | ServiceSurfaceStreamItem | DiffStreamItem | FileStreamItem;

interface ParsedDiffCacheEntry {
  patch: string;
  fileDiff: ParsedFileDiff | null;
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

interface ItemRevisionEntry {
  version: number;
  type: "diff" | "file" | "markdown" | "media";
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
  dedicatedItem?: DedicatedReviewItem | null;
  surfaceRuns?: Record<string, { old?: ReviewSurfaceRunResult; new?: ReviewSurfaceRunResult; error?: string }>;
  surfaceBusy?: string;
  errors: Record<string, string | undefined>;
  annotations: Annotation[];
  draft: Draft | null;
  proposalSelection?: ReviewProposalSelection | null;
  activeProposal?: ReviewChangeProposal | null;
  proposalBusy?: boolean;
  sourceMutationSupported?: boolean;
  comparisonMode?: boolean;
  diffStyle: DiffStylePreference;
  diffOverflow?: DiffOverflow;
  codeTheme?: ReviewCodeTheme;
  chromeTheme?: Theme;
  activeTargetId: string;
  busy: boolean;
  remainingFileCount: number;
  pendingVisibleFileCount?: number;
  readyHiddenFileCount?: number;
  preparingHiddenFileCount?: number;
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
  onCreateProposal?(): void;
  onCreateAndApplyProposal?(): void;
  onApplyProposal?(): void;
  onCloseProposal?(): void;
  onCreate(draft: AnnotationDraft): void;
  onSetAnnotationState(id: string, state: "open" | "resolved"): void;
  onMark(target: ReviewTarget, state: MarkState, advance?: boolean): void;
  onComment(target: ReviewTarget): void;
  onContext(target: ReviewTarget, tab?: ContextDrawerTab): void;
  onBulkReview(path: string): void;
  onLoadMore(): void;
  onRunSurface?(surface: ReviewSurfaceInfo, compare: boolean): void;
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

function buildMarkers(
  path: string,
  targets: readonly ReviewTarget[],
  annotations: Annotation[],
  draft: Draft | null,
): { side: "additions" | "deletions"; lineNumber: number; metadata: StreamMarkerMeta }[] {
  const markers = new Map<string, { side: "additions" | "deletions"; lineNumber: number; metadata: StreamMarkerMeta }>();

  const ensure = (side: "additions" | "deletions", lineNumber: number) => {
    const key = markerKey(side, lineNumber);
    let row = markers.get(key);
    if (!row) {
      row = { side, lineNumber, metadata: { key, targetIds: [] } };
      markers.set(key, row);
    }
    return row;
  };

  for (const target of targets) {
    const location = targetHeaderLocation(target);
    ensure(location.side, location.lineNumber).metadata.targetIds.push(target.target_id);
  }

  for (const marker of markerLines(annotations, path, draft)) {
    ensure(marker.side, marker.lineNumber);
  }

  const fileLevel = fileComments(annotations, path);
  const fileDraft = draft?.path === path && draft.fileLevel;
  if (fileLevel.length > 0 || fileDraft) ensure("additions", 0);

  return [...markers.values()].sort((a, b) => a.lineNumber - b.lineNumber || a.side.localeCompare(b.side));
}

function refusalText(detail: FileDetail | undefined, error: string | undefined, path: string): string {
  if (error) return `Could not load this file.\n\n${error}\n\nOther files in the review remain available.`;
  if (!detail) return `LemonCrow has not loaded ${path} yet.`;
  const title = detail.refusal ? detail.refusal.replaceAll("_", " ") : "diff unavailable";
  return `Diff unavailable: ${title}\n\n${detail.detail || "This recorded revision does not contain enough text to render a reliable diff."}`;
}

function targetHasContinuousScope(
  target: ReviewTarget,
  siblings: readonly ReviewTarget[] = [],
): boolean {
  if (target.kind === "file" || target.spans.length === 0) return false;
  const containsChild = target.start_line > 0 && target.end_line >= target.start_line
    && siblings.some((candidate) => (
      candidate.target_id !== target.target_id
      && candidate.path === target.path
      && candidate.start_line > 0
      && candidate.end_line >= candidate.start_line
      && candidate.start_line >= target.start_line
      && candidate.end_line <= target.end_line
      && (
        candidate.start_line > target.start_line
        || candidate.end_line < target.end_line
      )
    ));
  if (containsChild) return false;

  const counts = new Map<"old" | "new", number>();
  for (const span of target.spans) {
    counts.set(span.side, (counts.get(span.side) ?? 0) + 1);
  }
  return [...counts.values()].every((count) => count === 1);
}

function targetMarkerSignature(targets: readonly ReviewTarget[]): string {
  return targets.map((target) => {
    const location = targetHeaderLocation(target);
    const spans = target.spans
      .map((span) => `${span.side}:${span.start_line}-${span.end_line}:${span.hunk_ordinal}`)
      .join(",");
    return `${target.target_id}@${location.side}:${location.lineNumber}[${spans}]`;
  }).join("|");
}

function nextItemRevision(
  cache: Map<string, ItemRevisionEntry>,
  path: string,
  type: "diff" | "file" | "markdown" | "media",
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
  try {
    const fileDiff = parsePatchFiles(patch, `lc:${path}`, false)[0]?.files[0] ?? null;
    cache.set(path, { patch, fileDiff });
    return fileDiff;
  } catch {
    cache.set(path, { patch, fileDiff: null });
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
  if (detail?.preview?.kind === "markdown" && markdownMode !== "source") {
    const signature = `${markerSignature(annotations, path, draft)}#${targetMarkerSignature(targets)}#markdown-${markdownMode}`;
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
  if (detail?.preview?.kind === "media") {
    const signature = `${markerSignature(annotations, path, draft)}#${targetMarkerSignature(targets)}#media-${markdownMode}`;
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
      const markers = comparisonMode ? [] : buildMarkers(path, targets, annotations, draft);
      const signature = `${markerSignature(annotations, path, draft)}#${targetMarkerSignature(targets)}`;
      const item: DiffStreamItem = {
        id: path,
        type: "diff",
        fileDiff,
        version: nextItemRevision(revisionCache, path, "diff", detail.patch, signature, collapsed),
        annotations: markers,
        collapsed,
        estimatedHeight: collapsed
          ? 44
          : Math.max(160, Math.min(18_000, 76 + detail.patch.split("\n").length * 21 + targets.length * 48)),
      };
      return item;
    }
  }
  const contents = refusalText(detail, error, path);
  const item: FileStreamItem = {
    id: path,
    type: "file",
    file: { name: path, contents },
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

function renderedDiffLine(
  container: HTMLElement,
  side: "additions" | "deletions",
  lineNumber: number,
): HTMLElement | null {
  const hosts = container.matches("diffs-container")
    ? [container]
    : [...container.querySelectorAll<HTMLElement>("diffs-container")];
  for (const host of hosts) {
    const root: ParentNode = host.shadowRoot ?? host;
    const rows = [...root.querySelectorAll<HTMLElement>(`[data-line="${lineNumber}"]`)];
    if (rows.length === 0) continue;
    const preferred = rows.find((row) => {
      const lineType = row.getAttribute("data-line-type") ?? "";
      if (side === "additions") return !lineType.includes("deletion");
      return !lineType.includes("addition");
    });
    return preferred ?? rows[0] ?? null;
  }
  return null;
}

function decorateReviewScopeBoxes(
  container: HTMLElement,
  path: string,
  targets: readonly ReviewTarget[],
  phase: "mount" | "update" | "unmount",
  chromeTheme: Theme,
  diffStyle: DiffStyle,
): void {
  const wrapper = container.closest<HTMLElement>(`[data-testid="code-item:${CSS.escape(path)}"]`);
  if (!wrapper) return;

  for (const overlay of wrapper.querySelectorAll<HTMLElement>("[data-lc-review-scope-overlay]")) {
    overlay.remove();
  }

  const targetIds = new Set(targets.filter((target) => target.path === path).map((target) => target.target_id));
  const headers = [...wrapper.querySelectorAll<HTMLElement>(
    '[role="group"][data-review-target-id]',
  )].filter((header) => targetIds.has(header.dataset.reviewTargetId ?? ""));
  for (const header of headers) {
    header.style.position = "";
    header.style.left = "";
    header.style.width = "";
    header.style.maxWidth = "";
    header.style.boxSizing = "";
    header.style.zIndex = "";
  }
  if (phase === "unmount" || headers.length === 0) return;

  const wrapperRect = wrapper.getBoundingClientRect();
  if (wrapperRect.width <= 0 || wrapperRect.height <= 0) return;
  wrapper.style.position = "relative";

  const targetById = new Map(
    targets.filter((target) => target.path === path).map((target) => [target.target_id, target] as const),
  );

  const naturalHeaderRectByTarget = new Map<string, DOMRect>();
  const scopeBoundsByTarget = new Map<string, { left: number; right: number; gutter: number }>();

  const scopeHorizontalBounds = (
    target: ReviewTarget,
    naturalRect?: DOMRect,
  ): { left: number; right: number; gutter: number } => {
    const rect = naturalRect ?? naturalHeaderRectByTarget.get(target.target_id);
    const naturalLeft = rect
      ? Math.max(0, Math.min(wrapperRect.width, rect.left - wrapperRect.left))
      : 0;
    if (diffStyle !== "split") {
      // Unified diffs are one visual surface. Scope/annotation chrome should
      // include the line-number gutter instead of inheriting the code column's
      // left inset, especially for completely added/deleted files.
      return { left: 0, right: wrapperRect.width, gutter: 0 };
    }

    const midpoint = wrapperRect.width / 2;
    const tone = reviewScopeTone(target);
    if (tone === "modified") {
      const gutter = naturalLeft >= midpoint ? naturalLeft - midpoint : naturalLeft;
      return { left: gutter, right: wrapperRect.width, gutter };
    }
    return tone === "deleted"
      ? { left: naturalLeft, right: midpoint, gutter: naturalLeft }
      : { left: naturalLeft, right: wrapperRect.width, gutter: Math.max(0, naturalLeft - midpoint) };
  };

  for (const header of headers) {
    const target = targetById.get(header.dataset.reviewTargetId ?? "");
    if (!target) continue;
    const rect = header.getBoundingClientRect();
    naturalHeaderRectByTarget.set(target.target_id, rect);
    const bounds = scopeHorizontalBounds(target, rect);
    scopeBoundsByTarget.set(target.target_id, bounds);
    // An annotation is clipped to its own split column by the diff viewer.
    // Moving its header across the midpoint hides its label in that clip.
    // Keep the controls in their column; the scope outline can span both sides.
    const headerLeft = diffStyle === "split" && reviewScopeTone(target) === "modified"
      ? Math.max(bounds.left, rect.left - wrapperRect.left)
      : bounds.left;
    const desiredLeft = wrapperRect.left + headerLeft;
    header.style.position = "relative";
    header.style.left = `${desiredLeft - rect.left}px`;
    header.style.width = `${bounds.right - headerLeft}px`;
    header.style.maxWidth = "none";
    header.style.boxSizing = "border-box";
    header.style.zIndex = "4";
  }

  const ordered = headers
    .map((header) => ({ header, rect: header.getBoundingClientRect() }))
    .filter(({ rect }) => rect.width > 0 && rect.height > 0)
    .sort((left, right) => left.rect.top - right.rect.top);

  const lineHeightValue = getComputedStyle(container).getPropertyValue("--diffs-line-height")
    || getComputedStyle(container).lineHeight;
  const lineHeight = Number.parseFloat(lineHeightValue) || 21;

  ordered.forEach(({ header, rect }, index) => {
    const target = targetById.get(header.dataset.reviewTargetId ?? "");
    if (!target) return;

    // Only draw a body when this target owns one contiguous region per side.
    // Parent symbols and fallback targets with carved-out child regions stay header-only.
    if (!targetHasContinuousScope(target, targets)) return;

    const tone = reviewScopeTone(target);
    const edge = tone === "added"
      ? (chromeTheme === "dark" ? "rgba(74, 222, 128, 0.28)" : "rgba(22, 163, 74, 0.28)")
      : tone === "deleted"
        ? (chromeTheme === "dark" ? "rgba(251, 113, 133, 0.30)" : "rgba(225, 29, 72, 0.28)")
        : (chromeTheme === "dark" ? "rgba(56, 189, 248, 0.22)" : "rgba(2, 132, 199, 0.22)");
    const nextTop = ordered[index + 1]?.rect.top ?? wrapperRect.bottom;
    const targetBounds = scopeBoundsByTarget.get(target.target_id) ?? scopeHorizontalBounds(target);

    const scopeMetrics = (side?: "old" | "new") => {
      const spans = side ? target.spans.filter((span) => span.side === side) : target.spans;
      if (spans.length === 0) return { lineCount: 0, endLine: 0 };
      const spanStart = Math.min(...spans.map((span) => span.start_line));
      const spanEnd = Math.max(...spans.map((span) => span.end_line));
      const anchorLine = targetScrollLocation(target)?.lineNumber ?? spanStart;
      const blockLevel = target.kind === "symbol" || target.kind === "hunk";
      const targetEndApplies = blockLevel
        && target.end_line >= anchorLine
        && (
          side === undefined
          || tone !== "modified"
          || side === "new"
        );
      const endLine = targetEndApplies ? Math.max(spanEnd, target.end_line) : spanEnd;
      return {
        lineCount: Math.max(1, endLine - anchorLine + 1),
        endLine,
      };
    };

    const appendOverlay = ({
      side,
      left,
      right,
      lineCount,
      endLine,
      leftEdge,
      rightEdge,
    }: {
      side: "old" | "new" | "all";
      left: number;
      right: number;
      lineCount: number;
      endLine: number;
      leftEdge: boolean;
      rightEdge: boolean;
    }) => {
      if (lineCount <= 0) return;
      const top = Math.max(0, rect.bottom - wrapperRect.top);
      const endSides: Array<"additions" | "deletions"> = side === "all"
        ? ["additions", "deletions"]
        : [side === "old" ? "deletions" : "additions"];
      const expandedEndRects = endSides
        .map((endSide) => renderedDiffLine(container, endSide, endLine)?.getBoundingClientRect())
        .filter((endRect): endRect is DOMRect => Boolean(endRect && endRect.height > 0 && endRect.bottom > rect.bottom));
      const spanEndRects = target.spans
        .filter((span) => side === "all" || span.side === side)
        .map((span) => renderedDiffLine(container, span.side === "old" ? "deletions" : "additions", span.end_line)?.getBoundingClientRect())
        .filter((endRect): endRect is DOMRect => Boolean(endRect && endRect.height > 0 && endRect.bottom > rect.bottom));
      const endRects = [...expandedEndRects, ...spanEndRects];
      // Inline discussions add height between source lines. Use rendered end
      // bounds so a scope never draws its bottom border through a comment.
      const measuredBottom = endRects.length > 0
        ? Math.max(...endRects.map((endRect) => endRect.bottom))
        : rect.bottom + lineCount * lineHeight;
      const absoluteBottom = nextTop > rect.bottom
        ? Math.min(measuredBottom, nextTop)
        : measuredBottom;
      const bottom = Math.max(top + 1, absoluteBottom - wrapperRect.top);
      const overlay = document.createElement("div");
      overlay.dataset.lcReviewScopeOverlay = header.dataset.reviewTargetId ?? "";
      overlay.dataset.lcReviewScopeOverlaySide = side;
      overlay.setAttribute("aria-hidden", "true");
      Object.assign(overlay.style, {
        position: "absolute",
        pointerEvents: "none",
        zIndex: "3",
        top: `${top}px`,
        left: `${left}px`,
        width: `${Math.max(1, right - left)}px`,
        height: `${bottom - top}px`,
        borderTop: "0 solid transparent",
        borderLeft: leftEdge ? `1px solid ${edge}` : "0 solid transparent",
        borderRight: rightEdge ? `1px solid ${edge}` : "0 solid transparent",
        borderBottom: `1px solid ${edge}`,
        borderBottomLeftRadius: leftEdge ? "6px" : "0",
        borderBottomRightRadius: rightEdge ? "6px" : "0",
        boxSizing: "border-box",
      });
      wrapper.appendChild(overlay);
    };

    if (diffStyle === "split" && tone === "modified") {
      const midpoint = wrapperRect.width / 2;
      const oldScope = scopeMetrics("old");
      const newScope = scopeMetrics("new");
      appendOverlay({
        side: "old",
        left: targetBounds.gutter,
        right: midpoint,
        lineCount: oldScope.lineCount,
        endLine: oldScope.endLine,
        leftEdge: true,
        rightEdge: false,
      });
      appendOverlay({
        side: "new",
        left: midpoint + targetBounds.gutter,
        right: wrapperRect.width,
        lineCount: newScope.lineCount,
        endLine: newScope.endLine,
        leftEdge: false,
        rightEdge: true,
      });
      return;
    }

    const wholeScope = scopeMetrics();
    appendOverlay({
      side: tone === "deleted" ? "old" : tone === "added" ? "new" : "all",
      left: targetBounds.left,
      right: targetBounds.right,
      lineCount: wholeScope.lineCount,
      endLine: wholeScope.endLine,
      leftEdge: true,
      rightEdge: true,
    });
  });
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
    dedicatedItem = null,
    surfaceRuns = {},
    surfaceBusy = "",
    errors,
    annotations,
    draft,
    proposalSelection = null,
    activeProposal = null,
    proposalBusy = false,
    sourceMutationSupported = false,
    comparisonMode = false,
    diffStyle,
    diffOverflow = "scroll",
    codeTheme = "lemoncrow",
    chromeTheme = "light",
    activeTargetId,
    busy,
    remainingFileCount,
    pendingVisibleFileCount = 0,
    readyHiddenFileCount = 0,
    preparingHiddenFileCount = 0,
    outstandingByPath,
    onActiveTarget,
    onDraft,
    onSuggestEdit = () => {},
    onEditSource = () => {},
    onCreateProposal = () => {},
    onCreateAndApplyProposal = () => {},
    onApplyProposal = () => {},
    onCloseProposal = () => {},
    onCreate,
    onSetAnnotationState,
    onMark,
    onComment,
    onContext,
    onBulkReview,
    onLoadMore,
    onRunSurface,
  },
  ref,
) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const horizontalDockRef = useRef<HTMLDivElement>(null);
  const activeHorizontalScrollersRef = useRef<HTMLElement[]>([]);
  const diffNodesRef = useRef(new Map<string, HTMLElement>());
  const itemElementsRef = useRef(new Map<string, HTMLElement>());
  const diffOptionsCacheRef = useRef(new Map<string, { diffStyle: DiffStyle; diffOverflow: DiffOverflow; codeTheme: ReviewCodeTheme; chromeTheme: Theme; options: any }>());
  const parsedDiffCacheRef = useRef(new Map<string, ParsedDiffCacheEntry>());
  const itemRevisionCacheRef = useRef(new Map<string, ItemRevisionEntry>());
  const targetsRef = useRef(targets);
  const activeTargetIdRef = useRef(activeTargetId);
  const onActiveTargetRef = useRef(onActiveTarget);
  const onDraftRef = useRef(onDraft);
  const draftRef = useRef(draft);
  const committedDraftPathRef = useRef("");
  targetsRef.current = targets;
  activeTargetIdRef.current = activeTargetId;
  onActiveTargetRef.current = onActiveTarget;
  onDraftRef.current = onDraft;
  draftRef.current = draft;

  const [collapsedPaths, setCollapsedPaths] = useState<ReadonlySet<string>>(new Set());
  const [diffNodeVersion, setDiffNodeVersion] = useState(0);
  const [horizontalScrollRange, setHorizontalScrollRange] = useState(0);
  const [viewportWidth, setViewportWidth] = useState(0);
  const [lineSelections, setLineSelections] = useState<Record<string, SelectedLineRange | null>>({});
  const [markdownModes, setMarkdownModes] = useState<Record<string, MarkdownRenderMode | "source">>({});
  const [markdownPreviewThemes, setMarkdownPreviewThemes] = useState<Record<string, MarkdownPreviewTheme>>({});
  const [surfaceModes, setSurfaceModes] = useState<Record<string, MarkdownRenderMode>>({});
  const initialDedicatedSurfaceKey = dedicatedItem?.kind === "surface" ? `${dedicatedItem.provider}:${dedicatedItem.id}` : "";
  const [selectedWebSurfaceKey, setSelectedWebSurfaceKey] = useState(initialDedicatedSurfaceKey);
  const [selectedApiSurfaceKey, setSelectedApiSurfaceKey] = useState(initialDedicatedSurfaceKey);
  const [activeSurfaceKey, setActiveSurfaceKey] = useState(initialDedicatedSurfaceKey);
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

  const registerDiffNode = useCallback((path: string, node: HTMLElement, phase: "mount" | "update" | "unmount") => {
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
      if (Math.abs(dock.scrollLeft - scroller.scrollLeft) > 0.5) dock.scrollLeft = scroller.scrollLeft;
    }

    const clear = () => {
      for (const scroller of boundScrollers) scroller.removeEventListener("scroll", onCodeScroll);
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
      for (const scroller of scrollers) scroller.addEventListener("scroll", onCodeScroll, { passive: true });
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
  }, [activeDiffPath, diffNodeVersion, resolvedDiffStyle, diffOverflow, codeTheme]);
  const targetsByPath = useMemo(() => {
    const map = new Map<string, ReviewTarget[]>();
    for (const target of targets) {
      const rows = map.get(target.path) ?? [];
      rows.push(target);
      map.set(target.path, rows);
    }
    return map;
  }, [targets]);
  const pathPriority = useMemo(
    () => new Map(orderedPaths(targets).map((path, index) => [path, index])),
    [targets],
  );
  const webSurfaces = useMemo(
    () => surfaces.filter((surface) => surface.kind === "web.route"),
    [surfaces],
  );
  const apiSurfaces = useMemo(
    () => surfaces.filter((surface) => surface.kind === "api.request"),
    [surfaces],
  );
  const serviceSurfaces = useMemo(
    () => surfaces.filter((surface) => surface.kind === "service"),
    [surfaces],
  );
  const webSurfacesByPath = useMemo(() => {
    const map = new Map<string, ReviewSurfaceInfo[]>();
    for (const surface of webSurfaces) {
      for (const path of surface.affected_paths) {
        const rows = map.get(path) ?? [];
        rows.push(surface);
        map.set(path, rows);
      }
    }
    return map;
  }, [webSurfaces]);
  const webRouteOptions = useMemo<WebRouteOption[]>(() => {
    const routes = webSurfaces.map((surface) => surface.locator || surface.title || "/");
    const counts = new Map<string, number>();
    for (const route of routes) counts.set(route, (counts.get(route) ?? 0) + 1);
    return webSurfaces
      .map((surface) => {
        const route = surface.locator || surface.title || "/";
        const root = String(surface.metadata?.root ?? "");
        const framework = String(surface.metadata?.framework ?? "");
        const serviceName = configuredSurfaceName(surface);
        const disambiguator = serviceName || root || surface.title || surface.provider;
        const priority = Math.min(
          ...surface.affected_paths.map((path) => pathPriority.get(path) ?? Number.MAX_SAFE_INTEGER),
          Number.MAX_SAFE_INTEGER,
        );
        return {
          route,
          surfaceKey: `${surface.provider}:${surface.id}`,
          label: (counts.get(route) ?? 0) > 1 ? `${route} · ${disambiguator}` : route,
          detail: [serviceName, framework, root].filter(Boolean).join(" · ") || surface.provider,
          searchText: [surface.title, surface.provider, surface.runtime, serviceName, framework, root].filter(Boolean).join(" "),
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
  const apiSurfaceOptions = useMemo(() => apiSurfaces
    .map((surface) => {
      const method = String(surface.metadata.method ?? "").toUpperCase();
      const url = String(surface.metadata.url ?? surface.locator ?? surface.title);
      const priority = Math.min(
        ...surface.affected_paths.map((path) => pathPriority.get(path) ?? Number.MAX_SAFE_INTEGER),
        Number.MAX_SAFE_INTEGER,
      );
      return {
        value: `${surface.provider}:${surface.id}`,
        label: [method, url].filter(Boolean).join(" "),
        detail: configuredSurfaceName(surface),
        searchText: [surface.title, surface.provider, surface.runtime, configuredSurfaceName(surface), method, url].filter(Boolean).join(" "),
        priority,
      };
    })
    .sort((a, b) => a.priority - b.priority || a.label.localeCompare(b.label)), [apiSurfaces, pathPriority]);
  const orderedServiceSurfaces = useMemo(() => [...serviceSurfaces].sort((a, b) => {
    const priority = (surface: ReviewSurfaceInfo) => Math.min(
      ...surface.affected_paths.map((path) => pathPriority.get(path) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    return priority(a) - priority(b)
      || String(a.metadata.service ?? a.title ?? a.id).localeCompare(String(b.metadata.service ?? b.title ?? b.id));
  }), [pathPriority, serviceSurfaces]);
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
      return {
        id: "surface-group:api",
        type: "api-surface",
        version: 1,
        estimatedHeight: 520,
        collapsed: false,
        surface,
        mode: surfaceModes[surfaceKey] ?? "preview",
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

    if (dedicatedItem?.kind === "file") {
      const path = dedicatedItem.path;
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
        collapsedPaths.has(path),
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
      return [];
    }

    const webItem = selectedWebSurface ? webItemFor(selectedWebSurface) : null;
    if (webItem) rows.push(webItem);
    if (selectedApiSurface) rows.push(apiItemFor(selectedApiSurface));
    for (const service of orderedServiceSurfaces) rows.push(serviceItemFor(service));

    for (const path of pathOrder) {
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
        collapsedPaths.has(path),
        mode,
        parsedDiffCacheRef.current,
        itemRevisionCacheRef.current,
      );
      if (item) rows.push(item);
    }
    for (const path of parsedDiffCacheRef.current.keys()) {
      if (!activePaths.has(path)) parsedDiffCacheRef.current.delete(path);
    }
    for (const path of itemRevisionCacheRef.current.keys()) {
      if (!activePaths.has(path)) itemRevisionCacheRef.current.delete(path);
    }
    return rows;
  }, [
    targets,
    details,
    errors,
    targetsByPath,
    inlineAnnotations,
    draft,
    collapsedPaths,
    markdownModes,
    surfaceModes,
    webSurfaces,
    apiSurfaces,
    serviceSurfaces,
    dedicatedItem,
    previewFromSurface,
    selectedWebSurface,
    selectedApiSurface,
    orderedServiceSurfaces,
  ]);
  const focusFromScroll = useCallback(() => {
    const root = scrollRef.current;
    if (!root) return;
    const nodes = [...root.querySelectorAll<HTMLElement>("[data-review-target-id]")];
    if (nodes.length === 0) return;
    const threshold = root.getBoundingClientRect().top + 115;
    let best: { id: string; distance: number; surfaceKey: string } | null = null;
    for (const node of nodes) {
      const id = node.dataset.reviewTargetId ?? "";
      if (!id) continue;
      const rect = node.getBoundingClientRect();
      const distance = rect.top <= threshold
        ? Math.abs(threshold - rect.top)
        : Math.abs(rect.top - threshold) + 1000;
      if (!best || distance < best.distance) best = { id, distance, surfaceKey: node.dataset.reviewSurfaceKey ?? "" };
    }
    if (!best) return;
    if (best.id !== activeTargetId) onActiveTarget(best.id);
    setActiveSurfaceKey(best.surfaceKey);
  }, [activeTargetId, onActiveTarget]);

  const scrollToTarget = useCallback((target: ReviewTarget, behavior: "instant" | "smooth" = "smooth") => {
    const root = scrollRef.current;
    const item = itemElementsRef.current.get(target.path);
    if (!root || !item) return;
    const renderedMarkdown = details[target.path]?.preview?.kind === "markdown"
      && (markdownModes[target.path] ?? "preview") !== "source";
    const location = targetScrollLocation(target);
    const finalBehavior = browserScrollBehavior(behavior);

    if (location && !renderedMarkdown) {
      const line = renderedDiffLine(item, location.side, location.lineNumber);
      if (line) {
        const rootRect = root.getBoundingClientRect();
        const lineRect = line.getBoundingClientRect();
        root.scrollTo({
          top: root.scrollTop + lineRect.top - rootRect.top - 52,
          behavior: finalBehavior,
        });
      } else {
        item.scrollIntoView({ behavior: finalBehavior, block: "start" });
      }
    } else {
      const rootRect = root.getBoundingClientRect();
      const itemRect = item.getBoundingClientRect();
      root.scrollTo({
        top: root.scrollTop + itemRect.top - rootRect.top - 36,
        behavior: finalBehavior,
      });
    }
    onActiveTarget(target.target_id);
  }, [details, markdownModes, onActiveTarget]);

  const scrollToFile = useCallback((path: string, behavior: "instant" | "smooth" = "smooth") => {
    const root = scrollRef.current;
    const item = itemElementsRef.current.get(path);
    if (root && item) {
      const rootRect = root.getBoundingClientRect();
      const itemRect = item.getBoundingClientRect();
      root.scrollTo({
        top: root.scrollTop + itemRect.top - rootRect.top - 36,
        behavior: browserScrollBehavior(behavior),
      });
    }
    setActiveSurfaceKey("");
    const first = targets.find((target) => target.path === path);
    if (first) onActiveTarget(first.target_id);
  }, [onActiveTarget, targets]);

  const scrollToSurface = useCallback((surfaceKey: string, behavior: "instant" | "smooth" = "smooth") => {
    const surface = surfaces.find((candidate) => `${candidate.provider}:${candidate.id}` === surfaceKey);
    if (!surface) return;
    const itemId = surface.kind === "web.route"
      ? "surface-group:web"
      : surface.kind === "api.request"
        ? "surface-group:api"
        : surface.kind === "service"
          ? `surface:${surfaceKey}`
          : "";
    if (!itemId) return;
    if (surface.kind === "web.route") setSelectedWebSurfaceKey(surfaceKey);
    else if (surface.kind === "api.request") setSelectedApiSurfaceKey(surfaceKey);
    setActiveSurfaceKey(surfaceKey);

    const root = scrollRef.current;
    const item = itemElementsRef.current.get(itemId);
    if (root && item) {
      const rootRect = root.getBoundingClientRect();
      const itemRect = item.getBoundingClientRect();
      root.scrollTo({
        top: root.scrollTop + itemRect.top - rootRect.top - 36,
        behavior: browserScrollBehavior(behavior),
      });
    }
    const first = surface.affected_paths.flatMap((path) => targetsByPath.get(path) ?? [])[0];
    if (first) onActiveTarget(first.target_id);
  }, [onActiveTarget, surfaces, targetsByPath]);
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
    setCollapsedPaths((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const setMarkdownView = useCallback((path: string, mode: MarkdownRenderMode | "source") => {
    setMarkdownModes((current) => ({ ...current, [path]: mode }));
  }, []);

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
        <div className="flex items-center gap-2 pr-2 font-mono text-[10px] text-neutral-500">
          {additions > 0 && <span className="text-emerald-400">+{additions}</span>}
          {deletions > 0 && <span className="text-rose-400">-{deletions}</span>}
          <button
            type="button"
            onClick={() => toggleCollapsed(path)}
            aria-label={collapsedPaths.has(path) ? `Expand ${path}` : `Collapse ${path}`}
            title={collapsedPaths.has(path) ? "Expand file" : "Collapse file"}
            className="inline-flex h-6 w-6 items-center justify-center rounded text-neutral-600 hover:bg-neutral-900 hover:text-neutral-50"
          >
            {collapsedPaths.has(path) ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
          </button>
        </div>
      );
    }
    const unreviewedInFile = outstandingByPath.get(path) ?? 0;
    const visibleOutstanding = Math.max(0, file.targetCount - file.reviewed);
    const hiddenUnreviewed = unreviewedInFile - visibleOutstanding;
    const hasMarkdownPreview = details[path]?.preview?.kind === "markdown";
    const hasMediaPreview = details[path]?.preview?.kind === "media";
    const hasInlinePreview = hasMarkdownPreview || hasMediaPreview;
    const markdownMode: MarkdownRenderMode | "source" = markdownModes[path] ?? "preview";
    const markdownPreviewTheme = markdownPreviewThemes[path] ?? "auto";
    const resolvedMarkdownTheme: Theme = markdownPreviewTheme === "auto" ? chromeTheme : markdownPreviewTheme;
    const nextMarkdownTheme: MarkdownPreviewTheme = markdownPreviewTheme === "auto" ? "light" : markdownPreviewTheme === "light" ? "dark" : "auto";
    const MarkdownThemeIcon = markdownPreviewTheme === "auto" ? SunMoon : markdownPreviewTheme === "light" ? Sun : Moon;
    const relatedWebSurfaces = webSurfacesByPath.get(path) ?? [];
    return (
      <div className="flex items-center gap-2 pr-2 font-mono text-[10px] text-neutral-500">
        {file.targetCount > 0 && (
          <span className={visibleOutstanding === 0
            ? "font-medium text-emerald-300"
            : file.reviewed > 0
              ? "text-neutral-400"
              : ""}
          >
            {visibleOutstanding === 0
              ? "✓ reviewed"
              : hiddenUnreviewed > 0
                ? `${visibleOutstanding} shown · ${unreviewedInFile} left in file`
                : `${visibleOutstanding} left`}
          </span>
        )}
        {relatedWebSurfaces.length > 0 && (
          <span
            className="rounded border border-sky-950 bg-sky-950/20 px-1.5 py-0.5 text-[10px] text-sky-400/70"
            title={relatedWebSurfaces.map((surface) => surface.locator || surface.title).join("\n")}
          >
            surface {relatedWebSurfaces[0]?.locator || relatedWebSurfaces[0]?.title}
            {relatedWebSurfaces.length > 1 ? ` +${relatedWebSurfaces.length - 1}` : ""}
          </span>
        )}
        {hasInlinePreview && (
          <span className="flex overflow-hidden rounded border border-neutral-800">
            {(["preview", "compare", "source"] as const).map((mode, index) => (
              <button
                key={mode}
                type="button"
                aria-label={mode}
                aria-pressed={markdownMode === mode}
                title={`${mode[0]?.toUpperCase()}${mode.slice(1)} · ${index + 1}`}
                onClick={() => setMarkdownView(path, mode)}
                className={`px-2 py-0.5 capitalize ${
                  markdownMode === mode
                    ? "bg-neutral-800 text-neutral-100"
                    : "text-neutral-500 hover:text-neutral-50"
                }`}
              >
                {mode} <span className="ml-1 text-[10px] text-neutral-600">{index + 1}</span>
              </button>
            ))}
          </span>
        )}
        {hasMarkdownPreview && markdownMode !== "source" && (
          <button
            type="button"
            aria-label={`Markdown preview theme: ${markdownPreviewTheme === "auto" ? `Auto · ${resolvedMarkdownTheme}` : markdownPreviewTheme}. Switch to ${nextMarkdownTheme}`}
            title={`Markdown preview theme: ${markdownPreviewTheme === "auto" ? `Auto · ${resolvedMarkdownTheme}` : markdownPreviewTheme}`}
            onClick={() => cycleMarkdownPreviewTheme(path)}
            className="inline-flex h-6 w-7 items-center justify-center rounded border border-neutral-800 text-violet-200 hover:bg-neutral-900"
          >
            <MarkdownThemeIcon size={12} strokeWidth={1.7} />
          </button>
        )}
        {(unreviewedInFile > 0 || Boolean(details[path]?.patch)) && (
          <details data-review-dropdown className="group relative">
            <summary
              aria-label={`File actions for ${path}`}
              title="File actions"
              className="inline-flex h-6 w-6 cursor-pointer list-none items-center justify-center rounded text-neutral-600 hover:bg-neutral-900 hover:text-neutral-50 [&::-webkit-details-marker]:hidden"
            >
              <EllipsisVertical size={13} />
            </summary>
            <div
              role="menu"
              className="absolute right-0 top-full z-40 mt-1 min-w-52 rounded-md border border-neutral-800 bg-neutral-950 p-1 shadow-xl"
            >
              {details[path]?.patch && (
                <button
                  type="button"
                  role="menuitem"
                  onClick={(event) => {
                    const safeName = path.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "change";
                    downloadTextPatch(`${safeName}.patch`, details[path]?.patch ?? "");
                    const menu = event.currentTarget.closest("details");
                    if (menu instanceof HTMLDetailsElement) menu.open = false;
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[10px] text-neutral-300 hover:bg-neutral-900 hover:text-neutral-50"
                  aria-label={`Download patch for ${path}`}
                  title="Download this frozen file patch; apply it with git apply."
                >
                  <Download size={12} className="shrink-0 text-neutral-500" />
                  <span>Download file patch</span>
                  <span className="ml-auto font-mono text-[9px] text-neutral-600">git apply</span>
                </button>
              )}
              {unreviewedInFile > 0 && (
                <button
                  type="button"
                  role="menuitem"
                  disabled={busy || hiddenUnreviewed > 0}
                  onClick={(event) => {
                    onBulkReview(path);
                    const menu = event.currentTarget.closest("details");
                    if (menu instanceof HTMLDetailsElement) menu.open = false;
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[10px] text-neutral-300 hover:bg-neutral-900 hover:text-neutral-50 disabled:cursor-not-allowed disabled:opacity-40"
                  aria-label={hiddenUnreviewed > 0
                    ? `Mark safe items reviewed unavailable in ${path} while targets are hidden`
                    : `Mark safe items reviewed in ${path}`}
                  title={hiddenUnreviewed > 0
                    ? "Some targets are hidden. Show the whole file before bulk-marking."
                    : "Marks eligible remaining targets as reviewed. Anything needing individual attention is skipped."}
                >
                  <CheckCircle2 size={12} className="shrink-0 text-emerald-500/80" />
                  <span>Mark safe items reviewed</span>
                </button>
              )}
            </div>
          </details>
        )}
        <button
          type="button"
          onClick={() => toggleCollapsed(path)}
          aria-label={collapsedPaths.has(path) ? `Expand ${path}` : `Collapse ${path}`}
          title={collapsedPaths.has(path) ? "Expand file" : "Collapse file"}
          className="inline-flex h-6 w-6 items-center justify-center rounded text-neutral-600 hover:bg-neutral-900 hover:text-neutral-50"
        >
          {collapsedPaths.has(path) ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
        </button>
      </div>
    );
  }, [
    busy,
    chromeTheme,
    comparisonMode,
    collapsedPaths,
    cycleMarkdownPreviewTheme,
    details,
    fileByPath,
    markdownModes,
    markdownPreviewThemes,
    onBulkReview,
    outstandingByPath,
    setMarkdownView,
    toggleCollapsed,
    webSurfacesByPath,
  ]);

  const renderInlineAnnotation = useCallback((path: string, annotation: StreamMarkers[number]) => {
    const metadata = annotation.metadata;
    if (!metadata) return null;
    const targetRows = metadata.targetIds
      .map((id) => targetsById.get(id))
      .filter((row): row is ReviewTarget => Boolean(row));
    const comments = commentsAt(inlineAnnotations, path, metadata.key);
    const fileLevelRows = annotation.lineNumber === 0 ? fileComments(inlineAnnotations, path) : [];
    const shownComments = comments.length > 0 ? comments : fileLevelRows;
    const composing = Boolean(
      draft
      && draft.path === path
      && (draft.fileLevel ? annotation.lineNumber === 0 : draftMarkerKey(draft) === metadata.key),
    );
    const draftTarget = composing && draft
      ? (targets.find((target) => target.unit_key === draft.targetUnitKey)
        ?? targetAtLine(targets, path, draft.side, draft.startLine, activeTargetId))
      : null;
    return (
      <div className="space-y-1">
        {targetRows.map((target) => (
          <ReviewTargetHeader
            key={target.target_id}
            target={target}
            active={target.target_id === activeTargetId}
            busy={busy}
            scopeTop={targetHasContinuousScope(target, targetsRef.current)}
            comparisonMode={comparisonMode}
            onFocus={() => onActiveTarget(target.target_id)}
            onMark={(state, advance) => onMark(target, state, advance)}
            onComment={() => onComment(target)}
            onContext={(tab) => onContext(target, tab)}
          />
        ))}

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
          />
        )}
        {composing && draft?.mode === "proposal" && (
          <ReviewChangeProposalComposer
            selection={proposalSelection}
            proposal={activeProposal}
            loading={proposalBusy && !proposalSelection}
            busy={proposalBusy}
            directEdit={draft.sourceAction === "edit"}
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
    );
  }, [
    activeTargetId,
    busy,
    draft,
    inlineAnnotations,
    onActiveTarget,
    onComment,
    onContext,
    onDraft,
    onMark,
    onSetAnnotationState,
    submit,
    targets,
    targetsById,
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

  const diffOptionsFor = useCallback((path: string) => {
    const status = details[path]?.status;
    const effectiveDiffStyle: DiffStyle = status === "added" || status === "deleted"
      ? "unified"
      : resolvedDiffStyle;
    const cached = diffOptionsCacheRef.current.get(path);
    if (
      cached
      && cached.diffStyle === effectiveDiffStyle
      && cached.diffOverflow === diffOverflow
      && cached.codeTheme === codeTheme
      && cached.chromeTheme === chromeTheme
    ) return cached.options;
    const openRangeDraft = (range: SelectedLineRange | null) => {
      if (comparisonMode || !range) return;
      const side = markerSide(range.side ?? "additions");
      const endSide = markerSide(range.endSide ?? range.side ?? "additions");
      // A comment anchor is one side of a diff. Do not silently turn a
      // cross-column drag into a different range than the reviewer selected.
      if (side !== endSide) {
        setLineSelection(path, null);
        return;
      }
      setLineSelection(path, range);
      const startLine = Math.min(range.start, range.end);
      const endLine = Math.max(range.start, range.end);
      const target = targetAtLine(targetsRef.current, path, side, startLine, activeTargetIdRef.current);
      if (target) onActiveTargetRef.current(target.target_id);
      const recovered = draftRef.current?.recovered ? draftRef.current : null;
      onDraftRef.current({
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
    };
    const options = {
      diffStyle: effectiveDiffStyle,
      overflow: diffOverflow,
      theme: themeDefinition.pierreTheme,
      themeType: chromeTheme,
      enableLineSelection: !comparisonMode,
      enableGutterUtility: !comparisonMode,
      lineHoverHighlight: "both" as const,
      onPostRender: (node: HTMLElement, _instance: unknown, phase: "mount" | "update" | "unmount") => {
        registerDiffNode(path, node, phase);
        decorateDedicatedHeader(node, phase);
        if (comparisonMode) return;
        if (phase === "unmount") {
          decorateReviewScopeBoxes(node, path, targetsRef.current, phase, chromeTheme, effectiveDiffStyle);
          return;
        }
        const applyScope = () => decorateReviewScopeBoxes(
          node,
          path,
          targetsRef.current,
          phase,
          chromeTheme,
          effectiveDiffStyle,
        );
        applyScope();
        requestAnimationFrame(() => requestAnimationFrame(applyScope));
      },
      onGutterUtilityClick: openRangeDraft,
      // selectedLines is controlled below. Mirror Pierre's in-progress range
      // without opening/moving the annotation composer until pointer-up commits
      // the selection through onLineSelected.
      onLineSelectionChange: (range: SelectedLineRange | null) => {
        if (!comparisonMode) setLineSelection(path, range);
      },
      onLineSelected: openRangeDraft,
    };
    diffOptionsCacheRef.current.set(path, {
      diffStyle: effectiveDiffStyle,
      diffOverflow,
      codeTheme,
      chromeTheme,
      options,
    });
    return options;
  }, [chromeTheme, codeTheme, comparisonMode, decorateDedicatedHeader, details, diffOverflow, registerDiffNode, resolvedDiffStyle, setLineSelection, themeDefinition]);

  const renderCollapsedHeader = useCallback((path: string) => {
    const firstTargetId = targetsByPath.get(path)?.[0]?.target_id;
    return (
      <div
        className="flex min-h-10 items-center gap-3 border-y border-neutral-800 bg-neutral-950 px-3 py-1.5"
        data-review-target-id={firstTargetId}
        data-testid={`code-item:${path}`}
        data-collapsed="true"
      >
        {comparisonMode ? (
          <div className="inline-flex min-w-0 flex-1 items-center gap-2 truncate font-mono text-[12px] text-neutral-200">
            <FileTypeIcon path={path} />
            <span className="truncate">{path}</span>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "file", path })}
            aria-label={`Open ${path} in dedicated tab`}
            title="Open file in dedicated tab"
            className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 truncate text-left font-mono text-[12px] text-neutral-200 underline-offset-2 hover:text-neutral-50 hover:underline"
          >
            <FileTypeIcon path={path} />
            <span className="truncate">{path}</span>
            <ExternalLink size={11} className="shrink-0 text-neutral-600" />
          </button>
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

    const targetRows = targetsByPath.get(path) ?? [];
    const shownComments = partition(inlineAnnotations, path).anchored.filter((comment) => !comment.parent_id);
    const composing = Boolean(draft && draft.path === path);
    const draftTarget = composing && draft
      ? (targets.find((target) => target.unit_key === draft.targetUnitKey)
        ?? targetAtLine(targets, path, draft.side, draft.startLine, activeTargetId))
      : null;
    const firstTargetId = targetRows[0]?.target_id;

    return (
      <section
        className="border-b border-neutral-900 bg-neutral-950"
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
          className="sticky top-0 z-20 flex min-h-10 items-center gap-3 border-b border-neutral-800 bg-neutral-950/95 px-3 py-1.5 backdrop-blur"
          data-review-target-id={firstTargetId}
        >
          <button
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "file", path })}
            aria-label={`Open ${path} in dedicated tab`}
            title="Open file in dedicated tab"
            className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 truncate text-left font-mono text-[12px] text-neutral-200 underline-offset-2 hover:text-neutral-50 hover:underline"
          >
            <FileTypeIcon path={path} />
            <span className="truncate">{path}</span>
            <ExternalLink size={11} className="shrink-0 text-neutral-600" />
          </button>
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
        <div className="space-y-1 border-t border-neutral-900 bg-neutral-950 px-3 py-3">
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
            />
          )}
          {composing && draft?.mode === "proposal" && (
            <ReviewChangeProposalComposer
              selection={proposalSelection}
              proposal={activeProposal}
              loading={proposalBusy && !proposalSelection}
              busy={proposalBusy}
              directEdit={draft.sourceAction === "edit"}
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
      </section>
    );
  }, [
    activeTargetId,
    busy,
    details,
    draft,
    chromeTheme,
    headerControls,
    inlineAnnotations,
    markdownPreviewThemes,
    onActiveTarget,
    onComment,
    onContext,
    onDraft,
    onMark,
    onSetAnnotationState,
    renderCollapsedHeader,
    submit,
    targets,
    targetsByPath,
  ]);

  const renderMedia = useCallback((item: MediaStreamItem) => {
    const path = item.id;
    const detail = details[path];
    if (detail?.preview?.kind !== "media") return null;
    if (item.collapsed) return renderCollapsedHeader(path);
    const targetRows = targetsByPath.get(path) ?? [];
    const firstTargetId = targetRows[0]?.target_id;

    return (
      <section
        className="border-b border-neutral-900 bg-neutral-950"
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
          className="sticky top-0 z-20 flex min-h-10 items-center gap-3 border-b border-neutral-800 bg-neutral-950/95 px-3 py-1.5 backdrop-blur"
          data-review-target-id={firstTargetId}
        >
          <button
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "file", path })}
            aria-label={`Open ${path} in dedicated tab`}
            title="Open file in dedicated tab"
            className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 truncate text-left font-mono text-[12px] text-neutral-200 underline-offset-2 hover:text-neutral-50 hover:underline"
          >
            <FileTypeIcon path={path} />
            <span className="truncate">{path}</span>
            <ExternalLink size={11} className="shrink-0 text-neutral-600" />
          </button>
          {headerControls(path)}
        </div>
        <MediaReviewPreview
          preview={detail.preview}
          documentPath={path}
          revisionId={detail.revision_id ?? ""}
          status={detail.status}
          mode={item.mode}
        />
        <div className="space-y-1 border-t border-neutral-900 bg-neutral-950 px-3 py-3">
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
              onContext={(tab) => onContext(target, tab)}
            />
          ))}
        </div>
      </section>
    );
  }, [activeTargetId, busy, details, headerControls, onActiveTarget, onComment, onContext, onMark, renderCollapsedHeader, targetsByPath]);

  const renderWebSurface = useCallback((item: WebSurfaceStreamItem) => {
    const surfaceKey = `${item.surface.provider}:${item.surface.id}`;
    const firstTargetId = item.surface.affected_paths
      .map((path) => targetsByPath.get(path)?.[0]?.target_id)
      .find((id): id is string => Boolean(id));
    return (
      <section
        className="border-b border-sky-950/60 bg-neutral-950"
        data-testid={`web-surface:${surfaceKey}`}
        data-review-target-id={firstTargetId}
        data-review-surface-key={surfaceKey}
        onMouseEnter={() => {
          setActiveSurfaceKey(surfaceKey);
          activeMarkdownPathRef.current = "";
        }}
      >
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
  }, [activeSurfaceKey, chromeTheme, details, scrollToFile, targetsByPath, webRouteOptions]);

  const renderApiSurface = useCallback((item: ApiSurfaceStreamItem) => {
    const surfaceKey = `${item.surface.provider}:${item.surface.id}`;
    const firstTargetId = item.surface.affected_paths
      .map((path) => targetsByPath.get(path)?.[0]?.target_id)
      .find((id): id is string => Boolean(id));
    const run = surfaceRuns[surfaceKey];
    const executable = item.surface.capabilities.includes("execute") && Boolean(item.surface.runtime);
    const statusCode = run?.new?.data?.status_code;
    const statusLabel = surfaceBusy === surfaceKey
      ? "Checking…"
      : run?.error
        ? "Failed"
        : run?.new
          ? (typeof statusCode === "number" ? `HTTP ${statusCode}` : run.new.status)
          : executable
            ? "Ready"
            : "Not runnable";
    const statusTone = surfaceBusy === surfaceKey
      ? "text-amber-400/70"
      : run?.error || run?.new?.status === "failed"
        ? "text-rose-400/80"
        : run?.new?.status === "passed"
          ? "text-emerald-400/80"
          : "text-neutral-500";
    return (
      <section
        className="border-b border-cyan-950/60 bg-neutral-950"
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
        <div className="sticky top-0 z-20 flex min-h-10 items-center gap-2 border-b border-neutral-800 bg-neutral-950/95 px-3 py-1.5 backdrop-blur">
          <button
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "surface", provider: item.surface.provider, id: item.surface.id })}
            aria-label={`Open API surface ${item.surface.title || item.surface.id} in dedicated tab`}
            title="Open API surface in dedicated tab"
            className="inline-flex shrink-0 items-center gap-1 text-[10px] font-medium uppercase tracking-[0.14em] text-cyan-400/70 hover:text-cyan-300"
          >
            API <span className="normal-case tracking-normal text-cyan-300/90">· {configuredSurfaceName(item.surface)}</span> <ExternalLink size={10} />
          </button>
          {apiSurfaceOptions.length > 1 ? (
            <SurfacePicker
              ariaLabel="API endpoint"
              value={surfaceKey}
              storageKey={`lemoncrow-review-surface-filter:${currentReviewId()}:api`}
              placeholder="Search methods, paths, providers…"
              options={apiSurfaceOptions}
              onChange={(nextSurfaceKey) => {
                setSelectedApiSurfaceKey(nextSurfaceKey);
                setActiveSurfaceKey(nextSurfaceKey);
              }}
            />
          ) : (
            <span className="min-w-0 truncate font-mono text-[12px] text-neutral-100">{item.surface.locator || item.surface.title}</span>
          )}
          <span className={`shrink-0 text-[10px] ${statusTone}`}>{statusLabel}</span>
          <span className="flex-1" />
          <span className="flex overflow-hidden rounded border border-neutral-800">
            {(["preview", "compare"] as const).map((mode, index) => (
              <button
                key={mode}
                type="button"
                aria-label={mode}
                aria-pressed={item.mode === mode}
                title={`${mode[0]?.toUpperCase()}${mode.slice(1)} · ${index + 1}`}
                onClick={() => setSurfaceModes((current) => ({ ...current, [surfaceKey]: mode }))}
                className={`px-2 py-0.5 text-[10px] capitalize ${item.mode === mode ? "bg-neutral-800 text-neutral-100" : "text-neutral-500 hover:text-neutral-50"}`}
              >
                {mode} <span className="ml-1 text-[10px] text-neutral-600">{index + 1}</span>
              </button>
            ))}
          </span>
        </div>
        <ApiReviewPreview
          surface={item.surface}
          mode={item.mode}
          run={run}
          running={surfaceBusy === surfaceKey}
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
  }, [activeSurfaceKey, apiSurfaceOptions, onRunSurface, surfaceBusy, surfaceRuns, targetsByPath]);

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
    const statusLabel = surfaceBusy === surfaceKey
      ? "Checking…"
      : run?.error
        ? "Failed"
        : run?.new
          ? (run.new.status === "passed" ? "Valid" : run.new.status)
          : executable
            ? "Ready"
            : "Not runnable";
    const statusTone = surfaceBusy === surfaceKey
      ? "text-amber-400/70"
      : run?.error || run?.new?.status === "failed"
        ? "text-rose-400/80"
        : run?.new?.status === "passed"
          ? "text-emerald-400/80"
          : "text-neutral-500";
    return (
      <section
        className="border-b border-violet-950/60 bg-neutral-950"
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
        <div className="sticky top-0 z-20 flex min-h-10 items-center gap-2 border-b border-neutral-800 bg-neutral-950/95 px-3 py-1.5 backdrop-blur">
          <button
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "surface", provider: item.surface.provider, id: item.surface.id })}
            aria-label={`Open runtime surface ${item.surface.title || item.surface.id} in dedicated tab`}
            title="Open runtime surface in dedicated tab"
            className="inline-flex shrink-0 items-center gap-1 text-[10px] font-medium uppercase tracking-[0.14em] text-violet-400/75 hover:text-violet-300"
          >
            <Boxes size={12} /> {runtimeLabel} <span className="normal-case tracking-normal text-violet-300/90">· {configuredSurfaceName(item.surface)}</span> <ExternalLink size={10} />
          </button>
          <span className="min-w-0 truncate font-mono text-[12px] text-neutral-100">{serviceLabel}</span>
          <span className={`shrink-0 text-[10px] ${statusTone}`}>{statusLabel}</span>
          <span className="flex-1" />
          <span className="flex overflow-hidden rounded border border-neutral-800">
            {(["preview", "compare"] as const).map((mode, index) => (
              <button
                key={mode}
                type="button"
                aria-label={`runtime-${mode}`}
                aria-pressed={item.mode === mode}
                title={`${mode[0]?.toUpperCase()}${mode.slice(1)} · ${index + 1}`}
                onClick={() => setSurfaceModes((current) => ({ ...current, [surfaceKey]: mode }))}
                className={`px-2 py-0.5 text-[10px] capitalize ${item.mode === mode ? "bg-neutral-800 text-neutral-100" : "text-neutral-500 hover:text-neutral-50"}`}
              >
                {mode} <span className="ml-1 text-[10px] text-neutral-600">{index + 1}</span>
              </button>
            ))}
          </span>
        </div>
        <ServiceReviewPreview
          surface={item.surface}
          mode={item.mode}
          run={run}
          running={surfaceBusy === surfaceKey}
          active={activeSurfaceKey === surfaceKey}
          onActivate={() => {
            setActiveSurfaceKey(surfaceKey);
            activeMarkdownPathRef.current = "";
          }}
          onRun={(compare) => onRunSurface?.(item.surface, compare)}
        />
      </section>
    );
  }, [activeSurfaceKey, onRunSurface, surfaceBusy, surfaceRuns, targetsByPath]);
  const renderItem = useCallback((item: StreamItem) => {
    if (item.type === "markdown") return renderMarkdown(item);
    if (item.type === "media") return renderMedia(item);
    if (item.type === "web-surface") return renderWebSurface(item);
    if (item.type === "api-surface") return renderApiSurface(item);
    if (item.type === "service-surface") return renderServiceSurface(item);
    if (item.collapsed) return renderCollapsedHeader(item.id);
    const firstTargetId = targetsByPath.get(item.id)?.[0]?.target_id;
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
            fileDiff={item.fileDiff}
            options={diffOptionsFor(item.id)}
            lineAnnotations={item.annotations}
            selectedLines={comparisonMode ? undefined : (lineSelections[item.id] ?? draftSelection(draft, item.id))}
            renderHeaderPrefix={() => <FileTypeIcon path={item.id} />}
            renderHeaderFilenameSuffix={() => comparisonMode ? null : (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  openDedicatedReviewItem({ kind: "file", path: item.id });
                }}
                aria-label={`Open ${item.id} in dedicated tab`}
                title="Open file in dedicated tab"
                className="ml-1 inline-flex items-center text-neutral-600 hover:text-neutral-50"
              >
                <ExternalLink size={10} />
              </button>
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
            },
          }}
          renderHeaderPrefix={() => <FileTypeIcon path={item.id} />}
          renderHeaderFilenameSuffix={() => (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                openDedicatedReviewItem({ kind: "file", path: item.id });
              }}
              aria-label={`Open ${item.id} in dedicated tab`}
              title="Open file in dedicated tab"
              className="ml-1 inline-flex items-center text-neutral-600 hover:text-neutral-50"
            >
              <ExternalLink size={10} />
            </button>
          )}
          renderHeaderMetadata={() => headerControls(item.id)}
          style={diffSurfaceStyle}
        />
      </div>
    );
  }, [
    diffOptionsFor,
    headerControls,
    renderCollapsedHeader,
    renderInlineAnnotation,
    renderMarkdown,
    renderMedia,
    renderWebSurface,
    renderApiSurface,
    renderServiceSurface,
    targetsByPath,
    chromeTheme,
    themeDefinition,
    decorateDedicatedHeader,
    draft,
    lineSelections,
  ]);

  if (items.length === 0) {
    return <div className="flex min-h-0 flex-1 items-center justify-center text-[11px] text-neutral-600" role="status">Preparing review content…</div>;
  }

  return (
    <div
      ref={viewportRef}
      className="relative min-h-0 flex-1 bg-neutral-950"
      data-review-diff-preference={diffStyle}
      data-review-diff-style={resolvedDiffStyle}
    >
      <div
        ref={scrollRef}
        className="absolute inset-0 overflow-y-auto bg-neutral-950"
        data-testid="review-stream"
        onScroll={() => window.requestAnimationFrame(focusFromScroll)}
      >
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
              {renderItem(item)}
            </div>
          ))}
        </div>
        {/* Pagination is only offered for content that is already prepared. Until
            then this footer is progress, never a control that starts work. */}
        {!dedicatedItem && (pendingVisibleFileCount > 0 || remainingFileCount > 0) ? (
          <div className="flex items-center justify-center gap-3 border-t border-neutral-900 py-5">
            {pendingVisibleFileCount > 0 ? (
              <span role="status" aria-live="polite" className="text-[10px] text-neutral-600">
                Preparing {pendingVisibleFileCount} selected file{pendingVisibleFileCount === 1 ? "" : "s"}…
              </span>
            ) : readyHiddenFileCount > 0 ? (
              <button
                type="button"
                onClick={onLoadMore}
                className="border border-neutral-800 px-3 py-1.5 text-[10px] text-neutral-500 hover:border-neutral-600 hover:text-neutral-50"
              >
                Show {readyHiddenFileCount} ready file{readyHiddenFileCount === 1 ? "" : "s"} · {remainingFileCount} remaining
              </button>
            ) : remainingFileCount > 0 ? (
              <span role="status" aria-live="polite" className="text-[10px] text-neutral-600">
                {preparingHiddenFileCount > 0
                  ? `Preparing next ${preparingHiddenFileCount} file${preparingHiddenFileCount === 1 ? "" : "s"}…`
                  : "Preparing next review chunk…"}
              </span>
            ) : null}
          </div>
        ) : <div className="h-8" />}
      </div>
      {horizontalScrollRange > 1 && (
        <div
          ref={horizontalDockRef}
          data-testid="review-horizontal-scrollbar"
          aria-label={`Horizontal scroll for ${activeDiffPath}`}
          onScroll={onHorizontalDockScroll}
          className="review-horizontal-scrollbar absolute inset-x-0 bottom-0 z-40 overflow-x-auto overflow-y-hidden border-t border-neutral-800 bg-neutral-950/95"
        >
          <div aria-hidden="true" style={{ width: `calc(100% + ${horizontalScrollRange}px)`, height: 1 }} />
        </div>
      )}
    </div>
  );
});

export default ReviewStream;
