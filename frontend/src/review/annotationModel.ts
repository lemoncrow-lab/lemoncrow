/**
 * Everything the annotation UI decides, kept out of the components that draw it.
 *
 * The load-bearing rule lives here (spec §5.4): **a line number rendered by
 * `@pierre/diffs` is never read back as truth.** Markers are pushed *into* the
 * viewer from the server's anchors, and the only thing that travels in a
 * `DiffLineAnnotation`'s metadata is our own annotation id. The moment the
 * viewer's line number became the anchor of record, "what changed since I
 * reviewed?" would stop being answerable.
 *
 * The second rule is that an orphan is never drawn on a line. A comment whose
 * anchor could not be re-found still exists, still matters, and still has to be
 * read — but showing it beside a line number that no longer means anything is
 * exactly the silent relocation the whole ladder exists to refuse. Orphans get
 * their own list, with the reason attached.
 */

import type { Annotation, AnnotationKind, AnnotationSource, DiffSide } from "./types";

/** New human comments have three dispositions. `looks_good` remains render-only compatibility. */
export const KIND_ORDER: AnnotationKind[] = ["comment", "request_change", "suggestion"];

export const KIND_LABELS: Record<AnnotationKind, string> = {
  comment: "Comment",
  request_change: "Request change",
  suggestion: "Suggestion",
  looks_good: "Looks good",
};

export const SOURCE_LABELS: Record<AnnotationSource, string> = {
  human: "HUMAN",
  author: "AUTHOR",
  lemoncrow: "LEMONCROW",
  ai_review: "AI REVIEW",
};

export const SOURCE_ACCENTS: Record<AnnotationSource, string> = {
  human: "border-sky-900 text-sky-300",
  author: "border-amber-900 text-amber-300",
  lemoncrow: "border-emerald-900 text-emerald-300",
  ai_review: "border-violet-900 text-violet-300",
};

export function annotationSource(annotation: Annotation): AnnotationSource {
  return annotation.source ?? "human";
}

export function isHumanJudgment(annotation: Annotation): boolean {
  return (annotation.is_human_judgment ?? annotationSource(annotation) === "human") === true;
}

/** Tailwind accents per disposition. A request for change must not read as a note. */
export const KIND_ACCENTS: Record<AnnotationKind, string> = {
  comment: "border-sky-900 text-sky-300",
  request_change: "border-rose-900 text-rose-300",
  suggestion: "border-violet-900 text-violet-300",
  looks_good: "border-emerald-900 text-emerald-300",
};

/**
 * Tailwind accent for the line that says how a comment got where it is drawn.
 *
 * The server sends the rung's plain-language label and `anchor_exact`, and the
 * card renders them on every comment -- not only on the failures. A comment
 * re-found by its text alone, inside a function that has since been renamed and
 * re-documented, and a comment whose file never changed are two very different
 * claims; drawn with the same weight, the second one's credibility is lent to
 * the first. Plan SS4.4: *never silently move a comment to a different line
 * after a revision* -- and a move nobody is told about is a silent one whether
 * or not the line number happened to change.
 */
export function anchorAccent(annotation: Annotation): string {
  if (!annotation.anchored) return "border-amber-800 text-amber-300";
  return annotation.anchor_exact
    ? "border-neutral-800 text-neutral-500"
    : "border-amber-900/70 text-amber-300/80";
}

/** The id a draft comment carries while it has no server id yet. */
export const DRAFT_ID = "__draft__";

export interface Draft {
  path: string;
  startLine: number;
  endLine: number;
  side: DiffSide;
  fileLevel?: boolean;
  /** Current ReviewTarget when the draft was opened from target-level review. */
  targetUnitKey?: string;
  /** Unsaved human work lives here so refresh/unmount cannot destroy it. */
  body?: string;
  kind?: AnnotationKind;
  markTarget?: boolean;
  mode?: "annotation" | "proposal";
  sourceAction?: "suggest" | "edit";
  replacementText?: string;
  proposalIntent?: string;
  proposalId?: string;
  /** Anchor changed underneath this draft; the human must choose a new one. */
  recovered?: boolean;
}

/**
 * Which side of the diff a marker sits on.
 *
 * Our vocabulary is `old`/`new`, the viewer's is `deletions`/`additions`, and
 * the translation happens at the edge in both directions so neither side has to
 * learn the other's word.
 */
export function markerSide(side: string): DiffSide {
  return side === "old" || side === "deletions" ? "deletions" : "additions";
}

/** True when this comment still points at a line the reader can be shown. */
export function isAnchored(annotation: Annotation): boolean {
  return annotation.anchored;
}

/**
 * Split one file's comments into the ones that can be drawn and the ones that
 * can only be listed.
 *
 * Replies never appear on their own: a thread is read under its parent or it is
 * read out of order.
 */
export function partition(annotations: Annotation[], path: string): { anchored: Annotation[]; orphaned: Annotation[] } {
  const anchored: Annotation[] = [];
  const orphaned: Annotation[] = [];
  for (const annotation of annotations) {
    if (annotation.path !== path || annotation.parent_id || annotation.state === "obsolete") continue;
    (isAnchored(annotation) ? anchored : orphaned).push(annotation);
  }
  return { anchored, orphaned };
}

/** The replies under one comment, oldest first — the order they were written. */
export function repliesFor(annotations: Annotation[], parentId: string): Annotation[] {
  return annotations.filter((item) => item.parent_id === parentId);
}

/**
 * The line a comment card is drawn under: the last line of its range, so a
 * multi-line comment never splits the lines it talks about (GitHub convention).
 */
export function anchorLine(startLine: number, endLine: number): number {
  return Math.max(startLine, endLine);
}

/** Marker key for an in-diff comment row. */
export function draftMarkerKey(draft: Pick<Draft, "side" | "startLine" | "endLine">): string {
  return `${draft.side}:${anchorLine(draft.startLine, draft.endLine)}`;
}

function annotationMarkerKey(annotation: Annotation): string {
  return `${markerSide(annotation.side)}:${anchorLine(annotation.start_line, annotation.end_line)}`;
}

/**
 * One `DiffLineAnnotation` per anchored comment, plus the draft if there is one.
 *
 * Comments ending on the same line collapse to a single marker: `renderAnnotation`
 * is called once per entry, and two entries on one line would draw two
 * overlapping cards. The card itself renders every comment on that line.
 */
export function markerLines(
  annotations: Annotation[],
  path: string,
  draft: Draft | null,
): { side: DiffSide; lineNumber: number; metadata: { key: string } }[] {
  const seen = new Set<string>();
  const out: { side: DiffSide; lineNumber: number; metadata: { key: string } }[] = [];
  for (const annotation of partition(annotations, path).anchored) {
    if (annotation.file_level || annotation.start_line < 1) continue;
    const side = markerSide(annotation.side);
    const key = annotationMarkerKey(annotation);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ side, lineNumber: anchorLine(annotation.start_line, annotation.end_line), metadata: { key } });
  }
  if (draft && draft.path === path && !draft.fileLevel && draft.startLine > 0) {
    const key = draftMarkerKey(draft);
    if (!seen.has(key)) out.push({ side: draft.side, lineNumber: anchorLine(draft.startLine, draft.endLine), metadata: { key } });
  }
  return out.sort((a, b) => a.lineNumber - b.lineNumber || a.side.localeCompare(b.side));
}

export function fileComments(annotations: Annotation[], path: string): Annotation[] {
  return partition(annotations, path).anchored.filter((item) => item.file_level || item.start_line === 0);
}

/** The comments that belong to one marker key, in creation order. */
export function commentsAt(annotations: Annotation[], path: string, key: string): Annotation[] {
  return partition(annotations, path).anchored.filter(
    (item) => !item.file_level && item.start_line > 0 && annotationMarkerKey(item) === key,
  );
}

/**
 * A string that changes exactly when the rendered markers must change.
 *
 * `CodeView` reuses a rendered item unless its `version` number moves, so a new
 * comment would otherwise be invisible until something else forced a re-render.
 * Deriving the version from a signature — rather than bumping a counter on every
 * render — keeps the item stable when nothing about it actually changed.
 */
export function markerSignature(annotations: Annotation[], path: string, draft: Draft | null): string {
  const parts = partition(annotations, path)
    .anchored.map((item) => `${item.id}@${markerSide(item.side)}:${item.start_line}-${item.end_line}:${item.state}:${item.kind}`)
    .sort();
  const replies = annotations.filter((item) => item.parent_id && item.path === path).length;
  const drafted = draft && draft.path === path ? `${draft.side}:${draft.startLine}-${draft.endLine}` : "";
  return `${parts.join("|")}#${replies}#${drafted}`;
}

/** `L10` for one line, `L10-L20` for a range — the same spelling the CLI prints. */
export function locationLabel(startLine: number, endLine: number): string {
  if (startLine < 1) return "file";
  return endLine > startLine ? `L${startLine}-L${endLine}` : `L${startLine}`;
}
