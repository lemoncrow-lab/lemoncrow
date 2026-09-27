import { useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { api, type FileProjectionResponse } from "../api";
import {
  Button,
  Card,
  Chip,
  FieldLabel,
  Input,
} from "../components/WorkbenchUI";

const VIEW_OPTIONS = ["compact", "exact", "summary"] as const;

export default function ProjectionInspector() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [payload, setPayload] = useState<FileProjectionResponse | null>(null);
  const [comparison, setComparison] = useState<FileProjectionResponse | null>(
    null
  );
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [comparisonError, setComparisonError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const path = searchParams.get("path") ?? "";
  const view = (searchParams.get("view") ?? "compact") as
    | (typeof VIEW_OPTIONS)[number]
    | "range";
  const range = searchParams.get("range") ?? "";
  const maxLines = Number(searchParams.get("max_lines") ?? "200");

  const exactSegments =
    payload?.projection_mapping?.segments.filter((segment) => segment.exact)
      .length ?? 0;
  const whitespaceSegments =
    payload?.projection_mapping?.segments.filter((segment) => !segment.exact)
      .length ?? 0;

  const segmentPreview = useMemo(
    () => payload?.projection_mapping?.segments.slice(0, 12) ?? [],
    [payload?.projection_mapping?.segments]
  );
  const comparisonRows = useMemo(() => {
    if (!payload?.content || !comparison?.content) return [];
    const compactLines = payload.content.split("\n");
    const exactLines = comparison.content.split("\n");
    const rowCount = Math.max(compactLines.length, exactLines.length);
    return Array.from({ length: rowCount }, (_, index) => {
      const compactLine = compactLines[index] ?? "";
      const exactLine = exactLines[index] ?? "";
      return {
        line: index + 1,
        compact: compactLine,
        exact: exactLine,
        changed: compactLine !== exactLine,
      };
    });
  }, [payload?.content, comparison?.content]);
  const changedLineCount = comparisonRows.filter((row) => row.changed).length;

  const loadProjection = async (shouldCommit: () => boolean = () => true) => {
    if (!path.trim()) {
      if (!shouldCommit()) return;
      setPayload(null);
      setError(null);
      return;
    }
    setLoading(true);
    try {
      const response = await api.fileProjection(path, {
        view: view === "range" ? "range" : view,
        range: view === "range" ? range || undefined : undefined,
        maxLines: Number.isFinite(maxLines) && maxLines > 0 ? maxLines : 200,
      });
      if (!shouldCommit()) return;
      setPayload(response);
      setComparison(null);
      setComparisonError(null);
      setError(null);
    } catch (err) {
      if (!shouldCommit()) return;
      setPayload(null);
      setComparison(null);
      setComparisonError(null);
      setError(
        err instanceof Error ? err.message : "Failed to load projection"
      );
    } finally {
      if (shouldCommit()) setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      void loadProjection(() => !cancelled);
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [path, view, range, maxLines]);

  const loadExactComparison = async () => {
    if (!path.trim()) return;
    setComparisonLoading(true);
    try {
      const exactPayload = await api.fileProjection(path, { view: "exact" });
      setComparison(exactPayload);
      setComparisonError(null);
    } catch (err) {
      setComparison(null);
      setComparisonError(
        err instanceof Error ? err.message : "Failed to load exact comparison"
      );
    } finally {
      setComparisonLoading(false);
    }
  };

  const updateParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (!value) next.delete(key);
    else next.set(key, value);
    setSearchParams(next);
  };

  return (
    <div className="space-y-4">
      <Card className="space-y-4">
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto]">
          <div className="space-y-2">
            <FieldLabel>Path</FieldLabel>
            <Input
              value={path}
              onChange={(event) => updateParam("path", event.target.value)}
              placeholder="/repo/file.go"
            />
          </div>
          <div className="flex items-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => void loadProjection()}
              disabled={!path.trim() || loading}
            >
              <RefreshCw className={loading ? "animate-spin" : ""} size={14} />
              Refresh
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {VIEW_OPTIONS.map((option) => (
            <Button
              key={option}
              type="button"
              variant={view === option ? "accent" : "outline"}
              onClick={() => updateParam("view", option)}
            >
              {option}
            </Button>
          ))}
          <Button
            type="button"
            variant={view === "range" ? "accent" : "outline"}
            onClick={() => updateParam("view", "range")}
          >
            range
          </Button>
        </div>
        {view === "range" && (
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <FieldLabel>Range</FieldLabel>
              <Input
                value={range}
                onChange={(event) => updateParam("range", event.target.value)}
                placeholder="L10-L20"
              />
            </div>
            <div className="space-y-2">
              <FieldLabel>Summary max lines</FieldLabel>
              <Input
                value={
                  Number.isFinite(maxLines) && maxLines > 0
                    ? String(maxLines)
                    : "200"
                }
                onChange={(event) =>
                  updateParam("max_lines", event.target.value)
                }
                placeholder="200"
              />
            </div>
          </div>
        )}
      </Card>

      {error && <Card className="border-red-900/40 text-red-300">{error}</Card>}
      {!path.trim() && !error && (
        <Card className="text-neutral-400">
          Paste a file path or open this page from a Projection link.
        </Card>
      )}

      {payload && (
        <>
          <Card className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Chip tone={payload.projection.transformed ? "amber" : "emerald"}>
                {payload.projection.view}
              </Chip>
              <Chip tone="neutral">{payload.language || "unknown"}</Chip>
              {payload.projection.untransformed_text ? (
                <Chip tone="emerald">exact text</Chip>
              ) : (
                <Chip tone="amber">transformed</Chip>
              )}
              {payload.tokens_saved !== undefined && (
                <Chip tone="purple">{payload.tokens_saved} tokens saved</Chip>
              )}
            </div>
            {payload.projection.notice && (
              <div className="text-sm text-amber-200">
                {payload.projection.notice}
              </div>
            )}
            <div className="grid gap-3 md:grid-cols-4">
              <StatCard label="Mode" value={payload.mode} />
              <StatCard label="Exact segments" value={String(exactSegments)} />
              <StatCard
                label="Whitespace segments"
                value={String(whitespaceSegments)}
              />
              <StatCard
                label="Mapping"
                value={payload.projection_mapping ? "available" : "none"}
              />
            </div>
          </Card>

          {segmentPreview.length > 0 && (
            <Card className="space-y-3">
              <div className="text-xs font-black uppercase tracking-[0.2em] text-neutral-400">
                Segment preview
              </div>
              <div className="space-y-2 font-mono text-xs">
                {segmentPreview.map((segment) => (
                  <div
                    key={segment.segment_id}
                    className="grid gap-2 border border-neutral-800 bg-black/20 p-3 md:grid-cols-[auto_auto_1fr]"
                  >
                    <span className="text-neutral-400">
                      {segment.segment_id}
                    </span>
                    <span
                      className={
                        segment.exact ? "text-emerald-300" : "text-amber-300"
                      }
                    >
                      {segment.kind}
                    </span>
                    <span className="text-neutral-300">
                      projected {segment.projected_start}-
                      {segment.projected_end} · source L
                      {segment.source.start_line}-L{segment.source.end_line}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card className="space-y-3">
            <div className="text-xs font-black uppercase tracking-[0.2em] text-neutral-400">
              Content preview
            </div>
            <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap border border-neutral-800 bg-black/30 p-4 text-sm text-neutral-200">
              {payload.content || "(no body content for this view)"}
            </pre>
          </Card>

          {payload.projection.view === "compact" && !comparison && (
            <Card className="space-y-3">
              <div className="flex items-center justify-between gap-3">
                <div className="text-xs font-black uppercase tracking-[0.2em] text-neutral-400">
                  Exact comparison
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => void loadExactComparison()}
                  disabled={comparisonLoading}
                >
                  <RefreshCw
                    className={comparisonLoading ? "animate-spin" : ""}
                    size={14}
                  />
                  Load exact compare
                </Button>
              </div>
              {comparisonError && (
                <div className="text-sm text-red-300">{comparisonError}</div>
              )}
            </Card>
          )}

          {comparison && (
            <Card className="space-y-3">
              <div className="text-xs font-black uppercase tracking-[0.2em] text-neutral-400">
                Exact comparison
              </div>
              <div className="flex flex-wrap gap-2">
                <Chip tone={changedLineCount > 0 ? "amber" : "emerald"}>
                  {changedLineCount} changed lines
                </Chip>
                <Chip tone="neutral">
                  compact {payload.content?.length ?? 0} chars
                </Chip>
                <Chip tone="neutral">
                  exact {comparison.content?.length ?? 0} chars
                </Chip>
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                <div className="space-y-2">
                  <FieldLabel>Compact projection</FieldLabel>
                  <ComparisonPane rows={comparisonRows} side="compact" />
                </div>
                <div className="space-y-2">
                  <FieldLabel>Exact text</FieldLabel>
                  <ComparisonPane rows={comparisonRows} side="exact" />
                </div>
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-neutral-800 bg-black/20 p-3">
      <div className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-400">
        {label}
      </div>
      <div className="mt-1 text-sm text-neutral-100">{value}</div>
    </div>
  );
}

function ComparisonPane({
  rows,
  side,
}: {
  rows: Array<{
    line: number;
    compact: string;
    exact: string;
    changed: boolean;
  }>;
  side: "compact" | "exact";
}) {
  return (
    <div className="max-h-[24rem] overflow-auto border border-neutral-800 bg-black/30 font-mono text-sm text-neutral-200">
      {rows.map((row) => (
        <div
          key={`${side}-${row.line}`}
          className={
            row.changed
              ? "grid grid-cols-[auto_1fr] gap-3 border-b border-neutral-800 bg-amber-950/20 px-3 py-1.5"
              : "grid grid-cols-[auto_1fr] gap-3 border-b border-neutral-900/60 px-3 py-1.5"
          }
        >
          <span className="text-neutral-400">{row.line}</span>
          <span>
            {side === "compact" ? row.compact || " " : row.exact || " "}
          </span>
        </div>
      ))}
    </div>
  );
}
