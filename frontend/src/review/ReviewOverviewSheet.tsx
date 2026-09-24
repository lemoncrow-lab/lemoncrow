import { useEffect, useRef } from "react";
import { X } from "lucide-react";

import "./reviewUi.css";
import { trapModalTab } from "./focusTrap";
import type { ReviewOverview, ReviewSurfaceInfo } from "./types";

interface ReviewOverviewSheetProps {
  overview: ReviewOverview;
  surfaces?: ReviewSurfaceInfo[];
  onClose(): void;
  onSelectPath(path: string): void;
  onSelectSurface?(surfaceKey: string): void;
}

export default function ReviewOverviewSheet({
  overview,
  surfaces = [],
  onClose,
  onSelectPath,
  onSelectSurface,
}: ReviewOverviewSheetProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const brief = overview.brief;
  const changes = brief?.major_changes ?? [];
  const areas = overview.chapters?.intent ?? [];
  const verification = brief?.verification ?? { pass: 0, fail: 0, not_run: 0, unknown: 0 };
  const artifacts = brief?.artifacts ?? { current: 0, stale: 0 };
  const provenance = overview.provenance;

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  return (
    <div
      className="review-sheet-backdrop absolute z-[85] items-start justify-center px-4 pt-[10vh]"
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
        className="review-sheet flex max-h-[78vh] w-full max-w-[700px] flex-col rounded-[7px]"
      >
        <div className="flex items-start gap-3 border-b border-neutral-800/80 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="review-overview-title" className="text-[13px] font-semibold text-neutral-100">Change overview</h2>
            <div className="mt-0.5 text-[10px] leading-4 text-neutral-500">
              {brief?.summary || overview.title || "Current review revision"}
            </div>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close change overview" className="review-icon-button">
            <X size={14} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {areas.length > 0 && (
            <section>
              <div className="mb-2 flex items-center justify-between gap-3">
                <div className="review-kicker">Change areas</div>
                <div className="text-[10px] text-neutral-600">{areas.length}</div>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {areas.map((area) => {
                  const firstPath = area.rows[0]?.path ?? "";
                  const settled = Math.min(area.file_count, area.reviewed_count);
                  return (
                    <button
                      key={area.key}
                      type="button"
                      disabled={!firstPath}
                      onClick={() => {
                        if (!firstPath) return;
                        onClose();
                        onSelectPath(firstPath);
                      }}
                      className="flex min-w-0 items-center gap-2 rounded-md border border-neutral-800/80 px-2.5 py-1.5 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-900/45 disabled:cursor-default disabled:opacity-60"
                      aria-label={firstPath ? `Open ${area.label} change area` : `${area.label} change area`}
                      title={firstPath ? `Jump to ${firstPath}` : area.reason}
                    >
                      <span className="max-w-48 truncate text-[10px] font-medium text-neutral-300">{area.label}</span>
                      <span className="font-mono text-[10px] text-neutral-600">{area.file_count} file{area.file_count === 1 ? "" : "s"}</span>
                      {area.attention_count > 0 ? (
                        <span className="font-mono text-[10px] text-amber-400/80">{area.attention_count} attention</span>
                      ) : settled > 0 ? (
                        <span className="font-mono text-[10px] text-neutral-600">{settled} reviewed</span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            </section>
          )}

          {changes.length > 0 && (
            <section className={areas.length > 0 ? "mt-5" : ""}>
              <div className="review-kicker mb-2">Major changes</div>
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
                    className="flex w-full items-start gap-3 rounded-md border border-neutral-800/80 bg-neutral-900/20 px-3 py-2.5 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-900/50 disabled:cursor-default disabled:opacity-60"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[11px] text-neutral-200" title={change.label}>{change.label}</div>
                      {change.first_path && <div className="mt-0.5 truncate font-mono text-[10px] text-neutral-600">Start · {change.first_path}</div>}
                    </div>
                    <div className="shrink-0 text-right text-[10px] leading-4 text-neutral-500">
                      <div>{change.file_count} file{change.file_count === 1 ? "" : "s"} · {change.target_count} target{change.target_count === 1 ? "" : "s"}</div>
                      {change.attention_count > 0 && <div className="text-amber-400/70">{change.attention_count} elevated</div>}
                    </div>
                  </button>
                ))}
              </div>
            </section>
          )}

          {surfaces.length > 0 && (
            <section className={areas.length > 0 || changes.length > 0 ? "mt-5" : ""}>
              <div className="mb-2 flex items-center justify-between gap-3">
                <div className="review-kicker">Product surfaces</div>
                <div className="text-[10px] text-neutral-600">{surfaces.length}</div>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {surfaces.map((surface) => {
                  const key = `${surface.provider}:${surface.id}`;
                  const firstPath = surface.affected_paths[0] ?? "";
                  const locator = surface.locator || surface.title || surface.kind;
                  return (
                    <button
                      key={key}
                      type="button"
                      disabled={!firstPath}
                      aria-label={firstPath ? `Open ${locator} surface` : `${locator} surface`}
                      onClick={() => {
                        if (!firstPath) return;
                        onClose();
                        if (onSelectSurface) onSelectSurface(key);
                        else onSelectPath(firstPath);
                      }}
                      className="flex min-w-0 items-center gap-2 rounded-md border border-neutral-800/80 px-2 py-1.5 text-left transition-colors hover:bg-neutral-900/45 disabled:cursor-default disabled:opacity-60"
                      title={`Jump to ${locator}`}
                    >
                      <span className="max-w-56 truncate font-mono text-[10px] text-neutral-300">{locator}</span>
                      <span className="text-[10px] uppercase tracking-wider text-neutral-600">{surface.kind.startsWith("web") ? "web" : surface.kind.startsWith("api") ? "api" : surface.provider}</span>
                      {surface.affected_paths.length > 0 && <span className="font-mono text-[10px] text-neutral-700">{surface.affected_paths.length} file{surface.affected_paths.length === 1 ? "" : "s"}</span>}
                    </button>
                  );
                })}
              </div>
            </section>
          )}

          <section className="mt-5">
            <div className="review-kicker mb-2">Signals</div>
            <div className="divide-y divide-neutral-900 border-y border-neutral-900 text-[10px]">
              <div className="flex items-center gap-2 py-2">
                <span className="w-20 shrink-0 text-neutral-600">Verification</span>
                <span className="text-emerald-300">{verification.pass} passed</span>
                {verification.fail > 0 && <span className="text-rose-300">{verification.fail} failed</span>}
                {verification.not_run + verification.unknown > 0 && <span className="text-amber-300">{verification.not_run + verification.unknown} unresolved</span>}
              </div>
              <div className="flex items-center gap-2 py-2">
                <span className="w-20 shrink-0 text-neutral-600">Evidence</span>
                <span className="text-neutral-300">{artifacts.current} current</span>
                {artifacts.stale > 0 && <span className="text-amber-300">{artifacts.stale} previous revision</span>}
              </div>
              <div className="flex min-w-0 items-center gap-2 py-2">
                <span className="w-20 shrink-0 text-neutral-600">Author</span>
                <span className="truncate text-neutral-300">{provenance.host || overview.revision.provenance_host || "unknown"}</span>
                {(provenance.model || overview.revision.provenance_model) && <span className="truncate text-neutral-500">· {provenance.model || overview.revision.provenance_model}</span>}
                <span className="text-neutral-600">· {provenance.certainty || overview.revision.provenance_certainty || "unknown"}</span>
              </div>
            </div>
          </section>
        </div>
      </section>
    </div>
  );
}
