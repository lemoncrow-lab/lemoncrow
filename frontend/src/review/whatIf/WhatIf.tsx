import { useMemo, useState } from "react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Select, SelectItem } from "../../components/ui/select";
import { ToggleGroup } from "../../components/ui/toggle-group";
import type { CodeTarget } from "../../lib/codeLinks";
import { investigationContext, snapshotIdentity, type InvestigationInput } from "../investigation/investigationContext";
import type { EvidenceRow, ReviewProjection, ReviewTarget } from "../types";
import { scenariosFor, whatIfModel, whatIfTargets, type WhatIfModel, type WhatIfRow, type WhatIfScenarioKey } from "./whatIfModel";
import "./whatIf.css";

export interface WhatIfProps {
  projection: ReviewProjection;
  targets: readonly ReviewTarget[];
  checks?: readonly EvidenceRow[] | null;
  revision?: InvestigationInput["revision"];
  onOpenTarget: (targetId: string) => void;
  /** The Reader's code-location opener (in-patch target, else Snapshot source); without it rows outside the patch open the changed code they depend on. */
  onOpenCodeLocation?: (location: CodeTarget) => void;
  /** N04: challenge one row's claim; without it no Challenge control renders. */
  onChallenge?: (row: WhatIfRow, model: WhatIfModel, targetLabel: string) => void;
  /** Start with this changed target chosen (the Context drawer's Investigate tab); still per Snapshot. */
  initialTargetId?: string;
}

/** Rows shown before an explicit "Show all". */
const ROW_LIMIT = 25;

const EXACTNESS: Record<string, string> = { snapshot: "Snapshot", current_index: "Working index" };

function targetName(target: ReviewTarget): string {
  return target.symbol ? `${target.path} · ${target.symbol}` : target.path;
}

function Row({ row, model, targetLabel, props }: { row: WhatIfRow; model: WhatIfModel; targetLabel: string; props: WhatIfProps }) {
  const where = row.line > 0 ? `${row.path}:${row.line}` : row.path;
  const open = row.openTargetId
    ? { label: "Open source", run: () => props.onOpenTarget(row.openTargetId!) }
    : props.onOpenCodeLocation
      ? { label: "Open source", run: () => props.onOpenCodeLocation!({ path: row.path, line: Math.max(1, row.line), endLine: Math.max(1, row.line) }) }
      : { label: "Open changed code", run: () => props.onOpenTarget(row.fromTargetId) };
  return (
    <li className="wi-row" data-testid="what-if-row" data-origin={row.origin} data-unchanged={row.unchanged ? "true" : "false"}>
      <div className="wi-row-title">{row.symbol ? `${where} · ${row.symbol}` : where}</div>
      <div className="wi-row-tags">
        <Badge tone="neutral" className="wi-tag" data-origin={row.origin}>
          {row.origin === "simulated" ? "Simulated effect" : "Actual change in this PR"}
        </Badge>
        <Badge tone="secondary" className="wi-tag">{row.unchanged ? "Unchanged by this PR" : "Changed in this PR"}</Badge>
        <Badge tone="secondary" className="wi-tag" data-testid="what-if-exactness">{EXACTNESS[row.exactness] ?? row.exactness}</Badge>
        {row.kind === "test" && <Badge tone="secondary" className="wi-tag">Test</Badge>}
        {row.depth > 1 && <Badge tone="secondary" className="wi-tag">{`${row.depth} hops away`}</Badge>}
      </div>
      <div className="wi-row-detail">{row.reason}</div>
      {row.uncertainty && <div className="wi-row-detail wi-candidate">{`Candidate: ${row.uncertainty}`}</div>}
      <div className="wi-row-detail">
        {row.coverage.length > 0
          ? `Checks naming it: ${row.coverage.map((check) => `${check.key} (${check.status.toLowerCase().replace("_", " ")}, matched by name)`).join("; ")}`
          : "No recorded check names it."}
      </div>
      <div className="wi-row-actions">
        <Button variant="outline" size="compact" type="button" onClick={open.run}>{open.label}</Button>
        {props.onChallenge && (
          <Button variant="ghost" size="compact" type="button" onClick={() => props.onChallenge?.(row, model, targetLabel)}>Challenge</Button>
        )}
      </div>
    </li>
  );
}

function Overlay({ model, target, props, onClear }: { model: WhatIfModel; target: ReviewTarget; props: WhatIfProps; onClear: () => void }) {
  const [all, setAll] = useState(false);
  const unchanged = model.rows.filter((row) => row.unchanged).length;
  const shown = all ? model.rows : model.rows.slice(0, ROW_LIMIT);
  return (
    <div className="wi-overlay" data-testid="what-if-overlay" data-snapshot={model.snapshotIdentity} aria-label="Hypothetical impact" role="group">
      <div className="wi-overlay-head">
        <Badge tone="info" className="wi-tag">Hypothetical</Badge>
        <span className="wi-overlay-title">{`If ${target.symbol || target.path}: ${model.scenario.label.toLowerCase()}`}</span>
        <Button variant="ghost" size="compact" type="button" className="ml-auto" onClick={onClear}>Clear</Button>
      </div>
      <p className="wi-note" data-testid="what-if-summary">
        {model.rows.length > 0
          ? `${model.rows.length} reachable, ${unchanged} unchanged by this PR${model.omitted > 0 ? `, ${model.omitted} more not listed` : ""}. Not a fact about the current code.`
          : model.complete
            ? "No callers or consumers are recorded for it in this Snapshot's impact pass."
            : "No known callers. That is not the same as no callers: see what is unknown below."}
      </p>
      {shown.length > 0 && (
        <ul className="wi-rows">
          {shown.map((row) => <Row key={row.id} row={row} model={model} targetLabel={target.symbol || target.path} props={props} />)}
        </ul>
      )}
      {!all && model.rows.length > ROW_LIMIT && (
        <Button variant="ghost" size="compact" type="button" className="self-start" onClick={() => setAll(true)}>{`Show all ${model.rows.length}`}</Button>
      )}
      <p className="wi-note">
        {model.targetChecks.length > 0
          ? `Checks linked to ${target.symbol || target.path}: ${model.targetChecks.map((check) => `${check.key} (${check.status.toLowerCase().replace("_", " ")})`).join("; ")}`
          : `No recorded check is linked to ${target.symbol || target.path}.`}
      </p>
      <div className="wi-unknowns" data-testid="what-if-unknowns">
        <div className="wi-unknowns-title">Unknown to this overlay</div>
        <ul className="wi-rows">
          {model.gaps.map((gap) => (
            <li key={gap.key} className="wi-gap" data-testid="what-if-gap" data-gap={gap.key}>
              <Badge tone="neutral" className="wi-tag wi-unknown">Unknown</Badge>
              <span className="wi-gap-title">{gap.title}</span>
              <span className="wi-row-detail">{gap.detail}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

interface Choice {
  identity: string;
  targetId: string;
  scenario: WhatIfScenarioKey | "";
}

/** The Brief's What if section: pick a changed target and a hypothetical, read the bounded reach. */
export default function WhatIf(props: WhatIfProps) {
  const { projection, targets, checks } = props;
  const identity = snapshotIdentity(projection.review_id, projection.revision_id);
  const candidates = useMemo(() => whatIfTargets(targets), [targets]);
  const initialTargetId = props.initialTargetId ?? "";
  const [stored, setChoice] = useState<Choice>({ identity, targetId: initialTargetId, scenario: "" });
  // An overlay belongs to the Snapshot it was asked on; a new Snapshot drops it.
  const choice: Choice = stored.identity === identity ? stored : { identity, targetId: initialTargetId, scenario: "" };
  const target = candidates.find((row) => row.target_id === choice.targetId) ?? null;
  const scenarios = target ? scenariosFor(target) : [];
  const scenario = scenarios.find((row) => row.key === choice.scenario) ?? null;
  const context = useMemo(
    () => investigationContext({ projection, targets, checks, revision: props.revision }),
    [checks, projection, props.revision, targets],
  );
  const model = useMemo(() => {
    if (!target || !scenario) return null;
    return whatIfModel({ context, projection, targets, targetId: target.target_id, scenario: scenario.key });
  }, [context, projection, scenario, target, targets]);

  if (candidates.length === 0) return null;
  return (
    <div className="wi-root" data-testid="what-if" data-snapshot={identity}>
      <p className="wi-note">
        How far could a change to one changed target reach? A read-only hypothetical over this Snapshot's impact data: nothing is edited, and simulated rows are not facts about the current code.
      </p>
      <div className="wi-controls">
        <Select
          uiSize="xs"
          aria-label="Changed target"
          placeholder="Choose a changed target"
          searchable={candidates.length > 8}
          value={choice.targetId}
          onValueChange={(targetId) => setChoice({ identity, targetId, scenario: "" })}
          className="wi-select"
        >
          {candidates.map((row) => <SelectItem key={row.target_id} value={row.target_id}>{targetName(row)}</SelectItem>)}
        </Select>
        {target && (
          <ToggleGroup
            aria-label="Hypothetical change"
            value={choice.scenario}
            onChange={(key) => setChoice({ identity, targetId: target.target_id, scenario: key as WhatIfScenarioKey })}
            options={scenarios.map((row) => ({ value: row.key, label: row.label }))}
          />
        )}
      </div>
      {model && target && (
        <Overlay
          key={`${model.snapshotIdentity}|${model.targetId}|${model.scenario.key}`}
          model={model}
          target={target}
          props={props}
          onClear={() => setChoice({ identity, targetId: target.target_id, scenario: "" })}
        />
      )}
    </div>
  );
}
