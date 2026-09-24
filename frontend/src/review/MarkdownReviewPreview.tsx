import { useEffect, useState, type ImgHTMLAttributes, type MouseEvent, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import githubMarkdownDarkCss from "github-markdown-css/github-markdown-dark.css?inline";
import githubMarkdownLightCss from "github-markdown-css/github-markdown-light.css?inline";
import "./MarkdownReviewPreview.css";
import { currentReviewId, fetchMarkdownImage } from "./reviewApi";
import { setBoundedCacheEntry, touchCacheEntry } from "./previewCache";
import type { DiffSide, MarkdownPreview } from "./types";

interface LineRange {
  start: number;
  end: number;
}

interface SourceNode {
  position?: {
    start: { line: number };
    end: { line: number };
  };
}

export type MarkdownRenderMode = "preview" | "compare";
export type MarkdownPreviewTheme = "auto" | "light" | "dark";

interface MarkdownReviewPreviewProps {
  preview: MarkdownPreview;
  patch: string;
  documentPath: string;
  /** Exact Review revision backing relative repository assets. */
  revisionId?: string;
  status?: string;
  mode?: MarkdownRenderMode;
  theme?: Exclude<MarkdownPreviewTheme, "auto">;
  onSelect(side: DiffSide, startLine: number, endLine: number): void;
}

function scopeGithubMarkdownCss(css: string, scope: string): string {
  return css.replaceAll(".markdown-body", `${scope} .markdown-body`);
}

const GITHUB_MARKDOWN_THEME_CSS = [
  scopeGithubMarkdownCss(githubMarkdownLightCss, ".lc-md-theme-light"),
  scopeGithubMarkdownCss(githubMarkdownDarkCss, ".lc-md-theme-dark"),
].join("\n");
const GITHUB_MARKDOWN_THEME_STYLE_ID = "lemoncrow-github-markdown-themes";

function ensureGithubMarkdownThemeStyles(): void {
  if (typeof document === "undefined" || document.getElementById(GITHUB_MARKDOWN_THEME_STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = GITHUB_MARKDOWN_THEME_STYLE_ID;
  style.textContent = GITHUB_MARKDOWN_THEME_CSS;
  document.head.append(style);
}

function coalesce(lines: number[]): LineRange[] {
  const ordered = [...new Set(lines)].sort((left, right) => left - right);
  const ranges: LineRange[] = [];
  for (const line of ordered) {
    const previous = ranges[ranges.length - 1];
    if (previous && line <= previous.end + 1) previous.end = line;
    else ranges.push({ start: line, end: line });
  }
  return ranges;
}

/** Exact changed source lines, not the context span advertised by each hunk header. */
export function changedMarkdownRanges(patch: string): { additions: LineRange[]; deletions: LineRange[] } {
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  const additions: number[] = [];
  const deletions: number[] = [];

  for (const line of patch.split("\n")) {
    const header = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (header) {
      oldLine = Number(header[1]);
      newLine = Number(header[2]);
      inHunk = true;
      continue;
    }
    if (!inHunk || line === "\\ No newline at end of file") continue;
    if (line.startsWith("+")) {
      additions.push(newLine);
      newLine += 1;
    } else if (line.startsWith("-")) {
      deletions.push(oldLine);
      oldLine += 1;
    } else if (line.startsWith(" ")) {
      oldLine += 1;
      newLine += 1;
    }
  }

  return { additions: coalesce(additions), deletions: coalesce(deletions) };
}

function overlaps(node: SourceNode | undefined, ranges: readonly LineRange[]): boolean {
  const start = node?.position?.start.line;
  const end = node?.position?.end.line;
  if (!start || !end) return false;
  return ranges.some((range) => range.start <= end && range.end >= start);
}

function sourceAttributes(
  node: SourceNode | undefined,
  ranges: readonly LineRange[],
  side: DiffSide,
  className: string | undefined,
  onSelect: MarkdownReviewPreviewProps["onSelect"],
  highlightChanges: boolean,
) {
  const start = node?.position?.start.line;
  const end = node?.position?.end.line;
  const changed = highlightChanges && overlaps(node, ranges);
  const classes = [
    className,
    highlightChanges ? "lc-md-review-block" : "lc-md-preview-block",
    changed ? `lc-md-changed-${side === "additions" ? "add" : "del"}` : "",
  ]
    .filter(Boolean)
    .join(" ");
  return {
    className: classes || undefined,
    "data-source-start": start,
    "data-source-end": end,
    "data-changed": changed ? "true" : "false",
    onClick: start && end
      ? (event: MouseEvent<HTMLElement>) => {
          event.stopPropagation();
          onSelect(side, start, end);
        }
      : undefined,
  };
}

const markdownImageUrls = new Map<string, string>();
const markdownImageLoads = new Map<string, Promise<string>>();
let markdownImageCleanupRegistered = false;

function markdownImageKey(revisionId: string, documentPath: string, side: DiffSide, source: string): string {
  return `${currentReviewId()}\u0000${revisionId}\u0000${documentPath}\u0000${side}\u0000${source}`;
}

function loadMarkdownImage(
  key: string,
  revisionId: string,
  documentPath: string,
  side: DiffSide,
  source: string,
): Promise<string> {
  const cached = touchCacheEntry(markdownImageUrls, key);
  if (cached) return Promise.resolve(cached);
  const pending = markdownImageLoads.get(key);
  if (pending) return pending;

  if (!markdownImageCleanupRegistered && typeof window !== "undefined") {
    markdownImageCleanupRegistered = true;
    window.addEventListener("pagehide", () => {
      for (const url of markdownImageUrls.values()) URL.revokeObjectURL(url);
      markdownImageUrls.clear();
      markdownImageLoads.clear();
      markdownImageCleanupRegistered = false;
    }, { once: true });
  }

  const request = fetchMarkdownImage(
    currentReviewId(),
    documentPath,
    side === "deletions" ? "old" : "new",
    source,
    revisionId,
  ).then((blob) => {
    const url = URL.createObjectURL(blob);
    setBoundedCacheEntry(markdownImageUrls, key, url, (evicted) => URL.revokeObjectURL(evicted));
    markdownImageLoads.delete(key);
    return url;
  }).catch((error) => {
    markdownImageLoads.delete(key);
    throw error;
  });
  markdownImageLoads.set(key, request);
  return request;
}

function MarkdownImage({
  revisionId,
  documentPath,
  side,
  src,
  alt,
  ...props
}: ImgHTMLAttributes<HTMLImageElement> & {
  revisionId: string;
  documentPath: string;
  side: DiffSide;
}) {
  const source = typeof src === "string" ? src : "";
  const cacheKey = markdownImageKey(revisionId, documentPath, side, source);
  const [objectUrl, setObjectUrl] = useState(() => touchCacheEntry(markdownImageUrls, cacheKey) ?? "");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!source || source.startsWith("data:") || source.startsWith("blob:")) return;
    const cached = touchCacheEntry(markdownImageUrls, cacheKey);
    if (cached) {
      setObjectUrl(cached);
      setFailed(false);
      return;
    }

    let disposed = false;
    setFailed(false);
    loadMarkdownImage(cacheKey, revisionId, documentPath, side, source)
      .then((url) => {
        if (!disposed) setObjectUrl(url);
      })
      .catch(() => {
        if (!disposed) setFailed(true);
      });
    return () => {
      disposed = true;
    };
  }, [cacheKey, documentPath, revisionId, side, source]);

  if (!source) return null;
  if (source.startsWith("data:") || source.startsWith("blob:")) {
    return <img {...props} src={source} alt={alt ?? ""} />;
  }
  if (failed) {
    return <span className="lc-markdown-image-failed" title={source}>{alt || "Image unavailable"}</span>;
  }
  if (!objectUrl) {
    return <span className="lc-markdown-image-loading" aria-label={alt || "Loading image"} />;
  }
  return <img {...props} src={objectUrl} alt={alt ?? ""} />;
}

function componentsFor(
  ranges: readonly LineRange[],
  side: DiffSide,
  onSelect: MarkdownReviewPreviewProps["onSelect"],
  highlightChanges: boolean,
  documentPath: string,
  revisionId: string,
): Components {
  const attrs = (node: SourceNode | undefined, className?: string) => sourceAttributes(
    node,
    ranges,
    side,
    className,
    onSelect,
    highlightChanges,
  );
  return {
    h1: ({ node, className, ...props }) => <h1 {...props} {...attrs(node, className)} />,
    h2: ({ node, className, ...props }) => <h2 {...props} {...attrs(node, className)} />,
    h3: ({ node, className, ...props }) => <h3 {...props} {...attrs(node, className)} />,
    h4: ({ node, className, ...props }) => <h4 {...props} {...attrs(node, className)} />,
    h5: ({ node, className, ...props }) => <h5 {...props} {...attrs(node, className)} />,
    h6: ({ node, className, ...props }) => <h6 {...props} {...attrs(node, className)} />,
    p: ({ node, className, ...props }) => <p {...props} {...attrs(node, className)} />,
    li: ({ node, className, ...props }) => <li {...props} {...attrs(node, className)} />,
    blockquote: ({ node, className, ...props }) => <blockquote {...props} {...attrs(node, className)} />,
    pre: ({ node, className, ...props }) => <pre {...props} {...attrs(node, className)} />,
    table: ({ node, className, ...props }) => <table {...props} {...attrs(node, className)} />,
    details: ({ node, className, ...props }) => <details {...props} {...attrs(node, className)} />,
    hr: ({ node, className, ...props }) => <hr {...props} {...attrs(node, className)} />,
    img: ({ node: _node, ...props }) => <MarkdownImage {...props} revisionId={revisionId} documentPath={documentPath} side={side} />,
    a: ({ node: _node, onClick, ...props }) => (
      <a
        {...props}
        target="_blank"
        rel="noreferrer"
        onClick={(event) => {
          event.stopPropagation();
          onClick?.(event);
        }}
      />
    ),
  };
}

function DocumentSide({
  label,
  content,
  ranges,
  side,
  onSelect,
  showLabel = true,
  highlightChanges = true,
  documentPreview = false,
  documentPath,
  revisionId,
}: {
  label: string;
  content: string;
  ranges: readonly LineRange[];
  side: DiffSide;
  onSelect: MarkdownReviewPreviewProps["onSelect"];
  showLabel?: boolean;
  highlightChanges?: boolean;
  documentPreview?: boolean;
  documentPath: string;
  revisionId: string;
}) {
  const components = componentsFor(ranges, side, onSelect, highlightChanges, documentPath, revisionId);
  return (
    <section
      className={`lc-markdown-pane min-w-0 flex-1 ${documentPreview ? "lc-markdown-document" : ""}`}
      data-testid={`markdown-${side}`}
    >
      {showLabel && <div className="lc-markdown-side-label">{label}</div>}
      <div className="markdown-body">
        {content === "" ? (
          <div className="lc-markdown-empty">Empty document</div>
        ) : (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeRaw, rehypeSanitize]}
            components={components}
          >
            {content}
          </ReactMarkdown>
        )}
      </div>
    </section>
  );
}

export default function MarkdownReviewPreview({
  preview,
  patch,
  documentPath,
  revisionId = "",
  status = "modified",
  mode = "compare",
  theme = "light",
  onSelect,
}: MarkdownReviewPreviewProps) {
  useEffect(() => {
    ensureGithubMarkdownThemeStyles();
  }, []);
  const ranges = changedMarkdownRanges(patch);

  if (mode === "preview") {
    const deleted = status === "deleted";
    return (
      <div className={`lc-markdown-review lc-markdown-preview lc-md-theme-${theme}`} data-testid="markdown-review-preview" data-preview-theme={theme}>
        <div className="lc-markdown-preview-canvas">
          <DocumentSide
            label={deleted ? "Before" : "After"}
            content={deleted ? preview.old_content : preview.new_content}
            ranges={[]}
            side={deleted ? "deletions" : "additions"}
            onSelect={onSelect}
            documentPath={documentPath}
            revisionId={revisionId}
            showLabel={false}
            highlightChanges={false}
            documentPreview
          />
        </div>
      </div>
    );
  }

  const showOld = status !== "added";
  const showNew = status !== "deleted";
  const columns: ReactNode[] = [];

  if (showOld) {
    columns.push(
      <DocumentSide
        key="before"
        label="Before"
        content={preview.old_content}
        ranges={ranges.deletions}
        side="deletions"
        onSelect={onSelect}
        documentPath={documentPath}
        revisionId={revisionId}
      />,
    );
  }
  if (showNew) {
    columns.push(
      <DocumentSide
        key="after"
        label="After"
        content={preview.new_content}
        ranges={ranges.additions}
        side="additions"
        onSelect={onSelect}
        documentPath={documentPath}
        revisionId={revisionId}
      />,
    );
  }

  return (
    <div className={`lc-markdown-review lc-md-theme-${theme}`} data-testid="markdown-review-preview" data-preview-theme={theme}>
      <div className={`lc-markdown-columns ${columns.length === 1 ? "lc-markdown-single" : ""}`}>{columns}</div>
    </div>
  );
}
