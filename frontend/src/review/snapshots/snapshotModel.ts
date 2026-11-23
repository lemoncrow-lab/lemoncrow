import type { BadgeTone } from "../../components/ui/badge";
import { annotationSource } from "../annotationModel";
import { revisionName } from "../revisionLabel";
import type { Annotation, RevisionInfo } from "../types";

/**
 * The revision menu's rules, pure: newest first, which row is the newest saved one and which the running draft, the
 * tags each row carries, its parts change, its comment counts and the default pair to compare.
 */

export interface SnapshotTag { text: string; tone: BadgeTone }
export interface SnapshotCommentCount { open: number; resolved: number }

export function newestFirst(revisions: readonly RevisionInfo[]): RevisionInfo[] {
  return [...revisions].sort((a, b) => b.revision_number - a.revision_number);
}

function unpublishedDraft(row: RevisionInfo): boolean {
  return row.capture_kind === "draft" && !row.is_published_snapshot;
}

/** The newest row that is not an unpublished draft. `rows` are newest first. */
export function newestSaved(rows: readonly RevisionInfo[]): RevisionInfo | null {
  return rows.find((row) => !unpublishedDraft(row)) ?? null;
}

/** The latest row when it is an unpublished draft (working changes nobody saved yet). */
export function runningDraft(rows: readonly RevisionInfo[]): RevisionInfo | null {
  const latest = rows[0];
  return latest && unpublishedDraft(latest) ? latest : null;
}

/** "Revision 3", or "Draft revision 4" for working changes. */
export function snapshotName(row: RevisionInfo): string {
  return unpublishedDraft(row) ? `Draft revision ${row.revision_number + 1}` : revisionName(row.revision_number);
}

export function snapshotTags(row: RevisionInfo, rows: readonly RevisionInfo[], viewing: number): SnapshotTag[] {
  const tags: SnapshotTag[] = [];
  if (row.revision_number === viewing) tags.push({ text: "Viewing", tone: "secondary" });
  if (row.id === newestSaved(rows)?.id) tags.push({ text: "Newest saved", tone: "info" });
  if (row.id === runningDraft(rows)?.id) tags.push({ text: "Running draft", tone: "neutral" });
  if (row.is_last_reviewed) tags.push({ text: "Last reviewed", tone: "success" });
  if (row.is_last_commented) tags.push({ text: "Latest feedback", tone: "neutral" });
  else if (row.has_review_feedback) tags.push({ text: "Has feedback", tone: "neutral" });
  if (row.capture_kind === "commit" && row.commit_state === "superseded") tags.push({ text: "Rewritten", tone: "warning" });
  return tags;
}

/** "5 initial parts", "Δ +1 ~2 −0 parts" or "5 parts"; empty when the server sent no count. */
export function partsText(row: RevisionInfo): string {
  const count = row.review_area_count;
  if (count == null) return "";
  if (row.previous_revision_number == null) return `${count} initial part${count === 1 ? "" : "s"}`;
  const delta = row.review_area_delta;
  if (delta) return `Δ +${delta.added} ~${delta.changed} −${delta.removed} parts`;
  return `${count} part${count === 1 ? "" : "s"}`;
}

/** "Base moved · parts unchanged" when only the base moved; empty when it did not. */
export function baseMovedText(row: RevisionInfo): string {
  if (!row.base_moved) return "";
  if (row.semantic_changed === false) return "Base moved · parts unchanged";
  if (row.semantic_changed === true) return "Base moved · parts also changed";
  return "Base moved";
}

/** "Commit 3 · 1a2b3c4d" for a commit capture; empty otherwise. */
export function commitText(row: RevisionInfo): string {
  if (row.capture_kind !== "commit") return "";
  const number = row.commit_number != null ? `Commit ${row.commit_number}` : "Commit";
  return row.commit_sha ? `${number} · ${row.commit_sha.slice(0, 8)}` : number;
}

/**
 * Reviewer comments written on each revision, open and resolved, by revision number. The server sends no per-revision
 * count, so this reads the review's comment list: human root comments only (AI, author and LemonCrow notes are not
 * reviewer work, the server's own counting rule), each counted once on the revision it was written on.
 */
export function commentCounts(annotations: readonly Annotation[], rows: readonly RevisionInfo[]): Map<number, SnapshotCommentCount> {
  const numberById = new Map(rows.map((row) => [row.id, row.revision_number]));
  const out = new Map<number, SnapshotCommentCount>();
  for (const note of annotations) {
    if (note.parent_id || note.state === "obsolete" || annotationSource(note) !== "human") continue;
    const number = note.origin_revision_number ?? numberById.get(note.revision_id);
    if (number == null) continue;
    const count = out.get(number) ?? { open: 0, resolved: 0 };
    if (note.state === "resolved") count.resolved += 1; else count.open += 1;
    out.set(number, count);
  }
  return out;
}

export function commentCountText(count: SnapshotCommentCount | undefined): string {
  if (!count || count.open + count.resolved === 0) return "No comments";
  return `${count.open} open · ${count.resolved} resolved`;
}

/** The default From: the newest earlier revision that got feedback, else the latest itself (the reviewer then picks). */
export function defaultCompareFrom(rows: readonly RevisionInfo[]): number | null {
  const latest = rows[0];
  if (!latest) return null;
  return rows.find((row) => (row.has_review_feedback || row.is_last_commented) && row.revision_number < latest.revision_number)?.revision_number
    ?? rows[1]?.revision_number
    ?? latest.revision_number;
}

/** The `rr/<id>` a Compare address names. */
export function snapshotSpec(row: RevisionInfo): string {
  return row.ref?.startsWith("rr/") ? row.ref : `rr/${row.id}`;
}
