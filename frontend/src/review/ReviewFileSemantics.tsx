import type { ReactNode } from "react";
import { FileSymlink, FolderGit2, GitCompareArrows, KeyRound, Package, TriangleAlert, Wand2 } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import { webHref } from "./safeLinks";
import type { ReviewDependencyChange, ReviewDependencyProjection, ReviewSpecialChange } from "./types";

/**
 * Semantic presentations that come before raw diff text (plan 8F): special Git
 * changes, package-level dependency review and generated-file provenance. Each
 * renders only server projections; nothing here re-derives a fact from a patch.
 */

function shortCommit(value: string): string {
  return value ? value.slice(0, 12) : "";
}

function shortDigest(value: string): string {
  if (!value) return "";
  return value.length > 26 ? `${value.slice(0, 18)}…${value.slice(-7)}` : value;
}

function Arrow({ before, after, mono = true }: { before: ReactNode; after: ReactNode; mono?: boolean }) {
  return (
    <span className={`inline-flex flex-wrap items-baseline gap-x-1.5 ${mono ? "font-mono" : ""}`}>
      <span className="text-rose-300/90">{before}</span>
      <span aria-hidden="true" className="text-muted-foreground">→</span>
      <span className="sr-only">to</span>
      <span className="text-emerald-300/90">{after}</span>
    </span>
  );
}

function specialTitle(change: ReviewSpecialChange): string {
  if (change.kind === "rename") return change.copied ? "Copied without content changes" : "Renamed without content changes";
  if (change.kind === "mode") {
    if (change.executable === "added") return "Executable bit added";
    if (change.executable === "removed") return "Executable bit removed";
    return "File mode changed";
  }
  if (change.kind === "symlink") {
    if (change.old_type === "absent") return "Symlink added";
    if (change.new_type === "absent") return "Symlink removed";
    if (change.old_type !== change.new_type) return change.new_type === "symlink" ? "File replaced by a symlink" : "Symlink replaced by a file";
    return change.target_unchanged ? "Symlink moved" : "Symlink target changed";
  }
  if (change.kind === "submodule") {
    if (change.change === "added") return "Submodule added";
    if (change.change === "removed") return "Submodule removed";
    return "Submodule pointer moved";
  }
  return "Changed without content lines";
}

function SpecialIcon({ change }: { change: ReviewSpecialChange }) {
  const Icon = change.kind === "symlink" ? FileSymlink : change.kind === "submodule" ? FolderGit2 : change.kind === "mode" ? KeyRound : GitCompareArrows;
  return <Icon size={14} aria-hidden="true" className="shrink-0 text-sky-300" />;
}

function SpecialFacts({ change }: { change: ReviewSpecialChange }) {
  if (change.kind === "rename") {
    return (
      <>
        <div data-testid="special-change-paths"><Arrow before={change.old_path} after={change.new_path} /></div>
        <div className="text-muted-foreground">
          {change.similarity > 0 ? `${change.similarity}% similar · ` : ""}no content lines changed{change.mode ? ` · mode ${change.mode}` : ""}
        </div>
      </>
    );
  }
  if (change.kind === "mode") {
    return (
      <>
        {change.old_path && <div data-testid="special-change-paths"><Arrow before={change.old_path} after={change.new_path} /></div>}
        <div data-testid="special-change-mode"><Arrow before={change.old_mode} after={change.new_mode} /></div>
        <div className="text-muted-foreground">
          {change.executable === "added"
            ? "The file can now be executed directly; no content lines changed."
            : change.executable === "removed"
              ? "The file can no longer be executed directly; no content lines changed."
              : "Permission bits changed; no content lines changed."}
        </div>
      </>
    );
  }
  if (change.kind === "symlink") {
    const before = change.old_type === "symlink" ? (change.old_target || "target not recorded") : change.old_type === "file" ? "regular file" : "absent";
    const after = change.new_type === "symlink" ? (change.new_target || "target not recorded") : change.new_type === "file" ? "regular file" : "absent";
    return (
      <>
        {change.old_path && change.old_path !== change.new_path && (
          <div data-testid="special-change-paths"><Arrow before={change.old_path} after={change.new_path} /></div>
        )}
        <div data-testid="special-change-target">
          {change.target_unchanged ? <span className="font-mono">target unchanged</span> : <Arrow before={before} after={after} />}
        </div>
        {change.resolved_target && change.resolved_target !== change.new_target && (
          <div className="text-muted-foreground">Resolves to <span className="font-mono">{change.resolved_target}</span></div>
        )}
        {!change.target_recorded && <div className="text-amber-300/90">This Snapshot did not record the link target text.</div>}
        {change.warnings.length > 0 && (
          <ul role="alert" className="mt-1 space-y-0.5 rounded border border-amber-800/60 bg-amber-950/30 px-2 py-1 text-amber-200" data-testid="special-change-warning">
            {change.warnings.map((warning) => (
              <li key={warning} className="flex items-start gap-1.5"><TriangleAlert size={12} aria-hidden="true" className="mt-0.5 shrink-0" />{warning}</li>
            ))}
          </ul>
        )}
      </>
    );
  }
  if (change.kind === "submodule") {
    const compare = webHref(change.compare_url);
    const commit = webHref(change.commit_url);
    return (
      <>
        <div data-testid="special-change-commits">
          <Arrow before={shortCommit(change.old_commit) || "none"} after={shortCommit(change.new_commit) || "none"} />
        </div>
        {change.repository_url
          ? <div className="text-muted-foreground">Repository <span className="font-mono text-neutral-300">{change.repository_url}</span></div>
          : <div className="text-muted-foreground">Linked repository not recorded in .gitmodules.</div>}
        {(compare || commit) && (
          <div className="flex flex-wrap gap-3">
            {compare && <a className="text-sky-300 hover:underline" href={compare} target="_blank" rel="noopener noreferrer">Compare commits</a>}
            {commit && <a className="text-sky-300 hover:underline" href={commit} target="_blank" rel="noopener noreferrer">Open new commit</a>}
          </div>
        )}
      </>
    );
  }
  return (
    <>
      {change.old_path && <div data-testid="special-change-paths"><Arrow before={change.old_path} after={change.new_path} /></div>}
      <div className="text-muted-foreground">
        {change.modes_recorded
          ? <>Mode <Arrow before={change.old_mode} after={change.new_mode} /></>
          : "Git recorded this path as changed with no content lines. This Snapshot did not record per-file modes, so whether permissions changed is unknown."}
      </div>
    </>
  );
}

/** One special Git change in the Code stream: the semantic delta first, git's raw patch secondary. */
export function SpecialGitChange({
  path,
  change,
  patch,
  headerControls,
}: {
  path: string;
  change: ReviewSpecialChange;
  patch: string;
  headerControls?: ReactNode;
}) {
  return (
    <section className="border-b border-border bg-background" data-testid={`special-change:${path}`} data-special-kind={change.kind} aria-label={`${specialTitle(change)}: ${path}`}>
      <div className="flex min-h-10 items-center gap-2 border-b border-border/70 px-3 py-1.5">
        <SpecialIcon change={change} />
        <span className="min-w-0 flex-1 truncate font-mono text-body text-neutral-200" title={path}>{path}</span>
        {headerControls}
      </div>
      <div className="space-y-1 px-4 py-3 text-caption leading-5 text-neutral-300">
        <div className="text-label font-medium text-neutral-100">{specialTitle(change)}</div>
        <SpecialFacts change={change} />
        {patch && (
          <details className="pt-1">
            <summary className="cursor-pointer select-none text-muted-foreground hover:text-neutral-200">Raw Git patch</summary>
            <pre className="mt-1 overflow-x-auto rounded border border-border/70 bg-muted/30 p-2 font-mono text-micro text-neutral-400">{patch}</pre>
          </details>
        )}
      </div>
    </section>
  );
}

function dependencyChangeLabel(change: ReviewDependencyChange): string {
  if (change.changes.includes("added")) return "added";
  if (change.changes.includes("removed")) return "removed";
  if (change.changes.includes("version")) return `${change.before_version || "?"} → ${change.after_version || "?"}`;
  if (change.changes.includes("requirement")) return `${change.before_requirement || "?"} → ${change.after_requirement || "?"}`;
  return change.changes.join(", ").replaceAll("_", " ");
}

function DependencyRow({ change, onOpenTarget }: { change: ReviewDependencyChange; onOpenTarget?(targetId: string): void }) {
  const requirementChanged = change.before_requirement !== change.after_requirement && (change.before_requirement || change.after_requirement);
  const versionChanged = change.before_version !== change.after_version && (change.before_version || change.after_version);
  return (
    <article className="rounded-md border border-border/80 px-2.5 py-2 text-caption" data-testid={`dependency-change:${change.ecosystem}:${change.identity}`}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="font-medium text-neutral-100">{change.name}</span>
        <span className="text-muted-foreground">{change.package_root ? `${change.ecosystem} · ${change.package_root}` : change.ecosystem}</span>
        <span className={change.direct ? "text-sky-300" : "text-muted-foreground"}>{change.direct ? `direct${change.scope ? ` · ${change.scope}` : ""}` : "transitive"}</span>
        <span className="font-mono text-neutral-200">{dependencyChangeLabel(change)}</span>
      </div>
      <div className="mt-1 space-y-0.5 leading-4 text-muted-foreground">
        {requirementChanged && <div>Requirement · <Arrow before={change.before_requirement || "none"} after={change.after_requirement || "none"} /></div>}
        {versionChanged && !change.changes.includes("version") && <div>Version · <Arrow before={change.before_version || "none"} after={change.after_version || "none"} /></div>}
        {change.before_source !== change.after_source && <div>Source · <Arrow before={change.before_source || "none"} after={change.after_source || "none"} /></div>}
        {change.before_integrity !== change.after_integrity && (
          <div>Integrity · <Arrow before={shortDigest(change.before_integrity) || "none"} after={shortDigest(change.after_integrity) || "none"} /></div>
        )}
        {change.dependency_paths[0] && (
          <div data-testid="dependency-path">
            Path · <span className="font-mono">{change.dependency_paths[0].join(" → ")}</span>
            {change.dependency_paths.length > 1 ? ` · +${change.dependency_paths.length - 1} more` : ""}
          </div>
        )}
        {change.direct && change.transitive_changes > 0 && <div>{`${change.transitive_changes} transitive component${change.transitive_changes === 1 ? "" : "s"} changed beneath it`}</div>}
        {change.license && <div>License · {change.license}{change.license_source ? ` (${change.license_source})` : ""}</div>}
        {change.artifacts.map((artifact) => artifact.artifact_digest && (
          <div key={`${artifact.sbom_evidence_id}:${artifact.artifact_digest}`}>
            SBOM artifact · <span className="font-mono" title={artifact.artifact_digest}>{shortDigest(artifact.artifact_digest)}</span>{artifact.standard ? ` · ${artifact.standard}` : ""}
          </div>
        ))}
        {change.vulnerabilities.map((finding) => (
          <div key={`${finding.evidence_id}:${finding.advisory}:${finding.severity}`} className="text-amber-300">
            Vulnerability · {finding.advisory || "known finding"}{finding.severity ? ` · ${finding.severity}` : ""} · {finding.source || "source unknown"} · {finding.observed_at || "time unknown"}
          </div>
        ))}
      </div>
      {onOpenTarget && change.target_id && (
        <UiButton variant="link" size="compact" type="button" className="mt-1 px-0 text-caption" onClick={() => onOpenTarget(change.target_id)}>
          Open dependency target
        </UiButton>
      )}
    </article>
  );
}

function supplyChainLine(review: ReviewDependencyProjection): string {
  const { sboms, provenance, vulnerability_scans: scans } = review.supply_chain;
  if (sboms.length + provenance.length + scans.length === 0) return "No SBOM, provenance or vulnerability-scan evidence is attached to this Snapshot.";
  return `${sboms.length} SBOM · ${provenance.length} provenance · ${scans.length} vulnerability scan`;
}

/** Whether the package-level dependency review has anything to say for this Snapshot. */
export function dependencyReviewRelevant(review: ReviewDependencyProjection | null | undefined): review is ReviewDependencyProjection {
  return Boolean(review?.supported && (review.changes.length > 0 || review.degraded.length > 0 || review.lockfiles.length > 0));
}

/** Package semantics for the whole Snapshot (Brief), from the existing dependency projection. */
export function DependencyReviewSummary({
  review,
  limit,
  onOpenTarget,
}: {
  review: ReviewDependencyProjection;
  limit?: number;
  onOpenTarget?(targetId: string): void;
}) {
  const shown = limit === undefined ? review.changes : review.changes.slice(0, limit);
  return (
    <div className="flex flex-col gap-1.5" data-testid="dependency-review">
      <div className="text-caption text-muted-foreground">
        {`${review.summary.semantic_changes} package change${review.summary.semantic_changes === 1 ? "" : "s"} · ${review.summary.direct_changes} direct · ${review.summary.transitive_changes} transitive`}
      </div>
      {review.degraded.length > 0 && (
        <div className="rounded-md border border-amber-900/40 bg-amber-950/10 px-2.5 py-1.5 text-caption text-amber-300">{review.degraded.join(" · ")}</div>
      )}
      {review.changes.length === 0 && review.degraded.length === 0 && (
        <div className="text-caption text-muted-foreground">Manifest or lockfile changed, but the resolved dependency graph has no semantic component delta.</div>
      )}
      {shown.map((change) => <DependencyRow key={`${change.ecosystem}:${change.identity}`} change={change} onOpenTarget={onOpenTarget} />)}
      {limit !== undefined && review.changes.length > limit && (
        <div className="text-caption text-muted-foreground">{`+${review.changes.length - limit} more in the lockfile views`}</div>
      )}
      <div className="text-caption text-muted-foreground">{supplyChainLine(review)}</div>
    </div>
  );
}

function changesForFile(review: ReviewDependencyProjection, path: string): ReviewDependencyChange[] {
  return review.changes.filter((change) => change.lockfile === path || change.manifest === path);
}

/** True when the dependency projection describes this manifest or lockfile. */
export function dependencyFileRelevant(review: ReviewDependencyProjection | null | undefined, path: string): boolean {
  if (!review?.supported) return false;
  return review.lockfiles.some((lock) => lock.path === path || lock.manifest === path) || changesForFile(review, path).length > 0;
}

/** Package semantics for one manifest/lockfile, rendered before its raw lines in Code. */
export function DependencyFileSemantics({ review, path }: { review: ReviewDependencyProjection; path: string }) {
  const rows = changesForFile(review, path);
  const lock = review.lockfiles.find((item) => item.path === path);
  return (
    <section className="border-b border-border bg-muted/10 px-4 py-3" data-testid={`dependency-file:${path}`} aria-label={`Package changes: ${path}`}>
      <div className="mb-1.5 flex items-center gap-2 text-label font-medium text-neutral-100">
        <Package size={14} aria-hidden="true" className="text-sky-300" />
        <span>Package changes</span>
        <span className="text-caption font-normal text-muted-foreground">
          {lock
            ? `${lock.semantic_changes} semantic · ${lock.direct_changes} direct · ${lock.transitive_changes} transitive`
            : `${rows.length} declared`}
        </span>
      </div>
      {rows.length === 0
        ? <div className="text-caption text-muted-foreground">The resolved dependency graph has no semantic component delta for this file.</div>
        : <div className="flex flex-col gap-1.5">{rows.map((change) => <DependencyRow key={`${change.ecosystem}:${change.identity}`} change={change} />)}</div>}
      <div className="mt-1.5 text-caption text-muted-foreground">Raw lockfile lines follow.</div>
    </section>
  );
}

export interface ReviewFileRole {
  role: string;
  source: "gitattributes" | "inferred" | string;
  collapsedDefault: boolean;
}

/** Generated/vendor provenance first; the raw generated diff is a secondary disclosure. */
export function GeneratedFileSemantics({
  path,
  role,
  generatedFrom,
  rawShown,
  onToggleRaw,
}: {
  path: string;
  role: ReviewFileRole;
  generatedFrom: string;
  rawShown: boolean;
  onToggleRaw(): void;
}) {
  return (
    <section className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border bg-muted/10 px-4 py-2 text-caption" data-testid={`generated-file:${path}`} aria-label={`Generated file: ${path}`}>
      <Wand2 size={13} aria-hidden="true" className="shrink-0 text-muted-foreground" />
      <span className="font-medium text-neutral-200">Generated or vendored</span>
      <span className="text-muted-foreground">
        {role.source === "gitattributes" ? "marked by trusted-base .gitattributes" : "inferred from its path"}
      </span>
      {generatedFrom && <span className="text-neutral-300">Generated from <span className="font-mono">{generatedFrom}</span></span>}
      <span className="flex-1" />
      <UiButton variant="ghost" size="compact" type="button" aria-expanded={rawShown} onClick={onToggleRaw}>
        {rawShown ? "Hide raw diff" : "Show raw diff"}
      </UiButton>
    </section>
  );
}

/** The source a generated lockfile was resolved from, when the dependency projection names it. */
export function generatedSource(review: ReviewDependencyProjection | null | undefined, path: string): string {
  const lock = review?.lockfiles.find((item) => item.path === path);
  return lock?.manifest ?? "";
}
