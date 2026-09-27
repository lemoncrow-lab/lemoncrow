import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

type ToggleVariant = "pill" | "underline";
type ToggleTone = "neutral" | "purple" | "amber" | "emerald" | "cyan";
type ToggleSize = "xs" | "sm";

interface ToggleOption {
  value: string;
  label: ReactNode;
  title?: string;
}

interface ToggleGroupProps {
  options: ToggleOption[];
  value: string;
  onChange: (value: string) => void;
  variant?: ToggleVariant;
  tone?: ToggleTone;
  size?: ToggleSize;
  className?: string;
}

const ACTIVE_PILL: Record<ToggleTone, string> = {
  neutral: "border-neutral-500 bg-neutral-800 text-neutral-100",
  purple: "border-neutral-700 bg-neutral-800 text-neutral-100",
  amber: "border-amber-500/60 bg-amber-950/30 text-amber-200",
  emerald: "border-emerald-500/60 bg-emerald-950/30 text-emerald-200",
  cyan: "border-cyan-500/60 bg-cyan-950/30 text-cyan-200",
};

const ACTIVE_UNDERLINE: Record<ToggleTone, string> = {
  neutral: "border-neutral-500 bg-neutral-900/30 text-neutral-100",
  purple: "border-neutral-600 bg-neutral-900/30 text-neutral-100",
  amber: "border-amber-500 text-amber-300",
  emerald: "border-emerald-500 text-emerald-300",
  cyan: "border-cyan-500 text-cyan-300",
};

const SIZE_STYLES: Record<ToggleSize, string> = {
  xs: "h-8 px-2.5 text-[10px]",
  sm: "h-9 px-3 text-[11px]",
};

export function ToggleGroup({
  options,
  value,
  onChange,
  variant = "pill",
  tone = "neutral",
  size = "xs",
  className,
}: ToggleGroupProps) {
  return (
    <div
      className={cn(
        variant === "underline"
          ? "flex flex-wrap gap-0 border-b border-neutral-800"
          : "flex flex-wrap items-center gap-2",
        className
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            title={option.title}
            onClick={() => onChange(option.value)}
            className={cn(
              "rounded-md border font-medium transition-colors",
              SIZE_STYLES[size],
              variant === "underline"
                ? "border-x-0 border-t-0 border-b-2"
                : "tracking-tight",
              active
                ? variant === "underline"
                  ? ACTIVE_UNDERLINE[tone]
                  : ACTIVE_PILL[tone]
                : variant === "underline"
                  ? "border-transparent text-neutral-400 hover:text-neutral-300"
                  : "border-neutral-700 text-neutral-400 hover:text-neutral-300"
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
