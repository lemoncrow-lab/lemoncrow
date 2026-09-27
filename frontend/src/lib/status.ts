// Shared status → tone map. Merges sessions/StatusBadge's STATUS_MAP /
// STATUS_DOT_MAP with the ad hoc statusTone() helpers duplicated in Swarm
// and Workflow. Unknown statuses always fall back to "neutral".

export type StatusTone =
  | "emerald"
  | "amber"
  | "red"
  | "purple"
  | "cyan"
  | "neutral";

const STATUS_TONE: Record<string, StatusTone> = {
  success: "emerald",
  completed: "emerald",
  failed: "red",
  error: "red",
  stopped: "red",
  partial: "amber",
  pending: "amber",
  awaiting_review: "amber",
  paused: "purple",
  review_rejected: "purple",
  applying: "purple",
  running: "cyan",
};

export function statusTone(status: string): StatusTone {
  return STATUS_TONE[status] ?? "neutral";
}

const TONE_BADGE_CLASS: Record<StatusTone, string> = {
  emerald: "text-emerald-300 border-emerald-500/30 bg-emerald-500/5",
  amber: "text-amber-300 border-amber-500/30 bg-amber-500/5",
  red: "text-red-300 border-red-500/30 bg-red-500/5",
  purple: "text-violet-300 border-violet-500/30 bg-violet-500/5",
  cyan: "text-sky-300 border-sky-500/30 bg-sky-500/5",
  neutral: "text-neutral-400 border-neutral-500/30 bg-neutral-500/5",
};

const TONE_DOT_CLASS: Record<StatusTone, string> = {
  emerald: "bg-emerald-500",
  amber: "bg-amber-500",
  red: "bg-red-500",
  purple: "bg-violet-500",
  cyan: "bg-sky-500",
  neutral: "bg-neutral-500",
};

export function statusBadgeClass(status: string): string {
  return TONE_BADGE_CLASS[statusTone(status)];
}

export function statusDotClass(status: string): string {
  return TONE_DOT_CLASS[statusTone(status)];
}
