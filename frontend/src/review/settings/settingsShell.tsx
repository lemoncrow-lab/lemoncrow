import { ArrowLeft } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "../../components/ui/button";

/**
 * One titled section of a Settings page, shared with the organization page (P7-3): a framed card with a plain heading.
 * Not a diff hunk, so no `@@` label (review-23.4).
 */
export function SettingsRow({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="hunk scroll-mt-4">
      <div className="border-b border-border px-4 py-3"><h2 id={`${id}-title`} className="text-label font-semibold text-foreground">{title}</h2></div>
      <div className="space-y-3 p-4">{children}</div>
    </section>
  );
}

/** The Settings bar: Back, then the page's name. */
export function SettingsBar({ name, onBack }: { name: string; onBack: () => void }) {
  return (
    <header className="flex h-[52px] items-center gap-2 border-b border-border">
      <Button variant="ghost" size="sm" aria-label="Back, go back" onClick={onBack}>
        <ArrowLeft size={16} aria-hidden="true" />
        Back
      </Button>
      <span className="text-label font-semibold">{name}</span>
    </header>
  );
}
