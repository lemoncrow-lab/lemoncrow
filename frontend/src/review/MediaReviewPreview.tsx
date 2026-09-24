import { useEffect, useState } from "react";

import PdfReviewDocument from "./PdfReviewDocument";
import { currentReviewId, fetchMediaFile } from "./reviewApi";
import { setBoundedCacheEntry, touchCacheEntry } from "./previewCache";
import type { MediaPreview } from "./types";

export type MediaRenderMode = "preview" | "compare" | "source";

interface MediaReviewPreviewProps {
  preview: MediaPreview;
  documentPath: string;
  /** Exact Review revision backing this media projection. */
  revisionId?: string;
  status?: string;
  mode: MediaRenderMode;
}
interface LoadedMedia {
  url: string;
  blob: Blob;
}

const mediaAssets = new Map<string, LoadedMedia>();
const mediaLoads = new Map<string, Promise<LoadedMedia>>();
let cleanupRegistered = false;

function mediaKey(revisionId: string, path: string, side: "old" | "new"): string {
  return `${currentReviewId()}\u0000${revisionId}\u0000${path}\u0000${side}`;
}

function loadMedia(revisionId: string, path: string, side: "old" | "new"): Promise<LoadedMedia> {
  const key = mediaKey(revisionId, path, side);
  const cached = touchCacheEntry(mediaAssets, key);
  if (cached) return Promise.resolve(cached);
  const pending = mediaLoads.get(key);
  if (pending) return pending;

  if (!cleanupRegistered && typeof window !== "undefined") {
    cleanupRegistered = true;
    window.addEventListener("pagehide", () => {
      for (const asset of mediaAssets.values()) URL.revokeObjectURL(asset.url);
      mediaAssets.clear();
      mediaLoads.clear();
      cleanupRegistered = false;
    }, { once: true });
  }

  const request = fetchMediaFile(currentReviewId(), path, side, revisionId)
    .then((blob) => {
      const asset = { url: URL.createObjectURL(blob), blob };
      setBoundedCacheEntry(mediaAssets, key, asset, (evicted) => URL.revokeObjectURL(evicted.url));
      mediaLoads.delete(key);
      return asset;
    })
    .catch((error) => {
      mediaLoads.delete(key);
      throw error;
    });
  mediaLoads.set(key, request);
  return request;
}
function MediaObject({
  preview,
  revisionId,
  documentPath,
  side,
}: {
  preview: MediaPreview;
  revisionId: string;
  documentPath: string;
  side: "old" | "new";
}) {
  const key = mediaKey(revisionId, documentPath, side);
  const [asset, setAsset] = useState<LoadedMedia | null>(() => touchCacheEntry(mediaAssets, key) ?? null);
  const [error, setError] = useState("");

  useEffect(() => {
    const cached = touchCacheEntry(mediaAssets, key);
    if (cached) {
      setAsset(cached);
      setError("");
      return;
    }
    let disposed = false;
    setAsset(null);
    setError("");
    loadMedia(revisionId, documentPath, side)
      .then((next) => {
        if (!disposed) setAsset(next);
      })
      .catch((reason) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => { disposed = true; };
  }, [documentPath, key, revisionId, side]);

  if (error) {
    return (
      <div className="flex min-h-44 items-center justify-center px-6 text-center">
        <div>
          <div className="text-[11px] font-medium text-amber-300">Preview unavailable</div>
          <details className="mt-1 text-[10px] text-neutral-600">
            <summary className="cursor-pointer hover:text-neutral-50">Details</summary>
            <div className="mt-1 max-w-xl break-words">{error}</div>
          </details>
        </div>
      </div>
    );
  }
  if (!asset) {
    return <div className="flex min-h-44 items-center justify-center text-[10px] text-neutral-600">Loading preview…</div>;
  }
  if (preview.media_kind === "image") {
    return <img src={asset.url} alt={`${side === "old" ? "Before" : "After"} ${documentPath}`} className="mx-auto max-h-[70vh] max-w-full object-contain" />;
  }
  if (preview.media_kind === "video") {
    return <video src={asset.url} controls muted playsInline preload="metadata" className="mx-auto max-h-[70vh] max-w-full" />;
  }
  if (preview.media_kind === "audio") {
    return <audio src={asset.url} controls preload="metadata" className="mx-auto w-full max-w-4xl" />;
  }
  return <PdfReviewDocument blob={asset.blob} label={side === "old" ? "Before" : "After"} />;
}

function Side({
  label,
  available,
  preview,
  revisionId,
  documentPath,
  side,
}: {
  label: string;
  available: boolean;
  preview: MediaPreview;
  revisionId: string;
  documentPath: string;
  side: "old" | "new";
}) {
  return (
    <section className="min-w-0 flex-1 border-y border-neutral-800 bg-surface-raised">
      <div className="border-b border-neutral-800 px-3 py-1.5 text-[10px] font-medium text-neutral-500">{label}</div>
      <div className="flex min-h-52 items-center justify-center overflow-auto p-4">
        {available
          ? <MediaObject preview={preview} revisionId={revisionId} documentPath={documentPath} side={side} />
          : <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-neutral-700">Not present</span>}
      </div>
    </section>
  );
}

export default function MediaReviewPreview({ preview, documentPath, revisionId = "", status = "modified", mode }: MediaReviewPreviewProps) {
  if (mode === "source") {
    return (
      <div className="border-b border-neutral-900 bg-surface-sunken p-4" data-testid="media-review-source">
        <div className="mx-auto grid max-w-3xl gap-x-6 gap-y-2 border-y border-neutral-900 py-3 font-mono text-[10px] sm:grid-cols-[100px_1fr]">
          <span className="text-neutral-600">Path</span><span className="break-all text-neutral-300">{documentPath}</span>
          <span className="text-neutral-600">Media type</span><span className="text-neutral-300">{preview.media_type}</span>
          <span className="text-neutral-600">Status</span><span className="text-neutral-300">{status}</span>
          <span className="text-neutral-600">Source</span><span className="text-neutral-500">This format is reviewed as rendered media rather than a textual diff. Use Preview or Compare to inspect the recorded revision.</span>
        </div>
      </div>
    );
  }

  if (mode === "preview") {
    const side = status === "deleted" ? "old" : "new";
    return (
      <div className="border-b border-neutral-900 bg-surface-sunken p-4" data-testid="media-review-preview">
        <Side
          label={side === "old" ? "Before" : "After"}
          available
          preview={preview}
          revisionId={revisionId}
          documentPath={documentPath}
          side={side}
        />
      </div>
    );
  }

  return (
    <div className="grid gap-3 border-b border-neutral-900 bg-surface-sunken p-4 lg:grid-cols-2" data-testid="media-review-compare">
      <Side label="Before" available={status !== "added"} preview={preview} revisionId={revisionId} documentPath={documentPath} side="old" />
      <Side label="After" available={status !== "deleted"} preview={preview} revisionId={revisionId} documentPath={documentPath} side="new" />
    </div>
  );
}
