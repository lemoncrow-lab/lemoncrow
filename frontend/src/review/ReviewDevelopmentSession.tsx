import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { api, type ConversationEntry } from "../api";
import { Button } from "../components/ui/button";
import { ConversationTurn } from "../pages/sessions/TurnRenderers";
import { groupTurns, parseInspectorData } from "../pages/sessions/helpers";
import { hostLabel } from "./developmentLabels";

export interface DevelopmentSessionSelection {
  host: string;
  sessionId: string;
  context?: string;
}

export const DevelopmentSessionContext = createContext<
  ((session: DevelopmentSessionSelection, trigger: HTMLButtonElement) => void) | null
>(null);

export function OpenDevelopmentSession({ host, sessionId, context }: DevelopmentSessionSelection) {
  const open = useContext(DevelopmentSessionContext);
  if (!open || !sessionId.trim() || ["ci", "human"].includes(host.trim().toLowerCase())) return null;
  return (
    <Button
      unstyled
      type="button"
      onClick={(event) => open({ host, sessionId, context }, event.currentTarget)}
      aria-label={`Open ${hostLabel(host)} session ${sessionId}${context ? ` for ${context}` : ""}`}
      className="inline-flex shrink-0 rounded px-2 py-1 text-label font-medium text-sky-300 hover:bg-sky-950/40 hover:text-sky-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400"
    >
      Open session
    </Button>
  );
}

export default function ReviewDevelopmentSession({ session, onBack }: {
  session: DevelopmentSessionSelection;
  onBack(): void;
}) {
  const backRef = useRef<HTMLButtonElement>(null);
  const [entries, setEntries] = useState<ConversationEntry[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => { backRef.current?.focus(); }, []);

  useEffect(() => {
    let active = true;
    setEntries(null);
    setFailed(false);
    api.ledger(encodeURIComponent(session.sessionId))
      .then((ledger) => {
        if (!active) return;
        if (!ledger || ledger.error) {
          setFailed(true);
          return;
        }
        setEntries(parseInspectorData(session.sessionId, ledger).conversations ?? []);
      })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [session.sessionId, session.host, refresh]);

  const turns = useMemo(() => {
    const seen = new Map<string, number>();
    return groupTurns(entries ?? []).map((turn: ConversationEntry) => {
      const base = turn.tool_use_id || `${turn.kind}:${turn.at}`;
      const occurrence = seen.get(base) ?? 0;
      seen.set(base, occurrence + 1);
      return { turn, key: `${base}#${occurrence}` };
    });
  }, [entries]);

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="Agent session conversation">
      <div className="shrink-0 space-y-2 border-b border-border/70 px-5 py-3">
        <div className="flex items-center justify-between gap-2">
          <Button ref={backRef} unstyled type="button" onClick={onBack} className="review-context-action">
            <ArrowLeft size={13} aria-hidden="true" /> Back to Activity
          </Button>
          <Button
            unstyled type="button" onClick={() => setRefresh((value) => value + 1)}
            disabled={entries === null && !failed}
            className="review-context-action"
          >
            <RefreshCw size={12} aria-hidden="true" /> Refresh session
          </Button>
        </div>
        <h3 className="text-heading font-semibold text-foreground">
          {session.host.toLowerCase() === "claude" ? "Claude Code" : hostLabel(session.host)} session
        </h3>
        <p className="break-all font-mono text-label text-neutral-400">{session.sessionId}</p>
        {session.context && <p className="break-words text-body leading-5 text-neutral-300">Opened from: {session.context}</p>}
      </div>
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto px-4 py-5 sm:px-5">
        {failed ? (
          <div role="alert" className="space-y-3 text-body text-rose-200">
            <p>Could not load this session’s conversation.</p>
            <Button unstyled type="button" onClick={() => setRefresh((value) => value + 1)} className="review-context-action">Retry</Button>
          </div>
        ) : entries === null ? (
          <p role="status" className="text-body text-neutral-400">Loading conversation…</p>
        ) : turns.length === 0 ? (
          <p className="text-body leading-5 text-neutral-400">No conversation has been recorded for this session yet.</p>
        ) : (
          <div className="min-w-0 space-y-4">
            {turns.map(({ key, turn }) => <ConversationTurn key={key} turn={turn} forceExpand={false} compact />)}
          </div>
        )}
      </div>
    </section>
  );
}
