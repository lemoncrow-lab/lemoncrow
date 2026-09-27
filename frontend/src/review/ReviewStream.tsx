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
} from "react";
import { parsePatchFiles } from "@pierre/diffs";
import { Boxes, CheckCircle2, ChevronDown, ChevronUp, Download, EllipsisVertical, ExternalLink, FilePenLine, MessageSquareText, Moon, Pencil, Puzzle, Sparkles, Sun, SunMoon, X } from "lucide-react";
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
import SurfacePicker, { type SurfacePickerOption } from "./SurfacePicker";
import SurfacePluginPreview, { surfaceComparable, type SurfacePreviewMode } from "./SurfacePluginPreview";
import WebReviewPreview, { type WebRouteOption } from "./WebReviewPreview";
import { motionSafeScrollBehavior, resolveDiffStyle, type DiffOverflow, type DiffStyle, type DiffStylePreference } from "./diffModel";
import { reviewCodeThemeDefinition, type ReviewCodeTheme } from "./reviewCodeTheme";
import { openDedicatedReviewItem, type DedicatedReviewItem } from "./reviewItemNavigation";
import { currentReviewId, fetchSurfaceImpactWithRetry, type SurfaceImpactScore } from "./reviewApi";
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
import { groupReaderFiles, orderedPaths, targetScrollLocation } from "./readerModel";
import { decorateReviewScopeBoxes, renderedDiffLine } from "./reviewScopeOverlay";
import { decorateCssColorSwatches } from "./cssColorSwatches";
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
  annotations: StreamMarkers;
}

interface FileStreamItem extends StreamItemBase {
  type: "file";
  file: CodeViewFileItem<StreamMarkerMeta>["file"];
}

type StreamItem = MarkdownStreamItem | CsvStreamItem | MediaStreamItem | WebSurfaceStreamItem | ApiSurfaceStreamItem | ServiceSurfaceStreamItem | PluginSurfaceStreamItem | LinkedSurfaceStreamItem | DiffStreamItem | FileStreamItem;

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
  type: "diff" | "file" | "markdown" | "csv" | "media" | "linked-surface";
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
  /** Snapshot every plugin preview is pinned to. */
  revisionId?: string;
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
  onQuickEdit?(draft: Draft): void;
  onCreateProposal?(): void;
  onCreateAndApplyProposal?(): void;
  onApplyProposal?(): void;
  onCloseProposal?(): void;
  onCreate(draft: AnnotationDraft): void;
  onSetAnnotationState(id: string, state: "open" | "resolved"): void;
  onMark(target: ReviewTarget, state: MarkState, advance?: boolean): void;
  onComment(target: ReviewTarget): void;
  onAsk?(target: ReviewTarget): void;
  onAskSelection?(draft: Draft): void;
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

// Only real discussion/proposal content becomes a line annotation. Review targets
// are coverage metadata rendered by the scope overlay, not pseudo-comments.
function buildMarkers(
  path: string,
  annotations: Annotation[],
  draft: Draft | null,
): { side: "additions" | "deletions"; lineNumber: number; metadata: StreamMarkerMeta }[] {
  const markers = new Map<string, { side: "additions" | "deletions"; lineNumber: number; metadata: StreamMarkerMeta }>();

  const ensure = (side: "additions" | "deletions", lineNumber: number) => {
    const key = markerKey(side, lineNumber);
    let row = markers.get(key);
    if (!row) {
      row = { side, lineNumber, metadata: { key } };
      markers.set(key, row);
    }
    return row;
  };

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
  type: "diff" | "file" | "markdown" | "csv" | "media" | "linked-surface",
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
      const markers = comparisonMode ? [] : buildMarkers(path, annotations, draft);
      const signature = `${markerSignature(annotations, path, draft)}#${targetScopeSignature(targets)}`;
      const item: DiffStreamItem = {
        id: path,
        type: "diff",
        fileDiff,
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
    dedicatedItem = null,
    surfaceRuns = {},
    surfaceBusy = "",
    revisionId = "",
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
    onQuickEdit = () => {},
    onCreateProposal = () => {},
    onCreateAndApplyProposal = () => {},
    onApplyProposal = () => {},
    onCloseProposal = () => {},
    onCreate,
    onSetAnnotationState,
    onMark,
    onAskSelection = () => {},
    onBulkReview,
    onLoadMore,
    onRunSurface,
  },
  ref,
) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const horizontalDockRef = useRef<HTMLDivElement>(null);
  const selectionMenuRef = useRef<HTMLDivElement>(null);
  const lastPointerRef = useRef({ clientX: 0, clientY: 0 });
  const suppressNextSelectionEndRef = useRef(false);
  const scrollFocusFrameRef = useRef<number | null>(null);
  const scopeFrameByPathRef = useRef(new Map<string, number>());
  const targetLineAnchorsRef = useRef(new Map<string, HTMLElement>());
  const activeScopePathRef = useRef("");
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

  const dismissSelectionAction = useCallback((clearSelection = true) => {
    if (clearSelection && selectionAction) setLineSelection(selectionAction.path, null);
    setSelectionAction(null);
  }, [selectionAction, setLineSelection]);

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
        onAskSelection(selected.draft);
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
    const changeAffinity = (surface: ReviewSurfaceInfo, path: string) => {
      if (surface.kind !== "storybook.story") return 1;
      const exportName = String(surface.metadata?.export ?? "");
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
  }, [surfaces, targets]);
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
          collapsedPaths.has(path),
        ),
        estimatedHeight: collapsedPaths.has(path) ? 44 : (surfaceView(surface) === "web" ? 860 : 620),
        collapsed: collapsedPaths.has(path),
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

    // Keep the full Web preview as a first-class top-level surface. File-level
    // Preview/Compare is an additional on-demand affordance, not a replacement.
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
  ]);
  // Programmatic navigation (j/k, outline, file jumps) owns the active target
  // until its scroll settles; otherwise the scroll-sync below reassigns it to
  // whatever file header passes the threshold mid-animation.
  const programmaticScrollUntilRef = useRef(0);
  const pinProgrammaticScroll = useCallback(() => {
    programmaticScrollUntilRef.current = Date.now() + 400;
  }, []);
  const focusFromScroll = useCallback(() => {
    const root = scrollRef.current;
    if (!root) return;
    if (Date.now() < programmaticScrollUntilRef.current) {
      programmaticScrollUntilRef.current = Date.now() + 150;
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
      if (!line) continue;
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
    const root = scrollRef.current;
    const item = itemElementsRef.current.get(target.path);
    if (!root || !item) return;
    const renderedMarkdown = details[target.path]?.preview?.kind === "markdown"
      && (markdownModes[target.path] ?? "preview") !== "source";
    const location = targetScrollLocation(target);
    const finalBehavior = browserScrollBehavior(behavior);
    pinProgrammaticScroll();

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
  }, [details, markdownModes, onActiveTarget, pinProgrammaticScroll]);

  const scrollToFile = useCallback((path: string, behavior: "instant" | "smooth" = "smooth") => {
    const root = scrollRef.current;
    const item = itemElementsRef.current.get(path);
    if (root && item) {
      pinProgrammaticScroll();
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
  }, [onActiveTarget, pinProgrammaticScroll, targets]);

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

    const root = scrollRef.current;
    const item = itemElementsRef.current.get(itemId);
    if (root && item) {
      pinProgrammaticScroll();
      const rootRect = root.getBoundingClientRect();
      const itemRect = item.getBoundingClientRect();
      root.scrollTo({
        top: root.scrollTop + itemRect.top - rootRect.top - 36,
        behavior: browserScrollBehavior(behavior),
      });
    }
    const first = surface.affected_paths.flatMap((path) => targetsByPath.get(path) ?? [])[0];
    if (first) onActiveTarget(first.target_id);
  }, [onActiveTarget, pinProgrammaticScroll, surfaces, targetsByPath]);
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
                className="max-w-48 truncate rounded border border-violet-950 bg-violet-950/20 px-1.5 py-0.5 text-[10px] text-violet-300/80"
                title={selectedLinked?.title || selectedLinked?.locator || "Linked preview"}
              >
                {selectedLinked?.kind === "storybook.story" ? "Storybook" : selectedLinked?.kind === "web.route" ? "Web" : "Preview"}
                {selectedLinked?.title ? ` · ${selectedLinked.title}` : ""}
              </span>
            )}
            <span className="flex overflow-hidden rounded border border-neutral-800">
              <button
                type="button"
                aria-label={`Source for ${path}`}
                aria-pressed={fileSurfaceMode === "source"}
                onClick={() => setFileSurfaceView(path, "source")}
                className={`px-2 py-0.5 ${fileSurfaceMode === "source" ? "bg-neutral-800 text-neutral-100" : "text-neutral-500 hover:text-neutral-50"}`}
              >Source</button>
              <button
                type="button"
                aria-label={`Preview ${path}`}
                aria-pressed={fileSurfaceMode === "preview"}
                onClick={() => setFileSurfaceView(path, "preview")}
                className={`px-2 py-0.5 ${fileSurfaceMode === "preview" ? "bg-neutral-800 text-neutral-100" : "text-neutral-500 hover:text-neutral-50"}`}
              >Preview</button>
              {linkedComparable && (
                <button
                  type="button"
                  aria-label={`Compare preview ${path}`}
                  aria-pressed={fileSurfaceMode === "compare"}
                  onClick={() => setFileSurfaceView(path, "compare")}
                  className={`px-2 py-0.5 ${fileSurfaceMode === "compare" ? "bg-neutral-800 text-neutral-100" : "text-neutral-500 hover:text-neutral-50"}`}
                >Compare</button>
              )}
            </span>
          </>
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
                    ? `Mark safe items reviewed unavailable in ${path} while review areas are hidden`
                    : `Mark safe items reviewed in ${path}`}
                  title={hiddenUnreviewed > 0
                    ? "Some review areas are hidden. Show the whole file before bulk-marking."
                    : "Marks eligible remaining review areas as reviewed. Anything needing individual attention is skipped."}
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
    fileSurfaceModes,
    linkedPreviewSurfacesByPath,
    markdownModes,
    markdownPreviewThemes,
    onBulkReview,
    outstandingByPath,
    selectedFileSurfaceKeys,
    setFileSurfaceView,
    setMarkdownView,
    surfacesByKey,
    toggleCollapsed,
  ]);
  const renderInlineAnnotation = useCallback((path: string, annotation: StreamMarkers[number]) => {
    const metadata = annotation.metadata;
    if (!metadata) return null;
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
    if (shownComments.length === 0 && (!composing || draft?.mode === "proposal")) {
      if (!(composing && draft?.mode === "proposal")) return null;
    }
    return (
      <div className="space-y-1">
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
            onAsk={composing && draft ? () => onAskSelection(draft) : undefined}
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
            quickEdit={draft.sourceAction === "quick"}
            allowApply={sourceMutationSupported}
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
    onAskSelection,
    onDraft,
    onSetAnnotationState,
    submit,
    targets,
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
      return {
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
      };
    };
    const openRangeDraft = (range: SelectedLineRange | null) => {
      const nextDraft = draftForRange(range);
      if (!nextDraft) return;
      setSelectionAction(null);
      openDraftPreservingViewport(nextDraft);
    };
    const openRangeActions = (range: SelectedLineRange | null) => {
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
      enableLineSelection: !comparisonMode,
      enableGutterUtility: !comparisonMode,
      lineHoverHighlight: "both" as const,
      onPostRender: (node: HTMLElement, _instance: unknown, phase: "mount" | "update" | "unmount") => {
        registerDiffNode(path, node, phase);
        decorateDedicatedHeader(node, phase);
        decorateCssColorSwatches(node, path, phase);
        if (comparisonMode) return;
        const pendingScopeFrame = scopeFrameByPathRef.current.get(path);
        if (pendingScopeFrame !== undefined) {
          window.cancelAnimationFrame(pendingScopeFrame);
          scopeFrameByPathRef.current.delete(path);
        }
        if (phase === "unmount") {
          decorateReviewScopeBoxes(node, path, targetsRef.current, phase, chromeTheme, effectiveDiffStyle, activeTargetIdRef.current);
          return;
        }
        const applyScope = () => decorateReviewScopeBoxes(
          node,
          path,
          targetsRef.current,
          phase,
          chromeTheme,
          effectiveDiffStyle,
          activeTargetIdRef.current,
        );
        // Paint immediately on mount, then do one coalesced geometry settle.
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
        openRangeDraft(range);
      },
      onLineSelectionChange: (range: SelectedLineRange | null) => {
        if (comparisonMode) return;
        setSelectionAction(null);
        setLineSelection(path, range);
      },
      // Double-click the editable/new-side line number for a true inline quick
      // edit. Code-content double-click stays native so word selection/copying
      // is never hijacked by editing.
      onLineNumberClick: ({ lineNumber, side, event }: { lineNumber: number; side: string; event: MouseEvent }) => {
        if (event.detail !== 2) return;
        event.preventDefault();
        openQuickEdit(lineNumber, side);
      },
      // Normal code selection opens the contextual actions automatically on
      // pointer-up. Gutter/comment affordances are handled separately above.
      onLineSelectionEnd: (range: SelectedLineRange | null) => {
        if (suppressNextSelectionEndRef.current) {
          suppressNextSelectionEndRef.current = false;
          return;
        }
        openRangeActions(range);
      },
      // Do not use Pierre's generic committed callback for UI: it also follows
      // gutter gestures and was the source of annotation/comment click popups.
      onLineSelected: () => {},
    };
    diffOptionsCacheRef.current.set(path, {
      diffStyle: effectiveDiffStyle,
      diffOverflow,
      codeTheme,
      chromeTheme,
      options,
    });
    return options;
  }, [chromeTheme, codeTheme, comparisonMode, decorateDedicatedHeader, details, diffOverflow, openDraftPreservingViewport, registerDiffNode, resolvedDiffStyle, setLineSelection, themeDefinition]);

  useEffect(() => {
    if (comparisonMode) return;
    const currentPath = targetsById.get(activeTargetId)?.path ?? "";
    const affectedPaths = new Set([activeScopePathRef.current, currentPath].filter(Boolean));
    activeScopePathRef.current = currentPath;
    for (const path of affectedPaths) {
      const node = diffNodesRef.current.get(path);
      if (!node) continue;
      const status = details[path]?.status;
      const effectiveDiffStyle: DiffStyle = status === "added" || status === "deleted"
        ? "unified"
        : resolvedDiffStyle;
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
  }, [activeTargetId, chromeTheme, comparisonMode, details, resolvedDiffStyle, targets, targetsById]);

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
            <span className="truncate" title={path}>{path}</span>
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
            <span className="truncate" title={path}>{path}</span>
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
            <span className="truncate" title={path}>{path}</span>
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
        {(shownComments.length > 0 || composing) && (
          <div className="space-y-1 border-t border-neutral-900 bg-neutral-950 px-3 py-3">
            {(shownComments.length > 0 || draft?.mode !== "proposal") && (
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
                onAsk={composing && draft ? () => onAskSelection(draft) : undefined}
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
                quickEdit={draft.sourceAction === "quick"}
                allowApply={sourceMutationSupported}
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
    inlineAnnotations,
    markdownPreviewThemes,
    onActiveTarget,
    onAskSelection,
    onDraft,
    onSetAnnotationState,
    renderCollapsedHeader,
    submit,
    targets,
    targetsByPath,
  ]);
  const renderCsv = useCallback((item: CsvStreamItem) => {
    const path = item.id;
    const detail = details[path];
    if (detail?.preview?.kind !== "csv") return null;
    if (item.collapsed) return renderCollapsedHeader(path);
    const firstTargetId = targetsByPath.get(path)?.[0]?.target_id;

    return (
      <section
        className="border-b border-neutral-900 bg-neutral-950"
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
            <span className="truncate" title={path}>{path}</span>
            <ExternalLink size={11} className="shrink-0 text-neutral-600" />
          </button>
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
            <span className="truncate" title={path}>{path}</span>
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
      </section>
    );
  }, [details, headerControls, renderCollapsedHeader, targetsByPath]);
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
    const comparable = executable && item.surface.capabilities.includes("compare");
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
            <span className="min-w-0 truncate font-mono text-[12px] text-neutral-100">{item.surface.locator || item.surface.title}</span>
          )}
          <span className={`shrink-0 text-[10px] ${statusTone}`}>{statusLabel}</span>
          {surfaceImpactBusy && apiSurfaces.some(surfaceAutoRankable) && (
            <span className="shrink-0 font-mono text-[9px] text-neutral-600" title="Ranking affected safe endpoints by immutable old/new response delta">
              Ranking responses…
            </span>
          )}
          <span className="flex-1" />
          {comparable && (
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
          )}
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
  }, [activeSurfaceKey, apiSurfaceOptions, apiSurfaces, loadSurfaceImpact, onRunSurface, surfaceBusy, surfaceImpactBusy, surfaceImpactScores, surfaceRuns, surfacesByKey, targetsByPath]);

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

  const renderPluginSurface = useCallback((item: PluginSurfaceStreamItem) => {
    const surface = item.surface;
    const surfaceKey = `${surface.provider}:${surface.id}`;
    const firstTargetId = surface.affected_paths
      .map((path) => targetsByPath.get(path)?.[0]?.target_id)
      .find((id): id is string => Boolean(id));
    const descriptor = surface.preview;
    const run = surfaceRuns[surfaceKey];
    const running = surfaceBusy === surfaceKey;
    const executable = surface.capabilities.includes("execute") && Boolean(surface.runtime);
    const kindLabel = descriptor?.label || surface.kind || "Surface";
    const title = surface.title || surface.locator || surface.id;
    const statusLabel = descriptor
      ? (descriptor.status === "unavailable" ? "Unavailable" : "Snapshot-pinned")
      : running
        ? "Running…"
        : run?.error
          ? "Failed"
          : run?.new
            ? run.new.status
            : executable
              ? "Ready"
              : "Source only";
    const statusTone = descriptor?.status === "unavailable" || run?.error || run?.new?.status === "failed"
      ? "text-rose-400/80"
      : running
        ? "text-amber-400/70"
        : run?.new?.status === "passed"
          ? "text-emerald-400/80"
          : "text-neutral-500";
    const activate = () => {
      setActiveSurfaceKey(surfaceKey);
      activeMarkdownPathRef.current = "";
      if (surfaceAutoRankable(surface) && !surfaceImpactScores[surfaceKey]) {
        void loadSurfaceImpact(surface, 4).catch(() => undefined);
      }
    };
    return (
      <section
        className="border-b border-emerald-950/60 bg-neutral-950"
        data-testid={`plugin-surface:${surfaceKey}`}
        data-review-target-id={firstTargetId}
        data-review-surface-key={surfaceKey}
        onMouseEnter={activate}
        onFocusCapture={activate}
      >
        <div className="sticky top-0 z-20 flex min-h-10 items-center gap-2 border-b border-neutral-800 bg-neutral-950/95 px-3 py-1.5 backdrop-blur">
          <button
            type="button"
            onClick={() => openDedicatedReviewItem({ kind: "surface", provider: surface.provider, id: surface.id })}
            aria-label={`Open ${kindLabel} surface ${title} in dedicated tab`}
            title="Open surface in dedicated tab"
            className="inline-flex shrink-0 items-center gap-1 text-[10px] font-medium uppercase tracking-[0.14em] text-emerald-400/75 hover:text-emerald-300"
          >
            <Puzzle size={12} /> {kindLabel} <span className="normal-case tracking-normal text-emerald-300/90">· {surface.provider}</span> <ExternalLink size={10} />
          </button>
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
            <span className="min-w-0 truncate font-mono text-[12px] text-neutral-100">{title}</span>
          )}
          <span className={`shrink-0 text-[10px] ${statusTone}`}>{statusLabel}</span>
          <span className="flex-1" />
          {surfaceComparable(surface) && (
            <span className="flex overflow-hidden rounded border border-neutral-800">
              {(["preview", "compare"] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  aria-label={`surface-${mode}`}
                  aria-pressed={item.mode === mode}
                  title={`${mode[0]?.toUpperCase()}${mode.slice(1)}`}
                  onClick={() => setSurfaceModes((current) => ({ ...current, [surfaceKey]: mode }))}
                  className={`px-2 py-0.5 text-[10px] capitalize ${item.mode === mode ? "bg-neutral-800 text-neutral-100" : "text-neutral-500 hover:text-neutral-50"}`}
                >
                  {mode}
                </button>
              ))}
            </span>
          )}
        </div>
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
  }, [loadSurfaceImpact, onRunSurface, revisionId, scrollToFile, surfaceBusy, surfaceImpactScores, surfaceRuns, surfacesByKey, targetsByPath]);

  const renderLinkedSurface = useCallback((item: LinkedSurfaceStreamItem) => {
    const path = item.id;
    const surface = item.surface;
    const surfaceKey = `${surface.provider}:${surface.id}`;
    if (item.collapsed) return renderCollapsedHeader(path);
    const firstTargetId = targetsByPath.get(path)?.[0]?.target_id;
    const activate = () => {
      setActiveSurfaceKey(surfaceKey);
      activeMarkdownPathRef.current = "";
    };
    const fileHeader = (
      <div
        className="sticky top-0 z-40 flex min-h-10 items-center gap-3 border-b border-neutral-800 bg-neutral-950/95 px-3 py-1.5 backdrop-blur"
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
          <span className="truncate" title={path}>{path}</span>
          <ExternalLink size={11} className="shrink-0 text-neutral-600" />
        </button>
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
          className="border-b border-sky-950/60 bg-neutral-950"
          data-testid={`linked-surface:${path}`}
          data-review-surface-key={surfaceKey}
          onMouseEnter={activate}
          onFocusCapture={activate}
        >
          {fileHeader}
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
        className="border-b border-emerald-950/60 bg-neutral-950"
        data-testid={`linked-surface:${path}`}
        data-review-surface-key={surfaceKey}
        onMouseEnter={activate}
        onFocusCapture={activate}
      >
        {fileHeader}
        <SurfacePluginPreview
          surface={surface}
          revisionId={details[path]?.revision_id ?? revisionId}
          mode={item.mode}
          run={surfaceRuns[surfaceKey]}
          running={surfaceBusy === surfaceKey}
          onRun={(compare) => onRunSurface?.(surface, compare)}
          onOpenPath={scrollToFile}
        />
      </section>
    );
  }, [activeSurfaceKey, chromeTheme, details, headerControls, onRunSurface, previewFromSurface, renderCollapsedHeader, revisionId, scrollToFile, surfaceBusy, surfaceRuns, targetsByPath, webRouteOptions]);

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
              decorateCssColorSwatches(node, item.id, phase);
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

  if (items.length === 0) {
    return <div className="flex min-h-0 flex-1 items-center justify-center text-[11px] text-neutral-600" role="status">Preparing review content…</div>;
  }

  return (
    <div
      ref={viewportRef}
      className="relative min-h-0 flex-1 bg-neutral-950"
      data-review-diff-preference={diffStyle}
      data-review-diff-style={resolvedDiffStyle}
      onPointerUpCapture={(event) => {
        lastPointerRef.current = { clientX: event.clientX, clientY: event.clientY };
      }}
    >
      <div
        ref={scrollRef}
        className="absolute inset-0 overflow-y-auto bg-neutral-950"
        data-testid="review-stream"
        onScroll={() => {
          // Incidental wheel/trackpad movement may close the action popup, but
          // the selected range remains highlighted until an explicit action,
          // outside click, Escape, or a new selection replaces it.
          if (selectionAction) dismissSelectionAction(false);
          scheduleFocusFromScroll();
        }}
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
      {selectionAction && (() => {
        const viewportPadding = 8;
        const gap = 8;
        const menuWidth = selectionMenuSize.width || 360;
        const menuHeight = selectionMenuSize.height || 36;
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
            className="fixed z-[90] flex min-h-9 w-max max-w-[calc(100vw-16px)] flex-wrap items-center gap-0.5 rounded-lg border border-neutral-700 bg-neutral-900/98 p-1 shadow-2xl backdrop-blur transition-[opacity,transform] duration-100 motion-reduce:transition-none"
            style={{
              left,
              top,
              opacity: 1,
              transform: "scale(1)",
              transformOrigin: top < selectionAction.clientY ? "bottom right" : "top left",
            }}
            onPointerDown={(event) => event.stopPropagation()}
          >
            <button
              type="button"
              role="menuitem"
              aria-keyshortcuts="c"
              onClick={openDraft}
              className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] font-medium text-neutral-100 hover:bg-neutral-800"
            >
              <MessageSquareText size={13} aria-hidden="true" />
              Comment
              <kbd aria-hidden="true" className="ml-1 font-mono text-[9px] text-neutral-600">C</kbd>
            </button>
            <button
              type="button"
              role="menuitem"
              aria-keyshortcuts="a"
              onClick={() => {
                const selected = selectionAction;
                setSelectionAction(null);
                setLineSelection(selected.path, null);
                onAskSelection(selected.draft);
              }}
              className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] text-violet-300 hover:bg-violet-950/45"
            >
              <Sparkles size={13} aria-hidden="true" />
              Ask AI
              <kbd aria-hidden="true" className="ml-1 font-mono text-[9px] text-violet-500/70">A</kbd>
            </button>
            {selectedTarget && (
              <>
                <span aria-hidden="true" className="mx-0.5 h-4 w-px bg-neutral-700" />
                <button
                  type="button"
                  role="menuitem"
                  aria-keyshortcuts="r"
                  aria-label={`Mark ${selectedTarget.symbol || selectedTarget.label} reviewed`}
                  title="Mark review area reviewed · R"
                  disabled={busy || selectedTarget.state === "reviewed"}
                  onClick={() => finishTargetAction("reviewed")}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md text-emerald-300 hover:bg-emerald-950/45 disabled:opacity-40"
                >
                  <CheckCircle2 size={13} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  role="menuitem"
                  aria-keyshortcuts="x"
                  aria-label={`Mark ${selectedTarget.symbol || selectedTarget.label} needs changes`}
                  title="Mark review area needs changes · X"
                  disabled={busy}
                  onClick={() => finishTargetAction("needs_changes")}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md text-rose-300 hover:bg-rose-950/45 disabled:opacity-40"
                >
                  <X size={13} aria-hidden="true" />
                </button>
              </>
            )}
            {additions && (
              <button
                type="button"
                role="menuitem"
                aria-keyshortcuts="s"
                onClick={() => {
                  const selected = selectionAction;
                  setSelectionAction(null);
                  onSuggestEdit(selected.draft);
                }}
                className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] text-neutral-300 hover:bg-neutral-800 hover:text-neutral-50"
              >
                <Pencil size={13} aria-hidden="true" />
                Suggest edit
                <kbd aria-hidden="true" className="ml-1 font-mono text-[9px] text-neutral-600">S</kbd>
              </button>
            )}
            {additions && sourceMutationSupported && (
              <button
                type="button"
                role="menuitem"
                aria-keyshortcuts="e"
                onClick={() => {
                  const selected = selectionAction;
                  setSelectionAction(null);
                  onEditSource(selected.draft);
                }}
                className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] text-sky-300 hover:bg-sky-950/45"
              >
                <FilePenLine size={13} aria-hidden="true" />
                Edit source
                <kbd aria-hidden="true" className="ml-1 font-mono text-[9px] text-sky-500/70">E</kbd>
              </button>
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
          className="review-horizontal-scrollbar absolute inset-x-0 bottom-0 z-40 overflow-x-auto overflow-y-hidden border-t border-neutral-800 bg-neutral-950/95"
        >
          <div aria-hidden="true" style={{ width: `calc(100% + ${horizontalScrollRange}px)`, height: 1 }} />
        </div>
      )}
    </div>
  );
});

export default ReviewStream;
