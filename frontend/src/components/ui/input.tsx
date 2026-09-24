import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type InputSize = "xs" | "sm";

const SIZE_STYLES: Record<InputSize, string> = {
  xs: "h-8 px-2.5 text-[11px]",
  sm: "h-9 px-3 text-xs",
};

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  uiSize?: InputSize;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, uiSize = "sm", ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "w-full rounded-md border border-neutral-800/80 bg-neutral-900/55 text-neutral-200 outline-none transition-colors placeholder:text-neutral-600 hover:border-neutral-700 focus:border-neutral-600 focus-visible:outline-none",
        SIZE_STYLES[uiSize],
        className
      )}
      {...props}
    />
  )
);

Input.displayName = "Input";
