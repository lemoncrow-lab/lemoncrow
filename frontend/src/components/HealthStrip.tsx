import { useEffect, useState } from "react";
import { api, type HealthResponse, type HostAdapter } from "../api";
import { fmtDate, fmtRelativeTime } from "../lib/format";

/**
 * Daemon + per-host adapter freshness. Used both as a compact strip on
 * Overview and as the full System › Health section — the only two places
 * that read api.health() / api.hosts() for liveness.
 */
export function HealthStrip({ compact = false }: { compact?: boolean }) {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthErr, setHealthErr] = useState(false);
  const [hosts, setHosts] = useState<HostAdapter[] | null>(null);

  useEffect(() => {
    let active = true;
    Promise.allSettled([api.health(), api.hosts()]).then(([h, hs]) => {
      if (!active) return;
      if (h.status === "fulfilled") setHealth(h.value);
      else setHealthErr(true);
      if (hs.status === "fulfilled") setHosts(hs.value);
    });
    return () => {
      active = false;
    };
  }, []);

  const daemonOk = health?.status === "ok";

  return (
    <section
      className={
        compact
          ? "flex flex-wrap items-center gap-2"
          : "flex flex-wrap items-center gap-3"
      }
    >
      <div className="flex items-center gap-2 border border-neutral-800 bg-neutral-950/60 px-3 py-2">
        <span
          className={`h-2 w-2 rounded-full ${
            daemonOk
              ? "bg-emerald-500"
              : healthErr
                ? "bg-red-500"
                : "bg-neutral-500"
          }`}
        />
        <span className="text-xs font-mono text-neutral-300">
          Daemon {health?.status ?? (healthErr ? "unreachable" : "…")}
        </span>
        {health?.timestamp && (
          <span
            className="text-[10px] text-neutral-400"
            title={health.timestamp}
          >
            {fmtDate(health.timestamp)}
          </span>
        )}
      </div>
      {(hosts ?? []).map((host) => (
        <div
          key={host.host_id}
          className="flex items-center gap-2 border border-neutral-800 bg-neutral-950/60 px-3 py-2"
        >
          <span
            className={`h-2 w-2 rounded-full ${
              host.status === "active" ? "bg-emerald-500" : "bg-neutral-600"
            }`}
          />
          <span className="text-xs font-mono text-neutral-300">
            {host.label}
          </span>
          <span
            className="text-[10px] text-neutral-400"
            title={host.last_import_at ?? undefined}
          >
            {host.last_import_at
              ? `last import: ${fmtRelativeTime(host.last_import_at)} · ${host.imported_session_count ?? 0} sessions`
              : "no imports yet"}
          </span>
        </div>
      ))}
      {hosts && hosts.length === 0 && (
        <span className="text-xs text-neutral-400">
          No host adapters detected.
        </span>
      )}
    </section>
  );
}
