import { Button as UiButton } from "../components/ui/button";
import { useEffect, useMemo, useState } from "react";
import { Download, RotateCw } from "lucide-react";

import ImageOverlayCompare from "./ImageOverlayCompare";
import { prettyJsonText } from "./prettyJson";
import { currentReviewId, fetchSurfacePreview } from "./reviewApi";
import type { ReviewSurfaceInfo, ReviewSurfacePreviewDescriptor, ReviewSurfaceRunResult } from "./types";

export type SurfacePreviewMode = "preview" | "compare";
type Side = "old" | "new";

type SideState =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; url: string; text: string; mediaType: string; filename: string; size: number };

const TEXT_DISPLAYS = new Set(["html", "text", "json"]);
const SIDE_LABEL: Record<Side, string> = { old: "Before · old side", new: "After · new side" };

/** The sides one mode needs, restricted to what the descriptor can produce. */
export function previewSides(descriptor: ReviewSurfacePreviewDescriptor, mode: SurfacePreviewMode): Side[] {
  const available = descriptor.sides.filter((side): side is Side => side === "old" || side === "new");
  if (mode === "compare") return available;
  return available.includes("new") ? ["new"] : available.slice(0, 1);
}

/** Whether a surface can offer a two-sided comparison at all. */
export function surfaceComparable(surface: ReviewSurfaceInfo): boolean {
  if (surface.preview) {
    return surface.preview.status !== "unavailable"
      && surface.preview.sides.includes("old")
      && surface.preview.sides.includes("new");
  }
  return surface.capabilities.includes("execute")
    && surface.capabilities.includes("compare")
    && Boolean(surface.runtime);
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function saveBlobUrl(url: string, filename: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function blobText(blob: Blob): Promise<string> {
  if (typeof blob.text === "function") return blob.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

function useSidePreviews(surface: ReviewSurfaceInfo, revisionId: string, sides: Side[], attempt: number): Record<Side, SideState | undefined> {
  const [states, setStates] = useState<Record<Side, SideState | undefined>>({ old: undefined, new: undefined });
  const sidesKey = sides.join(",");
  const display = surface.preview?.display ?? "";
  useEffect(() => {
    let live = true;
    const urls: string[] = [];
    const wanted = sidesKey ? (sidesKey.split(",") as Side[]) : [];
    setStates({
      old: wanted.includes("old") ? { state: "loading" } : undefined,
      new: wanted.includes("new") ? { state: "loading" } : undefined,
    });
    for (const side of wanted) {
      void fetchSurfacePreview(currentReviewId(), surface.provider, surface.id, side, revisionId)
        .then(async (artifact) => {
          const text = TEXT_DISPLAYS.has(display) ? await blobText(artifact.blob) : "";
          // After cleanup ran, a URL created here would never be revoked.
          if (!live) return;
          const url = URL.createObjectURL(artifact.blob);
          urls.push(url);
          setStates((current) => ({
            ...current,
            [side]: { state: "ready", url, text, mediaType: artifact.mediaType, filename: artifact.filename, size: artifact.blob.size },
          }));
        })
        .catch((reason: unknown) => {
          if (!live) return;
          setStates((current) => ({
            ...current,
            [side]: { state: "error", message: String(reason instanceof Error ? reason.message : reason) },
          }));
        });
    }
    return () => {
      live = false;
      for (const url of urls) URL.revokeObjectURL(url);
    };
  }, [attempt, display, revisionId, sidesKey, surface.id, surface.provider]);
  return states;
}

function ArtifactBody({ display, side, value, label }: { display: string; side: Side; value: SideState & { state: "ready" }; label: string }) {
  if (display === "image") {
    return <img src={value.url} alt={`${label} · ${side} side`} className="block max-w-full bg-white" />;
  }
  if (display === "html") {
    // Plugin HTML is untrusted: an empty sandbox gives it an opaque origin
    // with scripts, forms, popups and navigation all disabled.
    return (
      <iframe
        title={`${label} · ${side} side`}
        sandbox=""
        srcDoc={value.text}
        className="block h-[480px] w-full border-0 bg-white"
      />
    );
  }
  if (display === "text" || display === "json") {
    return (
      <pre className="max-h-[520px] overflow-auto whitespace-pre-wrap p-3 font-mono text-label leading-5 text-neutral-300">
        {display === "json" ? prettyJsonText(value.text) : value.text}
      </pre>
    );
  }
  return (
    <div className="flex items-center gap-3 p-3 text-label text-neutral-400">
      <span className="min-w-0 flex-1 truncate font-mono">{value.filename}</span>
      <span className="text-muted-foreground">{value.mediaType} · {formatBytes(value.size)}</span>
      <UiButton
        type="button"
        variant="outline"
        size="xs"
        onClick={() => saveBlobUrl(value.url, value.filename)}
      >
        <Download size={11} /> Download {side}
      </UiButton>
    </div>
  );
}

function SidePane({
  side,
  value,
  display,
  label,
  onRetry,
}: {
  side: Side;
  value: SideState | undefined;
  display: string;
  label: string;
  onRetry(): void;
}) {
  return (
    <div className="min-w-0 overflow-hidden rounded border border-border bg-background" data-testid={`plugin-preview-side:${side}`}>
      <div className="flex items-center gap-2 border-b border-border px-2.5 py-1.5 text-micro">
        <span className="font-medium text-neutral-300">{SIDE_LABEL[side]}</span>
        {value?.state === "ready" && <span className="ml-auto font-mono text-muted-foreground">{value.filename}</span>}
      </div>
      {!value || value.state === "loading" ? (
        <div className="flex min-h-40 items-center justify-center text-caption text-muted-foreground" role="status">
          Rendering exact Snapshot {side} side…
        </div>
      ) : value.state === "error" ? (
        <div className="min-h-40 p-3 text-label" role="alert">
          <div className="font-medium text-rose-300">Preview unavailable</div>
          <pre className="mt-1 max-h-52 overflow-auto whitespace-pre-wrap font-mono text-caption leading-4 text-muted-foreground">{value.message}</pre>
          <div className="mt-2 flex items-center gap-3 text-caption text-muted-foreground">
            <span>Source review below is unaffected.</span>
            <UiButton type="button" variant="link" onClick={onRetry} className="text-neutral-400 hover:text-foreground">
              <RotateCw size={10} /> Retry
            </UiButton>
          </div>
        </div>
      ) : (
        <ArtifactBody display={display} side={side} value={value} label={label} />
      )}
    </div>
  );
}

function DescriptorPreview({ surface, descriptor, revisionId, mode }: {
  surface: ReviewSurfaceInfo;
  descriptor: ReviewSurfacePreviewDescriptor;
  revisionId: string;
  mode: SurfacePreviewMode;
}) {
  const [attempt, setAttempt] = useState(0);
  const unavailable = descriptor.status === "unavailable";
  const sides = useMemo(() => (unavailable ? [] : previewSides(descriptor, mode)), [descriptor, mode, unavailable]);
  const states = useSidePreviews(surface, revisionId, sides, attempt);
  const label = descriptor.label || `${descriptor.renderer} preview`;
  if (unavailable) {
    return (
      <div className="rounded border border-amber-900/50 bg-amber-950/10 p-3 text-label" role="alert">
        <div className="font-medium text-amber-300">{label} unavailable</div>
        <div className="mt-1 text-caption text-neutral-400">{descriptor.reason || "The preview renderer is unavailable."}</div>
        <div className="mt-1 text-caption text-muted-foreground">Source review below is unaffected.</div>
      </div>
    );
  }
  const oldState = states.old;
  const newState = states.new;
  if (
    mode === "compare"
    && descriptor.display === "image"
    && oldState?.state === "ready"
    && newState?.state === "ready"
  ) {
    return (
      <ImageOverlayCompare
        beforeSrc={oldState.url}
        afterSrc={newState.url}
        beforeLabel="Before · old side"
        afterLabel="After · new side"
      />
    );
  }
  return (
    <div className={sides.length > 1 ? "grid gap-3 lg:grid-cols-2" : ""}>
      {sides.map((side) => (
        <SidePane
          key={side}
          side={side}
          value={states[side]}
          display={descriptor.display}
          label={label}
          onRetry={() => setAttempt((value) => value + 1)}
        />
      ))}
    </div>
  );
}

function metadataValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    const text = JSON.stringify(value);
    return text.length > 240 ? `${text.slice(0, 240)}…` : text;
  } catch {
    return String(value);
  }
}

function RunPane({ side, result }: { side: Side; result?: ReviewSurfaceRunResult }) {
  if (!result) {
    return (
      <div className="flex min-h-20 items-center justify-center rounded border border-border text-caption text-muted-foreground">
        Not run yet
      </div>
    );
  }
  const tone = result.status === "passed" ? "text-emerald-300" : result.status === "failed" ? "text-rose-300" : "text-amber-300";
  return (
    <div className="min-w-0 rounded border border-border">
      <div className="flex items-center gap-2 border-b border-border px-2.5 py-1.5 text-caption">
        <span className="text-muted-foreground">{SIDE_LABEL[side]}</span>
        <span className={tone}>{result.status}</span>
        <span className="ml-auto font-mono text-muted-foreground">{result.duration_ms} ms</span>
      </div>
      <div className="px-2.5 py-1.5 text-caption text-neutral-400">{result.summary}</div>
      {result.output && (
        <pre className="max-h-52 overflow-auto whitespace-pre-wrap border-t border-neutral-900 p-2.5 font-mono text-caption leading-4 text-muted-foreground">{result.output}</pre>
      )}
    </div>
  );
}

/**
 * Generic fallback for a surface kind the Reader has no dedicated view for and
 * whose provider declared no preview. Everything shown comes from the surface
 * contract itself, so any provider is reviewable without a Reader change.
 */
function GenericSurfaceFacts({ surface, mode, run, running, onRun, onOpenPath }: {
  surface: ReviewSurfaceInfo;
  mode: SurfacePreviewMode;
  run?: { old?: ReviewSurfaceRunResult; new?: ReviewSurfaceRunResult; error?: string };
  running: boolean;
  onRun?(compare: boolean): void;
  onOpenPath?(path: string): void;
}) {
  const executable = surface.capabilities.includes("execute") && Boolean(surface.runtime);
  const metadata = Object.entries(surface.metadata ?? {}).filter(([, value]) => metadataValue(value) !== "");
  return (
    <div className="space-y-3 text-label">
      <div className="text-caption text-muted-foreground">
        No preview is declared for <span className="font-mono text-neutral-300">{surface.kind}</span>; showing the surface as its provider described it. Source review below is unaffected.
      </div>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 text-caption">
        <dt className="text-muted-foreground">Provider</dt><dd className="font-mono text-neutral-300">{surface.provider}</dd>
        <dt className="text-muted-foreground">Locator</dt><dd className="break-all font-mono text-neutral-300">{surface.locator || surface.id}</dd>
        <dt className="text-muted-foreground">Runtime</dt><dd className="font-mono text-neutral-300">{surface.runtime || "none bound"}</dd>
        <dt className="text-muted-foreground">Capabilities</dt><dd className="font-mono text-neutral-300">{surface.capabilities.join(" · ") || "none"}</dd>
        {metadata.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="text-muted-foreground">{key}</dt>
            <dd className="break-all font-mono text-neutral-400">{metadataValue(value)}</dd>
          </div>
        ))}
      </dl>
      {surface.affected_paths.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {surface.affected_paths.map((path) => (
            <UiButton unstyled
              key={path}
              type="button"
              onClick={() => onOpenPath?.(path)}
              className="rounded border border-border px-1.5 py-0.5 font-mono text-caption text-neutral-400 hover:text-foreground"
            >
              {path}
            </UiButton>
          ))}
        </div>
      )}
      {executable && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <UiButton
              type="button"
              disabled={running}
              onClick={() => onRun?.(mode === "compare")}
            >
              {mode === "compare" ? "Run compare" : "Run"}
            </UiButton>
            {running && <span className="text-caption text-amber-400/70" role="status">Running…</span>}
            {run?.error && <span className="text-caption text-rose-300" role="alert">{run.error}</span>}
          </div>
          <div className={mode === "compare" ? "grid gap-3 lg:grid-cols-2" : ""}>
            {mode === "compare" && <RunPane side="old" result={run?.old} />}
            <RunPane side="new" result={run?.new} />
          </div>
        </div>
      )}
    </div>
  );
}

interface SurfacePluginPreviewProps {
  surface: ReviewSurfaceInfo;
  revisionId: string;
  mode: SurfacePreviewMode;
  run?: { old?: ReviewSurfaceRunResult; new?: ReviewSurfaceRunResult; error?: string };
  running?: boolean;
  onRun?(compare: boolean): void;
  onOpenPath?(path: string): void;
}

/** Descriptor-driven preview for any surface; generic facts when none is declared. */
export default function SurfacePluginPreview({
  surface,
  revisionId,
  mode,
  run,
  running = false,
  onRun,
  onOpenPath,
}: SurfacePluginPreviewProps) {
  return (
    <div className="bg-surface-sunken p-3" data-testid={`plugin-preview:${surface.provider}:${surface.id}`}>
      {surface.preview ? (
        <DescriptorPreview surface={surface} descriptor={surface.preview} revisionId={revisionId} mode={mode} />
      ) : (
        <GenericSurfaceFacts surface={surface} mode={mode} run={run} running={running} onRun={onRun} onOpenPath={onOpenPath} />
      )}
    </div>
  );
}
