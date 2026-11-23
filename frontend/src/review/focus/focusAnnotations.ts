/** Focus uses the Reader's persisted annotations; no temporary note counts as review evidence. */
import type { Annotation, ReviewTarget } from "../types";
import type { PartRow } from "./partDiff";

export function visibleReviewNotes(annotations: readonly Annotation[], reviewId: string, revisionId: string): Annotation[] {
  return annotations.filter((note) =>
    note.review_id === reviewId &&
    note.state !== "obsolete" &&
    note.relation_to_revision !== "future" &&
    (note.revision_id === revisionId || note.relation_to_revision === "inherited") &&
    note.parent_id === "" &&
    note.kind === "request_change" &&
    (note.source === "human" || (!note.source && note.created_by_actor !== "agent")),
  );
}

/** Only an exact target key ties a persisted note to a part; path overlap can misattribute adjacent functions. */
export function notesForPart(notes: readonly Annotation[], target: ReviewTarget | null): Annotation[] {
  if (!target) return [];
  return notes.filter((note) => note.unit_key === target.unit_key && note.path === target.path);
}

/** Human root comments painted on the part's code lines: comments as well as change requests, tied by unit key or by an anchored line inside the part. */
export function inlineNotesForPart(annotations: readonly Annotation[], reviewId: string, revisionId: string, target: ReviewTarget | null): Annotation[] {
  if (!target) return [];
  return annotations.filter((note) =>
    note.review_id === reviewId &&
    note.state !== "obsolete" &&
    note.relation_to_revision !== "future" &&
    (note.revision_id === revisionId || note.relation_to_revision === "inherited") &&
    note.parent_id === "" &&
    (note.kind === "request_change" || note.kind === "comment") &&
    (note.source === "human" || (!note.source && note.created_by_actor !== "agent")) &&
    note.path === target.path &&
    (note.unit_key === target.unit_key || (!note.file_level && note.start_line >= target.start_line && note.start_line <= target.end_line)),
  );
}
/** A note on an orphan, stale path, or unknown side is never painted beside a code line. */
/** A displayed AI finding must have exact persisted provenance for this Snapshot and unit. */
export function aiFindingsForPart(
  annotations: readonly Annotation[], reviewId: string, revisionId: string, target: ReviewTarget | null, aiAvailable: boolean,
): Annotation[] {
  if (!aiAvailable || !target) return [];
  return annotations.filter((note) =>
    note.review_id === reviewId && note.revision_id === revisionId && note.relation_to_revision !== "future" &&
    note.source === "ai_review" && note.state === "open" &&
    (note.finding_disposition === undefined || note.finding_disposition === "" || note.finding_disposition === "open") &&
    note.unit_key === target.unit_key && note.path === target.path &&
    note.anchored && note.anchor_exact && !note.file_level && note.start_line > 0 &&
    Boolean(note.body.trim()),
  );
}

/** AI coverage only counts validated new-side ranges that actually overlap this part. */
export function aiHintRanges(findings: readonly Annotation[], target: ReviewTarget): Array<{ start: number; end: number }> {
  if (target.start_line <= 0 || target.end_line < target.start_line) return [];
  return findings.filter((finding) => finding.side === "additions" || finding.side === "new").map((finding) => ({
    start: Math.max(target.start_line, finding.start_line),
    end: Math.min(target.end_line, Math.max(finding.start_line, finding.end_line)),
  })).filter((range) => range.start <= range.end);
}

export function notesOnRow(notes: readonly Annotation[], row: PartRow): Annotation[] {
  return notes.filter((note) => {
    if (!note.anchored || note.file_level || !note.anchor_exact || note.start_line < 1) return false;
    if (row.kind === "del") {
      if (note.side !== "deletions" && note.side !== "old") return false;
    } else if (note.side !== "additions" && note.side !== "new") {
      return false;
    }
    return note.start_line <= row.num && row.num <= Math.max(note.start_line, note.end_line);
  });
}
