import { useId, useState, type ReactNode } from "react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import type { InvestigationContext } from "../investigation/investigationContext";
import type { BlindSpotCategory, BlindSpotItem, BlindSpotState, BlindSpotsModel } from "./blindSpotsModel";
import "./blindSpots.css";

/**
 * N04 hook point: a host that has a real Challenge flow passes this and each
 * item it can challenge (`canChallenge`) offers "Challenge". Without it no
 * Challenge control renders.
 */
export type BlindSpotChallenge = (item: BlindSpotItem, context: InvestigationContext) => void;

export interface BlindSpotsProps {
  model: BlindSpotsModel;
  onOpenTarget: (targetId: string) => void;
  onInspectEvidence: (targetId: string | null) => void;
  onAsk?: (targetId: string | null) => void;
  /** The host's own decision card for a decision key; recording goes through its existing write path. */
  renderDecision?: (decisionKey: string) => ReactNode;
  onChallenge?: BlindSpotChallenge;
  /** Which items have a claim and a source to challenge; without it every item does. */
  canChallenge?: (item: BlindSpotItem) => boolean;
}

const STATE_LABEL: Record<BlindSpotState, string> = {
  known: "Known blind spot",
  nothing_open: "Nothing open",
  not_measured: "Not measured",
  not_applicable: "Not applicable",
};

const STATE_TONE: Record<BlindSpotState, "warning" | "neutral" | "secondary"> = {
  known: "warning",
  nothing_open: "neutral",
  not_measured: "neutral",
  not_applicable: "secondary",
};

/** Above this many items a category groups them by file. */
const GROUP_AT = 8;
/** Items shown per group before an explicit "Show all". */
const GROUP_LIMIT = 25;

function StateBadge({ state }: { state: BlindSpotState }) {
  return (
    <Badge tone={STATE_TONE[state]} className="bs-badge" data-state={state}>
      {STATE_LABEL[state]}
    </Badge>
  );
}

function ItemRow({ item, props }: { item: BlindSpotItem; props: BlindSpotsProps }) {
  const titleId = useId();
  const [deciding, setDeciding] = useState(false);
  const target = item.anchor.level === "target" ? item.anchor.targetId : null;
  const decision = deciding && item.decisionKey && props.renderDecision ? props.renderDecision(item.decisionKey) : null;
  return (
    <li className="bs-item" data-testid="blind-spot-item" data-anchor={item.anchor.level}>
      <div id={titleId} className="bs-item-title">{item.title}</div>
      {item.detail && <div className="bs-item-detail">{item.detail}</div>}
      <div className="bs-item-anchor" data-testid="blind-spot-anchor">
        {item.anchor.level === "target" ? item.anchor.label : "Review level · not tied to one target"}
      </div>
      <div className="bs-item-actions">
        {item.actions.includes("open_source") && target && (
          <Button variant="outline" size="compact" type="button" aria-describedby={titleId} onClick={() => props.onOpenTarget(target)}>
            Open source
          </Button>
        )}
        {item.actions.includes("see_evidence") && (
          <Button variant="ghost" size="compact" type="button" aria-describedby={titleId} onClick={() => props.onInspectEvidence(target)}>
            See evidence
          </Button>
        )}
        {item.actions.includes("ask") && props.onAsk && (
          <Button variant="ghost" size="compact" type="button" aria-describedby={titleId} onClick={() => props.onAsk?.(target)}>
            Ask
          </Button>
        )}
        {item.actions.includes("record_decision") && props.renderDecision && (
          <Button variant="ghost" size="compact" type="button" aria-describedby={titleId} aria-expanded={deciding} onClick={() => setDeciding((open) => !open)}>
            Record decision
          </Button>
        )}
        {props.onChallenge && (props.canChallenge?.(item) ?? true) && (
          <Button variant="ghost" size="compact" type="button" aria-describedby={titleId} onClick={() => props.onChallenge?.(item, props.model.context)}>
            Challenge
          </Button>
        )}
      </div>
      {decision && <div className="bs-item-decision">{decision}</div>}
    </li>
  );
}

function ItemList({ items, props }: { items: readonly BlindSpotItem[]; props: BlindSpotsProps }) {
  return (
    <ul className="bs-items">
      {items.map((item) => <ItemRow key={item.id} item={item} props={props} />)}
    </ul>
  );
}

function FileGroup({ label, items, props }: { label: string; items: readonly BlindSpotItem[]; props: BlindSpotsProps }) {
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, GROUP_LIMIT);
  return (
    <details className="bs-group" open={open} onToggle={(event) => setOpen(event.currentTarget.open)} data-testid="blind-spot-group">
      <summary className="bs-summary">
        <span className="bs-group-label">{label}</span>
        <span className="bs-count">{items.length}</span>
      </summary>
      {/* Rows render only while open, so a 400-target category stays cheap. */}
      {open && (
        <>
          <ItemList items={shown} props={props} />
          {!all && items.length > GROUP_LIMIT && (
            <Button variant="ghost" size="compact" type="button" className="self-start" onClick={() => setAll(true)}>
              {`Show all ${items.length}`}
            </Button>
          )}
        </>
      )}
    </details>
  );
}

function groupByFile(items: readonly BlindSpotItem[]): { label: string; items: BlindSpotItem[] }[] {
  const groups = new Map<string, BlindSpotItem[]>();
  for (const item of items) {
    const label = item.anchor.level === "target" ? item.anchor.path : "Review level";
    groups.set(label, [...(groups.get(label) ?? []), item]);
  }
  // Review-level first, then files in the order their first item appeared.
  return [...groups].sort(([a], [b]) => Number(b === "Review level") - Number(a === "Review level")).map(([label, rows]) => ({ label, items: rows }));
}

function NotMeasuredLines({ lines }: { lines: readonly string[] }) {
  if (lines.length === 0) return null;
  return (
    <ul className="bs-unmeasured">
      {lines.map((line) => (
        <li key={line}>
          <StateBadge state="not_measured" />
          <span>{line}</span>
        </li>
      ))}
    </ul>
  );
}

/** A known category starts closed (the Brief's first screen already leads with attention) and renders rows only while open. */
function KnownCategory({ category, props }: { category: BlindSpotCategory; props: BlindSpotsProps }) {
  const [open, setOpen] = useState(false);
  const total = category.items.length + category.omitted;
  return (
    <details
      className="bs-category"
      data-state="known"
      data-testid={`blind-spot-${category.key}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="bs-summary">
        <span className="bs-category-label">{category.label}</span>
        <span className="bs-count">{total}</span>
        <StateBadge state="known" />
      </summary>
      {open && (
        <div className="bs-category-body">
          {category.items.length > GROUP_AT
            ? groupByFile(category.items).map((group) => <FileGroup key={group.label} label={group.label} items={group.items} props={props} />)
            : <ItemList items={category.items} props={props} />}
          {category.omitted > 0 && (
            <p className="bs-note">{`${category.omitted} more not listed here; open the code or evidence to see them.`}</p>
          )}
          {category.scope && <p className="bs-note">{`Measured: ${category.scope}`}</p>}
          <NotMeasuredLines lines={category.notMeasured} />
        </div>
      )}
    </details>
  );
}

function StatusRow({ category }: { category: BlindSpotCategory }) {
  const reason = category.state === "not_applicable"
    ? category.notApplicable
    : category.state === "nothing_open"
      ? `Nothing open in what was measured: ${category.scope}`
      : category.notMeasured[0] ?? "";
  const partial = category.state === "nothing_open" ? category.notMeasured : category.notMeasured.slice(1);
  return (
    <li className="bs-status" data-state={category.state} data-testid={`blind-spot-${category.key}`}>
      <div className="bs-status-head">
        <span className="bs-category-label">{category.label}</span>
        <StateBadge state={category.state} />
      </div>
      {reason && <div className="bs-item-detail">{reason}</div>}
      <NotMeasuredLines lines={partial} />
    </li>
  );
}

/** The Brief's Blind Spots section body: known gaps first, then what was not or could not be measured. */
export default function BlindSpots(props: BlindSpotsProps) {
  const { model } = props;
  const known = model.categories.filter((category) => category.state === "known");
  const rest = (state: BlindSpotState) => model.categories.filter((category) => category.state === state);
  const others = [...rest("not_measured"), ...rest("nothing_open"), ...rest("not_applicable")];
  return (
    <div className="bs-root" data-testid="blind-spots" data-snapshot={model.context.snapshot_identity}>
      <p className="bs-intro">
        What this Snapshot has not verified. Not measured means the Review has no data for it, which is not the same as safe.
      </p>
      <ul className="bs-legend" aria-label="Blind spot states" data-testid="blind-spot-counts">
        <li><StateBadge state="known" /><span>{`${model.counts.known} ${model.counts.known === 1 ? "area" : "areas"}`}</span></li>
        <li><StateBadge state="not_measured" /><span>{model.counts.notMeasured}</span></li>
        <li><StateBadge state="nothing_open" /><span>{model.counts.nothingOpen}</span></li>
        <li><StateBadge state="not_applicable" /><span>{model.counts.notApplicable}</span></li>
      </ul>
      {known.length === 0 && (
        <p className="bs-note" data-testid="blind-spot-none-known">No known blind spot in what was measured. The areas below were not measured or do not apply.</p>
      )}
      {known.map((category) => <KnownCategory key={category.key} category={category} props={props} />)}
      <details className="bs-category" data-testid="blind-spot-measurement">
        <summary className="bs-summary">
          <span className="bs-category-label">What was and was not measured</span>
          <span className="bs-count">{others.length}</span>
        </summary>
        <ul className="bs-statuses">
          {others.map((category) => <StatusRow key={category.key} category={category} />)}
        </ul>
      </details>
    </div>
  );
}
