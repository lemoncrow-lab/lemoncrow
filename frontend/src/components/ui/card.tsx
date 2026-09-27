import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type CardTone =
  | "neutral"
  | "subtle"
  | "muted"
  | "amber"
  | "emerald"
  | "cyan"
  | "purple"
  | "red";

const CARD_TONES: Record<CardTone, string> = {
  neutral: "border-neutral-800 bg-neutral-950/60",
  subtle: "border-neutral-800 bg-neutral-950/70",
  muted: "border-neutral-800 bg-neutral-900/50",
  amber: "border-amber-900/40 bg-amber-950/20",
  emerald: "border-emerald-900/40 bg-emerald-950/20",
  cyan: "border-cyan-900/40 bg-cyan-950/20",
  purple: "border-neutral-800/70 bg-neutral-900/60",
  red: "border-red-900/40 bg-red-950/20",
};

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  tone?: CardTone;
}

export function Card({
  className,
  tone = "neutral",
  ...props
}: CardProps) {
  return (
    <div
      className={cn("rounded-lg border", CARD_TONES[tone], className)}
      {...props}
    />
  );
}

export function CardHeader({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4", className)} {...props} />;
}

export function CardContent({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4 pt-0", className)} {...props} />;
}

export function CardFooter({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4 pt-0", className)} {...props} />;
}
