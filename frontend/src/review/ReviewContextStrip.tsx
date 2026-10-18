import { Button as UiButton } from "../components/ui/button";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

export type ContextSignalTone = "rose" | "amber" | "sky" | "neutral";

/**
 * One piece of secondary Reader context: historical/newer Snapshot state,
 * source movement, feedback delivery, exceptional readiness, recovery.
 *
 * The Reader orders signals by severity. The first becomes the strip's row;
 * the rest collapse into chips on the same row, so context never stacks into
 * several full-width bars above the diff.
 */
export interface ContextSignal {
  id: string;
  tone: ContextSignalTone;
  icon: LucideIcon;
  label: ReactNode;
  detail?: ReactNode;
  /** Plain-text label for the chip form and for assistive technology. */
  chipLabel: string;
  role?: "alert" | "status";
  testId?: string;
  actions?: ReactNode;
  /** Chip click when this signal is not the primary one. */
  onActivate?: () => void;
  /** Rendered in place of a chip button, e.g. a disclosure popover. */
  chip?: ReactNode;
}

const ROW_TONE: Record<ContextSignalTone, string> = {
  rose: "border-rose-900/60 bg-rose-950/35 text-rose-200",
  amber: "border-amber-700/65 bg-amber-950/55 text-amber-100",
  sky: "border-sky-900/60 bg-sky-950/25 text-sky-200",
  neutral: "border-border/80 bg-muted/45 text-neutral-300",
};

// Full-strength tones: a translucent tint drops below 4.5:1 on the light theme's tinted strip.
const DETAIL_TONE: Record<ContextSignalTone, string> = {
  rose: "text-rose-300",
  amber: "text-amber-300",
  sky: "text-sky-300",
  neutral: "text-muted-foreground",
};

const CHIP_TONE: Record<ContextSignalTone, string> = {
  rose: "border-rose-800/60 text-rose-200 hover:bg-rose-950/30",
  amber: "border-amber-800/60 text-amber-200 hover:bg-amber-950/30",
  sky: "border-sky-800/60 text-sky-200 hover:bg-sky-950/30",
  neutral: "border-neutral-700/70 text-neutral-300 hover:bg-muted/60",
};

export function ContextChip({ signal }: { signal: ContextSignal }) {
  if (signal.chip) return <>{signal.chip}</>;
  const Icon = signal.icon;
  return (
    <UiButton unstyled
      type="button"
      onClick={signal.onActivate}
      disabled={!signal.onActivate}
      className={`review-context-chip ${CHIP_TONE[signal.tone]}`}
      data-testid={signal.testId}
    >
      <Icon size={11} aria-hidden="true" />
      <span className="truncate">{signal.chipLabel}</span>
    </UiButton>
  );
}

export default function ReviewContextStrip({ signals }: { signals: ContextSignal[] }) {
  if (signals.length === 0) return null;
  const [primary, ...rest] = signals;
  const Icon = primary.icon;
  const live = primary.role === "alert" ? "assertive" : "polite";
  return (
    <div
      className={`review-context-strip ${ROW_TONE[primary.tone]}`}
      role={primary.role ?? "status"}
      aria-live={live}
      data-testid={primary.testId ?? "review-context-strip"}
      data-signal={primary.id}
      data-tone={primary.tone}
    >
      <Icon size={13} strokeWidth={1.9} aria-hidden="true" className="shrink-0 opacity-85" />
      <span className="review-context-label">{primary.label}</span>
      {primary.detail ? (
        <span className={`review-context-detail ${DETAIL_TONE[primary.tone]}`}>{primary.detail}</span>
      ) : (
        <span className="flex-1" aria-hidden="true" />
      )}
      {rest.length > 0 && (
        <span className="review-context-chips" aria-label="More Review context">
          {rest.map((signal) => <ContextChip key={signal.id} signal={signal} />)}
        </span>
      )}
      {primary.actions && <span className="review-context-actions">{primary.actions}</span>}
    </div>
  );
}
