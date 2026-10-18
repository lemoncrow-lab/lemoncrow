// Shared status → tone map. Merges sessions/StatusBadge's STATUS_MAP /
// STATUS_DOT_MAP with the ad hoc statusTone() helpers duplicated in Swarm
// and Workflow. Unknown statuses always fall back to "neutral".

export type StatusTone =
  | "success"
  | "warning"
  | "destructive"
  | "paused"
  | "info"
  | "neutral";

const STATUS_TONE: Record<string, StatusTone> = {
  success: "success",
  completed: "success",
  failed: "destructive",
  error: "destructive",
  stopped: "destructive",
  partial: "warning",
  pending: "warning",
  awaiting_review: "warning",
  paused: "paused",
  review_rejected: "paused",
  applying: "paused",
  running: "info",
};

export function statusTone(status: string): StatusTone {
  return STATUS_TONE[status] ?? "neutral";
}

/** Tone for a Chip/Badge, which has no dedicated "paused" colour. */
export function statusChipTone(status: string): Exclude<StatusTone, "paused"> {
  const tone = statusTone(status);
  return tone === "paused" ? "neutral" : tone;
}

const TONE_BADGE_CLASS: Record<StatusTone, string> = {
  success: "text-emerald-300 border-emerald-500/30 bg-emerald-500/5",
  warning: "text-amber-300 border-amber-500/30 bg-amber-500/5",
  destructive: "text-red-300 border-red-500/30 bg-red-500/5",
  paused: "text-violet-300 border-violet-500/30 bg-violet-500/5",
  info: "text-sky-300 border-sky-500/30 bg-sky-500/5",
  neutral: "text-neutral-400 border-neutral-500/30 bg-neutral-500/5",
};

const TONE_DOT_CLASS: Record<StatusTone, string> = {
  success: "bg-emerald-500",
  warning: "bg-amber-500",
  destructive: "bg-red-500",
  paused: "bg-violet-500",
  info: "bg-sky-500",
  neutral: "bg-neutral-500",
};

export function statusBadgeClass(status: string): string {
  return TONE_BADGE_CLASS[statusTone(status)];
}

export function statusDotClass(status: string): string {
  return TONE_DOT_CLASS[statusTone(status)];
}
