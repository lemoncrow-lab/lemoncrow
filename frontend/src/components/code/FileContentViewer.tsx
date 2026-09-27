import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import "github-markdown-css/github-markdown.css";

import ReadOnlyCodeViewer from "./ReadOnlyCodeViewer";
import { authenticatedFetch } from "../../review/reviewApi";

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
      <div className="flex min-h-[520px] items-center justify-center p-6 text-xs text-red-300">
        Could not preview {path}: {error}
      </div>
    );
  }
  if (!objectUrl) {
    return (
      <div className="flex min-h-[520px] items-center justify-center text-xs text-neutral-500">
        Loading preview…
      </div>
    );
  }
  if (kind === "image") {
    return (
      <div className="flex h-full min-h-[520px] items-start justify-center overflow-auto bg-neutral-900/60 p-6">
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
}: FileContentViewerProps) {
  if (mode === "source" || kind === "code") {
    return source === null ? null : (
      <ReadOnlyCodeViewer
        path={path}
        value={source}
        theme={theme}
        selectedRange={selectedRange}
      />
    );
  }

  if (kind === "markdown") {
    return source === null ? null : (
      <div className="h-full overflow-auto bg-neutral-950 px-7 py-6">
        <article
          className={`markdown-body mx-auto max-w-5xl ${
            theme === "dark" ? "markdown-body-dark" : ""
          }`}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeRaw, rehypeSanitize]}
          >
            {source}
          </ReactMarkdown>
        </article>
      </div>
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
