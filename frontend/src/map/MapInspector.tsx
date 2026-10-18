import type { ComponentProps, ReactNode } from "react";
import { X } from "lucide-react";

import { Button } from "../components/ui/button";
import { cn } from "../lib/utils";

/**
 * The inspector vocabulary Code and the Review Map share: a facts header for the
 * subject, titled sections (Used by / Uses, activity, changes, ...), and the
 * row every link or member list is made of. Each lens decides what goes in the
 * sections; the parts and their look are the same everywhere.
 */

export function InspectorSection({ title, aside, children }: { title: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="border-t border-border/70 px-4 py-3.5">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="text-label font-semibold text-neutral-200">{title}</h3>
        {aside ? <span className="text-caption tabular-nums text-muted-foreground">{aside}</span> : null}
      </div>
      {children}
    </section>
  );
}

/** Facts about the selected subject: what it is, where it lives, then status and actions as children. */
export function InspectorFacts({
  glyph,
  kicker,
  title,
  titleHint,
  location,
  headerAction,
  onClose,
  closeLabel = "Close inspector",
  children,
}: {
  glyph: ReactNode;
  /** Kind of subject, when the title alone does not say it. */
  kicker?: ReactNode;
  title: ReactNode;
  titleHint?: string;
  location?: string;
  headerAction?: ReactNode;
  onClose?: () => void;
  closeLabel?: string;
  children?: ReactNode;
}) {
  return (
    <div className="sticky top-0 z-10 border-b border-border/70 bg-background/95 px-4 pb-3.5 pt-4 backdrop-blur">
      <div className="flex items-start gap-2.5">
        <div className="mt-0.5">{glyph}</div>
        <div className="min-w-0 flex-1">
          {kicker && <div className="text-micro font-semibold uppercase tracking-widest text-muted-foreground">{kicker}</div>}
          <h2 className="truncate text-title font-semibold text-foreground" title={titleHint}>{title}</h2>
          {location && <div className="mt-0.5 truncate font-mono text-caption text-muted-foreground" title={location}>{location}</div>}
        </div>
        {headerAction}
        {onClose && <Button variant="ghost" size="icon" onClick={onClose} aria-label={closeLabel}><X size={13} /></Button>}
      </div>
      {children}
    </div>
  );
}

/** One clickable row of a relationship, member, or queue list. */
export function InspectorRow({ className, ...props }: Omit<ComponentProps<typeof Button>, "unstyled">) {
  return (
    <Button
      unstyled
      type="button"
      className={cn("flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1.5 text-left transition-colors hover:bg-muted", className)}
      {...props}
    />
  );
}

/** The list InspectorRows sit in: rows bleed into the section padding so their hover fills it. */
export function InspectorList({ className, ...props }: ComponentProps<"ul">) {
  return <ul className={cn("-mx-1.5 space-y-0.5", className)} {...props} />;
}
