import { useEffect, useState } from "react";
import { api, type PlanRecord } from "../api";

export default function Plans() {
  const [items, setItems] = useState<PlanRecord[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .plans()
      .then(setItems)
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) return <div className="text-red-400">Error: {err}</div>;
  if (!items) return <div className="text-neutral-500">Loading…</div>;

  return (
    <div className="space-y-6">
      {/* Feature Info */}
      <section className="border border-neutral-800 bg-neutral-900/50 p-5">
        <div className="flex items-start gap-4">
          <div className="text-3xl flex-shrink-0">📋</div>
          <div className="flex-1">
            <div className="flex items-center gap-3 mb-2">
              <h2 className="font-mono font-bold text-neutral-200 text-lg">
                Plan Validation
              </h2>
              <span className="text-[10px] px-2 py-0.5 font-mono font-bold uppercase tracking-wide bg-emerald-900/30 text-emerald-300">
                stable
              </span>
            </div>
            <p className="font-mono text-[11px] text-neutral-500 mb-3">
              Pre-Execution Plan Review
            </p>
            <p className="text-xs text-neutral-300 leading-relaxed mb-3">
              Agent plans are validated against reasoning context before
              implementation. Detects unachievable steps, missing dependencies,
              and violations of domain rules. Prevents wasted execution.
            </p>
            <div className="text-xs text-emerald-300/90 space-y-1">
              <p>✓ Catches impossible plans early</p>
              <p>✓ Enforces domain-specific guardrails</p>
              <p>✓ Prevents thrashing on unachievable goals</p>
            </div>
          </div>
        </div>
      </section>

      {/* Plan Results */}
      {items.length === 0 ? (
        <div className="text-neutral-500 text-center py-12">
          <div className="text-4xl mb-4">📋</div>
          <p className="text-lg">No plan-related validation results yet</p>
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((p) => {
            const isExpanded = expandedId === p.trace_id;
            const statusColor =
              p.status === "success"
                ? "bg-emerald-900/30 text-emerald-400"
                : "bg-red-900/30 text-red-400";

            return (
              <div
                key={p.trace_id}
                className="border border-neutral-800 bg-neutral-900/50 overflow-hidden"
              >
                {/* Header */}
                <button
                  onClick={() =>
                    setExpandedId(expandedId === p.trace_id ? null : p.trace_id)
                  }
                  className="w-full px-5 py-4 text-left hover:bg-neutral-800/50 transition-colors flex items-start justify-between"
                >
                  <div className="flex-1 flex items-start gap-3 min-w-0">
                    {/* Status badge */}
                    <span
                      className={`text-[10px] px-2 py-1 font-mono font-bold uppercase flex-shrink-0 mt-0.5 ${statusColor}`}
                    >
                      {p.status}
                    </span>

                    {/* Title & Details */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1 flex-wrap">
                        <span
                          className={`text-neutral-500 font-mono text-xs transition-transform ${
                            isExpanded ? "rotate-90" : ""
                          }`}
                        >
                          ❯
                        </span>
                        <span className="font-mono font-bold text-neutral-200 text-sm">
                          {p.domain}
                        </span>
                      </div>
                      <p className="text-xs text-neutral-400 truncate">
                        {p.task}
                      </p>
                    </div>
                  </div>
                </button>

                {/* Expanded Content */}
                {isExpanded && (
                  <div className="border-t border-neutral-800 bg-neutral-950/50 px-5 py-4 space-y-3">
                    {/* Task Description */}
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                        <span>❯</span> task
                      </div>
                      <p className="text-sm text-neutral-300">{p.task}</p>
                    </div>

                    {/* Trace ID */}
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                        <span>❯</span> trace id
                      </div>
                      <code className="text-xs font-mono text-neutral-500 bg-neutral-950 px-2 py-1 block border border-neutral-800">
                        {p.trace_id}
                      </code>
                    </div>

                    {/* Plan Checks */}
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                        <span>❯</span> checks
                      </div>
                      <ul className="space-y-1">
                        {p.plan_checks.map((c, i) => (
                          <li
                            key={i}
                            className={`text-xs px-2 py-1 border border-neutral-800 flex items-start gap-2 ${
                              c.passed
                                ? "text-emerald-300 bg-emerald-900/10"
                                : "text-red-300 bg-red-900/10"
                            }`}
                          >
                            <span className="flex-shrink-0 mt-0.5">
                              {c.passed ? "✓" : "✗"}
                            </span>
                            <div className="flex-1">
                              <div>{c.name}</div>
                              {c.detail && (
                                <div className="text-[10px] text-neutral-400 mt-0.5">
                                  {c.detail}
                                </div>
                              )}
                            </div>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
