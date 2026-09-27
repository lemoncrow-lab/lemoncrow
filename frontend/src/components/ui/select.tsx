import { forwardRef, type SelectHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type SelectSize = "xs" | "sm";

const SIZE_STYLES: Record<SelectSize, string> = {
  xs: "h-8 px-2.5 text-[10px]",
  sm: "h-9 px-3 text-xs",
};

interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  uiSize?: SelectSize;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, uiSize = "sm", ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        "rounded-md border border-neutral-800/80 bg-neutral-900/55 text-neutral-200 outline-none transition-colors hover:border-neutral-700 focus:border-neutral-600 focus-visible:outline-none",
        SIZE_STYLES[uiSize],
        className
      )}
      {...props}
    />
  )
);

Select.displayName = "Select";
