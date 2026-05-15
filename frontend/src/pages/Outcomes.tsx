import { useEffect, useState } from "react";
import { api, type OutcomesSummary } from "../api";
import { MetricCard, SectionHeader } from "../components/WorkbenchUI";
import { useTimeRange } from "../lib/TimeRangeContext";

export default function Outcomes() {
  const { range } = useTimeRange();
  const [summary, setSummary] = useState<OutcomesSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setSummary(null);
    setErr(null);
    api
      .outcomesSummary(range)
      .then(setSummary)
      .catch((e) => setErr(String(e)));
  }, [range]);

  const isEmpty =
    summary !== null &&
    summary.route_decisions === 0 &&
    summary.compact_events === 0;

  return (
    <div className="space-y-6">
      <SectionHeader
        title="Outcomes"
        description="Route and compact outcome scores from captured sessions"
      />

      {err && (
        <div className="border border-red-800 bg-red-950/30 p-4 text-sm text-red-300">{err}</div>
      )}

      {summary === null && !err && (
        <div className="border border-neutral-800 p-6 text-center text-sm text-neutral-500">
          Loading outcomes…
        </div>
      )}

      {isEmpty && (
        <div className="border border-neutral-800 p-8 text-center text-sm text-neutral-500">
          <p className="text-2xl mb-3">◎</p>
          <p className="font-semibold">No outcomes captured yet</p>
          <p className="mt-1 text-neutral-600">
            Outcome scores appear here after Atelier observes routing and
            compaction decisions in your sessions.
          </p>
        </div>
      )}

      {summary !== null && !isEmpty && (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <MetricCard
              label="Route decisions"
              value={String(summary.route_decisions)}
              tone="violet"
            />
            <MetricCard
              label="Avg route score"
              value={summary.route_decisions > 0 ? summary.route_avg_score.toFixed(3) : "—"}
              tone="emerald"
            />
            <MetricCard
              label="Compact events"
              value={String(summary.compact_events)}
              tone="amber"
            />
            <MetricCard
              label="Avg compact score"
              value={summary.compact_events > 0 ? summary.compact_avg_score.toFixed(3) : "—"}
              tone="emerald"
            />
          </div>

          {summary.sessions_with_high_extra_reads.length > 0 && (
            <section className="border border-amber-900/40 bg-amber-950/20 p-4">
              <h2 className="mb-2 text-xs font-bold uppercase tracking-widest text-amber-500">
                Sessions with high extra reads
              </h2>
              <p className="mb-3 text-xs text-neutral-500">
                These sessions show elevated re-read rates after compaction, suggesting
                context loss.
              </p>
              <ul className="space-y-1">
                {summary.sessions_with_high_extra_reads.map((sid) => (
                  <li key={sid} className="font-mono text-xs text-amber-300/80">
                    {sid}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}
