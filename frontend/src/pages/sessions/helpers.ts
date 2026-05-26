import { type Trace, type RunInspectorData } from "../../api";

// --- Constants ---
export const LONG_OUTPUT_THRESHOLD = 400; // chars

// --- Formatters ---

export function fmtUsd(v: number): string {
  return `$${v.toFixed(3)}`;
}

export function fmtTok(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}

export function parseAt(s: string | null | undefined): Date | null {
  if (!s) return null;
  // ms-epoch integers arrive as numeric strings from OpenCode
  const d = /^\d+$/.test(s) ? new Date(parseInt(s, 10)) : new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

export function fmtDate(s: string | null | undefined): string {
  const d = parseAt(s);
  return d ? d.toLocaleString() : "—";
}

export function fmtDuration(secs: number): string {
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

// --- Host detection ---

export const HOST_COLORS: Record<string, string> = {
  atelier: "bg-purple-900/40 text-purple-300 border-purple-700/50",
  claude: "bg-violet-900/40 text-violet-300 border-violet-700/50",
  gemini: "bg-blue-900/40 text-blue-300 border-blue-700/50",
  copilot: "bg-sky-900/40 text-sky-300 border-sky-700/50",
  codex: "bg-teal-900/40 text-teal-300 border-teal-700/50",
  opencode: "bg-indigo-900/40 text-indigo-300 border-indigo-700/50",
};

export function extractHost(trace: Trace | string): string {
  const agent = typeof trace === "string" ? trace : trace.agent;
  const host = typeof trace === "string" ? null : trace.host;
  const id = typeof trace === "string" ? "" : trace.id;
  if (host) return host;
  const a = agent.toLowerCase();
  const i = id.toLowerCase();
  if (i.startsWith("gemini-") || a === "gemini") return "gemini";
  if (i.startsWith("claude-") || a === "claude") return "claude";
  if (i.startsWith("codex-") || a === "codex") return "codex";
  if (i.startsWith("copilot-") || a === "copilot") return "copilot";
  if (i.startsWith("opencode-") || a === "opencode") return "opencode";
  if (a.startsWith("atelier:")) return "atelier";
  return "unknown";
}

// --- Tool name normalization (used by grouping + display) ---

function normalizeCommandName(value: unknown): string {
  if (typeof value === "string" && value.trim()) {
    return value.trim().split(/\s+/)[0].split("/").pop() || "";
  }
  if (typeof value === "object" && value !== null && "command" in value) {
    return normalizeCommandName((value as { command?: unknown }).command);
  }
  return "";
}

export function getNormName(t: any): string {
  if (typeof t?.tool_name === "string" && t.tool_name.trim()) {
    return t.tool_name.trim();
  }
  const rawName = t.raw?.name || t.raw?.payload?.name;
  if (typeof rawName === "string" && rawName.trim()) return rawName.trim();
  const rawCmd = t.raw?.command || t.raw?.payload?.command;
  const normalizedCommand = normalizeCommandName(rawCmd);
  if (normalizedCommand) return normalizedCommand;
  return (
    t.summary
      ?.replace(/^(Called|Used|Ran|Executed|Edited|Modified)\s+/i, "")
      .split("(")[0]
      .split("{")[0]
      .trim()
      .split(/\s+/)[0] || ""
  );
}

// --- Conversation turn grouping ---

export function groupTurns(turns: any[]): any[] {
  const grouped: any[] = [];
  for (const turn of turns) {
    const prev = grouped[grouped.length - 1];
    const isTool =
      turn.kind !== "user_message" &&
      turn.kind !== "agent_message" &&
      turn.kind !== "thinking";
    const toolName = getNormName(turn);

    if (prev && isTool && prev.kind === turn.kind) {
      const prevToolName = getNormName(prev);
      if (prevToolName === toolName) {
        prev.count = (prev.count || 1) + 1;
        prev.cost = (prev.cost || 0) + (turn.cost || 0);
        // Sum per-call Atelier savings across grouped turns so the badge
        // matches the ×N collapse the user sees.
        if (turn.saved) {
          const acc = prev.saved || { tokens: 0, calls: 0, usd: 0 };
          prev.saved = {
            tokens: (acc.tokens || 0) + (turn.saved.tokens || 0),
            calls: (acc.calls || 0) + (turn.saved.calls || 0),
            usd: (acc.usd || 0) + (turn.saved.usd || 0),
          };
        }
        if (turn.content && turn.content !== prev.content) {
          prev.content = (prev.content || "") + "\n\n---\n\n" + turn.content;
        }
        continue;
      }
    }
    grouped.push({ ...turn, count: 1 });
  }
  return grouped;
}

// --- Inspector data parser ---

export function parseInspectorData(
  sessionId: string,
  ledger: any
): RunInspectorData {
  const events: any[] = Array.isArray(ledger?.events) ? ledger.events : [];

  let conversations = Array.isArray(ledger?.conversations)
    ? [...ledger.conversations]
    : [];

  if (conversations.length === 0 && events.length > 0) {
    conversations = events.map((ev) => {
      const payload = ev.payload || {};
      const cost =
        typeof payload.cost_usd === "number"
          ? payload.cost_usd
          : typeof payload.cost === "number"
            ? payload.cost
            : 0;
      return {
        kind: ev.kind,
        at: ev.at || ledger.created_at,
        summary: ev.summary || "",
        content:
          payload.content ||
          payload.stdout ||
          payload.diff ||
          JSON.stringify(payload.args || payload, null, 2),
        tokens: payload.tokens || {
          in: payload.input_tokens || 0,
          out: payload.output_tokens || 0,
        },
        cost,
        raw: ev,
      };
    });
  }

  const defaultModel =
    (typeof ledger?.model === "string" && ledger.model) ||
    conversations.find((turn) => typeof turn?.model === "string" && turn.model)
      ?.model ||
    (typeof ledger?.trace?.model === "string" && ledger.trace.model) ||
    null;
  if (defaultModel) {
    conversations = conversations.map((turn) =>
      turn?.model ? turn : { ...turn, model: defaultModel }
    );
  }

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

  // --- dedupe helpers ---
  function uniqueBy<T>(arr: T[], keyFn: (item: T, idx: number) => string): T[] {
    const map = new Map<string, T>();
    arr.forEach((item, idx) => {
      const k = keyFn(item, idx);
      const key =
        k === undefined || k === null || k === "" ? `__idx_${idx}` : String(k);
      if (!map.has(key)) map.set(key, item);
    });
    return Array.from(map.values());
  }

  const rawSourceFiles = Array.isArray(ledger?.source_files)
    ? ledger.source_files
    : [];
  const source_files = uniqueBy(
    rawSourceFiles,
    (f: any, idx) => f?.artifact_id ?? f?.path ?? `__idx_${idx}`
  );

  const rawArtifacts = Array.isArray(ledger?.artifacts) ? ledger.artifacts : [];
  const artifacts = uniqueBy(
    rawArtifacts,
    (a: any, idx) =>
      (a?.id ??
        a?.relative_path ??
        `${a?.scope ?? ""}:${a?.relative_path ?? ""}`) ||
      `__idx_${idx}`
  );

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
    source_files,
    artifacts,
    conversations,
  };
}
