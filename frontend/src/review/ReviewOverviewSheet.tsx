import { useEffect, useRef } from "react";

import { trapModalTab } from "./focusTrap";
import type { ReviewOverview } from "./types";

interface ReviewOverviewSheetProps {
  overview: ReviewOverview;
  onClose(): void;
  onSelectPath(path: string): void;
}

function Count({ label, value, tone = "text-neutral-200" }: { label: string; value: number; tone?: string }) {
  return (
    <div className="border border-neutral-800 bg-neutral-900/20 px-2.5 py-2">
      <div className={`font-mono text-[13px] ${tone}`}>{value}</div>
      <div className="mt-0.5 text-[9px] text-neutral-600">{label}</div>
    </div>
  );
}

export default function ReviewOverviewSheet({ overview, onClose, onSelectPath }: ReviewOverviewSheetProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const brief = overview.brief;
  const changes = brief?.major_changes ?? [];
  const verification = brief?.verification ?? { pass: 0, fail: 0, not_run: 0, unknown: 0 };
  const artifacts = brief?.artifacts ?? { current: 0, stale: 0 };
  const provenance = overview.provenance;

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  return (
    <div
      className="absolute inset-0 z-[85] flex items-start justify-center bg-black/50 px-4 pt-[10vh]"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="review-overview-title"
        className="flex max-h-[78vh] w-full max-w-[760px] flex-col border border-neutral-700 bg-neutral-950 shadow-2xl"
      >
        <div className="flex items-start gap-3 border-b border-neutral-800 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="review-overview-title" className="text-[13px] font-medium text-neutral-100">Change overview</h2>
            <div className="mt-1 text-[10px] leading-4 text-neutral-500">
              {brief?.summary || overview.title || "Current review revision"}
            </div>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close change overview" className="text-[10px] text-neutral-500 hover:text-neutral-200">Esc</button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {changes.length > 0 && (
            <section>
              <div className="mb-2 text-[9px] font-medium uppercase tracking-[0.14em] text-neutral-500">Major changes</div>
              <div className="space-y-1.5">
                {changes.map((change) => (
                  <button
                    key={change.key || change.label}
                    type="button"
                    disabled={!change.first_path}
                    onClick={() => {
                      if (!change.first_path) return;
                      onClose();
                      onSelectPath(change.first_path);
                    }}
                    className="flex w-full items-start gap-3 border border-neutral-800 bg-neutral-900/15 px-3 py-2.5 text-left hover:border-neutral-700 hover:bg-neutral-900/35 disabled:cursor-default disabled:hover:border-neutral-800 disabled:hover:bg-neutral-900/15"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[11px] text-neutral-200" title={change.label}>{change.label}</div>
                      {change.first_path && <div className="mt-0.5 truncate font-mono text-[9px] text-neutral-600">Start · {change.first_path}</div>}
                    </div>
                    <div className="shrink-0 text-right text-[9px] leading-4 text-neutral-500">
                      <div>{change.file_count} file{change.file_count === 1 ? "" : "s"} · {change.target_count} target{change.target_count === 1 ? "" : "s"}</div>
                      {change.attention_count > 0 && <div className="text-amber-400/70">{change.attention_count} elevated</div>}
                    </div>
                  </button>
                ))}
              </div>
            </section>
          )}

          <div className="mt-5 grid gap-5 md:grid-cols-3">
            <section>
              <div className="mb-2 text-[9px] font-medium uppercase tracking-[0.14em] text-neutral-500">Verification</div>
              <div className="grid grid-cols-2 gap-1.5">
                <Count label="passed" value={verification.pass} tone="text-emerald-300" />
                <Count label="failed" value={verification.fail} tone={verification.fail > 0 ? "text-rose-300" : "text-neutral-400"} />
                <Count label="not run" value={verification.not_run} tone={verification.not_run > 0 ? "text-amber-300" : "text-neutral-400"} />
                <Count label="unknown" value={verification.unknown} tone={verification.unknown > 0 ? "text-amber-300" : "text-neutral-400"} />
              </div>
            </section>

            <section>
              <div className="mb-2 text-[9px] font-medium uppercase tracking-[0.14em] text-neutral-500">Author provenance</div>
              <div className="space-y-1.5 border border-neutral-800 bg-neutral-900/15 p-3 text-[10px]">
                <div className="flex gap-2"><span className="w-16 shrink-0 text-neutral-600">Host</span><span className="truncate text-neutral-300">{provenance.host || overview.revision.provenance_host || "unknown"}</span></div>
                <div className="flex gap-2"><span className="w-16 shrink-0 text-neutral-600">Certainty</span><span className="truncate text-neutral-300">{provenance.certainty || overview.revision.provenance_certainty || "unknown"}</span></div>
                {(provenance.model || overview.revision.provenance_model) && <div className="flex gap-2"><span className="w-16 shrink-0 text-neutral-600">Model</span><span className="truncate text-neutral-300">{provenance.model || overview.revision.provenance_model}</span></div>}
              </div>
            </section>

            <section>
              <div className="mb-2 text-[9px] font-medium uppercase tracking-[0.14em] text-neutral-500">Evidence</div>
              <div className="grid grid-cols-2 gap-1.5">
                <Count label="current" value={artifacts.current} tone="text-sky-300" />
                <Count label="stale" value={artifacts.stale} tone={artifacts.stale > 0 ? "text-amber-300" : "text-neutral-400"} />
              </div>
              {brief?.themes?.length ? (
                <div className="mt-2 text-[9px] leading-4 text-neutral-600">Themes · {brief.themes.join(" · ")}</div>
              ) : null}
            </section>
          </div>
        </div>
      </section>
    </div>
  );
}
