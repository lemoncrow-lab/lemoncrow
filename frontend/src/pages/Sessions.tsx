import { useEffect, useState, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { api, type SessionSummary } from "../api";
import { MetricCard, SectionHeader, cx } from "../components/WorkbenchUI";
import { useTimeRange } from "../lib/TimeRangeContext";

type SortKey = "started_at" | "total_cost_usd" | "total_atelier_savings_usd" | "total_turns";
type SortDir = "asc" | "desc";

function fmtUsd(v: number) {
  return `$${v.toFixed(2)}`;
}

function fmtDate(s: string) {
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

export default function Sessions() {
  const { range } = useTimeRange();
  const navigate = useNavigate();

  const [items, setItems] = useState<SessionSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("started_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  useEffect(() => {
    setItems(null);
    setErr(null);
    api
      .sessions(range)
      .then(setItems)
      .catch((e) => setErr(String(e)));
  }, [range]);

  const sorted = useMemo(() => {
    if (!items) return [];
    return [...items].sort((a, b) => {
      const av = a[sortKey] as string | number;
      const bv = b[sortKey] as string | number;
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [items, sortKey, sortDir]);

  const totalCost = items ? items.reduce((s, i) => s + i.total_cost_usd, 0) : 0;
  const totalSavings = items ? items.reduce((s, i) => s + i.total_atelier_savings_usd, 0) : 0;

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  function SortArrow({ k }: { k: SortKey }) {
    if (sortKey !== k) return <span className="ml-1 text-neutral-600">⇅</span>;
    return <span className="ml-1 text-purple-400">{sortDir === "asc" ? "↑" : "↓"}</span>;
  }

  return (
    <div className="space-y-6">
      <SectionHeader
        title="Sessions"
        description={`Recent sessions — sorted by ${sortKey.replace(/_/g, " ")}`}
      />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <MetricCard
          label="Sessions"
          value={items ? String(items.length) : "—"}
          tone="violet"
        />
        <MetricCard
          label="Total cost"
          value={items ? fmtUsd(totalCost) : "—"}
          tone="amber"
        />
        <MetricCard
          label="Atelier savings"
          value={items ? fmtUsd(totalSavings) : "—"}
          tone="emerald"
        />
        <MetricCard
          label="Avg cost"
          value={items && items.length ? fmtUsd(totalCost / items.length) : "—"}
          tone="neutral"
        />
      </div>

      {err && (
        <div className="border border-red-800 bg-red-950/30 p-4 text-sm text-red-300">{err}</div>
      )}

      {items === null && !err && (
        <div className="border border-neutral-800 p-6 text-center text-sm text-neutral-500">
          Loading sessions…
        </div>
      )}

      {items !== null && items.length === 0 && (
        <div className="border border-neutral-800 p-8 text-center text-sm text-neutral-500">
          <p className="text-2xl mb-3">◫</p>
          <p className="font-semibold">No sessions yet</p>
          <p className="mt-1 text-neutral-600">
            Sessions appear here after you run a Claude Code session with Atelier active.
          </p>
        </div>
      )}

      {sorted.length > 0 && (
        <div className="overflow-x-auto border border-neutral-800">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-neutral-800 bg-neutral-900/60 text-neutral-400">
              <tr>
                <th className="px-3 py-2">Session ID</th>
                <th
                  className="cursor-pointer px-3 py-2 hover:text-neutral-200"
                  onClick={() => toggleSort("started_at")}
                >
                  Started <SortArrow k="started_at" />
                </th>
                <th className="px-3 py-2">Vendor</th>
                <th
                  className="cursor-pointer px-3 py-2 hover:text-neutral-200"
                  onClick={() => toggleSort("total_turns")}
                >
                  Turns <SortArrow k="total_turns" />
                </th>
                <th
                  className="cursor-pointer px-3 py-2 text-right hover:text-neutral-200"
                  onClick={() => toggleSort("total_cost_usd")}
                >
                  Cost <SortArrow k="total_cost_usd" />
                </th>
                <th
                  className="cursor-pointer px-3 py-2 text-right hover:text-neutral-200"
                  onClick={() => toggleSort("total_atelier_savings_usd")}
                >
                  Savings <SortArrow k="total_atelier_savings_usd" />
                </th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((s) => (
                <tr
                  key={s.session_id}
                  className="cursor-pointer border-b border-neutral-800/60 hover:bg-neutral-800/30"
                  onClick={() => navigate(`/sessions/${s.session_id}`)}
                >
                  <td className="px-3 py-2 font-mono text-purple-400/80">
                    {s.session_id.slice(0, 12)}…
                  </td>
                  <td className="px-3 py-2 text-neutral-400">{fmtDate(s.started_at)}</td>
                  <td className="px-3 py-2">
                    <span
                      className={cx(
                        "px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide",
                        s.vendor === "anthropic"
                          ? "bg-orange-500/10 text-orange-400"
                          : s.vendor === "openai"
                          ? "bg-green-500/10 text-green-400"
                          : s.vendor === "google"
                          ? "bg-blue-500/10 text-blue-400"
                          : "bg-neutral-700/40 text-neutral-400"
                      )}
                    >
                      {s.vendor}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-neutral-300">{s.total_turns}</td>
                  <td className="px-3 py-2 text-right text-amber-300">
                    {fmtUsd(s.total_cost_usd)}
                  </td>
                  <td className="px-3 py-2 text-right text-green-400">
                    {s.total_atelier_savings_usd > 0
                      ? fmtUsd(s.total_atelier_savings_usd)
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
