import { Button as UiButton } from "../components/ui/button";
import type { MarkdownRenderMode } from "./MarkdownReviewPreview";
import { isUnavailableRunnerError, unavailableRunnerMessage } from "./surfaceRunPresentation";
import type { ReviewSurfaceInfo, ReviewSurfaceRunResult } from "./types";

interface ServiceReviewPreviewProps {
  surface: ReviewSurfaceInfo;
  mode: MarkdownRenderMode;
  run?: { old?: ReviewSurfaceRunResult; new?: ReviewSurfaceRunResult; error?: string };
  running?: boolean;
  active?: boolean;
  onActivate?(): void;
  onRun(compare: boolean): void;
}

function serviceName(surface: ReviewSurfaceInfo): string {
  const value = surface.metadata.service;
  return typeof value === "string" && value ? value : surface.title || surface.id;
}

function ResultPane({ label, result }: { label: string; result?: ReviewSurfaceRunResult }) {
  if (!result) {
    return (
      <div className="review-surface-result-empty flex min-h-28 items-center justify-center border border-border bg-background text-caption text-muted-foreground">
        Not run yet
      </div>
    );
  }
  const tone = result.status === "passed"
    ? "text-emerald-300"
    : result.status === "failed"
      ? "text-rose-300"
      : "text-amber-300";
  const services = Array.isArray(result.data?.services) ? result.data.services.map(String) : [];
  return (
    <div className="min-h-28 border border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-caption">
        <span className="font-mono uppercase tracking-[0.12em] text-muted-foreground">{label}</span>
        <span className={tone}>{result.status}</span>
        <span className="ml-auto font-mono text-muted-foreground">{result.duration_ms} ms</span>
      </div>
      <div className="px-3 py-2 text-caption text-neutral-400">{result.summary}</div>
      {services.length > 0 && (
        <div className="border-t border-neutral-900 px-3 py-2 font-mono text-caption text-muted-foreground">
          {services.join(" · ")}
        </div>
      )}
      {result.output && (
        <pre className="max-h-52 overflow-auto whitespace-pre-wrap border-t border-neutral-900 p-3 font-mono text-caption leading-4 text-neutral-400">
          {result.output}
        </pre>
      )}
    </div>
  );
}

export default function ServiceReviewPreview({
  surface,
  mode,
  run,
  running = false,
  active = false,
  onActivate,
  onRun,
}: ServiceReviewPreviewProps) {
  const executable = surface.capabilities.includes("execute") && Boolean(surface.runtime);
  const runner = run?.new?.runner || run?.old?.runner || "runtime";
  const unavailableRunner = isUnavailableRunnerError(run?.error);
  return (
    <div
      className="review-service-preview bg-surface-sunken p-3"
      data-testid={`service-preview:${surface.id}`}
      onMouseEnter={onActivate}
      onFocusCapture={onActivate}
      data-active={active ? "true" : "false"}
    >
      <div className="review-service-preview-heading mb-3 flex items-center gap-3 border-b border-neutral-900 pb-2.5">
        <div className="min-w-0 flex-1">
          <div className="font-mono text-label text-neutral-200">{serviceName(surface)}</div>
          <div className="mt-1 text-caption text-muted-foreground">
            {surface.runtime ? `runtime ${surface.runtime}` : "no runtime bound"}{run?.new?.runner || run?.old?.runner ? ` · ${runner}` : ""}
          </div>
        </div>
        {executable && (
          <UiButton
            type="button"
            disabled={running || unavailableRunner}
            onClick={() => onRun(mode === "compare")}
            className="h-7"
          >
            {running ? "Running…" : mode === "compare" ? "Run compare" : "Run check"}
          </UiButton>
        )}
      </div>
      {!executable && (
        <div className="mb-3 text-caption leading-4 text-amber-300/70">
          Execution needs a Snapshot-pinned runtime. Service metadata remains reviewable.
        </div>
      )}
      {run?.error && (
        <div className={`review-surface-run-message mb-3 border px-3 py-2 text-caption ${unavailableRunner
          ? "border-amber-900/55 bg-amber-950/10 text-amber-300"
          : "border-rose-950 bg-rose-950/10 text-rose-300"}`}>
          {unavailableRunner ? unavailableRunnerMessage() : run.error}
        </div>
      )}
      {!unavailableRunner && (mode === "compare" ? (
        <div className="review-surface-result-grid grid grid-cols-2 gap-3">
          <ResultPane label="Before" result={run?.old} />
          <ResultPane label="After" result={run?.new} />
        </div>
      ) : (
        <ResultPane label="Result" result={run?.new} />
      ))}
    </div>
  );
}
