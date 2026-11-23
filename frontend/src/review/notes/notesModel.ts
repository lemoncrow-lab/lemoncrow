import { annotationSource, isUnanchoredComment } from "../annotationModel";
import type { Annotation, ReviewTarget } from "../types";

/**
 * The review-wide Notes list: every root comment and AI finding, sorted into Open, Needs re-review, Resolved and
 * Lines moved. Pure, so the top bar's count and the sheet's tabs can never disagree.
 */
export type NotesFilter = "open" | "rereview" | "resolved" | "orphaned";

export const NOTES_FILTERS: readonly { id: NotesFilter; label: string }[] = [
  { id: "open", label: "Open" },
  { id: "rereview", label: "Needs re-review" },
  { id: "resolved", label: "Resolved" },
  { id: "orphaned", label: "Lines moved" },
];

export function isAiFinding(note: Annotation): boolean {
  return annotationSource(note) === "ai_review";
}

/** A root comment or AI finding. Replies ride with their root, an obsolete note is gone, and author and LemonCrow notes are not the reviewer's work. */
export function isListedNote(note: Annotation): boolean {
  if (note.parent_id || note.state === "obsolete") return false;
  const source = annotationSource(note);
  return source === "human" || source === "ai_review";
}

/** Done with: a resolved comment, or an AI finding the reviewer dismissed or that was fixed. */
export function isSettledNote(note: Annotation): boolean {
  if (note.state === "resolved") return true;
  return isAiFinding(note) && (note.finding_disposition === "dismissed" || note.finding_disposition === "fixed");
}

/** The author (or their coding agent) says it is addressed; it stays open until the reviewer looks again. */
export function awaitsRereview(note: Annotation): boolean {
  return note.author_response === "addressed" && note.state !== "resolved" && note.state !== "obsolete";
}

/** Open holds everything not settled; Needs re-review and Lines moved are the open notes that need a particular look. */
export function inNotesFilter(note: Annotation, filter: NotesFilter): boolean {
  if (filter === "resolved") return isSettledNote(note);
  if (isSettledNote(note)) return false;
  if (filter === "rereview") return awaitsRereview(note);
  if (filter === "orphaned") return isUnanchoredComment(note);
  return true;
}

export function listedNotes(annotations: readonly Annotation[]): Annotation[] {
  return annotations.filter(isListedNote);
}

export function notesCounts(annotations: readonly Annotation[]): Record<NotesFilter, number> {
  const counts: Record<NotesFilter, number> = { open: 0, rereview: 0, resolved: 0, orphaned: 0 };
  for (const note of annotations) {
    if (!isListedNote(note)) continue;
    for (const { id } of NOTES_FILTERS) if (inNotesFilter(note, id)) counts[id] += 1;
  }
  return counts;
}

/** The top bar's control: "Notes · 3 open", or "Notes" when nothing is open. */
export function notesButtonText(open: number): string {
  return open > 0 ? `Notes · ${open} open` : "Notes";
}

/** The part a note sits in on this revision, or null when it has no line here (lines moved, a change-wide note). */
export function noteTarget(note: Annotation, targets: readonly ReviewTarget[]): ReviewTarget | null {
  if (note.comment_scope === "change" || !note.path || isUnanchoredComment(note)) return null;
  const byUnit = note.unit_key ? targets.find((target) => target.unit_key === note.unit_key && target.path === note.path) : undefined;
  if (byUnit) return byUnit;
  if (note.start_line < 1) return null;
  const side = note.side === "old" || note.side === "new" ? note.side : "";
  return targets.find((target) => target.path === note.path && target.spans.some((span) =>
    (!side || span.side === side) && span.start_line <= note.start_line && note.start_line <= span.end_line,
  )) ?? null;
}

/** "src/a.py:12-14", "src/a.py · file" or "Change discussion". */
export function noteLocation(note: Annotation): string {
  if (note.comment_scope === "change") return "Change discussion";
  if (!note.path) return "No current path";
  if (note.file_level || note.start_line < 1) return `${note.path} · file`;
  return `${note.path}:${note.start_line}${note.end_line > note.start_line ? `-${note.end_line}` : ""}`;
}
