import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type BadgeTone =
  | "neutral"
  | "amber"
  | "cyan"
  | "emerald"
  | "violet"
  | "red"
  | "purple";

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "border-neutral-700 text-neutral-400",
  amber: "border-amber-700/50 text-amber-300",
  cyan: "border-cyan-900/60 text-cyan-300",
  emerald: "border-emerald-700/50 text-emerald-300",
  violet: "border-neutral-700 text-neutral-400",
  red: "border-red-700/50 text-red-300",
  purple: "border-neutral-700 text-neutral-400",
};

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
}

export function Badge({
  className,
  tone = "neutral",
  ...props
}: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium",
        BADGE_TONES[tone],
        className
      )}
      {...props}
    />
  );
}
