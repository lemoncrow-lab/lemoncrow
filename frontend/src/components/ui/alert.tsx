import { cva, type VariantProps } from "class-variance-authority";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

const alertVariants = cva("rounded-lg border p-4 text-body", {
  variants: {
    tone: {
      neutral: "border-border bg-card text-subtle",
      info: "border-p2-line bg-p2-bg text-p2",
      success: "border-ok-line bg-ok-bg text-ok",
      warning: "border-p1-line bg-p1-bg text-p1",
      destructive: "border-p0-line bg-p0-bg text-p0",
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
