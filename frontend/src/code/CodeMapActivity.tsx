import { Activity } from "lucide-react";
import type { CodeMapActivityEvent, CodeMapActivityKind } from "../api";

const ACTIVITY_TONE: Record<CodeMapActivityKind, { dot: string; text: string; label: string }> = {
  search: { dot: "bg-violet-300", text: "text-violet-200", label: "Search" },
  read: { dot: "bg-cyan-300", text: "text-cyan-200", label: "Read" },
  edit: { dot: "bg-amber-300", text: "text-amber-200", label: "Edit" },
  verify: { dot: "bg-emerald-300", text: "text-emerald-200", label: "Verify" },
};

function relativeTime(value: string): string {
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "now";
  const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (seconds < 10) return "now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

export default function CodeMapActivity({
  events,
  liveEnabled,
  onOpen,
}: {
  events: CodeMapActivityEvent[];
  liveEnabled: boolean;
  onOpen(event: CodeMapActivityEvent): void;
}) {
  return (
    <>
      <div className="border-b border-neutral-800 p-3">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-200">
          <Activity size={13} className="text-emerald-300" /> Live run
        </div>
        <p className="mt-2 text-[11px] leading-5 text-neutral-400">
          Run-ledger events only. No source, diffs, stdout, or stderr.
        </p>
      </div>
      <div className="max-h-72 overflow-y-auto p-2">
        {events.length ? (
          <ol className="space-y-1" aria-label="Recent run activity">
            {[...events].reverse().map((event) => {
              const tone = ACTIVITY_TONE[event.kind];
              const actionable = !!event.path || !!event.symbol_ids?.length;
              return (
                <li key={event.id}>
                  <button
                    type="button"
                    disabled={!actionable}
                    onClick={() => onOpen(event)}
                    className="w-full border border-neutral-800 bg-neutral-900/35 p-2 text-left transition enabled:hover:border-neutral-700 enabled:hover:bg-neutral-900/70 disabled:cursor-default"
                  >
                    <div className="flex items-center gap-2">
                      <span className={`h-2 w-2 rounded-full ${tone.dot}`} />
                      <span className={`text-[10px] font-semibold uppercase tracking-wider ${tone.text}`}>
                        {tone.label}
                      </span>
                      <time className="ml-auto text-[10px] text-neutral-400" dateTime={event.at}>
                        {relativeTime(event.at)}
                      </time>
                    </div>
                    <p className="mt-1.5 break-words text-[11px] leading-4 text-neutral-300">{event.label}</p>
                    {event.path && (
                      <p className="mt-1 truncate text-[9px] text-neutral-600">
                        {event.path}{event.line ? `:${event.line}` : ""}
                      </p>
                    )}
                  </button>
                </li>
              );
            })}
          </ol>
        ) : (
          <p className="p-2 text-xs leading-5 text-neutral-400">
            {liveEnabled ? "Waiting for local activity." : "Live follow is paused."}
          </p>
        )}
      </div>
    </>
  );
}
