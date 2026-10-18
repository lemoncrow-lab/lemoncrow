import { Button as UiButton } from "../../components/ui/button";
import { useEffect, useState, useMemo } from "react";
import { ExternalLink, ChevronRight, X } from "lucide-react";
import {
  api,
  apiUrl,
  type Trace,
  type SessionReport,
  type RunInspectorData,
} from "../../api";
import { cx } from "../../components/WorkbenchUI";
import { useRuntimeCapabilities } from "../../lib/RuntimeCapabilities";
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
  tone?: "warning" | "success" | "neutral";
  title?: string;
}) {
  return (
    <div
      className="group flex min-w-0 items-center justify-between gap-3 rounded-md border border-border/70 bg-background/60 px-2.5 py-1.5 transition-colors hover:bg-muted/50"
      title={title}
    >
      <div className="truncate text-caption text-neutral-400 uppercase font-black tracking-[0.18em] group-hover:text-neutral-400 transition-colors">
        {label}
      </div>
      <div
        className={cx(
          "truncate text-caption font-bold font-mono leading-none",
          tone === "warning"
            ? "text-amber-300"
            : tone === "success"
              ? "text-emerald-300"
              : tone === "neutral"
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
      <span className="text-caption text-neutral-400 font-mono uppercase font-bold">
        {label}
      </span>
      <span className={cx("text-caption font-mono font-black", color)}>
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
  // Raw files and artifacts live on the developer machine; a hosted server has no route for them.
  const localFiles = useRuntimeCapabilities().localMachine;

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
      <h3 className="text-caption font-black uppercase tracking-widest text-neutral-400">
        {title}
      </h3>
      <div className="space-y-1.5 font-mono text-caption">
        {items.map((item) => {
          const p = typeof item === "string" ? item : item.path;
          const artId = typeof item === "string" ? null : item.artifact_id;
          const isPath = p.startsWith("/");
          const canOpenRaw = localFiles && Boolean(artId || isPath);
          const rawUrl = !localFiles
            ? undefined
            : artId
              ? apiUrl(`/raw-artifacts/${artId}/content`)
              : apiUrl(`/v1/files/content?path=${encodeURIComponent(p)}`);
          const projectionUrl = localFiles && isPath
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
                "group/item flex items-center justify-between border-l border-border/60 pl-2 transition-colors",
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
                <UiButton unstyled
                  type="button"
                  onClick={() => copyToClipboard(p)}
                  className={cx(
                    "text-caption uppercase font-black flex items-center gap-1",
                    justCopied
                      ? "text-emerald-300"
                      : "text-neutral-400 hover:text-sky-300"
                  )}
                  title={`Copy path: ${p}`}
                >
                  {justCopied ? "Copied" : "Copy"}
                </UiButton>
                {projectionUrl && (
                  <a
                    href={projectionUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="text-caption text-neutral-400 hover:text-cyan-300 uppercase font-black"
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
                    className="text-caption text-neutral-400 hover:text-emerald-300 uppercase font-black flex items-center gap-1"
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
  tone?: "neutral" | "neutral" | "warning";
  /** When set, the pill truncates `value` and copies this on click. */
  copyValue?: string;
}) {
  const [copied, setCopied] = useState(false);
  const toneClass =
    tone === "neutral"
      ? "border-violet-900/40 bg-background/60 text-violet-300"
      : tone === "warning"
        ? "border-amber-900/40 bg-background/60 text-amber-300"
        : "border-border bg-background/60 text-neutral-400";

  if (copyValue === undefined) {
    return (
      <span
        className={cx(
          "inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-caption font-mono uppercase tracking-[0.2em]",
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
    <UiButton unstyled
      type="button"
      onClick={handleCopy}
      title={`Copy: ${copyValue}`}
      className={cx(
        "inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-caption font-mono uppercase tracking-[0.2em] transition hover:bg-muted/50",
        toneClass
      )}
    >
      <span className="text-neutral-400">{label}</span>
      <span className="normal-case tracking-normal text-current">
        {copied ? "Copied" : truncateMiddle(value)}
      </span>
    </UiButton>
  );
}

function usageSourceLabel(source?: string): string {
  switch (source) {
    case "claude_mod":
      return "Claude Code measured";
    case "host_transcript":
      return "Host transcript measured";
    case "lemoncrow_gateway":
      return "LemonCrow gateway measured";
    case "unavailable":
      return "Unavailable from host";
    default:
      return source ? source.replaceAll("_", " ") : "Recorded session";
  }
}

function costSourceLabel(source?: string): string {
  switch (source) {
    case "claude_mod":
    case "host_reported":
      return "Host measured";
    case "priced":
    case "claude_mod_tokens_priced":
      return "Priced from measured usage";
    case "unavailable":
      return "Unavailable from host";
    default:
      return source ? source.replaceAll("_", " ") : "Recorded session";
  }
}

// ---------------------------------------------------------------------------
// Main detail view
// ---------------------------------------------------------------------------

export function SessionExplorerDetail({ sessionId }: { sessionId: string }) {
  const localFiles = useRuntimeCapabilities().localMachine;
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
    // A late response for the previous run must not overwrite this one.
    let cancelled = false;
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
        if (cancelled) return;
        if (!rep && !tr && !led) {
          setErr("This run is not available.");
          setLoading(false);
          return;
        }
        setReport(rep);
        setTrace(tr);
        if (led) setInspectorData(parseInspectorData(sessionId, led));
        setOutcomes(outcomeEntries);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
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
      // Extract the short name: "mcp__plugin_lemoncrow_lc__read" → "read"
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
        <div className="text-caption text-neutral-400 uppercase tracking-[0.3em] font-mono animate-pulse">
          Reconstructing Ledger...
        </div>
      </div>
    );

  if (err)
    return (
      <div className="h-full flex items-center justify-center bg-surface px-6 py-8 text-center">
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
      <header className="sticky top-0 z-20 flex-shrink-0 border-b border-border/80 bg-background/95 px-4 py-2.5 backdrop-blur-md">
        <div className="space-y-2">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
            <div className="min-w-0 flex-1 space-y-1.5">
              <h1 className="text-sm font-bold tracking-wide text-foreground truncate uppercase">
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
                  <MetaPill label="Model" value={startedModel} tone="neutral" />
                )}
                <MetaPill
                  label="Agent"
                  value={`@${trace?.agent || "unknown"}`}
                  tone="warning"
                />
              </div>
            </div>

            <div className="flex items-center gap-3 self-start">
              {localFiles && report?.raw_artifact_ids &&
                report.raw_artifact_ids.length > 0 && (
                  <a
                    href={apiUrl(`/raw-artifacts/${report.raw_artifact_ids[0]}/content`)}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border/80 bg-muted/55 px-2.5 text-caption font-medium text-neutral-400 transition-colors hover:bg-neutral-800/80 hover:text-foreground"
                  >
                    <ExternalLink size={10} />
                    Raw Link
                  </a>
                )}
              <UiButton
                onClick={() => setAllExpanded(!allExpanded)}
                size="xs"
                variant="outline"
              >
                {allExpanded ? "Collapse View" : "Expand All"}
              </UiButton>
              <UiButton
                onClick={() => setRightPanelOpen(!rightPanelOpen)}
                size="icon"
                variant="outline"
                active={rightPanelOpen}
                aria-label="Toggle Detailed Metrics"
                tooltip="Toggle Detailed Metrics"
              >
                {rightPanelOpen ? <X size={14} /> : <ChevronRight size={14} />}
              </UiButton>
            </div>
          </div>

          <div className="grid grid-cols-5 gap-1.5">
            <HeaderStat
              label={report?.actual_cost_source === "priced" ? "Priced Cost" : "Cost"}
              value={
                report?.actual_cost_source === "unavailable"
                  ? "—"
                  : report
                    ? fmtUsd(report.total_cost_usd)
                    : trace?.input_tokens
                      ? "..."
                      : "—"
              }
              title={report ? costSourceLabel(report.actual_cost_source) : undefined}
              tone="warning"
            />
            <HeaderStat
              label="Est. Saved"
              value={report ? fmtUsd(report.total_lemoncrow_savings_usd) : "—"}
              tone="success"
            />
            <HeaderStat
              label="Tokens"
              value={
                report?.usage_measurement_source === "unavailable"
                  ? "—"
                  : report || trace || inspectorData
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
              value={
                report
                  ? String(report.measured_total_turns || report.total_turns)
                  : "—"
              }
              title={
                report?.measured_subagent_count
                  ? `${report.measured_main_turns ?? 0} main · ${report.measured_subagent_turns ?? 0} subagent`
                  : undefined
              }
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
            <div className="space-y-4 p-4 pb-16">
              <section className="space-y-4">
                <div className="flex items-center gap-3">
                  <h2 className="whitespace-nowrap text-caption font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                    Execution Flow
                  </h2>
                  <div className="h-px w-full bg-neutral-800/80" />
                </div>

                <div className="space-y-4">
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
                    <div className="space-y-4">
                      {trace?.reasoning && trace.reasoning.length > 0 && (
                        <div className="space-y-4">
                          <h3 className="text-caption font-bold uppercase tracking-widest text-neutral-400 px-1">
                            Strategy
                          </h3>
                          {trace.reasoning.map((r, i) => (
                            <div
                              key={i}
                              className="rounded-lg border border-border/70 bg-background/60 p-3 font-mono text-label leading-relaxed text-neutral-400 whitespace-pre-wrap"
                            >
                              {r}
                            </div>
                          ))}
                        </div>
                      )}
                      <div className="space-y-4">
                        <h3 className="text-caption font-bold uppercase tracking-widest text-neutral-400 px-1">
                          Events
                        </h3>
                        <div className="space-y-2">
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
                <section className="space-y-4 border-t border-border/70 pt-4">
                  <div className="flex items-center gap-3">
                    <h2 className="whitespace-nowrap text-caption font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                      File Changes
                    </h2>
                    <div className="h-px w-full bg-neutral-800/80" />
                    <span className="flex-shrink-0 font-mono text-caption font-medium text-muted-foreground">
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
            <aside className="w-96 flex-shrink-0 space-y-4 overflow-y-auto border-l border-border/70 bg-background/70 p-4 custom-scrollbar animate-in slide-in-from-right duration-300">
              <section className="space-y-4">
                <h3 className="border-b border-border pb-2 text-caption font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  Session Blueprint
                </h3>
                <div className="grid gap-3">
                  <SidebarMetric
                    label="Input Cost"
                    value={
                      report?.actual_cost_source === "unavailable"
                        ? "—"
                        : report
                          ? fmtUsd(report.input_token_cost_usd)
                          : "—"
                    }
                  />
                  <SidebarMetric
                    label="Output Cost"
                    value={
                      report?.actual_cost_source === "unavailable"
                        ? "—"
                        : report
                          ? fmtUsd(report.output_token_cost_usd)
                          : "—"
                    }
                  />
                  <SidebarMetric
                    label="Cache Read Cost"
                    value={
                      report?.actual_cost_source === "unavailable"
                        ? "—"
                        : report
                          ? fmtUsd(report.cache_read_cost_usd)
                          : "—"
                    }
                  />
                  <SidebarMetric
                    label="Cache Write Cost"
                    value={
                      report?.actual_cost_source === "unavailable"
                        ? "—"
                        : report
                          ? fmtUsd(report.cache_write_cost_usd)
                          : "—"
                    }
                  />
                </div>
                {(routeScore !== null || compactScore !== null) && (
                  <div className="flex flex-wrap gap-2 pt-1">
                    {routeScore !== null && (
                      <MetaPill
                        label="Route outcome"
                        value={routeScore.toFixed(2)}
                        tone="neutral"
                      />
                    )}
                    {compactScore !== null && (
                      <MetaPill
                        label="Compact outcome"
                        value={compactScore.toFixed(2)}
                        tone="warning"
                      />
                    )}
                  </div>
                )}
              </section>

              {report && (
                <section className="space-y-4">
                  <h3 className="border-b border-border pb-2 text-caption font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                    Measurement
                  </h3>
                  <div className="grid gap-3">
                    <SidebarMetric
                      label="Usage"
                      value={usageSourceLabel(report.usage_measurement_source)}
                    />
                    <SidebarMetric
                      label="Cost"
                      value={costSourceLabel(report.actual_cost_source)}
                    />
                    {report.usage_measurement_source === "claude_mod" && (
                      <>
                        <SidebarMetric
                          label="Measured Requests"
                          value={String(report.measured_request_steps ?? 0)}
                        />
                        <SidebarMetric
                          label="Main"
                          value={`${report.measured_main_turns ?? 0} turns`}
                        />
                        <SidebarMetric
                          label="Subagents"
                          value={`${report.measured_subagent_count ?? 0} agents · ${report.measured_subagent_turns ?? 0} turns`}
                        />
                      </>
                    )}
                  </div>
                </section>
              )}

              {report?.top_tools_by_cost &&
                report.top_tools_by_cost.length > 0 && (
                  <section className="space-y-4">
                    <h3 className="border-b border-border pb-2 text-caption font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                      Tool Breakdown
                    </h3>
                    <div className="space-y-2">
                      {report.top_tools_by_cost.map((t, i) => (
                        <div
                          key={i}
                          className="flex items-center justify-between text-caption font-mono border-b border-border/40 pb-1 last:border-0"
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
                  <h3 className="border-b border-border pb-2 text-caption font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                    Context Savings by Tool
                  </h3>
                  <div className="space-y-2">
                    {report.tool_savings.map((row, i) => (
                      <div
                        key={i}
                        className="flex items-center justify-between text-caption font-mono border-b border-border/40 pb-1 last:border-0"
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
                    <h3 className="border-b border-border pb-2 text-caption font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                      Models Involved
                    </h3>
                    <div className="space-y-2 text-caption font-mono">
                      {Object.entries(report.models_used).map(
                        ([model, count], i) => (
                          <div
                            key={i}
                            className="flex items-center justify-between border-b border-border/40 pb-1 last:border-0"
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
                    <h3 className="border-b border-border pb-2 text-caption font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                      Agent Config
                    </h3>
                    <div className="space-y-2 text-caption font-mono">
                      {Object.entries(report.agent_settings).map(
                        ([key, val], i) => (
                          <div
                            key={i}
                            className="flex flex-col border-b border-border/40 pb-1 last:border-0 gap-0.5"
                          >
                            <span className="text-neutral-400 uppercase font-bold tracking-tighter text-caption">
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

              <section className="space-y-3 opacity-60 hover:opacity-100 transition-opacity pt-4 border-t border-border">
                <h3 className="text-caption font-black uppercase tracking-widest text-neutral-400">
                  Audit Telemetry
                </h3>
                <div className="space-y-1.5 text-caption font-mono text-neutral-400">
                  {report?.telemetry &&
                    Object.entries(report.telemetry).map(([key, val], i) => (
                      <div
                        key={i}
                        className="flex justify-between border-b border-border/20 pb-0.5 last:border-0"
                      >
                        <span className="uppercase text-caption font-bold">
                          {key}
                        </span>
                        <span className="text-neutral-400">{String(val)}</span>
                      </div>
                    ))}
                  {inspectorData?.summarized_events_count ? (
                    <div className="flex justify-between border-b border-border/20 pb-0.5 last:border-0">
                      <span className="uppercase text-caption font-bold text-amber-600/80">
                        Compressed_Events
                      </span>
                      <span className="text-amber-300">
                        {inspectorData.summarized_events_count}
                      </span>
                    </div>
                  ) : null}
                  {inspectorData?.tokens_pre && (
                    <div className="flex justify-between border-b border-border/20 pb-0.5 last:border-0">
                      <span className="uppercase text-caption font-bold">
                        Context_Pre
                      </span>
                      <span className="text-neutral-400">
                        {fmtTok(inspectorData.tokens_pre)}
                      </span>
                    </div>
                  )}
                  {inspectorData?.tokens_post && (
                    <div className="flex justify-between border-b border-border/20 pb-0.5 last:border-0">
                      <span className="uppercase text-caption font-bold text-emerald-600/80">
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
