/**
 * The Home queue's rules (SPEC 4.1), pure: which section a review belongs in, what a row says, how the list is sorted,
 * filtered and counted, and when a snooze ends. Nothing here reads the DOM, storage or the network.
 *
 * Every section comes from a field the server sends for this viewer. "Sent by auto-approvers" has no field yet (no bot
 * verdict is stored), so it is not built: SPEC 7.1 P5-1 H.
 */

import { fmtWhen } from "../../lib/format";
import { plural } from "../overview/overviewModel";
import { revisionName } from "../revisionLabel";
import type { ReviewDirectoryRow, ReviewResume } from "../types";
import { rangeText, sumRanges, timeRange, type MinuteRange } from "./timeEstimate";

export type HomeSectionId = "secondSignature" | "policyApproval" | "yourTurn" | "cameBack" | "waiting" | "snoozed" | "team" | "done" | "finished";

export const SECTION_TITLE: Readonly<Record<HomeSectionId, string>> = {
  secondSignature: "Needs your second signature",
  policyApproval: "Policy changes only a CODEOWNER can approve",
  yourTurn: "Your turn",
  cameBack: "Came back to you",
  waiting: "Waiting on others",
  snoozed: "Snoozed",
  team: "Your team's reviews",
  done: "Done",
  finished: "Finished",
};

/** The order the sections are drawn in (SPEC 4.1). */
export const SECTION_ORDER: readonly HomeSectionId[] = ["secondSignature", "policyApproval", "yourTurn", "cameBack", "waiting", "snoozed", "team", "done", "finished"];

/** Reviews past this count switch the list to the dense one (SPEC 4.1). */
export const DENSE_ABOVE = 5;

// ------------------------------------------------------------------ snooze

export type SnoozeOption = 1 | 2 | 3 | 4;

export const SNOOZE_CHOICES: readonly { option: SnoozeOption; label: string }[] = [
  { option: 1, label: "1 hour" },
  { option: 2, label: "This afternoon" },
  { option: 3, label: "Tomorrow 9:00" },
  { option: 4, label: "Next week" },
];

function atHour(base: Date, dayOffset: number, hour: number): Date {
  const date = new Date(base);
  date.setDate(date.getDate() + dayOffset);
  date.setHours(hour, 0, 0, 0);
  return date;
}

/**
 * When a snooze option ends, in the viewer's own time zone. "This afternoon" is 14:00 when it is before 13:00, else four
 * hours from now; "Next week" is the coming Monday at 9:00 (a Monday snoozes to the next one).
 */
export function snoozeUntil(option: SnoozeOption, now: Date): Date {
  switch (option) {
    case 1:
      return new Date(now.getTime() + 3_600_000);
    case 2:
      return now.getHours() < 13 ? atHour(now, 0, 14) : new Date(now.getTime() + 4 * 3_600_000);
    case 3:
      return atHour(now, 1, 9);
    case 4:
      return atHour(now, ((8 - now.getDay()) % 7) || 7, 9);
  }
}

/** The shared date wording: "today 14:00", "tomorrow 09:00" or "Mon 09:00" ("Snoozed until tomorrow 09:00"). */
export function untilText(until: Date, now: Date): string {
  return fmtWhen(until, now);
}

/** Whether the row's snooze still holds at `now`. The server has already dropped it when a newer Snapshot arrived. */
export function isSnoozed(row: Pick<ReviewDirectoryRow, "queue">, now: number): boolean {
  const until = Date.parse(row.queue?.snoozed_until ?? "");
  return Number.isFinite(until) && until > now;
}

// ----------------------------------------------------------------- sections

export interface HomeOptions {
  /**
   * Hosted only: the local server has no teams. It also marks the hosted Inbox, which can list every review in the
   * organization: there a review is Waiting on others or Finished only when the viewer has a part in it.
   */
  teamsSupported?: boolean;
  /**
   * Hosted only (SPEC 4.19, 7.1 P9-5 F): the ids of reviews that edit the review policy and that the viewer may approve
   * (a CODEOWNER of every policy file it edits). They are read for those rows only, never for the whole list.
   */
  policyApprovable?: ReadonlySet<string>;
}

/** The Inbox views that mean "the viewer has a part in this review" (hosted rows carry `views`). */
const INVOLVED_VIEWS: readonly string[] = ["authored_by_me", "participated", "to_review", "waiting_on_me", "assigned_to_my_teams"];

function involved(row: ReviewDirectoryRow): boolean {
  return (row.views ?? []).some((view) => INVOLVED_VIEWS.includes(view));
}

/** You approved the current revision (a local row's `axes.your_review.approved`): it is done for you, not your turn. */
export function approvedByYou(row: ReviewDirectoryRow): boolean {
  return row.axes?.your_review?.approved === true;
}

function isForMe(row: ReviewDirectoryRow): boolean {
  return row.next_action?.for_me === true;
}

/** `thread_returned`: a comment thread came back; `work_returned`: the hosted server's mark for a new revision of work you asked for. */
const RETURNED_REASONS: readonly string[] = ["thread_returned", "work_returned"];

function cameBack(row: ReviewDirectoryRow): boolean {
  // A review you approved has left Came back to you for Done, so the Came back chip does not count it (review-1.4).
  if (approvedByYou(row)) return false;
  return row.primary_bucket === "returned_to_me" || (row.next_action?.my_reasons ?? []).some((reason) => RETURNED_REASONS.includes(reason.kind));
}

/** What the viewer owes on their own change (not a review to read): the words for each such reason. */
const AUTHOR_TURN_WORDS: Readonly<Record<string, string>> = {
  feedback_received: "Feedback arrived",
  changes_requested: "Changes were requested",
  check_failed: "A check failed",
  landing_attention: "Needs your decision to land",
};

/**
 * The viewer's turn is on their own change: every reason they hold is an author's (feedback, requested changes, a
 * failed check, a landing decision). SPEC 4.1 is silent; SPEC 7.1 P9-2 K: the row stays in Your turn, says what happened,
 * and is never the hero, "Start review", part of the time budget or snoozable.
 */
export function isAuthorTurn(row: ReviewDirectoryRow): boolean {
  const reasons = row.next_action?.my_reasons ?? [];
  return reasons.length > 0 && reasons.every((reason) => reason.kind in AUTHOR_TURN_WORDS);
}

/**
 * The one section a review is in, or null when it has none (archived; or open with nobody's turn, e.g. ready to merge).
 * A snooze only moves a review that is waiting on the viewer.
 */
export function sectionOf(row: ReviewDirectoryRow, now: number, options: HomeOptions = {}): HomeSectionId | null {
  if (row.status === "finished") return options.teamsSupported && !involved(row) ? null : "finished";
  if (row.status !== "open") return null;
  // Hosted only (SPEC 4.16): the server sets this for the one person asked to co-sign; a local install never does.
  if (row.second_signature?.for_me) return "secondSignature";
  // review-1.4: an approved review leaves Your turn and Came back to you for the folded Done group, whatever reasons remain.
  if (approvedByYou(row)) return "done";
  if (isForMe(row) && row.edits_policy && options.policyApprovable?.has(row.id)) return "policyApproval";
  if (isForMe(row)) {
    if (isSnoozed(row, now)) return "snoozed";
    return cameBack(row) ? "cameBack" : "yourTurn";
  }
  if (options.teamsSupported && (row.views ?? []).includes("assigned_to_my_teams")) return "team";
  if (options.teamsSupported && !involved(row)) return null;
  return (row.next_action?.owner_ids ?? []).length > 0 ? "waiting" : null;
}

/** Only a review waiting on the viewer can be snoozed. */
export function canSnooze(row: ReviewDirectoryRow, now: number, options: HomeOptions = {}): boolean {
  const section = sectionOf(row, now, options);
  return (section === "yourTurn" || section === "cameBack") && !isAuthorTurn(row);
}

export interface HomeSection {
  id: HomeSectionId;
  title: string;
  rows: ReviewDirectoryRow[];
}

/** The sections that have rows, in SPEC order (D15: an empty section is not drawn). Rows keep the order given. */
export function buildSections(rows: readonly ReviewDirectoryRow[], now: number, options: HomeOptions = {}): HomeSection[] {
  const grouped = new Map<HomeSectionId, ReviewDirectoryRow[]>();
  for (const row of rows) {
    const id = sectionOf(row, now, options);
    if (id) grouped.set(id, [...(grouped.get(id) ?? []), row]);
  }
  return SECTION_ORDER.filter((id) => grouped.has(id)).map((id) => ({ id, title: SECTION_TITLE[id], rows: grouped.get(id) ?? [] }));
}

// -------------------------------------------------------------------- rows

export interface PartTally {
  decisions: number;
  quickChecks: number;
}

/** The decisions and quick checks still to answer, as the server counted them; null for a row without them. */
export function tallyOf(row: ReviewDirectoryRow): PartTally | null {
  const progress = row.progress;
  if (!progress || typeof progress.decision_open !== "number" || typeof progress.quick_check_open !== "number") return null;
  return { decisions: progress.decision_open, quickChecks: progress.quick_check_open };
}

export function rangeOf(row: ReviewDirectoryRow): MinuteRange | null {
  const tally = tallyOf(row);
  return tally ? timeRange(tally) : null;
}

/** "2 decisions · 1 quick check", "no decisions · 4 quick checks". One wording for Home, Next up and the Overview. */
export function tallyLine(tally: PartTally): string {
  const decisions = tally.decisions > 0 ? plural(tally.decisions, "decision") : "no decisions";
  return tally.quickChecks > 0 ? `${decisions} · ${plural(tally.quickChecks, "quick check")}` : decisions;
}

/** The row's tally line, or the part count when the server sent no split. */
export function countsLine(row: ReviewDirectoryRow): string {
  const tally = tallyOf(row);
  if (!tally) return row.progress ? plural(row.progress.target_count, "part") : "";
  return tallyLine(tally);
}

export function sourceOf(row: ReviewDirectoryRow): "Local" | "GitHub" {
  return row.source_ref.startsWith("github:") ? "GitHub" : "Local";
}

/** "github:acme/payments#412" → { repo: "payments", number: "412" }. */
function githubParts(sourceRef: string): { repo: string; number: string } | null {
  const match = /^github:[^/]+\/([^#/]+)#(\d+)$/.exec(sourceRef);
  return match ? { repo: match[1], number: match[2] } : null;
}

export function repoOf(row: ReviewDirectoryRow): string {
  return githubParts(row.source_ref)?.repo || row.project || row.repo_name || row.repo_id;
}

/** "shop · main" for a local review, "payments · #412" for a pull request. */
export function placeOf(row: ReviewDirectoryRow): string {
  const pr = githubParts(row.source_ref);
  if (pr) return `${pr.repo} · #${pr.number}`;
  return [repoOf(row), row.branch].filter(Boolean).join(" · ");
}

export function waitingSinceMs(row: ReviewDirectoryRow): number | null {
  const since = Date.parse(row.attention?.waiting_since ?? "");
  return Number.isFinite(since) ? since : null;
}

/** "6 min", "3 h", "1 d 4 h". Under a minute reads "under 1 min". */
export function ageText(ms: number): string {
  const minutes = Math.floor(Math.max(0, ms) / 60_000);
  if (minutes < 1) return "under 1 min";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} d ${hours % 24} h` : `${days} d`;
}

/**
 * The row's saved place. A saved Wrap up while the server still counts parts to answer is dropped: opening Wrap up early
 * is not being ready to wrap up (an older Wrap up saved its place on any visit).
 */
export function rowResume(row: ReviewDirectoryRow): ReviewResume | null {
  const resume = row.queue?.resume ?? null;
  if (resume?.screen !== "wrap") return resume;
  const tally = tallyOf(row);
  return tally && tally.decisions + tally.quickChecks > 0 ? null : resume;
}

/** The resume line: "Paused at decision 2 of 2", "Paused at the quick check", "Ready to wrap up", plus " · 1 note kept". */
export function resumeLine(resume: ReviewResume | null | undefined, notesKept: number): string {
  if (!resume) return "";
  const where = resume.screen === "wrap"
    ? "Ready to wrap up"
    : resume.screen === "quick_check"
      ? "Paused at the quick check"
      : `Paused at decision ${Math.max(1, resume.position)} of ${Math.max(1, resume.total)}`;
  return notesKept > 0 ? `${where} · ${plural(notesKept, "note")} kept` : where;
}

/** Where "Continue review" goes: the saved screen and part under `/r/<review>`; the Overview when there is no place. */
export function resumePath(basePath: string, resume: ReviewResume | null | undefined): string {
  if (!resume) return basePath;
  if (resume.screen === "wrap") return `${basePath}/wrap`;
  return resume.part ? `${basePath}/one/${encodeURIComponent(resume.part)}` : `${basePath}/one`;
}

/** "Sam reviewed · 1 decision in app/auth" (SPEC 4.16): the row's detail on Home and in the hosted inbox. */
export function secondSignatureText(second: NonNullable<ReviewDirectoryRow["second_signature"]> | undefined): string {
  const who = second?.first_reviewer_name ? firstWord(second.first_reviewer_name) : "A teammate";
  const count = second?.decision_count ?? 0;
  const where = second?.area ? ` in ${second.area}` : "";
  return `${who} reviewed \u00b7 ${count > 0 ? plural(count, "decision") : "a decision"}${where}`;
}

function firstWord(name: string): string {
  return name.trim().split(/\s+/)[0] || name;
}

/** The state phrase a row ends with (SPEC 4.1 row fields). */
export function stateText(row: ReviewDirectoryRow, section: HomeSectionId, now: Date): string {
  if (section === "snoozed") {
    const until = new Date(row.queue?.snoozed_until ?? "");
    return Number.isNaN(until.getTime()) ? "Snoozed" : `Snoozed until ${untilText(until, now)}`;
  }
  if (section === "secondSignature") return secondSignatureText(row.second_signature);
  if (section === "cameBack") return `${revisionName(row.revision_number)} is back`;
  if (section === "done") return `${revisionName(row.revision_number)} approved`;
  const resume = resumeLine(rowResume(row), row.unsent_count ?? 0);
  if (section === "yourTurn") return isAuthorTurn(row) ? AUTHOR_TURN_WORDS[row.next_action?.my_reasons?.[0]?.kind ?? ""] : resume || "Your turn";
  if (section === "waiting") return row.handoff_label ? `Waiting on ${row.handoff_label}` : "Waiting on others";
  return "";
}

// ------------------------------------------------------------------ budget

export interface Budget {
  reviews: number;
  /** null unless every row carries counts: a total over some of the reviews would understate the rest (a hosted page derives only some). */
  decisions: number | null;
  /** null when any row carries no counts: a total over some of the reviews would understate the rest. */
  range: MinuteRange | null;
}

/** What waits on the viewer: the Your turn and Came back reviews. */
export function budgetOf(rows: readonly ReviewDirectoryRow[]): Budget {
  const tallies = rows.map(tallyOf);
  const known = tallies.every((tally) => tally !== null);
  return {
    reviews: rows.length,
    decisions: known ? tallies.reduce((total, tally) => total + (tally?.decisions ?? 0), 0) : null,
    range: known && rows.length > 0 ? sumRanges(rows.map((row) => rangeOf(row) as MinuteRange)) : null,
  };
}

/** "2 reviews · 7–13 min · 4 decisions"; at volume "12 reviews waiting · about 1 h 10 min–2 h 20 min · 23 decisions". */
export function budgetLine(budget: Budget, dense: boolean): string {
  const reviews = dense ? `${plural(budget.reviews, "review")} waiting` : plural(budget.reviews, "review");
  const time = budget.range ? `${dense ? "about " : ""}${rangeText(budget.range)}` : "";
  return [reviews, time, budget.decisions === null ? "" : plural(budget.decisions, "decision")].filter(Boolean).join(" · ");
}

// ------------------------------------------------- dense list: sort, filters

export type SortKey = "waiting" | "fewest" | "newest";

export const SORT_CHOICES: readonly { key: SortKey; label: string }[] = [
  { key: "waiting", label: "Waiting longest" },
  { key: "fewest", label: "Fewest decisions first" },
  { key: "newest", label: "Newest" },
];

export function isSortKey(value: unknown): value is SortKey {
  return SORT_CHOICES.some((choice) => choice.key === value);
}

/** A stable sort; a row with no value for the key goes last, and ties keep the order given. */
export function sortRows(rows: readonly ReviewDirectoryRow[], key: SortKey): ReviewDirectoryRow[] {
  const order = (row: ReviewDirectoryRow): number => {
    if (key === "waiting") return waitingSinceMs(row) ?? Number.POSITIVE_INFINITY;
    if (key === "fewest") return tallyOf(row)?.decisions ?? Number.POSITIVE_INFINITY;
    const created = Date.parse(row.created_at);
    return Number.isFinite(created) ? -created : Number.POSITIVE_INFINITY;
  };
  return rows.map((row, index) => ({ row, index })).sort((a, b) => order(a.row) - order(b.row) || a.index - b.index).map((item) => item.row);
}

export interface HomeFilters {
  repos: readonly string[];
  sources: readonly ("Local" | "GitHub")[];
  cameBack: boolean;
}

export const NO_FILTERS: HomeFilters = { repos: [], sources: [], cameBack: false };

export function filtersActive(filters: HomeFilters, query: string): boolean {
  return filters.repos.length > 0 || filters.sources.length > 0 || filters.cameBack || query.trim() !== "";
}

export function matchesQuery(row: ReviewDirectoryRow, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [row.title, row.source_ref, row.project, row.branch, row.repo_id, repoOf(row), placeOf(row)].some((text) => (text ?? "").toLowerCase().includes(needle));
}

export function matchesFilters(row: ReviewDirectoryRow, filters: HomeFilters, query: string): boolean {
  if (filters.repos.length > 0 && !filters.repos.includes(repoOf(row))) return false;
  if (filters.sources.length > 0 && !filters.sources.includes(sourceOf(row))) return false;
  if (filters.cameBack && !cameBack(row)) return false;
  return matchesQuery(row, query);
}

/** The repo and source chips with counts over `rows`, repos in first-seen order. */
export function chipCounts(rows: readonly ReviewDirectoryRow[]): { repos: { name: string; count: number }[]; sources: { name: "Local" | "GitHub"; count: number }[]; cameBack: number } {
  const repos = new Map<string, number>();
  const sources = new Map<"Local" | "GitHub", number>();
  let back = 0;
  for (const row of rows) {
    repos.set(repoOf(row), (repos.get(repoOf(row)) ?? 0) + 1);
    sources.set(sourceOf(row), (sources.get(sourceOf(row)) ?? 0) + 1);
    if (cameBack(row)) back += 1;
  }
  return {
    repos: [...repos].map(([name, count]) => ({ name, count })),
    sources: [...sources].map(([name, count]) => ({ name, count })),
    cameBack: back,
  };
}

/** The list is dense above five open reviews that are in a section (a review with none, or one you approved, does not count). */
export function isDense(rows: readonly ReviewDirectoryRow[], now: number, options: HomeOptions = {}): boolean {
  return rows.filter((row) => {
    const section = row.status === "open" ? sectionOf(row, now, options) : null;
    return section !== null && section !== "done";
  }).length > DENSE_ABOVE;
}

/** First run: not one review exists, in any state. */
export function isFirstRun(rows: readonly ReviewDirectoryRow[]): boolean {
  return rows.length === 0;
}

/** The sections that mean a review waits on the viewer. Home's "Needs attention" and the queue-clear card share them. */
const WAITS_ON_VIEWER: readonly HomeSectionId[] = ["yourTurn", "cameBack", "secondSignature"];

export function waitsOnViewer(row: ReviewDirectoryRow, now: number, options: HomeOptions = {}): boolean {
  const section = sectionOf(row, now, options);
  return section !== null && WAITS_ON_VIEWER.includes(section);
}

/** Queue clear: reviews exist but none waits on the viewer. */
export function isQueueClear(sections: readonly HomeSection[]): boolean {
  return !sections.some((section) => WAITS_ON_VIEWER.includes(section.id));
}
