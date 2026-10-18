import { cva, type VariantProps } from "class-variance-authority";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

const alertVariants = cva("rounded-lg border p-4 text-sm", {
  variants: {
    tone: {
      neutral: "border-border bg-background/60 text-neutral-300",
      info: "border-cyan-900/40 bg-cyan-950/20 text-cyan-100",
      success: "border-emerald-900/40 bg-emerald-950/20 text-emerald-100",
      warning: "border-amber-900/40 bg-amber-950/20 text-amber-100",
      destructive: "border-red-900/40 bg-red-950/20 text-red-100",
    },
  },
  defaultVariants: { tone: "neutral" },
});

interface AlertProps extends VariantProps<typeof alertVariants> {
  title?: ReactNode;
  description?: ReactNode;
  className?: string;
}

export function Alert({ tone, title, description, className }: AlertProps) {
  return (
    <div className={cn(alertVariants({ tone }), className)}>
      {title && <div className="font-semibold">{title}</div>}
      {description && (
        <div className={cn(Boolean(title) && "mt-1", "leading-relaxed")}>
          {description}
        </div>
      )}
    </div>
  );
}
