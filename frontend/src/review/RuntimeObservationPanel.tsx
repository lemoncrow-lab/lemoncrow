import { useCallback, useEffect, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import { PanelCard } from "./ContextSections";
import {
  fetchRuntimeArtifact,
  fetchRuntimeObservation,
  postRuntimeObservationRun,
  type EffectChange,
  type ObservationEntryRow,
  type RenderResult,
  type RuntimeObservationProjection,
  type SideEffectResult,
} from "./runtimeObservationApi";

const ENTRY_LABEL: Record<string, string> = {
  "side_effects.runtime": "Runtime side effects",
  "visual.dom_a11y": "DOM & accessibility diff",
  "visual.matrix": "Viewport × theme matrix",
};

const CATEGORY_LABEL: Record<string, string> = {
  network: "Network",
  file_write: "File written",
  process: "Process",
  db_query: "Query",
  schema_mutation: "Schema change",
  console_error: "Runtime error",
};

const ASPECT_LABEL: Record<string, string> = {
  screenshot: "Screenshot",
  dom: "DOM",
  a11y: "Accessibility",
  network: "Page network",
  console: "Console",
  layout_shift: "Layout shift",
  performance: "Performance",
  status: "Render status",
};

const CHANGE_TONE: Record<string, string> = {
  added: "text-amber-300",
  removed: "text-sky-300",
  changed: "text-amber-300",
};

function changeText(change: EffectChange): string {
  const shown = change.head ?? change.base;
  if (change.category === "db_query" && change.change === "changed" && change.base && change.head) {
    return `${change.base.count} → ${change.head.count}`;
  }
  return shown?.outcome ? `${change.change} · ${shown.outcome}` : change.change;
}

function SideEffectCard({ result }: { result: SideEffectResult }) {
  const title = result.recipe.title || result.recipe.id;
  const tier = result.sides.head?.observation?.tier ?? result.sides.base?.observation?.tier ?? "";
  const queries = result.diff.query_count;
  return (
    <div className="border-t border-border/60 py-2 first:border-t-0" data-testid={`side-effects-${result.recipe.id}`}>
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-label text-neutral-200">{title}</span>
        <span className="text-micro text-muted-foreground">
          {result.diff.comparable ? `${result.diff.changes.length} difference(s)` : "not comparable"}
        </span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-2 text-micro text-muted-foreground">
        <span>observed base vs head</span>
        {tier && <span>{tier === "strace" ? "syscall tracer" : tier === "hooks" ? "Python hooks" : tier}</span>}
        {result.executor_class && <span>{result.executor_class.replace("_", " ")}</span>}
        {queries && queries.base !== queries.head && (
          <span className="text-amber-300/80">
            {queries.base} → {queries.head} queries
          </span>
        )}
      </div>
      {!result.diff.comparable && (
        <div className="text-micro text-amber-300/80" data-testid="side-effects-not-comparable">
          Not comparable ({result.diff.differences.join(", ") || "environment"}): no difference is claimed.
        </div>
      )}
      {result.diff.comparable && result.diff.changes.length === 0 && (
        <div className="text-micro text-muted-foreground">No runtime side-effect difference ({result.diff.unchanged} unchanged).</div>
      )}
      <div className="mt-1 space-y-0.5">
        {result.diff.changes.map((change) => (
          <div key={`${change.category}:${change.identity}`} className="flex items-baseline gap-2 text-micro" data-testid="side-effect-change">
            <span className="shrink-0 text-muted-foreground">{CATEGORY_LABEL[change.category] ?? change.category}</span>
            <span className="min-w-0 flex-1 truncate font-mono text-neutral-300" title={(change.head ?? change.base)?.detail ?? change.identity}>
              {change.identity}
            </span>
            <span className={`shrink-0 ${CHANGE_TONE[change.change] ?? "text-muted-foreground"}`}>{changeText(change)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RenderCard({
  result,
  onOpen,
}: {
  result: RenderResult;
  onOpen: (ref: string) => void;
}) {
  const budget = result.reasons.find((reason) => reason.startsWith("Limited by budget"));
  const depth = result.reasons.find((reason) => reason.startsWith("Limited by depth"));
  return (
    <div className="border-t border-border/60 py-2 first:border-t-0" data-testid={`render-${result.entry}`}>
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-label text-neutral-200">{ENTRY_LABEL[result.entry] ?? result.entry}</span>
        <span className="text-micro text-muted-foreground">
          {result.comparable ? `${result.pages.length} page(s) differ` : "not comparable"}
        </span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-2 text-micro text-muted-foreground">
        <span>{result.plan.cells.length} cell(s)</span>
        {result.browser && <span className="truncate">{result.browser}</span>}
        {budget && <span className="text-amber-300/80">{budget}</span>}
        {depth && <span className="text-amber-300/80">{depth}</span>}
      </div>
      {result.selection.note && (
        <div className={`text-micro ${result.selection.mapping === "unknown" ? "text-amber-300/80" : "text-muted-foreground"}`} data-testid="render-selection">
          {result.selection.note}
        </div>
      )}
      {!result.comparable && result.plan.cells.length > 0 && (
        <div className="text-micro text-amber-300/80" data-testid="render-not-comparable">
          Not comparable ({result.differences.join(", ") || "environment"}): never reported as a regression.
        </div>
      )}
      <div className="mt-1 space-y-1">
        {result.pages.map((page) => (
          <div key={page.cell.id} className="text-micro" data-testid="render-page">
            <span className="font-mono text-neutral-300">{page.cell.id}</span>{" "}
            <span className="text-amber-300">
              {Object.keys(page.changes)
                .map((aspect) => ASPECT_LABEL[aspect] ?? aspect)
                .join(" · ")}
            </span>
            {page.changes.screenshot && (
              <span className="ml-2">
                {(["base", "head"] as const).map((side) => {
                  const ref = (page.changes.screenshot?.[side] as { ref?: string } | undefined)?.ref ?? "";
                  return ref ? (
                    <UiButton key={side} type="button" size="xs" variant="ghost" onClick={() => onOpen(ref)}>
                      {side}
                    </UiButton>
                  ) : null;
                })}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function EntryAction({ entry, busy, onRun }: { entry: ObservationEntryRow; busy: boolean; onRun: (entry: string, pick: boolean) => void }) {
  if (entry.status === "run") {
    return (
      <UiButton type="button" size="xs" variant="outline" disabled={busy} onClick={() => onRun(entry.id, false)}>
        Run
      </UiButton>
    );
  }
  if (entry.status === "on_request" || entry.status === "skipped" || entry.status === "not_planned") {
    return (
      <UiButton type="button" size="xs" variant="ghost" disabled={busy} onClick={() => onRun(entry.id, true)}>
        Add &amp; run
      </UiButton>
    );
  }
  return null;
}

/**
 * Runtime side effects and rendering diffs observed in planned sandboxed runs
 * (only differences raised). Self-contained so it mounts in the Checks rail.
 */
export default function RuntimeObservationPanel({
  reviewId,
  revisionId = "",
  readOnly = false,
}: {
  reviewId: string;
  revisionId?: string;
  readOnly?: boolean;
}) {
  const [data, setData] = useState<RuntimeObservationProjection | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [preview, setPreview] = useState("");

  const load = useCallback(
    () =>
      fetchRuntimeObservation(reviewId, revisionId).then(setData, (reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "runtime observation unavailable"),
      ),
    [reviewId, revisionId],
  );

  useEffect(() => {
    let cancelled = false;
    setError("");
    fetchRuntimeObservation(reviewId, revisionId)
      .then((projection) => {
        if (!cancelled) setData(projection);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "runtime observation unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [reviewId, revisionId]);

  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview);
  }, [preview]);

  if (error) {
    return (
      <PanelCard title="Runtime & rendering">
        <div className="text-caption text-muted-foreground">Runtime observation unavailable: {error}</div>
      </PanelCard>
    );
  }
  if (!data) {
    return (
      <PanelCard title="Runtime & rendering">
        <div className="text-caption text-muted-foreground">Loading runtime observation…</div>
      </PanelCard>
    );
  }

  const snapshot = data;
  const editable = !readOnly && !snapshot.historical;

  function run(entry: string, pick: boolean) {
    setBusy(true);
    setMessage("");
    postRuntimeObservationRun(reviewId, snapshot.revision_id, entry, pick)
      .then(async (response) => {
        const limited = response.limited_by_budget ? " · Limited by budget" : "";
        setMessage(`${ENTRY_LABEL[entry] ?? entry}: ${response.executions} run, ${response.reused} reused${limited}`);
        await load();
      })
      .catch((reason: unknown) => setMessage(reason instanceof Error ? reason.message : "request failed"))
      .finally(() => setBusy(false));
  }

  function open(ref: string) {
    fetchRuntimeArtifact(reviewId, ref, snapshot.revision_id)
      .then((blob) => setPreview(URL.createObjectURL(blob)))
      .catch((reason: unknown) => setMessage(reason instanceof Error ? reason.message : "artifact unavailable"));
  }

  return (
    <PanelCard title="Runtime & rendering">
      <div className="space-y-2" data-testid="runtime-observation-panel">
        <div>
          {snapshot.entries.map((entry) => (
            <div key={entry.id} className="flex items-baseline gap-2 py-0.5 text-micro" data-testid={`observation-entry-${entry.id}`}>
              <span className="min-w-0 flex-1 truncate text-neutral-300" title={entry.reason}>
                {ENTRY_LABEL[entry.id] ?? entry.id}
              </span>
              <span className="text-muted-foreground" title={entry.reason}>
                {entry.status.replaceAll("_", " ")}
              </span>
              {editable && <EntryAction entry={entry} busy={busy} onRun={run} />}
            </div>
          ))}
        </div>
        {snapshot.side_effects.map((result) => (
          <SideEffectCard key={result.recipe.id} result={result} />
        ))}
        {snapshot.render.map((result) => (
          <RenderCard key={result.entry} result={result} onOpen={open} />
        ))}
        {preview && <img src={preview} alt="Masked render capture" className="max-h-64 rounded border border-border/60" />}
        {snapshot.side_effects.length > 0 && snapshot.limitations.length > 0 && (
          <div className="text-micro leading-relaxed text-muted-foreground" data-testid="observation-limitations">
            {snapshot.limitations.join(" ")}
          </div>
        )}
        {message && (
          <div className="text-micro text-neutral-300" role="status">
            {message}
          </div>
        )}
      </div>
    </PanelCard>
  );
}
