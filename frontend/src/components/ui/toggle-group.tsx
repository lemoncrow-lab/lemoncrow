import * as ToggleGroupPrimitive from "@radix-ui/react-toggle-group";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";
import { Tip } from "./tooltip";

type ToggleVariant = "pill" | "underline" | "segment";
type ToggleTone = "neutral" | "warning" | "success" | "info";
type ToggleSize = "xs" | "sm";

export interface ToggleOption {
  value: string;
  label: ReactNode;
  /** Hover/focus hint (rendered as a Tooltip). */
  title?: string;
  disabled?: boolean;
}

interface ToggleGroupProps {
  options: ToggleOption[];
  value: string;
  onChange: (value: string) => void;
  variant?: ToggleVariant;
  tone?: ToggleTone;
  size?: ToggleSize;
  className?: string;
  itemClassName?: string;
  disabled?: boolean;
  "aria-label"?: string;
}

const ACTIVE_PILL: Record<ToggleTone, string> = {
  neutral: "border-input bg-accent text-foreground",
  warning: "border-amber-500/30 bg-amber-950/30 text-amber-200",
  success: "border-emerald-500/30 bg-emerald-950/30 text-emerald-200",
  info: "border-cyan-500/30 bg-cyan-950/30 text-cyan-200",
};

const ACTIVE_UNDERLINE: Record<ToggleTone, string> = {
  neutral: "border-neutral-500 bg-muted/30 text-foreground",
  warning: "border-amber-500 text-amber-300",
  success: "border-emerald-500 text-emerald-300",
  info: "border-cyan-500 text-cyan-300",
};

const SIZE_STYLES: Record<ToggleSize, string> = {
  xs: "h-8 px-2.5 text-caption",
  sm: "h-9 px-3 text-label",
};

const ROOT_STYLES: Record<ToggleVariant, string> = {
  pill: "flex flex-wrap items-center gap-2",
  underline: "flex flex-wrap gap-0 border-b border-border",
  segment: "inline-flex overflow-hidden rounded-md border border-border/80 bg-background",
};

function itemStyles(variant: ToggleVariant, tone: ToggleTone, size: ToggleSize, active: boolean): string {
  if (variant === "segment") {
    return cn(
      "rounded-none px-2 py-1 text-label font-medium transition-colors focus-visible:z-10 disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:min-h-11",
      active ? "bg-accent text-foreground" : "text-neutral-400 hover:bg-muted hover:text-accent-foreground"
    );
  }
  return cn(
    "rounded-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:min-h-11",
    SIZE_STYLES[size],
    variant === "underline" ? "border-x-0 border-t-0 border-b-2" : "tracking-tight",
    active
      ? variant === "underline" ? ACTIVE_UNDERLINE[tone] : ACTIVE_PILL[tone]
      : variant === "underline"
        ? "border-transparent text-neutral-400 hover:text-neutral-300"
        : "border-input text-neutral-400 hover:text-neutral-300"
  );
}

/** Single-choice toggle (radio semantics, roving arrow-key focus). */
export function ToggleGroup({
  options,
  value,
  onChange,
  variant = "pill",
  tone = "neutral",
  size = "xs",
  className,
  itemClassName,
  disabled,
  "aria-label": ariaLabel,
}: ToggleGroupProps) {
  return (
    <ToggleGroupPrimitive.Root
      type="single"
      value={value}
      // Radix lets a single group be emptied by re-clicking the active item; a choice is required here.
      onValueChange={(next) => {
        if (next) onChange(next);
      }}
      disabled={disabled}
      aria-label={ariaLabel}
      className={cn(ROOT_STYLES[variant], className)}
    >
      {options.map((option) => (
        <Tip key={option.value} content={option.title}>
          <ToggleGroupPrimitive.Item
            value={option.value}
            disabled={option.disabled}
            className={cn(itemStyles(variant, tone, size, option.value === value), itemClassName)}
          >
            {option.label}
          </ToggleGroupPrimitive.Item>
        </Tip>
      ))}
    </ToggleGroupPrimitive.Root>
  );
}
