import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api, type ReportMeta, type ReportContent } from "../api";
import { SectionHeader } from "../components/WorkbenchUI";

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
      <SectionHeader
        title="Benchmark Reports"
        description="Published weekly benchmark summaries"
      />

      {err && (
        <div className="border border-red-800 bg-red-950/30 p-4 text-sm text-red-300">{err}</div>
      )}

      {list === null && !err && (
        <div className="border border-neutral-800 p-6 text-center text-sm text-neutral-500">
          Loading reports…
        </div>
      )}

      {list !== null && list.length === 0 && (
        <div className="border border-neutral-800 p-8 text-center text-sm text-neutral-500">
          <p className="text-2xl mb-3">📄</p>
          <p className="font-semibold">No reports published yet</p>
          <p className="mt-1 text-neutral-600">
            Run <code className="text-purple-400">atelier benchmark publish</code> to generate
            your first report.
          </p>
        </div>
      )}

      {list !== null && list.length > 0 && (
        <div className="flex gap-4">
          {/* sidebar */}
          <nav className="w-40 shrink-0 space-y-1">
            {list.map((r) => (
              <button
                key={r.week}
                type="button"
                onClick={() => setSelected(r.week)}
                className={[
                  "w-full border px-3 py-2 text-left text-xs transition-colors",
                  selected === r.week
                    ? "border-purple-700 bg-purple-950/40 text-purple-200"
                    : "border-neutral-800 text-neutral-400 hover:border-neutral-700 hover:text-neutral-200",
                ].join(" ")}
              >
                <div className="font-semibold">{r.week}</div>
                <div className="mt-0.5 text-neutral-500">{r.generated_at.slice(0, 10)}</div>
              </button>
            ))}
          </nav>

          {/* content */}
          <div className="min-w-0 flex-1 border border-neutral-800 p-5">
            {contentErr && (
              <div className="text-sm text-red-400">{contentErr}</div>
            )}
            {content === null && !contentErr && (
              <div className="text-sm text-neutral-500">Loading report…</div>
            )}
            {content && (
              <article className="prose prose-invert prose-sm max-w-none prose-headings:text-neutral-200 prose-p:text-neutral-400 prose-strong:text-neutral-200 prose-code:text-purple-300 prose-a:text-purple-400 prose-table:text-neutral-300 prose-th:text-neutral-300 prose-td:text-neutral-400">
                <ReactMarkdown>{content.markdown}</ReactMarkdown>
              </article>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
