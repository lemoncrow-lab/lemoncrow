import { useState } from "react";

import { SOURCE_LABELS, annotationSource, locationLabel } from "./annotationModel";
import { fetchReviewEvidenceContent } from "./reviewApi";
import type {
  Annotation,
  EvidenceRow,
  ImpactSite,
  ProvenanceInfo,
  ReviewEvidence,
} from "./types";

export function PanelCard({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section>
      <div className="mb-1.5 flex items-center gap-2">
        <h3 className="text-[10px] font-medium text-neutral-300">{title}</h3>
        <span className="flex-1" />
        {action}
      </div>
      {children}
    </section>
  );
}

const STATUS_TONE: Record<string, string> = {
  PASS: "text-emerald-300",
  FAIL: "text-rose-300",
  UNKNOWN: "text-amber-300",
  NOT_RUN: "text-amber-300",
};

export function Verification({ rows }: { rows: EvidenceRow[] }) {
  if (rows.length === 0) return <div className="text-[10px] text-neutral-600">No verification evidence recorded.</div>;
  return (
    <div className="space-y-2">
      {rows.map((row, index) => (
        <div key={`${row.name}:${row.scope ?? "review"}:${index}`} className="border-l border-neutral-800 pl-2">
          <div className="flex items-baseline gap-2 text-[10px]">
            <span className="min-w-0 flex-1 text-neutral-300">{row.name}</span>
            <span className={`font-mono ${STATUS_TONE[row.status] ?? "text-neutral-500"}`}>{row.status}</span>
          </div>
          {row.detail && <div className="mt-0.5 text-[9px] leading-4 text-neutral-600">{row.detail}</div>}
          <div className="mt-0.5 text-[9px] text-neutral-700">{row.scope === "file" ? "this file" : "review-wide"}{row.source ? ` · ${row.source}` : ""}</div>
        </div>
      ))}
    </div>
  );
}

export function Impact({ sites, onOpen }: { sites: ImpactSite[]; onOpen(site: ImpactSite): void }) {
  if (sites.length === 0) return <div className="text-[10px] text-neutral-600">No impact sites recorded.</div>;
  return (
    <div className="space-y-2">
      {sites.map((site, index) => (
        <button
          key={`${site.path}:${site.kind}:${index}`}
          type="button"
          onClick={() => onOpen(site)}
          className="block w-full border-l border-neutral-800 pl-2 text-left hover:border-neutral-600"
        >
          <div className="flex items-baseline gap-2 text-[10px]">
            <span className="min-w-0 flex-1 truncate font-mono text-neutral-300">{site.path}</span>
            <span className="text-neutral-600">{site.in_patch ? "in patch" : "outside patch"}</span>
          </div>
          <div className={`mt-0.5 text-[9px] ${site.uncertainty ? "text-amber-400/80" : "text-neutral-600"}`}>
            {site.uncertainty ? "possible " : ""}{site.kind.replaceAll("_", " ")}{site.inspected_by_agent === false ? " · not inspected by author" : ""}
          </div>
          {site.uncertainty && <div className="mt-0.5 text-[9px] leading-4 text-amber-500/70">? {site.uncertainty}</div>}
          {site.snippet && <div className="mt-1 truncate font-mono text-[9px] text-neutral-500">{site.snippet}</div>}
        </button>
      ))}
    </div>
  );
}

export function emptyProvenance(): ProvenanceInfo {
  return {
    status: "unknown",
    host: "",
    model: "",
    session_id: "",
    task: "",
    certainty: "none",
    match_confidence: 0,
    match_reason: "",
    commands_run: [],
    subagents: [],
    reads_recorded: false,
    inspected: null,
    uninspected_impacted: [],
  };
}

export function Provenance({ info }: { info: ProvenanceInfo }) {
  if (!info.host && !info.session_id && !info.task) {
    return <div className="text-[10px] text-neutral-600">No exact authoring provenance recorded.</div>;
  }
  return (
    <div className="space-y-1.5 text-[10px]">
      <div className="flex gap-2"><span className="w-20 shrink-0 text-neutral-600">Host</span><span className="text-neutral-300">{info.host || "unknown"}</span></div>
      <div className="flex gap-2"><span className="w-20 shrink-0 text-neutral-600">Certainty</span><span className="text-neutral-300">{info.certainty || "unknown"}</span></div>
      {info.model && <div className="flex gap-2"><span className="w-20 shrink-0 text-neutral-600">Model</span><span className="text-neutral-300">{info.model}</span></div>}
      {info.session_id && <div className="flex gap-2"><span className="w-20 shrink-0 text-neutral-600">Session</span><span className="min-w-0 truncate font-mono text-neutral-300">{info.session_id}</span></div>}
      {info.task && <div className="pt-1 leading-4 text-neutral-400">{info.task}</div>}
      <div className="flex gap-2">
        <span className="w-20 shrink-0 text-neutral-600">Reads</span>
        {!info.reads_recorded ? (
          <span className="text-neutral-500">reads not recorded</span>
        ) : info.inspected === false ? (
          <span className="text-amber-300">authoring agent did not read this file</span>
        ) : info.inspected === true ? (
          <span className="text-emerald-400/80">file inspected by authoring agent</span>
        ) : (
          <span className="text-neutral-500">inspection unknown</span>
        )}
      </div>
      {info.commands_run.length > 0 && (
        <div className="pt-1">
          <div className="text-[9px] uppercase tracking-wider text-neutral-600">Commands run</div>
          <div className="mt-1 space-y-1">{info.commands_run.map((command, index) => <div key={`${index}:${command}`} className="truncate font-mono text-[9px] text-neutral-500" title={command}>{command}</div>)}</div>
        </div>
      )}
      {info.match_reason && <div className="text-[9px] leading-4 text-neutral-600">{info.match_reason}</div>}
    </div>
  );
}

export function Discussion({ annotations, path }: { annotations: Annotation[]; path: string }) {
  const rows = annotations.filter((item) => item.path === path && !item.parent_id && item.state !== "obsolete");
  if (rows.length === 0) return <div className="text-[10px] text-neutral-600">No discussion on this file.</div>;
  return (
    <div className="space-y-2">
      {rows.map((item) => (
        <div key={item.id} className="border-l border-neutral-800 pl-2">
          <div className="flex items-center gap-2 text-[9px] text-neutral-600">
            <span>{SOURCE_LABELS[annotationSource(item)]}</span>
            <span>{item.file_level ? "file" : locationLabel(item.start_line, item.end_line)}</span>
            <span>{item.state}</span>
          </div>
          {item.title && <div className="mt-1 text-[10px] font-medium text-neutral-300">{item.title}</div>}
          <div className="mt-1 whitespace-pre-wrap text-[10px] leading-4 text-neutral-400">{item.body}</div>
        </div>
      ))}
    </div>
  );
}

export function ArtifactCard({ artifact }: { artifact: ReviewEvidence }) {
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState("");

  async function openArtifact() {
    if (artifact.url) {
      window.open(artifact.url, "_blank", "noopener,noreferrer");
      return;
    }
    if (!artifact.content_url || opening) return;
    setOpening(true);
    try {
      const blob = await fetchReviewEvidenceContent(artifact.id);
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener,noreferrer");
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setOpening(false);
    }
  }

  return (
    <div className="border border-neutral-800 bg-neutral-900/20 p-2">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[10px] text-neutral-300">{artifact.title || artifact.kind}</div>
          <div className="mt-0.5 font-mono text-[9px] text-neutral-600">
            {artifact.kind} · {artifact.source}{artifact.verification_status ? ` · ${artifact.verification_status}` : ""}{artifact.status === "stale" ? " · previous revision" : ""}
          </div>
        </div>
        {(artifact.url || artifact.content_url) && (
          <button type="button" onClick={() => void openArtifact()} disabled={opening} className="text-[9px] text-sky-400 disabled:opacity-40">{opening ? "Opening…" : "Open"}</button>
        )}
      </div>
      {artifact.status === "stale" && <div className="mt-1 text-[9px] leading-4 text-amber-400/70">This artifact is from a previous revision and is not current proof.</div>}
      {artifact.detail && <div className="mt-1 text-[9px] leading-4 text-neutral-500">{artifact.detail}</div>}
      {error && <div className="mt-1 text-[9px] text-amber-300">{error}</div>}
    </div>
  );
}
