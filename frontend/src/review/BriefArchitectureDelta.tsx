import { useEffect, useState } from "react";
import { Network } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import type { ReviewArchitectureDelta } from "./types";

const MOVED: ReadonlySet<string> = new Set(["added", "removed", "changed"]);
const CONCERN_LIMIT = 3;

/** True only when the deterministic delta says the architecture materially moved. */
export function architectureMaterial(delta: ReviewArchitectureDelta | null | undefined): delta is ReviewArchitectureDelta {
  return Boolean(delta && delta.status !== "unavailable" && delta.summary.material);
}

/**
 * The Map node the card opens: the first Snapshot-exact component that moved,
 * else the delta's own focus, else the overview.
 */
export function architectureFocusNode(delta: ReviewArchitectureDelta): string {
  const moved = delta.graph.nodes.find((node) => node.exactness === "snapshot" && MOVED.has(String(node.delta_status ?? "")));
  return moved?.id ?? delta.graph.focus ?? "";
}

function exactnessLabel(delta: ReviewArchitectureDelta): string {
  switch (delta.summary.exactness) {
    case "snapshot":
      return "Snapshot-exact · base ↔ Snapshot";
    case "current_index":
      return "Current index · not Snapshot-exact";
    case "inferred":
      return "Inferred · not Snapshot-exact";
    default:
      return "Exactness unavailable";
  }
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

/**
 * Brief §5.1 item 2: the Architecture Delta, shown only when the deterministic
 * delta reports material movement. Its one action opens the Map's
 * Architecture Δ at the moved component; the Brief stays on the backtrail.
 */
export default function BriefArchitectureDelta({
  load,
  onOpen,
  eager = true,
}: {
  load: () => Promise<ReviewArchitectureDelta | null>;
  onOpen: (nodeId: string) => void;
  eager?: boolean;
}) {
  const [requestedLoad, setRequestedLoad] = useState<typeof load | null>(null);
  const [requestNumber, setRequestNumber] = useState(0);
  const [result, setResult] = useState<{
    load: typeof load;
    state: "idle" | "loading" | "loaded" | "failed";
    delta: ReviewArchitectureDelta | null;
  }>({ load, state: eager ? "loading" : "idle", delta: null });
  // The Reader changes `load` with Snapshot identity. An older result must not
  // appear even in the render before the new effect cancels the old request.
  const state = result.load === load ? result.state : eager ? "loading" : "idle";
  const delta = result.load === load ? result.delta : null;

  useEffect(() => {
    let cancelled = false;
    if (!eager && requestedLoad !== load) {
      setResult({ load, state: "idle", delta: null });
      return () => { cancelled = true; };
    }
    setResult({ load, state: "loading", delta: null });
    Promise.resolve()
      .then(load)
      .then((next) => {
        if (!cancelled) setResult({ load, state: "loaded", delta: next });
      })
      .catch(() => {
        if (!cancelled) setResult({ load, state: "failed", delta: null });
      });
    return () => { cancelled = true; };
  }, [eager, load, requestedLoad, requestNumber]);

  const request = () => {
    setRequestedLoad(() => load);
    setRequestNumber((current) => current + 1);
  };

  if (!eager && !Boolean(architectureMaterial(delta))) {
    return (
      <section aria-label="Architecture delta" className="border-t border-border/70 py-3">
        {state === "loaded" && delta?.status !== "unavailable" && delta !== null ? (
          <p className="text-caption text-muted-foreground" role="status">No material architecture changes</p>
        ) : (
          <>
            <UiButton variant="ghost" size="compact" type="button" onClick={request} disabled={state === "loading"}>
              {state === "loading" ? "Checking architecture changes…" : "Check architecture changes"}
            </UiButton>
            {(state === "failed" || state === "loaded") && (
              <p className="text-caption text-muted-foreground" role="status">Architecture details could not load. Try again.</p>
            )}
          </>
        )}
      </section>
    );
  }
  if (state === "loading" || !architectureMaterial(delta)) return null;
  const summary = delta.summary;
  const concerns = summary.important_concerns.slice(0, CONCERN_LIMIT);
  const relationshipParts = [
    summary.added_edges ? `${summary.added_edges} added` : "",
    summary.removed_edges ? `${summary.removed_edges} removed` : "",
    summary.modified_edges ? `${summary.modified_edges} changed` : "",
  ].filter(Boolean);
  return (
    <section
      aria-label="Architecture delta"
      className="flex flex-col gap-1.5 border-t border-border/70 py-3"
      data-testid="brief-architecture-delta"
    >
      <div className="flex items-center gap-2 text-label font-medium text-neutral-200">
        <Network size={14} aria-hidden="true" className="text-sky-300" />
        <span>Architecture moved</span>
      </div>
      <p className="text-caption text-neutral-300">
        {`${plural(summary.changed_components, "component")} · ${plural(summary.changed_edges, "relationship")} changed`}
        {relationshipParts.length > 0 ? ` (${relationshipParts.join(", ")})` : ""}
      </p>
      {concerns.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4 text-caption text-amber-200">
          {concerns.map((concern) => <li key={concern}>{concern}</li>)}
        </ul>
      )}
      <p className="text-micro text-muted-foreground" data-testid="brief-architecture-exactness">
        {exactnessLabel(delta)}{delta.status === "degraded" ? " · partial" : ""}
      </p>
      {summary.degraded.length > 0 && (
        <p className="text-micro text-amber-300">{`Not covered: ${summary.degraded.join(" · ")}`}</p>
      )}
      <div>
        <UiButton variant="outline" size="compact" type="button" onClick={() => onOpen(architectureFocusNode(delta))}>
          Open architecture view
        </UiButton>
      </div>
    </section>
  );
}
