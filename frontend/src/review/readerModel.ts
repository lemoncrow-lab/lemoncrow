import type {
  RevisionTargetDelta,
  ReviewOutlineItem,
  ReviewOverview,
  ReviewProgress,
  ReviewTarget,
  ReviewTargetPriority,
} from "./types";

export type ReaderOrder = "recommended" | "file";
export type OutlineSectionKey = "review_first" | "unreviewed" | "mechanical" | "done";

export interface ReaderFile {
  path: string;
  targets: ReviewTarget[];
  reviewed: number;
  targetCount: number;
  attentionRank: number;
}

export interface OutlineSection {
  key: OutlineSectionKey;
  label: string;
  rows: ReviewOutlineItem[];
}

export interface ReviewStoryStep {
  label: string;
  path: string;
  fileCount?: number;
  targetCount?: number;
  reviewed?: number;
  changedSinceReview?: number;
  explanation?: string;
}

export interface ReviewGuideTarget {
  targetId: string;
  path: string;
  label: string;
  reason: string;
  /** The typed code behind `reason`; "" for a synthetic row the server never placed. */
  reasonCode: string;
  attention: ReviewTargetPriority["attention"] | "";
}

/**
 * One target's place in the canonical review queue and why it is there.
 *
 * Every surface that names a reason or orders attention -- the outline's
 * section and file reason, the Next card, `]`/`[`, the primary action and the
 * target header chip -- reads this one result, so a shown reason can never
 * disagree with the order. It projects the server's placement
 * (`target.priority`); it never ranks. `attention_rank` is the queue position,
 * not the legacy scalar `ReviewTarget.attention_rank`.
 */
export interface TargetAttention {
  tier: number;
  attention: ReviewTargetPriority["attention"];
  reason_code: string;
  reason_label: string;
  actionable: boolean;
  attention_rank: number;
}

export type ReviewScopeTone = "added" | "deleted" | "modified";

export function reviewScopeTone(target: ReviewTarget): ReviewScopeTone {
  if (target.additions > 0 && target.deletions > 0) return "modified";
  if (target.additions > 0 && target.deletions === 0) return "added";
  if (target.deletions > 0 && target.additions === 0) return "deleted";
  const hasNew = target.spans.some((span) => span.side === "new");
  const hasOld = target.spans.some((span) => span.side === "old");
  if (hasNew && !hasOld) return "added";
  if (hasOld && !hasNew) return "deleted";
  return "modified";
}

export function targetDisplayLabel(target: ReviewTarget): string {
  if (target.kind === "document_section") return "Imports and top-level code";
  if (target.symbol) return target.symbol;
  if (target.kind === "hunk") {
    const oldSpanCount = target.spans.filter((span) => span.side === "old").length;
    const newSpanCount = target.spans.filter((span) => span.side === "new").length;
    const hunkCount = new Set(target.spans.map((span) => span.hunk_ordinal)).size;
    const regionCount = Math.max(oldSpanCount, newSpanCount, hunkCount);
    if (regionCount > 1) return `Other changed lines · ${regionCount} places`;
    const span = target.spans.find((item) => item.side === "new") ?? target.spans[0];
    const start = span?.start_line || target.start_line || 0;
    const end = span?.end_line || target.end_line || start;
    if (start > 0 && end === start) return `Changed line ${start}`;
    if (start > 0 && end >= start) return `Changed lines ${start}–${end}`;
    return "Other changed lines";
  }
  return target.path;
}

function legacyActionable(target: ReviewTarget): boolean {
  return (
    target.state !== "reviewed"
    || target.annotation_counts.open > 0
    || target.annotation_counts.orphaned > 0
    || target.annotation_counts.addressed_needs_rereview > 0
    || target.verification.fail > 0
    || target.verification.unknown > 0
  );
}

const NEEDS_JUDGMENT = "Needs human judgment";

function informativeReason(reasons: readonly string[]): string {
  return reasons.find((reason) => !/^\+\d+\s+-\d+$/.test(reason.trim())) ?? "";
}

/**
 * The canonical classification (plan §8.8). The server orders `reasons` so
 * the first is the one that placed the target; a target without a placement
 * (a synthetic compare row) is Understand while outstanding, in list order.
 */
export function targetAttention(target: ReviewTarget, index = 0): TargetAttention {
  const priority = target.priority;
  if (priority) {
    const reason = priority.reasons[0];
    return {
      tier: priority.tier,
      attention: priority.attention,
      reason_code: reason?.code ?? "",
      reason_label: reason?.detail || (priority.outstanding ? NEEDS_JUDGMENT : ""),
      actionable: priority.outstanding,
      attention_rank: priority.position,
    };
  }
  const actionable = legacyActionable(target);
  return {
    tier: actionable ? 4 : 6,
    attention: actionable ? "understand" : "done",
    reason_code: "",
    reason_label: actionable ? informativeReason(target.reasons) || NEEDS_JUDGMENT : "",
    actionable,
    attention_rank: index,
  };
}

/** Outstanding per the server queue; targets without one (synthetic compare rows) use their state. */
export function isActionable(target: ReviewTarget): boolean {
  return targetAttention(target).actionable;
}

/**
 * The canonical review queue: every outstanding target in server position
 * order. Brief, Next card, `]`/`[`, initial selection and resume all read this;
 * nothing here ranks by reason text (spec §7, §11).
 */
export function reviewQueue(targets: readonly ReviewTarget[]): ReviewTarget[] {
  return targets
    .map((target, index) => ({ target, attention: targetAttention(target, index) }))
    .filter(({ attention }) => attention.actionable)
    .sort((left, right) => left.attention.attention_rank - right.attention.attention_rank)
    .map(({ target }) => target);
}

/**
 * Each file's attention: the classification of its first queue entry, or
 * done when nothing in it is outstanding. The outline's section and reason
 * read this, so a file row agrees with the Next card and the target headers.
 */
export function fileAttention(targets: readonly ReviewTarget[]): Map<string, TargetAttention> {
  const byPath = new Map<string, TargetAttention>();
  targets.forEach((target, index) => {
    const attention = targetAttention(target, index);
    const current = byPath.get(target.path);
    if (
      !current
      || (attention.actionable && (!current.actionable || attention.attention_rank < current.attention_rank))
    ) {
      byPath.set(target.path, attention);
    }
  });
  return byPath;
}

export function targetSearchText(target: ReviewTarget): string {
  return [
    target.path,
    target.symbol,
    target.label,
    target.kind,
    target.state.replaceAll("_", " "),
    ...target.reasons,
  ].join(" ").toLowerCase();
}

export function filterTargets(
  targets: readonly ReviewTarget[],
  query: string,
  supplementalSearchText?: ReadonlyMap<string, string>,
): ReviewTarget[] {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return [...targets];
  return targets.filter((target) => {
    const extra = supplementalSearchText?.get(target.target_id) ?? "";
    const haystack = `${targetSearchText(target)} ${extra}`.toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

export function orderedPaths(targets: readonly ReviewTarget[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const target of targets) {
    if (seen.has(target.path)) continue;
    seen.add(target.path);
    out.push(target.path);
  }
  return out;
}

export function groupReaderFiles(targets: readonly ReviewTarget[]): ReaderFile[] {
  const order = orderedPaths(targets);
  const byPath = new Map<string, ReviewTarget[]>();
  for (const target of targets) {
    const rows = byPath.get(target.path) ?? [];
    rows.push(target);
    byPath.set(target.path, rows);
  }
  return order.map((path) => {
    const rows = byPath.get(path) ?? [];
    const ranks = rows.map((target) => target.attention_rank).filter((rank) => rank > 0);
    return {
      path,
      targets: rows,
      reviewed: rows.filter((target) => target.state === "reviewed").length,
      targetCount: rows.length,
      attentionRank: ranks.length ? Math.min(...ranks) : 0,
    };
  });
}

export function targetIndex(targets: readonly ReviewTarget[], targetId: string): number {
  return targets.findIndex((target) => target.target_id === targetId);
}

export function stepTarget(
  targets: readonly ReviewTarget[],
  currentId: string,
  delta: number,
  options: { actionableOnly?: boolean; wrap?: boolean } = {},
): ReviewTarget | null {
  if (targets.length === 0) return null;
  const eligibleIds = new Set(
    targets
      .filter((target) => !options.actionableOnly || isActionable(target))
      .map((target) => target.target_id),
  );
  if (eligibleIds.size === 0) return null;

  const current = targets.findIndex((target) => target.target_id === currentId);
  if (current < 0) {
    const eligible = targets.filter((target) => eligibleIds.has(target.target_id));
    return delta >= 0 ? eligible[0] : eligible[eligible.length - 1];
  }

  const direction = delta >= 0 ? 1 : -1;
  for (let offset = 1; offset <= targets.length; offset += 1) {
    const raw = current + direction * offset;
    if (!options.wrap && (raw < 0 || raw >= targets.length)) return null;
    const index = ((raw % targets.length) + targets.length) % targets.length;
    const candidate = targets[index];
    if (eligibleIds.has(candidate.target_id)) return candidate;
  }
  return null;
}

export function stepFile(
  targets: readonly ReviewTarget[],
  currentId: string,
  delta: number,
): ReviewTarget | null {
  if (targets.length === 0) return null;
  const paths = orderedPaths(targets);
  const current = targets.find((target) => target.target_id === currentId) ?? targets[0];
  const currentFile = Math.max(0, paths.indexOf(current.path));
  const next = Math.min(paths.length - 1, Math.max(0, currentFile + (delta >= 0 ? 1 : -1)));
  if (next === currentFile) return current;
  const path = paths[next];
  return targets.find((target) => target.path === path && isActionable(target)) ??
    targets.find((target) => target.path === path) ??
    null;
}

export function firstActionable(targets: readonly ReviewTarget[]): ReviewTarget | null {
  return reviewQueue(targets)[0] ?? targets[0] ?? null;
}

function guideFor(target: ReviewTarget): ReviewGuideTarget {
  const attention = targetAttention(target);
  return {
    targetId: target.target_id,
    path: target.path,
    label: targetDisplayLabel(target),
    reason: attention.reason_label,
    reasonCode: attention.reason_code,
    attention: target.priority ? attention.attention : "",
  };
}

export function reviewAttentionPath(targets: readonly ReviewTarget[]): ReviewGuideTarget[] {
  return reviewQueue(targets).map(guideFor);
}

export function nextReviewGuideTarget(targets: readonly ReviewTarget[]): ReviewGuideTarget | null {
  const first = reviewQueue(targets)[0];
  return first ? guideFor(first) : null;
}

/** The queue's first part that is not a requested change: where review goes once those are with the fixer. */
export function nextReviewGuideTargetAfterHandoff(targets: readonly ReviewTarget[]): ReviewGuideTarget | null {
  for (const target of reviewQueue(targets)) {
    const guide = guideFor(target);
    if (guide.reasonCode !== "request_change_open") return guide;
  }
  return null;
}

/** `]` / `[`: the next or previous item of the canonical queue, wrapping. */
export function stepQueueTarget(
  targets: readonly ReviewTarget[],
  currentId: string,
  delta: number,
): ReviewTarget | null {
  const rows = reviewQueue(targets);
  if (rows.length === 0) return null;
  const current = rows.findIndex((target) => target.target_id === currentId);
  if (current < 0) return delta >= 0 ? rows[0] : rows[rows.length - 1];
  const index = (((current + (delta >= 0 ? 1 : -1)) % rows.length) + rows.length) % rows.length;
  return rows[index];
}

export function reviewStorySteps(overview: ReviewOverview): ReviewStoryStep[] {
  const labels = overview.change_story ?? [];
  const chapters = overview.chapters?.intent ?? [];
  const used = new Set<string>();
  const out: ReviewStoryStep[] = [];
  for (const label of labels) {
    const chapter = chapters.find((item) => item.label === label);
    const path = chapter?.rows.find((row) => Boolean(row.path))?.path ?? "";
    if (!path || used.has(path)) continue;
    used.add(path);
    const step: ReviewStoryStep = { label, path };
    if (chapter?.progress || chapter?.target_count) {
      step.targetCount = chapter.progress?.target_count ?? chapter.target_count ?? 0;
      step.reviewed = chapter.progress?.reviewed ?? 0;
      step.changedSinceReview = chapter.progress?.changed_since_review ?? chapter.changed_count ?? 0;
    }
    const explanation = chapter?.explanation ?? chapter?.reason ?? "";
    if (explanation) step.explanation = explanation;
    out.push(step);
  }
  return out;
}

export function bestTargetAfterRevision(
  previous: ReviewTarget | null,
  targets: readonly ReviewTarget[],
  delta: RevisionTargetDelta | undefined,
): ReviewTarget | null {
  if (targets.length === 0) return null;
  const activeIds = new Set((delta?.active ?? []).map((item) => item.target_id));
  const active = targets.filter((target) => activeIds.has(target.target_id));
  const survivor = previous
    ? targets.find((target) => target.unit_key === previous.unit_key) ?? null
    : null;

  if (survivor && activeIds.has(survivor.target_id)) return survivor;
  if (previous) {
    const samePath = active
      .filter((target) => target.path === previous.path)
      .sort((left, right) => Math.abs(left.start_line - previous.start_line) - Math.abs(right.start_line - previous.start_line));
    if (samePath[0]) return samePath[0];
  }
  if (active[0]) return active[0];
  if (survivor) return survivor;
  return firstActionable(targets);
}

export function progressFromTargets(targets: readonly ReviewTarget[]): ReviewProgress {
  // `later` is present only when a part was left for later, so a review without one reads exactly as it always did.
  const later = targets.filter((target) => target.state === "later").length;
  return {
    target_count: targets.length,
    reviewed: targets.filter((target) => target.state === "reviewed").length,
    changed_since_review: targets.filter((target) => target.state === "changed_since_review").length,
    needs_changes: targets.filter((target) => target.state === "needs_changes").length,
    unreviewed: targets.filter((target) => target.state === "unreviewed").length,
    unknown: targets.filter((target) => target.state === "unknown").length,
    mechanical: targets.filter((target) => target.attention_level === "mechanical").length,
    ...(later > 0 ? { later } : {}),
  };
}

const SECTION_FOR_ATTENTION: Record<TargetAttention["attention"], OutlineSectionKey> = {
  review_first: "review_first",
  understand: "unreviewed",
  routine: "mechanical",
  done: "done",
};

/**
 * The file's canonical attention when known (see `fileAttention`); else the
 * server's section; else counts -- never reasons or paths.
 */
function sectionFor(row: ReviewOutlineItem, attention: TargetAttention | undefined): OutlineSectionKey {
  if (attention) return SECTION_FOR_ATTENTION[attention.attention];
  if (row.section) return row.section;
  return row.reviewed === row.target_count && row.target_count > 0 ? "done" : "unreviewed";
}

const SECTION_ORDER: { key: OutlineSectionKey; label: string }[] = [
  { key: "review_first", label: "Review first" },
  { key: "unreviewed", label: "Unreviewed" },
  { key: "mechanical", label: "Routine" },
  { key: "done", label: "Done" },
];

export function outlineSections(
  rows: readonly ReviewOutlineItem[],
  query = "",
  attentionByPath?: ReadonlyMap<string, TargetAttention>,
): OutlineSection[] {
  const needle = query.trim().toLowerCase();
  const buckets = new Map<OutlineSectionKey, ReviewOutlineItem[]>();
  for (const row of rows) {
    if (needle && !`${row.path} ${row.reasons.join(" ")}`.toLowerCase().includes(needle)) continue;
    const key = sectionFor(row, attentionByPath?.get(row.path));
    const bucket = buckets.get(key) ?? [];
    bucket.push(row);
    buckets.set(key, bucket);
  }
  return SECTION_ORDER.map(({ key, label }) => ({ key, label, rows: buckets.get(key) ?? [] })).filter(
    (section) => section.rows.length > 0,
  );
}

export function targetScrollLocation(target: ReviewTarget): {
  lineNumber: number;
  side: "additions" | "deletions";
} | null {
  const preferred = target.spans.find((span) => span.side === "new") ?? target.spans[0];
  if (!preferred || preferred.start_line < 1) {
    return target.start_line > 0 ? { lineNumber: target.start_line, side: "additions" } : null;
  }
  return {
    lineNumber: preferred.start_line,
    side: preferred.side === "new" ? "additions" : "deletions",
  };
}
