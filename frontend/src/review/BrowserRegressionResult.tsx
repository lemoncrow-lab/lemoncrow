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
  if (error) return <div className="text-[10px] text-amber-300">Screenshots unavailable: {error}</div>;
  if (!urls[1]) return <div className="text-[10px] text-neutral-600">Loading frozen screenshots…</div>;
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
    <figure className="overflow-hidden rounded border border-neutral-800 bg-neutral-950">
      <figcaption className="border-b border-neutral-800 px-2.5 py-1.5 font-mono text-[9px] text-neutral-500">
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
  if (error) return <div className="text-[10px] text-amber-300">Recordings unavailable: {error}</div>;
  const panes: Array<{ label: string; url: string; artifact: ReviewBrowserArtifact }> = [];
  if (before && baseline) panes.push({ label: `Before · ${runLabel(baseline)}`, url: urls[0] ?? "", artifact: before });
  panes.push({ label: `After · ${runLabel(run)}`, url: urls[1] ?? "", artifact: after });
  return (
    <div className={`grid gap-2 ${panes.length > 1 ? "grid-cols-2" : "grid-cols-1"}`} aria-label="Paired browser recordings">
      {panes.map((pane) => (
        <figure key={pane.artifact.evidence_id} className="min-w-0 overflow-hidden rounded border border-neutral-800 bg-neutral-950">
          <figcaption className="truncate border-b border-neutral-800 px-2 py-1 font-mono text-[9px] text-neutral-500" title={pane.artifact.digest}>
            {pane.label} · {shortDigest(pane.artifact.digest)}
          </figcaption>
          {pane.url ? (
            <video src={pane.url} controls preload="metadata" className="block w-full bg-black" aria-label={pane.label} />
          ) : (
            <div className="px-2 py-3 text-[10px] text-neutral-600">Loading recording…</div>
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
      <button type="button" onClick={() => void download()} className="text-sky-300 hover:underline">
        {label} trace
      </button>
      <span className="font-mono text-neutral-600" title={trace.digest}>{shortDigest(trace.digest)}</span>
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
        <div className={`text-[10px] ${statusTone(run.status)}`}>{run.status_reasons.join(" · ")}</div>
      )}
      {run.assertions.length > 0 && (
        <div>
          <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-neutral-700">
            Assertions · {passing.length}/{run.assertions.length} passed
          </div>
          <ul className="space-y-0.5">
            {[...failing, ...passing].slice(0, 24).map((row, index) => (
              <li key={`${row.name}:${index}`} className="flex gap-2 text-[10px]">
                <span className={`w-12 shrink-0 font-mono ${statusTone(row.status)}`}>{row.status}</span>
                <span className="min-w-0 flex-1 text-neutral-300">
                  {row.name}
                  {(row.expected || row.actual) && (
                    <span className="text-neutral-600"> — expected {row.expected || "∅"}, observed {row.actual || "∅"}</span>
                  )}
                  {row.detail && <span className="block whitespace-pre-wrap break-words font-mono text-[9px] text-neutral-600">{row.detail}</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {(run.navigation.status != null || run.navigation.ok != null) && (
        <div className="text-[10px] text-neutral-500">
          Navigation {run.navigation.status != null ? `HTTP ${run.navigation.status}` : run.navigation.ok ? "ok" : "failed"}
          {run.navigation.final_url && <span className="font-mono text-neutral-600"> → {run.navigation.final_url}</span>}
        </div>
      )}
      {(consoleErrors.length > 0 || run.runtime_errors.length > 0) && (
        <div>
          <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-neutral-700">Console / runtime errors</div>
          <ul className="space-y-0.5 font-mono text-[9px] text-rose-300/80">
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
        <div className="text-[10px] text-neutral-500">
          Visual difference {run.visual.diff_ratio.toFixed(4)}
          {run.visual.threshold != null
            ? ` · configured threshold ${run.visual.threshold.toFixed(4)}`
            : " · informational (no configured threshold)"}
        </div>
      )}
      {run.observations.length > 0 && (
        <div className="border-l border-neutral-800 pl-2">
          <div className="text-[9px] font-semibold uppercase tracking-wider text-neutral-700">Observations · not verification</div>
          {run.observations.slice(0, 6).map((row, index) => (
            <div key={`obs:${index}`} className="text-[10px] leading-4 text-neutral-500">{row}</div>
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
      className="mt-1.5 rounded border border-neutral-800/80 bg-neutral-950/60"
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer px-2 py-1.5 text-[10px] text-neutral-400 hover:text-neutral-100">
        What the browser observed · <span className="font-mono text-neutral-600">{browser.route}</span>
        {environment && <span className="text-neutral-600"> · {environment}</span>}
      </summary>
      {open && (
        <div className="space-y-3 border-t border-neutral-800/80 px-2.5 py-2.5">
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[9px] text-neutral-600">
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
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px]">
            <TraceLink run={run} label="After" />
            {baseline && <TraceLink run={baseline} label="Before" />}
          </div>
          <div className="text-[9px] leading-4 text-neutral-600">
            {baseline
              ? `Baseline: ${runLabel(baseline)} (${baseline.relation === "base" ? "pre-change side of this Snapshot" : "earlier Snapshot"}) · ${baseline.status}${baseline.observed_at ? ` · observed ${new Date(baseline.observed_at).toLocaleString()}` : ""}`
              : "No frozen baseline run was recorded for this scenario and viewport."}
          </div>
        </div>
      )}
    </details>
  );
}
