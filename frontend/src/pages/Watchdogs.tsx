import { useEffect, useState } from "react";
import { api, type WatchdogLibraryEntry } from "../api";
import { Chip, SectionHeader } from "../components/WorkbenchUI";

// Watchdogs are not wired into live agent sessions yet: the only runtime that
// runs them (ContextRuntime.run) has no production caller, so tuning weights
// here would change nothing. Until that lands the page is a read-only preview
// of the planned watchdog types, with no save path.
export default function Watchdogs() {
  const [library, setLibrary] = useState<WatchdogLibraryEntry[]>([]);

  useEffect(() => {
    api
      .watchdogConfig()
      .then((config) => setLibrary(config.library))
      .catch(() => setLibrary([]));
  }, []);

  return (
    <div className="space-y-4">
      <section className="rounded-lg border border-border/70 bg-background/60 p-4">
        <SectionHeader
          title="Watchdogs"
          description="Watchdogs will watch live agent sessions for retry loops, skipped verification, and risky actions, then nudge the agent to change course. They are not running yet, so there is nothing to configure."
          action={<Chip tone="info">Coming soon</Chip>}
        />

        {library.length > 0 && (
          <div className="mt-4 space-y-2">
            <div className="text-caption text-muted-foreground">
              Planned watchdogs
            </div>
            {library.map((monitor) => (
              <div
                key={monitor.key}
                className="rounded-lg border border-border/70 bg-background/60 p-3"
              >
                <div className="flex items-center gap-2">
                  <div className="text-sm font-semibold text-foreground">
                    {monitor.title}
                  </div>
                  <Chip
                    tone={monitor.severity === "high" ? "warning" : "neutral"}
                  >
                    {monitor.severity}
                  </Chip>
                </div>
                <div className="mt-1 text-xs text-neutral-400">
                  {monitor.description}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
