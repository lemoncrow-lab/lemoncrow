import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, ExternalLink, Footprints, Loader2, ScanSearch, X } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { ToggleGroup } from "../../components/ui/toggle-group";
import MapCanvas from "../../map/MapCanvas";
import { buildMapModel } from "../reviewMapModel";
import type { Annotation, ReviewArchitectureDelta, ReviewMapGraph, ReviewMapSelection, ReviewTarget } from "../types";
import {
  DEFAULT_MAX_CHANGED,
  buildChangeStory,
  checkText,
  storyStage,
  type ChangeStoryStep,
  type StoryReveal,
} from "./changeStoryModel";
import { judgedParts } from "../vocabulary";
import "./changeStory.css";

export interface ChangeStoryEvidence {
  findings?: Annotation[] | null;
  targets?: ReviewTarget[] | null;
}

export interface ChangeStoryProps {
  graph: ReviewMapGraph;
  flow?: ReviewArchitectureDelta["flow"] | null;
  /** Recorded checks on the whole Snapshot; shown apart from the steps, never attached to one. */
  snapshotChecks?: readonly { name: string; status: string; detail?: string }[] | null;
  /** Review targets already in hand; otherwise `loadEvidence` supplies them. */
  targets?: readonly ReviewTarget[] | null;
  /** Local Review reads only (findings, targets); never a model or external call. */
  loadEvidence?: () => Promise<ChangeStoryEvidence>;
  /** The step to open on (a node id); follows Back/Forward when the host keeps it in the URL. */
  stepNodeId?: string;
  onStepChange?(nodeId: string): void;
  /** Jumps to Snapshot source through the host's own source navigation. */
  onOpenSource(selection: ReviewMapSelection): void;
  /** Opens the X-Ray rooted on a step's file; absent means no X-Ray control. */
  onOpenXRay?(nodeId: string): void;
  /** Set when a newer Snapshot exists: this walkthrough is then the older path, and says so. */
  stale?: string;
  /** The Walkthrough's Steps / On the map switch; when given it replaces the close button (Escape still closes). */
  viewSwitch?: ReactNode;
  onClose(): void;
}

type LoadState = "idle" | "loading" | "loaded" | "failed";

const REVEAL_OPTIONS = [
  { value: "path", label: "Path", title: "Only the files on the walkthrough" },
  { value: "neighbors", label: "Path + neighbours", title: "The walkthrough and the current step's closest links" },
  { value: "all", label: "Whole map", title: "Everything on this Map, folded the usual way" },
];

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function attributeValue(value: string): string {
  return value.replace(/["\\]/g, "\\$&");
}

function plural(count: number, one: string, many = one + "s"): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

/**
 * The Walkthrough on the map (formerly the Change Story): the changed path played one step at a time on the Review Map's
 * own canvas. Each step names its source location, change kind and exactness,
 * and shows findings, checks and receipts only where the Review data links
 * them to that file or relationship. Snapshot-exact and observed links move;
 * inferred and current-index links stay still, dashed and labelled.
 */
export default function ChangeStory({
  graph,
  flow,
  snapshotChecks,
  targets: givenTargets,
  loadEvidence,
  stepNodeId,
  onStepChange,
  onOpenSource,
  onOpenXRay,
  stale,
  viewSwitch,
  onClose,
}: ChangeStoryProps) {
  const rootRef = useRef<HTMLElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [evidence, setEvidence] = useState<ChangeStoryEvidence>({});
  const [loadState, setLoadState] = useState<LoadState>(loadEvidence ? "loading" : "idle");
  const [reveal, setReveal] = useState<StoryReveal>("neighbors");
  const [walkAll, setWalkAll] = useState(false);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [width, setWidth] = useState(0);
  const [reduced] = useState(prefersReducedMotion);

  useEffect(() => {
    if (!loadEvidence) return;
    let cancelled = false;
    setLoadState("loading");
    loadEvidence()
      .then((next) => {
        if (cancelled) return;
        setEvidence(next);
        setLoadState("loaded");
      })
      .catch(() => { if (!cancelled) setLoadState("failed"); });
    return () => { cancelled = true; };
  }, [loadEvidence]);

  useEffect(() => {
    rootRef.current?.focus({ preventScroll: true });
  }, []);

  const targets = givenTargets ?? evidence.targets ?? null;
  const findings = loadState === "loaded" ? evidence.findings ?? null : null;
  const story = useMemo(
    () => buildChangeStory({ graph, flow, findings, targets, maxChanged: walkAll ? Number.POSITIVE_INFINITY : DEFAULT_MAX_CHANGED }),
    [findings, flow, graph, targets, walkAll],
  );
  const steps = story.steps;
  const requested = stepNodeId ? steps.findIndex((step) => step.nodeId === stepNodeId) : -1;
  const [index, setIndex] = useState(Math.max(0, requested));
  // The host's URL moved (Back/Forward): follow it.
  useEffect(() => {
    if (requested >= 0) setIndex(requested);
  }, [requested]);
  const current = Math.min(index, Math.max(0, steps.length - 1));
  const step: ChangeStoryStep | undefined = steps[current];

  const onStepChangeRef = useRef(onStepChange);
  onStepChangeRef.current = onStepChange;
  const stepNode = step?.nodeId ?? "";
  useEffect(() => {
    if (stepNode) onStepChangeRef.current?.(stepNode);
  }, [stepNode]);

  const go = useCallback((next: number) => {
    setIndex(Math.max(0, Math.min(steps.length - 1, next)));
  }, [steps.length]);

  const stage = useMemo(() => storyStage(story, graph, current, reveal), [current, graph, reveal, story]);
  // The cards walk the change: a file's "0/4 marked" progress is the Map's business, not a step's.
  const stageGraph = useMemo(() => ({ ...stage.graph, nodes: stage.graph.nodes.map((node) => (node.review ? { ...node, review: null } : node)) }), [stage.graph]);
  const model = useMemo(
    () => buildMapModel({ level: graph.level, graph: stageGraph, context: { source: "snapshot" } }, {
      width,
      linkMode: "all",
      hideTests: false,
      expanded,
      chapters: stage.chapters,
    }),
    [expanded, graph.level, stage.chapters, stageGraph, width],
  );
  const via = step?.via ?? null;
  const viaDrawn = Boolean(via && model.edges.some((edge) => edge.id === via.edgeId));

  // Keep the current card in view; a reduced-motion reader gets a jump, not a glide.
  useEffect(() => {
    if (!step) return;
    const card = stageRef.current?.querySelector(`[data-node-id="${attributeValue(step.nodeId)}"]`);
    if (card && typeof card.scrollIntoView === "function") {
      card.scrollIntoView({ block: "center", inline: "nearest", behavior: reduced ? "auto" : "smooth" });
    }
  }, [model, reduced, step]);

  const selectStepNode = useCallback((id: string | null) => {
    if (!id) return;
    const at = steps.findIndex((item) => item.nodeId === id);
    if (at >= 0) go(at);
  }, [go, steps]);
  const selectStepEdge = useCallback((id: string) => {
    const at = steps.findIndex((item) => item.via?.edgeId === id);
    if (at >= 0) go(at);
  }, [go, steps]);
  const toggleGroup = useCallback((key: string) => {
    setExpanded((value) => {
      const next = new Set(value);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const onKeyDownCapture = (event: KeyboardEvent<HTMLElement>) => {
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, [contenteditable='true'], [role='radiogroup'], [role='radio']")) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      event.stopPropagation();
      go(current + (event.key === "ArrowRight" ? 1 : -1));
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    }
  };

  const motion = !via || !viaDrawn ? "none" : via.animate && !reduced ? "flow" : via.animate ? "still" : "static";
  const checks = snapshotChecks ?? null;

  return (
    <section
      ref={rootRef}
      tabIndex={-1}
      aria-label="Walkthrough on the map"
      data-testid="change-story"
      onKeyDownCapture={onKeyDownCapture}
      className="flex h-full min-h-0 w-full flex-col overflow-hidden bg-background outline-none"
    >
      <header className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/80 px-4 py-2.5">
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <Footprints size={16} aria-hidden="true" className="shrink-0 text-subtle" />
          <h2 className="text-heading font-semibold text-foreground">Walkthrough</h2>
          {viewSwitch}
        </div>
        <ToggleGroup
          aria-label="What the map shows"
          options={REVEAL_OPTIONS}
          value={reveal}
          onChange={(value) => setReveal(value === "path" || value === "all" ? value : "neighbors")}
          className="flex-nowrap gap-1"
        />
        <div className="flex items-center gap-1.5">
          <Button variant="outline" size="xs" onClick={() => go(current - 1)} disabled={current <= 0} icon={<ArrowLeft size={14} />} aria-keyshortcuts="ArrowLeft">
            Back
          </Button>
          <span className="min-w-16 text-center font-mono text-caption tabular-nums text-muted-foreground" data-testid="change-story-position">
            {steps.length ? `${current + 1} / ${steps.length}` : "0 / 0"}
          </span>
          <Button variant="outline" size="xs" onClick={() => go(current + 1)} disabled={current >= steps.length - 1} aria-keyshortcuts="ArrowRight">
            Next<ArrowRight size={14} aria-hidden="true" />
          </Button>
          {viewSwitch ? null : (
            <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close the map" aria-keyshortcuts="Escape">
              <X size={14} />
            </Button>
          )}
        </div>
      </header>

      {stale && (
        <p role="status" data-testid="change-story-stale" className="shrink-0 border-b border-amber-500/40 bg-amber-500/10 px-4 py-1.5 text-label text-amber-200">
          {stale}
        </p>
      )}

      {!step ? (
        <div className="flex flex-1 items-center justify-center px-6 text-center text-label text-muted-foreground">
          This revision records no changed file to walk through.
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
          <div
            ref={stageRef}
            className="cs-stage relative min-h-64 min-w-0 flex-1"
            data-motion={motion}
            data-testid="change-story-stage"
          >
            <style>{`.cs-stage .rm-card[data-node-id="${attributeValue(step.nodeId)}"]{border-color:rgb(var(--foreground) / .7);background:rgb(var(--accent));}`}</style>
            <MapCanvas
              model={model}
              selectedId={viaDrawn ? null : step.nodeId}
              selectedEdgeId={viaDrawn && via ? via.edgeId : null}
              onSelect={selectStepNode}
              onSelectEdge={selectStepEdge}
              onOpen={selectStepNode}
              onOpenSymbol={() => undefined}
              onToggleGroup={toggleGroup}
              onWidth={setWidth}
              ariaLabel="Walkthrough map"
            />
            {via && (
              <p className="cs-legend" data-testid="change-story-link-legend">
                {`${via.relation} · ${via.exactnessLabel}`}
                {via.animate ? "" : " · drawn still and dashed"}
                {viaDrawn ? "" : " · not drawn at this zoom"}
              </p>
            )}
            {(stage.hiddenNodes > 0 || story.omittedChanged > 0) && (
              <p className="cs-hidden-note" data-testid="change-story-hidden">
                {[
                  stage.hiddenNodes > 0 ? `${plural(stage.hiddenNodes, "file")} on the Map not shown` : "",
                  story.omittedChanged > 0 ? `${plural(story.omittedChanged, "changed file")} not in the walkthrough` : "",
                ].filter(Boolean).join(" · ")}
              </p>
            )}
          </div>

          <aside className="flex max-h-[45%] w-full shrink-0 flex-col overflow-y-auto border-t border-border/80 lg:max-h-none lg:w-96 lg:border-l lg:border-t-0" aria-label="Current step">
            <StepCard
              key={step.nodeId}
              step={step}
              total={steps.length}
              loadState={loadState}
              findingsKnown={story.findingsKnown}
              targetsKnown={story.targetsKnown || Boolean(givenTargets)}
              onOpenSource={onOpenSource}
              onXRay={onOpenXRay ? () => onOpenXRay(step.nodeId) : undefined}
            />

            <section className="border-t border-border/70 px-4 py-3" aria-label="All steps">
              <h3 className="text-micro uppercase tracking-wide text-muted-foreground">Steps</h3>
              <ol className="mt-1.5 flex flex-col gap-0.5" data-testid="change-story-steps">
                {steps.map((item) => (
                  <li key={item.nodeId}>
                    <Button
                      unstyled
                      className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-label text-subtle hover:bg-accent aria-[current=step]:bg-accent aria-[current=step]:text-foreground"
                      aria-current={item.index === current ? "step" : undefined}
                      onClick={() => go(item.index)}
                    >
                      <span className="w-5 shrink-0 text-right font-mono text-caption tabular-nums text-muted-foreground">{item.index + 1}</span>
                      <span className="min-w-0 flex-1 truncate font-mono">{item.title}</span>
                      <span className="shrink-0 text-caption text-muted-foreground">{item.roleLabel}</span>
                    </Button>
                  </li>
                ))}
              </ol>
              {story.omittedChanged > 0 && (
                <Button variant="ghost" size="compact" className="mt-1.5" onClick={() => setWalkAll(true)}>
                  {`Walk all ${plural(story.changedTotal, "changed file")}`}
                </Button>
              )}
              {story.omittedAffected > 0 && (
                <p className="mt-1.5 text-caption text-muted-foreground">{`${plural(story.omittedAffected, "more affected caller")} on the Map.`}</p>
              )}
            </section>

            <section className="border-t border-border/70 px-4 py-3 text-caption" aria-label="Checks on this revision" data-testid="change-story-snapshot-checks">
              <h3 className="text-micro uppercase tracking-wide text-muted-foreground">Checks on this revision · not tied to a step</h3>
              {checks === null ? (
                <p className="mt-1.5 text-muted-foreground">Revision checks are not available here, so none are shown. That is not a pass.</p>
              ) : checks.length && checks.every((check) => check.status !== "PASS" && check.status !== "FAIL") ? (
                // Nothing ran: one line naming them, not a "did not run" row per check.
                <p className="mt-1.5 text-amber-300/90" data-testid="change-story-none-ran">
                  {`No check ran on this revision (${checks.map((check) => check.name).join(", ")}). That is not a pass.`}
                </p>
              ) : checks.length ? (
                <ul className="mt-1.5 flex flex-col gap-0.5">
                  {checks.map((check) => (
                    <li key={`${check.name}:${check.status}`} className="flex flex-wrap items-baseline gap-x-2">
                      <span className="break-all font-mono text-subtle">{check.name}</span>
                      <span className={checkText(check.status).tone}>{checkText(check.status).label}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1.5 text-amber-300/90">No checks recorded for this revision. Nothing here is verified by a recorded run.</p>
              )}
            </section>
          </aside>
        </div>
      )}
    </section>
  );
}

function StepCard({
  step,
  total,
  loadState,
  findingsKnown,
  targetsKnown,
  onOpenSource,
  onXRay,
}: {
  step: ChangeStoryStep;
  total: number;
  loadState: LoadState;
  findingsKnown: boolean;
  targetsKnown: boolean;
  onOpenSource(selection: ReviewMapSelection): void;
  onXRay?(): void;
}) {
  const { evidence, via } = step;
  const checks = evidence.checks;
  const human = evidence.human;
  const viaSite = via?.site && via.exactness !== "current_index" && via.exactness !== "unknown" && via.selection?.path ? via.site : null;
  return (
    <div className="cs-step flex flex-col gap-3 px-4 py-3" data-testid="change-story-step" aria-live="polite">
      <div>
        <div className="text-micro uppercase tracking-wide text-muted-foreground">{`Step ${step.index + 1} of ${total} · ${step.roleLabel}`}</div>
        <h3 className="mt-0.5 break-all text-heading font-semibold text-foreground">{step.title}</h3>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <Badge tone="neutral" data-testid="change-story-change">{step.changeLabel}</Badge>
          <Badge tone={step.exactness === "snapshot" ? "secondary" : "neutral"} data-testid="change-story-exactness">{step.exactnessLabel}</Badge>
          {step.boundaries.map((boundary) => <Badge key={boundary} tone="neutral">{`${boundary} boundary`}</Badge>)}
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <span className="break-all font-mono text-caption text-subtle" data-testid="change-story-location">{step.locationLabel}</span>
        <div>
          <Button
            variant="outline"
            size="xs"
            disabled={!step.canOpenSource}
            tooltip={step.canOpenSource ? "" : "Only revision source opens from here; this file is context from the current index."}
            onClick={() => onOpenSource(step.selection)}
            icon={<ExternalLink size={14} />}
          >
            Open source
          </Button>
          {onXRay && (
            <Button
              variant="ghost"
              size="xs"
              className="ml-1.5"
              tooltip="What this file calls, what reaches it, and where it meets storage or another service, before and after"
              onClick={onXRay}
              icon={<ScanSearch size={14} />}
            >
              X-Ray this step
            </Button>
          )}
        </div>
      </div>

      {via && (
        <div className="flex flex-col gap-1 border-l-2 border-border pl-2.5 text-caption" data-testid="change-story-via">
          <span className="text-label text-foreground">{via.sentence}</span>
          <span className="text-muted-foreground">{`Relationship ${via.changeLabel} · ${via.exactnessLabel}`}</span>
          <span className={via.exactness === "observed" ? "text-emerald-300/90" : "text-muted-foreground"}>{via.runtime}</span>
          {viaSite && (
            <Button variant="link" size="compact" className="h-auto self-start p-0 font-mono text-caption" onClick={() => onOpenSource(via.selection)}>
              {`${viaSite.path}:${viaSite.line}`}
            </Button>
          )}
        </div>
      )}

      <div className="flex flex-col gap-1 text-caption" data-testid="change-story-findings">
        <h4 className="text-micro uppercase tracking-wide text-muted-foreground">Serious findings</h4>
        {loadState === "loading" ? (
          <span className="inline-flex items-center gap-1 text-muted-foreground"><Loader2 size={14} className="animate-spin" aria-hidden="true" />Loading findings…</span>
        ) : loadState === "failed" ? (
          <span className="text-amber-300/90">Findings could not load, so none are shown. That is not a clean result.</span>
        ) : !findingsKnown ? (
          <span className="text-muted-foreground">Findings are not available here.</span>
        ) : evidence.findings.length ? (
          <ul className="flex flex-col gap-1">
            {evidence.findings.map((finding) => (
              <li key={finding.id} className="flex flex-wrap items-baseline gap-x-1.5">
                <Badge tone={finding.severity}>{finding.severity}</Badge>
                <span className="text-foreground">{finding.title}</span>
                <span className="font-mono text-muted-foreground">{`${finding.path}:${finding.line} · ${finding.side}${finding.on === "relationship" ? " · on this relationship's site" : ""}`}</span>
              </li>
            ))}
          </ul>
        ) : (
          <span className="text-muted-foreground">No open serious finding is anchored to this file.</span>
        )}
      </div>

      <div className="flex flex-col gap-1 text-caption" data-testid="change-story-checks">
        <h4 className="text-micro uppercase tracking-wide text-muted-foreground">Checks linked to this step</h4>
        {checks ? (
          <span className={checks.tone}>{`${checks.words.join(" · ")} · across ${plural(checks.targets, "part")}`}</span>
        ) : loadState === "loading" && !targetsKnown ? (
          <span className="text-muted-foreground">Loading…</span>
        ) : targetsKnown ? (
          <span className="text-amber-300/90">No recorded check is linked to this step. That is not a pass.</span>
        ) : (
          <span className="text-muted-foreground">Check links are not available here.</span>
        )}
      </div>

      {evidence.receipts.length > 0 && (
        <div className="flex flex-col gap-1 text-caption" data-testid="change-story-receipts">
          <h4 className="text-micro uppercase tracking-wide text-muted-foreground">Receipts</h4>
          <ul className="flex flex-col gap-0.5">
            {evidence.receipts.map((receipt) => (
              <li key={`${receipt.ref}:${receipt.label}`} className="break-all font-mono text-subtle" title={receipt.recordId || receipt.ref}>
                {receipt.label}<span className="text-muted-foreground">{` · ${receipt.ref}`}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {human && (
        <div className="text-caption" data-testid="change-story-human">
          <span className={human.needed ? "text-amber-300/90" : "text-muted-foreground"}>
            {[
              `Human review: ${judgedParts({ reviewed: human.reviewed, needs_changes: human.needsChanges })} of ${plural(human.targets, "part")} reviewed`,
              human.needsChanges ? `${human.needsChanges} need changes` : "",
              human.openThreads ? plural(human.openThreads, "open thread") : "",
              human.needed ? "still needed" : "",
            ].filter(Boolean).join(" · ")}
          </span>
        </div>
      )}
    </div>
  );
}
