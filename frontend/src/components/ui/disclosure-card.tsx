import * as CollapsiblePrimitive from "@radix-ui/react-collapsible";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

export const Collapsible = CollapsiblePrimitive.Root;
export const CollapsibleTrigger = CollapsiblePrimitive.Trigger;
export const CollapsibleContent = CollapsiblePrimitive.Content;

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
    <Collapsible
      open={open}
      onOpenChange={onToggle}
      className={cn(
        "overflow-hidden rounded-lg border border-border/70 bg-muted/50 transition-colors",
        className
      )}
    >
      <CollapsibleTrigger
        className={cn(
          "w-full px-4 py-3 text-left transition-colors hover:bg-accent/50",
          triggerClassName
        )}
      >
        {header}
      </CollapsibleTrigger>
      <CollapsibleContent
        className={cn(
          "border-t border-border bg-background/50 px-4 py-3",
          contentClassName
        )}
      >
        {children}
      </CollapsibleContent>
    </Collapsible>
  );
}
