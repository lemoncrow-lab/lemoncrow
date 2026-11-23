// The side panel: what a selected card or arrow is, on the side shown, and what is (and is not) known about it.
import type { ReactNode } from "react";
import { ExternalLink, ScanSearch } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import type { ReviewMapSelection } from "../types";
import { LineSample, cardChangeLabel, cardGlyph } from "./XRayCanvas";
import { LANE_LABEL, edgePaths, edgeStyle, type LensCard, type LensDiagram, type LensEdge, type LensView } from "./xrayLens";
import type { XRaySide } from "./xrayModel";

function plural(count: number, one: string, many = one + "s"): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

export interface PanelHandlers {
  onOpenSource(selection: ReviewMapSelection): void;
  /** Zooms into a file's path (whole change) or re-roots the path on it (focus). */
  onFocus(nodeId: string): void;
  onSelectCard(cardId: string): void;
  /** By the arrow's card pair, which both sides share. */
  onSelectEdge(pair: string): void;
  onOpenEvidence?(selection: ReviewMapSelection): void;
  onChallenge?(card: LensCard): void;
  renderBehavior?(paths: string[], scope: "file" | "arrow"): ReactNode;
}

function EdgeRow({ edge, other, onSelect }: { edge: LensEdge; other: string; onSelect(): void }) {
  const style = edgeStyle(edge);
  return (
    <li>
      <Button unstyled className="xr-panel-edge" onClick={onSelect} aria-label={style.label}>
        <LineSample line={style.line} tone={style.tone} />
        {style.glyph && <span className="xr-glyph" data-tone={style.tone} aria-hidden="true">{style.glyph}</span>}
        <span className="xr-panel-edge-name">{other}</span>
        <span className="xr-panel-edge-basis" data-observed={edge.observed ? "true" : "false"}>
          {edge.observed ? (edge.sequence !== null ? `observed #${edge.sequence}` : "observed") : "static"}
        </span>
      </Button>
    </li>
  );
}

export function CardPanel({
  card,
  diagram,
  view,
  side,
  start,
  targetsKnown,
  handlers,
}: {
  card: LensCard;
  diagram: LensDiagram;
  view: LensView;
  side: XRaySide;
  /** The walkthrough's first card, offered before anything is chosen. */
  start: boolean;
  targetsKnown: boolean;
  handlers: PanelHandlers;
}) {
  const byId = new Map(view.cards.map((item) => [item.id, item]));
  const incoming = view.edges.filter((edge) => edge.to === card.id);
  const outgoing = view.edges.filter((edge) => edge.from === card.id);
  const step = card.steps?.[side];
  const isRoot = diagram.mode === "focus" && card.nodeId === diagram.rootId;
  if (card.kind === "fold") {
    return (
      <aside className="xr-panel" aria-label="Selected X-Ray card" data-testid="xray-detail" aria-live="polite">
        <div className="xr-panel-kicker">{card.roleLabel}</div>
        <h4 className="xr-panel-title">{`${plural(card.folded?.count ?? 0, "file")} folded`}</h4>
        <p className="xr-panel-note">{`${cardChangeLabel(card, side)}. The picture keeps ${diagram.mode === "change" ? "each lane" : "each step"} readable by folding past its bound; ${diagram.mode === "change" ? "open any of them to see its path." : "re-root the path on a step to see past it."}`}</p>
        {card.folded && card.folded.names.length > 0 && (
          <ul className="xr-panel-list" data-testid="xray-fold-list">
            {card.folded.names.slice(0, 40).map((path, index) => (
              <li key={path}>
                <Button variant="link" size="compact" className="xr-panel-path" onClick={() => handlers.onFocus(card.folded?.ids[index] ?? "")}>
                  {path}
                </Button>
              </li>
            ))}
            {card.folded.names.length > 40 && <li className="xr-panel-note">{`and ${card.folded.names.length - 40} more`}</li>}
          </ul>
        )}
      </aside>
    );
  }
  return (
    <aside className="xr-panel" aria-label="Selected X-Ray card" data-testid="xray-detail" aria-live="polite">
      <div className="xr-panel-kicker">
        {start ? "Start here · " : ""}
        {diagram.mode === "focus" ? card.roleLabel : LANE_LABEL[card.lane]}
      </div>
      <h4 className="xr-panel-title">
        <span className="xr-glyph" data-tone={card.change === "context_only" ? "unknown" : card.change} aria-hidden="true">{cardGlyph(card)}</span>
        <span className="min-w-0 break-all">{card.title}</span>
      </h4>
      <div className="flex min-w-0 flex-wrap items-center gap-1.5 [&>*]:max-w-full [&>*]:truncate">
        <Badge tone="neutral" data-testid="xray-detail-change">{`${cardGlyph(card)} ${cardChangeLabel(card, side)}`}</Badge>
        {/* This revision's own code is the normal case and needs no badge; only a weaker basis is named. */}
        {card.exactness === "snapshot" ? null : <Badge tone="neutral" data-testid="xray-detail-exactness">{card.exactnessLabel}</Badge>}
        {card.boundaries.map((boundary) => (
          <Badge key={boundary.label} tone="neutral" title={boundary.basis === "name" ? "Recognised from the name only" : "The Review Map's boundary tag"}>
            {boundary.basis === "name" ? `${boundary.label} · from the name` : boundary.label}
          </Badge>
        ))}
      </div>
      <span className="break-all font-mono text-caption text-subtle" data-testid="xray-detail-location">{card.path}</span>
      <div className="flex flex-wrap gap-1.5">
        <Button
          variant="outline"
          size="xs"
          disabled={!card.canOpenSource || !card.selection}
          tooltip={card.canOpenSource ? "" : "Only revision source opens from here; this file is context from the current index."}
          onClick={() => card.selection && handlers.onOpenSource(card.selection)}
          icon={<ExternalLink size={14} />}
        >
          Open source
        </Button>
        {!isRoot && (
          <Button variant="ghost" size="xs" data-testid="xray-detail-focus" onClick={() => handlers.onFocus(card.nodeId)} icon={<ScanSearch size={14} />}>
            {diagram.mode === "change" ? "Open its path" : "X-Ray from here"}
          </Button>
        )}
        {handlers.onChallenge && diagram.mode === "focus" && (
          <Button variant="ghost" size="xs" onClick={() => handlers.onChallenge?.(card)}>Challenge</Button>
        )}
      </div>
      {step?.exitLabel && <p className="xr-panel-note" data-exit={step.exit ?? ""}>{step.exitLabel}</p>}
      {step && step.errorHints.length > 0 && (
        <div className="text-caption" data-testid="xray-detail-errors">
          <h5 className="xr-panel-heading">Error-handling sites</h5>
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
      {(incoming.length > 0 || outgoing.length > 0) && (
        <div className="xr-panel-section" data-testid="xray-detail-arrows">
          {incoming.length > 0 && (
            <>
              <h5 className="xr-panel-heading">{`Reached from · ${incoming.length}`}</h5>
              <ul className="xr-panel-list">
                {incoming.map((edge) => <EdgeRow key={edge.key} edge={edge} other={byId.get(edge.from)?.title ?? edge.from} onSelect={() => handlers.onSelectEdge(edge.pair)} />)}
              </ul>
            </>
          )}
          {outgoing.length > 0 && (
            <>
              <h5 className="xr-panel-heading">{`Uses · ${outgoing.length}`}</h5>
              <ul className="xr-panel-list">
                {outgoing.map((edge) => <EdgeRow key={edge.key} edge={edge} other={byId.get(edge.to)?.title ?? edge.to} onSelect={() => handlers.onSelectEdge(edge.pair)} />)}
              </ul>
            </>
          )}
        </div>
      )}
      <div className="text-caption" data-testid="xray-detail-checks">
        <h5 className="xr-panel-heading">Checks linked to this file</h5>
        {card.checks ? (
          <span className={card.checks.fail ? "text-p0" : card.checks.pass && !card.checks.unknown ? "text-ok" : "text-p1"}>{`${card.checks.words.join(" · ")} · across ${plural(card.checks.targets, "part")}`}</span>
        ) : targetsKnown ? (
          <span className="text-p1">No recorded check is linked to this file. That is not a pass.</span>
        ) : (
          <span className="text-muted-foreground">Check links are not available here.</span>
        )}
      </div>
      {handlers.renderBehavior?.([card.path], "file")}
    </aside>
  );
}

export function EdgePanel({
  edge,
  view,
  side,
  handlers,
}: {
  edge: LensEdge;
  view: LensView;
  side: XRaySide;
  handlers: PanelHandlers;
}) {
  const byId = new Map(view.cards.map((card) => [card.id, card]));
  const from = byId.get(edge.from);
  const to = byId.get(edge.to);
  const style = edgeStyle(edge);
  const { link } = edge;
  // The call site opens only from revision (or observed) evidence; a current-index line may have moved.
  const site = !edge.fold && link.site && link.exactness !== "current_index" && link.exactness !== "unknown" ? link.site : null;
  const siteSelection: ReviewMapSelection | null = site ? { ...link.selection, path: site.path, line: site.line } : null;
  const sideWord = side === "before" ? "Before" : "After";
  return (
    <aside className="xr-panel" aria-label="Selected X-Ray arrow" data-testid="xray-edge-detail" data-observed={edge.observed ? "true" : "false"} aria-live="polite">
      <div className="xr-panel-kicker">{`Arrow · ${sideWord}`}</div>
      <h4 className="xr-panel-title xr-panel-arrow">
        <Button unstyled className="xr-panel-end" onClick={() => from && handlers.onSelectCard(from.id)}>{from?.title ?? edge.from}</Button>
        <LineSample line={style.line} tone={style.tone} />
        <Button unstyled className="xr-panel-end" onClick={() => to && handlers.onSelectCard(to.id)}>{to?.title ?? edge.to}</Button>
      </h4>
      <div className="xr-panel-basis" data-testid="xray-detail-order" data-observed={edge.observed ? "true" : "false"}>
        <LineSample line={style.line} tone="unchanged" />
        <span>
          {edge.observed
            ? edge.sequence !== null ? `Observed: call #${edge.sequence} in this revision's recorded ${side === "before" ? "base" : "head"} run` : `Observed in this revision's recorded ${side === "before" ? "base" : "head"} run; its position is not recorded`
            : edge.fold ? `Static: ${plural(edge.fold, "relationship")} into folded files, not all observed` : "Static: a relationship in the code, not observed in a recorded run"}
        </span>
      </div>
      {!edge.fold && (
        <div className="xr-panel-section">
          <h5 className="xr-panel-heading">Functions</h5>
          {link.trace ? (
            <span className="break-words font-mono text-caption text-foreground" data-testid="xray-detail-trace" title={link.trace.recordId || link.trace.evidenceRef}>
              {`${link.trace.callerFn || "?"}() → ${link.trace.calleeFn || "?"}()`}
            </span>
          ) : link.landsOn.length ? (
            <span className="break-words font-mono text-caption text-subtle">{`Lands on ${link.landsOn.join(", ")}`}</span>
          ) : (
            <span className="text-caption text-muted-foreground">Which function calls which is not recorded for this relationship.</span>
          )}
          {link.trace && (
            <span className="break-words font-mono text-micro text-muted-foreground">
              {[link.trace.recipe, link.trace.evidenceRef].filter(Boolean).join(" · ")}
            </span>
          )}
          {link.trace && <span className="text-caption text-muted-foreground">The run records the call, not what it carried.</span>}
        </div>
      )}
      <div className="flex flex-wrap gap-1.5">
        {site && siteSelection && (
          <Button variant="outline" size="xs" data-testid="xray-detail-site" tooltip={`Open the call site, ${site.path}:${site.line}`} onClick={() => handlers.onOpenSource(siteSelection)} icon={<ExternalLink size={14} />}>
            {`${site.path.split("/").pop()}:${site.line}`}
          </Button>
        )}
        {link.trace && handlers.onOpenEvidence && (
          <Button variant="ghost" size="xs" data-testid="xray-detail-evidence" onClick={() => to?.selection && handlers.onOpenEvidence?.(to.selection)}>
            See the recorded run
          </Button>
        )}
      </div>
      <div className="xr-panel-section" data-testid="xray-detail-link" data-order={edge.observed ? "observed" : "reachable"}>
        <h5 className="xr-panel-heading">{edge.members.length > 1 ? `Relationships · ${edge.members.length}` : "Relationship"}</h5>
        <ul className="xr-panel-list">
          {edge.members.slice(0, 8).map((member, index) => (
            <li key={`${member.kind}:${index}`} className="text-caption text-subtle">
              {[
                member.kindLabel,
                member.change === "context_only" ? "not compared" : member.change === "unknown" ? "change unknown" : member.change,
                member.observed ? "observed" : member.exactness === "snapshot" ? "" : member.exactnessLabel,
              ].filter(Boolean).join(" · ")}
            </li>
          ))}
          {edge.members.length > 8 && <li className="text-caption text-muted-foreground">{`and ${edge.members.length - 8} more`}</li>}
        </ul>
        {link.weights && link.change === "changed" && !edge.fold && (
          <span className="text-caption text-p1">{`${link.weights.base} → ${link.weights.head} references`}</span>
        )}
        {link.handoff && (
          <span className="text-caption text-p1" data-testid="xray-detail-handoff">
            {`⇢ ${link.handoff.label}, going by its name. What runs after it is not followed here.`}
          </span>
        )}
      </div>
      {handlers.renderBehavior?.(edgePaths(view, edge), "arrow")}
    </aside>
  );
}
