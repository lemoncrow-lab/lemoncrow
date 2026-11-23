/**
 * The Brief (`/r/<review>/brief`): the decision-first summary of one revision. What changed and how risky it reads, the
 * parts to read first, the decisions only a person can settle, the behavior observed before and after, and what nothing
 * has verified. Every line but the labeled Project history comes from the Review's plan for this revision; nothing here
 * calls a model on load.
 */
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { ChevronRight, ScanSearch } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { targetAnswer } from "../answerModel";
import BlindSpots from "../blindSpots/BlindSpots";
import { blindSpotsModel } from "../blindSpots/blindSpotsModel";
import { FLOW_RAIL_CLASS, FlowShortcutsDialog, FlowTopBar, repoNameOf, useShortcutsKey } from "../FlowTopBar";
import { counterFor } from "../focus/focusModel";
import { investigationContext, type InvestigationInput } from "../investigation/investigationContext";
import { Label } from "../overview/OverviewParts";
import { partName, partSlug, plural } from "../overview/overviewModel";
import { useFlowPaletteScreen } from "../palette/FlowPalette";
import { partEntries, reviewPlace } from "../palette/partEntries";
import ProjectHistory from "../ProjectHistory";
import { revisionName } from "../revisionLabel";
import type { EvidenceRow, ReviewDecisionEventInput, ReviewOverview, ReviewProjection, ReviewProjectionDecision, ReviewTarget } from "../types";
import { useReadingViewSwitch, useReviewFlowNavigation } from "../useReviewFlowNavigation";
import BehaviorChanges, { type BehaviorHandlers } from "./BehaviorChanges";
import { DecisionCard } from "./BriefDecisionCard";
import BriefSignals from "./BriefSignals";
import BriefWorkspaceImpact, { workspaceImpactOf } from "./BriefWorkspaceImpact";
import "../blindSpots/blindSpots.css";

export interface ReviewBriefScreenProps extends BehaviorHandlers {
  reviewRef: string;
  apiReviewId: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  complete?: boolean;
  /** The plan of the revision on screen; null while it loads or where the Review has none. */
  projection: ReviewProjection | null;
  loading?: boolean;
  checks?: readonly EvidenceRow[] | null;
  revision?: InvestigationInput["revision"];
  /** Records one decision against this Snapshot; absent when read-only or historical. */
  onDecision?: (key: string, input: ReviewDecisionEventInput) => Promise<void>;
  /** The change has a Map to walk, so the X-Ray is offered. */
  xrayAvailable?: boolean;
  onAsk?: (target: ReviewTarget) => void;
  onAskChange?: () => void;
  askPanel?: ReactNode;
}

/** How many review-first parts the first screen names; the rest are one click away in All files. */
const ATTENTION_LIMIT = 5;

function Section({ title, testId, action, children }: { title: string; testId: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section aria-label={title} className="rounded-lg border border-border bg-card" data-testid={testId}>
      <div className="flex min-h-9 items-center justify-between gap-2 px-3 py-2">
        <Label>{title}</Label>
        {action}
      </div>
      <div className="flex flex-col gap-2 border-t border-border p-3">{children}</div>
    </section>
  );
}

/** Review-first parts in the server's one order, the same order Focus and the queue use. */
export function reviewFirstParts(targets: readonly ReviewTarget[]): ReviewTarget[] {
  return targets
    .filter((target) => target.priority?.attention === "review_first")
    .sort((a, b) => (a.priority?.position ?? Number.MAX_SAFE_INTEGER) - (b.priority?.position ?? Number.MAX_SAFE_INTEGER));
}

/** Decisions a person can still act on, then the deferred ones; withdrawn ones are listed apart. */
export function briefDecisions(projection: ReviewProjection) {
  const live = projection.decisions.filter((decision) => !decision.state?.withdrawn);
  const deferred = (decision: ReviewProjectionDecision) => decision.state?.disposition === "deferred" && !decision.state.navigable;
  return {
    active: live.filter((decision) => !deferred(decision)),
    deferred: live.filter(deferred),
    withdrawn: projection.decisions.filter((decision) => decision.state?.withdrawn),
  };
}

export default function ReviewBriefScreen(props: ReviewBriefScreenProps) {
  const { reviewRef, apiReviewId, overview, targets, projection, checks, revision, onDecision, onAsk, onAskChange } = props;
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const readingView = useReadingViewSwitch(reviewRef, targets);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  useShortcutsKey(() => setShortcutsOpen(true));
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    parts: partEntries(reviewRef, targets, "one"),
    place: reviewPlace(repoNameOf(overview), overview.title, "Brief"),
    openShortcuts: () => setShortcutsOpen(true),
    context: { screen: "other", actions: {} },
  });

  const byId = useMemo(() => new Map(targets.map((target) => [target.target_id, target])), [targets]);
  const back = useCallback(() => goTo({ kind: "overview" }), [goTo]);
  const openPart = useCallback((target: ReviewTarget) => goTo({ kind: "one", part: partSlug(target, targets) }), [goTo, targets]);
  const decisionParts = useMemo(() => targets.filter((target) => targetAnswer(target).category === "decision"), [targets]);
  const openTarget = useCallback((targetId: string) => {
    const target = byId.get(targetId);
    if (target) openPart(target);
  }, [byId, openPart]);
  const ask = useCallback((targetId: string | null) => {
    const target = targetId ? byId.get(targetId) : undefined;
    if (target && onAsk) onAsk(target);
    else onAskChange?.();
  }, [byId, onAsk, onAskChange]);
  const canAsk = Boolean(onAsk || onAskChange);
  const canDecide = Boolean(onDecision) && !projection?.historical;

  const blindSpots = useMemo(() => (projection
    ? blindSpotsModel({
      context: investigationContext({ projection, targets, checks, revision }),
      projection,
      targets,
      capabilities: { openSource: true, evidence: true, ask: canAsk, decide: canDecide },
    })
    : null), [canAsk, canDecide, checks, projection, revision, targets]);

  const attention = useMemo(() => reviewFirstParts(targets), [targets]);
  const decisionCard = (decision: ReviewProjectionDecision) => (
    <DecisionCard key={decision.key} decision={decision} byId={byId} onOpenTarget={openTarget} onAsk={canAsk ? ask : undefined} onDecision={canDecide ? onDecision : undefined} />
  );

  const counter = counterFor(targets, "", props.complete !== false);
  const decisions = projection ? briefDecisions(projection) : null;
  const behavior = projection?.behavior ?? null;
  const summary = projection?.summary;
  // The Review's own name leads (the server's headline); a server without one gets the name the top bar shows.
  const title = summary?.headline?.title || overview.title || overview.session.title || "";
  // "No check ran" is said once: when the Risk line already says it, the fact line does not repeat it.
  // lc-debt: matched by wording; upgrade path: a structured `checks_ran` flag on the risk and the fact from the server.
  const NO_CHECK = /no (test or )?checks? ran/i;
  const riskSaysNoCheck = NO_CHECK.test(summary?.risk?.reason ?? "");
  const facts = (summary?.facts ?? []).filter((fact) => fact.text !== title && !(riskSaysNoCheck && fact.kind === "unchecked" && NO_CHECK.test(fact.text)));
  const historical = Boolean(projection?.historical);
  const added = targets.reduce((sum, target) => sum + target.additions, 0);
  const removed = targets.reduce((sum, target) => sum + target.deletions, 0);
  const files = new Set(targets.map((target) => target.path)).size;
  const counts = summary?.headline?.counts
    || [plural(files, "file"), plural(targets.length, "part"), added + removed > 0 ? `+${added} −${removed}` : ""].filter(Boolean).join(" · ");
  const workspaceImpact = workspaceImpactOf(projection?.workspace_impact);

  return (
    <>
      <FlowTopBar
        repo={repoNameOf(overview)}
        documentTitle="Brief"
        title={overview.title || overview.session.title}
        revisionNumber={overview.revision.revision_number}
        back={{ text: "Overview", onClick: back }}
        view={readingView}
        counter={counter}
        status={counter ? undefined : { text: "Loading all parts…" }}
        onAsk={onAskChange}
        onShortcuts={() => setShortcutsOpen(true)}
      />
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend="? all shortcuts" />
      <div className="flex flex-1" data-testid="flow-brief-screen">
        <div role="region" aria-label="Brief" className="min-w-0 flex-1" data-review-scroll-root="brief">
          {!projection || !decisions || !blindSpots ? (
            <div role="status" className="mx-auto flex max-w-3xl flex-col items-start gap-2 px-4 py-6 text-label text-muted-foreground" data-testid="brief-unavailable">
              <span>{props.loading ? "Preparing the Brief…" : "No Brief is available for this revision."}</span>
              {props.loading ? null : <Button variant="outline" size="sm" onClick={back}>Back to the Overview</Button>}
            </div>
          ) : (
            <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-6">
              <div className="flex flex-col gap-2">
                <Label>
                  {revisionName(overview.revision.revision_number)}
                  {historical ? " · Historical" : ""}
                </Label>
                <h1 className="text-title font-semibold text-foreground" data-testid="brief-title">{title || "Review this change"}</h1>
                <p className="text-meta text-muted-foreground" data-testid="brief-size">{counts}</p>
                {summary?.risk ? <BriefSignals risk={summary.risk} targets={targets} onOpenTarget={openTarget} /> : null}
                {facts.length > 0 ? (
                  <ul className="flex flex-col gap-0.5 text-caption" data-testid="brief-change-facts">
                    {facts.map((fact) => <li key={fact.text} className={fact.kind === "unchecked" ? "text-p1" : "text-foreground"}>{fact.text}</li>)}
                  </ul>
                ) : null}
                {props.xrayAvailable ? (
                  <Button variant="outline" size="sm" className="self-start" data-testid="brief-open-xray" onClick={() => goTo({ kind: "xray", at: "" })}>
                    <ScanSearch size={16} aria-hidden="true" /> X-Ray
                  </Button>
                ) : null}
              </div>

              {attention.length > 0 ? (
                <Section title={`Read first · ${plural(attention.length, "part")}`} testId="brief-attention">
                  <ul className="flex flex-col">
                    {attention.slice(0, ATTENTION_LIMIT).map((target) => {
                      const reason = target.priority?.reasons[0]?.detail ?? "";
                      return (
                        <li key={target.target_id}>
                          <Button variant="ghost" className="h-auto w-full justify-between gap-3 px-2 py-2 text-left" data-testid="brief-attention-part" onClick={() => openPart(target)}>
                            <span className="flex min-w-0 flex-col gap-0.5">
                              <span className="truncate font-mono text-label text-foreground">{partName(target)}<span className="font-sans text-muted-foreground"> · {target.path}</span></span>
                              {reason ? <span className="truncate text-meta font-normal text-muted-foreground">{reason}</span> : null}
                            </span>
                            <ChevronRight size={16} aria-hidden="true" className="shrink-0" />
                          </Button>
                        </li>
                      );
                    })}
                  </ul>
                  {attention.length > ATTENTION_LIMIT ? (
                    <Button variant="ghost" size="sm" className="self-start" data-testid="brief-attention-more" onClick={() => goTo({ kind: "all", part: "" })}>
                      {`${attention.length - ATTENTION_LIMIT} more · open All files`}
                    </Button>
                  ) : null}
                </Section>
              ) : null}

              <Section title={decisions.active.length > 0 ? `Decisions · ${decisions.active.length}` : "Decisions"} testId="brief-decisions">
                {decisions.active.length > 0
                  ? decisions.active.map(decisionCard)
                  : decisionParts.length > 0
                    // The same count Home and the Overview show: parts whose answer is a decision.
                    ? <div className="flex flex-wrap items-center gap-2 text-caption text-muted-foreground" data-testid="brief-no-decisions">
                      <span>{`${plural(decisionParts.length, "part")} ${decisionParts.length === 1 ? "needs" : "need"} your decision.`}</span>
                      <Button variant="outline" size="sm" onClick={() => openPart(decisionParts[0])}>Open the first</Button>
                    </div>
                    : <p className="text-caption text-muted-foreground" data-testid="brief-no-decisions">No part needs your decision on this revision.</p>}
                {decisions.deferred.length > 0 ? (
                  <details className="text-caption text-muted-foreground" data-testid="brief-deferred">
                    <summary className="cursor-pointer">{`Left for later · ${decisions.deferred.length}`}</summary>
                    <div className="mt-2 flex flex-col gap-2">{decisions.deferred.map(decisionCard)}</div>
                  </details>
                ) : null}
                {decisions.withdrawn.length > 0 ? (
                  <details className="text-caption text-muted-foreground" data-testid="brief-withdrawn">
                    <summary className="cursor-pointer">{`Withdrawn by the author · ${decisions.withdrawn.length}`}</summary>
                    <div className="mt-2 flex flex-col gap-2">{decisions.withdrawn.map(decisionCard)}</div>
                  </details>
                ) : null}
              </Section>

              <Section
                title={behavior && behavior.total > 0 ? `Behavior changes · ${behavior.total}` : "Behavior changes"}
                testId="brief-behavior"
                action={behavior && behavior.progress.critical > 0 ? <Badge tone="p0">{`${behavior.progress.critical} critical`}</Badge> : undefined}
              >
                {behavior ? (
                  <BehaviorChanges
                    behavior={behavior}
                    revisionId={projection.revision_id}
                    onLoadTarget={historical ? undefined : props.onLoadTarget}
                    onJudge={historical ? undefined : props.onJudge}
                    onNarrate={historical ? undefined : props.onNarrate}
                    onManualCheck={historical ? undefined : props.onManualCheck}
                  />
                ) : null}
                {!behavior || (behavior.total === 0 && (behavior.agent_actions?.length ?? 0) === 0) ? (
                  <p className="text-caption text-muted-foreground" data-testid="brief-no-behavior">
                    No before and after run is recorded on this revision, so no behavior change was observed. That is not proof that behavior is unchanged.
                  </p>
                ) : null}
              </Section>

              <Section title={blindSpots.counts.known > 0 ? `Blind spots · ${plural(blindSpots.counts.known, "area")} to check` : "Blind spots"} testId="brief-blind-spots">
                <BlindSpots
                  key={projection.revision_id}
                  model={blindSpots}
                  onOpenTarget={openTarget}
                  // A part's evidence is on its Focus card; the Review's evidence is on the Overview.
                  onInspectEvidence={(targetId) => { if (targetId) openTarget(targetId); else back(); }}
                  onAsk={canAsk ? ask : undefined}
                  renderDecision={(key) => {
                    const decision = projection.decisions.find((row) => row.key === key);
                    return decision ? decisionCard(decision) : null;
                  }}
                />
              </Section>

              <ProjectHistory apiReviewId={apiReviewId} revisionNumber={overview.revision.revision_number} />

              {workspaceImpact ? (
                <Section title="Workspace impact" testId="brief-workspace-impact">
                  <BriefWorkspaceImpact key={projection.revision_id} impact={workspaceImpact} />
                </Section>
              ) : null}
            </div>
          )}
        </div>
        {props.askPanel ? (
          <aside data-review-rail="ask" className={`review-secondary-rail ${FLOW_RAIL_CLASS} w-[min(32vw,400px)] shrink-0 border-l border-border bg-background`}>
            {props.askPanel}
          </aside>
        ) : null}
      </div>
    </>
  );
}
