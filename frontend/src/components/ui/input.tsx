import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type InputSize = "xs" | "sm";

/** Shared field chrome for Input, Textarea and the Select trigger. */
export const fieldStyles =
  "rounded-md border border-input bg-secondary/90 text-neutral-200 outline-none transition-[color,border-color,box-shadow] placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/15 disabled:cursor-not-allowed disabled:opacity-40";

const SIZE_STYLES: Record<InputSize, string> = {
  xs: "h-8 px-2.5 text-label",
  sm: "h-9 px-3 text-xs",
};

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  uiSize?: InputSize;
  /** Feature-owned visuals (e.g. inline code editors); prefer the default field chrome. */
  unstyled?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, uiSize = "sm", unstyled = false, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        !unstyled && "w-full",
        !unstyled && fieldStyles,
        !unstyled && SIZE_STYLES[uiSize],
        className
      )}
      {...props}
    />
  )
);

Input.displayName = "Input";
