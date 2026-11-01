import { useCallback, useId, useMemo, useRef, useState, type ReactNode } from "react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import type { CodeTarget } from "../../lib/codeLinks";
import Assumptions from "../assumptions/Assumptions";
import type { BlindSpotItem } from "../blindSpots/blindSpotsModel";
import Challenge from "../challenge/Challenge";
import {
  OUTCOME_LABEL,
  claimFromAssumption,
  claimFromBlindSpot,
  claimFromWhatIf,
  observationFromRun,
  type ChallengeClaim,
} from "../challenge/challengeModel";
import { useHeldChallenge } from "../challenge/heldChallenge";
import type { InvestigationInput } from "../investigation/investigationContext";
import type { ReviewMapLocation } from "../reviewMapLocation";
import type { EvidenceRow, ReviewMapGraph, ReviewProjection, ReviewTarget } from "../types";
import WhatIf from "../whatIf/WhatIf";
import {
  INVESTIGATE_STEPS,
  blindSpotNote,
  holdClaim,
  investigateModel,
  xrayLocationFor,
  type InvestigateModel,
  type InvestigateStep,
} from "./investigateModel";
import "./investigate.css";

/** What the Reader already holds for the Snapshot shown now; the tab adds no fetch and no authority of its own. */
export interface InvestigateHost {
  projection: ReviewProjection | null;
  targets: readonly ReviewTarget[];
  checks?: readonly EvidenceRow[] | null;
  revision?: InvestigationInput["revision"];
  /** The Reader's cached Map graph read for this Snapshot; absent when the Snapshot has none. */
  loadMapGraph?: () => Promise<ReviewMapGraph | null>;
  /** Opens the Map at *location*, keeping this drawer on the backtrail. */
  onWalk: (location: ReviewMapLocation) => void;
  onOpenTarget: (targetId: string) => void;
  onOpenCodeLocation?: (location: CodeTarget) => void;
  /** Opens the decision where it is recorded (the Brief); absent when there is no such path. */
  onOpenDecision?: (decisionKey: string) => void;
  /** The session may request a check: the Review's own read-only and historical state, nothing kept here. */
  canRun: boolean;
}

export interface InvestigateProps extends InvestigateHost {
  target: ReviewTarget;
  /** This target's checks, in the same drawer. */
  onInspectEvidence: () => void;
}

function Step({ step, index, title, children }: { step: InvestigateStep; index: number; title: string; children: ReactNode }) {
  const headingId = useId();
  return (
    <section className="iv-step" aria-labelledby={headingId} data-testid={`investigate-${step}`} data-step={step}>
      <h3 id={headingId} className="iv-step-title" tabIndex={-1}>
        <span className="iv-step-index" aria-hidden="true">{index}</span>
        {title}
      </h3>
      <div className="iv-step-body">{children}</div>
    </section>
  );
}

function Facts({ model, onOpenSource }: { model: InvestigateModel; onOpenSource: () => void }) {
  return (
    <div className="iv-facts" data-testid="investigate-facts">
      <dl className="iv-fact-list">
        {model.facts.map((fact) => (
          <div key={fact.label} className="iv-fact">
            <dt>{fact.label}</dt>
            <dd>{fact.value}</dd>
          </div>
        ))}
      </dl>
      <Button variant="outline" size="compact" type="button" className="self-start" onClick={onOpenSource}>Open source</Button>
    </div>
  );
}

function Walk({ model, props }: { model: InvestigateModel; props: InvestigateProps }) {
  const [state, setState] = useState<{ path: string; busy: boolean; note: string }>({ path: "", busy: false, note: "" });
  const shown = state.path === model.target.path ? state : { path: "", busy: false, note: "" };
  const load = props.loadMapGraph;
  if (!model.walk.available || !load) {
    return <p className="iv-note" data-testid="investigate-no-walk">{model.walk.reason || "This Snapshot has no Map graph to walk, so there is no X-Ray from here."}</p>;
  }
  const path = model.target.path;
  const walk = async () => {
    setState({ path, busy: true, note: "" });
    try {
      const location = xrayLocationFor(await load(), model.target);
      if (location) {
        setState({ path, busy: false, note: "" });
        props.onWalk(location);
      } else {
        setState({ path, busy: false, note: `No node on this Snapshot's Map is ${path}, so there is no X-Ray rooted here.` });
      }
    } catch (reason) {
      setState({ path, busy: false, note: `The Map could not be loaded: ${reason instanceof Error ? reason.message : String(reason)}` });
    }
  };
  return (
    <>
      <p className="iv-note">What this file reaches before and after the change, walked on the Map from its node. Possible paths, not an execution order, unless a recorded run traced them.</p>
      <Button variant="outline" size="compact" type="button" className="self-start" disabled={shown.busy} onClick={() => void walk()}>
        {shown.busy ? "Opening the Map…" : "Walk from here"}
      </Button>
      {shown.note && <p className="iv-note" role="status" data-testid="investigate-walk-note">{shown.note}</p>}
    </>
  );
}

function BlindSpotRows({
  model,
  props,
  claimFor,
  onChallenge,
}: {
  model: InvestigateModel;
  props: InvestigateProps;
  /** The item's claim, or null when the item is a gap rather than a claim. */
  claimFor: (item: BlindSpotItem) => ChallengeClaim | null;
  onChallenge: (claim: ChallengeClaim | null) => void;
}) {
  const note = blindSpotNote(model);
  return (
    <>
      {model.blindSpots.items.length > 0 && (
        <ul className="iv-rows">
          {model.blindSpots.items.map((item) => (
            <li key={item.id} className="iv-row" data-testid="investigate-blind-spot">
              <div className="iv-row-title">
                <Badge tone="warning" className="iv-badge">Known blind spot</Badge>
                <span>{item.title}</span>
              </div>
              {item.detail && <div className="iv-row-detail">{item.detail}</div>}
              <div className="iv-row-actions">
                {item.actions.includes("see_evidence") && (
                  <Button variant="ghost" size="compact" type="button" onClick={props.onInspectEvidence}>See evidence</Button>
                )}
                {claimFor(item) && (
                  <Button variant="ghost" size="compact" type="button" onClick={() => onChallenge(claimFor(item))}>Challenge</Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {note && <p className="iv-note" data-testid="investigate-blind-spot-note">{note}</p>}
    </>
  );
}

/**
 * The Context drawer's Investigate tab: one changed target, investigated in
 * order -- walk it on the Map, read what it relies on, ask what if, see what is
 * not verified, challenge any of those, and reach the human decision on it.
 */
export default function Investigate(props: InvestigateProps) {
  const { projection, targets, target, checks, revision } = props;
  // Keyed on whether a Map exists, not on the loader's identity: hosts pass a fresh closure per render.
  const map = Boolean(props.loadMapGraph);
  const model = useMemo(
    () => investigateModel({ projection, targets, target, checks, revision, map }),
    [checks, map, projection, revision, target, targets],
  );
  const rootRef = useRef<HTMLDivElement>(null);
  const [held, setHeld] = useHeldChallenge(projection?.review_id ?? "");
  const [challengeOpen, setChallengeOpen] = useState(false);
  const open = useCallback((claim: ChallengeClaim | null) => {
    if (!claim) return;
    setHeld(holdClaim(held, claim));
    setChallengeOpen(true);
  }, [held, setHeld]);
  const goTo = (step: InvestigateStep) => {
    const heading = rootRef.current?.querySelector<HTMLElement>(`[data-step="${step}"] .iv-step-title`);
    heading?.scrollIntoView({ block: "start" });
    heading?.focus({ preventScroll: true });
  };
  const openSource = () => props.onOpenTarget(target.target_id);

  if (!projection || !model.analysed || !model.context || !model.reviewContext) {
    return (
      <div className="iv-root" data-testid="investigate" data-analysed="false">
        <p className="iv-note" data-testid="investigate-unanalysed">
          The Review&apos;s analysis of this Snapshot is not loaded, so only the source and its recorded facts are shown. Nothing here is a finding.
        </p>
        <Facts model={model} onOpenSource={openSource} />
      </div>
    );
  }
  const reviewContext = model.reviewContext;
  const snapshot = model.context.snapshot_identity;
  const heldStale = held ? held.claim.snapshotIdentity !== snapshot : false;
  const heldResult = held?.run ? observationFromRun(held.run, held.claim, snapshot) : null;
  const surfaceKey = `${snapshot}|${target.target_id}`;

  return (
    <div ref={rootRef} className="iv-root" data-testid="investigate" data-analysed="true" data-snapshot={snapshot} data-target={target.target_id}>
      <nav className="iv-nav" aria-label="Investigation steps">
        <ol>
          {INVESTIGATE_STEPS.map((step, index) => (
            <li key={step.key}>
              <Button variant="ghost" size="compact" type="button" onClick={() => goTo(step.key)}>{`${index + 1} ${step.label}`}</Button>
            </li>
          ))}
        </ol>
      </nav>
      <Facts model={model} onOpenSource={openSource} />

      <Step step="xray" index={1} title="X-Ray before and after">
        <Walk model={model} props={props} />
      </Step>

      <Step step="assumptions" index={2} title="Assumptions on this code">
        <Assumptions
          key={surfaceKey}
          projection={projection}
          targets={targets}
          checks={checks}
          revision={revision}
          initialFocus={target.target_id}
          onOpenTarget={props.onOpenTarget}
          onOpenCodeLocation={props.onOpenCodeLocation}
          onChallenge={(row, context) => open(claimFromAssumption(row, context.snapshot_identity))}
        />
      </Step>

      <Step step="what_if" index={3} title="What if">
        {!model.whatIf.available ? (
          <p className="iv-note" data-testid="investigate-what-if-unavailable">{model.whatIf.reason}</p>
        ) : (
          <>
            {model.whatIf.reason && <p className="iv-note">{model.whatIf.reason}</p>}
            <WhatIf
              key={surfaceKey}
              projection={projection}
              targets={targets}
              checks={checks}
              revision={revision}
              initialTargetId={model.whatIf.targetId ?? undefined}
              onOpenTarget={props.onOpenTarget}
              onOpenCodeLocation={props.onOpenCodeLocation}
              onChallenge={(row, whatIf, label) => open(claimFromWhatIf(row, whatIf, label))}
            />
          </>
        )}
      </Step>

      <Step step="blind_spots" index={4} title="Blind spots on this code">
        <BlindSpotRows
          model={model}
          props={props}
          claimFor={(item) => claimFromBlindSpot(item, projection, targets, reviewContext.snapshot_identity)}
          onChallenge={open}
        />
      </Step>

      <Step step="challenge" index={5} title="Challenge">
        {held && !challengeOpen ? (
          <div className="iv-held" data-testid="investigate-held" data-stale={heldStale ? "true" : "false"}>
            {heldStale && <Badge tone="warning">Stale</Badge>}
            <span className="iv-held-text" title={held.claim.text}>{held.claim.text}</span>
            <span className="iv-note">{heldResult ? OUTCOME_LABEL[heldResult.outcome] : "Not run"}</span>
            <Button variant="outline" size="compact" type="button" onClick={() => setChallengeOpen(true)}>Reopen</Button>
            <Button variant="ghost" size="compact" type="button" onClick={() => setHeld(null)}>Dismiss</Button>
          </div>
        ) : (
          <p className="iv-note">Challenge an assumption, a What-if row or a blind spot above to see what supports it and what would run. The Brief holds the same claim.</p>
        )}
      </Step>

      <Step step="decision" index={6} title="Human decision">
        {model.decisions.length > 0 ? (
          <ul className="iv-rows">
            {model.decisions.map((decision) => (
              <li key={decision.key} className="iv-row" data-testid="investigate-decision">
                <div className="iv-row-title">{decision.question || decision.key}</div>
                {decision.reason_human_required && <div className="iv-row-detail">{decision.reason_human_required}</div>}
                {props.onOpenDecision && (
                  <div className="iv-row-actions">
                    <Button variant="outline" size="compact" type="button" onClick={() => props.onOpenDecision?.(decision.key)}>Decide in the Brief</Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="iv-note" data-testid="investigate-no-decision">No open human decision names this code. Your judgment on it is the review mark: reviewed, or needs changes.</p>
        )}
      </Step>

      {held && challengeOpen && (
        <Challenge
          claim={held.claim}
          projection={projection}
          targets={targets}
          checks={checks}
          revision={revision}
          canRun={props.canRun && !projection.historical}
          run={held.run}
          onRun={(run) => setHeld({ claim: held.claim, run })}
          onOpenTarget={props.onOpenTarget}
          onOpenCodeLocation={props.onOpenCodeLocation}
          onClose={() => setChallengeOpen(false)}
        />
      )}
    </div>
  );
}
