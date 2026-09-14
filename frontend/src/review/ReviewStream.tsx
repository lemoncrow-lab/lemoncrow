import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { parsePatchFiles } from "@pierre/diffs";
import {
  CodeView,
  type CodeViewDiffItem,
  type CodeViewFileItem,
  type CodeViewHandle,
  type CodeViewItem,
} from "@pierre/diffs/react";

import AnnotationCard from "./AnnotationCard";
import type { ContextDrawerTab } from "./ContextDrawer";
import { appThemeType, motionSafeScrollBehavior, type DiffStyle } from "./diffModel";
import ReviewTargetHeader from "./ReviewTargetHeader";
import type { Draft } from "./annotationModel";
import { commentsAt, fileComments, markerLines, markerSide, markerSignature } from "./annotationModel";
import { groupReaderFiles, orderedPaths, targetScrollLocation } from "./readerModel";
import type {
  Annotation,
  AnnotationDraft,
  AnnotationKind,
  FileDetail,
  MarkState,
  ReviewTarget,
} from "./types";

interface StreamMarkerMeta {
  key: string;
  targetIds: string[];
}

type StreamItem = CodeViewItem<StreamMarkerMeta>;
type ParsedFileDiff = CodeViewDiffItem<StreamMarkerMeta>["fileDiff"];

interface ParsedDiffCacheEntry {
  patch: string;
  fileDiff: ParsedFileDiff | null;
}

interface ItemRevisionEntry {
  version: number;
  type: "diff" | "file";
  source: string;
  markers: string;
  collapsed: boolean;
}

export interface ReviewStreamHandle {
  scrollToTarget(target: ReviewTarget, behavior?: "instant" | "smooth"): void;
  scrollToFile(path: string, behavior?: "instant" | "smooth"): void;
}

interface ReviewStreamProps {
  targets: ReviewTarget[];
  details: Record<string, FileDetail | undefined>;
  errors: Record<string, string | undefined>;
  annotations: Annotation[];
  draft: Draft | null;
  diffStyle: DiffStyle;
  activeTargetId: string;
  busy: boolean;
  remainingFileCount: number;
  /**
   * How many targets each file still owes a judgment, counted over the whole
   * review and not over `targets`. The caller already keeps it for the bulk
   * "review eligible remainder" action, and it is the only unfiltered per-file
   * tally this component is handed.
   */
  outstandingByPath: ReadonlyMap<string, number>;
  onActiveTarget(targetId: string): void;
  onDraft(next: Draft | null): void;
  onCreate(draft: AnnotationDraft): void;
  onSetAnnotationState(id: string, state: "open" | "resolved"): void;
  onMark(target: ReviewTarget, state: MarkState, advance?: boolean): void;
  onComment(target: ReviewTarget): void;
  onContext(target: ReviewTarget, tab?: ContextDrawerTab): void;
  onBulkReview(path: string): void;
  onLoadMore(): void;
}

function markerKey(side: "additions" | "deletions", line: number): string {
  return `${side}:${line}`;
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
    const location = targetScrollLocation(target);
    const side = location?.side ?? "additions";
    const line = location?.lineNumber ?? 0;
    ensure(side, line).metadata.targetIds.push(target.target_id);
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
  if (error) return `LemonCrow could not load this review file.\n\n${error}`;
  if (!detail) return `LemonCrow has not loaded ${path} yet.`;
  const title = detail.refusal ? detail.refusal.replaceAll("_", " ") : "diff unavailable";
  return `LemonCrow review notice: ${title}\n\n${detail.detail || "The recorded revision did not contain enough text to render this diff honestly."}`;
}

function targetMarkerSignature(targets: readonly ReviewTarget[]): string {
  return targets.map((target) => {
    const location = targetScrollLocation(target);
    return `${target.target_id}@${location?.side ?? "additions"}:${location?.lineNumber ?? 0}`;
  }).join("|");
}

function nextItemRevision(
  cache: Map<string, ItemRevisionEntry>,
  path: string,
  type: "diff" | "file",
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

function itemFor(
  path: string,
  detail: FileDetail | undefined,
  error: string | undefined,
  targets: ReviewTarget[],
  annotations: Annotation[],
  draft: Draft | null,
  collapsed: boolean,
  diffCache: Map<string, ParsedDiffCacheEntry>,
  revisionCache: Map<string, ItemRevisionEntry>,
): StreamItem | null {
  if (!detail && !error) return null;
  if (detail?.renderable && detail.patch) {
    const fileDiff = parsedFileDiff(diffCache, path, detail.patch);
    if (fileDiff) {
      const markers = buildMarkers(path, targets, annotations, draft);
      const signature = `${markerSignature(annotations, path, draft)}#${targetMarkerSignature(targets)}`;
      const item: CodeViewDiffItem<StreamMarkerMeta> = {
        id: path,
        type: "diff",
        fileDiff,
        version: nextItemRevision(revisionCache, path, "diff", detail.patch, signature, collapsed),
        annotations: markers,
        collapsed,
      };
      return item;
    }
  }
  const contents = refusalText(detail, error, path);
  const item: CodeViewFileItem<StreamMarkerMeta> = {
    id: path,
    type: "file",
    file: { name: path, contents },
    version: nextItemRevision(revisionCache, path, "file", contents, "", collapsed),
    collapsed,
  };
  return item;
}

const ReviewStream = forwardRef<ReviewStreamHandle, ReviewStreamProps>(function ReviewStream(
  {
    targets,
    details,
    errors,
    annotations,
    draft,
    diffStyle,
    activeTargetId,
    busy,
    remainingFileCount,
    outstandingByPath,
    onActiveTarget,
    onDraft,
    onCreate,
    onSetAnnotationState,
    onMark,
    onComment,
    onContext,
    onBulkReview,
    onLoadMore,
  },
  ref,
) {
  const codeViewRef = useRef<CodeViewHandle<StreamMarkerMeta, undefined>>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const parsedDiffCacheRef = useRef(new Map<string, ParsedDiffCacheEntry>());
  const itemRevisionCacheRef = useRef(new Map<string, ItemRevisionEntry>());
  const targetsRef = useRef(targets);
  const activeTargetIdRef = useRef(activeTargetId);
  const onDraftRef = useRef(onDraft);
  targetsRef.current = targets;
  activeTargetIdRef.current = activeTargetId;
  onDraftRef.current = onDraft;
  const [collapsedPaths, setCollapsedPaths] = useState<ReadonlySet<string>>(new Set());
  const themeType = appThemeType();
  // Only the comments a reader could have written are drawn on the diff; see
  // showInlineAnnotation for why the machine's attention notes are not.
  const inlineAnnotations = useMemo(() => annotations.filter(showInlineAnnotation), [annotations]);
  const files = useMemo(() => groupReaderFiles(targets), [targets]);
  const targetsById = useMemo(() => new Map(targets.map((target) => [target.target_id, target])), [targets]);
  const targetsByPath = useMemo(() => {
    const map = new Map<string, ReviewTarget[]>();
    for (const target of targets) {
      const rows = map.get(target.path) ?? [];
      rows.push(target);
      map.set(target.path, rows);
    }
    return map;
  }, [targets]);

  const items = useMemo<StreamItem[]>(() => {
    const rows: StreamItem[] = [];
    const activePaths = new Set<string>();
    for (const path of orderedPaths(targets)) {
      activePaths.add(path);
      const item = itemFor(
        path,
        details[path],
        errors[path],
        targetsByPath.get(path) ?? [],
        inlineAnnotations,
        draft,
        collapsedPaths.has(path),
        parsedDiffCacheRef.current,
        itemRevisionCacheRef.current,
      );
      if (item) rows.push(item);
    }
    // Search/delta focus can temporarily remove files from the stream. Their
    // CodeView instances are removed too, so retaining parsed objects here only
    // wastes memory and can make a later re-add look like it belongs to the old
    // virtualized instance.
    for (const path of parsedDiffCacheRef.current.keys()) {
      if (!activePaths.has(path)) parsedDiffCacheRef.current.delete(path);
    }
    for (const path of itemRevisionCacheRef.current.keys()) {
      if (!activePaths.has(path)) itemRevisionCacheRef.current.delete(path);
    }
    return rows;
  }, [targets, details, errors, targetsByPath, inlineAnnotations, draft, collapsedPaths]);

  const focusFromScroll = useCallback(() => {
    const root = containerRef.current;
    if (!root) return;
    const nodes = [...root.querySelectorAll<HTMLElement>("[data-review-target-id]")];
    if (nodes.length === 0) return;
    const threshold = root.getBoundingClientRect().top + 115;
    let best: { id: string; distance: number } | null = null;
    for (const node of nodes) {
      const id = node.dataset.reviewTargetId ?? "";
      if (!id) continue;
      const rect = node.getBoundingClientRect();
      const distance = rect.top <= threshold ? Math.abs(threshold - rect.top) : Math.abs(rect.top - threshold) + 1000;
      if (!best || distance < best.distance) best = { id, distance };
    }
    if (best && best.id !== activeTargetId) onActiveTarget(best.id);
  }, [activeTargetId, onActiveTarget]);

  const scrollToTarget = useCallback((target: ReviewTarget, behavior: "instant" | "smooth" = "smooth") => {
    const viewer = codeViewRef.current;
    if (!viewer) return;
    const location = targetScrollLocation(target);
    const safeBehavior = motionSafeScrollBehavior(behavior);
    if (location) {
      viewer.scrollTo({
        type: "line",
        id: target.path,
        lineNumber: location.lineNumber,
        side: location.side,
        align: "start",
        offset: -52,
        behavior: safeBehavior,
      });
    } else {
      viewer.scrollTo({ type: "item", id: target.path, align: "start", offset: -52, behavior: safeBehavior });
    }
    onActiveTarget(target.target_id);
  }, [onActiveTarget]);

  useImperativeHandle(ref, () => ({
    scrollToTarget,
    scrollToFile(path: string, behavior: "instant" | "smooth" = "smooth") {
      codeViewRef.current?.scrollTo({ type: "item", id: path, align: "start", offset: -36, behavior: motionSafeScrollBehavior(behavior) });
      const first = targets.find((target) => target.path === path);
      if (first) onActiveTarget(first.target_id);
    },
  }), [scrollToTarget, targets, onActiveTarget]);

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
  }, [inlineAnnotations, draft, onCreate]);

  const options = useMemo(() => ({
    diffStyle,
    themeType,
    enableLineSelection: true,
    enableGutterUtility: true,
    lineHoverHighlight: "both" as const,
    stickyHeaders: true,
    onGutterUtilityClick: ((
      range: { start: number; end: number; side?: string },
      context?: { item?: { id?: string } },
    ) => {
      const path = context?.item?.id ?? "";
      if (!path) return;
      const side = markerSide(range.side ?? "additions");
      const startLine = Math.min(range.start, range.end);
      const target = targetAtLine(targetsRef.current, path, side, startLine, activeTargetIdRef.current);
      onDraftRef.current({
        path,
        startLine,
        endLine: Math.max(range.start, range.end),
        side,
        targetUnitKey: target?.unit_key,
      });
    }) as never,
  }), [diffStyle, themeType]);

  const fileByPath = useMemo(() => new Map(files.map((file) => [file.path, file])), [files]);

  if (items.length === 0) {
    return <div className="flex min-h-0 flex-1 items-center justify-center text-[11px] text-neutral-600">Loading review stream…</div>;
  }

  return (
    <div ref={containerRef} className="min-h-0 flex-1 overflow-hidden bg-neutral-950" data-testid="review-stream">
      <CodeView<StreamMarkerMeta, undefined>
        ref={codeViewRef}
        items={items}
        options={options}
        onScroll={() => window.requestAnimationFrame(focusFromScroll)}
        renderHeaderMetadata={(item) => {
          const file = fileByPath.get(item.id);
          if (!file) return null;
          // `file` is grouped from `targets`, which is whatever survived the
          // reader's search and delta focus. The ratio is therefore a statement
          // about the stream and says so: a bare "0/2 targets" on a file that
          // holds seven is read as the file's own tally.
          //
          // Then say what can be proved about the file itself. `outstandingByPath`
          // is counted over the whole review, so a file showing fewer unreviewed
          // targets than it owes is provably showing a slice of itself, and the
          // header names that remainder. The file's *total* is deliberately not
          // claimed: a target that is both reviewed and filtered out leaves no
          // trace in anything this component is handed, so any total computed in
          // here would be the understatement the disclosure exists to prevent.
          const unreviewedInFile = outstandingByPath.get(item.id) ?? 0;
          const hiddenUnreviewed = unreviewedInFile - (file.targetCount - file.reviewed);
          const beyondView = hiddenUnreviewed > 0 ? ` · ${unreviewedInFile} unreviewed in file` : "";
          return (
            <div className="flex items-center gap-2 pr-2 font-mono text-[10px] text-neutral-500">
              <span>{file.reviewed}/{file.targetCount} targets in view{beyondView}</span>
              {unreviewedInFile > 0 && (
                <button
                  type="button"
                  disabled={busy || hiddenUnreviewed > 0}
                  onClick={() => onBulkReview(item.id)}
                  className="text-neutral-500 hover:text-neutral-200 disabled:opacity-40"
                  aria-label={hiddenUnreviewed > 0
                    ? `Bulk review unavailable in ${item.id} while targets are hidden`
                    : `Mark eligible remainder reviewed in ${item.id}`}
                >
                  Review eligible remainder
                </button>
              )}
              <button
                type="button"
                onClick={() => setCollapsedPaths((current) => {
                  const next = new Set(current);
                  if (next.has(item.id)) next.delete(item.id);
                  else next.add(item.id);
                  return next;
                })}
                className="text-neutral-600 hover:text-neutral-300"
              >
                {collapsedPaths.has(item.id) ? "Expand" : "Collapse"}
              </button>
            </div>
          );
        }}
        renderAnnotation={(annotation, item) => {
          const metadata = annotation.metadata;
          if (!metadata) return null;
          const path = item.id;
          const targetRows = metadata.targetIds.map((id) => targetsById.get(id)).filter((row): row is ReviewTarget => Boolean(row));
          const comments = commentsAt(inlineAnnotations, path, metadata.key);
          const fileLevelRows = annotation.lineNumber === 0 ? fileComments(inlineAnnotations, path) : [];
          const shownComments = comments.length > 0 ? comments : fileLevelRows;
          const composing = Boolean(
            draft &&
            draft.path === path &&
            (draft.fileLevel ? annotation.lineNumber === 0 : `${draft.side}:${draft.startLine}` === metadata.key),
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
                  onFocus={() => onActiveTarget(target.target_id)}
                  onMark={(state, advance) => onMark(target, state, advance)}
                  onComment={() => onComment(target)}
                  onContext={(tab) => onContext(target, tab)}
                />
              ))}
              {(shownComments.length > 0 || composing) && (
                <AnnotationCard
                  comments={shownComments}
                  all={inlineAnnotations}
                  label={draft?.fileLevel ? "File comment" : "New comment"}
                  composing={composing}
                  busy={busy}
                  targetMarkAvailable={Boolean(draftTarget)}
                  onSubmit={(body, kind, parentId, markTarget) => submit(path, body, kind, parentId, markTarget, draftTarget?.unit_key)}
                  onCancel={() => onDraft(null)}
                  onResolve={(id) => onSetAnnotationState(id, "resolved")}
                  onReopen={(id) => onSetAnnotationState(id, "open")}
                />
              )}
            </div>
          );
        }}
        renderCodeViewFooter={() => remainingFileCount > 0 ? (
          <div className="flex justify-center border-t border-neutral-900 py-5">
            <button type="button" onClick={onLoadMore} className="border border-neutral-800 px-3 py-1.5 text-[10px] text-neutral-500 hover:border-neutral-600 hover:text-neutral-300">
              Load next files · {remainingFileCount} remaining
            </button>
          </div>
        ) : <div className="h-8" />}
        style={
          {
            height: "100%",
            overflowY: "auto",
            "--diffs-font-size": "13px",
            "--diffs-line-height": "21px",
          } as CSSProperties
        }
      />
    </div>
  );
});

export default ReviewStream;
