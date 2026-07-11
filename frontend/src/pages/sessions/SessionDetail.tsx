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
  parseAt,
} from "../../lib/format";
import { parseInspectorData, groupTurns } from "./helpers";
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
      <div className="truncate text-[10px] text-neutral-400 uppercase font-black tracking-[0.18em] group-hover:text-neutral-400 transition-colors">
        {label}
      </div>
      <div
        className={cx(
          "truncate text-[10px] font-bold font-mono leading-none",
          tone === "amber"
            ? "text-amber-300"
            : tone === "emerald"
              ? "text-emerald-300"
              : tone === "violet"
                ? "text-violet-300"
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
      <span className="text-[10px] text-neutral-400 font-mono uppercase font-bold">
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
  color = "text-neutral-400",
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
      <h3 className="text-[10px] font-black uppercase tracking-widest text-neutral-400">
        {title}
      </h3>
      <div className="space-y-1.5 font-mono text-[10px]">
        {items.map((item) => {
          const p = typeof item === "string" ? item : item.path;
          const artId = typeof item === "string" ? null : item.artifact_id;
          const isPath = p.startsWith("/");
          const canOpenRaw = Boolean(artId || isPath);
          const rawUrl = artId
            ? `/api/raw-artifacts/${artId}/content`
            : `/api/v1/files/content?path=${encodeURIComponent(p)}`;
          const projectionUrl = isPath
            ? api.fileProjectionInspectUrl(p, { view: "compact" })
            : null;
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
              <div className="flex items-center gap-2 ml-2 opacity-0 group-hover/item:opacity-100 transition-opacity flex-shrink-0 bg-surface px-1">
                <button
                  type="button"
                  onClick={() => copyToClipboard(p)}
                  className={cx(
                    "text-[10px] uppercase font-black flex items-center gap-1",
                    justCopied
                      ? "text-emerald-300"
                      : "text-neutral-400 hover:text-sky-300"
                  )}
                  title={`Copy path: ${p}`}
                >
                  {justCopied ? "Copied" : "Copy"}
                </button>
                {projectionUrl && (
                  <a
                    href={projectionUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[10px] text-neutral-400 hover:text-cyan-300 uppercase font-black"
                    title="Inspect compact projection metadata"
                  >
                    Projection
                  </a>
                )}
                {canOpenRaw && (
                  <a
                    href={rawUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[10px] text-neutral-400 hover:text-emerald-300 uppercase font-black flex items-center gap-1"
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

function truncateMiddle(value: string, keepEach = 6): string {
  if (value.length <= keepEach * 2 + 1) return value;
  return `${value.slice(0, keepEach)}…${value.slice(-keepEach)}`;
}

function MetaPill({
  label,
  value,
  tone = "neutral",
  copyValue,
}: {
  label: string;
  value: string;
  tone?: "neutral" | "violet" | "amber";
  /** When set, the pill truncates `value` and copies this on click. */
  copyValue?: string;
}) {
  const [copied, setCopied] = useState(false);
  const toneClass =
    tone === "violet"
      ? "border-violet-900/30 bg-violet-950/25 text-violet-200"
      : tone === "amber"
        ? "border-amber-900/30 bg-amber-950/25 text-amber-200"
        : "border-neutral-800 bg-black/20 text-neutral-400";

  if (copyValue === undefined) {
    return (
      <span
        className={cx(
          "inline-flex items-center gap-1 rounded-sm border px-2.5 py-1 text-[10px] font-mono uppercase tracking-[0.2em]",
          toneClass
        )}
      >
        <span className="text-neutral-400">{label}</span>
        <span className="normal-case tracking-normal text-current">
          {value}
        </span>
      </span>
    );
  }

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(copyValue);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard may be disabled */
    }
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      title={`Copy: ${copyValue}`}
      className={cx(
        "inline-flex items-center gap-1 rounded-sm border px-2.5 py-1 text-[10px] font-mono uppercase tracking-[0.2em] transition hover:border-neutral-600",
        toneClass
      )}
    >
      <span className="text-neutral-400">{label}</span>
      <span className="normal-case tracking-normal text-current">
        {copied ? "Copied" : truncateMiddle(value)}
      </span>
    </button>
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
  const [outcomes, setOutcomes] = useState<Record<string, unknown>[]>([]);
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
      api.outcomesForSession(sessionId).catch(() => []),
    ])
      .then(([rep, tr, led, outcomeEntries]) => {
        setReport(rep);
        setTrace(tr);
        if (led) setInspectorData(parseInspectorData(sessionId, led));
        setOutcomes(outcomeEntries);
        setLoading(false);
      })
      .catch((e) => {
        setErr(String(e));
        setLoading(false);
      });
  }, [sessionId]);

  // Average outcome_window.outcome_score across all entries of one kind
  // ("route" | "compact") returned by api.outcomesForSession.
  const outcomeScore = useMemo(() => {
    return (kind: string): number | null => {
      const scores = outcomes
        .filter((entry) => entry.kind === kind)
        .map((entry) => {
          const window = entry.outcome_window as
            | Record<string, unknown>
            | undefined;
          const score = window?.outcome_score;
          return typeof score === "number" ? score : null;
        })
        .filter((value): value is number => value !== null);
      if (scores.length === 0) return null;
      return scores.reduce((a, b) => a + b, 0) / scores.length;
    };
  }, [outcomes]);
  const routeScore = outcomeScore("route");
  const compactScore = outcomeScore("compact");

  const activeDurationSecs = useMemo(() => {
    if (
      !inspectorData?.conversations ||
      inspectorData.conversations.length === 0
    ) {
      return report?.duration_seconds || 0;
    }
    let secs = 0;
    let currentStart: number | null = null;
    for (const turn of inspectorData.conversations) {
      // Missing/unparseable timestamps must not collapse to epoch (1970) or
      // NaN — skip them rather than corrupting the running chunk baseline.
      const at = parseAt(turn.at)?.getTime();
      if (at === undefined) continue;
      if (turn.kind === "user_message") {
        currentStart = at;
      } else if (currentStart !== null) {
        const chunk = (at - currentStart) / 1000;
        // Mirror the backend's active-duration guard: only count positive
        // gaps under an hour as "active" time between turns.
        if (chunk > 0 && chunk < 3600) secs += chunk;
        currentStart = at;
      } else {
        currentStart = at;
      }
    }
    return secs;
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
      // Extract the short name: "mcp__plugin_lemoncrow_lemon__read" → "read"
      const parts = (turn.tool_name as string).split("__");
      const shortName = parts[parts.length - 1];
      const rows = queue.get(shortName);
      if (!rows) return turn;
      let i = ptr.get(shortName) ?? 0;
      const turnMs = new Date(turn.at || 0).getTime();
      // A savings row that's more than 2s older than this turn can never
      // match a later turn either — advance past it so a stale first event
      // doesn't permanently stall the queue for every turn that follows.
      while (
        i < rows.length &&
        new Date(rows[i].at).getTime() < turnMs - 2000
      ) {
        i++;
      }
      ptr.set(shortName, i);
      if (i >= rows.length) return turn;
      // Savings timestamp is slightly after the tool call — allow up to 60 s.
      const savMs = new Date(rows[i].at).getTime();
      if (savMs <= turnMs + 60000) {
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
      <div className="flex flex-col items-center justify-center h-full space-y-4 bg-surface">
        <div className="w-10 h-10 border border-brand-500/20 border-t-brand-500 rounded-full animate-spin" />
        <div className="text-[10px] text-neutral-400 uppercase tracking-[0.3em] font-mono animate-pulse">
          Reconstructing Ledger...
        </div>
      </div>
    );

  if (err)
    return (
      <div className="h-full flex items-center justify-center bg-surface p-12 text-center">
        <div className="max-w-xs space-y-3">
          <div className="text-red-300 text-sm font-mono font-bold uppercase tracking-widest">
            Load Failure
          </div>
          <div className="text-neutral-400 text-xs font-mono leading-relaxed">
            {err}
          </div>
        </div>
      </div>
    );

  return (
    <div className="flex flex-col h-full bg-surface relative animate-in fade-in duration-500">
      {/* Header */}
      <header className="flex-shrink-0 px-8 py-4 border-b border-neutral-800/80 bg-surface-raised/95 backdrop-blur-md sticky top-0 z-20 shadow-2xl">
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
                <MetaPill
                  label="Session"
                  value={sessionId}
                  copyValue={sessionId}
                />
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
                    className="px-3 py-1.5 border border-neutral-700 hover:border-neutral-500 hover:text-neutral-100 transition-all text-[10px] font-mono text-neutral-400 uppercase tracking-widest flex items-center gap-2"
                  >
                    <ExternalLink size={10} />
                    Raw Link
                  </a>
                )}
              <button
                onClick={() => setAllExpanded(!allExpanded)}
                className="px-3 py-1.5 border border-neutral-700 hover:border-neutral-500 hover:text-neutral-100 transition-all text-[10px] font-mono text-neutral-400 uppercase tracking-widest"
              >
                {allExpanded ? "Collapse View" : "Expand All"}
              </button>
              <button
                onClick={() => setRightPanelOpen(!rightPanelOpen)}
                className={cx(
                  "w-8 h-8 flex items-center justify-center border transition-all text-sm font-mono",
                  rightPanelOpen
                    ? "bg-brand-600 border-brand-500 text-white"
                    : "border-neutral-700 text-neutral-400 hover:border-neutral-500 hover:text-neutral-100"
                )}
                title="Toggle Detailed Metrics"
              >
                {rightPanelOpen ? <X size={14} /> : <ChevronRight size={14} />}
              </button>
            </div>
          </div>

          <div className="grid grid-cols-5 gap-1.5">
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
              value={report ? fmtUsd(report.total_lemoncrow_savings_usd) : "—"}
              tone="emerald"
            />
            <HeaderStat
              label="Tokens"
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
                      const out =
                        report?.output_tokens ??
                        trace?.output_tokens ??
                        inspectorData?.tokens_post ??
                        0;
                      return `${fmtTok(newIn + cw)} / ${fmtTok(out)}`;
                    })()
                  : "—"
              }
              title={
                report
                  ? `in: ${fmtTok(report.input_tokens)} new + ${fmtTok(report.cache_write_tokens)} cache-write · out: ${fmtTok(report.output_tokens)} · cache-read: ${fmtTok(report.cache_read_tokens)}`
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
          <div className="flex-1 overflow-y-auto custom-scrollbar bg-surface">
            <div className="p-10 space-y-16 pb-48">
              <section className="space-y-12">
                <div className="flex items-center gap-6">
                  <h2 className="text-[10px] font-black uppercase tracking-[0.5em] text-neutral-400 whitespace-nowrap">
                    Execution Flow
                  </h2>
                  <div className="h-px w-full bg-gradient-to-r from-neutral-800 to-transparent" />
                </div>

                <div className="space-y-12">
                  {enrichedConversations.length > 0 ? (
                    (() => {
                      const seen = new Map<string, number>();
                      return groupTurns(enrichedConversations).map((turn) => {
                        // Stable per-turn key so React preserves each
                        // ConversationTurn's expand state when the list is
                        // re-enriched (savings reattachment) or turns are
                        // inserted/prepended. tool_use_id is unique when
                        // present; otherwise disambiguate same-(kind, at)
                        // turns by occurrence order.
                        const base =
                          turn.tool_use_id || `${turn.kind}:${turn.at}`;
                        const n = seen.get(base) ?? 0;
                        seen.set(base, n + 1);
                        const key = n === 0 ? base : `${base}#${n}`;
                        return (
                          <ConversationTurn
                            key={key}
                            turn={turn}
                            forceExpand={allExpanded}
                          />
                        );
                      });
                    })()
                  ) : (
                    <div className="space-y-8">
                      {trace?.reasoning && trace.reasoning.length > 0 && (
                        <div className="space-y-4">
                          <h3 className="text-[10px] font-bold uppercase tracking-widest text-neutral-400 px-1">
                            Strategy
                          </h3>
                          {trace.reasoning.map((r, i) => (
                            <div
                              key={i}
                              className="bg-brand-950/[0.03] border border-brand-900/10 p-5 text-[11px] leading-relaxed text-brand-400/60 font-mono whitespace-pre-wrap rounded-sm shadow-inner"
                            >
                              {r}
                            </div>
                          ))}
                        </div>
                      )}
                      <div className="space-y-4">
                        <h3 className="text-[10px] font-bold uppercase tracking-widest text-neutral-400 px-1">
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
                    <h2 className="text-[10px] font-black uppercase tracking-[0.5em] text-neutral-400 whitespace-nowrap">
                      File Changes
                    </h2>
                    <div className="h-px w-full bg-gradient-to-r from-neutral-800 to-transparent" />
                    <span className="text-[10px] text-neutral-400 font-mono font-bold uppercase tracking-widest flex-shrink-0">
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
            <aside className="w-96 flex-shrink-0 border-l border-neutral-800/60 bg-surface-raised/40 overflow-y-auto custom-scrollbar p-6 space-y-10 animate-in slide-in-from-right duration-300">
              <section className="space-y-4">
                <h3 className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-400 border-b border-neutral-800 pb-2">
                  Session Blueprint
                </h3>
                <div className="grid gap-4">
                  <SidebarMetric
                    label="Input Cost"
                    value={report ? fmtUsd(report.input_token_cost_usd) : "—"}
                  />
                  <SidebarMetric
                    label="Output Cost"
                    value={report ? fmtUsd(report.output_token_cost_usd) : "—"}
                  />
                  <SidebarMetric
                    label="Cache Read Cost"
                    value={report ? fmtUsd(report.cache_read_cost_usd) : "—"}
                  />
                </div>
                {(routeScore !== null || compactScore !== null) && (
                  <div className="flex flex-wrap gap-2 pt-1">
                    {routeScore !== null && (
                      <MetaPill
                        label="Route outcome"
                        value={routeScore.toFixed(2)}
                        tone="violet"
                      />
                    )}
                    {compactScore !== null && (
                      <MetaPill
                        label="Compact outcome"
                        value={compactScore.toFixed(2)}
                        tone="amber"
                      />
                    )}
                  </div>
                )}
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
                          <span className="text-blue-300 truncate pr-4">
                            {t.tool} ({t.calls})
                          </span>
                          <span className="text-neutral-400">
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
                        <span className="text-emerald-300 truncate pr-4">
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
                            <span className="text-violet-300 truncate pr-4">
                              {model}
                            </span>
                            <span className="text-neutral-400">
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
                            <span className="text-neutral-400 uppercase font-bold tracking-tighter text-[10px]">
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
                  color="text-amber-300"
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
                    color="text-sky-300"
                  />
                )}
              {inspectorData?.pinned_blocks &&
                inspectorData.pinned_blocks.length > 0 && (
                  <SidebarList
                    title="Pinned Logic"
                    items={inspectorData.pinned_blocks}
                    color="text-brand-400/70"
                  />
                )}
              {inspectorData?.recalled_passages &&
                inspectorData.recalled_passages.length > 0 && (
                  <SidebarList
                    title="Memory Recall"
                    items={inspectorData.recalled_passages.map((p) => p.id)}
                    color="text-cyan-300"
                  />
                )}

              <section className="space-y-3 opacity-60 hover:opacity-100 transition-opacity pt-4 border-t border-neutral-800">
                <h3 className="text-[10px] font-black uppercase tracking-widest text-neutral-400">
                  Audit Telemetry
                </h3>
                <div className="space-y-1.5 text-[10px] font-mono text-neutral-400">
                  {report?.telemetry &&
                    Object.entries(report.telemetry).map(([key, val], i) => (
                      <div
                        key={i}
                        className="flex justify-between border-b border-neutral-800/20 pb-0.5 last:border-0"
                      >
                        <span className="uppercase text-[10px] font-bold">
                          {key}
                        </span>
                        <span className="text-neutral-400">{String(val)}</span>
                      </div>
                    ))}
                  {inspectorData?.summarized_events_count ? (
                    <div className="flex justify-between border-b border-neutral-800/20 pb-0.5 last:border-0">
                      <span className="uppercase text-[10px] font-bold text-amber-600/80">
                        Compressed_Events
                      </span>
                      <span className="text-amber-300">
                        {inspectorData.summarized_events_count}
                      </span>
                    </div>
                  ) : null}
                  {inspectorData?.tokens_pre && (
                    <div className="flex justify-between border-b border-neutral-800/20 pb-0.5 last:border-0">
                      <span className="uppercase text-[10px] font-bold">
                        Context_Pre
                      </span>
                      <span className="text-neutral-400">
                        {fmtTok(inspectorData.tokens_pre)}
                      </span>
                    </div>
                  )}
                  {inspectorData?.tokens_post && (
                    <div className="flex justify-between border-b border-neutral-800/20 pb-0.5 last:border-0">
                      <span className="uppercase text-[10px] font-bold text-emerald-600/80">
                        Context_Post
                      </span>
                      <span className="text-emerald-300">
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
