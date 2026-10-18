import { Button as UiButton } from "../components/ui/button";
import { useEffect, useState } from "react";

import ImageOverlayCompare from "./ImageOverlayCompare";
import { fetchReviewEvidenceContent } from "./reviewApi";
import type { ReviewBrowserArtifact, ReviewBrowserRun, ReviewVerificationCheck } from "./types";

function shortDigest(value: string): string {
  return value.length > 14 ? `${value.slice(0, 12)}…` : value;
}

function runLabel(run: ReviewBrowserRun): string {
  const snapshot = run.snapshot_number != null ? `Snapshot ${run.snapshot_number}` : `Snapshot ${run.snapshot_id}`;
  return run.side === "old" ? `${snapshot} · base` : snapshot;
}

function statusTone(status: string): string {
  if (status === "PASS") return "text-emerald-300";
  if (status === "FAIL") return "text-rose-300";
  return "text-amber-300";
}

/** Object URLs for frozen evidence bytes, fetched only while the result is expanded. */
function useEvidenceUrls(artifacts: Array<ReviewBrowserArtifact | undefined>): { urls: string[]; error: string } {
  const ids = artifacts.map((item) => item?.evidence_id ?? "");
  const key = ids.join("\u0000");
  const [urls, setUrls] = useState<string[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    const created: string[] = [];
    setUrls([]);
    setError("");
    void Promise.all(ids.map((id) => (id ? fetchReviewEvidenceContent(id) : Promise.resolve(null))))
      .then((blobs) => {
        if (!live) return;
        const next = blobs.map((blob) => {
          if (!blob) return "";
          const url = URL.createObjectURL(blob);
          created.push(url);
          return url;
        });
        setUrls(next);
      })
      .catch((reason: unknown) => {
        if (live) setError(String(reason instanceof Error ? reason.message : reason));
      });
    return () => {
      live = false;
      created.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [key]);
  return { urls, error };
}

function ScreenshotPair({ run, baseline }: { run: ReviewBrowserRun; baseline: ReviewBrowserRun | null }) {
  const after = run.artifacts.screenshot;
  const before = baseline?.artifacts.screenshot;
  const { urls, error } = useEvidenceUrls([before, after]);
  if (!after) return null;
  if (error) return <div className="text-caption text-amber-300">Screenshots unavailable: {error}</div>;
  if (!urls[1]) return <div className="text-caption text-muted-foreground">Loading frozen screenshots…</div>;
  if (before && baseline && urls[0]) {
    return (
      <ImageOverlayCompare
        beforeSrc={urls[0]}
        afterSrc={urls[1]}
        beforeLabel="Before"
        afterLabel="After"
      />
    );
  }
  return (
    <figure className="overflow-hidden rounded border border-border bg-background">
      <figcaption className="border-b border-border px-2.5 py-1.5 font-mono text-micro text-muted-foreground">
        {runLabel(run)} · {shortDigest(after.digest)} · no baseline screenshot recorded
      </figcaption>
      <img src={urls[1]} alt={`Browser screenshot for ${runLabel(run)}`} className="block h-auto w-full bg-white" />
    </figure>
  );
}

function RecordingPair({ run, baseline }: { run: ReviewBrowserRun; baseline: ReviewBrowserRun | null }) {
  const after = run.artifacts.video;
  const before = baseline?.artifacts.video;
  const { urls, error } = useEvidenceUrls([before, after]);
  if (!after) return null;
  if (error) return <div className="text-caption text-amber-300">Recordings unavailable: {error}</div>;
  const panes: Array<{ label: string; url: string; artifact: ReviewBrowserArtifact }> = [];
  if (before && baseline) panes.push({ label: `Before · ${runLabel(baseline)}`, url: urls[0] ?? "", artifact: before });
  panes.push({ label: `After · ${runLabel(run)}`, url: urls[1] ?? "", artifact: after });
  return (
    <div className={`grid gap-2 ${panes.length > 1 ? "grid-cols-2" : "grid-cols-1"}`} aria-label="Paired browser recordings">
      {panes.map((pane) => (
        <figure key={pane.artifact.evidence_id} className="min-w-0 overflow-hidden rounded border border-border bg-background">
          <figcaption className="truncate border-b border-border px-2 py-1 font-mono text-micro text-muted-foreground" title={pane.artifact.digest}>
            {pane.label} · {shortDigest(pane.artifact.digest)}
          </figcaption>
          {pane.url ? (
            <video src={pane.url} controls preload="metadata" className="block w-full bg-black" aria-label={pane.label} />
          ) : (
            <div className="px-2 py-3 text-caption text-muted-foreground">Loading recording…</div>
          )}
        </figure>
      ))}
    </div>
  );
}

function TraceLink({ run, label }: { run: ReviewBrowserRun; label: string }) {
  const trace = run.artifacts.trace;
  const [error, setError] = useState("");
  if (!trace) return null;
  const download = async () => {
    setError("");
    try {
      const blob = await fetchReviewEvidenceContent(trace.evidence_id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `trace-${trace.digest.slice(0, 12) || trace.evidence_id}.zip`;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    }
  };
  return (
    <span className="inline-flex items-center gap-1.5">
      <UiButton type="button" variant="link" onClick={() => void download()} className="text-sky-300 hover:text-sky-200 hover:underline">
        {label} trace
      </UiButton>
      <span className="font-mono text-muted-foreground" title={trace.digest}>{shortDigest(trace.digest)}</span>
      {error && <span className="text-amber-300">{error}</span>}
    </span>
  );
}

function RunFacts({ run }: { run: ReviewBrowserRun }) {
  const failing = run.assertions.filter((row) => row.status !== "PASS");
  const passing = run.assertions.filter((row) => row.status === "PASS");
  const consoleErrors = run.console.filter((row) => row.level === "error");
  return (
    <div className="space-y-2">
      {run.status_reasons.length > 0 && (
        <div className={`text-caption ${statusTone(run.status)}`}>{run.status_reasons.join(" · ")}</div>
      )}
      {run.assertions.length > 0 && (
        <div>
          <div className="mb-1 text-micro font-semibold uppercase tracking-wider text-muted-foreground">
            Assertions · {passing.length}/{run.assertions.length} passed
          </div>
          <ul className="space-y-0.5">
            {[...failing, ...passing].slice(0, 24).map((row, index) => (
              <li key={`${row.name}:${index}`} className="flex gap-2 text-caption">
                <span className={`w-12 shrink-0 font-mono ${statusTone(row.status)}`}>{row.status}</span>
                <span className="min-w-0 flex-1 text-neutral-300">
                  {row.name}
                  {(row.expected || row.actual) && (
                    <span className="text-muted-foreground"> — expected {row.expected || "∅"}, observed {row.actual || "∅"}</span>
                  )}
                  {row.detail && <span className="block whitespace-pre-wrap break-words font-mono text-micro text-muted-foreground">{row.detail}</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {(run.navigation.status != null || run.navigation.ok != null) && (
        <div className="text-caption text-muted-foreground">
          Navigation {run.navigation.status != null ? `HTTP ${run.navigation.status}` : run.navigation.ok ? "ok" : "failed"}
          {run.navigation.final_url && <span className="font-mono text-muted-foreground"> → {run.navigation.final_url}</span>}
        </div>
      )}
      {(consoleErrors.length > 0 || run.runtime_errors.length > 0) && (
        <div>
          <div className="mb-1 text-micro font-semibold uppercase tracking-wider text-muted-foreground">Console / runtime errors</div>
          <ul className="space-y-0.5 font-mono text-micro text-rose-300/80">
            {consoleErrors.slice(0, 8).map((row, index) => (
              <li key={`console:${index}`} className="break-words">{row.text}{row.location ? ` (${row.location})` : ""}</li>
            ))}
            {run.runtime_errors.slice(0, 8).map((row, index) => (
              <li key={`runtime:${index}`} className="break-words">{row.message}</li>
            ))}
          </ul>
        </div>
      )}
      {run.visual.diff_ratio != null && (
        <div className="text-caption text-muted-foreground">
          Visual difference {run.visual.diff_ratio.toFixed(4)}
          {run.visual.threshold != null
            ? ` · configured threshold ${run.visual.threshold.toFixed(4)}`
            : " · informational (no configured threshold)"}
        </div>
      )}
      {run.observations.length > 0 && (
        <div className="border-l border-border pl-2">
          <div className="text-micro font-semibold uppercase tracking-wider text-muted-foreground">Observations · not verification</div>
          {run.observations.slice(0, 6).map((row, index) => (
            <div key={`obs:${index}`} className="text-caption leading-4 text-muted-foreground">{row}</div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * What the browser actually observed for one scenario on one exact Snapshot,
 * beside the frozen baseline run. Loaded only when expanded.
 */
export default function BrowserRegressionResult({ check }: { check: ReviewVerificationCheck }) {
  const [open, setOpen] = useState(false);
  const browser = check.browser;
  const run = browser?.run ?? null;
  if (!browser || !run) return null;
  const baseline = browser.baseline ?? null;
  const viewport = run.viewport.width && run.viewport.height ? `${run.viewport.width}×${run.viewport.height}` : "";
  const environment = [run.environment.browser, run.environment.browser_version, viewport, run.viewport.device]
    .filter(Boolean)
    .join(" ");
  return (
    <details
      className="mt-1.5 rounded border border-border/80 bg-background/60"
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer px-2 py-1.5 text-caption text-neutral-400 hover:text-foreground">
        What the browser observed · <span className="font-mono text-muted-foreground">{browser.route}</span>
        {environment && <span className="text-muted-foreground"> · {environment}</span>}
      </summary>
      {open && (
        <div className="space-y-3 border-t border-border/80 px-2.5 py-2.5">
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-micro text-muted-foreground">
            <span>Tested {runLabel(run)}</span>
            <span>{run.provider} · {run.source}</span>
            {run.observed_at && <span>observed {new Date(run.observed_at).toLocaleString()}</span>}
            {run.target_url && <span className="font-mono">{run.target_url}</span>}
            {run.run_ref && <span className="truncate font-mono" title={run.run_ref}>{run.run_ref}</span>}
            <span className="font-mono" title={run.record_digest}>result {shortDigest(run.record_digest)}</span>
          </div>
          <RunFacts run={run} />
          <ScreenshotPair run={run} baseline={baseline} />
          <RecordingPair run={run} baseline={baseline} />
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-caption">
            <TraceLink run={run} label="After" />
            {baseline && <TraceLink run={baseline} label="Before" />}
          </div>
          <div className="text-micro leading-4 text-muted-foreground">
            {baseline
              ? `Baseline: ${runLabel(baseline)} (${baseline.relation === "base" ? "pre-change side of this Snapshot" : "earlier Snapshot"}) · ${baseline.status}${baseline.observed_at ? ` · observed ${new Date(baseline.observed_at).toLocaleString()}` : ""}`
              : "No frozen baseline run was recorded for this scenario and viewport."}
          </div>
        </div>
      )}
    </details>
  );
}
