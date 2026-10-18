import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

/** Accessible native checkbox with the shared theme tokens. */
export const Checkbox = forwardRef<HTMLInputElement, Omit<InputHTMLAttributes<HTMLInputElement>, "type">>(
  ({ className, ...props }, ref) => (
    <input
      type="checkbox"
      ref={ref}
      className={cn(
        "h-4 w-4 shrink-0 rounded-sm border border-input accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:opacity-40",
        className,
      )}
      {...props}
    />
  ),
);
Checkbox.displayName = "Checkbox";
