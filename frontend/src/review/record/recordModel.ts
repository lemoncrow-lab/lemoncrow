import { plural } from "../overview/overviewModel";
import type { ReviewRecordOutcomeName, ReviewRecordSummary, ReviewRecordVerifyResult } from "../types";

/** SPEC 4.18: the sentence above a record's rows, and the footer sentence of every record surface. */
export const RECORD_INTRO = "Proof of what you reviewed, tied to this exact version. It stops being valid if the code changes.";
export const LEGAL_SENTENCE = "This is a technical signature that proves who reviewed what. It is not a legal e-signature.";
/** Wrap up, before the send: the signature is made when the verdict is recorded (SPEC 4.18). */
export const NOT_SIGNED_YET = "Not signed yet · set when you send";
export const UNSIGNED_LINE = "UNSIGNED record (no signature)";
/** P6-2: the line's head once the verdict was retracted (SPEC 7.1 P6-2 D). */
export const RETRACTED_LINE = "Retracted record";
export const RETRACT_LABEL = "Retract this verdict";

/** The newest record of a person's own decision on this exact revision (a landing or merge record is not it). */
export function latestHumanRecord(
  records: readonly ReviewRecordSummary[],
  revisionId: string,
  /** Hosted: whose decision this is (a review has more than one person's). Absent: anyone's, as before. */
  decidedBy = "",
): ReviewRecordSummary | null {
  return [...records]
    .filter((record) => record.decision_kind === "human_outcome" && record.revision_id === revisionId && (!decidedBy || record.decided_by === decidedBy))
    .sort((left, right) => right.issued_at.localeCompare(left.issued_at) || right.record_id.localeCompare(left.record_id))[0] ?? null;
}

/** "Signed record · Revision 1 · 3 decisions answered · Details" (SPEC 4.18, REC-1). */
export function recordLineText(record: ReviewRecordSummary, humanRevision: number): string {
  const head = record.retracted ? RETRACTED_LINE : record.signing_state === "signed" ? "Signed record" : UNSIGNED_LINE;
  const one = record.parts_reviewed_one_by_one;
  const parts = typeof one === "number" ? `${plural(one, "decision")} answered` : "";
  return [head, `Revision ${humanRevision}`, parts, "Details"].filter(Boolean).join(" · ");
}

/**
 * Done, when no record of the decision exists yet: say why. A failure is claimed only once the server has had its
 * time to sign (`stillSigning` false); until then the record is on its way, not missing.
 */
export function noRecordText(signingAvailable: boolean | null, stillSigning = false): string {
  if (signingAvailable === false) return "Not signed · no signing key is set up on this machine";
  if (signingAvailable === true) return stillSigning ? "Signing the record…" : "Not signed · the record could not be signed";
  return "Checking for the signed record…";
}

export type VerifyTone = "success" | "warning" | "destructive" | "neutral";

/** Valid is green; a changed or altered record is red; one that cannot be checked is amber; a retraction is neutral. */
export function verifyTone(outcome: ReviewRecordOutcomeName): VerifyTone {
  switch (outcome) {
    case "valid": return "success";
    case "code_changed":
    case "not_valid": return "destructive";
    case "key_not_allowed":
    case "unsigned": return "warning";
    case "retracted": return "neutral";
  }
}

/** The address `lc review verify` reads: the record's own file on this server. */
export function recordLink(origin: string, reviewId: string, recordId: string): string {
  return `${origin.replace(/\/$/, "")}/api/reviews/${encodeURIComponent(reviewId)}/records/${encodeURIComponent(recordId)}`;
}

export function verifyCommand(link: string): string {
  return `lc review verify ${link}`;
}

/** "Checked just now against the latest captured revision, not against this page. Revision 1 · shop @ 4e1b9c2" */
export function verifyFooter(result: ReviewRecordVerifyResult): string {
  const number = result.record.snapshot_number;
  const sha = (result.record.head_sha ?? "").slice(0, 7);
  const where = result.repository && sha ? `${result.repository} @ ${sha}` : result.repository || sha;
  const revision = typeof number === "number" ? `Revision ${number + 1}` : "";
  const subject = [revision, where].filter(Boolean).join(" · ");
  return `Checked just now against the latest captured revision, not against this page.${subject ? ` ${subject}` : ""}`;
}

const pad2 = (value: number): string => String(value).padStart(2, "0");
const utcDay = (moment: Date): number => Date.UTC(moment.getUTCFullYear(), moment.getUTCMonth(), moment.getUTCDate());

/** "14:02 UTC" for an ISO instant ("" when unreadable); minutes are cut, never rounded up. */
export function utcClock(iso: string): string {
  const moment = new Date(iso);
  return Number.isNaN(moment.getTime()) ? "" : `${pad2(moment.getUTCHours())}:${pad2(moment.getUTCMinutes())} UTC`;
}

/**
 * "until tomorrow 14:02 UTC" (SPEC 4.18): the server's end of the window, named relative to today in UTC, the one
 * clock Signed at uses. Past tomorrow it names the date. "" when the instant is unreadable.
 */
export function retractUntilText(untilIso: string, now: number): string {
  const until = new Date(untilIso);
  if (Number.isNaN(until.getTime())) return "";
  const days = Math.round((utcDay(until) - utcDay(new Date(now))) / 86_400_000);
  const when = days === 0 ? "today" : days === 1 ? "tomorrow" : `${until.getUTCFullYear()}-${pad2(until.getUTCMonth() + 1)}-${pad2(until.getUTCDate())}`;
  return `until ${when} ${utcClock(untilIso)}`;
}

/**
 * Whether to offer Retract: only when the server says it can (`retractable`) and, as a second guard against a stale
 * page, its window has not ended on this clock. The server refuses a late retraction whatever this says.
 */
export function canRetract(record: ReviewRecordSummary | null, now: number): record is ReviewRecordSummary {
  if (!record || record.retractable !== true || record.retracted) return false;
  const until = Date.parse(record.retract_until ?? "");
  return Number.isFinite(until) && now < until;
}

/** "Retract this verdict · until tomorrow 14:02 UTC" */
export function retractButtonText(record: ReviewRecordSummary, now: number): string {
  const until = retractUntilText(record.retract_until ?? "", now);
  return until ? `${RETRACT_LABEL} · ${until}` : RETRACT_LABEL;
}

/** The newest human record on this revision when it is the retracted one; a newer unretracted send replaces it. */
export function latestRetractedRecord(records: readonly ReviewRecordSummary[], revisionId: string): ReviewRecordSummary | null {
  const latest = latestHumanRecord(records, revisionId);
  return latest?.retracted ? latest : null;
}

/** Wrap up (SPEC 4.18): "You retracted the earlier record · Revision 1 · shop @ 4e1b9c2 · 15:10 UTC. Sending again signs a new one." */
export function retractedNotice(record: ReviewRecordSummary, humanRevision: number, repo: string): string {
  const sha = (record.head_sha ?? "").slice(0, 7);
  const where = repo && sha ? `${repo} @ ${sha}` : repo || sha;
  const at = utcClock(record.retracted?.at ?? "");
  return `You retracted the earlier record · ${[`Revision ${humanRevision}`, where, at].filter(Boolean).join(" · ")}. Sending again signs a new one.`;
}

/** Why the server said no, in plain words (SPEC 7.1 P6-2 F). Nothing was changed in any of these cases. */
export function retractRefusalText(error: unknown): string {
  const detail = (error as { detail?: { code?: string } } | null)?.detail;
  if (detail?.code === "window_closed") return "Can't retract · the 24 hours from signing have passed. Nothing was changed.";
  if (detail?.code === "different_key") return "Can't retract · the key that signed this record is not the key set up now. Nothing was changed.";
  return "Couldn't retract. Nothing was changed.";
}
