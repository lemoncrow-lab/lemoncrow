import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";

import { cn } from "../../lib/utils";

export const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-caption font-medium",
  {
    variants: {
      tone: {
        neutral: "border-input text-neutral-400",
        /** Filled quiet chip (Review pill). */
        secondary: "border-border/70 bg-muted/60 px-1.5 text-neutral-400",
        info: "border-cyan-900/60 text-cyan-300",
        success: "border-emerald-700/50 text-emerald-300",
        warning: "border-amber-700/50 text-amber-300",
        destructive: "border-red-700/50 text-red-300",
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
