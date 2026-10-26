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
import type { BadgeTone } from "../components/ui/badge";
import type { HunkRail } from "../components/ui/hunk";

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

/**
 * Who is making the claim is attribution, not severity: sources are neutral chips. Only AI and agent
 * authorship takes the `ai` (violet) tone, and only in these chips; red, amber and cyan stay reserved
 * for P0, P1 and P2 and the human kinds below.
 */
export const SOURCE_BADGE_TONES: Record<AnnotationSource, BadgeTone> = {
  human: "neutral",
  author: "ai",
  lemoncrow: "neutral",
  ai_review: "ai",
};

/** Text colour for a source label shown outside a badge (lists, replies). */
export const SOURCE_ACCENTS: Record<AnnotationSource, string> = {
  human: "text-muted-foreground",
  author: "text-ai",
  lemoncrow: "text-muted-foreground",
  ai_review: "text-ai",
};

export function annotationSource(annotation: Annotation): AnnotationSource {
  return annotation.source ?? "human";
}

export function annotationIdentityColor(annotation: Annotation): string {
  return annotation.created_by_actor === "agent" ? "text-ai" : SOURCE_ACCENTS[annotationSource(annotation)];
}

/** Badge tone for the source chip of one annotation: AI and agent authorship is `ai`, everything else neutral. */
export function annotationSourceTone(annotation: Annotation): BadgeTone {
  return annotation.created_by_actor === "agent" ? "ai" : SOURCE_BADGE_TONES[annotationSource(annotation)];
}

export function annotationIdentityLabel(annotation: Annotation): string {
  const source = annotationSource(annotation);
  return (source === "author" || source === "ai_review") && annotation.agent_role
    ? `Agent(${annotation.agent_role})`
    : SOURCE_LABELS[source];
}

export function annotationProducerDetail(annotation: Annotation): string {
  const hosts: Record<string, string> = { claude: "Claude Code", codex: "Codex", opencode: "OpenCode" };
  return annotation.agent_host
    ? [hosts[annotation.agent_host] || annotation.agent_host, annotation.agent_model].filter(Boolean).join(" · ")
    : annotation.source_id || annotation.created_by;
}

export function annotationProducerTitle(annotation: Annotation): string {
  return [
    annotation.source_id && `Session: ${annotation.source_id}`,
    annotation.parent_session_id && `Parent session: ${annotation.parent_session_id}`,
    annotation.source_message_id && `Message: ${annotation.source_message_id}`,
  ].filter(Boolean).join("\n");
}

export function isUnanchoredComment(annotation: Annotation): boolean {
  return annotation.comment_scope !== "change" && (annotation.state === "orphaned" || !annotation.anchored);
}

export function isHumanJudgment(annotation: Annotation): boolean {
  return (annotation.is_human_judgment ?? annotationSource(annotation) === "human") === true;
}

/**
 * Tailwind accents per disposition (the composer's kind toggle). A request for change must not read as
 * a note: request_change is P0 red, suggestion is P2 cyan, a plain comment is neutral.
 */
export const KIND_ACCENTS: Record<AnnotationKind, string> = {
  comment: "border-border bg-muted text-foreground",
  request_change: "border-p0-line bg-p0-bg text-p0",
  suggestion: "border-p2-line bg-p2-bg text-p2",
  looks_good: "border-ok-line bg-ok-bg text-ok",
};

/** The same dispositions as `Badge` tones. */
export const KIND_BADGE_TONES: Record<AnnotationKind, BadgeTone> = {
  comment: "neutral",
  request_change: "p0",
  suggestion: "p2",
  looks_good: "success",
};

/** Severity of an AI finding, as the annotation wire carries it. */
export type FindingSeverityLevel = NonNullable<Annotation["severity"]>;

export const SEVERITY_LABELS: Record<FindingSeverityLevel, string> = {
  p0: "P0",
  p1: "P1",
  p2: "P2",
};

/** Badge tone for a severity: the severity tones are named after the levels. */
export function severityTone(severity: FindingSeverityLevel): BadgeTone {
  return severity;
}

/** The severity an AI finding carries, if the server gave it one. A reply never has one. */
export function annotationSeverity(annotation: Annotation): FindingSeverityLevel | null {
  return annotation.source === "ai_review" && !annotation.parent_id ? annotation.severity ?? null : null;
}

/**
 * The 3px gutter rail of a thread card. It carries state, never decoration, and is always redundant with
 * a text badge: AI findings by severity, human comments by kind (request_change red, suggestion cyan,
 * a plain comment neutral).
 */
export function annotationRail(annotation: Annotation): HunkRail {
  if (annotation.source === "ai_review") {
    return annotationSeverity(annotation) ?? "neutral";
  }
  const byKind: Record<AnnotationKind, HunkRail> = { comment: "neutral", request_change: "p0", suggestion: "p2", looks_good: "ok" };
  return byKind[annotation.kind];
}

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
  if (!annotation.anchored) return "border-p1-line bg-p1-bg text-p1";
  return annotation.anchor_exact
    ? "border-border text-muted-foreground"
    : "border-p1-line text-p1";
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
  sourceAction?: "suggest" | "edit" | "quick";
  replacementText?: string;
  /** New-side source already loaded to render the selected range. */
  selectedText?: string;
  selectedRevisionId?: string;
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
 * Where the viewer can draw a card for a range: a rendered line of that range,
 * or 0 when the viewer renders none of it. Never a line outside the range.
 */
export type MarkerDisplayLine = (side: DiffSide, startLine: number, endLine: number) => number;

/**
 * One `DiffLineAnnotation` per anchored comment, plus the draft if there is one.
 *
 * Comments ending on the same line collapse to a single marker: `renderAnnotation`
 * is called once per entry, and two entries on one line would draw two
 * overlapping cards. The card itself renders every comment on that line.
 *
 * `displayLine` lets the viewer draw a card whose last line is hidden (collapsed
 * unchanged context) under the last line of its range it does render. Only the
 * card moves: `metadata.key` stays the comment's own anchor.
 *
 * A finding below the finding threshold (`below_threshold`) gets no marker: a
 * marker is surfacing, and the threshold decides what is surfaced. It stays
 * readable behind the "N below threshold" disclosure of the lists, and inside
 * the card of a surfaced comment that shares its line.
 */
export function markerLines(
  annotations: Annotation[],
  path: string,
  draft: Draft | null,
  displayLine?: MarkerDisplayLine,
): { side: DiffSide; lineNumber: number; metadata: { key: string } }[] {
  const groups = new Map<string, { side: DiffSide; startLine: number; endLine: number }>();
  const add = (key: string, side: DiffSide, startLine: number, endLine: number) => {
    const group = groups.get(key);
    if (group) group.startLine = Math.max(group.startLine, Math.min(startLine, endLine));
    else groups.set(key, { side, startLine: Math.min(startLine, endLine), endLine: anchorLine(startLine, endLine) });
  };
  for (const annotation of partition(annotations, path).anchored) {
    if (annotation.file_level || annotation.start_line < 1 || annotation.below_threshold) continue;
    add(annotationMarkerKey(annotation), markerSide(annotation.side), annotation.start_line, annotation.end_line);
  }
  if (draft && draft.path === path && !draft.fileLevel && draft.startLine > 0) {
    add(draftMarkerKey(draft), draft.side, draft.startLine, draft.endLine);
  }
  const out = [...groups].map(([key, group]) => ({
    side: group.side,
    // A card shared by several ranges stays inside every one of them.
    lineNumber: displayLine?.(group.side, group.startLine, group.endLine) || group.endLine,
    metadata: { key },
  }));
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
    .anchored.map((item) => `${item.id}@${markerSide(item.side)}:${item.start_line}-${item.end_line}:${item.state}:${item.kind}${item.below_threshold ? ":below" : ""}`)
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
