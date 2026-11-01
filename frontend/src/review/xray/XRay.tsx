import { useEffect, useMemo, useState } from "react";
import { ExternalLink, RefreshCw, ScanSearch, X } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { ToggleGroup } from "../../components/ui/toggle-group";
import type { ReviewMapGraph, ReviewMapSelection, ReviewTarget } from "../types";
import { SIDE_LABEL, buildXRay, type XRay as XRayModel, type XRayFlow, type XRaySide, type XRayStep } from "./xrayModel";
import "./xray.css";

export interface XRayProps {
  graph: ReviewMapGraph;
  flow?: XRayFlow | null;
  targets?: readonly ReviewTarget[] | null;
  rootId: string;
  side: XRaySide;
  /** The Snapshot this walk was opened on; when the graph shows another, the walk is stale. */
  walkSnapshot: string;
  onSideChange(side: XRaySide): void;
  /** Re-roots the X-Ray on another step ("X-Ray from here"). */
  onRootChange(nodeId: string): void;
  /** Rebuilds a stale walk on the Snapshot shown now. */
  onRebuild(): void;
  /** Jumps to exact code (a node or a relationship's site) through the host's own source navigation. */
  onOpenSource(selection: ReviewMapSelection): void;
  onClose(): void;
  /** N04: challenge the selected step's claim; without it no Challenge control renders. */
  onChallenge?(step: XRayStep, xray: XRayModel): void;
  /** Opens the recorded run behind a traced link (the host's checks view for the step's code); without it the trace is text. */
  onOpenEvidence?(selection: ReviewMapSelection): void;
}

const SIDE_OPTIONS = [
  { value: "before", label: "Before", title: "The base: relationships before this change" },
  { value: "after", label: "After", title: "This Snapshot: relationships after this change" },
];

function plural(count: number, one: string, many = one + "s"): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function truncationWords(xray: XRayModel): string[] {
  const { truncation, bounds } = xray;
  return [
    truncation.depth ? `${plural(truncation.depth, "link")} past depth ${bounds.depth}` : "",
    truncation.branching ? `${plural(truncation.branching, "sibling link")} over ${bounds.branching} per step` : "",
    truncation.total ? `${plural(truncation.total, "step")} over the ${bounds.steps}-step bound` : "",
    truncation.consumers ? `${plural(truncation.consumers, "more caller")}` : "",
    truncation.joins ? `${plural(truncation.joins, "link")} into steps already shown` : "",
  ].filter(Boolean);
}

/**
 * Behavioral X-Ray: one focused path from a changed file -- what reaches it,
 * what it calls, and where it meets storage or hands off to a queue or another
 * service -- on the Before or After side. Links read as possible/reachable
 * unless this Snapshot's recorded run observed them, and only those observed
 * in order move.
 */
export default function XRay({
  graph,
  flow,
  targets,
  rootId,
  side,
  walkSnapshot,
  onSideChange,
  onRootChange,
  onRebuild,
  onOpenSource,
  onClose,
  onChallenge,
  onOpenEvidence,
}: XRayProps) {
  const xray = useMemo(
    () => buildXRay({ graph, flow, targets, rootId, side, walkSnapshot }),
    [flow, graph, rootId, side, targets, walkSnapshot],
  );
  const [reduced] = useState(prefersReducedMotion);
  const all = useMemo(() => [...xray.consumers, ...xray.steps], [xray]);
  const [selectedKey, setSelectedKey] = useState("");
  // A new walk (side, root, Snapshot) starts on its entry point.
  useEffect(() => setSelectedKey(xray.steps[0]?.key ?? ""), [xray]);
  const selected = all.find((step) => step.key === selectedKey) ?? xray.steps[0] ?? null;
  const truncated = truncationWords(xray);

  return (
    <section className="xr-root" aria-label="Behavioral X-Ray" data-testid="xray" data-side={side} data-status={xray.status}>
      <header className="xr-header">
        <ScanSearch size={16} aria-hidden="true" className="shrink-0 text-subtle" />
        <div className="min-w-0 flex-1">
          <h3 className="text-label font-semibold text-foreground">
            Behavioral X-Ray <span className="font-mono font-normal text-subtle">{`from ${xray.rootTitle}`}</span>
          </h3>
          <p className="text-caption text-muted-foreground" data-testid="xray-side-label">{SIDE_LABEL[side]}</p>
        </div>
        <ToggleGroup
          aria-label="Side of the change"
          variant="segment"
          options={SIDE_OPTIONS}
          value={side}
          onChange={(value) => onSideChange(value === "before" ? "before" : "after")}
        />
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close the X-Ray">
          <X size={14} />
        </Button>
      </header>

      {xray.status !== "ready" ? (
        <div className="xr-notice" role="status" data-testid="xray-status">
          <p>{xray.statusNote}</p>
          {xray.status === "stale" && (
            <Button variant="outline" size="xs" onClick={onRebuild} icon={<RefreshCw size={14} />}>Rebuild on this Snapshot</Button>
          )}
          {xray.status === "absent" && (
            <Button variant="outline" size="xs" onClick={() => onSideChange(side === "before" ? "after" : "before")}>
              {`Show ${side === "before" ? "After" : "Before"}`}
            </Button>
          )}
          {xray.notMeasured.length > 0 && <NotMeasured xray={xray} />}
        </div>
      ) : (
        <div className="xr-body">
          <div className="xr-path-column">
            <p className="xr-order" data-testid="xray-order" data-observed={xray.observedLinks}>{xray.orderNote}</p>
            <ul className="xr-legend" aria-label="How to read the path">
              <li><span className="xr-glyph" data-tone="added">+</span>added</li>
              <li><span className="xr-glyph" data-tone="removed">−</span>removed</li>
              <li><span className="xr-glyph" data-tone="changed">~</span>changed</li>
              <li><span className="xr-glyph" data-tone="unchanged">=</span>unchanged</li>
              <li><span className="xr-line" data-order="observed" aria-hidden="true" />observed</li>
              <li><span className="xr-line" data-order="reachable" aria-hidden="true" />possible/reachable</li>
              <li><span className="xr-handoff-chip" aria-hidden="true">⇢</span>handoff, not traced</li>
            </ul>
            <p className="xr-diff" data-testid="xray-diff">
              {[
                `On this path: ${xray.diff.added} added · ${xray.diff.removed} removed · ${xray.diff.changed} changed`,
                xray.comparison === "measured" && xray.diff.otherSideOnly
                  ? `${plural(xray.diff.otherSideOnly, "link")} only in ${side === "before" ? "After" : "Before"}`
                  : "",
                xray.diff.notCompared
                  ? `${plural(xray.diff.notCompared, "link")} from these files not compared with the base${side === "before" ? ", so not drawn here" : ""}`
                  : "",
                xray.comparison === "not_measured" ? "Before is not measured on this Map" : "",
              ].filter(Boolean).join(" · ")}
            </p>

            {xray.consumers.length > 0 && (
              <>
                <h4 className="xr-heading">Reached from</h4>
                <ol className="xr-steps" data-testid="xray-consumers">
                  {xray.consumers.map((step) => (
                    <StepRow key={step.key} step={step} selected={selected?.key === step.key} reduced={reduced} onSelect={setSelectedKey} />
                  ))}
                </ol>
              </>
            )}
            <h4 className="xr-heading">Path</h4>
            <ol className="xr-steps" data-testid="xray-steps">
              {xray.steps.map((step) => (
                <StepRow key={step.key} step={step} selected={selected?.key === step.key} reduced={reduced} onSelect={setSelectedKey} />
              ))}
            </ol>
            {truncated.length > 0 && (
              <p className="xr-truncated" data-testid="xray-truncated">{`Not shown: ${truncated.join(" · ")}`}</p>
            )}
            {xray.degraded.length > 0 && (
              <ul className="xr-degraded" data-testid="xray-degraded">
                {xray.degraded.map((line) => <li key={line}>{line}</li>)}
              </ul>
            )}
            <NotMeasured xray={xray} />
          </div>
          {selected && (
            <StepDetail
              step={selected}
              isRoot={selected.nodeId === xray.rootId}
              targetsKnown={Boolean(targets)}
              onOpenSource={onOpenSource}
              onRootChange={onRootChange}
              onChallenge={onChallenge && selected.path ? () => onChallenge(selected, xray) : undefined}
              onOpenEvidence={onOpenEvidence && selected.path ? () => onOpenEvidence(selected.selection) : undefined}
            />
          )}
        </div>
      )}
    </section>
  );
}

function NotMeasured({ xray }: { xray: XRayModel }) {
  return (
    <details className="xr-not-measured" data-testid="xray-not-measured">
      <summary>{`Not measured · ${xray.notMeasured.length}`}</summary>
      <ul>
        {xray.notMeasured.map((item) => (
          <li key={item.key} data-key={item.key}>
            <span className="font-medium text-foreground">{item.label}</span>
            <span>{item.reason}</span>
            <span className="xr-needs">{`Needs: ${item.needs}`}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function StepRow({ step, selected, reduced, onSelect }: { step: XRayStep; selected: boolean; reduced: boolean; onSelect(key: string): void }) {
  const link = step.link;
  const motion = link?.animate && !reduced ? "observed" : "still";
  return (
    <li
      className="xr-step"
      data-testid="xray-step"
      data-role={step.role}
      data-tone={step.mark.tone}
      data-link-tone={link?.mark.tone ?? ""}
      data-order={link?.order ?? ""}
      data-motion={motion}
      style={{
        paddingLeft: `${Math.max(0, step.depth) * 14}px`,
        ...(motion === "observed" && link?.sequence ? { animationDelay: `${Math.min(12, link.sequence) * 140}ms` } : {}),
      }}
    >
      <Button
        unstyled
        className="xr-step-button"
        aria-current={selected ? "step" : undefined}
        onClick={() => onSelect(step.key)}
      >
        {link && (
          <span className="xr-link" data-order={link.order} title={link.orderLabel}>
            <span className="xr-glyph" data-tone={link.mark.tone} aria-label={`link ${link.mark.label}`}>{link.mark.glyph}</span>
            {link.handoff && <span className="xr-handoff-chip" aria-label="handoff, not traced">⇢</span>}
          </span>
        )}
        <span className="xr-glyph" data-tone={step.mark.tone} aria-label={`file ${step.mark.label}`}>{step.mark.glyph}</span>
        <span className="xr-step-title">{step.title}</span>
        <span className="xr-step-role">{step.roleLabel}</span>
        {link && (
          <span className="xr-step-order" data-testid="xray-step-order">
            {link.order === "observed" ? (link.sequence ? `observed #${link.sequence}` : "observed") : "possible"}
          </span>
        )}
      </Button>
      {step.exitLabel && <div className="xr-exit" data-exit={step.exit ?? ""}>{step.exitLabel}</div>}
    </li>
  );
}

function StepDetail({
  step,
  isRoot,
  targetsKnown,
  onOpenSource,
  onRootChange,
  onChallenge,
  onOpenEvidence,
}: {
  step: XRayStep;
  isRoot: boolean;
  targetsKnown: boolean;
  onOpenSource(selection: ReviewMapSelection): void;
  onRootChange(nodeId: string): void;
  onChallenge?(): void;
  onOpenEvidence?(): void;
}) {
  const link = step.link;
  const site = link?.site && link.exactness !== "current_index" && link.exactness !== "unknown" && link.selection?.path ? link.site : null;
  return (
    <aside className="xr-detail" aria-label="Selected X-Ray step" data-testid="xray-detail" aria-live="polite">
      <div className="text-micro uppercase tracking-wide text-muted-foreground">{step.roleLabel}</div>
      <h4 className="break-all text-label font-semibold text-foreground">{step.title}</h4>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="neutral" data-testid="xray-detail-change">{`${step.mark.glyph} ${step.mark.label}`}</Badge>
        <Badge tone={step.exactness === "snapshot" ? "secondary" : "neutral"}>{step.exactnessLabel}</Badge>
        {step.boundaries.map((boundary) => (
          <Badge key={boundary.label} tone="neutral" title={boundary.basis === "name" ? "Recognised from the name only" : "The Review Map's boundary tag"}>
            {boundary.basis === "name" ? `${boundary.label} · from the name` : boundary.label}
          </Badge>
        ))}
      </div>
      <span className="break-all font-mono text-caption text-subtle" data-testid="xray-detail-location">{step.locationLabel}</span>
      <div className="flex flex-wrap gap-1.5">
        <Button
          variant="outline"
          size="xs"
          disabled={!step.canOpenSource}
          tooltip={step.canOpenSource ? "" : "Only Snapshot source opens from here; this file is context from the current index."}
          onClick={() => onOpenSource(step.selection)}
          icon={<ExternalLink size={14} />}
        >
          Open source
        </Button>
        {!isRoot && (
          <Button variant="ghost" size="xs" onClick={() => onRootChange(step.nodeId)} icon={<ScanSearch size={14} />}>
            X-Ray from here
          </Button>
        )}
        {onChallenge && (
          <Button variant="ghost" size="xs" onClick={onChallenge}>Challenge</Button>
        )}
      </div>

      {link && (
        <div className="xr-detail-link" data-testid="xray-detail-link" data-order={link.order}>
          <span className="text-label text-foreground">{link.sentence}</span>
          <span className="text-muted-foreground">
            {`Link ${link.mark.label}${link.weights && link.change === "changed" ? ` · ${link.weights.base} → ${link.weights.head} references` : ""} · ${link.exactnessLabel}`}
          </span>
          <span className={link.order === "observed" ? "text-emerald-300/90" : "text-muted-foreground"} data-testid="xray-detail-order">{link.orderLabel}</span>
          {link.handoff && (
            <span className="text-amber-300/90" data-testid="xray-detail-handoff">
              {`⇢ ${link.handoff.label}, recognised from the name. Not a direct call: what runs after it is not traced.`}
            </span>
          )}
          {link.landsOn.length > 0 && <span className="font-mono text-subtle">{`Lands on ${link.landsOn.join(", ")}`}</span>}
          {site && (
            <Button variant="link" size="compact" className="h-auto self-start p-0 font-mono text-caption" onClick={() => onOpenSource(link.selection)}>
              {`${site.path}:${site.line}`}
            </Button>
          )}
          {link.trace && (
            <span className="break-all font-mono text-subtle" data-testid="xray-detail-trace" title={link.trace.recordId || link.trace.evidenceRef}>
              {`Trace: ${link.trace.callerFn} → ${link.trace.calleeFn} (${link.trace.side}${link.trace.recipe ? ` · ${link.trace.recipe}` : ""}) · ${link.trace.evidenceRef}`}
            </span>
          )}
          {link.trace && onOpenEvidence && (
            <Button variant="link" size="compact" className="h-auto self-start p-0 text-caption" data-testid="xray-detail-evidence" onClick={onOpenEvidence}>
              See the recorded run
            </Button>
          )}
        </div>
      )}

      {step.errorHints.length > 0 && (
        <div className="text-caption" data-testid="xray-detail-errors">
          <h5 className="text-micro uppercase tracking-wide text-muted-foreground">Error-handling sites</h5>
          <ul>
            {step.errorHints.map((hint) => (
              <li key={hint.name} className="font-mono text-subtle">
                {hint.name}
                <span className="font-sans text-muted-foreground">{hint.basis === "trace" ? " · called in a recorded run" : " · from the name; branch not analysed"}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="text-caption" data-testid="xray-detail-checks">
        <h5 className="text-micro uppercase tracking-wide text-muted-foreground">Checks linked to this file</h5>
        {step.checks ? (
          <span className={step.checks.tone}>{`${step.checks.words.join(" · ")} · across ${plural(step.checks.targets, "review target")}`}</span>
        ) : targetsKnown ? (
          <span className="text-amber-300/90">No recorded check is linked to this file. That is not a pass.</span>
        ) : (
          <span className="text-muted-foreground">Check links are not available here.</span>
        )}
      </div>
    </aside>
  );
}
