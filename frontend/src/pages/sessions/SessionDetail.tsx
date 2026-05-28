import { useEffect, useState, useMemo } from "react";
import { ExternalLink, ChevronRight, X } from "lucide-react";
import {
  api,
  type Trace,
  type SessionReport,
  type RunInspectorData,
} from "../../api";
import { cx } from "../../components/WorkbenchUI";
import {
  fmtUsd,
  fmtTok,
  fmtDate,
  fmtDuration,
  parseInspectorData,
  groupTurns,
} from "./helpers";
import { StatusDot } from "./StatusBadge";
import { FileDetail, getFileEditInfo, type FileEditInfo } from "./DiffView";
import {
  ConversationTurn,
  ToolCallDetail,
  CommandDetail,
} from "./TurnRenderers";

// ---------------------------------------------------------------------------
// Header stat chip
// ---------------------------------------------------------------------------

function HeaderStat({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: string;
  tone?: "amber" | "emerald" | "violet";
  title?: string;
}) {
  return (
    <div
      className="flex min-w-0 items-center justify-between gap-3 border border-neutral-800/40 bg-black/15 px-2.5 py-1.5 transition-colors hover:bg-neutral-800/20 group"
      title={title}
    >
      <div className="truncate text-[8px] text-neutral-400 uppercase font-black tracking-[0.18em] group-hover:text-neutral-500 transition-colors">
        {label}
      </div>
      <div
        className={cx(
          "truncate text-[10px] font-bold font-mono leading-none",
          tone === "amber"
            ? "text-amber-500/90"
            : tone === "emerald"
              ? "text-emerald-500/90"
              : tone === "violet"
                ? "text-violet-400"
                : "text-neutral-400"
        )}
      >
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Right-panel helpers
// ---------------------------------------------------------------------------

function SidebarMetric({
  label,
  value,
  color = "text-neutral-400",
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-[9px] text-neutral-400 font-mono uppercase font-bold">
        {label}
      </span>
      <span className={cx("text-[10px] font-mono font-black", color)}>
        {value}
      </span>
    </div>
  );
}

function SidebarList({
  title,
  items,
  color = "text-neutral-500",
}: {
  title: string;
  items: Array<string | { path: string; artifact_id?: string }>;
  color?: string;
}) {
  const [copiedPath, setCopiedPath] = useState<string | null>(null);

  const copyToClipboard = async (text: string) => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for non-secure contexts (e.g. http on LAN)
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopiedPath(text);
      setTimeout(
        () => setCopiedPath((cur) => (cur === text ? null : cur)),
        1200
      );
    } catch {
      /* swallow — clipboard may be disabled */
    }
  };

  return (
    <section className="space-y-3">
      <h3 className="text-[9px] font-black uppercase tracking-widest text-neutral-500">
        {title}
      </h3>
      <div className="space-y-1.5 font-mono text-[9px]">
        {items.map((item) => {
          const p = typeof item === "string" ? item : item.path;
          const artId = typeof item === "string" ? null : item.artifact_id;
          const isPath = p.startsWith("/");
          const canOpenRaw = Boolean(artId || isPath);
          const rawUrl = artId
            ? `/api/raw-artifacts/${artId}/content`
            : `/api/v1/files/content?path=${encodeURIComponent(p)}`;
          // Absolute filesystem path → let Chrome try to open it directly.
          // (Browsers may block file:// from http(s) origins; the user can
          // still drop the URL into the address bar from the copy button.)
          const fileUrl = isPath ? `file://${p}` : null;
          const justCopied = copiedPath === p;

          return (
            <div
              key={p}
              className={cx(
                "group/item flex items-center justify-between border-l border-neutral-800/60 pl-2 transition-colors",
                color
              )}
            >
              {fileUrl ? (
                <a
                  href={fileUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="truncate flex-1 hover:text-neutral-200 hover:underline decoration-dotted underline-offset-2"
                  title={`Open ${p}`}
                >
                  {p}
                </a>
              ) : (
                <span
                  className="truncate flex-1 hover:text-neutral-300 cursor-default"
                  title={p}
                >
                  {p}
                </span>
              )}
              <div className="flex items-center gap-2 ml-2 opacity-0 group-hover/item:opacity-100 transition-opacity flex-shrink-0 bg-[#0a0a0a] px-1">
                <button
                  type="button"
                  onClick={() => copyToClipboard(p)}
                  className={cx(
                    "text-[8px] uppercase font-black flex items-center gap-1",
                    justCopied
                      ? "text-emerald-500"
                      : "text-neutral-400 hover:text-sky-400"
                  )}
                  title={`Copy path: ${p}`}
                >
                  {justCopied ? "Copied" : "Copy"}
                </button>
                {canOpenRaw && (
                  <a
                    href={rawUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[8px] text-neutral-400 hover:text-emerald-500 uppercase font-black flex items-center gap-1"
                    title="View raw content"
                  >
                    Raw <ExternalLink size={10} />
                  </a>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function MetaPill({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "violet" | "amber";
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-sm border px-2.5 py-1 text-[9px] font-mono uppercase tracking-[0.2em]",
        tone === "violet"
          ? "border-violet-900/30 bg-violet-950/25 text-violet-200"
          : tone === "amber"
            ? "border-amber-900/30 bg-amber-950/25 text-amber-200"
            : "border-neutral-800 bg-black/20 text-neutral-400"
      )}
    >
      <span className="text-neutral-500">{label}</span>
      <span className="normal-case tracking-normal text-current">{value}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Main detail view
// ---------------------------------------------------------------------------

export function SessionExplorerDetail({ sessionId }: { sessionId: string }) {
  const [report, setReport] = useState<SessionReport | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [inspectorData, setInspectorData] = useState<RunInspectorData | null>(
    null
  );
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [allExpanded, setAllExpanded] = useState(false);
  const [rightPanelOpen, setRightPanelOpen] = useState(false);

  useEffect(() => {
    setLoading(true);
    setErr(null);
    setAllExpanded(false);
    Promise.all([
      api.sessionReport(sessionId).catch(() => null),
      api.trace(sessionId).catch(() => null),
      api.ledger(sessionId).catch(() => null),
    ])
      .then(([rep, tr, led]) => {
        setReport(rep);
        setTrace(tr);
        if (led) setInspectorData(parseInspectorData(sessionId, led));
        setLoading(false);
      })
      .catch((e) => {
        setErr(String(e));
        setLoading(false);
      });
  }, [sessionId]);

  const activeDurationSecs = useMemo(() => {
    if (
      !inspectorData?.conversations ||
      inspectorData.conversations.length === 0
    ) {
      return report?.duration_seconds || 0;
    }
    let ms = 0;
    let currentStart: number | null = null;
    for (const turn of inspectorData.conversations) {
      const at = new Date(turn.at || 0).getTime();
      if (turn.kind === "user_message") {
        currentStart = at;
      } else {
        if (currentStart !== null) {
          ms += at - currentStart;
          currentStart = at;
        } else {
          currentStart = at;
        }
      }
    }
    return ms / 1000;
  }, [inspectorData, report]);

  const startedModel = useMemo(() => {
    if (report?.started_model) return report.started_model;
    const firstConversationModel = inspectorData?.conversations?.find(
      (turn) => turn.model
    )?.model;
    if (firstConversationModel) return firstConversationModel;
    if (trace?.model) return trace.model;
    const reportModels = report?.models_used
      ? Object.keys(report.models_used)
      : [];
    return reportModels[0] || null;
  }, [inspectorData, report, trace]);

  // Join per-call savings from the session report onto conversation turns.
  // Claude strips the MCP response's `saved` field before writing the transcript,
  // so we reattach it here by matching tool name (short form) + sequential order
  // within each tool type (savings events are written in call order).
  const enrichedConversations = useMemo(() => {
    const base = inspectorData?.conversations ?? [];
    if (!report?.tool_savings?.length) return base;

    // Group savings by short tool name, sorted by timestamp.
    const queue = new Map<string, typeof report.tool_savings>();
    for (const row of [...report.tool_savings].sort((a, b) =>
      a.at < b.at ? -1 : 1
    )) {
      if (!queue.has(row.tool)) queue.set(row.tool, []);
      queue.get(row.tool)!.push(row);
    }
    const ptr = new Map<string, number>();

    return base.map((turn) => {
      if (turn.kind !== "tool_call" || !turn.tool_name) return turn;
      // Extract the short name: "mcp__plugin_atelier_atelier__read" → "read"
      const parts = (turn.tool_name as string).split("__");
      const shortName = parts[parts.length - 1];
      const rows = queue.get(shortName);
      if (!rows) return turn;
      const i = ptr.get(shortName) ?? 0;
      if (i >= rows.length) return turn;
      // Savings timestamp is slightly after the tool call — allow up to 60 s.
      const turnMs = new Date(turn.at || 0).getTime();
      const savMs = new Date(rows[i].at).getTime();
      if (savMs >= turnMs - 2000 && savMs <= turnMs + 60000) {
        ptr.set(shortName, i + 1);
        return {
          ...turn,
          saved: {
            tokens: rows[i].tokens_saved,
            calls: rows[i].calls_saved,
            usd: rows[i].cost_saved_usd,
          },
        };
      }
      return turn;
    });
  }, [inspectorData, report]);

  // Derive file changes from the session ledger directly — every file_edit turn
  // is already captured there. The trace's files_touched is redundant for this;
  // trace should only carry signals that can't be reconstructed from the session.
  // Dedup by path: last-write wins (preserves the most recent diff).
  const ledgerFilesTouched = useMemo((): FileEditInfo[] => {
    const byPath = new Map<string, FileEditInfo>();
    for (const turn of inspectorData?.conversations ?? []) {
      if (turn.kind !== "file_edit") continue;
      const info = getFileEditInfo(turn);
      if (info?.path) byPath.set(info.path, info);
    }
    return [...byPath.values()];
  }, [inspectorData]);

  if (loading)
    return (
      <div className="flex flex-col items-center justify-center h-full space-y-4 bg-[#0a0a0a]">
        <div className="w-10 h-10 border border-purple-500/20 border-t-purple-500 rounded-full animate-spin" />
        <div className="text-[10px] text-neutral-400 uppercase tracking-[0.3em] font-mono animate-pulse">
          Reconstructing Ledger...
        </div>
      </div>
    );

  if (err)
    return (
      <div className="h-full flex items-center justify-center bg-[#0a0a0a] p-12 text-center">
        <div className="max-w-xs space-y-3">
          <div className="text-red-500 text-sm font-mono font-bold uppercase tracking-widest">
            Load Failure
          </div>
          <div className="text-neutral-400 text-xs font-mono leading-relaxed">
            {err}
          </div>
        </div>
      </div>
    );

  return (
    <div className="flex flex-col h-full bg-[#0a0a0a] relative animate-in fade-in duration-500">
      {/* Header */}
      <header className="flex-shrink-0 px-8 py-4 border-b border-neutral-800/80 bg-[#0d0d0d]/95 backdrop-blur-md sticky top-0 z-20 shadow-2xl">
        <div className="space-y-3">
          <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
            <div className="min-w-0 flex-1 space-y-2">
              <h1 className="text-sm font-bold tracking-wide text-neutral-100 font-mono truncate uppercase">
                {trace && (
                  <span
                    className="inline-flex items-center px-1"
                    title={trace.status}
                  >
                    <StatusDot status={trace.status} className="h-2.5 w-2.5" />
                  </span>
                )}{" "}
                {trace?.task || "Execution Detail"}
              </h1>
              <div className="flex flex-wrap items-center gap-2">
                <MetaPill label="Session" value={sessionId} />
                <MetaPill
                  label="Started"
                  value={fmtDate(report?.started_at || trace?.created_at)}
                />
                {startedModel && (
                  <MetaPill label="Model" value={startedModel} tone="violet" />
                )}
                <MetaPill
                  label="Agent"
                  value={`@${trace?.agent || "unknown"}`}
                  tone="amber"
                />
              </div>
            </div>

            <div className="flex items-center gap-3 self-start">
              {report?.raw_artifact_ids &&
                report.raw_artifact_ids.length > 0 && (
                  <a
                    href={`/api/raw-artifacts/${report.raw_artifact_ids[0]}/content`}
                    target="_blank"
                    rel="noreferrer"
                    className="px-3 py-1.5 border border-neutral-700 hover:border-neutral-500 hover:text-white transition-all text-[9px] font-mono text-neutral-500 uppercase tracking-widest flex items-center gap-2"
                  >
                    <ExternalLink size={10} />
                    Raw Link
                  </a>
                )}
              <button
                onClick={() => setAllExpanded(!allExpanded)}
                className="px-3 py-1.5 border border-neutral-700 hover:border-neutral-500 hover:text-white transition-all text-[9px] font-mono text-neutral-500 uppercase tracking-widest"
              >
                {allExpanded ? "Collapse View" : "Expand All"}
              </button>
              <button
                onClick={() => setRightPanelOpen(!rightPanelOpen)}
                className={cx(
                  "w-8 h-8 flex items-center justify-center border transition-all text-sm font-mono",
                  rightPanelOpen
                    ? "bg-purple-600 border-purple-500 text-white"
                    : "border-neutral-700 text-neutral-500 hover:border-neutral-500 hover:text-white"
                )}
                title="Toggle Detailed Metrics"
              >
                {rightPanelOpen ? <X size={14} /> : <ChevronRight size={14} />}
              </button>
            </div>
          </div>

          <div className="grid grid-cols-7 gap-1.5">
            <HeaderStat
              label="Cost"
              value={
                report
                  ? fmtUsd(report.total_cost_usd)
                  : trace?.input_tokens
                    ? "..."
                    : "—"
              }
              tone="amber"
            />
            <HeaderStat
              label="Saved"
              value={report ? fmtUsd(report.total_atelier_savings_usd) : "—"}
              tone="emerald"
            />
            <HeaderStat
              label="Input"
              value={
                report || trace || inspectorData
                  ? (() => {
                      // "Input" = bytes the model freshly processed this
                      // session = new input + cache writes. Anthropic's
                      // raw `input_tokens` excludes cW even though cW is
                      // also new input the model paid to ingest. Mirrors
                      // the stop-hook formatter so live and post-session
                      // numbers match.
                      const newIn =
                        report?.input_tokens ??
                        trace?.input_tokens ??
                        inspectorData?.tokens_pre ??
                        0;
                      const cw =
                        report?.cache_write_tokens ??
                        trace?.cache_creation_input_tokens ??
                        0;
                      return fmtTok(newIn + cw);
                    })()
                  : "—"
              }
              title={
                report
                  ? `${fmtTok(report.input_tokens)} new + ${fmtTok(report.cache_write_tokens)} cache-write`
                  : undefined
              }
            />
            <HeaderStat
              label="Output"
              value={
                report || trace || inspectorData
                  ? fmtTok(
                      report?.output_tokens ??
                        trace?.output_tokens ??
                        inspectorData?.tokens_post ??
                        0
                    )
                  : "—"
              }
            />
            <HeaderStat
              label="Cache"
              value={
                report || trace
                  ? (() => {
                      const cr =
                        report?.cache_read_tokens ??
                        trace?.cached_input_tokens ??
                        0;
                      const cw =
                        report?.cache_write_tokens ??
                        trace?.cache_creation_input_tokens ??
                        0;
                      return cw > 0
                        ? `${fmtTok(cr)} / ${fmtTok(cw)}`
                        : fmtTok(cr);
                    })()
                  : "—"
              }
              title={
                report
                  ? `${fmtTok(report.cache_read_tokens)} cache-read · ${fmtTok(report.cache_write_tokens)} cache-write`
                  : undefined
              }
            />
            <HeaderStat
              label="Turns"
              value={report ? String(report.total_turns) : "—"}
            />
            <HeaderStat
              label="Time"
              value={
                report
                  ? fmtDuration(
                      report.active_duration_seconds || activeDurationSecs
                    )
                  : "—"
              }
            />
          </div>
        </div>
      </header>

      {/* Timeline + right panel */}
      <div className="flex-1 overflow-hidden">
        <div className="flex h-full">
          {/* Scrollable timeline */}
          <div className="flex-1 overflow-y-auto custom-scrollbar bg-[#0a0a0a]">
            <div className="p-10 space-y-16 pb-48">
              <section className="space-y-12">
                <div className="flex items-center gap-6">
                  <h2 className="text-[10px] font-black uppercase tracking-[0.5em] text-neutral-500 whitespace-nowrap">
                    Execution Flow
                  </h2>
                  <div className="h-px w-full bg-gradient-to-r from-neutral-800 to-transparent" />
                </div>

                <div className="space-y-12">
                  {enrichedConversations.length > 0 ? (
                    groupTurns(enrichedConversations).map((turn, i) => (
                      <ConversationTurn
                        key={i}
                        turn={turn}
                        forceExpand={allExpanded}
                      />
                    ))
                  ) : (
                    <div className="space-y-8">
                      {trace?.reasoning && trace.reasoning.length > 0 && (
                        <div className="space-y-4">
                          <h3 className="text-[10px] font-bold uppercase tracking-widest text-neutral-500 px-1">
                            Strategy
                          </h3>
                          {trace.reasoning.map((r, i) => (
                            <div
                              key={i}
                              className="bg-purple-950/[0.03] border border-purple-900/10 p-5 text-[11px] leading-relaxed text-purple-400/60 font-mono whitespace-pre-wrap rounded-sm shadow-inner"
                            >
                              {r}
                            </div>
                          ))}
                        </div>
                      )}
                      <div className="space-y-4">
                        <h3 className="text-[10px] font-bold uppercase tracking-widest text-neutral-500 px-1">
                          Events
                        </h3>
                        <div className="space-y-3">
                          {trace?.tools_called.map((t, i) => (
                            <ToolCallDetail
                              key={i}
                              tool={t}
                              forceExpand={allExpanded}
                            />
                          ))}
                          {trace?.commands_run.map((c, i) => (
                            <CommandDetail
                              key={i}
                              command={c}
                              forceExpand={allExpanded}
                            />
                          ))}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </section>

              {ledgerFilesTouched.length > 0 && (
                <section className="space-y-6 pt-12 border-t border-neutral-900/50">
                  <div className="flex items-center gap-6">
                    <h2 className="text-[10px] font-black uppercase tracking-[0.5em] text-neutral-500 whitespace-nowrap">
                      File Changes
                    </h2>
                    <div className="h-px w-full bg-gradient-to-r from-neutral-800 to-transparent" />
                    <span className="text-[9px] text-neutral-500 font-mono font-bold uppercase tracking-widest flex-shrink-0">
                      {ledgerFilesTouched.length} file
                      {ledgerFilesTouched.length !== 1 ? "s" : ""} · from
                      session
                    </span>
                  </div>
                  <div className="space-y-2">
                    {ledgerFilesTouched.map((f, i) => (
                      <FileDetail
                        key={i}
                        file={f.diff ? { path: f.path, diff: f.diff } : f.path}
                        forceExpand={allExpanded}
                      />
                    ))}
                  </div>
                </section>
              )}
            </div>
          </div>

          {/* Right rail — detailed metrics */}
          {rightPanelOpen && (
            <aside className="w-96 flex-shrink-0 border-l border-neutral-800/60 bg-[#0d0d0d]/40 overflow-y-auto custom-scrollbar p-6 space-y-10 animate-in slide-in-from-right duration-300">
              <section className="space-y-4">
                <h3 className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-400 border-b border-neutral-800 pb-2">
                  Session Blueprint
                </h3>
                <div className="grid gap-4">
                  <SidebarMetric
                    label="Total cost"
                    value={report ? fmtUsd(report.total_cost_usd) : "—"}
                    color="text-amber-500"
                  />
                  <SidebarMetric
                    label="Model Savings"
                    value={
                      report ? fmtUsd(report.total_atelier_savings_usd) : "—"
                    }
                    color="text-emerald-500"
                  />
                  <SidebarMetric
                    label="Started Model"
                    value={startedModel || "—"}
                    color="text-violet-400"
                  />
                  <SidebarMetric
                    label="Total Tokens"
                    value={
                      report
                        ? fmtTok(report.input_tokens + report.output_tokens)
                        : "—"
                    }
                  />
                  <SidebarMetric
                    label="Input Cost"
                    value={report ? fmtUsd(report.input_token_cost_usd) : "—"}
                  />
                  <SidebarMetric
                    label="Output Cost"
                    value={report ? fmtUsd(report.output_token_cost_usd) : "—"}
                  />
                  <SidebarMetric
                    label="Cache Savings"
                    value={report ? fmtUsd(report.cache_read_cost_usd) : "—"}
                  />
                </div>
              </section>

              {report?.top_tools_by_cost &&
                report.top_tools_by_cost.length > 0 && (
                  <section className="space-y-4">
                    <h3 className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-400 border-b border-neutral-800 pb-2">
                      Tool Breakdown
                    </h3>
                    <div className="space-y-2">
                      {report.top_tools_by_cost.map((t, i) => (
                        <div
                          key={i}
                          className="flex items-center justify-between text-[10px] font-mono border-b border-neutral-800/40 pb-1 last:border-0"
                        >
                          <span className="text-blue-400/80 truncate pr-4">
                            {t.tool} ({t.calls})
                          </span>
                          <span className="text-neutral-500">
                            {fmtUsd(t.cost_usd)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </section>
                )}

              {report?.tool_savings && report.tool_savings.length > 0 && (
                <section className="space-y-4">
                  <h3 className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-400 border-b border-neutral-800 pb-2">
                    Context Savings by Tool
                  </h3>
                  <div className="space-y-2">
                    {report.tool_savings.map((row, i) => (
                      <div
                        key={i}
                        className="flex items-center justify-between text-[10px] font-mono border-b border-neutral-800/40 pb-1 last:border-0"
                      >
                        <span className="text-emerald-400/80 truncate pr-4">
                          {row.tool}
                        </span>
                        <span className="text-neutral-400">
                          {(row.tokens_saved / 1000).toFixed(1)}k tok ·{" "}
                          {fmtUsd(row.cost_saved_usd)}
                        </span>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {report?.models_used &&
                Object.keys(report.models_used).length > 0 && (
                  <section className="space-y-4">
                    <h3 className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-400 border-b border-neutral-800 pb-2">
                      Models Involved
                    </h3>
                    <div className="space-y-2 text-[10px] font-mono">
                      {Object.entries(report.models_used).map(
                        ([model, count], i) => (
                          <div
                            key={i}
                            className="flex items-center justify-between border-b border-neutral-800/40 pb-1 last:border-0"
                          >
                            <span className="text-violet-400/80 truncate pr-4">
                              {model}
                            </span>
                            <span className="text-neutral-500">
                              {count} calls
                            </span>
                          </div>
                        )
                      )}
                    </div>
                  </section>
                )}

              {report?.agent_settings &&
                Object.keys(report.agent_settings).length > 0 && (
                  <section className="space-y-4">
                    <h3 className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-400 border-b border-neutral-800 pb-2">
                      Agent Config
                    </h3>
                    <div className="space-y-2 text-[10px] font-mono">
                      {Object.entries(report.agent_settings).map(
                        ([key, val], i) => (
                          <div
                            key={i}
                            className="flex flex-col border-b border-neutral-800/40 pb-1 last:border-0 gap-0.5"
                          >
                            <span className="text-neutral-400 uppercase font-bold tracking-tighter text-[8px]">
                              {key}
                            </span>
                            <span className="text-neutral-400 truncate">
                              {String(val)}
                            </span>
                          </div>
                        )
                      )}
                    </div>
                  </section>
                )}

              {report?.skills && report.skills.length > 0 && (
                <SidebarList
                  title="Active Skills"
                  items={report.skills}
                  color="text-amber-500/70"
                />
              )}
              {inspectorData?.source_files &&
                inspectorData.source_files.length > 0 && (
                  <SidebarList
                    title="Context Files"
                    items={inspectorData.source_files}
                  />
                )}
              {inspectorData?.artifacts &&
                inspectorData.artifacts.length > 0 && (
                  <SidebarList
                    title="Session Artifacts"
                    items={inspectorData.artifacts.map((artifact) => ({
                      path: `${artifact.label || (artifact.scope === "subagent" ? "subagent" : "main")} · ${artifact.relative_path.split("/").pop() || artifact.relative_path}`,
                      artifact_id: artifact.id,
                    }))}
                    color="text-sky-500/70"
                  />
                )}
              {inspectorData?.pinned_blocks &&
                inspectorData.pinned_blocks.length > 0 && (
                  <SidebarList
                    title="Pinned Logic"
                    items={inspectorData.pinned_blocks}
                    color="text-purple-500/70"
                  />
                )}
              {inspectorData?.recalled_passages &&
                inspectorData.recalled_passages.length > 0 && (
                  <SidebarList
                    title="Memory Recall"
                    items={inspectorData.recalled_passages.map((p) => p.id)}
                    color="text-cyan-500/70"
                  />
                )}

              <section className="space-y-3 opacity-60 hover:opacity-100 transition-opacity pt-4 border-t border-neutral-800">
                <h3 className="text-[9px] font-black uppercase tracking-widest text-neutral-500">
                  Audit Telemetry
                </h3>
                <div className="space-y-1.5 text-[9px] font-mono text-neutral-500">
                  {report?.telemetry &&
                    Object.entries(report.telemetry).map(([key, val], i) => (
                      <div
                        key={i}
                        className="flex justify-between border-b border-neutral-800/20 pb-0.5 last:border-0"
                      >
                        <span className="uppercase text-[8px] font-bold">
                          {key}
                        </span>
                        <span className="text-neutral-400">{String(val)}</span>
                      </div>
                    ))}
                  {inspectorData?.summarized_events_count ? (
                    <div className="flex justify-between border-b border-neutral-800/20 pb-0.5 last:border-0">
                      <span className="uppercase text-[8px] font-bold text-amber-600/80">
                        Compressed_Events
                      </span>
                      <span className="text-amber-500/70">
                        {inspectorData.summarized_events_count}
                      </span>
                    </div>
                  ) : null}
                  {inspectorData?.tokens_pre && (
                    <div className="flex justify-between border-b border-neutral-800/20 pb-0.5 last:border-0">
                      <span className="uppercase text-[8px] font-bold">
                        Context_Pre
                      </span>
                      <span className="text-neutral-400">
                        {fmtTok(inspectorData.tokens_pre)}
                      </span>
                    </div>
                  )}
                  {inspectorData?.tokens_post && (
                    <div className="flex justify-between border-b border-neutral-800/20 pb-0.5 last:border-0">
                      <span className="uppercase text-[8px] font-bold text-emerald-600/80">
                        Context_Post
                      </span>
                      <span className="text-emerald-500/70">
                        {fmtTok(inspectorData.tokens_post)}
                      </span>
                    </div>
                  )}
                </div>
              </section>
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}
