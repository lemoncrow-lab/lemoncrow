import { useEffect, useState } from "react";
import { CheckCircle2, Circle } from "lucide-react";

import {
  fetchLocalIntegrations,
  type LocalIntegrationHost,
  type LocalIntegrations,
} from "../control/controlApi";
import { PageFrame } from "../components/WorkbenchUI";

function hostTone(host: LocalIntegrationHost) {
  if (host.configured) return "text-emerald-300 border-emerald-900/60 bg-emerald-950/20";
  if (host.detected) return "text-amber-300 border-amber-900/60 bg-amber-950/20";
  return "text-neutral-500 border-neutral-800 bg-neutral-900/40";
}

function hostLabel(host: LocalIntegrationHost) {
  if (host.configured) return "Configured";
  if (host.detected) return "Detected";
  return "Not detected";
}

export default function Integrations() {
  const [data, setData] = useState<LocalIntegrations | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchLocalIntegrations()
      .then((payload) => {
        setData(payload);
        setError("");
      })
      .catch((reason) =>
        setError(String(reason instanceof Error ? reason.message : reason)),
      );
  }, []);

  return (
    <PageFrame className="space-y-4 text-neutral-200">
      {error && (
        <div className="border border-rose-900/50 bg-rose-950/15 px-3 py-2 text-[11px] text-rose-300">
          Could not load integrations · {error}
        </div>
      )}

      {!data && !error && (
        <div className="border-y border-neutral-900 py-8 text-center text-[11px] text-neutral-600">
          Loading integrations…
        </div>
      )}

      {data && (
        <div className="space-y-6">
          <section>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-[10px] font-semibold uppercase tracking-[0.1em] text-neutral-500">
                Coding hosts
              </h2>
              <div className="flex items-center gap-2">
                {!data.cached_status_available && (
                  <span className="text-[10px] text-neutral-700">Live detection</span>
                )}
                <span
                  className={`rounded border px-2 py-1 text-[10px] ${data.dispatcher_available ? "border-emerald-900/60 bg-emerald-950/20 text-emerald-300" : "border-rose-900/60 bg-rose-950/20 text-rose-300"}`}
                >
                  Connection {data.dispatcher_available ? "ready" : "unavailable"}
                </span>
              </div>
            </div>
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
              {data.hosts.map((host) => (
                <div
                  key={host.id}
                  className="rounded border border-neutral-800/80 bg-neutral-900/25 p-3"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-[12px] font-semibold text-neutral-200">
                        {host.label}
                      </div>
                      <p className="mt-1 min-h-8 text-[10px] leading-4 text-neutral-600">
                        {host.description}
                      </p>
                    </div>
                    {host.configured ? (
                      <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-emerald-400" />
                    ) : (
                      <Circle size={13} className="mt-0.5 shrink-0 text-neutral-700" />
                    )}
                  </div>
                  <div className="mt-3 flex items-center gap-2">
                    <span className={`rounded border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${hostTone(host)}`}>
                      {hostLabel(host)}
                    </span>
                    {host.cached_status && (
                      <span className="truncate text-[9px] text-neutral-700">
                        cached: {host.cached_status.replaceAll("_", " ")}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>

        </div>
      )}
    </PageFrame>
  );
}
