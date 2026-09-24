import {
  forwardRef,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import { cn } from "../../lib/utils";

type ButtonVariant =
  | "outline"
  | "ghost"
  | "accent"
  | "amber"
  | "emerald"
  | "danger"
  | "link";
type ButtonSize = "xs" | "sm" | "icon";

const VARIANT_STYLES: Record<ButtonVariant, string> = {
  outline:
    "border-neutral-700 text-neutral-300 hover:border-neutral-500 hover:text-neutral-100",
  ghost:
    "border-transparent text-neutral-400 hover:border-neutral-700 hover:bg-neutral-900/40 hover:text-neutral-200",
  accent:
    "border-neutral-200 bg-neutral-100 text-neutral-950 hover:border-neutral-200 hover:bg-neutral-200 hover:text-neutral-950",
  amber:
    "border-amber-500/60 text-amber-200 hover:bg-amber-500/10 hover:text-amber-100",
  emerald:
    "border-emerald-700 text-emerald-200 hover:border-emerald-500 hover:text-emerald-100",
  danger: "border-red-700 text-red-200 hover:border-red-500 hover:text-red-100",
  link: "border-transparent px-0 py-0 text-neutral-400 hover:text-neutral-300",
};

const SIZE_STYLES: Record<ButtonSize, string> = {
  xs: "h-7 px-2 text-[10px]",
  sm: "h-8 px-3 text-[11px]",
  icon: "h-7 w-7 px-0 py-0 text-xs",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  active?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      className,
      variant = "outline",
      size = "sm",
      active = false,
      icon,
      children,
      type = "button",
      ...props
    },
    ref
  ) => (
    <button
      ref={ref}
      type={type}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-md border bg-transparent font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        SIZE_STYLES[size],
        VARIANT_STYLES[variant],
        active && variant === "outline" && "border-neutral-500 bg-neutral-800 text-neutral-100",
        className
      )}
      {...props}
    >
      {icon}
      {children}
    </button>
  )
);

Button.displayName = "Button";
