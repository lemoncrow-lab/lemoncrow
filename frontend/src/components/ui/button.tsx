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

/**
 * One recipe, named variants. `brand` (lemon) is the single primary action of a view: use it at most
 * once per screen. `solid` is the ink button for a strong but secondary action; `outline` is the
 * standard control; `ghost` and `link` are quiet. Sizes keep the dense control heights (28/32px).
 */
export const buttonVariants = cva(
  "inline-flex shrink-0 select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-md border font-semibold transition-colors duration-fast active:translate-y-px disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50 pointer-coarse:min-h-11",
  {
    variants: {
      variant: {
        /** The one primary call to action of a view. */
        brand: "border-brand-border bg-brand text-brand-foreground hover:bg-brand-hover",
        /** Ink fill: a strong secondary action (never two next to each other). */
        solid: "border-primary bg-primary text-primary-foreground hover:bg-primary/90",
        /** Standard bordered control (the default). */
        outline: "border-input bg-transparent text-foreground hover:bg-accent",
        /** Quiet control that gains a surface on hover (icon and row actions). */
        ghost: "border-transparent bg-transparent font-medium text-subtle hover:bg-accent hover:text-foreground",
        link: "h-auto border-transparent p-0 font-medium text-foreground underline decoration-foreground/40 underline-offset-4 hover:decoration-foreground",
        destructive: "border-p0-line bg-p0-bg text-p0 hover:bg-p0-bg/70",
        warning: "border-p1-line bg-p1-bg text-p1 hover:bg-p1-bg/70",
        success: "border-ok-line bg-ok-bg text-ok hover:bg-ok-bg/70",
      },
      size: {
        xs: "h-7 px-2 text-meta",
        sm: "h-8 px-3 text-meta",
        /** Inline row action (Review compact action). */
        compact: "h-7 gap-1 px-1.5 text-meta",
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
