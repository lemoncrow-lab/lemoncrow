import { useEffect, useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Circle } from "lucide-react";

import {
  fetchLocalIntegrations,
  type LocalIntegrationHost,
  type LocalIntegrations,
} from "../control/controlApi";
import { Chip, PageFrame } from "../components/WorkbenchUI";
import { Button as UiButton } from "../components/ui/button";
import { useRuntimeCapabilities } from "../lib/RuntimeCapabilities";

function isComingSoon(host: LocalIntegrationHost) {
  return host.support === "coming_soon";
}

function hostTone(host: LocalIntegrationHost) {
  if (host.configured) return "text-emerald-300 border-emerald-900/60 bg-emerald-950/20";
  if (host.detected) return "text-amber-300 border-amber-900/60 bg-amber-950/20";
  return "text-muted-foreground border-border bg-muted/40";
}

function hostLabel(host: LocalIntegrationHost) {
  const components = host.components ?? [];
  const present = components.filter((component) => component.present).length;
  if (host.configured && components.length > 0 && present > 0) {
    return `${present}/${components.length} surfaces present`;
  }
  if (host.configured) return "Installer reports configured";
  if (host.detected) return "Detected · not configured";
  return "Not detected";
}

function IntegrationDetails({ host }: { host: LocalIntegrationHost }) {
  const components = host.components ?? [];
  const presentCount = components.filter((component) => component.present).length;

  if (isComingSoon(host)) {
    return (
      <div className="border-t border-border/70 bg-muted/20 px-3 py-3 text-caption leading-5 text-muted-foreground">
        This host is not offered by the current LemonCrow setup wizard. It is shown here for
        roadmap visibility only; LemonCrow does not claim an active integration for it yet.
      </div>
    );
  }

  return (
    <div className="border-t border-border/70 bg-muted/20 px-3 py-3">
      <div className="mb-3 grid gap-2 text-caption sm:grid-cols-2">
        <div className="rounded-md border border-border/70 bg-background/50 px-2.5 py-2">
          <div className="text-micro font-semibold uppercase tracking-wide text-muted-foreground">
            Host CLI
          </div>
          <div className="mt-1 text-neutral-200">
            {host.detected ? "Detected on this machine" : "Not detected on PATH"}
          </div>
        </div>
        <div className="rounded-md border border-border/70 bg-background/50 px-2.5 py-2">
          <div className="text-micro font-semibold uppercase tracking-wide text-muted-foreground">
            Live integration evidence
          </div>
          <div className="mt-1 text-neutral-200">
            {components.length > 0
              ? `${presentCount}/${components.length} surfaces present`
              : "No surfaces detected"}
          </div>
        </div>
      </div>

      {components.length > 0 ? (
        <div className="space-y-1.5" aria-label={`${host.label} integration surfaces`}>
          {components.map((component) => (
            <div
              key={component.id}
              className="flex items-start gap-2.5 rounded-md border border-border/60 bg-background/40 px-2.5 py-2"
            >
              {component.present ? (
                <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-emerald-400" />
              ) : (
                <Circle size={13} className="mt-0.5 shrink-0 text-muted-foreground" />
              )}
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                  <div className="text-caption font-medium text-neutral-200">{component.label}</div>
                  <span
                    className={
                      component.present
                        ? "text-micro font-semibold uppercase tracking-wide text-emerald-400"
                        : "text-micro font-semibold uppercase tracking-wide text-muted-foreground"
                    }
                  >
                    {component.present ? "Present" : "Missing"}
                  </span>
                </div>
                <p className="mt-0.5 text-caption leading-4 text-muted-foreground">
                  {component.description}
                </p>
                {component.path && (
                  <div className="mt-1 truncate font-mono text-micro text-muted-foreground/80" title={component.path}>
                    {component.path}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-caption text-muted-foreground">
          No integration-surface evidence is available for this host.
        </div>
      )}
    </div>
  );
}

export default function Integrations() {
  const runtimeCapabilities = useRuntimeCapabilities();
  const [data, setData] = useState<LocalIntegrations | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (!runtimeCapabilities.localIntegrations) return;
    fetchLocalIntegrations()
      .then((payload) => {
        setData(payload);
        setError("");
      })
      .catch((reason) =>
        setError(String(reason instanceof Error ? reason.message : reason)),
      );
  }, [runtimeCapabilities.localIntegrations]);

  if (!runtimeCapabilities.localIntegrations) {
    return (
      <PageFrame className="space-y-4 text-neutral-200">
        <section className="max-w-2xl space-y-3">
          <div>
            <h2 className="text-heading font-semibold text-foreground">Developer connections</h2>
            <p className="mt-1 text-body leading-6 text-muted-foreground">
              Coding hosts run on each developer machine; the hosted workspace does not inspect your local Claude, Codex, OpenCode, or editor installation from the browser.
            </p>
          </div>
          <div className="rounded-lg border border-border bg-muted/30 p-4">
            <div className="text-label font-semibold text-foreground">Connect this installation</div>
            <ol className="mt-2 list-decimal space-y-1 pl-5 text-body text-muted-foreground">
              <li>Point LemonCrow at this hosted server.</li>
              <li>Run <code className="text-foreground">lc auth login</code> and approve the device sign-in.</li>
              <li>Use the normal installer to connect the coding hosts on that machine.</li>
            </ol>
            <div className="mt-3 rounded-md border border-border/70 bg-background/60 px-3 py-2 font-mono text-caption text-foreground">
              {window.location.origin}
            </div>
          </div>
        </section>
      </PageFrame>
    );
  }

  const toggleHost = (hostId: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(hostId)) next.delete(hostId);
      else next.add(hostId);
      return next;
    });
  };

  return (
    <PageFrame className="space-y-4 text-neutral-200">
      {error && (
        <div className="border border-rose-900/50 bg-rose-950/15 px-3 py-2 text-label text-rose-300">
          Could not load integrations · {error}
        </div>
      )}

      {!data && !error && (
        <div className="border-y border-neutral-900 py-6 text-center text-label text-muted-foreground">
          Loading integrations…
        </div>
      )}

      {data && (
        <div className="space-y-4">
          <section>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-caption font-semibold uppercase tracking-[0.1em] text-muted-foreground">
                  Coding hosts
                </h2>
                <p className="mt-1 text-caption text-muted-foreground">
                  Expand a host to see the exact LemonCrow surfaces detected on this machine.
                </p>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-caption text-muted-foreground">Live detection</span>
                <span
                  className={`rounded-md border px-2 py-1 text-caption ${
                    data.dispatcher_available
                      ? "border-emerald-900/60 bg-emerald-950/20 text-emerald-300"
                      : "border-rose-900/60 bg-rose-950/20 text-rose-300"
                  }`}
                >
                  Connection {data.dispatcher_available ? "ready" : "unavailable"}
                </span>
              </div>
            </div>

            <div className="grid items-start gap-2 md:grid-cols-2 xl:grid-cols-3">
              {data.hosts.map((host) => {
                const open = expanded.has(host.id);
                const presentComponents = (host.components ?? []).filter(
                  (component) => component.present,
                );

                return (
                  <div
                    key={host.id}
                    className="overflow-hidden rounded-lg border border-border/70 bg-background/60"
                  >
                    <UiButton
                      unstyled
                      type="button"
                      className="w-full p-3 text-left transition-colors hover:bg-muted/35 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                      aria-expanded={open}
                      aria-controls={`integration-details-${host.id}`}
                      onClick={() => toggleHost(host.id)}
                    >
                      <div className="flex items-start gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <div className="truncate text-body font-semibold text-neutral-200">
                              {host.label}
                            </div>
                            {isComingSoon(host) && <Chip tone="info">Coming soon</Chip>}
                          </div>
                          <p className="mt-1 min-h-8 text-caption leading-4 text-muted-foreground">
                            {host.description}
                          </p>
                        </div>
                        <div className="mt-0.5 flex shrink-0 items-center gap-2">
                          {!isComingSoon(host) &&
                            (host.configured ? (
                              <CheckCircle2 size={14} className="text-emerald-400" />
                            ) : (
                              <Circle size={13} className="text-muted-foreground" />
                            ))}
                          {open ? (
                            <ChevronDown size={14} className="text-muted-foreground" />
                          ) : (
                            <ChevronRight size={14} className="text-muted-foreground" />
                          )}
                        </div>
                      </div>

                      <div className="mt-3 flex min-w-0 items-center gap-2">
                        {isComingSoon(host) ? (
                          <span className="text-micro text-muted-foreground">Roadmap integration</span>
                        ) : (
                          <>
                            <span
                              className={`shrink-0 rounded border px-1.5 py-0.5 text-micro font-semibold uppercase tracking-wide ${hostTone(host)}`}
                            >
                              {hostLabel(host)}
                            </span>
                            {presentComponents.length > 0 && (
                              <span className="truncate text-micro text-muted-foreground">
                                {presentComponents.slice(0, 3).map((component) => component.label).join(" · ")}
                                {presentComponents.length > 3 ? ` · +${presentComponents.length - 3} more` : ""}
                              </span>
                            )}
                          </>
                        )}
                      </div>
                    </UiButton>

                    {open && (
                      <div id={`integration-details-${host.id}`}>
                        <IntegrationDetails host={host} />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        </div>
      )}
    </PageFrame>
  );
}
