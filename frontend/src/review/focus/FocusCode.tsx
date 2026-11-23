import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { parsePatchFiles } from "@pierre/diffs";
import { FileDiff, type SelectedLineRange } from "@pierre/diffs/react";
import { aaPierreTheme } from "./aaCodeTheme";

import AnnotationCard from "../AnnotationCard";
import { useCodeView } from "../codeView";
import { resolveDiffStyle } from "../diffModel";
import { fetchFile, fetchFileContents } from "../reviewApi";
import { reviewCodeThemeDefinition } from "../reviewCodeTheme";
import type { Annotation, AnnotationKind, AnnotationState, FileDetail, ReviewTarget } from "../types";
import { fullFunctionItems, itemsToPatch, parsePatchRows, partItems } from "./partDiff";
import { patchOldLineNumbers } from "./splitDiff";
import { readFlowTimings } from "../flowTimings";
import { blocksForTarget, fractionVisible, MIN_VISIBLE_FRACTION, type SeenBlock } from "../seen/seenModel";

type Side = "additions" | "deletions";

/** A line (or line range) comment written in the inline composer, the Classic Reader's way. */
export interface LineComment {
  start: number;
  end: number;
  side: Side;
  body: string;
  kind: AnnotationKind;
  parentId: string;
  markTarget: boolean;
}

interface InlineDraft { start: number; end: number; side: Side; body: string; kind: AnnotationKind; markTarget: boolean }
type Marker = { key: string };

const diffSurfaceStyle = { width: "100%", "--diffs-font-size": "13px", "--diffs-line-height": "21px" } as CSSProperties;

function sideOf(value: string): Side {
  return value === "deletions" || value === "old" ? "deletions" : "additions";
}

/** The content rows Pierre drew (in its shadow root), keyed `old|new:line`, for the seen rule and line jumps. */
/** Bars pinned over the page: rows under them are not exposed. */
const STICKY_TOP = '[data-testid="flow-top-bar"]';
const STICKY_BOTTOM = '[data-testid="all-files-tray"], [data-testid="quick-tray"], [data-testid="focus-phone-dock"], [data-testid="all-files-phone-dock"]';

/** The part of the viewport the reader can see: the window scrolls, under the sticky top bar and above any bottom bar. */
function readingArea(): { top: number; bottom: number } {
  const height = window.innerHeight;
  let top = 0;
  const bar = document.querySelector(STICKY_TOP)?.getBoundingClientRect();
  if (bar && bar.height > 0) top = Math.max(top, bar.bottom);
  let bottom = height;
  document.querySelectorAll(STICKY_BOTTOM).forEach((node) => {
    const rect = node.getBoundingClientRect();
    // Only a bar that is pinned to the viewport's bottom edge covers code; one still in the page flow sits below it.
    if (rect.height > 0 && rect.bottom >= height - 1 && rect.top < bottom) bottom = rect.top;
  });
  return { top, bottom: Math.max(top, bottom) };
}

function renderedRows(root: HTMLElement): Map<string, HTMLElement> {
  const rows = new Map<string, HTMLElement>();
  const hosts = [root, ...root.querySelectorAll<HTMLElement>("*")].filter((node) => node.shadowRoot);
  const scopes: ParentNode[] = [root, ...hosts.map((host) => host.shadowRoot as ShadowRoot)];
  for (const scope of scopes) {
    for (const row of scope.querySelectorAll<HTMLElement>("[data-line][data-line-type]")) {
      const type = row.getAttribute("data-line-type");
      const side = type === "change-deletion" ? "old" : type === "change-addition" ? "new" : "";
      if (side) rows.set(`${side}:${row.getAttribute("data-line")}`, row);
      else rows.set(`ctx:${row.getAttribute("data-line")}`, row);
    }
  }
  return rows;
}

/** Scroll a drawn code line (new side first) into view inside `container`. Returns false when it is not drawn. */
export function scrollToCodeLine(container: HTMLElement | null, line: number): boolean {
  if (!container) return false;
  const rows = renderedRows(container);
  const row = rows.get(`new:${line}`) ?? rows.get(`ctx:${line}`) ?? rows.get(`old:${line}`);
  row?.scrollIntoView?.({ block: "center" });
  return Boolean(row);
}

/** Read only the frozen Review patch; never read a live checkout for evidence. */
export function FocusCode({ reviewRef, revisionId, target, notes = [], aiFindings = [], preferWhole = true, onComment, onAnnotationState, onSeen }: {
  reviewRef: string;
  revisionId: string;
  target: ReviewTarget;
  notes?: readonly Annotation[];
  aiFindings?: readonly Annotation[];
  preferWhole?: boolean;
  /** Comment on a line or a dragged range: the gutter + or the line numbers open the inline composer. */
  onComment?: (comment: LineComment) => Promise<boolean | "queued">;
  onAnnotationState?: (id: string, state: AnnotationState) => void;
  onSeen?: (block: SeenBlock, elapsedMs: number) => void;
}) {
  const view = useCodeView();
  const theme = reviewCodeThemeDefinition(view.codeTheme);
  const codeRoot = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [onscreen, setOnscreen] = useState(false);
  const notified = useRef(new Set<string>());
  const onSeenRef = useRef(onSeen);
  onSeenRef.current = onSeen;
  const [result, setResult] = useState<{ key: string; detail: FileDetail | null; error: string } | null>(null);
  const [source, setSource] = useState<{ key: string; content: string | null } | null>(null);
  const [draft, setDraft] = useState<InlineDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [commentError, setCommentError] = useState("");
  const key = `${reviewRef}\u0000${revisionId}\u0000${target.path}`;
  useEffect(() => {
    let live = true;
    setResult(null);
    setSource(null);
    setDraft(null);
    fetchFile(reviewRef, target.path, revisionId).then((detail) => {
      if (!live) return;
      if (detail.revision_id !== revisionId) {
        setResult({ key, detail: null, error: "Code changed while loading. Reload the review before deciding." });
        return;
      }
      setResult({ key, detail, error: "" });
      if (detail.renderable) {
        fetchFileContents(reviewRef, target.path, revisionId).then((contents) => {
          if (live && contents?.revision_id === revisionId && contents.path === target.path) {
            setSource({ key, content: contents.new_content });
          }
        }).catch(() => { /* The frozen patch remains valid when full content is unavailable. */ });
      }
    }).catch(() => {
      if (live) setResult({ key, detail: null, error: "Couldn't load the code for this part." });
    });
    return () => { live = false; };
  }, [reviewRef, target.path, revisionId, key]);
  const detail = result?.key === key ? result.detail : null;
  const viewItems = useMemo(() => {
    if (!detail?.renderable || !detail.patch) return { items: [], full: false };
    const patchRows = parsePatchRows(detail.patch);
    const complete = preferWhole && source?.key === key
      ? fullFunctionItems(patchRows, target.start_line, target.end_line, source.content)
      : null;
    return { items: complete ?? partItems(patchRows, target.start_line, target.end_line), full: complete !== null };
  }, [detail, key, source, target.start_line, target.end_line, preferWhole]);
  const { items } = viewItems;
  const fileDiff = useMemo(() => {
    if (!items.some((item) => item.type === "row")) return null;
    try {
      return parsePatchFiles(itemsToPatch(target.path, items, patchOldLineNumbers(detail?.patch ?? "")), `lc:focus:${target.path}`, false)[0]?.files[0] ?? null;
    } catch {
      return null;
    }
  }, [items, target.path, detail?.patch]);

  useEffect(() => {
    const node = codeRoot.current;
    if (!node) return;
    setWidth(node.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setWidth(node.clientWidth));
    observer.observe(node);
    return () => observer.disconnect();
  }, [Boolean(fileDiff)]);
  const diffStyle = resolveDiffStyle(view.diffStyle, width);
  const reportShown = view.setShown;
  useEffect(() => { if (width > 0) reportShown(diffStyle); }, [diffStyle, width, reportShown]);

  // Comments drawn on their line (the Classic Reader's inline display): root notes and AI findings, on their last line.
  const inline = useMemo(() => [...notes, ...aiFindings].filter((note) => !note.parent_id && note.anchored !== false && !note.file_level && note.start_line > 0), [notes, aiFindings]);
  const all = useMemo(() => [...notes, ...aiFindings], [notes, aiFindings]);
  const markerKey = (side: Side, line: number) => `${side}:${line}`;
  const commentsAt = useCallback((markKey: string) => inline.filter((note) => markerKey(sideOf(note.side), Math.max(note.start_line, note.end_line)) === markKey), [inline]);
  const lineAnnotations = useMemo(() => {
    const keys = new Map<string, { side: Side; lineNumber: number; metadata: Marker }>();
    for (const note of inline) {
      const side = sideOf(note.side);
      const line = Math.max(note.start_line, note.end_line);
      keys.set(markerKey(side, line), { side, lineNumber: line, metadata: { key: markerKey(side, line) } });
    }
    if (draft) keys.set(markerKey(draft.side, draft.end), { side: draft.side, lineNumber: draft.end, metadata: { key: markerKey(draft.side, draft.end) } });
    return [...keys.values()];
  }, [inline, draft]);

  const openRange = useCallback((range: SelectedLineRange | null) => {
    if (!range || !onComment) return;
    const side = sideOf(range.side ?? "additions");
    // One composer belongs to one side of the diff: a cross-column drag is not a range the reviewer can comment on.
    if (sideOf(range.endSide ?? range.side ?? "additions") !== side) return;
    const start = Math.min(range.start, range.end);
    const end = Math.max(range.start, range.end);
    setCommentError("");
    setDraft((before) => before && before.start === start && before.end === end && before.side === side
      ? before
      : { start, end, side, body: before?.body ?? "", kind: before?.kind ?? "comment", markTarget: before?.markTarget ?? true });
  }, [onComment]);
  const openRangeRef = useRef(openRange);
  openRangeRef.current = openRange;
  const commentable = Boolean(onComment);
  const options = useMemo(() => ({
    diffStyle,
    overflow: view.overflow,
    theme: aaPierreTheme(theme.pierreTheme),
    themeType: theme.chromeTheme,
    hunkSeparators: "line-info" as const,
    disableFileHeader: true,
    enableLineSelection: commentable,
    enableGutterUtility: commentable,
    lineHoverHighlight: "both" as const,
    onGutterUtilityClick: (range: SelectedLineRange | null) => openRangeRef.current(range),
    onLineSelected: (range: SelectedLineRange | null) => openRangeRef.current(range),
  }), [diffStyle, view.overflow, theme.pierreTheme, theme.chromeTheme, commentable]);

  const submit = useCallback(async (body: string, kind: AnnotationKind, parentId: string, markTarget: boolean, anchor: { start: number; end: number; side: Side }) => {
    if (!onComment || !body.trim()) return;
    setBusy(true);
    setCommentError("");
    try {
      const ok = await onComment({ ...anchor, body: body.trim(), kind, parentId, markTarget });
      if (ok === false) { setCommentError("The comment was not saved. Your text is kept; try again."); return; }
      if (!parentId) setDraft(null);
      if (ok === "queued") setCommentError("The comment was queued for hosted review. It has not been applied yet.");
    } catch {
      setCommentError("The comment was not saved. Your text is kept; try again.");
    } finally {
      setBusy(false);
    }
  }, [onComment]);

  const renderAnnotation = (annotation: { side: Side; lineNumber: number; metadata?: Marker }) => {
    const markKey = annotation.metadata?.key ?? markerKey(annotation.side, annotation.lineNumber);
    const comments = commentsAt(markKey);
    const composing = Boolean(draft && markerKey(draft.side, draft.end) === markKey);
    if (!comments.length && !composing) return null;
    const anchorOf = (comment?: Annotation) => comment
      ? { start: comment.start_line, end: Math.max(comment.start_line, comment.end_line), side: sideOf(comment.side) }
      : draft ? { start: draft.start, end: draft.end, side: draft.side } : { start: annotation.lineNumber, end: annotation.lineNumber, side: annotation.side };
    return (
      <div className="review-inline-annotation-stack space-y-1" data-testid="focus-inline-comment">
        <AnnotationCard
          comments={comments}
          all={all}
          label={draft && composing ? `New comment · ${draft.end > draft.start ? `L${draft.start}–${draft.end}` : `L${draft.start}`}` : "New comment"}
          composing={composing}
          draftFocusKey={draft && composing ? `${draft.side}:${draft.start}:${draft.end}` : ""}
          busy={busy}
          targetMarkAvailable
          draftBody={composing ? draft?.body : ""}
          draftKind={composing ? draft?.kind : undefined}
          draftMarkTarget={composing ? draft?.markTarget : undefined}
          onDraftChange={(fields) => setDraft((before) => before ? { ...before, ...fields } : before)}
          onSubmit={(body, kind, parentId, markTarget) => void submit(body, kind, parentId, markTarget, anchorOf(parentId ? comments.find((item) => item.id === parentId) ?? comments[0] : undefined))}
          onCancel={() => setDraft(null)}
          onResolve={(id) => onAnnotationState?.(id, "resolved")}
          onReopen={(id) => onAnnotationState?.(id, "open")}
        />
      </div>
    );
  };

  // D7: require at least 60% of *all* changed line rows in one frozen block
  // to be visibly exposed continuously for the time threshold. Hidden tabs,
  // unrendered lines, partial source and scroll clipping never earn exposure.
  useEffect(() => {
    if (!onSeen || !codeRoot.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => setOnscreen(entries.some((entry) => entry.isIntersecting)));
    observer.observe(codeRoot.current);
    return () => { observer.disconnect(); setOnscreen(false); };
  }, [Boolean(onSeen), revisionId, target.unit_key, detail?.renderable, items.length]);

  useEffect(() => { notified.current.clear(); }, [revisionId, target.unit_key]);
  useEffect(() => {
    if (!onSeen || !onscreen || !detail?.renderable || !detail.patch || !codeRoot.current) return;
    const blocks = blocksForTarget(target, readFlowTimings(window));
    const since = new Map<string, number>();
    const tick = () => {
      const root = codeRoot.current;
      if (!root || document.visibilityState !== "visible" || !document.hasFocus()) {
        since.clear();
        return;
      }
      const { top, bottom } = readingArea();
      const byLine = renderedRows(root);
      const now = performance.now();
      for (const block of blocks) {
        if (notified.current.has(block.key)) continue;
        // A row that is not rendered still counts in the denominator, as a zero-height rect: it never earns exposure.
        const rects: { top: number; bottom: number; height: number }[] = [];
        for (let line = block.start; line <= block.end; line++) {
          const row = byLine.get(`${block.side}:${line}`);
          rects.push(row ? row.getBoundingClientRect() : { top: 0, bottom: 0, height: 0 });
        }
        if (fractionVisible(rects, top, bottom) < MIN_VISIBLE_FRACTION) { since.delete(block.key); continue; }
        if (!since.has(block.key)) since.set(block.key, now);
        const elapsed = now - (since.get(block.key) ?? now);
        if (elapsed >= block.thresholdMs) {
          notified.current.add(block.key);
          onSeenRef.current?.(block, Math.max(elapsed, block.thresholdMs));
        }
      }
    };
    tick();
    const interval = window.setInterval(tick, 100);
    return () => window.clearInterval(interval);
  }, [Boolean(onSeen), onscreen, detail?.renderable, detail?.patch, revisionId, target.unit_key, target.path, diffStyle, viewItems.full]);

  if (!result || result.key !== key) return <p role="status" className="py-4 text-label text-muted-foreground">Loading frozen code…</p>;
  if (result.error) return <p role="alert" className="py-4 text-label text-p1">{result.error}</p>;
  if (!detail?.renderable || !detail.patch || items.length === 0 || !fileDiff) {
    return <p className="py-4 text-label text-muted-foreground">{detail?.refusal || "No renderable code lines were returned for this part."}</p>;
  }
  return (
    <div ref={codeRoot} className="min-w-0">
      <div className="mb-2 flex flex-wrap gap-x-1 text-meta text-muted-foreground"><p>{viewItems.full ? "Whole function from the frozen revision" : "Frozen patch · unchanged source may be omitted"}</p>{commentable && <p className="max-sm:hidden">· hover a line and press + (or drag line numbers) to comment</p>}</div>
      {commentError && <p role="alert" className="mb-2 text-meta text-p1">{commentError}</p>}
      <div className="max-w-full overflow-hidden rounded-lg border border-border bg-card" role="region" aria-label={`Code evidence for ${target.symbol || target.label}`} tabIndex={0} data-testid="focus-code" data-diff-style={diffStyle} data-diff-overflow={view.overflow}>
        <FileDiff<Marker, undefined>
          fileDiff={fileDiff}
          options={options}
          lineAnnotations={lineAnnotations}
          selectedLines={draft ? { start: draft.start, end: draft.end, side: draft.side, endSide: draft.side } : undefined}
          renderAnnotation={(annotation) => renderAnnotation(annotation as { side: Side; lineNumber: number; metadata?: Marker })}
          style={diffSurfaceStyle}
        />
      </div>
    </div>
  );
}
