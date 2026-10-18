import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { FileText } from "lucide-react";
import { api, type ReportMeta, type ReportContent } from "../api";
import {
  Alert,
  Button,
  Card,
  Chip,
  EmptyState,
} from "../components/WorkbenchUI";
import { fmtDate } from "../lib/format";

export default function Reports() {
  const [list, setList] = useState<ReportMeta[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [content, setContent] = useState<ReportContent | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [contentErr, setContentErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .reports()
      .then((reports) => {
        setList(reports);
        setSelectedId((current) =>
          current && reports.some((item) => item.id === current)
            ? current
            : reports[0]?.id ?? null
        );
        setErr(null);
      })
      .catch((error) => setErr(error instanceof Error ? error.message : String(error)));
  }, []);

  const selected = useMemo(
    () => list?.find((item) => item.id === selectedId) ?? null,
    [list, selectedId]
  );

  useEffect(() => {
    if (!selected) {
      setContent(null);
      return;
    }
    setContent(null);
    setContentErr(null);
    api
      .report(selected)
      .then(setContent)
      .catch((error) => setContentErr(String(error)));
  }, [selected]);

  return (
    <div className="space-y-4">
      {err && <Alert tone="destructive" description={err} />}

      {list === null && !err && (
        <EmptyState title="Loading benchmark reports…" />
      )}

      {list !== null && list.length === 0 && (
        <EmptyState
          icon={<FileText size={28} />}
          title="No benchmark reports yet"
          description={
            <>
              Benchmark runs written under{" "}
              <code className="text-brand-400">reports/benchmark/</code> appear
              here automatically.
            </>
          }
        />
      )}

      {list !== null && list.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
          <nav className="max-h-[calc(100vh-190px)] space-y-1 overflow-y-auto">
            {list.map((report) => (
              <Button
                key={report.id}
                onClick={() => setSelectedId(report.id)}
                variant={selectedId === report.id ? "default" : "ghost"}
                className="h-auto w-full justify-start px-3 py-2 text-left normal-case tracking-normal"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-semibold text-neutral-200">
                      {report.suite}
                    </span>
                    <span className="text-caption text-muted-foreground">
                      {report.project_label}
                    </span>
                  </div>
                  <div className="mt-0.5 truncate font-mono text-caption text-muted-foreground">
                    {report.run_id}
                  </div>
                  <div className="mt-0.5 text-caption text-muted-foreground">
                    {fmtDate(report.generated_at)}
                  </div>
                </div>
              </Button>
            ))}
          </nav>

          <Card className="min-w-0 p-4">
            {contentErr && <Alert tone="destructive" description={contentErr} />}
            {content === null && !contentErr && (
              <div className="text-xs text-muted-foreground">
                Loading benchmark report…
              </div>
            )}
            {content && (
              <>
                <div className="mb-4 flex flex-wrap items-center gap-2 border-b border-border pb-3">
                  <Chip tone="neutral">{content.suite}</Chip>
                  <span className="font-mono text-caption text-muted-foreground">
                    {content.run_id}
                  </span>
                  <span className="text-caption text-muted-foreground">
                    {content.project_label}
                  </span>
                  <span className="ml-auto text-caption text-muted-foreground">
                    {content.files.length} artifacts
                  </span>
                </div>
                <article className="prose prose-invert prose-sm max-w-none prose-headings:text-neutral-200 prose-p:text-neutral-400 prose-strong:text-neutral-200 prose-code:text-brand-300 prose-a:text-brand-400 prose-table:text-neutral-300 prose-th:text-neutral-300 prose-td:text-neutral-400">
                  <ReactMarkdown>{content.markdown}</ReactMarkdown>
                </article>
              </>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
