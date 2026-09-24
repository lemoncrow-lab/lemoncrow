import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

type AlertTone = "neutral" | "info" | "success" | "warning" | "danger";

const ALERT_STYLES: Record<AlertTone, string> = {
  neutral: "border-neutral-800 bg-neutral-950/60 text-neutral-300",
  info: "border-cyan-900/40 bg-cyan-950/20 text-cyan-100",
  success: "border-emerald-900/40 bg-emerald-950/20 text-emerald-100",
  warning: "border-amber-900/40 bg-amber-950/20 text-amber-100",
  danger: "border-red-900/40 bg-red-950/20 text-red-100",
};

interface AlertProps {
  tone?: AlertTone;
  title?: ReactNode;
  description?: ReactNode;
  className?: string;
}

export function Alert({
  tone = "neutral",
  title,
  description,
  className,
}: AlertProps) {
  return (
    <div className={cn("rounded-lg border p-4 text-sm", ALERT_STYLES[tone], className)}>
      {title && <div className="font-semibold">{title}</div>}
      {description && (
        <div className={cn(Boolean(title) && "mt-1", "leading-relaxed")}>
          {description}
        </div>
      )}
    </div>
  );
}
