import { useEffect, useState } from "react";
import { api, type OutcomesSummary } from "../api";
import {
  Alert,
  Card,
  EmptyState,
  MetricCard,
} from "../components/WorkbenchUI";
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
      {err && <Alert tone="danger" description={err} />}

      {summary === null && !err && (
        <EmptyState title="Loading outcomes…" className="p-6" />
      )}

      {isEmpty && (
        <EmptyState
          icon="◎"
          title="No outcomes captured yet"
          description="Outcome scores appear here after Atelier observes routing and compaction decisions in your sessions."
        />
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
              value={
                summary.route_decisions > 0
                  ? summary.route_avg_score.toFixed(3)
                  : "—"
              }
              tone="emerald"
            />
            <MetricCard
              label="Compact events"
              value={String(summary.compact_events)}
              tone="amber"
            />
            <MetricCard
              label="Avg compact score"
              value={
                summary.compact_events > 0
                  ? summary.compact_avg_score.toFixed(3)
                  : "—"
              }
              tone="emerald"
            />
          </div>

          {summary.sessions_with_high_extra_reads.length > 0 && (
            <Card tone="amber" className="p-4">
              <h2 className="mb-2 text-xs font-bold uppercase tracking-widest text-amber-500">
                Sessions with high extra reads
              </h2>
              <p className="mb-3 text-xs text-neutral-500">
                These sessions show elevated re-read rates after compaction,
                suggesting context loss.
              </p>
              <ul className="space-y-1">
                {summary.sessions_with_high_extra_reads.map((sid) => (
                  <li key={sid} className="font-mono text-xs text-amber-300/80">
                    {sid}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
