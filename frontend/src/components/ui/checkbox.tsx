import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

/** Accessible native checkbox with the shared theme tokens. */
export const Checkbox = forwardRef<HTMLInputElement, Omit<InputHTMLAttributes<HTMLInputElement>, "type">>(
  ({ className, ...props }, ref) => (
    <input
      type="checkbox"
      ref={ref}
      className={cn(
        "h-4 w-4 shrink-0 rounded-sm border border-input accent-primary focus-visible:outline-1 focus-visible:outline-ring/40 disabled:opacity-40",
        className,
      )}
      {...props}
    />
  ),
);
Checkbox.displayName = "Checkbox";
