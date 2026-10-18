import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";

import { cn } from "../../lib/utils";

export const cardVariants = cva("rounded-lg border", {
  variants: {
    tone: {
      neutral: "border-border bg-background/60",
      subtle: "border-border bg-background/70",
      muted: "border-border bg-muted/50",
      info: "border-cyan-900/50 bg-background/60",
      success: "border-emerald-900/50 bg-background/60",
      warning: "border-amber-900/50 bg-background/60",
      destructive: "border-red-900/50 bg-background/60",
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
