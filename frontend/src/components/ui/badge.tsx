import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";

import { cn } from "../../lib/utils";

/**
 * One badge shape everywhere: 20px, mono, uppercase, 11px. Colour has one job per tone.
 * `p0`/`p1`/`p2` are the severity badges (they come first in a row); `info`, `success`, `warning`
 * and `destructive` are the generic status tones and share the same tokens. `new` is ink (never
 * lemon), `ai` is only for AI or agent attribution.
 */
export const badgeVariants = cva(
  "inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 font-mono text-tag font-medium uppercase leading-none",
  {
    variants: {
      tone: {
        neutral: "border-border bg-transparent text-muted-foreground",
        /** Filled quiet chip (Review pill). */
        secondary: "border-border bg-muted text-muted-foreground",
        info: "border-p2-line bg-p2-bg text-p2",
        success: "border-ok-line bg-ok-bg text-ok",
        warning: "border-p1-line bg-p1-bg text-p1",
        destructive: "border-p0-line bg-p0-bg text-p0",
        /** Must fix. */
        p0: "border-p0-line bg-p0-bg text-p0",
        /** Should fix. */
        p1: "border-p1-line bg-p1-bg text-p1",
        /** Optional. */
        p2: "border-p2-line bg-p2-bg text-p2",
        new: "border-foreground bg-foreground text-background",
        carried: "border-border bg-transparent text-muted-foreground",
        fixed: "border-ok-line bg-ok-bg text-ok",
        /** AI or agent authorship, attribution chips only. */
        ai: "border-ai-line bg-ai-bg text-ai",
      },
    },
    defaultVariants: { tone: "neutral" },
  }
);

export type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

interface BadgeProps extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
