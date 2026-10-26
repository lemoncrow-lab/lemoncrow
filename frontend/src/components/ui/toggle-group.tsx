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
  warning: "border-p1-line bg-p1-bg text-p1",
  success: "border-ok-line bg-ok-bg text-ok",
  info: "border-p2-line bg-p2-bg text-p2",
};

// The underline is the shared indicator token (ink on light, lemon on dark); the text changes too,
// so the active tab never relies on colour alone.
const ACTIVE_UNDERLINE: Record<ToggleTone, string> = {
  neutral: "text-foreground shadow-indicator",
  warning: "text-p1 shadow-indicator",
  success: "text-ok shadow-indicator",
  info: "text-p2 shadow-indicator",
};

const SIZE_STYLES: Record<ToggleSize, string> = {
  xs: "h-8 px-2.5 text-meta",
  sm: "h-9 px-3 text-ui",
};

const ROOT_STYLES: Record<ToggleVariant, string> = {
  pill: "flex flex-wrap items-center gap-2",
  underline: "flex flex-wrap gap-1 border-b border-border",
  segment: "inline-flex gap-0.5 rounded-md border border-border bg-muted p-0.5",
};

function itemStyles(variant: ToggleVariant, tone: ToggleTone, size: ToggleSize, active: boolean): string {
  if (variant === "segment") {
    return cn(
      "h-6 rounded-sm border px-2 text-meta font-medium transition-colors duration-fast focus-visible:z-10 disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11",
      active ? "border-border bg-card text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"
    );
  }
  return cn(
    "font-medium transition-colors duration-fast disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11",
    SIZE_STYLES[size],
    variant === "underline" ? "relative -mb-px rounded-none border-0" : "rounded-md border",
    active
      ? variant === "underline" ? ACTIVE_UNDERLINE[tone] : ACTIVE_PILL[tone]
      : variant === "underline"
        ? "text-muted-foreground hover:text-foreground"
        : "border-input text-muted-foreground hover:text-foreground"
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
