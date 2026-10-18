import { Button as UiButton } from "../components/ui/button";
import { useEffect, useState } from "react";

import { AgentActionList } from "./AgentActions";
import { ROLE_LABEL, hostLabel, samePath, shortTime, statusTone } from "./developmentLabels";
import { fetchReviewDevelopmentEvents } from "./reviewApi";
import type { DevelopmentContributor, DevelopmentEvent, ReviewDevelopment } from "./types";

interface ContextDevelopmentProps {
  reviewId: string;
  development: ReviewDevelopment;
  path: string;
  onOpenDevelopment?(): void;
}

/** How much Development touched one file in this Snapshot (the drawer tab's count). */
export function fileDevelopmentCount(development: ReviewDevelopment, path: string): number {
  const changed = development.snapshot.changed_paths.find((item) => samePath(item.path, path));
  return changed ? Math.max(changed.edits, changed.sessions.length) : 0;
}

function contributorsFor(development: ReviewDevelopment, path: string): DevelopmentContributor[] {
  const { snapshot } = development;
  const changed = snapshot.changed_paths.find((item) => samePath(item.path, path));
  const byKey = new Map(snapshot.contributors.map((item) => [item.key, item]));
  const listed = (changed?.sessions ?? [])
    .map((key) => byKey.get(key))
    .filter((item): item is DevelopmentContributor => Boolean(item));
  if (listed.length > 0) return listed;
  // Attribution-only authors carry their paths but no stream rows.
  return snapshot.contributors.filter((item) => item.role === "author" && item.paths.some((candidate) => samePath(candidate, path)));
}

export default function ContextDevelopment({ reviewId, development, path, onOpenDevelopment }: ContextDevelopmentProps) {
  const { snapshot, since } = development;
  const contributors = contributorsFor(development, path);
  const changed = snapshot.changed_paths.find((item) => samePath(item.path, path));
  const unattributed = snapshot.unattributed_paths.some((candidate) => samePath(candidate, path));
  const newer = since?.changed_paths.find((item) => samePath(item.path, path)) ?? null;
  const feedback = snapshot.feedback.filter((item) => item.paths.some((candidate) => samePath(candidate, path)));
  const outside = snapshot.outside_workspace ?? [];
  const [events, setEvents] = useState<DevelopmentEvent[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setEvents(null);
    setError("");
    fetchReviewDevelopmentEvents(reviewId, { revisionNumber: development.revision_number, scope: "snapshot", path, limit: 30 })
      .then((page) => {
        if (active) setEvents(page.events);
      })
      .catch((reason: unknown) => {
        if (active) setError(String(reason instanceof Error ? reason.message : reason));
      });
    return () => {
      active = false;
    };
  }, [reviewId, development.revision_number, development.snapshot.cursor, path]);

  return (
    <div className="space-y-4" data-testid="context-development">
      <section>
        <div className="review-kicker mb-2">Who changed this file</div>
        {contributors.length > 0 ? (
          <ul className="space-y-1.5">
            {contributors.map((item) => (
              <li key={item.key} className="flex min-w-0 items-baseline gap-2 text-label">
                <span className="shrink-0 text-neutral-200" title={`${item.host}:${item.session_id}`}>{hostLabel(item.host)}</span>
                {item.model && <span className="min-w-0 truncate font-mono text-micro text-muted-foreground">{item.model}</span>}
                <span className="ml-auto shrink-0 text-caption text-muted-foreground">{ROLE_LABEL[item.role] ?? item.role}</span>
              </li>
            ))}
          </ul>
        ) : unattributed ? (
          <p className="text-caption leading-relaxed text-amber-300/85">
            This file changed, but no agent recorded an exact edit to it. Treat it as a manual or unexplained change.
          </p>
        ) : (
          <p className="text-caption leading-relaxed text-muted-foreground">No development activity touched this file in this Snapshot.</p>
        )}
        {changed && changed.edits > 0 && (
          <p className="mt-1.5 text-caption tabular-nums text-muted-foreground">
            {changed.edits} recorded edit{changed.edits === 1 ? "" : "s"}
            {changed.last_at && ` · last ${shortTime(changed.last_at)}`}
          </p>
        )}
      </section>

      {newer && (
        <p className="border-l border-sky-800/70 pl-2 text-caption leading-relaxed text-sky-300/90" role="status">
          Changed again after this Snapshot by {[...new Set(newer.sessions.map((key) => hostLabel(key.split(":")[0] ?? "")))].join(", ") || "an agent"}
          {newer.edits > 0 && ` · ${newer.edits} edit${newer.edits === 1 ? "" : "s"}`}
        </p>
      )}

      {feedback.length > 0 && (
        <section className="border-t border-neutral-900 pt-3">
          <div className="review-kicker mb-2">Review feedback</div>
          <ul className="space-y-1.5 text-caption leading-relaxed text-neutral-300">
            {feedback.map((item) => (
              <li key={item.causation_id}>
                {item.summary || "Review feedback"}
                <span className="ml-1.5 text-muted-foreground">
                  {item.included_in_revision !== null ? `addressed in Snapshot ${item.included_in_revision}` : item.edit_count > 0 ? "being addressed" : "no follow-up edits yet"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="border-t border-neutral-900 pt-3">
        <div className="review-kicker mb-2">File activity</div>
        {error ? (
          <p className="text-caption text-rose-300/85" title={error}>Could not load this file's development activity.</p>
        ) : events === null ? (
          <p className="text-caption text-muted-foreground">Loading activity…</p>
        ) : events.length === 0 ? (
          <p className="text-caption text-muted-foreground">No recorded events name this file.</p>
        ) : (
          <ul className="space-y-2">
            {events.map((event) => (
              <li key={event.id} className="border-l border-border pl-2 text-caption">
                <div className="flex min-w-0 items-baseline gap-2">
                  <span className="min-w-0 flex-1 truncate text-neutral-300" title={event.summary}>{event.summary}</span>
                  {event.status && <span className={`shrink-0 ${statusTone(event.status)}`}>{event.status}</span>}
                </div>
                <div className="mt-0.5 text-micro text-muted-foreground">
                  {hostLabel(event.host)}
                  {event.at && ` · ${shortTime(event.at)}`}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {outside.length > 0 && (
        <section className="border-t border-neutral-900 pt-3" data-testid="context-outside-workspace">
          <div className="review-kicker mb-2">Outside the repository</div>
          <AgentActionList rows={outside} limit={6} />
        </section>
      )}

      {onOpenDevelopment && (
        <UiButton unstyled type="button" onClick={onOpenDevelopment} className="review-context-action">
          Open Activity
        </UiButton>
      )}

      <div className="border-t border-neutral-900 pt-2 text-micro leading-relaxed text-muted-foreground">
        Observable development evidence only: edits, checks, decisions and visible messages. Hidden reasoning is not captured.
      </div>
    </div>
  );
}
