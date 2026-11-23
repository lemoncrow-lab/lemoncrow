import { forwardRef, type MouseEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { Badge } from "../../components/ui/badge";
import { Button, buttonVariants } from "../../components/ui/button";
import { cn } from "../../lib/utils";
import { Checkbox } from "../../components/ui/checkbox";
import { plural } from "../overview/overviewModel";
import { POLICY_EDIT_TITLE } from "../policy/policyEdit";
import type { ReviewDirectoryRow } from "../types";
import {
  ageText, countsLine, placeOf, rangeOf, resumeLine, rowResume, sourceOf, stateText, SECTION_TITLE, tallyOf, waitingSinceMs, type HomeSectionId,
} from "./homeModel";
import { rangeText } from "./timeEstimate";

export interface HeroTask {
  text: string;
  host: string;
}

function Dot({ tone }: { tone: "p1" | "ok" | "ai" }) {
  return <span aria-hidden="true" className={`size-2 shrink-0 rounded-full ${tone === "p1" ? "bg-p1-solid" : tone === "ok" ? "bg-ok-solid" : "bg-ai"}`} />;
}

/** "2 decisions need you, plus 1 quick check." from the server's split; empty when it sent none. */
export function needsLine(row: ReviewDirectoryRow): string {
  const tally = tallyOf(row);
  if (!tally) return "";
  if (tally.decisions === 0) return tally.quickChecks > 0 ? `No decisions, ${plural(tally.quickChecks, "quick check")}.` : "";
  const head = `${plural(tally.decisions, "decision")} ${tally.decisions === 1 ? "needs" : "need"} you`;
  return tally.quickChecks > 0 ? `${head}, plus ${plural(tally.quickChecks, "quick check")}.` : `${head}.`;
}

/** The hero card: the top Your turn review (SPEC 4.1). One lemon button; Open in terminal and Snooze are ghost. */
export const Hero = forwardRef<HTMLDivElement, {
  row: ReviewDirectoryRow;
  /** `/r/<review>`, the address the review opens at. */
  path: string;
  now: number;
  active: boolean;
  task: HeroTask | null;
  onStart: () => void;
  /** Absent where the source offers no terminal command / no snooze (hosted). */
  onTerminal?: () => void;
  onSnooze?: () => void;
  terminalOpen: boolean;
  children?: ReactNode;
}>(function Hero({ row, path, now, active, task, onStart, onTerminal, onSnooze, terminalOpen, children }, ref) {
  const since = waitingSinceMs(row);
  const range = rangeOf(row);
  const resume = resumeLine(rowResume(row), row.unsent_count ?? 0);
  const started = Boolean(row.queue?.resume);
  const needs = needsLine(row);
  // A row with no part counts (the hosted Inbox sends none) has no line here: nothing to count, nothing to estimate.
  // The tally is said once: the needs line above carries it, so the pips line keeps only the time.
  const pips = [needs ? "" : countsLine(row), range ? `about ${rangeText(range)}` : ""].filter(Boolean).join(" · ");
  return (
    <div
      ref={ref}
      tabIndex={-1}
      data-row={row.id}
      data-hero="true"
      data-review-path={path}
      aria-current={active ? "true" : undefined}
      className={`rounded-lg border bg-card p-4 shadow-frame outline-none ${active ? "border-foreground/60" : "border-border"}`}
    >
      <div className="flex flex-wrap items-center gap-2 text-meta">
        <Badge>{sourceOf(row)}</Badge>
        <span className="font-mono text-muted-foreground">{placeOf(row)}</span>
        {since !== null ? (
          <span className="inline-flex items-center gap-1.5 text-p1"><Dot tone="p1" />waiting on you {ageText(now - since)}</span>
        ) : null}
      </div>
      <h2 className="mt-2 text-title font-semibold [overflow-wrap:anywhere]">{row.title}</h2>
      {task ? (
        <div data-testid="home-hero-asked" className="mt-3 rounded-md border border-border bg-muted/40 p-3">
          <p className="font-mono text-tag uppercase text-muted-foreground">Asked</p>
          <p className="mt-1 text-body [overflow-wrap:anywhere]">{task.text}</p>
          <p className="mt-1 text-meta text-muted-foreground">{task.host ? `${task.host} · ` : ""}from the agent session</p>
          <p data-testid="home-untrusted" className="mt-1 text-meta text-muted-foreground">Task text from the agent session · treated as untrusted</p>
        </div>
      ) : null}
      {row.edits_policy ? <p className="mt-2 text-label font-semibold text-p0" data-testid="home-edits-policy">{POLICY_EDIT_TITLE}</p> : null}
      {needs ? <p className="mt-3 text-body">{needs}</p> : null}
      {resume ? <p data-resume="" data-testid="home-resume" className="mt-1 text-label text-muted-foreground">{resume}</p> : null}
      {pips ? <p data-testid="home-hero-pips" className="mt-2 font-mono text-meta text-muted-foreground">{pips}</p> : null}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="brand" data-testid="home-start" onClick={onStart}>
          {started ? "Continue review" : "Start review"}
          <kbd className="rounded border border-current px-1 font-mono text-tag pointer-coarse:hidden">Enter</kbd>
        </Button>
        {onTerminal ? <Button variant="outline" aria-expanded={terminalOpen} onClick={onTerminal}>Open in terminal</Button> : null}
        {onSnooze ? (
          <Button variant="outline" onClick={onSnooze}>
            Snooze
            <kbd className="rounded border border-border px-1 font-mono text-tag pointer-coarse:hidden">S</kbd>
          </Button>
        ) : null}
      </div>
      {children}
    </div>
  );
});

/** One list row. Dense rows are 52 px tall (SPEC 4.1); both stay at least 44 px on a phone. */
export const QueueRow = forwardRef<HTMLLIElement, {
  row: ReviewDirectoryRow;
  section: HomeSectionId;
  now: number;
  dense: boolean;
  active: boolean;
  selected: boolean;
  selectable: boolean;
  href: string;
  /** `/r/<review>`, the address the review opens at (the link may go further, to the saved part). */
  basePath: string;
  onToggle: () => void;
  /** Hosted only (SPEC 4.16): the team's line and actions under the row, and the chance to ask before opening it. */
  team?: ReactNode;
  onOpen?: (event: MouseEvent<HTMLAnchorElement>) => void;
  /** The id of the team line, so the link is described by it (the line sits beside the link, never inside it). */
  describedBy?: string;
}>(function QueueRow({ row, section, now, dense, active, selected, selectable, href, basePath, onToggle, team, onOpen, describedBy }, ref) {
  const since = waitingSinceMs(row);
  const range = rangeOf(row);
  const unsent = row.unsent_count ?? 0;
  // The section heading already says "Your turn" or "Waiting on others": a row repeats only a state that adds to it.
  const state = stateText(row, section, new Date(now));
  const detail = [
    section === "finished" || section === "done" ? "" : countsLine(row),
    range && section !== "finished" && section !== "done" ? rangeText(range) : "",
    since !== null && (section === "yourTurn" || section === "cameBack") ? `waiting ${ageText(now - since)}` : "",
  ].filter(Boolean);
  return (
    <li
      ref={ref}
      tabIndex={-1}
      data-row={row.id}
      data-section={section}
      data-review-path={basePath}
      aria-current={active ? "true" : undefined}
      className={`flex items-center gap-2 rounded-md border px-2 outline-none ${team ? "flex-wrap" : ""} ${dense ? "min-h-[52px]" : "min-h-14 py-2"} pointer-coarse:min-h-14 ${active ? "border-foreground/60 bg-accent/40" : "border-border"}`}
    >
      {selectable ? (
        <label className="flex min-w-11 shrink-0 cursor-pointer items-center justify-center">
          <Checkbox
            checked={selected}
            onChange={onToggle}
            aria-label={`Select ${row.title}`}
            className="size-5 pointer-coarse:size-6"
          />
        </label>
      ) : null}
      <Link
        to={href}
        onClick={onOpen}
        aria-describedby={describedBy}
        data-testid="home-row-link"
        className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Badge>{sourceOf(row)}</Badge>
        <span className="font-mono text-meta text-muted-foreground">{placeOf(row)}</span>
        <span className={`min-w-0 flex-1 text-body font-medium ${dense ? "truncate" : "line-clamp-2"}`} title={row.title}>{row.title}</span>
        {/* A roomy row puts its facts on their own line, so they never squeeze the title into a narrow column. */}
        <span className={`flex max-w-full flex-wrap items-center gap-x-2 text-meta text-muted-foreground ${dense ? "shrink-0" : "basis-full"}`}>
          {row.edits_policy ? <span className="font-semibold text-p0" data-testid="home-edits-policy">{POLICY_EDIT_TITLE}</span> : null}
          {unsent > 0 ? <span className="text-p1">{plural(unsent, "note")} not sent</span> : null}
          {detail.map((part) => <span key={part}>{part}</span>)}
          {state && state !== SECTION_TITLE[section] ? <span className={section === "cameBack" ? "text-p2" : undefined}>{state}</span> : null}
        </span>
      </Link>
      {section === "secondSignature" ? (
        <Link to={href} data-testid="home-second-review" className={cn(buttonVariants({ variant: "brand" }), " shrink-0")}>Review</Link>
      ) : null}
      {team}
    </li>
  );
});
