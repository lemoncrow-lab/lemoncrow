import type { ReactNode } from "react";
import { Card } from "./card";
import { cn } from "../../lib/utils";

interface EmptyStateProps {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  className?: string;
}

export function EmptyState({
  icon,
  title,
  description,
  className,
}: EmptyStateProps) {
  return (
    <Card className={cn("p-6 text-center text-sm text-neutral-400", className)}>
      {icon && <div className="mb-3 text-2xl">{icon}</div>}
      <div className="font-semibold text-neutral-200">{title}</div>
      {description && <div className="mt-1 text-neutral-400">{description}</div>}
    </Card>
  );
}
