/**
 * The Focus card's rules (SPEC 4.3, 4.4), pure: which part comes next, what the pips and the counter say, when
 * Looks good pauses, what the confirmation line reads. Nothing here reads the DOM, storage or the network.
 *
 * Every fact it uses is a field the server sent; a rule with no field behind it is not built (SPEC 1).
 */

import { answerCounts, isJudgedState } from "../answerModel";
import type { CheckRunResponse, CheckVerdict } from "../checkRecipesApi";
import { partBySlug, partName, partsOf, plural, questionFor } from "../overview/overviewModel";
import { readOrder, type HasProof } from "../sinceReview";
import type {
  MarkState,
  ReviewConsumerSite,
  ReviewMarkEvent,
  ReviewProjection,
  ReviewTarget,
} from "../types";

/** Up to this many parts the top bar draws one pip per part; beyond it, SPEC 4.15 (P5-3) owns the counter. */
export const MAX_PIPS = 12;

const OPEN_STATES: ReadonlySet<MarkState> = new Set(<MarkState[]>["unreviewed", "unknown", "changed_since_review", "later"]);

/** A part a person still has to answer: not judged, or judged on content that has since moved. `later` is open. */
export function needsAnswer(target: Pick<ReviewTarget, "state">): boolean {
  return OPEN_STATES.has(target.state);
}

// ------------------------------------------------------------------ order

/**
 * The decisions in the order they are asked: parts changed since the reviewer's own review first (no proof before
 * proof, P11-2), then the server's risk order, with parts left for later at the end (SPEC 4.3 Order). Stable: no group
 * is reordered inside itself.
 */
export function decisionQueue(targets: readonly ReviewTarget[], hasProof?: HasProof): ReviewTarget[] {
  const decisions = readOrder(partsOf(targets, "decision"), hasProof);
  return [...decisions.filter((target) => target.state !== "later"), ...decisions.filter((target) => target.state === "later")];
}

/** The parts a person judges, in the fixed order "Part n of N" counts: decisions, then quick checks. */
export function judgedParts(targets: readonly ReviewTarget[], hasProof?: HasProof): ReviewTarget[] {
  return [...readOrder(partsOf(targets, "decision"), hasProof), ...readOrder(partsOf(targets, "quick_check"), hasProof)];
}

/** The part a route names: by its symbol or its target id (SPEC 7.1 P2-1 E). */
export function resolvePart(targets: readonly ReviewTarget[], slug: string): ReviewTarget | null {
  if (!slug) return null;
  return partBySlug(targets, slug);
}

/**
 * The next part to answer after `fromId`: the first one after it in the queue that needs an answer, else the
 * first one before it. Null when no other part needs one. Pure over the queue as it stood when `fromId` was
 * answered, so a part just moved to the end still hands over to the one that followed it.
 */
export function nextNeeding(queue: readonly ReviewTarget[], fromId: string, answered: ReadonlySet<string> = new Set()): ReviewTarget | null {
  const index = queue.findIndex((target) => target.target_id === fromId);
  const open = (target: ReviewTarget) => target.target_id !== fromId && needsAnswer(target) && !answered.has(target.target_id);
  for (let i = index + 1; i < queue.length; i += 1) if (open(queue[i])) return queue[i];
  for (let i = 0; i < Math.max(index, 0); i += 1) if (open(queue[i])) return queue[i];
  return null;
}

/** The part J or K reaches: one step through the decision queue; null past either end (J then shows the interstitial). */
export function stepPart(queue: readonly ReviewTarget[], fromId: string, delta: 1 | -1): ReviewTarget | null {
  const index = queue.findIndex((target) => target.target_id === fromId);
  if (index < 0) return null;
  return queue[index + delta] ?? null;
}

/**
 * "Part n of N" within the part's own kind: n of the decisions for a decision, n of the quick checks for a quick check, so a
 * decision never reads "Part 3 of 3048" because of the quick checks behind it. n is 0 when the part is not one a person judges.
 */
export function partPosition(targets: readonly ReviewTarget[], id: string, hasProof?: HasProof): { n: number; total: number } {
  const decisions = readOrder(partsOf(targets, "decision"), hasProof);
  const index = decisions.findIndex((target) => target.target_id === id);
  if (index >= 0) return { n: index + 1, total: decisions.length };
  const quick = readOrder(partsOf(targets, "quick_check"), hasProof);
  const at = quick.findIndex((target) => target.target_id === id);
  return at >= 0 ? { n: at + 1, total: quick.length } : { n: 0, total: decisions.length };
}

// ------------------------------------------------------------- pips, counter

export type PipState = "open" | "good" | "change" | "later";

export interface Pip {
  id: string;
  state: PipState;
  quick: boolean;
  current: boolean;
}

export function pipState(target: Pick<ReviewTarget, "state">): PipState {
  if (target.state === "reviewed") return "good";
  if (target.state === "needs_changes") return "change";
  if (target.state === "later") return "later";
  return "open";
}

export interface CounterSegment { kind: "decision" | "quick_check" | "accepted_by_rule"; count: number; text: string }
export interface CounterBar {
  segments: CounterSegment[];
  /** "12 decisions · 31 quick checks · 187 accepted by rule": the bar's accessible text and its hover and focus text. */
  label: string;
}

/** The quick checks' own, secondary progress: "quick checks 3/40". */
export interface QuickProgress {
  judged: number;
  total: number;
  text: string;
}

/**
 * The work counter: decisions only. Quick checks keep their own small progress beside it, and parts accepted by a
 * repository rule are not work, so they are in neither number.
 */
export interface Counter {
  /** Decisions answered (Looks good or Needs a change). */
  judged: number;
  /** Decisions. */
  total: number;
  later: number;
  /** One pip per decision; null above the pip limit or with no decisions. */
  pips: Pip[] | null;
  /** Visible: "1 of 3 decisions". */
  text: string;
  /** Above the pip limit: the segmented bar (SPEC 4.15 SCALE-1). Null up to it. */
  bar?: CounterBar | null;
  /** The pip row's accessible name: "1 of 3 decisions judged" and, with any, " · 1 left for later". */
  label: string;
  /** Null when the review has no quick checks. */
  quick?: QuickProgress | null;
}

/** The three segments from the server's answer category of every part; accepted by rule is counted, never judged. */
export function counterBar(targets: readonly ReviewTarget[]): CounterBar {
  const counts = answerCounts(targets);
  const segments: CounterSegment[] = [
    { kind: "decision" as const, count: counts.decision, text: plural(counts.decision, "decision") },
    { kind: "quick_check" as const, count: counts.quick_check, text: plural(counts.quick_check, "quick check") },
    { kind: "accepted_by_rule" as const, count: counts.accepted_by_rule, text: `${counts.accepted_by_rule} accepted by rule` },
  ];
  return { segments, label: segments.map((segment) => segment.text).join(" · ") };
}

/**
 * The counter from the complete parts list only. Null while the list is a startup slice, because a count taken
 * from part of a review is not the review's count (SPEC 1).
 */
export function counterFor(targets: readonly ReviewTarget[], currentId: string, complete: boolean, hasProof?: HasProof): Counter | null {
  if (!complete) return null;
  const decisions = readOrder(partsOf(targets, "decision"), hasProof);
  const quick = partsOf(targets, "quick_check");
  const judged = decisions.filter((target) => isJudgedState(target.state)).length;
  const later = decisions.filter((target) => target.state === "later").length;
  const quickJudged = quick.filter((target) => isJudgedState(target.state)).length;
  const total = decisions.length;
  const pips: Pip[] | null = total === 0 || total > MAX_PIPS ? null : decisions.map((target) => ({
    id: target.target_id,
    state: pipState(target),
    quick: false,
    current: target.target_id === currentId,
  }));
  const noun = total === 1 ? "decision" : "decisions";
  return {
    judged,
    total,
    later,
    pips,
    bar: total > MAX_PIPS ? counterBar(targets) : null,
    text: total === 0 ? "No decisions" : `${judged} of ${total} ${noun}`,
    label: total === 0 ? "No decisions" : `${judged} of ${total} ${noun} judged${later > 0 ? ` · ${later} left for later` : ""}`,
    quick: quick.length > 0 ? { judged: quickJudged, total: quick.length, text: `quick checks ${quickJudged}/${quick.length}` } : null,
  };
}

// --------------------------------------------------------- Decide later

/**
 * "Moved to the end once" (SPEC 4.3, 7.1 P1-2 F): a part that is `later` now, or one whose mark events on this
 * revision already show a move to `later`. A client rule over the part's own history; the server never refuses a second.
 */
export function movedToEndOnce(target: Pick<ReviewTarget, "state">, events: readonly ReviewMarkEvent[] | null, revisionId: string): boolean {
  if (target.state === "later") return true;
  return (events ?? []).some((event) => event.event_kind === "judgment" && event.to_state === "later" && event.revision_id === revisionId);
}

export const MOVED_ONCE_REASON = "You already moved this one to the end once";
/** The server does not record Decide later yet (a hosted server waits until no older build can be serving it). */
export const LATER_OFF_REASON = "Decide later is not available on this server yet";

// ----------------------------------------------------------- answer plans

export interface AnswerPlan {
  /** The mark to record; null when the answer is already recorded (A on a part already Looks good keeps it). */
  mark: MarkState | null;
  /** Move to the next part now. */
  advance: boolean;
  /** Looks good shows the pause strip instead of advancing. */
  pause: boolean;
  /** Decide later is refused (moved once). */
  blocked: boolean;
}

/** Looks good: records `reviewed`; advances at once unless the structure shows a fact the card does not (SPEC 4.4). */
export function planLooksGood(target: Pick<ReviewTarget, "state">, pausable: boolean): AnswerPlan {
  if (target.state === "reviewed") return { mark: null, advance: true, pause: false, blocked: false };
  return { mark: "reviewed", advance: !pausable, pause: pausable, blocked: false };
}

/** Decide later: records `later` and moves on, once. A part that came back keeps the button off. */
export function planDecideLater(moved: boolean): AnswerPlan {
  if (moved) return { mark: null, advance: false, pause: false, blocked: true };
  return { mark: "later", advance: true, pause: false, blocked: false };
}

// ----------------------------------------------------------- pause strip

/**
 * Notable structure facts: call sites in files the change did not touch, of a definition that gained a required
 * parameter (`impact_kind: signature_change`, from impact.py `signature_change_impact`), found as facts (no
 * `uncertainty`) whose line calls this part's name. A site the server could only match by name is not a fact and does not stop anyone. The SPEC's
 * other triggers (untraceable value source, a new connection, a route without the permission check, a protected
 * area) have no field yet, so they never fire.
 */
export function notableSites(target: ReviewTarget, projection: ReviewProjection | null): ReviewConsumerSite[] {
  const name = (target.symbol || "").split(".").pop() ?? "";
  const groups = projection?.context?.consumers ?? [];
  const sites: ReviewConsumerSite[] = [];
  for (const group of groups) {
    const mine = group.target_ids.includes(target.target_id) || (group.join === "file" && group.path === target.path);
    if (!mine) continue;
    for (const site of group.sites) {
      if (site.impact_kind === "signature_change" && site.exactness === "snapshot" && !site.in_patch && !(site.uncertainty ?? "").trim() && callsName(site, name)) sites.push(site);
    }
  }
  return sites;
}

/**
 * The server joins a signature-change site to every part of the changed file (it records no owning symbol), so the
 * site's own line must call this part's name; otherwise another function's changed signature would stop this part.
 */
function callsName(site: ReviewConsumerSite, name: string): boolean {
  if (!name) return false;
  return new RegExp(`(?<![\\w$])${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\(`).test(site.snippet ?? "");
}

export const PAUSE_EYEBROW = "Before you move on";

export function pauseHeading(count: number): string {
  return `${PAUSE_EYEBROW} · ${plural(count, "thing")} the structure shows`;
}

/** The reason line: what the server checked, in the SPEC's "We stopped you because …" form. */
export function pauseReason(name: string, sites: readonly ReviewConsumerSite[]): string {
  const where = [...new Set(sites.map((site) => site.path))];
  return `We stopped you because ${plural(sites.length, "call site")} of ${name} in ${where.join(", ")} may not pass its new required parameter.`;
}

// ------------------------------------------------------ confirmation line

export type ConfirmationKind = "good" | "change" | "later";

export interface Confirmation {
  kind: ConfirmationKind;
  /** The part just answered. */
  part: string;
  /** The name of the part the card now shows. */
  next: string;
}

export function confirmationText(line: Confirmation): string {
  const head = line.kind === "good" ? "Looks good" : line.kind === "change" ? "Note saved" : "Moved to the end";
  return `${head} · next: ${line.next}`;
}

/** The confirmation line's tone (FOC-4): green for Looks good, amber for a saved note, neutral for a move to the end. */
export function confirmationTone(line: Confirmation): "success" | "warning" | "neutral" {
  return line.kind === "good" ? "success" : line.kind === "change" ? "warning" : "neutral";
}

// ------------------------------------------------------------------ Run test

export interface TestOutcome {
  verdict: CheckVerdict;
  /** The server replayed, carried forward or shared part of this run instead of executing it again. */
  reused: boolean;
  detail: string;
}

/**
 * The result of this click's run, from the finished report only (never from a CI or earlier result). The click
 * asks for `fresh`, which reuses nothing; if the report still says something was reused, carried or shared, the
 * line says so instead of presenting it as a new run.
 */
export function testOutcome(finished: Pick<CheckRunResponse, "verdicts" | "reused" | "carried" | "shared_base">, entryId: string, title: string): TestOutcome {
  const verdict = finished.verdicts[entryId] ?? "UNKNOWN";
  const reused = (finished.reused ?? 0) + (finished.carried ?? 0) + (finished.shared_base ?? 0) > 0;
  const head = verdict === "PASS" ? `${title} · passed` : verdict === "FAIL" ? `${title} · failed` : `${title} · no result`;
  return { verdict, reused, detail: reused ? `${head} · reused from an earlier run, not run again for this click` : head };
}

/** The momentum line in the card footer (SPEC 4.3). */
export function momentumText(next: ReviewTarget | null, nextQuick: ReviewTarget | null): string {
  if (next) return `Next: ${partName(next)}${next.state === "later" ? " · you left this for later" : " · risky"}`;
  return nextQuick ? `Next: ${partName(nextQuick)} · quick check` : "";
}

// -------------------------------------------------- the "only check there" line

export interface LineRange {
  start: number;
  end: number;
}

function formatLines(ranges: readonly LineRange[]): string {
  const parts = ranges.map((range) => (range.end > range.start ? `${range.start}–${range.end}` : `${range.start}`));
  const label = ranges.length === 1 && ranges[0].end === ranges[0].start ? "line" : "lines";
  return `${label} ${parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0]}`;
}

/** Overlapping or touching ranges joined into one, in line order: 6–7 and 7–9 are 6–9. */
export function mergeRanges(ranges: readonly LineRange[]): LineRange[] {
  const merged: LineRange[] = [];
  for (const range of [...ranges].sort((a, b) => a.start - b.start || a.end - b.end)) {
    const last = merged[merged.length - 1];
    if (last && range.start <= last.end + 1) last.end = Math.max(last.end, range.end);
    else merged.push({ start: range.start, end: range.end });
  }
  return merged;
}

/** The part's lines not covered by any hinted range. */
export function unhintedRanges(part: LineRange, hinted: readonly LineRange[]): LineRange[] {
  const merged = [...hinted]
    .map((range) => ({ start: Math.max(range.start, part.start), end: Math.min(range.end, part.end) }))
    .filter((range) => range.end >= range.start)
    .sort((a, b) => a.start - b.start);
  const gaps: LineRange[] = [];
  let cursor = part.start;
  for (const range of merged) {
    if (range.start > cursor) gaps.push({ start: cursor, end: range.start - 1 });
    cursor = Math.max(cursor, range.end + 1);
  }
  if (cursor <= part.end) gaps.push({ start: cursor, end: part.end });
  return gaps;
}

/**
 * "AI hinted at lines 6–7. Nothing hinted at lines 1–5 and 8 — you are the only check there." Null when nothing
 * was hinted (no sentence to make) or when AI is off. The server sends no per-hint line range yet (SPEC 7.1
 * P2-1 J), so no screen calls this until it does.
 */
export function onlyCheckThereLine(part: LineRange, hinted: readonly LineRange[], aiAvailable: boolean): string | null {
  if (!aiAvailable || hinted.length === 0) return null;
  const inside = hinted.filter((range) => range.end >= part.start && range.start <= part.end);
  if (inside.length === 0) return null;
  const gaps = unhintedRanges(part, inside);
  const covered = mergeRanges(inside.map((range) => ({ start: Math.max(range.start, part.start), end: Math.min(range.end, part.end) })));
  const first = `AI hinted at ${formatLines(covered)}.`;
  return gaps.length === 0 ? first : `${first} Nothing hinted at ${formatLines(gaps)} — you are the only check there.`;
}

// ------------------------------------------------------------ interstitial

/** The interstitial's heading (SPEC 4.3): the SPEC's own words for two decisions, the same shape for any other number. */
export function interstitialHeading(decisions: number): string {
  return decisions === 2 ? "Both risky parts judged." : "All risky parts judged.";
}

/** The answer badge on the interstitial: only judged parts get one, because only they reach it. */
export function answerBadge(target: Pick<ReviewTarget, "state">): string {
  return target.state === "needs_changes" ? "Needs a change" : "Looks good";
}

export { partName, questionFor };
