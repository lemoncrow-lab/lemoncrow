import { Button as UiButton } from "../components/ui/button";
import { useEffect } from "react";

import type { MarkdownRenderMode } from "./MarkdownReviewPreview";
import { prettyJsonText } from "./prettyJson";
import { isUnavailableRunnerError, unavailableRunnerMessage } from "./surfaceRunPresentation";
import type { ReviewSurfaceInfo, ReviewSurfaceRunResult } from "./types";

interface ApiReviewPreviewProps {
  surface: ReviewSurfaceInfo;
  mode: MarkdownRenderMode;
  run?: { old?: ReviewSurfaceRunResult; new?: ReviewSurfaceRunResult; error?: string };
  running?: boolean;
  active?: boolean;
  onActivate?(): void;
  onModeChange(mode: MarkdownRenderMode): void;
  onRun(compare: boolean): void;
}

function jsonish(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return prettyJsonText(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function responseBody(result: ReviewSurfaceRunResult | undefined): string {
  if (!result) return "";
  const data = result.data ?? {};
  const body = data.body ?? data.response ?? data.json;
  return body !== undefined ? jsonish(body) : result.output;
}

function ResultPane({ label, result }: { label: string; result?: ReviewSurfaceRunResult }) {
  if (!result) {
    return (
      <div className="review-surface-result-empty flex min-h-56 items-center justify-center border border-border bg-background text-caption text-muted-foreground">
        Not run yet
      </div>
    );
  }
  const tone = result.status === "passed"
    ? "text-emerald-300"
    : result.status === "failed"
      ? "text-red-300"
      : "text-amber-300";
  const statusCode = result.data?.status_code ?? result.data?.status;
  const assertions = Array.isArray(result.data?.assertions) ? result.data.assertions : [];
  const body = responseBody(result);
  return (
    <div className="min-h-56 border border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-caption">
        <span className="font-mono uppercase tracking-tag text-muted-foreground">{label}</span>
        <span className={tone}>{result.status}</span>
        {statusCode != null && <span className="font-mono text-muted-foreground">HTTP {String(statusCode)}</span>}
        <span className="font-mono text-muted-foreground" title={result.revision_id}>Snapshot {result.revision_id} · {result.side}</span>
        <span className="ml-auto font-mono text-muted-foreground">{result.duration_ms} ms</span>
      </div>
      <div className="px-3 py-2 text-caption text-muted-foreground">{result.summary}</div>
      {assertions.length > 0 && (
        <div className="border-t border-border px-3 py-2">
          <div className="mb-1 text-caption uppercase tracking-tag text-muted-foreground">Assertions</div>
          <div className="space-y-1 font-mono text-caption text-muted-foreground">
            {assertions.map((item, index) => <div key={index}>{jsonish(item)}</div>)}
          </div>
        </div>
      )}
      {body && <pre className="max-h-80 overflow-auto whitespace-pre-wrap border-t border-border p-3 font-mono text-caption leading-4 text-subtle">{body}</pre>}
    </div>
  );
}

export default function ApiReviewPreview({
  surface,
  mode,
  run,
  running = false,
  active = false,
  onActivate,
  onModeChange,
  onRun,
}: ApiReviewPreviewProps) {
  const method = String(surface.metadata.method ?? "").toUpperCase();
  const url = String(surface.metadata.url ?? surface.locator ?? "");
  const executable = surface.capabilities.includes("execute") && Boolean(surface.runtime);
  const unavailableRunner = isUnavailableRunnerError(run?.error);

  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable)) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "1") onModeChange("preview");
      else if (event.key === "2") onModeChange("compare");
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, onModeChange]);

  return (
    <div
      className="review-api-preview bg-surface-sunken p-3"
      data-testid={`api-preview:${surface.id}`}
      onMouseEnter={onActivate}
      onFocusCapture={onActivate}
    >
      <div className="review-api-preview-heading mb-3 flex items-start gap-3 border-b border-border pb-2.5">
        <span className="shrink-0 rounded border border-border px-2 py-1 font-mono text-caption font-semibold text-cyan-300">{method || "API"}</span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-mono text-label text-foreground">{url}</div>
          <div className="mt-1 text-caption text-muted-foreground">
            {surface.title}{surface.runtime ? ` · runtime ${surface.runtime}` : " · no runtime bound"}
          </div>
        </div>
        {executable && (
          <UiButton
            type="button"
            disabled={running || unavailableRunner}
            onClick={() => onRun(mode === "compare")}
            className="h-7"
          >
            {running ? "Running…" : mode === "compare" ? "Run compare" : "Run request"}
          </UiButton>
        )}
      </div>

      {!executable && (
        <div className="mb-3 text-caption leading-4 text-amber-300/70">
          Response execution needs a Snapshot-pinned runtime. The request definition is still reviewable.
        </div>
      )}
      {run?.error && (
        <div className={`review-surface-run-message mb-3 border px-3 py-2 text-caption ${unavailableRunner
          ? "border-amber-900/55 bg-amber-950/10 text-amber-300"
          : "border-red-950 bg-red-950/10 text-red-300"}`}>
          {unavailableRunner ? unavailableRunnerMessage() : run.error}
        </div>
      )}

      {!unavailableRunner && (mode === "compare" ? (
        <div className="review-surface-result-grid grid grid-cols-2 gap-3">
          <ResultPane label="Before" result={run?.old} />
          <ResultPane label="After" result={run?.new} />
        </div>
      ) : (
        <ResultPane label="Response" result={run?.new} />
      ))}
    </div>
  );
}
