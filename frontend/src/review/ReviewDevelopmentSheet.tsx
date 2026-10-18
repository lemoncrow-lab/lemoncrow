import { Button as UiButton } from "../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { ToggleGroup } from "../components/ui/toggle-group";
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
import { AgentActionList, AgentApprovalPanel } from "./AgentActions";
import { ROLE_LABEL, hostLabel, shortTime, statusTone } from "./developmentLabels";
import { fetchReviewDevelopmentEvents } from "./reviewApi";
import ReviewDevelopmentSession, { DevelopmentSessionContext, OpenDevelopmentSession, type DevelopmentSessionSelection } from "./ReviewDevelopmentSession";
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
      <h3 className="text-body font-semibold text-neutral-200">{children}</h3>
      {meta && <span className="text-label tabular-nums text-muted-foreground">{meta}</span>}
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
    <UiButton unstyled
      type="button"
      disabled={!onOpenPath}
      onClick={() => {
        onClose();
        onOpenPath?.(path);
      }}
      className={`max-w-full truncate rounded px-1 py-0.5 text-left font-mono text-label ${tone} hover:bg-muted hover:text-foreground disabled:cursor-default disabled:hover:bg-transparent`}
      title={path}
    >
      {path}
    </UiButton>
  );
}

function ContributorRow({ item, recent, onOpenPath, onClose }: {
  item: DevelopmentContributor;
  recent: boolean;
  onOpenPath?(path: string): void;
  onClose(): void;
}) {
  const title = item.task || item.intent || item.latest_message || `${hostLabel(item.host)} session`;
  const detail = item.latest_message && item.latest_message !== title
    ? item.latest_message
    : item.path_count > 0
      ? plural(item.path_count, "file") + " changed"
      : item.checks.fail > 0
        ? plural(item.checks.fail, "check") + " failed"
        : item.checks.pass > 0
          ? plural(item.checks.pass, "check") + " passed"
          : "";
  return (
    <li className="border-b border-border/60 py-3 last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-heading font-medium text-foreground">
            {item.active && <span className="review-live-dot shrink-0" data-active="true" aria-label="working now" />}
            <span className="break-words">{title}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-label text-neutral-400">
            <span>Driver: {item.host.toLowerCase() === "claude" ? "Claude Code" : hostLabel(item.host)}</span>
            {item.model && <span>Model: {item.model}</span>}
            <span className="text-muted-foreground">{ROLE_LABEL[item.role] ?? item.role}</span>
            <span className="text-muted-foreground">{item.active ? "Working now" : recent ? "Updated since Snapshot" : item.status === "done" ? "Completed" : "Recorded"}</span>
          </div>
          {detail && <p className="mt-1.5 line-clamp-2 text-label leading-5 text-neutral-400">{detail}</p>}
        </div>
        <OpenDevelopmentSession host={item.host} sessionId={item.session_id} />
      </div>
      {item.paths.length > 0 && (
        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-1">
          {item.paths.slice(0, 2).map((path) => (
            <PathButton key={path} path={path} onOpenPath={onOpenPath} onClose={onClose} />
          ))}
          {item.path_count > 2 && <span className="text-caption text-muted-foreground">+{item.path_count - 2} files</span>}
        </div>
      )}
    </li>
  );
}

function Summary({ projection, onOpenPath, onClose }: {
  projection: DevelopmentProjection;
  onOpenPath?(path: string): void;
  onClose(): void;
}) {
  const { counts, verification } = projection;
  const facts = [
    counts.files > 0 && plural(counts.files, "file"),
    verification.pass > 0 && `${verification.pass} checks passed`,
    verification.fail > 0 && `${verification.fail} failed`,
    verification.pending > 0 && `${verification.pending} pending`,
  ].filter(Boolean) as string[];
  const failed = verification.checks.filter((check) => check.status === "fail");
  const attention = failed.length + projection.open_items.length + (projection.unattributed_paths.length > 0 ? 1 : 0);
  return (
    <>
      <section className="border-b border-border/70 px-5 py-4" aria-label="Activity summary">
        {projection.headline ? (
          <p className="max-w-[68ch] text-heading leading-6 text-foreground">{projection.headline}</p>
        ) : (
          <p className="text-body text-muted-foreground">No summary was recorded.</p>
        )}
        {facts.length > 0 && (
          <p className="mt-2 text-label tabular-nums text-neutral-400">{facts.join(" · ")}</p>
        )}
        {projection.source === "legacy_journal" && (
          <p className="mt-2 text-caption text-muted-foreground">From the author journal frozen with this Snapshot.</p>
        )}
      </section>

      {attention > 0 && (
        <section className="border-b border-border/70 px-5 py-4">
          <SectionHeading meta={attention}>Reviewer attention</SectionHeading>
          <ul className="space-y-1.5">
            {failed.map((check) => (
              <li key={`fail-${check.sequence}`} className="flex gap-2 text-body leading-5">
                <XCircle size={13} className="mt-1 shrink-0 text-rose-300" aria-hidden="true" />
                <div className="min-w-0">
                  <span className="text-rose-200">{check.summary}</span>
                  <span className="ml-2 text-caption text-muted-foreground">{hostLabel(check.host)} · failed</span>
                  {check.detail && <div className="truncate text-label text-muted-foreground" title={check.detail}>{check.detail}</div>}
                  <OpenDevelopmentSession host={check.host} sessionId={check.session_id} context={check.summary} />
                </div>
              </li>
            ))}
            {projection.open_items.map((item) => (
              <li key={item.id} className="flex gap-2 text-body leading-5">
                <CircleAlert size={13} className="mt-1 shrink-0 text-amber-300" aria-hidden="true" />
                <div className="min-w-0 text-neutral-300">
                  {item.summary}
                  <span className="ml-2 text-caption text-muted-foreground">{hostLabel(item.host)} · {item.kind === "blocker" ? "blocker" : "open question"}</span>
                  <OpenDevelopmentSession host={item.host} sessionId={item.session_id} context={item.summary} />
                </div>
              </li>
            ))}
            {projection.unattributed_paths.length > 0 && (
              <li className="flex gap-2 text-body leading-5">
                <CircleDashed size={13} className="mt-1 shrink-0 text-neutral-400" aria-hidden="true" />
                <div className="min-w-0 text-neutral-300">
                  {plural(projection.unattributed_paths.length, "changed file")} with no recorded author
                  <div className="mt-0.5 flex flex-wrap gap-x-1">
                    {projection.unattributed_paths.slice(0, 6).map((path) => (
                      <PathButton key={path} path={path} onOpenPath={onOpenPath} onClose={onClose} tone="text-muted-foreground" />
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

function Contributors({ projection, newer, onOpenPath, onClose }: {
  projection: DevelopmentProjection;
  newer: DevelopmentProjection | null;
  onOpenPath?(path: string): void;
  onClose(): void;
}) {
  const recentKeys = new Set(newer?.contributors.map((item) => item.key) ?? []);
  const contributors = [
    ...(newer?.contributors ?? []),
    ...projection.contributors.filter((item) => !recentKeys.has(item.key)),
  ];
  if (contributors.length === 0) return null;
  return (
    <section className="border-b border-border/70 px-5 py-4" aria-label="Agent sessions">
      <SectionHeading meta={contributors.length}>Agent sessions</SectionHeading>
      <ul>
        {contributors.map((item) => (
          <ContributorRow key={item.key} item={item} recent={recentKeys.has(item.key)} onOpenPath={onOpenPath} onClose={onClose} />
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
    <section className="border-b border-border/70 px-5 py-4">
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
          <li key={`${check.sequence}-${check.name}`} className="flex items-center gap-2 text-label">
            <span className={statusTone(check.status)} aria-label={check.status}>
              {check.status === "pass" ? <CheckCircle2 size={12} /> : check.status === "fail" ? <XCircle size={12} /> : <CircleDashed size={12} />}
            </span>
            <span className="min-w-0 flex-1 truncate font-mono text-neutral-300" title={check.summary}>{check.summary}</span>
            <span className="shrink-0 text-caption text-muted-foreground">{hostLabel(check.host)}</span>
            <OpenDevelopmentSession host={check.host} sessionId={check.session_id} context={check.summary} />
          </li>
        ))}
      </ul>
      {verification.checks.length > 5 && (
        <UiButton variant="link" type="button" onClick={() => setExpanded((value) => !value)} className="mt-2 text-label text-muted-foreground hover:text-neutral-200">
          {expanded ? "Show fewer" : `Show all ${verification.checks.length}`}
        </UiButton>
      )}
    </section>
  );
}

function OutsideWorkspace({ projection }: { projection: DevelopmentProjection }) {
  const rows = projection.outside_workspace ?? [];
  const decisions = projection.approval_events ?? [];
  if (rows.length === 0 && decisions.length === 0) return null;
  return (
    <section className="border-b border-border/70 px-5 py-4" data-testid="development-outside-workspace">
      <SectionHeading meta={projection.counts.outside_workspace ?? rows.length}>Outside the repository</SectionHeading>
      <AgentActionList rows={rows} limit={12} />
      {decisions.length > 0 && (
        <ul className="mt-2 space-y-1" data-testid="development-approval-decisions">
          {decisions.slice(0, 12).map((item) => (
            <li key={item.id || `${item.sequence}`} className="flex min-w-0 items-baseline gap-2 text-caption">
              <span className={`shrink-0 font-medium ${item.status === "approved" ? "text-emerald-300" : "text-rose-300"}`}>
                {item.status === "approved" ? "Pre-approved" : "Denied"}
              </span>
              <span className="min-w-0 flex-1 truncate text-neutral-300" title={item.detail}>{item.summary}</span>
              <span className="shrink-0 text-muted-foreground">
                {hostLabel(item.host)}
                {item.at ? ` · ${shortTime(item.at)}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Decisions({ projection }: { projection: DevelopmentProjection }) {
  if (projection.decisions.length === 0 && !projection.plan) return null;
  return (
    <section className="border-b border-border/70 px-5 py-4">
      <SectionHeading meta={projection.decisions.length || undefined}>Decisions & facts</SectionHeading>
      <ul className="space-y-2">
        {projection.decisions.slice(0, 8).map((item) => (
          <li key={item.id} className="text-body leading-5 text-neutral-300">
            {item.summary}
            <span className="ml-2 text-caption text-muted-foreground">
              {hostLabel(item.host)}{item.role === "investigator" ? " · accepted finding" : ""}
            </span>
            <OpenDevelopmentSession host={item.host} sessionId={item.session_id} context={item.summary} />
          </li>
        ))}
      </ul>
      {projection.plan && (
        <details className="mt-3 text-label text-muted-foreground">
          <summary className="w-fit cursor-pointer hover:text-neutral-200">Latest plan · {hostLabel(projection.plan.host)}</summary>
          <div className="mt-1.5 whitespace-pre-wrap leading-5 text-neutral-400">{projection.plan.detail}</div>
          <OpenDevelopmentSession host={projection.plan.host} sessionId={projection.plan.session_id} context={projection.plan.summary} />
        </details>
      )}
    </section>
  );
}

function Feedback({ projection, revisionNumber }: { projection: DevelopmentProjection; revisionNumber: number }) {
  if (projection.feedback.length === 0) return null;
  return (
    <section className="border-b border-border/70 px-5 py-4">
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
            <li key={item.causation_id} className="flex gap-2 text-body leading-5">
              <Handshake size={13} className="mt-1 shrink-0 text-sky-300" aria-hidden="true" />
              <div className="min-w-0">
                <div className="text-neutral-200">
                  {who} {item.edit_count > 0 ? "addressed" : "received"} {plural(item.annotation_ids.length || 1, "comment")}
                </div>
                <div className="text-label text-muted-foreground">{parts.join(" · ")}</div>
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
    <section className="border-b border-border/70 px-5 py-4">
      <SectionHeading meta={projection.changed_paths.length}>Changed during development</SectionHeading>
      <ul className="grid gap-x-3 gap-y-0.5 sm:grid-cols-2">
        {rows.map((row) => (
          <li key={row.path} className="flex min-w-0 items-center gap-1.5">
            <PathButton path={row.path} onOpenPath={onOpenPath} onClose={onClose} tone={row.status === "reverted" ? "text-muted-foreground line-through" : "text-neutral-400"} />
            {row.in_snapshot === false && <span className="shrink-0 text-caption text-muted-foreground">not in Snapshot</span>}
            {row.sessions.length > 1 && <span className="shrink-0 text-caption text-muted-foreground">{row.sessions.length} agents</span>}
          </li>
        ))}
      </ul>
      {projection.changed_paths.length > 12 && (
        <UiButton variant="link" type="button" onClick={() => setExpanded((value) => !value)} className="mt-2 text-label text-muted-foreground hover:text-neutral-200">
          {expanded ? "Show fewer" : `Show all ${projection.changed_paths.length}`}
        </UiButton>
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
    <li className="relative border-l border-border py-2 pl-5">
      <span className={`absolute -left-[10px] top-[9px] inline-flex h-[19px] w-[19px] items-center justify-center rounded-full border border-border bg-background ${statusTone(event.status)}`} aria-hidden="true">
        {eventIcon(event)}
      </span>
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="min-w-0 flex-1 text-body leading-5 text-neutral-200">{event.summary}</span>
        <time className="shrink-0 text-caption tabular-nums text-muted-foreground" dateTime={event.at}>{shortTime(event.at)}</time>
      </div>
      <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 text-caption text-muted-foreground">
        <span>{hostLabel(event.host)}{event.role && event.role !== "author" ? ` · ${ROLE_LABEL[event.role] ?? event.role}` : ""}</span>
        <span>{event.kind.replace("_", " ")}</span>
        {event.status && event.status !== "changed" && <span className={statusTone(event.status)}>{event.status}</span>}
        {event.causation_id && <span className="text-sky-300/80">review follow-up</span>}
        <OpenDevelopmentSession host={event.host} sessionId={event.session_id} context={event.summary} />
        {event.path && event.kind !== "edit" && event.kind !== "revert" && (
          <PathButton path={event.path} onOpenPath={onOpenPath} onClose={onClose} tone="text-sky-300/70" />
        )}
      </div>
      {event.detail && event.detail !== event.summary && (
        <details className="mt-1 text-label text-muted-foreground">
          <summary className="w-fit cursor-pointer hover:text-neutral-300">Details</summary>
          <div className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap break-words leading-5">{event.detail}</div>
        </details>
      )}
    </li>
  );
}

const TIMELINE_PAGE = 40;
/** The server's largest Development timeline page. */
const TIMELINE_MAX_PAGE = 200;

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
  const shownCount = useRef(0);

  useEffect(() => setScope(initialScope), [initialScope]);

  const load = useCallback(async (before: number | null, limit = TIMELINE_PAGE) => {
    const token = ++request.current;
    if (before === null) setState("loading");
    try {
      const page = await fetchReviewDevelopmentEvents(reviewId, { revisionNumber, scope, view, before, limit });
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
    shownCount.current = events.length;
  }, [events]);

  // A new scope or filter starts over at the newest page. A live update (only
  // the cursor moved) refetches as many events as are shown, so the earlier
  // pages a reader loaded stay loaded.
  // lc-debt: a live update keeps at most the server's 200-event page; merge new events by sequence to keep more.
  const loadedFor = useRef({ load, cursor });
  useEffect(() => {
    const live = loadedFor.current.load === load && loadedFor.current.cursor !== cursor;
    loadedFor.current = { load, cursor };
    void load(null, live ? Math.min(TIMELINE_MAX_PAGE, Math.max(TIMELINE_PAGE, shownCount.current)) : TIMELINE_PAGE);
  }, [load, cursor]);

  const rows = useMemo(() => groupTimeline(events), [events]);

  return (
    <section className="px-5 py-4" aria-label="Activity timeline">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h3 className="mr-auto text-body font-semibold text-neutral-200">Timeline</h3>
        {hasSince && (
          <ToggleGroup
            variant="segment"
            aria-label="Timeline scope"
            itemClassName="px-2 py-1 text-label"
            value={scope}
            onChange={(value) => setScope(value as DevelopmentTimelineScope)}
            options={([["snapshot", `Snapshot ${revisionNumber}`], ["since", "Since"]] as Array<[DevelopmentTimelineScope, string]>).map(([value, label]) => ({ value, label }))}
          />
        )}
      </div>
      <ToggleGroup
        variant="segment"
        aria-label="Timeline filter"
        className="mb-3 flex w-full overflow-x-auto"
        itemClassName="flex-1 whitespace-nowrap px-2 py-1 text-label"
        value={view}
        onChange={(value) => setView(value as DevelopmentTimelineView)}
        options={VIEWS.map(([value, label]) => ({ value, label }))}
      />
      {state === "loading" && events.length === 0 ? (
        <div className="py-6 text-center text-label text-muted-foreground" role="status">Loading development activity…</div>
      ) : state === "failed" ? (
        <div className="rounded-md border border-rose-900/50 bg-rose-950/15 px-3 py-2 text-label text-rose-200" role="alert">
          Timeline unavailable: {error}
          <UiButton variant="link" type="button" onClick={() => void load(null)} className="ml-2 underline underline-offset-2">Retry</UiButton>
        </div>
      ) : rows.length === 0 ? (
        <div className="py-6 text-center text-label text-muted-foreground">No development activity in this view.</div>
      ) : (
        <ol className="ml-2.5">
          {rows.map((row) => row.type === "event" ? (
            <TimelineEvent key={row.event.id} event={row.event} onOpenPath={onOpenPath} onClose={onClose} />
          ) : (
            <li key={row.key} className="relative border-l border-border py-2 pl-5">
              <span className="absolute -left-[10px] top-[9px] inline-flex h-[19px] w-[19px] items-center justify-center rounded-full border border-border bg-background text-muted-foreground" aria-hidden="true">
                <MessageSquareText size={11} />
              </span>
              <details className="text-body">
                <summary className="cursor-pointer text-neutral-400 hover:text-neutral-200">
                  {plural(row.events.length, "progress update")} from {hostLabel(row.events[0].host)}
                  <span className="ml-2 text-caption tabular-nums text-muted-foreground">{shortTime(row.events[row.events.length - 1].at)}–{shortTime(row.events[0].at)}</span>
                </summary>
                <ul className="mt-1.5 space-y-1.5">
                  {row.events.map((event) => (
                    <li key={event.id} className="text-label leading-5 text-neutral-400">{event.summary}</li>
                  ))}
                </ul>
              </details>
            </li>
          ))}
        </ol>
      )}
      {nextBefore !== null && state !== "failed" && (
        <UiButton variant="ghost" size="compact" type="button" onClick={() => void load(nextBefore)} className="mt-2 h-7 text-label">
          Load earlier activity
        </UiButton>
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
  const { snapshot, since } = development;
  const newer = since && since.event_count > 0 ? since : null;
  const [timelineScope, setTimelineScope] = useState<DevelopmentTimelineScope>("snapshot");
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [session, setSession] = useState<DevelopmentSessionSelection | null>(null);
  const sessionTrigger = useRef<HTMLButtonElement | null>(null);
  const openSession = useCallback((selection: DevelopmentSessionSelection, trigger: HTMLButtonElement) => {
    sessionTrigger.current = trigger;
    setSession(selection);
  }, []);
  const backToDevelopment = useCallback(() => setSession(null), []);

  useEffect(() => {
    if (!session && sessionTrigger.current) {
      sessionTrigger.current.focus();
      sessionTrigger.current = null;
    }
  }, [session]);

  const empty = !snapshot.available && !newer;
  const newerPaths = newer?.newer_paths?.length ?? 0;

  return (
    <DevelopmentSessionContext.Provider value={openSession}>
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent
        className="w-[min(640px,100%)]"
        onEscapeKeyDown={(event) => {
          if (session) {
            event.preventDefault();
            backToDevelopment();
          }
        }}
      >
        <SheetHeader className="gap-3 px-5 py-3.5">
          <Workflow size={15} className="mt-0.5 shrink-0 text-neutral-400" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <SheetTitle className="text-title">Activity</SheetTitle>
            <p className="mt-0.5 text-label text-muted-foreground">
              {session ? "Latest recorded conversation" : `Snapshot ${development.revision_number} · ${historical ? "frozen historical record" : "frozen with this Snapshot"}`}
            </p>
          </div>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close activity">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>

        {newer && !session && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-sky-900/50 bg-sky-950/20 px-5 py-2.5 text-body text-sky-200" role="status">
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
            <UiButton unstyled
              type="button"
              onClick={() => {
                setEvidenceOpen(true);
                setTimelineScope((value) => (value === "since" ? "snapshot" : "since"));
              }}
              className="review-context-action border-sky-800/60 text-sky-200 hover:bg-sky-950/30"
            >
              {timelineScope === "since" ? "View Snapshot activity" : "View newer activity"}
            </UiButton>
          </div>
        )}

        <div hidden={Boolean(session)} className="min-h-0 flex-1 overflow-y-auto">
          {empty ? (
            <div className="px-5 py-10 text-center">
              <ListChecks size={18} className="mx-auto text-muted-foreground" aria-hidden="true" />
              <p className="mx-auto mt-3 max-w-[46ch] text-body leading-5 text-neutral-400">
                {development.reason || "No development evidence is attached to this Snapshot."}
              </p>
              <p className="mx-auto mt-1.5 max-w-[46ch] text-label leading-5 text-muted-foreground">
                Activity appears when an agent edits this Change, runs checks for it, or is bound to it.
              </p>
            </div>
          ) : (
            <>
              <Summary projection={snapshot} onOpenPath={onOpenPath} onClose={onClose} />
              <Contributors projection={snapshot} newer={newer} onOpenPath={onOpenPath} onClose={onClose} />
              {!historical && (
                <div className="border-b border-border/70 px-5 py-3 empty:hidden">
                  <AgentApprovalPanel reviewId={reviewId} initial={development.agent_approvals} />
                </div>
              )}
              <section data-testid="development-evidence">
                <UiButton unstyled
                  type="button"
                  aria-expanded={evidenceOpen}
                  onClick={() => setEvidenceOpen((open) => !open)}
                  className="w-full border-b border-border/70 px-5 py-3 text-left text-body font-medium text-neutral-300 hover:bg-muted/40 hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400"
                >
                  {evidenceOpen ? "Hide" : "Show"} checks, decisions, files and activity
                </UiButton>
                {evidenceOpen && (
                  <>
                    {(snapshot.intent || snapshot.narrative?.length) && (
                      <div className="border-b border-border/70 px-5 py-4 text-body leading-5 text-neutral-400">
                        {snapshot.intent && <p><span className="font-medium text-neutral-300">Asked: </span><span>{snapshot.intent}</span></p>}
                        {snapshot.narrative?.length ? <p className="mt-2" data-testid="development-narrative">{snapshot.narrative.join(" ")}</p> : null}
                      </div>
                    )}
                    <Verification projection={snapshot} />
                    <OutsideWorkspace projection={snapshot} />
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
                    <p className="px-5 pb-4 text-caption leading-4 text-muted-foreground">
                      Activity shows recorded edits, checks, decisions, and visible messages. Hidden reasoning, prompts, and raw tool output are not included.
                    </p>
                  </>
                )}
              </section>
            </>
          )}
        </div>
        {session && (
          <ReviewDevelopmentSession key={`${session.host}:${session.sessionId}`} session={session} onBack={backToDevelopment} />
        )}
      </SheetContent>
    </Sheet>
    </DevelopmentSessionContext.Provider>
  );
}
