import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type InputSize = "xs" | "sm";

/** Shared field chrome for Input, Textarea and the Select trigger. */
export const fieldStyles =
  "rounded-md border border-input bg-card text-foreground outline-none transition-colors duration-fast placeholder:text-muted-foreground focus-visible:border-ring focus-visible:outline-1 focus-visible:outline-offset-0 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-p0 aria-[invalid=true]:focus-visible:outline-p0";

// 16px under a coarse pointer keeps iOS from zooming into the field.
const SIZE_STYLES: Record<InputSize, string> = {
  xs: "h-8 px-2.5 text-meta pointer-coarse:text-copy",
  sm: "h-9 px-3 text-ui pointer-coarse:text-copy",
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
