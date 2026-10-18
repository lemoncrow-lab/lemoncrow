import { Suspense, lazy, useEffect, useState } from "react";

import ReadOnlyCodeViewer, { type ChangedLines } from "./ReadOnlyCodeViewer";
import { authenticatedFetch } from "../../review/reviewApi";

// The Markdown stack (react-markdown, rehype-raw's HTML parser, GFM, styles) is
// ~330 KB; only a rendered .md preview needs it.
const MarkdownFilePreview = lazy(() => import("./MarkdownFilePreview"));

export type FileRenderKind = "code" | "markdown" | "html" | "image" | "pdf";

const IMAGE_EXTENSIONS = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".svg",
  ".ico",
  ".avif",
]);

function extension(path: string): string {
  const name = path.replaceAll("\\", "/").split("/").pop() || path;
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

export function fileRenderKind(path: string): FileRenderKind {
  const ext = extension(path);
  if (ext === ".md" || ext === ".markdown" || ext === ".mdx") return "markdown";
  if (ext === ".html" || ext === ".htm") return "html";
  if (IMAGE_EXTENSIONS.has(ext)) return "image";
  if (ext === ".pdf") return "pdf";
  return "code";
}

export function fileNeedsText(kind: FileRenderKind, mode: "source" | "preview"): boolean {
  return kind === "code" || mode === "source" || kind === "markdown" || kind === "html";
}

interface FileContentViewerProps {
  path: string;
  source: string | null;
  sourceUrl: string;
  kind: FileRenderKind;
  mode: "source" | "preview";
  theme: "light" | "dark";
  selectedRange?: readonly [number, number] | null;
  changedLines?: ChangedLines | null;
  onSelectionChange?: (selection: { start: number; end: number; text: string } | null) => void;
}

function BinaryPreview({
  path,
  sourceUrl,
  kind,
}: {
  path: string;
  sourceUrl: string;
  kind: "image" | "pdf";
}) {
  const [objectUrl, setObjectUrl] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let blobUrl = "";
    setObjectUrl("");
    setError("");
    void authenticatedFetch(sourceUrl, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        blobUrl = URL.createObjectURL(blob);
        setObjectUrl(blobUrl);
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setError(caught instanceof Error ? caught.message : "Could not load preview.");
      });

    return () => {
      controller.abort();
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  }, [sourceUrl]);

  if (error) {
    return (
      <div className="flex min-h-[520px] items-center justify-center p-4 text-xs text-red-300">
        Could not preview {path}: {error}
      </div>
    );
  }
  if (!objectUrl) {
    return (
      <div className="flex min-h-[520px] items-center justify-center text-xs text-muted-foreground">
        Loading preview…
      </div>
    );
  }
  if (kind === "image") {
    return (
      <div className="flex h-full min-h-[520px] items-start justify-center overflow-auto bg-surface-sunken p-4">
        <img
          src={objectUrl}
          alt={path}
          className="max-h-full max-w-full object-contain shadow-2xl"
        />
      </div>
    );
  }
  return (
    <iframe
      title={`Preview ${path}`}
      className="h-full min-h-[620px] w-full border-0 bg-white"
      src={objectUrl}
    />
  );
}

export default function FileContentViewer({
  path,
  source,
  sourceUrl,
  kind,
  mode,
  theme,
  selectedRange = null,
  changedLines = null,
  onSelectionChange,
}: FileContentViewerProps) {
  if (mode === "source" || kind === "code") {
    return source === null ? null : (
      <ReadOnlyCodeViewer
        path={path}
        value={source}
        theme={theme}
        selectedRange={selectedRange}
        changedLines={changedLines}
        onSelectionChange={onSelectionChange}
      />
    );
  }

  if (kind === "markdown") {
    return source === null ? null : (
      <Suspense fallback={null}>
        <MarkdownFilePreview source={source} theme={theme} />
      </Suspense>
    );
  }

  if (kind === "html") {
    return source === null ? null : (
      <iframe
        title={`Preview ${path}`}
        className="h-full min-h-[520px] w-full border-0 bg-white"
        sandbox=""
        srcDoc={source}
      />
    );
  }

  if (kind === "image" || kind === "pdf") {
    return <BinaryPreview path={path} sourceUrl={sourceUrl} kind={kind} />;
  }

  return null;
}
