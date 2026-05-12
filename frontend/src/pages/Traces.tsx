import { useEffect, useState, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import {
  api,
  type Trace,
  type CommandRecord,
  type FileEditRecord,
  type ToolCall,
  type TraceListResponse,
} from "../api";
import RunInspectorDrawer from "../components/RunInspectorDrawer";

export default function Traces() {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialQuery = searchParams.get("q") ?? "";
  const [items, setItems] = useState<Trace[] | null>(null);
  const [metrics, setMetrics] = useState<TraceListResponse["metrics"] | null>(
    null
  );
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<
    "all" | "success" | "failed" | "partial"
  >("all");
  const [domainFilter, setDomainFilter] = useState<string>("all");
  const [hostFilter, setHostFilter] = useState<string>("all");
  const [query, setQuery] = useState<string>(initialQuery);
  const [searchInput, setSearchInput] = useState<string>(initialQuery);
  const [page, setPage] = useState(0);

  useEffect(() => {
    const urlQuery = searchParams.get("q") ?? "";
    setSearchInput((prev) => (prev === urlQuery ? prev : urlQuery));
    setQuery((prev) => (prev === urlQuery ? prev : urlQuery));
  }, [searchParams]);

  // Debounce search input
  useEffect(() => {
    const timer = setTimeout(() => {
      const nextQuery = searchInput.trim();
      setQuery((prev) => (prev === nextQuery ? prev : nextQuery));

      const currentQuery = searchParams.get("q") ?? "";
      if (currentQuery === nextQuery) return;

      const next = new URLSearchParams(searchParams);
      if (nextQuery) {
        next.set("q", nextQuery);
      } else {
        next.delete("q");
      }
      setSearchParams(next, { replace: true });
    }, 250);
    return () => clearTimeout(timer);
  }, [searchInput, searchParams, setSearchParams]);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [inspectorTrace, setInspectorTrace] = useState<Trace | null>(null);
  const deepLinkedTraceId = searchParams.get("trace");

  const setTraceQuery = (traceId: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (traceId) {
      next.set("trace", traceId);
    } else {
      next.delete("trace");
    }
    setSearchParams(next);
  };

  // Fetch traces when filters change
  useEffect(() => {
    setLoading(true);
    setPage(0);
    setErr(null);
    api
      .traces(50, 0, domainFilter, hostFilter, query)
      .then((res) => {
        setItems(res.items);
        setMetrics(res.metrics);
        setHasMore(res.items.length >= 50);
        setLoading(false);
      })
      .catch((e) => {
        setErr(String(e));
        setLoading(false);
      });
  }, [domainFilter, hostFilter, query]);

  useEffect(() => {
    if (!deepLinkedTraceId) {
      setInspectorTrace(null);
      return;
    }

    const localTrace = items?.find((trace) => trace.id === deepLinkedTraceId);
    if (localTrace) {
      setExpandedId(localTrace.id);
      setInspectorTrace(localTrace);
      return;
    }

    let cancelled = false;
    api
      .trace(deepLinkedTraceId)
      .then((trace) => {
        if (cancelled) return;
        setExpandedId(trace.id);
        setInspectorTrace(trace);
        setItems((prev) => {
          if (!prev) return [trace];
          if (prev.some((item) => item.id === trace.id)) return prev;
          return [trace, ...prev];
        });
      })
      .catch((error) => {
        if (!cancelled) {
          setErr(String(error));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [deepLinkedTraceId, items]);

  const loadMore = () => {
    if (loading || !hasMore) return;
    setLoading(true);
    const nextOffset = (page + 1) * 50;
    api
      .traces(50, nextOffset, domainFilter, hostFilter, query)
      .then((res) => {
        setItems((prev) => (prev ? [...prev, ...res.items] : res.items));
        setMetrics(res.metrics);
        setHasMore(res.items.length >= 50);
        setPage((p) => p + 1);
        setLoading(false);
      })
      .catch((e) => {
        setErr(String(e));
        setLoading(false);
      });
  };

  // Status filtering remains client-side for immediate response,
  // but aggregate counts come from metrics.stats (global)
  const filtered = useMemo(() => {
    if (!items) return [];
    return items.filter((t) => {
      if (filter !== "all" && t.status !== filter) return false;
      return true;
    });
  }, [items, filter]);

  // Derived from global metrics
  const hosts = useMemo(() => {
    if (!metrics) return [];
    // We use all unique hosts from the database metrics
    return [...new Set(metrics.hosts.map(extractHost))];
  }, [metrics]);

  const domains = useMemo(() => {
    if (!metrics) return [];
    return metrics.domains;
  }, [metrics]);

  const searchActive = query.length > 0;
  const searchPending = searchInput.trim() !== query;

  if (err) return <div className="text-red-400">Error: {err}</div>;
  if (!items && !loading)
    return <div className="text-neutral-500">No traces found.</div>;

  const toggleExpanded = (id: string) => {
    setExpandedId((prev) => (prev === id ? null : id));
  };

  const openInspector = (trace: Trace) => {
    setTraceQuery(trace.id);
    setExpandedId(trace.id);
    setInspectorTrace(trace);
  };

  return (
    <div className="space-y-6">
      <section className="border border-neutral-800 bg-neutral-950/70 p-5">
        <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_280px]">
          <label className="block">
            <div className="flex items-center justify-between gap-3 text-[10px] uppercase tracking-widest text-neutral-500">
              <span>Search All Runs</span>
              {searchPending && (
                <span className="text-amber-300/70">Updating…</span>
              )}
            </div>
            <div className="mt-2 flex gap-2">
              <input
                type="search"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder='Search tasks, reasoning, tools, commands, files, validations, and summaries. Use "" to search the whole word.'
                className="w-full border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 outline-none transition placeholder:text-neutral-600 focus:border-amber-500/50"
              />
              {searchInput && (
                <button
                  type="button"
                  onClick={() => setSearchInput("")}
                  className="border border-neutral-700 px-3 py-2 text-[10px] uppercase tracking-widest text-neutral-300 transition hover:border-amber-500/50 hover:text-amber-300"
                >
                  Clear
                </button>
              )}
            </div>
          </label>
        </div>
      </section>

      {/* Filters */}
      <div className="flex items-center justify-between gap-2 mb-4 flex-wrap">
        {/* Left: status + domain */}
        <div className="flex gap-2 flex-wrap items-center">
          {["all", "success", "failed", "partial"].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f as any)}
              className={`text-[10px] px-2.5 py-1 uppercase font-bold tracking-tight font-mono transition border ${
                filter === f
                  ? "border-neutral-500 bg-neutral-800 text-neutral-100"
                  : "border-neutral-700 text-neutral-500 hover:text-neutral-300"
              }`}
            >
              {f}
            </button>
          ))}
          <select
            aria-label="Filter traces by domain"
            value={domainFilter}
            onChange={(e) => setDomainFilter(e.target.value)}
            className="text-[10px] bg-neutral-900/50 border border-neutral-700 px-2 py-1 text-neutral-400 font-mono"
          >
            <option value="all">All domains</option>
            {domains.map((d: string) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </div>
        {/* Right: host buttons */}
        <div className="flex gap-2 flex-wrap items-center">
          {(["all", ...hosts] as string[]).map((h) => (
            <button
              key={h}
              onClick={() => setHostFilter(h)}
              className={`text-[10px] px-2.5 py-1 uppercase font-bold tracking-tight font-mono transition border ${
                hostFilter === h
                  ? "border-neutral-500 bg-neutral-800 text-neutral-100"
                  : "border-neutral-700 text-neutral-500 hover:text-neutral-300"
              }`}
            >
              {h === "all" ? "all hosts" : h}
            </button>
          ))}
        </div>
      </div>

      {searchActive && (
        <section className="border border-amber-900/40 bg-amber-950/10 p-4 text-sm text-neutral-300">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="text-[10px] uppercase tracking-widest text-amber-300/80">
                Cross-Session Search
              </div>
              <div className="mt-1 text-neutral-100">
                Showing matches for{" "}
                <span className="font-mono text-amber-200">{query}</span>
              </div>
            </div>
            <div className="text-[11px] font-mono text-neutral-400">
              {filtered.length}
              {hasMore ? "+" : ""} loaded match
              {filtered.length === 1 && !hasMore ? "" : "es"}
            </div>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-neutral-500">
            Each run card shows surrounding matched text so you can jump
            straight to the right session without leaving{" "}
            <span className="font-mono text-neutral-400">/runs</span>.
          </p>
        </section>
      )}

      {/* Traces List */}
      <div className="space-y-2">
        {filtered.map((t) => (
          <TraceCard
            key={t.id}
            trace={t}
            isExpanded={expandedId === t.id}
            onToggle={() => toggleExpanded(t.id)}
            onOpenInspector={() => openInspector(t)}
          />
        ))}
        {loading && (
          <div className="text-center py-4 text-neutral-500 italic font-mono text-xs">
            Loading more traces…
          </div>
        )}
        {!loading && filtered.length === 0 && (
          <div className="text-neutral-500 text-sm italic py-4 font-mono">
            {searchActive
              ? "No runs match the current cross-session search."
              : "No traces match the current filters."}
          </div>
        )}
        {!loading && hasMore && (
          <button
            onClick={loadMore}
            className="w-full py-2.5 border border-dashed border-neutral-700 text-xs text-neutral-400 hover:text-neutral-300 hover:border-neutral-500/50 transition font-mono"
          >
            Load More Traces
          </button>
        )}
      </div>

      <RunInspectorDrawer
        open={Boolean(inspectorTrace)}
        trace={inspectorTrace}
        onClose={() => {
          setInspectorTrace(null);
          setTraceQuery(null);
        }}
      />
    </div>
  );
}

function TraceCard({
  trace,
  isExpanded,
  onToggle,
  onOpenInspector,
}: {
  trace: Trace;
  isExpanded: boolean;
  onToggle: () => void;
  onOpenInspector: () => void;
}) {
  return (
    <div className="border border-neutral-800 bg-neutral-900/50 overflow-hidden transition-all">
      {/* Header */}
      <button
        onClick={onToggle}
        className="w-full px-5 py-4 text-left hover:bg-neutral-800/50 transition-colors flex items-start justify-between"
      >
        <div className="flex-1 flex items-start gap-4 min-w-0">
          {/* Icon/Status */}
          <div className="text-lg flex-shrink-0 mt-0.5">
            {trace.status === "success"
              ? "✓"
              : trace.status === "failed"
                ? "✗"
                : "◐"}
          </div>

          {/* Title & Details */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-1 flex-wrap">
              {/* Expandable indicator */}
              <span
                className={`text-neutral-500 font-mono text-xs transition-transform ${
                  isExpanded ? "rotate-90" : ""
                }`}
              >
                ❯
              </span>
              <div className="flex items-center gap-2 flex-wrap">
                <StatusBadge status={trace.status} />
                {(trace as any)._live && (
                  <span className="text-[10px] px-1.5 py-0.5 font-bold uppercase tracking-tight font-mono bg-cyan-900/40 text-cyan-300 animate-pulse">
                    LIVE
                  </span>
                )}
                {trace.domain && (
                  <span className="text-[10px] px-2 py-0.5 bg-neutral-800 text-neutral-300 uppercase font-bold tracking-tight font-mono">
                    {trace.domain}
                  </span>
                )}
                <HostBadge trace={trace} />
              </div>
            </div>
            <p className="font-mono text-sm text-neutral-200 mb-1">
              {trace.task}
            </p>
            {trace.snippets && trace.snippets.length > 0 && (
              <TraceSearchHits snippets={trace.snippets} />
            )}
            <div className="flex items-center gap-3 text-[10px] text-neutral-500 font-mono">
              <span>Session: {trace.session_id}</span>
              <span>Agent: {trace.agent}</span>
              <span>ID: {trace.id}</span>
              <span>{new Date(trace.created_at).toLocaleString()}</span>
            </div>
          </div>
        </div>
      </button>

      {/* Expanded Content */}
      {isExpanded && (
        <div className="border-t border-neutral-800 bg-neutral-950/50 px-5 py-4">
          <TraceDetail trace={trace} onOpenInspector={onOpenInspector} />
        </div>
      )}
    </div>
  );
}

function TraceSearchHits({ snippets }: { snippets: string[] }) {
  return (
    <div className="mb-2 mt-2 space-y-1.5">
      {snippets.slice(0, 4).map((snippet, index) => (
        <div
          key={`${snippet}-${index}`}
          className="border border-amber-900/30 bg-amber-950/10 px-2.5 py-1.5 text-[11px] leading-relaxed text-neutral-300"
        >
          <SnippetText text={snippet} />
        </div>
      ))}
    </div>
  );
}

function SnippetText({ text }: { text: string }) {
  const compact = text.replace(/\s+/g, " ").trim();
  const parts = compact.split(/(\[\[.*?\]\])/g);

  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith("[[") && part.endsWith("]]")) {
          return (
            <mark
              key={`${part}-${index}`}
              className="bg-amber-300/20 px-0.5 text-amber-100"
            >
              {part.slice(2, -2)}
            </mark>
          );
        }
        return <span key={`${part}-${index}`}>{part}</span>;
      })}
    </>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    success: "bg-emerald-900/40 text-emerald-400",
    failed: "bg-red-900/40 text-red-400",
    partial: "bg-amber-900/40 text-amber-400",
  };
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 font-bold uppercase tracking-tight font-mono ${
        map[status] || map.failed
      }`}
    >
      {status}
    </span>
  );
}

function TraceDetail({
  trace,
  onOpenInspector,
}: {
  trace: Trace;
  onOpenInspector: () => void;
}) {
  return (
    <div className="space-y-6 text-sm">
      <header>
        <div className="mt-3">
          <button
            type="button"
            aria-label="Open run inspector"
            onClick={onOpenInspector}
            className="text-[11px] px-2.5 py-1 border border-neutral-700 text-neutral-300 hover:text-amber-300 hover:border-amber-500/50 transition"
          >
            Open run inspector
          </button>
        </div>
      </header>

      {/* Reasoning section - show AI thinking/thought process */}
      {trace.reasoning && trace.reasoning.length > 0 && (
        <ReasoningSection reasoning={trace.reasoning} />
      )}

      <div className="grid gap-4">
        <div>
          <div className="text-[10px] uppercase font-bold tracking-widest text-neutral-500 mb-2">
            Tools Used
          </div>
          <div className="space-y-1">
            {trace.tools_called.map((t, i) => (
              <ToolCallDetail key={i} tool={t} />
            ))}
          </div>
        </div>
        <FilesTouchedSection
          files={trace.files_touched}
          runId={trace.session_id}
        />
      </div>

      {trace.commands_run.length > 0 && (
        <CommandsSection commands={trace.commands_run} />
      )}
      <Section title="Errors Seen" items={trace.errors_seen} variant="danger" />

      {trace.validation_results.length > 0 && (
        <div>
          <div className="text-[10px] uppercase font-bold tracking-widest text-neutral-500 mb-2">
            Validations
          </div>
          <ul className="space-y-1.5">
            {trace.validation_results.map((v, i) => (
              <li
                key={i}
                className={`p-2 border ${
                  v.passed
                    ? "bg-emerald-950/20 border-emerald-900/50 text-emerald-300"
                    : "bg-red-950/20 border-red-900/50 text-red-300"
                }`}
              >
                <div className="flex items-center gap-2 font-bold text-xs">
                  <span>{v.passed ? "✓" : "✗"}</span>
                  <span>{v.name}</span>
                </div>
                {v.detail && (
                  <div className="text-[11px] mt-1 opacity-80">{v.detail}</div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function CommandsSection({
  commands,
}: {
  commands: (string | CommandRecord)[];
}) {
  const [expanded, setExpanded] = useState(false);
  const display = expanded ? commands : commands.slice(0, 5);
  return (
    <div>
      <div className="text-[10px] uppercase font-bold tracking-widest text-neutral-500 mb-2">
        Commands Run{" "}
        <span className="text-neutral-600">({commands.length})</span>
      </div>
      <div className="space-y-1">
        {display.map((c, i) =>
          typeof c === "string" ? (
            <div
              key={i}
              className="text-[11px] font-mono text-neutral-300 bg-neutral-900/40 px-2 py-1 border border-neutral-800/50 truncate"
              title={c}
            >
              {c.length > 100 ? c.slice(0, 100) + "..." : c}
            </div>
          ) : (
            <CommandRecordDetail key={i} record={c} />
          )
        )}
      </div>
      {commands.length > 5 && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-[10px] text-neutral-500 hover:text-neutral-300 mt-2 underline"
        >
          {expanded ? "Show less" : `Show all ${commands.length} commands`}
        </button>
      )}
    </div>
  );
}

function ReasoningSection({ reasoning }: { reasoning: string[] }) {
  const [expanded, setExpanded] = useState(false);
  if (!reasoning || reasoning.length === 0) return null;

  const display = expanded ? reasoning : reasoning.slice(0, 3);
  return (
    <div className="border border-purple-800/50 bg-purple-950/20 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase font-bold tracking-widest text-purple-400">
          AI Reasoning / Thinking
        </div>
        <span className="text-[9px] text-purple-600">
          ({reasoning.length} blocks)
        </span>
      </div>
      <div className="space-y-2">
        {display.map((r, i) => (
          <div
            key={i}
            className="text-[11px] text-purple-200 leading-relaxed bg-neutral-900/40 px-2 py-1.5 border border-purple-900/30"
          >
            {r.length > 300 ? r.slice(0, 300) + "…" : r}
          </div>
        ))}
      </div>
      {reasoning.length > 3 && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-[10px] text-purple-400 hover:text-purple-300 mt-2 underline"
        >
          {expanded
            ? "Show less"
            : `Show all ${reasoning.length} reasoning blocks`}
        </button>
      )}
    </div>
  );
}

function FilesTouchedSection({
  files,
  runId,
}: {
  files: (string | FileEditRecord)[];
  runId?: string | null;
}) {
  if (!files || files.length === 0) return null;
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] uppercase font-bold tracking-widest text-neutral-500">
        Files Touched
      </div>
      <div className="space-y-1">
        {files.map((f) => {
          const path = typeof f === "string" ? f : f.path;
          const diff = typeof f === "string" ? undefined : f.diff;
          return (
            <FileRow key={path} path={path} runId={runId} inlineDiff={diff} />
          );
        })}
      </div>
    </div>
  );
}

function FileRow({
  path,
  runId,
  inlineDiff,
}: {
  path: string;
  runId?: string | null;
  inlineDiff?: string;
}) {
  const [open, setOpen] = useState(false);
  const [diffs, setDiffs] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // If we have an inline diff, use it directly without fetching the ledger
  const hasInlineDiff = !!inlineDiff;

  const handleClick = async () => {
    setOpen((o) => !o);
    if (hasInlineDiff || diffs !== null || loading) return;
    if (!runId) return;
    setLoading(true);
    try {
      const ledger = await api.ledger(runId);
      const events: any[] = ledger?.events ?? [];
      const collected = events
        .filter(
          (ev) =>
            ev.kind === "file_edit" &&
            ev.payload?.diff &&
            (ev.payload?.path === path ||
              ev.payload?.path?.endsWith("/" + path) ||
              path.endsWith("/" + (ev.payload?.path ?? "").split("/").pop()))
        )
        .map((ev) => ev.payload.diff as string);
      setDiffs(collected.length > 0 ? collected : []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  const filename = path.split("/").pop() ?? path;
  const canExpand = hasInlineDiff || !!runId;

  return (
    <div className="border border-neutral-800/50 overflow-hidden">
      <button
        onClick={handleClick}
        disabled={!canExpand}
        className={`w-full flex items-center justify-between px-2 py-1 text-left transition-colors ${
          canExpand
            ? "hover:bg-neutral-800/40 cursor-pointer"
            : "cursor-default"
        }`}
      >
        <span className="text-[11px] text-neutral-300 font-mono">{path}</span>
        {canExpand && (
          <span className="text-[9px] text-neutral-500 font-mono ml-2 flex-shrink-0">
            {open ? "▲ hide diff" : "▼ diff"}
          </span>
        )}
      </button>

      {open && (
        <div className="border-t border-neutral-800/50">
          {/* Show inline diff directly if available */}
          {hasInlineDiff && (
            <SideBySideDiffViewer diff={inlineDiff!} path={path} />
          )}
          {/* Otherwise fall back to ledger fetch */}
          {!hasInlineDiff && loading && (
            <div className="px-3 py-2 text-[11px] text-neutral-500 italic animate-pulse">
              Loading diff…
            </div>
          )}
          {!hasInlineDiff && error && (
            <div className="px-3 py-2 text-[11px] text-red-400">{error}</div>
          )}
          {!hasInlineDiff &&
            !loading &&
            !error &&
            diffs !== null &&
            diffs.length === 0 && (
              <div className="px-3 py-2 text-[11px] text-neutral-500 italic">
                No diff captured for {filename}.
              </div>
            )}
          {!hasInlineDiff &&
            diffs &&
            diffs.map((diff, i) => (
              <SideBySideDiffViewer key={i} diff={diff} path={path} />
            ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Side-by-side diff viewer
// ---------------------------------------------------------------------------

type DiffLine = {
  lineNo: number | null;
  content: string;
  type: "add" | "remove" | "context" | "header";
};

function parseDiffSides(raw: string): { left: DiffLine[]; right: DiffLine[] } {
  const lines = raw.split("\n");
  const left: DiffLine[] = [];
  const right: DiffLine[] = [];
  let leftNo = 1;
  let rightNo = 1;

  for (const line of lines) {
    if (line.startsWith("---") || line.startsWith("+++")) {
      left.push({ lineNo: null, content: line, type: "header" });
      right.push({ lineNo: null, content: line, type: "header" });
    } else if (line.startsWith("@@")) {
      // Parse hunk header: @@ -l,s +l,s @@
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) {
        leftNo = parseInt(m[1]);
        rightNo = parseInt(m[2]);
      }
      left.push({ lineNo: null, content: line, type: "header" });
      right.push({ lineNo: null, content: line, type: "header" });
    } else if (line.startsWith("-")) {
      left.push({ lineNo: leftNo++, content: line.slice(1), type: "remove" });
      right.push({ lineNo: null, content: "", type: "remove" });
    } else if (line.startsWith("+")) {
      left.push({ lineNo: null, content: "", type: "add" });
      right.push({ lineNo: rightNo++, content: line.slice(1), type: "add" });
    } else {
      const content = line.startsWith(" ") ? line.slice(1) : line;
      left.push({ lineNo: leftNo++, content, type: "context" });
      right.push({ lineNo: rightNo++, content, type: "context" });
    }
  }
  return { left, right };
}

function SideBySideDiffViewer({ diff, path }: { diff: string; path: string }) {
  const [expanded, setExpanded] = useState(true);
  const { left, right } = useMemo(() => parseDiffSides(diff), [diff]);

  const addedCount = right.filter(
    (l) => l.type === "add" && l.content !== ""
  ).length;
  const removedCount = left.filter(
    (l) => l.type === "remove" && l.content !== ""
  ).length;

  const lineClass = (type: DiffLine["type"], side: "left" | "right") => {
    if (type === "header") return "bg-neutral-900/60 text-neutral-500";
    if (type === "add" && side === "right")
      return "bg-emerald-950/40 text-emerald-300";
    if (type === "remove" && side === "left")
      return "bg-red-950/40 text-red-300";
    if (type === "add" || type === "remove")
      return "bg-transparent text-transparent select-none";
    return "text-neutral-400";
  };

  return (
    <div className="border-t border-neutral-800/30">
      {/* Diff header */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-neutral-900/40 border-b border-neutral-800/50">
        <div className="flex items-center gap-3 text-[10px] font-mono">
          <span className="text-neutral-400 truncate max-w-[300px]">
            {path}
          </span>
          {addedCount > 0 && (
            <span className="text-emerald-400">+{addedCount}</span>
          )}
          {removedCount > 0 && (
            <span className="text-red-400">-{removedCount}</span>
          )}
        </div>
        <button
          onClick={() => setExpanded((e) => !e)}
          className="text-[10px] text-neutral-500 hover:text-neutral-300 uppercase font-bold font-mono"
        >
          {expanded ? "collapse" : "expand"}
        </button>
      </div>

      {expanded && (
        <div className="flex overflow-x-auto max-h-[500px] overflow-y-auto">
          {/* Left (old) */}
          <div className="flex-1 min-w-0 border-r border-neutral-800/50">
            <div className="text-[9px] px-2 py-0.5 bg-red-950/20 text-red-400 font-mono font-bold border-b border-neutral-800/30">
              before
            </div>
            {left.map((line, i) => (
              <div
                key={i}
                className={`flex text-[10px] font-mono leading-5 ${lineClass(line.type, "left")}`}
              >
                <span className="w-8 flex-shrink-0 text-right pr-2 text-neutral-600 select-none border-r border-neutral-800/40 bg-black/20">
                  {line.lineNo ?? ""}
                </span>
                <span className="px-2 whitespace-pre overflow-hidden text-ellipsis flex-1">
                  {line.content}
                </span>
              </div>
            ))}
          </div>
          {/* Right (new) */}
          <div className="flex-1 min-w-0">
            <div className="text-[9px] px-2 py-0.5 bg-emerald-950/20 text-emerald-400 font-mono font-bold border-b border-neutral-800/30">
              after
            </div>
            {right.map((line, i) => (
              <div
                key={i}
                className={`flex text-[10px] font-mono leading-5 ${lineClass(line.type, "right")}`}
              >
                <span className="w-8 flex-shrink-0 text-right pr-2 text-neutral-600 select-none border-r border-neutral-800/40 bg-black/20">
                  {line.lineNo ?? ""}
                </span>
                <span className="px-2 whitespace-pre overflow-hidden text-ellipsis flex-1">
                  {line.content}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Enriched detail components
// ---------------------------------------------------------------------------

function ToolCallDetail({ tool }: { tool: ToolCall }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails =
    (tool.args && Object.keys(tool.args).length > 0) || tool.result_summary;
  return (
    <div className="border border-neutral-800/50 overflow-hidden">
      <div className="flex items-center gap-2 px-2 py-1 bg-neutral-900/40">
        <span className="text-[11px] px-2 py-0.5 bg-blue-900/30 text-blue-300 border border-blue-800/50">
          {tool.name}
          {tool.count > 1 ? ` ×${tool.count}` : ""}
        </span>
        {hasDetails && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[9px] text-neutral-500 hover:text-neutral-300 underline"
          >
            {expanded ? "hide" : "details"}
          </button>
        )}
      </div>
      {expanded && hasDetails && (
        <div className="px-2 py-1.5 space-y-1 border-t border-neutral-800/50 bg-neutral-950/40">
          {tool.args && Object.keys(tool.args).length > 0 && (
            <div>
              <div className="text-[9px] uppercase text-neutral-500 font-bold mb-0.5">
                Args
              </div>
              <pre className="text-[10px] bg-black/40 p-1.5 border border-neutral-800/50 overflow-auto max-h-32 text-neutral-300 font-mono whitespace-pre-wrap break-all">
                {JSON.stringify(tool.args, null, 2).slice(0, 1000)}
              </pre>
            </div>
          )}
          {tool.result_summary && (
            <div>
              <div className="text-[9px] uppercase text-neutral-500 font-bold mb-0.5">
                Result
              </div>
              <pre className="text-[10px] bg-black/40 p-1.5 border border-neutral-800/50 overflow-auto max-h-24 text-emerald-300/80 font-mono whitespace-pre-wrap break-all">
                {tool.result_summary}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CommandRecordDetail({ record }: { record: CommandRecord }) {
  const [expanded, setExpanded] = useState(false);
  const rc = record.exit_code;
  const ok = rc === 0 || rc === null || rc === undefined;
  return (
    <div className="border border-neutral-800/50 overflow-hidden">
      <div
        className="flex items-center gap-2 px-2 py-1 cursor-pointer hover:bg-neutral-800/30 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <pre className="text-[10px] font-mono text-amber-300 flex-1 truncate">
          $ {record.command}
        </pre>
        <span
          className={`text-[9px] font-mono font-bold ${ok ? "text-emerald-400" : "text-red-400"}`}
        >
          exit {rc ?? "?"}
        </span>
        <span className="text-[9px] text-neutral-500">
          {expanded ? "▲" : "▼"}
        </span>
      </div>
      {expanded && (
        <div className="border-t border-neutral-800/50 px-2 py-1.5 space-y-1 bg-neutral-950/40">
          {record.stdout && (
            <pre className="text-[9px] bg-black/40 p-1.5 border border-neutral-800/50 text-neutral-300 font-mono overflow-auto max-h-32 whitespace-pre-wrap break-all leading-relaxed">
              {record.stdout}
            </pre>
          )}
          {record.stderr && (
            <pre className="text-[9px] bg-red-950/20 p-1.5 border border-red-900/40 text-red-300 font-mono overflow-auto max-h-24 whitespace-pre-wrap break-all leading-relaxed">
              {record.stderr}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  items,
  variant,
}: {
  title: string;
  items: string[];
  variant?: string;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <div className="text-[10px] uppercase font-bold tracking-widest text-neutral-500 mb-2">
        {title}
      </div>
      <div className="space-y-1">
        {items.map((item, i) => (
          <div
            key={i}
            className={`p-2 border text-[11px] font-mono ${
              variant === "danger"
                ? "bg-red-950/20 border-red-900/50 text-red-300"
                : "bg-neutral-900/50 border-neutral-800 text-neutral-300"
            }`}
          >
            {item}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Host helpers
// ---------------------------------------------------------------------------

function extractHost(trace: Trace | string): string {
  const agent = typeof trace === "string" ? trace : trace.agent;
  const host = typeof trace === "string" ? null : trace.host;
  const id = typeof trace === "string" ? "" : trace.id;

  if (host) return host;

  // Derivations for legacy/imported sessions
  const a = agent.toLowerCase();
  const i = id.toLowerCase();

  if (i.startsWith("gemini-") || a === "gemini") return "gemini";
  if (i.startsWith("claude-") || a === "claude") return "claude";
  if (i.startsWith("codex-") || a === "codex") return "codex";
  if (i.startsWith("copilot-") || a === "copilot") return "copilot";
  if (i.startsWith("opencode-") || a === "opencode") return "opencode";

  // For native atelier runs where host wasn't recorded
  if (a.startsWith("atelier:")) return "atelier";

  return "unknown";
}

const HOST_COLORS: Record<string, string> = {
  atelier: "bg-amber-900/40 text-amber-300 border-amber-700/50",
  claude: "bg-violet-900/40 text-violet-300 border-violet-700/50",
  gemini: "bg-blue-900/40 text-blue-300 border-blue-700/50",
  copilot: "bg-sky-900/40 text-sky-300 border-sky-700/50",
  codex: "bg-teal-900/40 text-teal-300 border-teal-700/50",
  opencode: "bg-indigo-900/40 text-indigo-300 border-indigo-700/50",
};

function HostBadge({ trace }: { trace: Trace }) {
  const host = extractHost(trace);
  const cls =
    HOST_COLORS[host] ??
    "bg-neutral-800/60 text-neutral-400 border-neutral-700/50";
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 font-bold uppercase tracking-tight font-mono border ${cls}`}
    >
      {host}
    </span>
  );
}
