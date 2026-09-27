import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  CircleAlert,
  CircleDashed,
  CircleDot,
  FilePenLine,
  GitBranch,
  Handshake,
  ListChecks,
  MessageSquareText,
  Play,
  Undo2,
  Workflow,
  X,
  XCircle,
} from "lucide-react";

import "./reviewUi.css";
import { trapModalTab } from "./focusTrap";
import { ROLE_LABEL, hostLabel, shortTime, statusTone } from "./developmentLabels";
import { fetchReviewDevelopmentEvents } from "./reviewApi";
import type {
  DevelopmentContributor,
  DevelopmentEvent,
  DevelopmentProjection,
  DevelopmentTimelineScope,
  DevelopmentTimelineView,
  ReviewDevelopment,
} from "./types";

export { hostLabel };

interface ReviewDevelopmentSheetProps {
  reviewId: string;
  development: ReviewDevelopment;
  historical: boolean;
  onClose(): void;
  onOpenPath?(path: string): void;
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

function joinNames(contributors: DevelopmentContributor[]): string {
  const names = [...new Set(contributors.map((item) => hostLabel(item.host)))];
  if (names.length <= 2) return names.join(" + ");
  return `${names.slice(0, 2).join(", ")} + ${names.length - 2} more`;
}

const ROLE_TONE: Record<string, string> = {
  author: "text-neutral-200",
  verifier: "text-emerald-300",
  investigator: "text-violet-300",
};

function eventIcon(event: DevelopmentEvent) {
  switch (event.kind) {
    case "edit":
      return <FilePenLine size={11} />;
    case "revert":
      return <Undo2 size={11} />;
    case "test":
    case "validation":
    case "command":
      return event.status === "pass" ? <CheckCircle2 size={11} /> : event.status === "fail" ? <XCircle size={11} /> : <CircleDashed size={11} />;
    case "decision":
    case "fact":
    case "plan":
      return <GitBranch size={11} />;
    case "question":
    case "blocker":
      return <CircleAlert size={11} />;
    case "handoff":
      return <Handshake size={11} />;
    case "task_started":
    case "task_completed":
      return <Play size={10} />;
    case "intent":
    case "message":
      return <MessageSquareText size={11} />;
    default:
      return <CircleDot size={10} />;
  }
}

type TimelineRow =
  | { type: "event"; event: DevelopmentEvent }
  | { type: "updates"; key: string; events: DevelopmentEvent[] };

/** Collapse runs of progress messages from one session into a single row. */
export function groupTimeline(events: DevelopmentEvent[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  for (const event of events) {
    const last = rows[rows.length - 1];
    if (event.kind === "message") {
      if (last?.type === "updates" && last.events[0].session_id === event.session_id && last.events[0].host === event.host) {
        last.events.push(event);
        continue;
      }
      rows.push({ type: "updates", key: `updates-${event.id}`, events: [event] });
      continue;
    }
    rows.push({ type: "event", event });
  }
  return rows.map((row) => (row.type === "updates" && row.events.length === 1 ? { type: "event", event: row.events[0] } : row));
}

function SectionHeading({ children, meta }: { children: React.ReactNode; meta?: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex items-baseline gap-2">
      <h3 className="text-[12px] font-semibold text-neutral-200">{children}</h3>
      {meta && <span className="text-[11px] tabular-nums text-neutral-500">{meta}</span>}
    </div>
  );
}

function PathButton({ path, onOpenPath, onClose, tone = "text-neutral-400" }: {
  path: string;
  onOpenPath?(path: string): void;
  onClose(): void;
  tone?: string;
}) {
  return (
    <button
      type="button"
      disabled={!onOpenPath}
      onClick={() => {
        onClose();
        onOpenPath?.(path);
      }}
      className={`max-w-full truncate rounded px-1 py-0.5 text-left font-mono text-[11px] ${tone} hover:bg-neutral-900 hover:text-neutral-100 disabled:cursor-default disabled:hover:bg-transparent`}
      title={path}
    >
      {path}
    </button>
  );
}

function ContributorRow({ item, onOpenPath, onClose }: {
  item: DevelopmentContributor;
  onOpenPath?(path: string): void;
  onClose(): void;
}) {
  const checks = item.checks.pass + item.checks.fail + item.checks.pending;
  return (
    <li className="grid grid-cols-[minmax(0,9rem)_5.5rem_minmax(0,1fr)] items-start gap-x-3 gap-y-1 border-b border-neutral-800/60 py-2 last:border-b-0 max-sm:grid-cols-[minmax(0,1fr)_auto]">
      <div className="min-w-0">
        <div className="flex items-center gap-1.5 text-[12px] font-medium text-neutral-100">
          {item.active && <span className="review-live-dot" data-active="true" aria-label="working now" />}
          <span className="truncate" title={`${item.host}:${item.session_id}`}>{hostLabel(item.host)}</span>
        </div>
        <div className="truncate text-[10px] text-neutral-500" title={item.model || item.session_id}>
          {item.model || item.session_id}
        </div>
      </div>
      <div className={`text-[11px] ${ROLE_TONE[item.role] ?? "text-neutral-400"}`}>
        {ROLE_LABEL[item.role] ?? item.role}
        {item.evidence === "attribution" && <div className="text-[10px] text-neutral-600">edit record</div>}
      </div>
      <div className="min-w-0 max-sm:col-span-2">
        {item.paths.length > 0 ? (
          <div className="flex min-w-0 flex-wrap items-center gap-x-1">
            {item.paths.slice(0, 3).map((path) => (
              <PathButton key={path} path={path} onOpenPath={onOpenPath} onClose={onClose} />
            ))}
            {item.path_count > 3 && <span className="text-[10px] text-neutral-500">+{item.path_count - 3}</span>}
          </div>
        ) : checks > 0 ? (
          <span className="text-[11px] text-neutral-400">
            {item.checks.fail > 0
              ? `${plural(item.checks.fail, "check")} failed`
              : `${plural(item.checks.pass, "check")} passed`}
            {item.checks.pending > 0 && ` · ${item.checks.pending} pending`}
          </span>
        ) : (
          <span className="text-[11px] text-neutral-500">{item.task || "Accepted findings"}</span>
        )}
      </div>
    </li>
  );
}

function Summary({ projection, onOpenPath, onClose }: {
  projection: DevelopmentProjection;
  onOpenPath?(path: string): void;
  onClose(): void;
}) {
  const { counts, verification } = projection;
  const narrative = projection.narrative ?? [];
  const facts = [
    counts.authors > 0 && plural(counts.authors, "contributor"),
    counts.verifiers > 0 && plural(counts.verifiers, "verifier"),
    counts.files > 0 && plural(counts.files, "file"),
    verification.pass + verification.fail + verification.pending > 0
      && `${verification.pass} passed · ${verification.fail} failed${verification.pending ? ` · ${verification.pending} pending` : ""}`,
  ].filter(Boolean) as string[];
  const failed = verification.checks.filter((check) => check.status === "fail");
  const attention = failed.length + projection.open_items.length + (projection.unattributed_paths.length > 0 ? 1 : 0);
  return (
    <>
      <section className="border-b border-neutral-800/70 px-5 py-4" aria-label="Development summary">
        {projection.headline ? (
          <p className="max-w-[68ch] text-[13px] leading-6 text-neutral-100">{projection.headline}</p>
        ) : (
          <p className="text-[12px] text-neutral-500">No summary was recorded.</p>
        )}
        {projection.intent && projection.intent !== projection.headline && (
          <p className="mt-2 max-w-[68ch] text-[12px] leading-5 text-neutral-400">
            <span className="mr-1.5 font-medium text-neutral-300">Asked</span>
            {projection.intent}
          </p>
        )}
        {narrative.length > 0 ? (
          <p className="mt-3 max-w-[68ch] text-[12px] leading-5 text-neutral-400" data-testid="development-narrative">
            {narrative.join(" ")}
          </p>
        ) : facts.length > 0 && (
          <p className="mt-3 text-[11px] tabular-nums text-neutral-500">{facts.join(" · ")}</p>
        )}
        {projection.source === "legacy_journal" && (
          <p className="mt-2 text-[10px] text-neutral-600">From the author journal frozen with this Snapshot.</p>
        )}
      </section>

      {attention > 0 && (
        <section className="border-b border-neutral-800/70 px-5 py-4">
          <SectionHeading meta={attention}>Reviewer attention</SectionHeading>
          <ul className="space-y-1.5">
            {failed.map((check) => (
              <li key={`fail-${check.sequence}`} className="flex gap-2 text-[12px] leading-5">
                <XCircle size={13} className="mt-1 shrink-0 text-rose-300" aria-hidden="true" />
                <div className="min-w-0">
                  <span className="text-rose-200">{check.summary}</span>
                  <span className="ml-2 text-[10px] text-neutral-500">{hostLabel(check.host)} · failed</span>
                  {check.detail && <div className="truncate text-[11px] text-neutral-500" title={check.detail}>{check.detail}</div>}
                </div>
              </li>
            ))}
            {projection.open_items.map((item) => (
              <li key={item.id} className="flex gap-2 text-[12px] leading-5">
                <CircleAlert size={13} className="mt-1 shrink-0 text-amber-300" aria-hidden="true" />
                <div className="min-w-0 text-neutral-300">
                  {item.summary}
                  <span className="ml-2 text-[10px] text-neutral-500">{hostLabel(item.host)} · {item.kind === "blocker" ? "blocker" : "open question"}</span>
                </div>
              </li>
            ))}
            {projection.unattributed_paths.length > 0 && (
              <li className="flex gap-2 text-[12px] leading-5">
                <CircleDashed size={13} className="mt-1 shrink-0 text-neutral-400" aria-hidden="true" />
                <div className="min-w-0 text-neutral-300">
                  {plural(projection.unattributed_paths.length, "changed file")} with no recorded author
                  <div className="mt-0.5 flex flex-wrap gap-x-1">
                    {projection.unattributed_paths.slice(0, 6).map((path) => (
                      <PathButton key={path} path={path} onOpenPath={onOpenPath} onClose={onClose} tone="text-neutral-500" />
                    ))}
                  </div>
                </div>
              </li>
            )}
          </ul>
        </section>
      )}
    </>
  );
}

function Contributors({ projection, onOpenPath, onClose }: {
  projection: DevelopmentProjection;
  onOpenPath?(path: string): void;
  onClose(): void;
}) {
  if (projection.contributors.length === 0) return null;
  const { counts } = projection;
  const meta = [
    counts.authors && plural(counts.authors, "author"),
    counts.verifiers && plural(counts.verifiers, "verifier"),
    counts.investigators && `${counts.investigators} supporting`,
  ].filter(Boolean).join(" · ");
  return (
    <section className="border-b border-neutral-800/70 px-5 py-4">
      <SectionHeading meta={meta}>Contributors</SectionHeading>
      <ul>
        {projection.contributors.map((item) => (
          <ContributorRow key={item.key} item={item} onOpenPath={onOpenPath} onClose={onClose} />
        ))}
      </ul>
    </section>
  );
}

function Verification({ projection }: { projection: DevelopmentProjection }) {
  const [expanded, setExpanded] = useState(false);
  const { verification } = projection;
  const total = verification.pass + verification.fail + verification.pending;
  if (total === 0) return null;
  const visible = expanded ? verification.checks : verification.checks.slice(0, 5);
  return (
    <section className="border-b border-neutral-800/70 px-5 py-4">
      <SectionHeading
        meta={(
          <>
            <span className="text-emerald-300/90">{verification.pass} passed</span>
            {" · "}
            <span className={verification.fail ? "text-rose-300" : ""}>{verification.fail} failed</span>
            {verification.pending > 0 && <> · <span className="text-amber-300">{verification.pending} pending</span></>}
          </>
        )}
      >
        Verification
      </SectionHeading>
      <ul className="space-y-1">
        {visible.map((check) => (
          <li key={`${check.sequence}-${check.name}`} className="flex items-center gap-2 text-[11px]">
            <span className={statusTone(check.status)} aria-label={check.status}>
              {check.status === "pass" ? <CheckCircle2 size={12} /> : check.status === "fail" ? <XCircle size={12} /> : <CircleDashed size={12} />}
            </span>
            <span className="min-w-0 flex-1 truncate font-mono text-neutral-300" title={check.summary}>{check.summary}</span>
            <span className="shrink-0 text-[10px] text-neutral-500">{hostLabel(check.host)}</span>
          </li>
        ))}
      </ul>
      {verification.checks.length > 5 && (
        <button type="button" onClick={() => setExpanded((value) => !value)} className="mt-2 text-[11px] text-neutral-500 hover:text-neutral-200">
          {expanded ? "Show fewer" : `Show all ${verification.checks.length}`}
        </button>
      )}
    </section>
  );
}

function Decisions({ projection }: { projection: DevelopmentProjection }) {
  if (projection.decisions.length === 0 && !projection.plan) return null;
  return (
    <section className="border-b border-neutral-800/70 px-5 py-4">
      <SectionHeading meta={projection.decisions.length || undefined}>Decisions & facts</SectionHeading>
      <ul className="space-y-2">
        {projection.decisions.slice(0, 8).map((item) => (
          <li key={item.id} className="text-[12px] leading-5 text-neutral-300">
            {item.summary}
            <span className="ml-2 text-[10px] text-neutral-500">
              {hostLabel(item.host)}{item.role === "investigator" ? " · accepted finding" : ""}
            </span>
          </li>
        ))}
      </ul>
      {projection.plan && (
        <details className="mt-3 text-[11px] text-neutral-500">
          <summary className="w-fit cursor-pointer hover:text-neutral-200">Latest plan · {hostLabel(projection.plan.host)}</summary>
          <div className="mt-1.5 whitespace-pre-wrap leading-5 text-neutral-400">{projection.plan.detail}</div>
        </details>
      )}
    </section>
  );
}

function Feedback({ projection, revisionNumber }: { projection: DevelopmentProjection; revisionNumber: number }) {
  if (projection.feedback.length === 0) return null;
  return (
    <section className="border-b border-neutral-800/70 px-5 py-4">
      <SectionHeading>Review feedback</SectionHeading>
      <ul className="space-y-2">
        {projection.feedback.map((item) => {
          const who = item.sessions.map((key) => hostLabel(key.split(":")[0] ?? "")).filter(Boolean)[0] ?? "The agent";
          const parts = [
            item.edit_count > 0 ? plural(item.paths.length || item.edit_count, "file") + " changed" : "no edits yet",
            item.checks.pass + item.checks.fail > 0 && `${item.checks.pass} checks passed${item.checks.fail ? ` · ${item.checks.fail} failed` : ""}`,
            item.included_in_revision !== null && item.included_in_revision !== undefined
              ? `included in Snapshot ${item.included_in_revision}`
              : item.edit_count > 0 ? `after Snapshot ${revisionNumber}` : "",
          ].filter(Boolean);
          return (
            <li key={item.causation_id} className="flex gap-2 text-[12px] leading-5">
              <Handshake size={13} className="mt-1 shrink-0 text-sky-300" aria-hidden="true" />
              <div className="min-w-0">
                <div className="text-neutral-200">
                  {who} {item.edit_count > 0 ? "addressed" : "received"} {plural(item.annotation_ids.length || 1, "comment")}
                </div>
                <div className="text-[11px] text-neutral-500">{parts.join(" · ")}</div>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function ChangedPaths({ projection, onOpenPath, onClose }: {
  projection: DevelopmentProjection;
  onOpenPath?(path: string): void;
  onClose(): void;
}) {
  const [expanded, setExpanded] = useState(false);
  if (projection.changed_paths.length === 0) return null;
  const rows = expanded ? projection.changed_paths : projection.changed_paths.slice(0, 12);
  return (
    <section className="border-b border-neutral-800/70 px-5 py-4">
      <SectionHeading meta={projection.changed_paths.length}>Changed during development</SectionHeading>
      <ul className="grid gap-x-3 gap-y-0.5 sm:grid-cols-2">
        {rows.map((row) => (
          <li key={row.path} className="flex min-w-0 items-center gap-1.5">
            <PathButton path={row.path} onOpenPath={onOpenPath} onClose={onClose} tone={row.status === "reverted" ? "text-neutral-500 line-through" : "text-neutral-400"} />
            {row.in_snapshot === false && <span className="shrink-0 text-[10px] text-neutral-500">not in Snapshot</span>}
            {row.sessions.length > 1 && <span className="shrink-0 text-[10px] text-neutral-500">{row.sessions.length} agents</span>}
          </li>
        ))}
      </ul>
      {projection.changed_paths.length > 12 && (
        <button type="button" onClick={() => setExpanded((value) => !value)} className="mt-2 text-[11px] text-neutral-500 hover:text-neutral-200">
          {expanded ? "Show fewer" : `Show all ${projection.changed_paths.length}`}
        </button>
      )}
    </section>
  );
}

const VIEWS: Array<[DevelopmentTimelineView, string]> = [
  ["all", "All"],
  ["authors", "Authors"],
  ["verification", "Verification"],
  ["decisions", "Decisions"],
  ["changes", "Changes"],
];

function TimelineEvent({ event, onOpenPath, onClose }: { event: DevelopmentEvent; onOpenPath?(path: string): void; onClose(): void }) {
  return (
    <li className="relative border-l border-neutral-800 py-2 pl-5">
      <span className={`absolute -left-[10px] top-[9px] inline-flex h-[19px] w-[19px] items-center justify-center rounded-full border border-neutral-800 bg-neutral-950 ${statusTone(event.status)}`} aria-hidden="true">
        {eventIcon(event)}
      </span>
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="min-w-0 flex-1 text-[12px] leading-5 text-neutral-200">{event.summary}</span>
        <time className="shrink-0 text-[10px] tabular-nums text-neutral-600" dateTime={event.at}>{shortTime(event.at)}</time>
      </div>
      <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 text-[10px] text-neutral-500">
        <span>{hostLabel(event.host)}{event.role && event.role !== "author" ? ` · ${ROLE_LABEL[event.role] ?? event.role}` : ""}</span>
        <span>{event.kind.replace("_", " ")}</span>
        {event.status && event.status !== "changed" && <span className={statusTone(event.status)}>{event.status}</span>}
        {event.causation_id && <span className="text-sky-300/80">review follow-up</span>}
        {event.path && event.kind !== "edit" && event.kind !== "revert" && (
          <PathButton path={event.path} onOpenPath={onOpenPath} onClose={onClose} tone="text-sky-300/70" />
        )}
      </div>
      {event.detail && event.detail !== event.summary && (
        <details className="mt-1 text-[11px] text-neutral-500">
          <summary className="w-fit cursor-pointer hover:text-neutral-300">Details</summary>
          <div className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap break-words leading-5">{event.detail}</div>
        </details>
      )}
    </li>
  );
}

function Timeline({ reviewId, revisionNumber, hasSince, initialScope, onOpenPath, onClose, cursor }: {
  reviewId: string;
  revisionNumber: number;
  hasSince: boolean;
  initialScope: DevelopmentTimelineScope;
  cursor: string;
  onOpenPath?(path: string): void;
  onClose(): void;
}) {
  const [view, setView] = useState<DevelopmentTimelineView>("all");
  const [scope, setScope] = useState<DevelopmentTimelineScope>(initialScope);
  const [events, setEvents] = useState<DevelopmentEvent[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState("");
  const request = useRef(0);

  useEffect(() => setScope(initialScope), [initialScope]);

  const load = useCallback(async (before: number | null) => {
    const token = ++request.current;
    if (before === null) setState("loading");
    try {
      const page = await fetchReviewDevelopmentEvents(reviewId, { revisionNumber, scope, view, before, limit: 40 });
      if (token !== request.current) return;
      setEvents((current) => (before === null ? page.events : [...current, ...page.events]));
      setNextBefore(page.next_before);
      setState("ready");
      setError("");
    } catch (reason) {
      if (token !== request.current) return;
      setState("failed");
      setError(String(reason instanceof Error ? reason.message : reason));
    }
  }, [reviewId, revisionNumber, scope, view]);

  useEffect(() => {
    void load(null);
  }, [load, cursor]);

  const rows = useMemo(() => groupTimeline(events), [events]);

  return (
    <section className="px-5 py-4" aria-label="Development timeline">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h3 className="mr-auto text-[12px] font-semibold text-neutral-200">Timeline</h3>
        {hasSince && (
          <div className="review-segment" role="group" aria-label="Timeline scope">
            {([["snapshot", `Snapshot ${revisionNumber}`], ["since", "Since"]] as Array<[DevelopmentTimelineScope, string]>).map(([value, label]) => (
              <button
                key={value}
                type="button"
                aria-pressed={scope === value}
                onClick={() => setScope(value)}
                className={`review-segment-button px-2 py-1 text-[11px] ${scope === value ? "review-segment-button-active" : ""}`}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="review-segment mb-3 flex w-full overflow-x-auto" role="tablist" aria-label="Timeline filter">
        {VIEWS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={view === value}
            onClick={() => setView(value)}
            className={`review-segment-button flex-1 whitespace-nowrap px-2 py-1 text-[11px] ${view === value ? "review-segment-button-active" : ""}`}
          >
            {label}
          </button>
        ))}
      </div>
      {state === "loading" && events.length === 0 ? (
        <div className="py-6 text-center text-[11px] text-neutral-500" role="status">Loading development activity…</div>
      ) : state === "failed" ? (
        <div className="rounded-md border border-rose-900/50 bg-rose-950/15 px-3 py-2 text-[11px] text-rose-200" role="alert">
          Timeline unavailable: {error}
          <button type="button" onClick={() => void load(null)} className="ml-2 underline underline-offset-2">Retry</button>
        </div>
      ) : rows.length === 0 ? (
        <div className="py-6 text-center text-[11px] text-neutral-500">No development activity in this view.</div>
      ) : (
        <ol className="ml-2.5">
          {rows.map((row) => row.type === "event" ? (
            <TimelineEvent key={row.event.id} event={row.event} onOpenPath={onOpenPath} onClose={onClose} />
          ) : (
            <li key={row.key} className="relative border-l border-neutral-800 py-2 pl-5">
              <span className="absolute -left-[10px] top-[9px] inline-flex h-[19px] w-[19px] items-center justify-center rounded-full border border-neutral-800 bg-neutral-950 text-neutral-500" aria-hidden="true">
                <MessageSquareText size={11} />
              </span>
              <details className="text-[12px]">
                <summary className="cursor-pointer text-neutral-400 hover:text-neutral-200">
                  {plural(row.events.length, "progress update")} from {hostLabel(row.events[0].host)}
                  <span className="ml-2 text-[10px] tabular-nums text-neutral-600">{shortTime(row.events[row.events.length - 1].at)}–{shortTime(row.events[0].at)}</span>
                </summary>
                <ul className="mt-1.5 space-y-1.5">
                  {row.events.map((event) => (
                    <li key={event.id} className="text-[11px] leading-5 text-neutral-400">{event.summary}</li>
                  ))}
                </ul>
              </details>
            </li>
          ))}
        </ol>
      )}
      {nextBefore !== null && state !== "failed" && (
        <button type="button" onClick={() => void load(nextBefore)} className="review-compact-action mt-2 h-7 text-[11px]">
          Load earlier activity
        </button>
      )}
    </section>
  );
}

export default function ReviewDevelopmentSheet({
  reviewId,
  development,
  historical,
  onClose,
  onOpenPath,
}: ReviewDevelopmentSheetProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const { snapshot, since } = development;
  const newer = since && since.event_count > 0 ? since : null;
  const [timelineScope, setTimelineScope] = useState<DevelopmentTimelineScope>("snapshot");

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const empty = !snapshot.available && !newer;
  const newerPaths = newer?.newer_paths?.length ?? 0;

  return (
    <div
      className="absolute inset-0 z-[88] flex justify-end bg-black/45"
      role="dialog"
      aria-modal="true"
      aria-labelledby="review-development-title"
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section className="review-sheet flex h-full w-[min(640px,100%)] flex-col border-y-0 border-r-0">
        <header className="flex items-start gap-3 border-b border-neutral-800/80 px-5 py-3.5">
          <Workflow size={15} className="mt-0.5 shrink-0 text-neutral-400" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <h2 id="review-development-title" className="text-[14px] font-semibold text-neutral-100">Development</h2>
            <p className="mt-0.5 text-[11px] text-neutral-500">
              Snapshot {development.revision_number}
              {" · "}
              {historical ? "frozen historical record" : "frozen with this Snapshot"}
              {snapshot.event_count > 0 && ` · ${plural(snapshot.event_count, "event")}`}
            </p>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close development" className="review-icon-button">
            <X size={14} />
          </button>
        </header>

        {newer && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-sky-900/50 bg-sky-950/20 px-5 py-2.5 text-[12px] text-sky-200" role="status">
            <span className="review-live-dot" data-active={newer.active ? "true" : "false"} aria-hidden="true" />
            <span className="font-semibold">{newer.active ? "Working now" : "Since this Snapshot"}</span>
            <span className="min-w-0 flex-1 truncate text-sky-300/80" title={newer.narrative?.join(" ") || undefined}>
              {[
                newer.contributors.length > 0 && joinNames(newer.contributors),
                newerPaths > 0 && `${plural(newerPaths, "newer file")}`,
                newer.verification.pass + newer.verification.fail > 0 && `${newer.verification.pass} passed${newer.verification.fail ? ` · ${newer.verification.fail} failed` : ""}`,
                newer.feedback.some((item) => item.edit_count > 0) && "addressing review feedback",
                !newer.contributors.length && plural(newer.event_count, "event"),
              ].filter(Boolean).join(" · ")}
            </span>
            <button
              type="button"
              onClick={() => setTimelineScope((value) => (value === "since" ? "snapshot" : "since"))}
              className="review-context-action border-sky-800/60 text-sky-200 hover:border-sky-600"
            >
              {timelineScope === "since" ? "Show Snapshot activity" : "Show newer activity"}
            </button>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {empty ? (
            <div className="px-5 py-10 text-center">
              <ListChecks size={18} className="mx-auto text-neutral-600" aria-hidden="true" />
              <p className="mx-auto mt-3 max-w-[46ch] text-[12px] leading-5 text-neutral-400">
                {development.reason || "No development evidence is attached to this Snapshot."}
              </p>
              <p className="mx-auto mt-1.5 max-w-[46ch] text-[11px] leading-5 text-neutral-600">
                Development appears when an agent edits this Change, runs checks for it, or is bound to it.
              </p>
            </div>
          ) : (
            <>
              <Summary projection={snapshot} onOpenPath={onOpenPath} onClose={onClose} />
              <Contributors projection={snapshot} onOpenPath={onOpenPath} onClose={onClose} />
              <Verification projection={snapshot} />
              <Decisions projection={snapshot} />
              <Feedback projection={snapshot} revisionNumber={development.revision_number} />
              <ChangedPaths projection={snapshot} onOpenPath={onOpenPath} onClose={onClose} />
              <Timeline
                reviewId={reviewId}
                revisionNumber={development.revision_number}
                hasSince={Boolean(newer)}
                initialScope={timelineScope}
                cursor={development.cursor}
                onOpenPath={onOpenPath}
                onClose={onClose}
              />
            </>
          )}
        </div>

        <footer className="border-t border-neutral-800/70 px-5 py-2 text-[10px] leading-4 text-neutral-600">
          Observable development evidence only: no hidden reasoning, model prompts, or raw tool output. Development never creates Review comments.
        </footer>
      </section>
    </div>
  );
}
