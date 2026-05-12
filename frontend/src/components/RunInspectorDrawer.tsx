import { type ReactNode, useEffect, useMemo, useState } from "react";
import { api, type RunInspectorData, type Trace } from "../api";

interface RunInspectorDrawerProps {
  open: boolean;
  trace: Trace | null;
  onClose: () => void;
}

function parseInspectorData(sessionId: string, ledger: any): RunInspectorData {
  const events: any[] = Array.isArray(ledger?.events) ? ledger.events : [];

  const recalled = events
    .filter((event) =>
      String(event?.kind || "")
        .toLowerCase()
        .includes("recall")
    )
    .flatMap((event) => {
      const payload = event?.payload || {};
      if (Array.isArray(payload.top_passages)) {
        return payload.top_passages.map((id: string) => ({
          id,
          source_ref: payload.source_ref || "",
        }));
      }
      if (payload.selected_passage_id) {
        return [
          {
            id: String(payload.selected_passage_id),
            source_ref: String(payload.source_ref || ""),
          },
        ];
      }
      return [];
    });

  let tokensPre: number | null = null;
  let tokensPost: number | null = null;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const payload = events[i]?.payload || {};
    const pre = payload.tokens_pre_summary ?? payload.tokens_pre;
    const post = payload.tokens_post_summary ?? payload.tokens_post;
    if (typeof pre === "number" && typeof post === "number") {
      tokensPre = pre;
      tokensPost = post;
      break;
    }
  }

  const summarizedEventsCount = events.reduce((acc, event) => {
    const payload = event?.payload || {};
    if (Array.isArray(payload.evicted_event_ids))
      return acc + payload.evicted_event_ids.length;
    if (Array.isArray(payload.summarized_events))
      return acc + payload.summarized_events.length;
    return acc;
  }, 0);

  return {
    session_id: sessionId,
    pinned_blocks: Array.isArray(ledger?.active_reasonblocks)
      ? ledger.active_reasonblocks
      : [],
    recalled_passages: recalled,
    summarized_events_count: summarizedEventsCount,
    tokens_pre: tokensPre,
    tokens_post: tokensPost,
    source_paths: Array.isArray(ledger?.source_paths)
      ? ledger.source_paths
      : [],
    conversations: Array.isArray(ledger?.conversations)
      ? ledger.conversations
      : [],
  };
}

type SessionSearchMatch = {
  id: string;
  section: string;
  label: string;
  snippet: string;
};

function normalizeSearchTerms(query: string): string[] {
  return Array.from(
    new Set(query.toLowerCase().trim().split(/\s+/).filter(Boolean))
  );
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function matchesSearch(value: string, terms: string[]): boolean {
  if (terms.length === 0) return true;
  const lower = value.toLowerCase();
  return terms.every((term) => lower.includes(term));
}

function buildSearchSnippet(
  value: string,
  terms: string[],
  radius = 90
): string {
  const compact = value.replace(/\s+/g, " ").trim();
  if (!compact) return "";
  if (terms.length === 0) return compact;

  const lower = compact.toLowerCase();
  let matchIndex = -1;
  let matchLength = 0;

  for (const term of terms) {
    const index = lower.indexOf(term);
    if (index === -1) continue;
    if (matchIndex === -1 || index < matchIndex) {
      matchIndex = index;
      matchLength = term.length;
    }
  }

  if (matchIndex === -1) {
    return compact.length > radius * 2
      ? `${compact.slice(0, radius * 2)}...`
      : compact;
  }

  const start = Math.max(0, matchIndex - radius);
  const end = Math.min(compact.length, matchIndex + matchLength + radius);

  return `${start > 0 ? "..." : ""}${compact.slice(start, end)}${end < compact.length ? "..." : ""}`;
}

function highlightSearchText(value: string, terms: string[]): ReactNode {
  if (!value || terms.length === 0) return value;

  const pattern = terms
    .slice()
    .sort((left, right) => right.length - left.length)
    .map(escapeRegExp)
    .join("|");

  if (!pattern) return value;

  const matcher = new RegExp(`(${pattern})`, "gi");
  const parts = value.split(matcher);

  return parts.map((part, index) =>
    terms.includes(part.toLowerCase()) ? (
      <mark
        key={`${part}-${index}`}
        className="bg-amber-300/20 px-0.5 text-amber-100"
      >
        {part}
      </mark>
    ) : (
      <span key={`${part}-${index}`}>{part}</span>
    )
  );
}

export default function RunInspectorDrawer({
  open,
  trace,
  onClose,
}: RunInspectorDrawerProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<RunInspectorData | null>(null);
  const [sessionQuery, setSessionQuery] = useState("");

  useEffect(() => {
    setSessionQuery("");
  }, [trace?.id]);

  useEffect(() => {
    // Use trace.id as the primary lookup key for the backend
    if (!open || !trace?.id) return;

    setLoading(true);
    setError(null);
    api
      .ledger(trace.id)
      .then((ledger) => {
        setData(parseInspectorData(trace.session_id || trace.id, ledger));
      })
      .catch((err) => {
        setError(String(err));
        setData({
          session_id: trace.session_id || trace.id,
          pinned_blocks: [],
          recalled_passages: [],
          summarized_events_count: 0,
          tokens_pre: null,
          tokens_post: null,
          source_paths: [],
          conversations: [],
        });
      })
      .finally(() => setLoading(false));
  }, [open, trace]);

  const title = useMemo(() => {
    if (!trace) return "Run Inspector";
    return trace.task ? `Run Inspector: ${trace.task}` : "Run Inspector";
  }, [trace]);

  const sessionTerms = useMemo(
    () => normalizeSearchTerms(sessionQuery),
    [sessionQuery]
  );

  const sessionSearch = useMemo(() => {
    const sourcePaths = data?.source_paths ?? [];
    const pinnedBlocks = data?.pinned_blocks ?? [];
    const recalledPassages = data?.recalled_passages ?? [];
    const conversations = data?.conversations ?? [];

    if (sessionTerms.length === 0) {
      return {
        sourcePaths,
        pinnedBlocks,
        recalledPassages,
        conversations,
        matches: [] as SessionSearchMatch[],
      };
    }

    const matches: SessionSearchMatch[] = [];
    const pushMatch = (
      id: string,
      section: string,
      label: string,
      text: string,
      radius = 90
    ) => {
      if (!matchesSearch(text, sessionTerms)) return false;
      matches.push({
        id,
        section,
        label,
        snippet: buildSearchSnippet(text, sessionTerms, radius),
      });
      return true;
    };

    if (trace) {
      pushMatch(
        "run-meta",
        "Run",
        trace.session_id || trace.id,
        [trace.task, trace.agent, trace.host, trace.session_id, trace.id]
          .filter(Boolean)
          .join("\n"),
        70
      );

      trace.tools_called.forEach((tool, index) => {
        pushMatch(
          `tool-${index}`,
          "Tool",
          tool.name,
          [
            tool.name,
            tool.result_summary || "",
            tool.args ? JSON.stringify(tool.args) : "",
          ]
            .filter(Boolean)
            .join("\n")
        );
      });

      trace.commands_run.forEach((command, index) => {
        const text =
          typeof command === "string"
            ? command
            : [command.command, command.stdout, command.stderr]
                .filter(Boolean)
                .join("\n");
        pushMatch(
          `command-${index}`,
          "Command",
          typeof command === "string" ? command : command.command,
          text,
          100
        );
      });

      trace.files_touched.forEach((file, index) => {
        const label = typeof file === "string" ? file : file.path;
        const text =
          typeof file === "string"
            ? file
            : [file.path, file.event, file.diff].filter(Boolean).join("\n");
        pushMatch(`trace-file-${index}`, "Trace file", label, text, 80);
      });
    }

    const filteredSourcePaths = sourcePaths.filter((path, index) =>
      pushMatch(`source-${index}`, "Source file", path, path, 60)
    );

    const filteredPinnedBlocks = pinnedBlocks.filter((blockId, index) =>
      pushMatch(`block-${index}`, "Pinned block", blockId, blockId, 60)
    );

    const filteredRecalledPassages = recalledPassages.filter((passage, index) =>
      pushMatch(
        `passage-${index}`,
        "Recalled passage",
        passage.id,
        [passage.id, passage.source_ref].filter(Boolean).join("\n"),
        80
      )
    );

    const filteredConversations = conversations.filter((turn, index) =>
      pushMatch(
        `turn-${index}`,
        "Timeline",
        turn.kind.replace(/_/g, " "),
        [turn.kind, turn.summary, turn.content].filter(Boolean).join("\n"),
        120
      )
    );

    return {
      sourcePaths: filteredSourcePaths,
      pinnedBlocks: filteredPinnedBlocks,
      recalledPassages: filteredRecalledPassages,
      conversations: filteredConversations,
      matches,
    };
  }, [data, sessionTerms, trace]);

  const sessionSearchActive = sessionTerms.length > 0;
  const visibleSourcePaths = sessionSearchActive
    ? sessionSearch.sourcePaths
    : (data?.source_paths ?? []);
  const visiblePinnedBlocks = sessionSearchActive
    ? sessionSearch.pinnedBlocks
    : (data?.pinned_blocks ?? []);
  const visibleRecalledPassages = sessionSearchActive
    ? sessionSearch.recalledPassages
    : (data?.recalled_passages ?? []);
  const visibleConversations = sessionSearchActive
    ? sessionSearch.conversations
    : (data?.conversations ?? []);

  if (!open || !trace) return null;

  return (
    <>
      <div
        className="fixed inset-0 bg-black/50 z-40"
        aria-hidden="true"
        onClick={onClose}
      />
      <aside
        className="fixed right-0 top-0 h-full w-full max-w-xl bg-neutral-950 border-l border-neutral-800 z-50 p-5 overflow-y-auto transition-transform"
        role="dialog"
        aria-modal="true"
        aria-label="Run inspector drawer"
      >
        <div className="flex items-start justify-between gap-3 pb-4 border-b border-neutral-800">
          <div>
            <h2 className="font-mono text-sm text-neutral-200 font-bold">
              {title}
            </h2>
            <div className="flex gap-4 text-[10px] text-neutral-500 mt-1 font-mono uppercase tracking-widest">
              <span>Session: {trace.session_id}</span>
              <span>Agent: {trace.agent}</span>
            </div>
            <div className="flex gap-4 text-[10px] text-neutral-500 mt-1 font-mono uppercase tracking-widest">
              <span>ID: {trace.id}</span>
              <span>{new Date(trace.created_at).toLocaleString()}</span>
            </div>
          </div>
          <button
            type="button"
            aria-label="Close run inspector"
            onClick={onClose}
            className="text-xs px-2 py-1 border border-neutral-700 text-neutral-300 hover:text-amber-300 hover:border-amber-500/50"
          >
            Close
          </button>
        </div>

        <div className="pt-4 pb-4 border-b border-neutral-900 space-y-2">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-[10px] uppercase tracking-widest text-neutral-500">
                Search This Session
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-neutral-600">
                Search only inside the currently open session: timeline, source
                files, pinned blocks, recalled passages, trace tools, commands,
                and file touches.
              </p>
            </div>
            {sessionSearchActive && (
              <div className="text-[10px] font-mono text-amber-300/80">
                {sessionSearch.matches.length} hits
              </div>
            )}
          </div>

          <div className="flex gap-2">
            <input
              type="search"
              value={sessionQuery}
              onChange={(e) => setSessionQuery(e.target.value)}
              placeholder="Search this run: timeline, files, commands, tools, passages..."
              className="w-full border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 outline-none transition placeholder:text-neutral-600 focus:border-amber-500/50"
            />
            {sessionQuery && (
              <button
                type="button"
                onClick={() => setSessionQuery("")}
                className="border border-neutral-700 px-3 py-2 text-[10px] uppercase tracking-widest text-neutral-300 transition hover:border-amber-500/50 hover:text-amber-300"
              >
                Clear
              </button>
            )}
          </div>
        </div>

        {loading && (
          <p className="text-xs text-neutral-500 pt-4">Loading run data...</p>
        )}
        {error && <p className="text-xs text-red-400 pt-4">{error}</p>}

        {data && (
          <div className="pt-4 space-y-5">
            {sessionSearchActive && (
              <section>
                <div className="flex items-center justify-between gap-3 mb-2">
                  <h3 className="text-[11px] uppercase tracking-widest text-neutral-500">
                    Session Search Results
                  </h3>
                  <span className="text-[10px] font-mono text-neutral-500">
                    {sessionSearch.matches.length} hits
                  </span>
                </div>
                {sessionSearch.matches.length === 0 ? (
                  <p className="text-xs text-neutral-600">
                    No matches inside this session.
                  </p>
                ) : (
                  <ul className="space-y-2">
                    {sessionSearch.matches.map((match) => (
                      <li
                        key={match.id}
                        className="border border-amber-900/30 bg-amber-950/10 p-2 rounded"
                      >
                        <div className="flex flex-wrap items-center gap-2 text-[10px] font-mono uppercase tracking-widest text-amber-300/80">
                          <span>{match.section}</span>
                          <span className="text-neutral-500 normal-case tracking-normal">
                            {match.label}
                          </span>
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-neutral-300">
                          {highlightSearchText(match.snippet, sessionTerms)}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )}

            <section className="grid grid-cols-2 gap-2">
              <div className="bg-neutral-900/50 border border-neutral-800 p-2 rounded">
                <div className="text-[9px] uppercase text-neutral-500 font-bold mb-1">
                  Host / Model
                </div>
                <div className="text-xs text-neutral-200 font-mono truncate">
                  {trace.host || "unknown"} · {trace.agent}
                </div>
              </div>
              <div className="bg-neutral-900/50 border border-neutral-800 p-2 rounded">
                <div className="text-[9px] uppercase text-neutral-500 font-bold mb-1">
                  Magnitude
                </div>
                <div className="text-xs text-neutral-200 font-mono">
                  {(
                    (trace.input_tokens || 0) +
                    (trace.output_tokens || 0) +
                    (trace.thinking_tokens || 0) +
                    (trace.cached_input_tokens || 0)
                  ).toLocaleString()}{" "}
                  tokens
                </div>
              </div>
              <div className="bg-neutral-900/50 border border-neutral-800 p-2 rounded">
                <div className="text-[9px] uppercase text-neutral-500 font-bold mb-1">
                  Activity
                </div>
                <div className="text-xs text-neutral-200 font-mono">
                  {trace.tools_called.length} tools ·{" "}
                  {trace.commands_run.length} commands
                </div>
              </div>
              <div className="bg-neutral-900/50 border border-neutral-800 p-2 rounded">
                <div className="text-[9px] uppercase text-neutral-500 font-bold mb-1">
                  Files
                </div>
                <div className="text-xs text-neutral-200 font-mono">
                  {trace.files_touched.length} paths touched
                </div>
              </div>
            </section>

            {(data.source_paths && data.source_paths.length > 0) ||
            sessionSearchActive ? (
              <section>
                <h3 className="text-[11px] uppercase tracking-widest text-neutral-500 mb-2">
                  Source Files
                </h3>
                {visibleSourcePaths.length === 0 ? (
                  <p className="text-xs text-neutral-600">
                    No source files match the current session search.
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {visibleSourcePaths.map((p) => (
                      <li key={p} className="flex items-center gap-2">
                        <span className="text-[11px] text-neutral-300 font-mono break-all">
                          {highlightSearchText(p, sessionTerms)}
                        </span>
                        <button
                          type="button"
                          className="text-[9px] text-amber-400/60 hover:text-amber-300 shrink-0 underline"
                          onClick={() => navigator.clipboard.writeText(p)}
                        >
                          copy
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            ) : null}

            <section>
              <h3 className="text-[11px] uppercase tracking-widest text-neutral-500 mb-2">
                Pinned Blocks
              </h3>
              {visiblePinnedBlocks.length === 0 ? (
                <p className="text-xs text-neutral-600">
                  {sessionSearchActive
                    ? "No pinned blocks match the current session search."
                    : "No pinned blocks recorded for this run."}
                </p>
              ) : (
                <ul className="space-y-1">
                  {visiblePinnedBlocks.map((blockId) => (
                    <li
                      key={blockId}
                      className="text-xs text-neutral-300 break-all"
                    >
                      {highlightSearchText(blockId, sessionTerms)}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h3 className="text-[11px] uppercase tracking-widest text-neutral-500 mb-2">
                Recalled Passages
              </h3>
              {visibleRecalledPassages.length === 0 ? (
                <p className="text-xs text-neutral-600">
                  {sessionSearchActive
                    ? "No recalled passages match the current session search."
                    : "No recalled passages captured."}
                </p>
              ) : (
                <ul className="space-y-2">
                  {visibleRecalledPassages.map((passage) => (
                    <li
                      key={`${passage.id}-${passage.source_ref}`}
                      className="text-xs text-neutral-300 break-all"
                    >
                      <div>{highlightSearchText(passage.id, sessionTerms)}</div>
                      {passage.source_ref && (
                        <div className="text-[10px] text-neutral-500 font-mono break-all mb-1">
                          {highlightSearchText(
                            passage.source_ref,
                            sessionTerms
                          )}
                        </div>
                      )}
                      {passage.source_ref ? (
                        <a
                          href={passage.source_ref}
                          target="_blank"
                          rel="noreferrer"
                          className="text-amber-300 hover:text-amber-200 underline"
                        >
                          Source
                        </a>
                      ) : (
                        <span className="text-neutral-600">No source</span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h3 className="text-[11px] uppercase tracking-widest text-neutral-500 mb-2">
                Session Ledger (Timeline)
              </h3>
              {visibleConversations.length === 0 ? (
                <p className="text-xs text-neutral-600 italic">
                  {sessionSearchActive
                    ? "No conversation history matches the current session search."
                    : "No conversation history available."}
                </p>
              ) : (
                <div className="space-y-4">
                  {visibleConversations.map((turn, i) => (
                    <div
                      key={i}
                      className="border border-neutral-800 bg-neutral-900/30 p-3 rounded"
                    >
                      <div className="flex items-start justify-between gap-2 mb-2">
                        <div className="flex items-center gap-2">
                          <span
                            className={`text-[9px] px-1.5 py-0.5 rounded font-bold uppercase tracking-tighter ${
                              turn.kind === "user_message"
                                ? "bg-emerald-950 text-emerald-400"
                                : "bg-violet-950 text-violet-400"
                            }`}
                          >
                            {turn.kind === "user_message" ? "USER" : "AGENT"}
                          </span>
                          {turn.kind !== "user_message" &&
                            turn.kind !== "agent_message" && (
                              <div className="flex items-center gap-1">
                                <span
                                  className={`text-[9px] px-1.5 py-0.5 rounded font-bold uppercase tracking-tighter ${
                                    turn.kind === "thinking"
                                      ? "bg-cyan-950 text-cyan-400"
                                      : "bg-neutral-800 text-neutral-400"
                                  }`}
                                >
                                  {turn.kind.replace("_", " ")}
                                </span>
                                {(turn.kind === "tool_call" ||
                                  turn.kind === "shell_command" ||
                                  turn.kind === "file_edit") && (
                                  <span className="text-[9px] px-1.5 py-0.5 bg-blue-900/40 text-blue-300 rounded font-bold uppercase tracking-widest border border-blue-800/50">
                                    {turn.summary.split("(")[0].split(" ")[0]}
                                  </span>
                                )}
                              </div>
                            )}
                          <span className="text-[10px] text-neutral-500 font-mono">
                            {turn.at
                              ? new Date(turn.at).toLocaleTimeString()
                              : "—"}
                          </span>
                        </div>
                        {turn.cost !== undefined && (
                          <div className="flex gap-2 text-[9px] font-mono">
                            {(turn.tokens?.in || 0) > 0 && (
                              <span className="text-emerald-500/80">
                                In: {turn.tokens?.in}
                              </span>
                            )}
                            {(turn.tokens?.out || 0) > 0 && (
                              <span className="text-violet-500/80">
                                Out: {turn.tokens?.out}
                              </span>
                            )}
                            {(turn.tokens?.cache_read || 0) > 0 && (
                              <span className="text-red-400/80">
                                CacheR: {turn.tokens?.cache_read}
                              </span>
                            )}
                            {(turn.tokens?.cache_write || 0) > 0 && (
                              <span className="text-orange-400/80">
                                CacheW: {turn.tokens?.cache_write}
                              </span>
                            )}
                            {(turn.cost || 0) > 0 && (
                              <span className="text-emerald-300 font-bold ml-1 border-l border-neutral-700 pl-2">
                                ${turn.cost.toFixed(4)}
                              </span>
                            )}
                          </div>
                        )}
                      </div>

                      <div className="text-xs text-neutral-200 font-medium mb-1">
                        {highlightSearchText(turn.summary, sessionTerms)}
                      </div>

                      <div className="text-[11px] text-neutral-400 mb-2 font-mono whitespace-pre-wrap">
                        {highlightSearchText(turn.content, sessionTerms)}
                      </div>

                      <div className="flex gap-2">
                        <button
                          className="text-[9px] text-amber-300/60 hover:text-amber-300 underline"
                          onClick={() => {
                            console.log("Raw Event:", turn.raw);
                            alert(
                              "Raw event logged to console for verification.\n\n" +
                                JSON.stringify(turn.raw, null, 2).slice(
                                  0,
                                  1000
                                ) +
                                "..."
                            );
                          }}
                        >
                          View raw event
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section>
              <h3 className="text-[11px] uppercase tracking-widest text-neutral-500 mb-2">
                Summary Metrics
              </h3>
              <dl className="grid grid-cols-2 gap-3 text-xs">
                <div>
                  <dt className="text-neutral-500">Summarized events</dt>
                  <dd className="text-neutral-200 font-mono">
                    {data.summarized_events_count}
                  </dd>
                </div>
                <div>
                  <dt className="text-neutral-500">tokens_pre</dt>
                  <dd className="text-neutral-200 font-mono">
                    {data.tokens_pre ?? "n/a"}
                  </dd>
                </div>
                <div>
                  <dt className="text-neutral-500">tokens_post</dt>
                  <dd className="text-neutral-200 font-mono">
                    {data.tokens_post ?? "n/a"}
                  </dd>
                </div>
              </dl>
            </section>
          </div>
        )}
      </aside>
    </>
  );
}
