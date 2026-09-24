import type { RevisionTargetDelta, ReviewOutlineItem, ReviewOverview, ReviewProgress, ReviewTarget } from "./types";

export type ReaderOrder = "recommended" | "file";
export type OutlineSectionKey = "attention" | "changed" | "remaining" | "tests" | "mechanical" | "done";

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
}

export interface ReviewGuideTarget {
  targetId: string;
  path: string;
  label: string;
  reason: string;
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
  if (target.symbol) return target.symbol;
  if (target.kind === "hunk") {
    const oldSpanCount = target.spans.filter((span) => span.side === "old").length;
    const newSpanCount = target.spans.filter((span) => span.side === "new").length;
    const hunkCount = new Set(target.spans.map((span) => span.hunk_ordinal)).size;
    const regionCount = Math.max(oldSpanCount, newSpanCount, hunkCount);
    if (regionCount > 1) return `Uncovered changes · ${regionCount} regions`;
    const span = target.spans.find((item) => item.side === "new") ?? target.spans[0];
    const start = span?.start_line || target.start_line || 0;
    const end = span?.end_line || target.end_line || start;
    if (start > 0 && end === start) return `Line ${start}`;
    if (start > 0 && end >= start) return `Lines ${start}–${end}`;
    return "Uncovered changes";
  }
  return target.path;
}

export function isActionable(target: ReviewTarget): boolean {
  return (
    target.state !== "reviewed"
    || target.annotation_counts.open > 0
    || target.annotation_counts.orphaned > 0
    || target.annotation_counts.addressed_needs_rereview > 0
    || target.verification.fail > 0
    || target.verification.unknown > 0
  );
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
  options: { actionableOnly?: boolean; highPriorityOnly?: boolean; wrap?: boolean } = {},
): ReviewTarget | null {
  if (targets.length === 0) return null;
  const eligibleIds = new Set(
    targets
      .filter((target) => {
        if (options.actionableOnly && !isActionable(target)) return false;
        if (
          options.highPriorityOnly &&
          target.attention_level !== "high" &&
          target.state !== "changed_since_review" &&
          target.state !== "needs_changes"
        ) {
          return false;
        }
        return true;
      })
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
  return targets.find(isActionable) ?? targets[0] ?? null;
}

function concreteTargetReason(target: ReviewTarget): string {
  if (target.annotation_counts.addressed_needs_rereview > 0) {
    const count = target.annotation_counts.addressed_needs_rereview;
    return `${count} addressed comment${count === 1 ? " needs" : "s need"} re-review`;
  }
  if (target.state === "changed_since_review") return "Changed since your last review";
  if (target.verification.fail > 0) {
    const count = target.verification.fail;
    return `${count} verification failure${count === 1 ? "" : "s"}`;
  }
  if (target.state === "needs_changes") return "Requested changes remain open";
  if (target.annotation_counts.orphaned > 0) {
    const count = target.annotation_counts.orphaned;
    return `${count} comment${count === 1 ? "" : "s"} lost ${count === 1 ? "its" : "their"} anchor`;
  }
  if (target.annotation_counts.open > 0) {
    const count = target.annotation_counts.open;
    return `${count} open review comment${count === 1 ? "" : "s"}`;
  }
  if (target.verification.unknown > 0) {
    const count = target.verification.unknown;
    return `${count} verification result${count === 1 ? "" : "s"} unknown`;
  }
  const reason = target.reasons.find((item) => !/^\+\d+\s+-\d+$/.test(item.trim()));
  return reason || "Needs human judgment";
}

function explicitAttentionSignal(target: ReviewTarget): boolean {
  return (
    target.annotation_counts.addressed_needs_rereview > 0
    || target.annotation_counts.orphaned > 0
    || target.annotation_counts.open > 0
    || target.verification.fail > 0
    || target.verification.unknown > 0
    || target.state === "changed_since_review"
    || target.state === "needs_changes"
    || target.state === "unknown"
  );
}

function attentionTier(target: ReviewTarget): number {
  if (target.annotation_counts.addressed_needs_rereview > 0) return 0;
  if (target.state === "changed_since_review") return 1;
  if (target.verification.fail > 0) return 2;
  if (target.annotation_counts.orphaned > 0) return 3;
  if (target.state === "unknown" || target.verification.unknown > 0) return 4;
  if (target.state === "needs_changes") return 5;
  if (target.attention_level === "high") return 6;
  if (target.annotation_counts.open > 0) return 7;
  if (target.state === "unreviewed") return 8;
  return 9;
}

export function orderedAttentionTargets(targets: readonly ReviewTarget[]): ReviewTarget[] {
  return targets
    .map((target, index) => ({ target, index }))
    .filter(({ target }) =>
      isActionable(target)
      && (target.attention_level !== "mechanical" || explicitAttentionSignal(target)),
    )
    .sort((left, right) => {
      const tier = attentionTier(left.target) - attentionTier(right.target);
      if (tier !== 0) return tier;
      const leftRank = left.target.attention_rank > 0 ? left.target.attention_rank : Number.MAX_SAFE_INTEGER;
      const rightRank = right.target.attention_rank > 0 ? right.target.attention_rank : Number.MAX_SAFE_INTEGER;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return left.index - right.index;
    })
    .map(({ target }) => target);
}

export function reviewAttentionPath(targets: readonly ReviewTarget[]): ReviewGuideTarget[] {
  return orderedAttentionTargets(targets).map((target) => ({
    targetId: target.target_id,
    path: target.path,
    label: targetDisplayLabel(target),
    reason: concreteTargetReason(target),
  }));
}

export function nextReviewGuideTarget(targets: readonly ReviewTarget[]): ReviewGuideTarget | null {
  const attention = reviewAttentionPath(targets)[0];
  if (attention) return attention;
  const target = targets.find(isActionable);
  if (!target) return null;
  return {
    targetId: target.target_id,
    path: target.path,
    label: targetDisplayLabel(target),
    reason: concreteTargetReason(target),
  };
}

export function stepAttentionTarget(
  targets: readonly ReviewTarget[],
  currentId: string,
  delta: number,
): ReviewTarget | null {
  const rows = orderedAttentionTargets(targets);
  if (rows.length === 0) return null;
  const current = rows.findIndex((target) => target.target_id === currentId);
  if (current < 0) return delta >= 0 ? rows[0] : rows[rows.length - 1];
  const direction = delta >= 0 ? 1 : -1;
  const index = ((current + direction) % rows.length + rows.length) % rows.length;
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
    out.push({ label, path });
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
  return {
    target_count: targets.length,
    reviewed: targets.filter((target) => target.state === "reviewed").length,
    changed_since_review: targets.filter((target) => target.state === "changed_since_review").length,
    needs_changes: targets.filter((target) => target.state === "needs_changes").length,
    unreviewed: targets.filter((target) => target.state === "unreviewed").length,
    unknown: targets.filter((target) => target.state === "unknown").length,
    mechanical: targets.filter((target) => target.attention_level === "mechanical").length,
  };
}

function sectionFor(row: ReviewOutlineItem): OutlineSectionKey {
  if (row.changed_since_review > 0) return "changed";
  if (row.needs_changes > 0 || row.unknown > 0) return "attention";
  if (row.reviewed === row.target_count && row.target_count > 0) return "done";
  if (row.mechanical === row.target_count && row.target_count > 0) return "mechanical";
  if (row.reasons.some((reason) => !/^\+\d+\s+-\d+$/.test(reason.trim()))) return "attention";
  if (/(^|\/)(tests?|__tests__)(\/|$)|\.(test|spec)\.[^.]+$/i.test(row.path)) return "tests";
  return "remaining";
}

const SECTION_ORDER: { key: OutlineSectionKey; label: string }[] = [
  { key: "attention", label: "Needs attention" },
  { key: "changed", label: "Changed since review" },
  { key: "remaining", label: "Remaining" },
  { key: "tests", label: "Tests" },
  { key: "mechanical", label: "Mechanical" },
  { key: "done", label: "Done" },
];

export function outlineSections(rows: readonly ReviewOutlineItem[], query = ""): OutlineSection[] {
  const needle = query.trim().toLowerCase();
  const buckets = new Map<OutlineSectionKey, ReviewOutlineItem[]>();
  for (const row of rows) {
    if (needle && !`${row.path} ${row.reasons.join(" ")}`.toLowerCase().includes(needle)) continue;
    const key = sectionFor(row);
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
