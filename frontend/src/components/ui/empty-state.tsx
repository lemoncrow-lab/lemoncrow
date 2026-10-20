import type { ReactNode } from "react";

import { Hunk, HunkHead, HunkLabel } from "./hunk";

interface EmptyStateProps {
  /** A 20px lucide icon (`size-5`), optional. */
  icon?: ReactNode;
  title: ReactNode;
  /** One sentence: why it is empty and what to do. */
  description?: ReactNode;
  /** Header strip text (`@@ NO REVIEWS @@`). Omit for a quiet frame without a strip. */
  label?: string;
  /** At most one action. */
  action?: ReactNode;
  /** False inside an existing Hunk or panel: the same content without a second frame. */
  frame?: boolean;
  className?: string;
}

/** The one empty-state recipe: a Hunk with an optional strip, icon, one-line title, one sentence, one action. */
export function EmptyState({ icon, title, description, label, action, frame = true, className }: EmptyStateProps) {
  const body = (
    <div className="flex flex-col items-center gap-3 px-6 py-10 text-center">
      {icon && <div className="text-muted-foreground [&_svg]:size-5">{icon}</div>}
      <p className="text-heading font-semibold text-foreground">{title}</p>
      {description && <div className="max-w-[44ch] text-meta text-muted-foreground">{description}</div>}
      {action}
    </div>
  );
  if (!frame) return <div className={className}>{body}</div>;
  return (
    <Hunk className={className}>
      {label && (
        <HunkHead>
          <HunkLabel>{label}</HunkLabel>
        </HunkHead>
      )}
      {body}
    </Hunk>
  );
}
