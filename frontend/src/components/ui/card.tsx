import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";

import { cn } from "../../lib/utils";

/** Plain hairline card. For a framed object with a header strip use Hunk (./hunk). */
export const cardVariants = cva("rounded-lg border", {
  variants: {
    tone: {
      neutral: "border-border bg-card",
      subtle: "border-border bg-card",
      muted: "border-border bg-muted",
      info: "border-p2-line bg-card",
      success: "border-ok-line bg-card",
      warning: "border-p1-line bg-card",
      destructive: "border-p0-line bg-card",
    },
  },
  defaultVariants: { tone: "neutral" },
});

export type CardTone = NonNullable<VariantProps<typeof cardVariants>["tone"]>;

interface CardProps extends HTMLAttributes<HTMLDivElement>, VariantProps<typeof cardVariants> {}

export function Card({ className, tone, ...props }: CardProps) {
  return <div className={cn(cardVariants({ tone }), className)} {...props} />;
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4", className)} {...props} />;
}

export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return <h3 className={cn("text-heading font-semibold text-foreground", className)} {...props} />;
}

export function CardDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-body text-muted-foreground", className)} {...props} />;
}

export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4 pt-0", className)} {...props} />;
}

export function CardFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4 pt-0", className)} {...props} />;
}
