/** P10-7 on the Map: the stored data path report (the Data path lens), and the on-request run while it has none. */
import { LoaderCircle, Play } from "lucide-react";
import { Button } from "../../components/ui/button";
import type { ReviewDataPathChange, ReviewDataPathFlow, ReviewDataPathReport } from "../types";
import type { DataPathGate } from "./dataPathApi";
import { dataPathUnavailableText } from "./mapFlowModel";

export type DataPathRun =
  | { state: "idle" }
  /** `spoken`: what the Map's one live region says; `summary`: the Map summary when it was set (a newer one wins). */
  | { state: "running" | "failed" | "done"; spoken: string; summary: string };

export const DATA_PATH_RUNNING = "Running the data path with Opengrep on this machine. It can take a few minutes; the map reloads when it finishes.";

const CHANGES: readonly ReviewDataPathChange[] = ["added", "removed", "rerouted"];
const CHANGE_TEXT: Record<ReviewDataPathChange, string> = { added: "Added", removed: "Removed", rerouted: "Rerouted" };
const CHANGE_MARK: Record<ReviewDataPathChange, string> = { added: "+", removed: "−", rerouted: "~" };
const CHANGE_CLASS: Record<ReviewDataPathChange, string> = { added: "text-ok", removed: "text-p0", rerouted: "text-p1" };
const SINK_TEXT: Record<string, string> = { db: "database", http: "HTTP", file: "file", log: "log" };

/** While the report holds no flows: what it says, and Run data path where the run plan runs it here. */
export function DataPathRunStrip({ report, canRun, gate, run, onRun }: {
  report: ReviewDataPathReport;
  canRun: boolean;
  /** The run plan's answer; null while it loads or when it could not be read. */
  gate: DataPathGate | null;
  run: DataPathRun;
  onRun: () => void;
}) {
  const running = run.state === "running";
  // A missing tool is a one-time setup: say so plainly and keep the install steps (URL, checksum, variable) folded.
  const setup = !running && run.state !== "failed" && report.state === "not_available" && Boolean(report.setup);
  const text = running ? DATA_PATH_RUNNING : run.state === "failed" ? run.spoken
    : setup ? "Data path needs a one-time setup on this machine before it can run." : dataPathUnavailableText(report);
  const blocked = canRun && gate !== null && !gate.runnable;
  const note = !canRun ? "This view is read-only, so the data path cannot be run from here."
    : blocked ? "It cannot run on this machine yet." : "";
  const details = [setup ? dataPathUnavailableText(report) : "", blocked ? gate.reason : ""].filter(Boolean);
  const offer = canRun && gate?.runnable === true;
  // Says why Run sits beside a greyed-out Data path lens: a finished run turns that lens on (review-12.1).
  const lensNote = offer && !running && run.state !== "failed" && !setup ? "The Data path lens needs this run first." : "";
  return <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-dashed border-border bg-card p-3" data-testid="map-data-path-run">
    <div className="min-w-0 flex-1">
      <p className={`break-words text-label ${run.state === "failed" ? "text-p0" : ""}`} data-testid="map-data-path-status">{text}</p>
      {(note || lensNote) && <p className="mt-1 break-words text-meta text-muted-foreground" data-testid="map-data-path-note">{note || lensNote}</p>}
      {details.length > 0 && <details className="mt-1 text-meta text-muted-foreground" data-testid="map-data-path-setup">
        <summary className="cursor-pointer">Setup details</summary>
        {details.map((line) => <p key={line} className="mt-1 break-all">{line}</p>)}
      </details>}
    </div>
    {(offer || running) && <Button size="sm" variant="outline" disabled={running} onClick={onRun} data-testid="map-data-path-run-button">
      {running ? <><LoaderCircle size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true"/> Running…</> : <><Play size={16} aria-hidden="true"/> Run data path</>}
    </Button>}
  </div>;
}

function FlowRow({ flow }: { flow: ReviewDataPathFlow }) {
  const routes = ([["Before", flow.base], ["After", flow.head]] as const).filter(([, route]) => route && route.path.length > 0);
  return <li className="border-b border-border/70 p-3" data-testid="map-data-path-flow">
    <div className="flex min-w-0 items-start gap-3">
      <span aria-label={CHANGE_TEXT[flow.change]} title={CHANGE_TEXT[flow.change]}
        className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-current/20 font-mono text-label font-semibold ${CHANGE_CLASS[flow.change]}`}>
        {CHANGE_MARK[flow.change]}
      </span>
      <div className="min-w-0 flex-1">
        <p className="break-all text-label font-semibold">{flow.source.symbol} → {flow.sink.symbol}</p>
        <p className="mt-0.5 break-all font-mono text-tag text-muted-foreground">{flow.source.label || "source"} at {flow.source.at} → {flow.sink.callee || "sink"} at {flow.sink.at}</p>
        <p className="mt-1 text-meta text-muted-foreground">
          {[CHANGE_TEXT[flow.change], `${SINK_TEXT[flow.kind] ?? flow.kind} sink`, flow.entry_point ? `entry ${flow.entry_point}` : "", flow.found_by].filter(Boolean).join(" · ")}
        </p>
        {routes.map(([side, route]) => <p key={side} className="mt-0.5 break-all text-meta text-muted-foreground">
          {side}: {route!.path.join(" → ")}{route!.omitted_steps > 0 ? ` (${route!.omitted_steps} steps not listed)` : ""}
        </p>)}
      </div>
    </div>
  </li>;
}

/** The Data path lens: the flows the report found added, removed and rerouted, with what it did not measure. */
export function DataPathFlows({ report }: { report: ReviewDataPathReport }) {
  const flows = CHANGES.flatMap((change) => report.flows?.[change] ?? []);
  const omitted = CHANGES.reduce((sum, change) => sum + (report.omitted?.[change] ?? 0), 0);
  const missing = report.not_measured ?? [];
  return <section aria-label="Data path flows" className="min-w-0 rounded-xl border border-border bg-card" data-testid="map-data-path-flows">
    <div className="border-b border-border p-4">
      <h2 className="text-heading font-semibold">Data path · experimental</h2>
      <p className="mt-1 text-label font-medium tabular-nums" data-testid="map-data-path-summary">{report.summary}</p>
      {report.detail && <p className="mt-1 text-meta text-muted-foreground">{report.detail}</p>}
    </div>
    {(flows.length > 0 || omitted > 0) && <ol aria-label="Data flows that changed">
      {flows.map((flow, index) => <FlowRow key={`${flow.change}:${flow.source.at}:${flow.sink.at}:${index}`} flow={flow}/>)}
      {omitted > 0 && <li className="p-3 text-meta text-muted-foreground" data-testid="map-data-path-omitted">{omitted} more {omitted === 1 ? "flow is" : "flows are"} past the listing cap and not shown.</li>}
    </ol>}
    {missing.length > 0 && <details className="border-t border-border p-4 text-meta" data-testid="map-data-path-not-measured">
      <summary className="cursor-pointer text-label font-medium">Not checked · {missing.length}</summary>
      <ul className="mt-2 space-y-1 text-muted-foreground">
        {missing.map((row, index) => <li key={`${row.scope}:${index}`} className="break-words"><span className="font-medium text-foreground">{row.scope}</span>: {row.reason}</li>)}
      </ul>
    </details>}
  </section>;
}
