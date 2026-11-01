import { useId, useMemo, useState } from "react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Select, SelectItem } from "../../components/ui/select";
import type { CodeTarget } from "../../lib/codeLinks";
import { investigationContext, type InvestigationContext, type InvestigationInput } from "../investigation/investigationContext";
import type { EvidenceRow, ReviewProjection, ReviewTarget } from "../types";
import {
  ORIGIN_LABEL,
  assumptionsModel,
  rowsForTarget,
  type AssumptionFinding,
  type AssumptionGroup,
  type AssumptionOrigin,
  type AssumptionRow,
  type AssumptionSupport,
  type AssumptionsModel,
} from "./assumptionsModel";
import "./assumptions.css";

/**
 * N04 hook point: a host with a real Challenge flow passes this and each row
 * offers "Challenge". Without it no Challenge control renders.
 */
export type AssumptionChallenge = (row: AssumptionRow, context: InvestigationContext) => void;

export interface AssumptionsProps {
  projection: ReviewProjection;
  targets: readonly ReviewTarget[];
  /** Recorded checks on this exact Snapshot; a relied-on check counts only when it is recorded here. */
  checks?: readonly EvidenceRow[] | null;
  revision?: InvestigationInput["revision"];
  /** Start from a Review finding: only the assumptions on the code it sits in. */
  finding?: AssumptionFinding | null;
  onOpenTarget: (targetId: string) => void;
  /** The Reader's code-location opener; without it consumer sites outside the patch are listed, not linked. */
  onOpenCodeLocation?: (location: CodeTarget) => void;
  /** Opens Ask scoped to the row's changed code. */
  onAsk?: (targetId: string | null) => void;
  onChallenge?: AssumptionChallenge;
  /** Start focused on one changed target (the Context drawer's Investigate tab); the control can still widen it. */
  initialFocus?: string;
}

/** Rows shown per kind before an explicit "Show all". */
const ROW_LIMIT = 8;

const ORIGIN_TONE: Record<AssumptionOrigin, "neutral" | "secondary" | "ai"> = {
  deterministic: "neutral",
  author_claim: "secondary",
  model_suggested: "ai",
};

const RELATION: Record<AssumptionSupport["relation"], string> = {
  claim_evidence: "linked by the claim",
  relied_on: "relied on by the author",
};

function status(value: string): string {
  return value === "NOT_RUN" ? "not run" : value === "UNKNOWN" ? "no result" : value.toLowerCase();
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

function StateBadge({ row }: { row: AssumptionRow }) {
  if (row.state === "checked") {
    return <Badge tone="success" className="as-badge" data-state="checked">{`Checked by ${row.checkedBy.join(", ")}`}</Badge>;
  }
  if (row.state === "contradicted") {
    return <Badge tone="destructive" className="as-badge" data-state="contradicted">{`Contradicted by ${row.contradictedBy.join(", ")}`}</Badge>;
  }
  return <Badge tone="neutral" className="as-badge" data-state="unverified">Unverified</Badge>;
}

function SupportLine({ support }: { support: AssumptionSupport }) {
  const notes = [RELATION[support.relation]];
  if (support.mapping === "inferred") notes.push("relevance inferred, not counted");
  if (!support.current) notes.push(support.snapshot_number !== null ? `from Snapshot ${support.snapshot_number}, not re-run on this one` : "not recorded on this Snapshot");
  return (
    <li data-testid="assumption-support" data-current={support.current ? "true" : "false"}>
      <span className="as-code">{support.key}</span>
      {` — ${status(support.status)} · ${notes.join(" · ")}`}
    </li>
  );
}

function Row({ row, model, props }: { row: AssumptionRow; model: AssumptionsModel; props: AssumptionsProps }) {
  const titleId = useId();
  const firstTarget = row.sources[0]?.target_id ?? null;
  return (
    <li className="as-row" data-testid="assumption-row" data-kind={row.kind} data-origin={row.origins[0]} data-state={row.state}>
      <div className="as-row-head">
        <div id={titleId} className="as-row-title">{row.hypothesis}</div>
        <div className="as-row-tags">
          {row.origins.map((origin) => (
            <Badge key={origin} tone={ORIGIN_TONE[origin]} className="as-badge" data-origin={origin}>{ORIGIN_LABEL[origin]}</Badge>
          ))}
          <StateBadge row={row} />
        </div>
      </div>
      {row.detail && <div className="as-detail">{row.detail}</div>}
      <div className="as-meta">
        {`From: ${row.signal}`}
        {row.duplicates > 0 && ` · ${plural(row.duplicates, "repeated record")} folded in`}
        {row.origins[0] === "model_suggested" && " · a suggestion to investigate, not a fact"}
      </div>

      <div className="as-block">
        <div className="as-block-label">Affected code</div>
        {row.sources.length > 0 ? (
          <ul className="as-links">
            {row.sources.map((source) => (
              <li key={source.target_id}>
                <Button
                  variant="link"
                  size="compact"
                  type="button"
                  className="as-code"
                  aria-describedby={titleId}
                  data-testid="assumption-source"
                  onClick={() => props.onOpenTarget(source.target_id)}
                >
                  {`${source.path}:${source.start_line}–${source.end_line}${source.symbol ? ` · ${source.symbol}` : ""}`}
                </Button>
                {source.changed_since_review && <span className="as-note"> · changed since review</span>}
              </li>
            ))}
          </ul>
        ) : (
          <div className="as-note">Review level · not tied to a loaded changed target</div>
        )}
        {(row.sourcesOmitted > 0 || row.unresolvedTargets > 0) && (
          <div className="as-note">
            {[row.sourcesOmitted ? `${row.sourcesOmitted} more` : "", row.unresolvedTargets ? `${row.unresolvedTargets} not loaded in this view` : ""].filter(Boolean).join(" · ")}
          </div>
        )}
      </div>

      {row.consumers.length > 0 && (
        <div className="as-block">
          <div className="as-block-label">Related consumers</div>
          <ul className="as-links">
            {row.consumers.map((site) => {
              const where = site.line > 0 ? `${site.path}:${site.line}` : site.path;
              const label = site.symbol ? `${where} · ${site.symbol}` : where;
              return (
                <li key={`${where}:${site.symbol}:${site.impact_kind}`} data-testid="assumption-consumer">
                  {props.onOpenCodeLocation ? (
                    <Button
                      variant="link"
                      size="compact"
                      type="button"
                      className="as-code"
                      aria-describedby={titleId}
                      onClick={() => props.onOpenCodeLocation!({ path: site.path, line: Math.max(1, site.line), endLine: Math.max(1, site.line) })}
                    >
                      {label}
                    </Button>
                  ) : <span className="as-code">{label}</span>}
                  <span className="as-note">{` · ${site.exactness === "snapshot" ? "this Snapshot" : "working index"}${site.uncertainty ? ` · candidate: ${site.uncertainty}` : ""}`}</span>
                </li>
              );
            })}
          </ul>
          {row.consumersOmitted > 0 && <div className="as-note">{`${row.consumersOmitted} more`}</div>}
        </div>
      )}

      <div className="as-block">
        <div className="as-block-label">Checks</div>
        {row.support.length > 0 && <ul className="as-lines">{row.support.map((support, index) => <SupportLine key={`${support.key}:${index}`} support={support} />)}</ul>}
        {row.coverage.length > 0 && (
          <ul className="as-lines" aria-label="Related checks, not proof">
            {row.coverage.map((coverage) => <li key={coverage.label} data-testid="assumption-coverage">{`Related, not proof: ${coverage.label}`}</li>)}
          </ul>
        )}
        {row.support.length === 0 && row.coverage.length === 0 && <div className="as-note">No recorded check is linked to this code.</div>}
      </div>

      {(props.onAsk || props.onChallenge) && (
        <div className="as-actions">
          {props.onAsk && (
            <Button variant="ghost" size="compact" type="button" aria-describedby={titleId} onClick={() => props.onAsk?.(firstTarget)}>
              Ask
            </Button>
          )}
          {props.onChallenge && (
            <Button variant="ghost" size="compact" type="button" aria-describedby={titleId} onClick={() => props.onChallenge?.(row, model.context)}>
              Challenge
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

function Group({ group, rows, model, props }: { group: AssumptionGroup; rows: AssumptionRow[]; model: AssumptionsModel; props: AssumptionsProps }) {
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.slice(0, ROW_LIMIT);
  const headingId = useId();
  return (
    <section className="as-group" aria-labelledby={headingId} data-testid={`assumption-group-${group.kind}`}>
      <div className="as-group-head">
        <h4 id={headingId} className="as-group-label">{group.label}</h4>
        <span className="as-count">{rows.length}</span>
      </div>
      <p className="as-note">{`If it does not hold: ${group.ifFails}`}</p>
      <ul className="as-rows">
        {shown.map((row) => <Row key={row.id} row={row} model={model} props={props} />)}
      </ul>
      {!all && rows.length > ROW_LIMIT && (
        <Button variant="ghost" size="compact" type="button" className="self-start" onClick={() => setAll(true)}>{`Show all ${rows.length}`}</Button>
      )}
      {group.omitted > 0 && <p className="as-note">{`${group.omitted} more past the display bound.`}</p>}
    </section>
  );
}

function targetName(target: ReviewTarget): string {
  return target.symbol ? `${target.path} · ${target.symbol}` : target.path;
}

/** The Assumption Explorer body: hypotheses this change relies on, grouped by kind, each with its origin and recorded state. */
export default function Assumptions(props: AssumptionsProps) {
  const { projection, targets, checks, finding, revision } = props;
  const model = useMemo(
    () => assumptionsModel({ context: investigationContext({ projection, targets, checks, revision }), projection, targets, finding }),
    [checks, finding, projection, revision, targets],
  );
  const identity = model.snapshotIdentity;
  // Focus is ephemeral view state and belongs to one Snapshot; a new Snapshot starts from all code.
  const initialFocus = props.initialFocus ?? "";
  const [focus, setFocus] = useState({ identity, targetId: initialFocus });
  const focusId = focus.identity === identity ? focus.targetId : initialFocus;
  const focusable = useMemo(() => {
    const ids = new Set(model.groups.flatMap((group) => group.rows.flatMap((row) => row.targetIds)));
    // A target asked for by the host stays choosable even when nothing is on it.
    if (initialFocus) ids.add(initialFocus);
    return targets.filter((target) => ids.has(target.target_id));
  }, [initialFocus, model, targets]);
  const groups = model.groups
    .map((group) => ({ group, rows: rowsForTarget(group, focusId || null) }))
    .filter(({ rows }) => rows.length > 0);
  const { looked } = model;
  const lookedAt = [
    plural(looked.claims, "claim"),
    plural(looked.declaredAssumptions, "declared assumption"),
    plural(looked.statedLimits, "stated limit"),
    plural(looked.aiQuestions, "AI question"),
    plural(looked.behaviorChanges, "behavior change"),
    plural(looked.consumerSites, "consumer site"),
  ].join(", ");

  return (
    <div className="as-root" data-testid="assumptions" data-snapshot={identity}>
      <p className="as-intro">
        What this change relies on, and whether a recorded check demonstrates it. Unverified means no linked check on this Snapshot shows it holds; AI suggestions are leads to investigate, not facts.
      </p>
      {model.scope.kind !== "review" && <p className="as-note" data-testid="assumptions-scope">{`Scoped to ${model.scope.label}`}</p>}
      {model.counts.rows > 0 && (
        <ul className="as-legend" aria-label="Assumption states" data-testid="assumptions-counts">
          <li>{plural(model.counts.rows, "assumption")}</li>
          <li>{`${model.counts.unverified} unverified`}</li>
          {model.counts.checked > 0 && <li>{`${model.counts.checked} checked`}</li>}
          {model.counts.contradicted > 0 && <li>{`${model.counts.contradicted} contradicted`}</li>}
          {model.counts.duplicatesMerged > 0 && <li>{`${plural(model.counts.duplicatesMerged, "repeated record")} folded in`}</li>}
        </ul>
      )}
      {focusable.length > 1 && (
        <div className="as-controls">
          <Select
            uiSize="xs"
            aria-label="Focus on changed code"
            searchable={focusable.length > 8}
            value={focusId || "__all__"}
            onValueChange={(value) => setFocus({ identity, targetId: value === "__all__" ? "" : value })}
            className="as-select"
          >
            <SelectItem value="__all__">All changed code</SelectItem>
            {focusable.map((target) => <SelectItem key={target.target_id} value={target.target_id}>{targetName(target)}</SelectItem>)}
          </Select>
        </div>
      )}
      {model.counts.rows === 0 ? (
        <p className="as-note" data-testid="assumptions-empty">
          {`No precondition, invariant, idempotency, concurrency, authorization, data-shape, failure/retry or side-effect assumption in what was looked at: ${lookedAt}. That is not the same as safe.`}
        </p>
      ) : groups.length === 0 && focusId ? (
        <p className="as-note" data-testid="assumptions-focus-empty">
          {`None of the ${plural(model.counts.rows, "assumption")} found is on this code. That is not the same as safe: choose All changed code to see them.`}
        </p>
      ) : (
        groups.map(({ group, rows }) => <Group key={`${identity}:${group.kind}`} group={group} rows={rows} model={model} props={props} />)
      )}
      <details className="as-measure" data-testid="assumptions-measurement">
        <summary className="as-summary">What was and was not looked at</summary>
        <ul className="as-lines">
          <li>{`Looked at: ${lookedAt}.`}</li>
          <li>Kinds come from the record's own words; a kind is a label, not a finding.</li>
          {model.notMeasured.map((line) => <li key={line}>{`Not measured: ${line}`}</li>)}
        </ul>
      </details>
    </div>
  );
}
