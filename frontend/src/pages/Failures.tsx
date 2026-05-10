import { useEffect, useState } from "react";
import { api, type Cluster } from "../api";

export default function Failures() {
  const [items, setItems] = useState<Cluster[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .clusters()
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
          <div className="text-3xl flex-shrink-0">🚨</div>
          <div className="flex-1">
            <div className="flex items-center gap-3 mb-2">
              <h2 className="font-mono font-bold text-neutral-200 text-lg">
                Failure Analyzer
              </h2>
              <span className="text-[10px] px-2 py-0.5 font-mono font-bold uppercase tracking-wide bg-emerald-900/30 text-emerald-300">
                stable
              </span>
            </div>
            <p className="font-mono text-[11px] text-neutral-500 mb-3">
              Recurring Error Detection & Rescue
            </p>
            <p className="text-xs text-neutral-300 leading-relaxed mb-3">
              Clusters traces by error signature. Detects repeated failures and
              generates rescue procedures automatically. Surfaces top failure
              patterns for visibility.
            </p>
            <div className="text-xs text-emerald-300/90 space-y-1">
              <p>✓ Stops agents from retrying known dead-end paths</p>
              <p>✓ Auto-generates rescue blocks from failure clusters</p>
              <p>✓ Quantifies failure impact across the system</p>
            </div>
          </div>
        </div>
      </section>

      {/* Failure Clusters */}
      {items.length === 0 ? (
        <div className="text-neutral-500 text-center py-12">
          <div className="text-4xl mb-4">✅</div>
          <p className="text-lg">No failure clusters detected</p>
          <p className="text-sm text-neutral-600 mt-2">
            Your agents are running smoothly!
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((c, i) => {
            const isExpanded = expandedId === c.id;
            const severityColor =
              c.severity === "high"
                ? "bg-red-900/30 text-red-400"
                : c.severity === "medium"
                  ? "bg-amber-900/30 text-amber-400"
                  : "bg-neutral-800/50 text-neutral-400";

            return (
              <div
                key={i}
                className="border border-neutral-800 bg-neutral-900/50 overflow-hidden"
              >
                {/* Header */}
                <button
                  onClick={() =>
                    setExpandedId(expandedId === c.id ? null : c.id)
                  }
                  className="w-full px-5 py-4 text-left hover:bg-neutral-800/50 transition-colors flex items-start justify-between"
                >
                  <div className="flex-1 flex items-start gap-3 min-w-0">
                    {/* Severity badge */}
                    <span
                      className={`text-[10px] px-2 py-1 font-mono font-bold uppercase flex-shrink-0 mt-0.5 ${severityColor}`}
                    >
                      {c.severity}
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
                          {c.domain}
                        </span>
                      </div>
                      <p className="text-xs text-neutral-400">
                        {c.trace_ids.length} trace
                        {c.trace_ids.length !== 1 ? "s" : ""} · ID: {c.id}
                      </p>
                    </div>
                  </div>
                </button>

                {/* Expanded Content */}
                {isExpanded && (
                  <div className="border-t border-neutral-800 bg-neutral-950/50 px-5 py-4 space-y-4">
                    {/* Fingerprint */}
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                        <span>❯</span> fingerprint
                      </div>
                      <div className="text-xs font-mono text-red-400/70 whitespace-pre-wrap break-words bg-neutral-950 p-2 border border-neutral-800">
                        {c.fingerprint}
                      </div>
                    </div>

                    {/* Sample Errors */}
                    {c.sample_errors && c.sample_errors.length > 0 && (
                      <div>
                        <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                          <span>❯</span> sample errors
                        </div>
                        <div className="space-y-1">
                          {c.sample_errors.map((e, j) => (
                            <div
                              key={j}
                              className="text-xs font-mono text-neutral-400 bg-neutral-950 p-2 border border-neutral-800"
                            >
                              {e}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Suggested Block */}
                    {c.suggested_block_title && (
                      <div>
                        <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                          <span>❯</span> suggested block
                        </div>
                        <div className="text-sm text-neutral-300 bg-neutral-950 p-2 border border-neutral-800">
                          {c.suggested_block_title}
                        </div>
                      </div>
                    )}

                    {/* Suggested Rubric Check */}
                    {c.suggested_rubric_check && (
                      <div>
                        <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                          <span>❯</span> suggested rubric check
                        </div>
                        <div className="text-sm font-mono text-blue-300 bg-neutral-950 p-2 border border-neutral-800">
                          {c.suggested_rubric_check}
                        </div>
                      </div>
                    )}

                    {/* Suggested Eval Case */}
                    {c.suggested_eval_case && (
                      <div>
                        <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                          <span>❯</span> suggested eval case
                        </div>
                        <div className="text-sm font-mono text-neutral-400 bg-neutral-950 p-2 border border-neutral-800">
                          {c.suggested_eval_case}
                        </div>
                      </div>
                    )}

                    {/* Suggested Prompt */}
                    {c.suggested_prompt && (
                      <div>
                        <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                          <span>❯</span> suggested prompt
                        </div>
                        <pre className="text-xs text-neutral-400 whitespace-pre-wrap break-words bg-neutral-950 p-2 border border-neutral-800 overflow-x-auto">
                          {c.suggested_prompt}
                        </pre>
                      </div>
                    )}

                    {/* Trace IDs */}
                    {c.trace_ids && c.trace_ids.length > 0 && (
                      <div className="pt-2 border-t border-neutral-800">
                        <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
                          <span>❯</span> trace ids
                        </div>
                        <div className="flex flex-wrap gap-1">
                          {c.trace_ids.slice(0, 10).map((t, j) => (
                            <span
                              key={j}
                              className="text-xs font-mono text-neutral-500 bg-neutral-950 px-2 py-0.5 border border-neutral-800"
                            >
                              {t}
                            </span>
                          ))}
                          {c.trace_ids.length > 10 && (
                            <span className="text-xs text-neutral-600">
                              +{c.trace_ids.length - 10} more
                            </span>
                          )}
                        </div>
                      </div>
                    )}
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
