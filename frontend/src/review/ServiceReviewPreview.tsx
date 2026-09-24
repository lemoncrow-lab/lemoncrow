import type { MarkdownRenderMode } from "./MarkdownReviewPreview";
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
      <div className="flex min-h-28 items-center justify-center border border-neutral-800 bg-neutral-950 text-[10px] text-neutral-600">
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
    <div className="min-h-28 border border-neutral-800 bg-neutral-950">
      <div className="flex items-center gap-2 border-b border-neutral-800 px-3 py-2 text-[10px]">
        <span className="font-mono uppercase tracking-[0.12em] text-neutral-600">{label}</span>
        <span className={tone}>{result.status}</span>
        <span className="ml-auto font-mono text-neutral-600">{result.duration_ms} ms</span>
      </div>
      <div className="px-3 py-2 text-[10px] text-neutral-400">{result.summary}</div>
      {services.length > 0 && (
        <div className="border-t border-neutral-900 px-3 py-2 font-mono text-[10px] text-neutral-500">
          {services.join(" · ")}
        </div>
      )}
      {result.output && (
        <pre className="max-h-52 overflow-auto whitespace-pre-wrap border-t border-neutral-900 p-3 font-mono text-[10px] leading-4 text-neutral-400">
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
  return (
    <div
      className="bg-surface-sunken p-3"
      data-testid={`service-preview:${surface.id}`}
      onMouseEnter={onActivate}
      onFocusCapture={onActivate}
      data-active={active ? "true" : "false"}
    >
      <div className="mb-3 flex items-center gap-3 border-b border-neutral-900 pb-2.5">
        <div className="min-w-0 flex-1">
          <div className="font-mono text-[11px] text-neutral-200">{serviceName(surface)}</div>
          <div className="mt-1 text-[10px] text-neutral-600">
            {surface.runtime ? `runtime ${surface.runtime}` : "no runtime bound"}{run?.new?.runner || run?.old?.runner ? ` · ${runner}` : ""}
          </div>
        </div>
        {executable && (
          <button
            type="button"
            disabled={running}
            onClick={() => onRun(mode === "compare")}
            className="review-toolbar-button h-7 disabled:opacity-40"
          >
            {running ? "Running…" : mode === "compare" ? "Run compare" : "Run check"}
          </button>
        )}
      </div>
      {!executable && (
        <div className="mb-3 text-[10px] leading-4 text-amber-300/70">
          Execution needs a revision-pinned runtime. Service metadata remains reviewable.
        </div>
      )}
      {run?.error && <div className="mb-3 border border-rose-950 bg-rose-950/10 px-3 py-2 text-[10px] text-rose-300">{run.error}</div>}
      {mode === "compare" ? (
        <div className="grid grid-cols-2 gap-3">
          <ResultPane label="Before" result={run?.old} />
          <ResultPane label="After" result={run?.new} />
        </div>
      ) : (
        <ResultPane label="Result" result={run?.new} />
      )}
    </div>
  );
}
