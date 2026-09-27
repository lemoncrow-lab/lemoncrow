import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";

import "./reviewUi.css";
import BrowserRegressionResult from "./BrowserRegressionResult";
import { trapModalTab } from "./focusTrap";
import { fetchReviewDependencies, fetchReviewVerification, waiveReviewVerificationCheck } from "./reviewApi";
import type {
  ReviewChapter,
  ReviewDependencyChange,
  ReviewDependencyProjection,
  ReviewOverview,
  ReviewSurfaceInfo,
  ReviewVerificationCheck,
  ReviewVerificationProjection,
} from "./types";

function verificationTone(status: string): string {
  if (status === "PASS") return "text-emerald-300";
  if (status === "FAIL") return "text-rose-300";
  return "text-amber-300";
}

function shortDigest(value: string): string {
  if (!value) return "";
  return value.length > 26 ? `${value.slice(0, 18)}…${value.slice(-7)}` : value;
}

function dependencyChangeLabel(change: ReviewDependencyChange): string {
  if (change.changes.includes("added")) return "added";
  if (change.changes.includes("removed")) return "removed";
  if (change.changes.includes("version")) return `${change.before_version || "?"} → ${change.after_version || "?"}`;
  if (change.changes.includes("requirement")) return `${change.before_requirement || "?"} → ${change.after_requirement || "?"}`;
  return change.changes.join(", ").replaceAll("_", " ");
}

interface ReviewOverviewSheetProps {
  overview: ReviewOverview;
  surfaces?: ReviewSurfaceInfo[];
  onClose(): void;
  onSelectPath(path: string): void;
  onSelectTarget?(targetId: string, path: string): void;
  onSelectSurface?(surfaceKey: string): void;
  /** Present only where a human may record a waiver; called after one is recorded. */
  onWaived?(): void;
}

function WaiverForm({ check, onSubmit, onCancel }: {
  check: ReviewVerificationCheck;
  onSubmit(reason: string): Promise<void>;
  onCancel(): void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <form
      className="mt-1.5 space-y-1.5"
      onSubmit={(event) => {
        event.preventDefault();
        if (!reason.trim() || busy) return;
        setBusy(true);
        setError("");
        void onSubmit(reason.trim()).catch((failure: unknown) => {
          setError(String(failure instanceof Error ? failure.message : failure));
          setBusy(false);
        });
      }}
    >
      <textarea
        aria-label={`Waiver reason for ${check.name}`}
        className="w-full rounded border border-neutral-800 bg-neutral-950 px-2 py-1 text-[10px] text-neutral-200"
        rows={2}
        placeholder="Why this check does not need to pass on this Snapshot"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      <div className="flex items-center gap-2">
        <button type="submit" className="review-compact-action h-6" disabled={!reason.trim() || busy}>Record waiver</button>
        <button type="button" className="review-compact-action h-6" onClick={onCancel} disabled={busy}>Cancel</button>
        {error && <span className="text-[10px] text-rose-300" role="alert">{error}</span>}
      </div>
    </form>
  );
}

export default function ReviewOverviewSheet({
  overview,
  surfaces = [],
  onClose,
  onSelectPath,
  onSelectTarget,
  onSelectSurface,
  onWaived,
}: ReviewOverviewSheetProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const brief = overview.brief;
  const changes = brief?.major_changes ?? [];
  const areas = overview.chapters?.intent ?? [];
  const areasByKey = new Map(areas.map((area) => [area.key, area] as const));
  const conceptualAreas = changes
    .map((change) => areasByKey.get(change.key))
    .filter((area): area is ReviewChapter => Boolean(area));
  const dependencyAreas = overview.chapters?.dependency ?? [];
  const commitAreas = overview.chapters?.commits ?? [];
  const briefVerification = brief?.verification ?? { pass: 0, fail: 0, not_run: 0, unknown: 0 };
  const artifacts = brief?.artifacts ?? { current: 0, stale: 0 };
  const provenance = overview.provenance;
  const attributionSessions = Array.from(
    new Map(
      (overview.attributions ?? [])
        .filter((item) => item.certainty === "exact" && item.host && item.session_id)
        .map((item) => [`${item.host}:${item.session_id}`, item] as const),
    ).values(),
  );
  const singleAttribution = attributionSessions.length === 1 ? attributionSessions[0] : undefined;
  const provenanceHost = singleAttribution?.host || provenance.host || overview.revision.provenance_host || "unknown";
  const provenanceModel = singleAttribution?.model || provenance.model || overview.revision.provenance_model || "";
  const provenanceCertainty = singleAttribution?.certainty || provenance.certainty || overview.revision.provenance_certainty || "unknown";
  const [snapshotVerification, setSnapshotVerification] = useState<ReviewVerificationProjection | null>(null);
  const [dependencyReview, setDependencyReview] = useState<ReviewDependencyProjection | null>(null);
  const [snapshotSignalsError, setSnapshotSignalsError] = useState("");
  const verification = snapshotVerification?.summary ?? { ...briefVerification, stale: 0 };
  const [waiving, setWaiving] = useState("");

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  useEffect(() => {
    let cancelled = false;
    setSnapshotVerification(null);
    setDependencyReview(null);
    setSnapshotSignalsError("");
    void Promise.allSettled([
      fetchReviewVerification(overview.session.id, overview.revision.id),
      fetchReviewDependencies(overview.session.id, overview.revision.id),
    ]).then(([verificationResult, dependencyResult]) => {
      if (cancelled) return;
      if (verificationResult.status === "fulfilled") setSnapshotVerification(verificationResult.value);
      if (dependencyResult.status === "fulfilled") setDependencyReview(dependencyResult.value);
      const failures = [verificationResult, dependencyResult]
        .filter((result) => result.status === "rejected")
        .map((result) => result.status === "rejected" ? String(result.reason instanceof Error ? result.reason.message : result.reason) : "");
      setSnapshotSignalsError(failures.filter(Boolean).join(" · "));
    });
    return () => { cancelled = true; };
  }, [overview.revision.id, overview.session.id]);

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
              {brief?.summary || overview.title || "Current Snapshot"}
            </div>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close change overview" className="review-icon-button">
            <X size={14} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {conceptualAreas.length > 0 && (
            <section>
              <div className="mb-2 flex items-center justify-between gap-3">
                <div className="review-kicker">Major changes</div>
                <div className="text-[10px] text-neutral-600">{conceptualAreas.length} concepts</div>
              </div>
              <div className="space-y-2">
                {conceptualAreas.map((area) => {
                  const firstPath = area.rows[0]?.path ?? "";
                  const progress = area.progress ?? {
                    target_count: area.target_count ?? 0,
                    reviewed: 0,
                    changed_since_review: area.changed_count,
                    needs_changes: 0,
                    unreviewed: area.target_count ?? 0,
                    unknown: 0,
                  };
                  const targets = area.targets ?? [];
                  return (
                    <article key={area.key} className="rounded-md border border-neutral-800/80 bg-neutral-900/20 px-3 py-2.5">
                      <div className="flex items-start gap-3">
                        <button
                          type="button"
                          disabled={!firstPath}
                          onClick={() => {
                            if (!firstPath) return;
                            onClose();
                            onSelectPath(firstPath);
                          }}
                          className="min-w-0 flex-1 text-left disabled:cursor-default"
                          aria-label={firstPath ? `Open ${area.label} conceptual change` : `${area.label} conceptual change`}
                        >
                          <div className="text-[11px] font-medium text-neutral-200">{area.label}</div>
                          <div className="mt-0.5 text-[10px] leading-4 text-neutral-500">
                            {area.explanation || area.reason}
                          </div>
                        </button>
                        <div className="shrink-0 text-right font-mono text-[10px] leading-4 text-neutral-500">
                          <div>{progress.reviewed}/{progress.target_count} reviewed</div>
                          {progress.changed_since_review > 0 && <div className="text-amber-300">{progress.changed_since_review} changed</div>}
                          {progress.needs_changes > 0 && <div className="text-rose-300">{progress.needs_changes} need changes</div>}
                        </div>
                      </div>
                      <details className="mt-2 border-t border-neutral-800/70 pt-1.5">
                        <summary className="cursor-pointer text-[10px] text-neutral-500 hover:text-neutral-300">
                          {area.file_count} file{area.file_count === 1 ? "" : "s"} · {progress.target_count} review area{progress.target_count === 1 ? "" : "s"}
                        </summary>
                        <div className="mt-1.5 grid gap-1 md:grid-cols-2">
                          <div className="min-w-0">
                            <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-neutral-700">Files</div>
                            {area.rows.map((row) => (
                              <button
                                key={row.path}
                                type="button"
                                onClick={() => {
                                  onClose();
                                  onSelectPath(row.path);
                                }}
                                className="block w-full truncate rounded px-1.5 py-1 text-left font-mono text-[10px] text-neutral-500 hover:bg-neutral-900 hover:text-neutral-200"
                                title={row.path}
                              >
                                {row.path}
                              </button>
                            ))}
                          </div>
                          <div className="min-w-0">
                            <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-neutral-700">Review areas</div>
                            {targets.map((target) => (
                              <button
                                key={target.target_id}
                                type="button"
                                onClick={() => {
                                  onClose();
                                  if (onSelectTarget) onSelectTarget(target.target_id, target.path);
                                  else onSelectPath(target.path);
                                }}
                                className="block w-full truncate rounded px-1.5 py-1 text-left text-[10px] text-neutral-500 hover:bg-neutral-900 hover:text-neutral-200"
                                title={`${target.path}:${target.start_line}${target.end_line !== target.start_line ? `-${target.end_line}` : ""}`}
                              >
                                <span className="text-neutral-400">{target.symbol || target.label || target.path}</span>
                                {target.start_line > 0 && <span className="ml-1 font-mono text-neutral-700">L{target.start_line}</span>}
                                {target.state === "changed_since_review" && <span className="ml-1 text-amber-300">changed</span>}
                              </button>
                            ))}
                          </div>
                        </div>
                      </details>
                    </article>
                  );
                })}
              </div>

              {(dependencyAreas.length > 0 || commitAreas.length > 0) && (
                <details className="mt-2 rounded-md border border-neutral-900 px-2.5 py-2">
                  <summary className="cursor-pointer text-[10px] text-neutral-600 hover:text-neutral-300">Dependency / commit lenses</summary>
                  <div className="mt-2 grid gap-3 md:grid-cols-2">
                    {dependencyAreas.length > 0 && (
                      <div>
                        <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-neutral-700">Dependency order</div>
                        {dependencyAreas.map((area) => (
                          <button key={area.key} type="button" onClick={() => { const path = area.rows[0]?.path; if (!path) return; onClose(); onSelectPath(path); }} className="block w-full truncate px-1 py-0.5 text-left text-[10px] text-neutral-500 hover:text-neutral-200">
                            {area.label}
                          </button>
                        ))}
                      </div>
                    )}
                    {commitAreas.length > 0 && (
                      <div>
                        <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-neutral-700">Commits</div>
                        {commitAreas.map((area) => (
                          <button key={area.key} type="button" onClick={() => { const path = area.rows[0]?.path; if (!path) return; onClose(); onSelectPath(path); }} className="block w-full truncate px-1 py-0.5 text-left text-[10px] text-neutral-500 hover:text-neutral-200" title={area.label}>
                            {area.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </details>
              )}
            </section>
          )}

          {surfaces.length > 0 && (
            <section className={conceptualAreas.length > 0 ? "mt-5" : ""}>
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
            <div className="mb-2 flex items-center justify-between gap-3">
              <div className="review-kicker">Snapshot verification</div>
              <div className="font-mono text-[10px] text-neutral-600">Snapshot {overview.revision.revision_number}</div>
            </div>
            {snapshotVerification ? (
              <div className="rounded-md border border-neutral-800/80 bg-neutral-900/20">
                {snapshotVerification.browser?.policy_error && (
                  <div className="border-b border-neutral-800/80 px-3 py-2 text-[10px] text-amber-300" role="alert">
                    Browser regression policy unavailable ({snapshotVerification.browser.policy_source || "review configuration"}): {snapshotVerification.browser.policy_error}
                  </div>
                )}
                {snapshotVerification.checks.length === 0 ? (
                  <div className="px-3 py-2.5 text-[10px] text-amber-300">No verification result ran against this Snapshot.</div>
                ) : (
                  <div className="divide-y divide-neutral-900">
                    {snapshotVerification.checks.map((check) => (
                      <div key={`${check.state}:${check.name}:${check.snapshot_id}`} className="flex items-start gap-3 px-3 py-2">
                        <span className={`w-16 shrink-0 font-mono text-[10px] ${check.waiver ? "text-neutral-300" : check.state === "current" ? verificationTone(check.status) : "text-neutral-600"}`}>
                          {check.waiver ? "waived" : check.state === "current" ? check.status : `${check.state} ${check.status}`}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px]">
                            <span className={check.state === "current" ? "text-neutral-200" : "text-neutral-500"}>{check.name}</span>
                            <span className="font-mono text-neutral-700">{check.lane}</span>
                            {check.browser && check.state === "current" && (
                              <span className={check.required ? "text-neutral-400" : "text-neutral-700"}>{check.required ? "required" : "optional"}</span>
                            )}
                            {check.state === "stale" && <span className="text-amber-400/70">Snapshot {check.snapshot_number ?? "?"}</span>}
                            {/* Recorded by the authoring session at an unknown point: context, never a result for this Snapshot. */}
                            {check.state === "session" && <span className="text-neutral-600">author session · not run against this Snapshot</span>}
                          </div>
                          {check.detail && <div className="mt-0.5 text-[10px] leading-4 text-neutral-600">{check.detail}</div>}
                          {check.waiver && (
                            <div className="mt-0.5 text-[10px] leading-4 text-amber-300/80">
                              Waived by a human: {check.waiver.reason} · last observed {check.status}
                            </div>
                          )}
                          {check.waivable && onWaived && waiving !== check.name && (
                            <button
                              type="button"
                              className="mt-1 text-[10px] text-neutral-500 underline-offset-2 hover:text-neutral-300 hover:underline"
                              aria-label={`Waive ${check.name}`}
                              onClick={() => setWaiving(check.name)}
                            >
                              Waive…
                            </button>
                          )}
                          {check.waivable && onWaived && waiving === check.name && (
                            <WaiverForm
                              check={check}
                              onCancel={() => setWaiving("")}
                              onSubmit={async (reason) => {
                                const result = await waiveReviewVerificationCheck(overview.session.id, {
                                  revision_id: overview.revision.id,
                                  name: check.name,
                                  reason,
                                });
                                setSnapshotVerification(result.verification);
                                setWaiving("");
                                onWaived();
                              }}
                            />
                          )}
                          {check.browser?.run && <BrowserRegressionResult check={check} />}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {snapshotVerification.artifacts.some((artifact) => ["sbom", "provenance", "vulnerability_scan"].includes(artifact.kind)) && (
                  <div className="border-t border-neutral-800/80 px-3 py-2">
                    <div className="mb-1 text-[9px] font-semibold uppercase tracking-wider text-neutral-700">Artifact evidence</div>
                    <div className="space-y-1">
                      {snapshotVerification.artifacts
                        .filter((artifact) => ["sbom", "provenance", "vulnerability_scan"].includes(artifact.kind))
                        .map((artifact) => (
                          <div key={artifact.id} className="flex flex-wrap items-center gap-x-2 text-[10px] text-neutral-500">
                            <span className={artifact.state === "stale" ? "text-amber-400/70" : "text-neutral-300"}>{artifact.kind.replaceAll("_", " ")}</span>
                            <span>{artifact.state === "stale" ? `stale · Snapshot ${artifact.snapshot_number ?? "?"}` : `Snapshot ${artifact.snapshot_number ?? "?"}`}</span>
                            {artifact.artifact_digest && <span className="font-mono" title={artifact.artifact_digest}>{shortDigest(artifact.artifact_digest)}</span>}
                            {artifact.standard && <span>{artifact.standard}{artifact.format_version ? ` ${artifact.format_version}` : ""}</span>}
                          </div>
                        ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="rounded-md border border-neutral-900 px-3 py-2 text-[10px] text-neutral-600">Loading Snapshot verification…</div>
            )}
          </section>

          {(dependencyReview?.supported || dependencyReview?.degraded.length) && (
            <section className="mt-5">
              <div className="mb-2 flex items-center justify-between gap-3">
                <div className="review-kicker">Dependency review</div>
                {dependencyReview && (
                  <div className="text-[10px] text-neutral-600">
                    {dependencyReview.summary.direct_changes} direct · {dependencyReview.summary.transitive_changes} transitive
                  </div>
                )}
              </div>
              {dependencyReview?.degraded.length ? (
                <div className="rounded-md border border-amber-900/40 bg-amber-950/10 px-3 py-2 text-[10px] text-amber-300">
                  {dependencyReview.degraded.join(" · ")}
                </div>
              ) : dependencyReview && dependencyReview.changes.length === 0 ? (
                <div className="rounded-md border border-neutral-900 px-3 py-2 text-[10px] text-neutral-500">
                  Supported manifest/lockfile changed, but the resolved dependency graph has no semantic component delta.
                </div>
              ) : dependencyReview ? (
                <div className="space-y-1.5">
                  {dependencyReview.changes.map((change) => (
                    <article key={`${change.ecosystem}:${change.identity}`} className="rounded-md border border-neutral-800/80 bg-neutral-900/20 px-3 py-2.5">
                      <div className="flex items-start gap-3">
                        <button
                          type="button"
                          className="min-w-0 flex-1 text-left"
                          onClick={() => {
                            onClose();
                            const path = change.manifest || change.lockfile;
                            if (onSelectTarget) onSelectTarget(change.target_id, path);
                            else if (path) onSelectPath(path);
                          }}
                        >
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px]">
                            <span className="font-medium text-neutral-200">{change.name}</span>
                            <span className="text-neutral-600">{change.package_root ? `${change.ecosystem} · ${change.package_root}` : change.ecosystem}</span>
                            <span className={change.direct ? "text-sky-300" : "text-neutral-500"}>{change.direct ? `direct · ${change.scope}` : "transitive"}</span>
                          </div>
                          <div className="mt-0.5 text-[10px] text-neutral-500">
                            {`${dependencyChangeLabel(change)}${change.direct && change.transitive_changes > 0 ? ` · ${change.transitive_changes} transitive component${change.transitive_changes === 1 ? "" : "s"} changed` : ""}`}
                          </div>
                        </button>
                        {change.license && <span className="shrink-0 text-[10px] text-neutral-500" title={`license from ${change.license_source || "evidence"}`}>{change.license}</span>}
                      </div>
                      {(change.dependency_paths.length > 0 || change.artifacts.length > 0 || change.vulnerabilities.length > 0 || change.before_source !== change.after_source || change.before_integrity !== change.after_integrity) && (
                        <div className="mt-2 space-y-1 border-t border-neutral-800/70 pt-1.5 text-[10px] leading-4 text-neutral-600">
                          {change.dependency_paths[0] && <div>Path · {change.dependency_paths[0].join(" → ")}</div>}
                          {change.before_source !== change.after_source && <div>Source · {change.before_source || "none"} → {change.after_source || "none"}</div>}
                          {change.before_integrity !== change.after_integrity && <div>Integrity/checksum changed</div>}
                          {change.artifacts.map((artifact) => artifact.artifact_digest && (
                            <div key={`${artifact.sbom_evidence_id}:${artifact.artifact_digest}`}>Artifact · <span className="font-mono" title={artifact.artifact_digest}>{shortDigest(artifact.artifact_digest)}</span> · {artifact.standard}</div>
                          ))}
                          {change.vulnerabilities.map((finding) => (
                            <div key={`${finding.evidence_id}:${finding.advisory}:${finding.severity}`} className="text-amber-300">
                              Vulnerability · {finding.advisory || "known finding"}{finding.severity ? ` · ${finding.severity}` : ""} · {finding.source || "source unknown"} · {finding.observed_at || "time unknown"}
                            </div>
                          ))}
                        </div>
                      )}
                    </article>
                  ))}
                </div>
              ) : null}
              {dependencyReview && (dependencyReview.supply_chain.sboms.length + dependencyReview.supply_chain.provenance.length + dependencyReview.supply_chain.vulnerability_scans.length > 0) && (
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-neutral-600">
                  <span>{dependencyReview.supply_chain.sboms.length} SBOM</span>
                  <span>{dependencyReview.supply_chain.provenance.length} provenance</span>
                  <span>{dependencyReview.supply_chain.vulnerability_scans.length} vulnerability scan</span>
                </div>
              )}
            </section>
          )}

          {snapshotSignalsError && (
            <div className="mt-3 text-[10px] text-amber-400/80">Some Snapshot evidence is unavailable: {snapshotSignalsError}</div>
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
                {artifacts.stale > 0 && <span className="text-amber-300">{artifacts.stale} previous Snapshot</span>}
              </div>
              <div className="flex min-w-0 items-center gap-2 py-2">
                <span className="w-20 shrink-0 text-neutral-600">Provenance</span>
                {attributionSessions.length > 1 ? (
                  <>
                    <span className="text-neutral-300">{attributionSessions.length} recorded sessions</span>
                    <span className="text-neutral-600">· no single author assumed</span>
                  </>
                ) : (
                  <>
                    <span className="truncate text-neutral-300">{provenanceHost}</span>
                    {provenanceModel && <span className="truncate text-neutral-500">· {provenanceModel}</span>}
                    <span className="text-neutral-600">· {provenanceCertainty}</span>
                  </>
                )}
              </div>
              {attributionSessions.length > 1 && (
                <div className="flex min-w-0 items-start gap-2 py-2">
                  <span className="w-20 shrink-0 text-neutral-600">Sessions</span>
                  <span className="min-w-0 text-neutral-300">
                    {attributionSessions.map((item) => `${item.host}${item.model ? ` · ${item.model}` : ""} · ${item.certainty}`).join("; ")}
                  </span>
                </div>
              )}
            </div>
          </section>
        </div>
      </section>
    </div>
  );
}
