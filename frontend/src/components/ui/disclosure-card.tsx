import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

interface DisclosureCardProps {
  open: boolean;
  onToggle: () => void;
  header: ReactNode;
  children?: ReactNode;
  className?: string;
  triggerClassName?: string;
  contentClassName?: string;
}

export function DisclosureCard({
  open,
  onToggle,
  header,
  children,
  className,
  triggerClassName,
  contentClassName,
}: DisclosureCardProps) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-lg border border-neutral-800/70 bg-neutral-900/50 transition-colors",
        className
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className={cn(
          "w-full px-4 py-3 text-left transition-colors hover:bg-neutral-800/50",
          triggerClassName
        )}
      >
        {header}
      </button>
      {open && (
        <div
          className={cn(
            "border-t border-neutral-800 bg-neutral-950/50 px-4 py-3",
            contentClassName
          )}
        >
          {children}
        </div>
      )}
    </div>
  );
}
