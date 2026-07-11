import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { FileText } from "lucide-react";
import { api, type ReportMeta, type ReportContent } from "../api";
import {
  Alert,
  Button,
  Card,
  EmptyState,
} from "../components/WorkbenchUI";

export default function Reports() {
  const [list, setList] = useState<ReportMeta[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<ReportContent | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [contentErr, setContentErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .reports()
      .then((r) => {
        setList(r);
        if (r.length > 0) setSelected(r[0].week);
      })
      .catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setContent(null);
    setContentErr(null);
    api
      .report(selected)
      .then(setContent)
      .catch((e) => setContentErr(String(e)));
  }, [selected]);

  return (
    <div className="space-y-6">
      {err && <Alert tone="danger" description={err} />}

      {list === null && !err && (
        <EmptyState title="Loading reports…" className="p-6" />
      )}

      {list !== null && list.length === 0 && (
        <EmptyState
          icon={<FileText size={32} />}
          title="No reports published yet"
          description={
            <>
              Run{" "}
              <code className="text-brand-400">lemon benchmark publish</code>{" "}
              to generate your first report.
            </>
          }
        />
      )}

      {list !== null && list.length > 0 && (
        <div className="flex gap-4">
          {/* sidebar */}
          <nav className="w-40 shrink-0 space-y-1">
            {list.map((r) => (
              <Button
                key={r.week}
                onClick={() => setSelected(r.week)}
                variant={selected === r.week ? "accent" : "outline"}
                className="w-full justify-start text-left normal-case tracking-normal"
              >
                <div className="font-semibold">{r.week}</div>
                <div className="mt-0.5 text-neutral-400">
                  {r.generated_at.slice(0, 10)}
                </div>
              </Button>
            ))}
          </nav>

          {/* content */}
          <Card className="min-w-0 flex-1 p-5">
            {contentErr && <Alert tone="danger" description={contentErr} />}
            {content === null && !contentErr && (
              <div className="text-sm text-neutral-400">Loading report…</div>
            )}
            {content && (
              <article className="prose prose-invert prose-sm max-w-none prose-headings:text-neutral-200 prose-p:text-neutral-400 prose-strong:text-neutral-200 prose-code:text-brand-300 prose-a:text-brand-400 prose-table:text-neutral-300 prose-th:text-neutral-300 prose-td:text-neutral-400">
                <ReactMarkdown>{content.markdown}</ReactMarkdown>
              </article>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
