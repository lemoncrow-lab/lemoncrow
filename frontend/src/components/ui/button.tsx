import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import {
  forwardRef,
  type ButtonHTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from "react";

import { cn } from "../../lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "./tooltip";

export const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md border bg-transparent font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 aria-disabled:cursor-not-allowed aria-disabled:opacity-40 pointer-coarse:min-h-11",
  {
    variants: {
      variant: {
        /** Primary call to action (Review toolbar primary). */
        default:
          "border-neutral-200 bg-primary font-semibold text-primary-foreground hover:bg-neutral-200",
        /** Standard bordered control (Review toolbar button). */
        outline:
          "border-border/80 bg-secondary/55 text-secondary-foreground hover:bg-accent/80 hover:text-accent-foreground",
        /** Quiet control that gains a surface on hover (Review icon / compact actions). */
        ghost:
          "border-transparent text-neutral-400 hover:bg-accent/50 hover:text-accent-foreground",
        link: "border-transparent text-neutral-400 hover:text-neutral-300",
        destructive:
          "border-red-700 text-destructive-foreground hover:bg-destructive/10 hover:text-red-100",
        warning:
          "border-warning/60 text-warning-foreground hover:bg-warning/10 hover:text-amber-100",
        success:
          "border-emerald-700 text-success-foreground hover:bg-success/10 hover:text-emerald-100",
      },
      size: {
        xs: "h-7 px-2 text-caption",
        sm: "h-8 px-2.5 text-label",
        /** Inline row action (Review compact action). */
        compact: "h-7 gap-1 px-1.5 text-label",
        icon: "h-7 w-7 p-0 pointer-coarse:min-w-11",
      },
    },
    compoundVariants: [{ variant: "link", className: "h-auto p-0" }],
    defaultVariants: { variant: "outline", size: "sm" },
  }
);

export type ButtonVariant = NonNullable<VariantProps<typeof buttonVariants>["variant"]>;
export type ButtonSize = NonNullable<VariantProps<typeof buttonVariants>["size"]>;

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** Pressed/selected look for toggle-style outline buttons. */
  active?: boolean;
  icon?: ReactNode;
  /** Render the single child (e.g. a router Link) with button styling instead of a <button>. */
  asChild?: boolean;
  /** Accessible hover/focus hint; replaces `title=`. */
  tooltip?: ReactNode;
  tooltipSide?: "top" | "right" | "bottom" | "left";
  /**
   * Escape hatch for content-specific controls (diff gutters, map nodes, chat bubbles)
   * whose visuals are owned by the feature. Prefer a variant for anything chrome-like.
   */
  unstyled?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      className,
      variant,
      size,
      active = false,
      icon,
      children,
      type = "button",
      asChild = false,
      tooltip,
      tooltipSide,
      unstyled = false,
      disabled,
      onClick,
      ...props
    },
    ref
  ) => {
    const Comp = asChild ? Slot : "button";
    // A natively disabled button emits no pointer/focus events, so its hint could never show.
    // With a tooltip it stays focusable and hoverable as aria-disabled and swallows activation;
    // the element never changes shape, so refs held across enable/disable stay valid.
    const softDisabled = Boolean(tooltip) && Boolean(disabled) && !asChild;
    const button = (
      <Comp
        ref={ref}
        {...(asChild ? {} : { type })}
        {...props}
        data-active={active || undefined}
        disabled={softDisabled ? undefined : disabled}
        aria-disabled={softDisabled ? true : props["aria-disabled"]}
        onClick={softDisabled ? (event: MouseEvent<HTMLButtonElement>) => event.preventDefault() : onClick}
        className={cn(
          !unstyled && buttonVariants({ variant, size }),
          !unstyled && active && (variant ?? "outline") === "outline" && "bg-accent text-foreground",
          className
        )}
      >
        {asChild ? children : (
          <>
            {icon}
            {children}
          </>
        )}
      </Comp>
    );
    // Pass tooltip="" (not undefined) for a hint that comes and goes, so the tree keeps its shape.
    if (tooltip === undefined || tooltip === null) return button;
    return (
      <Tooltip>
        <TooltipTrigger asChild>{button}</TooltipTrigger>
        {tooltip ? <TooltipContent side={tooltipSide}>{tooltip}</TooltipContent> : null}
      </Tooltip>
    );
  }
);

Button.displayName = "Button";
