/**
 * Agent Memory and Playbooks where the server keeps no memory or playbook
 * store (a local Dashboard): the lessons agents recorded at the end of their
 * sessions (`Trace.learnings`), read from the newest sessions this server holds.
 */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api, type TraceLearning, type TraceSummary } from "../api";
import { Alert, Badge, Card, EmptyState, SectionHeader } from "../components/WorkbenchUI";
import type { BadgeTone } from "../components/ui/badge";
import { ServerUnreachable, describeError, isUnreachable } from "../components/ServerUnreachable";
import { fmtWhen } from "../lib/format";

export type LessonShelf = "memory" | "playbook";

// lc-debt: reads the newest SESSION_WINDOW session summaries and groups their lessons in the browser;
// a server endpoint over every session's learnings would lift the window.
export const SESSION_WINDOW = 200;

export interface LessonSession {
  id: string;
  task: string;
  agent: string;
  at: string;
}

export interface SessionLesson {
  key: string;
  kind: TraceLearning["kind"];
  text: string;
  evidence: string;
  /** The sessions that recorded this lesson, newest first. */
  sessions: LessonSession[];
}

/** Playbook candidates go to Playbooks; rubric candidates are Checks, not memory; every other lesson is memory. */
export function shelfOf(learning: Pick<TraceLearning, "promote_to">): LessonShelf | null {
  if (learning.promote_to === "playbook") return "playbook";
  if (learning.promote_to === "rubric") return null;
  return "memory";
}

function stamp(value: string): number {
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : 0;
}

/** One row per distinct lesson on a shelf, newest first, with every session that recorded it. */
export function sessionLessons(traces: readonly TraceSummary[], shelf: LessonShelf): SessionLesson[] {
  const lessons = new Map<string, SessionLesson>();
  const newestFirst = [...traces].sort((a, b) => stamp(b.created_at) - stamp(a.created_at));
  for (const trace of newestFirst) {
    for (const learning of trace.learnings ?? []) {
      const text = (learning.text ?? "").trim();
      if (!text || shelfOf(learning) !== shelf) continue;
      const key = text.toLowerCase().replace(/\s+/g, " ");
      const session = { id: trace.session_id || trace.id, task: trace.task, agent: trace.agent, at: trace.created_at };
      const known = lessons.get(key);
      if (!known) {
        lessons.set(key, { key, kind: learning.kind ?? "note", text, evidence: learning.evidence ?? "", sessions: [session] });
        continue;
      }
      if (!known.sessions.some((row) => row.id === session.id)) known.sessions.push(session);
      if (!known.evidence && learning.evidence) known.evidence = learning.evidence;
    }
  }
  return [...lessons.values()];
}

const KIND: Record<TraceLearning["kind"], { label: string; tone: BadgeTone }> = {
  worked: { label: "Worked", tone: "success" },
  did_not_work: { label: "Didn't work", tone: "destructive" },
  next_rule: { label: "Next time", tone: "info" },
  risk: { label: "Risk", tone: "warning" },
  note: { label: "Note", tone: "neutral" },
};

const COPY: Record<LessonShelf, { eyebrow: string; title: string; description: string; empty: string; emptyWhy: string; noun: [string, string] }> = {
  memory: {
    eyebrow: "Agent Memory",
    title: "What your agents recall across sessions",
    description: "Lessons your agents recorded at the end of their sessions on this machine: what worked, what didn't and what to do next time.",
    empty: "No lessons recorded yet",
    emptyWhy: "When an agent finishes a session it can note what worked and what to avoid. Those notes appear here.",
    noun: ["lesson", "lessons"],
  },
  playbook: {
    eyebrow: "Playbooks",
    title: "Reusable procedures learned from past runs",
    description: "Procedures your agents proposed as playbooks at the end of their sessions on this machine. Each one links to the sessions it came from.",
    empty: "No playbooks proposed yet",
    emptyWhy: "When an agent finds a procedure worth reusing, it proposes it as a playbook at the end of the session. Those appear here.",
    noun: ["playbook", "playbooks"],
  },
};

export function SessionLessons({ shelf }: { shelf: LessonShelf }) {
  const [traces, setTraces] = useState<TraceSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setTraces(null);
    setError(null);
    api
      .traces(SESSION_WINDOW, 0)
      .then((page) => {
        if (active) setTraces(page.items ?? []);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason ?? new Error("The sessions could not be read."));
      });
    return () => {
      active = false;
    };
  }, [attempt]);

  const lessons = useMemo(() => (traces ? sessionLessons(traces, shelf) : []), [traces, shelf]);
  const copy = COPY[shelf];

  return (
    <div className="space-y-4" data-testid={`session-lessons-${shelf}`}>
      <SectionHeader eyebrow={copy.eyebrow} title={copy.title} description={copy.description} />
      {error !== null && isUnreachable(error) && <ServerUnreachable onRetry={() => setAttempt((n) => n + 1)} />}
      {error !== null && !isUnreachable(error) && (
        <div role="alert">
          <Alert tone="destructive" title="Couldn't read your sessions" description={describeError(error)} />
        </div>
      )}
      {error === null && traces === null && (
        <p role="status" className="text-body text-muted-foreground">Reading your sessions…</p>
      )}
      {traces !== null && lessons.length === 0 && <EmptyState title={copy.empty} description={copy.emptyWhy} />}
      {traces !== null && lessons.length > 0 && (
        <>
          <p className="text-caption text-muted-foreground">
            {lessons.length} {lessons.length === 1 ? copy.noun[0] : copy.noun[1]} from your{" "}
            {traces.length >= SESSION_WINDOW ? `${SESSION_WINDOW} most recent sessions` : `${traces.length} ${traces.length === 1 ? "session" : "sessions"}`}.
          </p>
          <ul className="space-y-2" aria-label={copy.eyebrow}>
            {lessons.map((lesson) => (
              <LessonRow key={lesson.key} lesson={lesson} />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function LessonRow({ lesson }: { lesson: SessionLesson }) {
  const [newest] = lesson.sessions;
  const others = lesson.sessions.length - 1;
  const kind = KIND[lesson.kind] ?? KIND.note;
  return (
    <li>
      <Card className="space-y-2 p-3">
        <div className="flex flex-wrap items-start gap-2">
          <Badge tone={kind.tone}>{kind.label}</Badge>
          <p className="min-w-0 flex-1 text-body text-foreground">{lesson.text}</p>
        </div>
        {lesson.evidence && <p className="text-caption text-muted-foreground">Evidence: {lesson.evidence}</p>}
        <p className="text-caption text-muted-foreground">
          From{" "}
          <Link to={`/activity/${encodeURIComponent(newest.id)}`} className="text-foreground underline underline-offset-2">
            {newest.task || "a session"}
          </Link>
          {newest.agent ? ` · ${newest.agent}` : ""}
          {newest.at ? ` · ${fmtWhen(newest.at)}` : ""}
          {others > 0 ? ` · and ${others} more ${others === 1 ? "session" : "sessions"}` : ""}
        </p>
      </Card>
    </li>
  );
}
