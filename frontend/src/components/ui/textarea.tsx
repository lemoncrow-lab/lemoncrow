import { forwardRef, type TextareaHTMLAttributes } from "react";
import { cn } from "../../lib/utils";
import { fieldStyles } from "./input";

type TextareaSize = "xs" | "sm";

const SIZE_STYLES: Record<TextareaSize, string> = {
  xs: "min-h-20 px-2.5 py-2 text-label",
  sm: "min-h-24 px-3 py-2.5 text-xs",
};

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  uiSize?: TextareaSize;
  /** Preserve caller-owned styling while still routing native textarea ownership through the UI layer. */
  unstyled?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, uiSize = "sm", unstyled = false, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        !unstyled && "w-full resize-y",
        !unstyled && fieldStyles,
        !unstyled && SIZE_STYLES[uiSize],
        className
      )}
      {...props}
    />
  )
);

Textarea.displayName = "Textarea";
