import { Button } from "../../components/ui/button";
import type { ReviewDirectoryRow } from "../types";
import { initials, teamLine } from "./teamCopy";

/**
 * What a list row says about its team (SPEC 4.16): who started, who took it, who it was handed to, and the row actions
 * "I'll take this" and "Hand off" while nobody has (and "Let go" to the one who took it). Draws nothing where the
 * server sent nothing, which is everywhere on a local install. It sits beside the row's link, never inside it.
 */
export function TeamRowLine({ row, now, actions, onTake, onHandOff, onRelease, className, lineId }: {
  row: Pick<ReviewDirectoryRow, "title" | "team">;
  now: number;
  /** The two actions show on the team's open rows only. */
  actions: boolean;
  onTake: () => void;
  onHandOff: () => void;
  onRelease: () => void;
  className?: string;
  /** The id of the line, for the row's link to describe itself by. */
  lineId?: string;
}) {
  const line = teamLine(row.team, now);
  const free = actions && !row.team?.claim && !row.team?.handoff;
  const mine = Boolean(row.team?.claim?.by_me);
  if (!line && !free) return null;
  return (
    <div data-testid="team-row" className={className ?? "flex basis-full flex-wrap items-center gap-x-3 gap-y-1 pb-2 pl-2"}>
      {line ? (
        <span id={lineId} data-testid="team-row-line" data-presence={line.tone} className="inline-flex min-h-6 items-center gap-1.5 text-meta text-muted-foreground">
          <span aria-hidden="true" className="inline-flex size-5 shrink-0 items-center justify-center rounded-full border border-border bg-muted font-mono text-tag">{initials(line.who)}</span>
          {line.text}
        </span>
      ) : null}
      {free ? (
        <>
          <Button variant="outline" size="sm" className="sm:min-h-8" data-testid="team-take" aria-label={`I'll take this: ${row.title}`} onClick={onTake}>I'll take this</Button>
          <Button variant="ghost" size="sm" className="sm:min-h-8" data-testid="team-handoff" aria-label={`Hand off: ${row.title}`} onClick={onHandOff}>Hand off</Button>
        </>
      ) : null}
      {mine && actions ? (
        <>
          <Button variant="ghost" size="sm" className="sm:min-h-8" data-testid="team-release" aria-label={`Let go: ${row.title}`} onClick={onRelease}>Let go</Button>
          <Button variant="ghost" size="sm" className="sm:min-h-8" data-testid="team-handoff" aria-label={`Hand off: ${row.title}`} onClick={onHandOff}>Hand off</Button>
        </>
      ) : null}
    </div>
  );
}
