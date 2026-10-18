import { useCallback, useEffect, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import { PanelCard } from "./ContextSections";
import {
  fetchAnalysisArtifact,
  fetchArtifactAnalyses,
  postArtifactAnalysisRun,
  type AnalysisArtifactView,
  type AnalysisEntryRow,
  type AnalysisRecord,
  type ArtifactAnalysesProjection,
  compatSummary,
  flagMatrixSummary,
} from "./artifactAnalysesApi";

const VERDICT_TONE: Record<string, string> = {
  PASS: "text-emerald-300",
  FAIL: "text-rose-300",
  UNKNOWN: "text-amber-300",
  NOT_RUN: "text-amber-300",
};

const VERDICT_LABEL: Record<string, string> = { PASS: "Passed", FAIL: "Failed", UNKNOWN: "Unknown", NOT_RUN: "Not run" };

const SEVERITY_TONE: Record<string, string> = { high: "text-rose-300", medium: "text-amber-300", low: "text-muted-foreground" };

const PAIRING_TONE: Record<string, string> = {
  compatible: "text-emerald-300",
  incompatible: "text-rose-300",
  unknown: "text-amber-300",
  not_available: "text-amber-300",
  limited_by_budget: "text-amber-300",
};

const FLAG_TONE: Record<string, string> = {
  regressed: "text-rose-300",
  changed: "text-amber-300",
  limited_by_budget: "text-amber-300",
  not_run: "text-amber-300",
};

function stateLabel(state: string): string {
  return state === "limited_by_budget" ? "Limited by budget" : state.replaceAll("_", " ");
}

// lc-debt: at most 16 flag-matrix rows render here; the rest are counted. Upgrade path: page them in Behavior changes.
const MAX_MATRIX_ROWS = 16;

/** Mixed-version pairings and deploy order (item 41) or the flag on/off matrix (item 44) of one record. */
function SummaryDetails({ summary }: { summary: Record<string, unknown> }) {
  const compat = compatSummary(summary);
  const flags = flagMatrixSummary(summary);
  if (compat.pairings.length === 0 && compat.deployOrder.length === 0 && !flags) return null;
  return (
    <div className="mt-1 space-y-0.5">
      {compat.pairings.map((row) => (
        <div key={row.pairing} className="flex items-baseline gap-2 text-micro" data-testid="analysis-pairing" title={row.reason}>
          <span className="min-w-0 flex-1 truncate text-neutral-300">{row.label}</span>
          <span className={PAIRING_TONE[row.state] ?? "text-muted-foreground"}>{stateLabel(row.state)}</span>
        </div>
      ))}
      {compat.deployOrder.map((hint) => (
        <div key={hint} className="text-micro text-amber-300/80" data-testid="analysis-deploy-order">
          Deploy order: {hint}
        </div>
      ))}
      {flags?.rows.slice(0, MAX_MATRIX_ROWS).map((row) => (
        <div key={row.state} className="flex items-baseline gap-2 text-micro" data-testid="analysis-flag-state" title={row.reason || undefined}>
          <span className="min-w-0 flex-1 truncate font-mono text-neutral-300">
            {row.flag} {row.value}
          </span>
          {(row.tests_changed ?? 0) > 0 && <span className="text-muted-foreground">{row.tests_changed} test(s) changed</span>}
          <span className={FLAG_TONE[row.status] ?? "text-muted-foreground"}>{stateLabel(row.status)}</span>
        </div>
      ))}
      {flags && flags.rows.length > MAX_MATRIX_ROWS && (
        <div className="text-micro text-muted-foreground">+{flags.rows.length - MAX_MATRIX_ROWS} more flag states</div>
      )}
      {flags?.bound && flags.bound.limited_flags > 0 && (
        <div className="text-micro text-amber-300/80" data-testid="analysis-flag-bound">
          Limited by budget: {flags.bound.max_flags} of {flags.bound.touched} touched flags expanded at {flags.bound.depth || "the current"} depth
        </div>
      )}
    </div>
  );
}

function EntryAction({ entry, busy, onRun }: { entry: AnalysisEntryRow; busy: boolean; onRun: (entry: string, pick: boolean) => void }) {
  if (entry.status === "run") {
    return (
      <UiButton type="button" size="xs" variant="outline" disabled={busy} onClick={() => onRun(entry.id, false)}>
        Run
      </UiButton>
    );
  }
  if (entry.status === "on_request" || entry.status === "skipped" || entry.status === "not_planned") {
    return (
      <UiButton type="button" size="xs" variant="ghost" disabled={busy} onClick={() => onRun(entry.id, true)}>
        Add &amp; run
      </UiButton>
    );
  }
  return null;
}

function ResultCard({
  result,
  onOpen,
  opened,
}: {
  result: AnalysisRecord;
  onOpen: (result: AnalysisRecord, name: string) => void;
  opened: { key: string; view: AnalysisArtifactView | null; error: string } | null;
}) {
  const title = result.spec.title || result.spec.id;
  const ran = result.attempts.filter((row) => row.state === "ok" || row.state === "findings" || row.state === "failed");
  return (
    <div className="border-t border-border/60 py-2 first:border-t-0" data-testid={`analysis-result-${result.entry_id}-${result.spec.id}`}>
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-label text-neutral-200" title={`${result.entry_title} · ${result.spec.root}`}>
          {title}
        </span>
        <span className={`text-caption ${VERDICT_TONE[result.status] ?? "text-muted-foreground"}`}>{VERDICT_LABEL[result.status] ?? result.status}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-2 text-micro text-muted-foreground">
        <span>{result.entry_title}</span>
        <span>{result.sides.includes("old") ? "base vs head" : "head"}</span>
        {result.executor_class && <span>{result.executor_class.replace("_", " ")}</span>}
        {ran.length > 0 && <span>{ran.map((row) => (row.side === "old" ? "base" : "head") + (row.reused ? " (reused)" : "")).join(" · ")}</span>}
      </div>
      {result.reasons.map((reason) => (
        <div key={reason} className={`text-micro leading-relaxed ${reason.startsWith("Limited by budget") ? "text-amber-300/80" : "text-muted-foreground"}`}>
          {reason}
        </div>
      ))}
      {result.setup_hint && result.status === "NOT_RUN" && (
        <div className="text-micro leading-relaxed text-amber-300/80" data-testid="analysis-setup-hint">
          {result.setup_hint}
        </div>
      )}
      <SummaryDetails summary={result.summary} />
      {result.findings.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {result.findings.slice(0, 12).map((finding) => (
            <div key={finding.subject} className="flex items-baseline gap-2 text-micro" data-testid="analysis-finding">
              <span className={`shrink-0 ${SEVERITY_TONE[finding.severity] ?? "text-muted-foreground"}`}>{finding.critical ? "critical" : finding.severity}</span>
              <span className="min-w-0 flex-1 truncate text-neutral-300" title={finding.summary}>
                {finding.title}
              </span>
            </div>
          ))}
          {result.findings.length > 12 && <div className="text-micro text-muted-foreground">+{result.findings.length - 12} more in Behavior changes</div>}
        </div>
      )}
      {result.artifacts.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {result.artifacts.map((artifact) => (
            <UiButton key={artifact.name} type="button" size="xs" variant="ghost" onClick={() => onOpen(result, artifact.name)}>
              {artifact.name}
            </UiButton>
          ))}
        </div>
      )}
      {opened && opened.key.startsWith(`${result.evidence_id}:`) && (
        <div className="mt-1" data-testid="analysis-artifact">
          {opened.error ? (
            <div className="text-micro text-rose-300/80">{opened.error}</div>
          ) : !opened.view ? (
            <div className="text-micro text-muted-foreground">Loading…</div>
          ) : opened.view.expired ? (
            <div className="text-micro text-muted-foreground">This raw artifact expired; re-run the analysis to regenerate it.</div>
          ) : (
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words border border-border bg-muted/20 p-2 font-mono text-micro leading-4 text-neutral-300">
              {opened.view.text}
              {opened.view.truncated ? "\n…" : ""}
            </pre>
          )}
        </div>
      )}
      {result.reproduce.evidence_ids.length > 0 && (
        <div className="mt-1 truncate font-mono text-micro text-muted-foreground" title="Reproduce locally" data-testid="analysis-reproduce">
          {result.reproduce.command.replace("<evidence-id|record.json>", result.reproduce.evidence_ids[result.reproduce.evidence_ids.length - 1])}
        </div>
      )}
    </div>
  );
}

/**
 * Artifact and infrastructure analyses (migrations, regenerated files, shipped
 * artifacts, infrastructure renders) in the Checks rail. Renders nothing when
 * the trusted base gives no analysis anything to act on.
 */
export default function ArtifactAnalysesPanel({
  reviewId,
  revisionId = "",
  readOnly = false,
}: {
  reviewId: string;
  revisionId?: string;
  readOnly?: boolean;
}) {
  const [data, setData] = useState<ArtifactAnalysesProjection | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [opened, setOpened] = useState<{ key: string; view: AnalysisArtifactView | null; error: string } | null>(null);

  const load = useCallback(
    () =>
      fetchArtifactAnalyses(reviewId, revisionId).then(setData, (reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "analyses unavailable"),
      ),
    [reviewId, revisionId],
  );

  useEffect(() => {
    let cancelled = false;
    setError("");
    fetchArtifactAnalyses(reviewId, revisionId)
      .then((projection) => {
        if (!cancelled) setData(projection);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "analyses unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [reviewId, revisionId]);

  // Nothing to act on: the analyses neither run nor appear.
  if (error || !data || data.entries.length === 0) return null;

  const snapshot = data;
  const editable = !readOnly && !snapshot.historical;

  function run(entry: string, pick: boolean) {
    setBusy(true);
    setMessage("");
    postArtifactAnalysisRun(reviewId, snapshot.revision_id, entry, pick)
      .then(async (response) => {
        setMessage(`${entry}: ${VERDICT_LABEL[response.verdict] ?? response.verdict} (${response.executions} run, ${response.reused} reused)`);
        await load();
      })
      .catch((reason: unknown) => setMessage(reason instanceof Error ? reason.message : "request failed"))
      .finally(() => setBusy(false));
  }

  function open(result: AnalysisRecord, name: string) {
    const key = `${result.evidence_id}:${name}`;
    if (opened?.key === key) {
      setOpened(null);
      return;
    }
    setOpened({ key, view: null, error: "" });
    fetchAnalysisArtifact(reviewId, snapshot.revision_id, result.evidence_id, name).then(
      (view) => setOpened((current) => (current?.key === key ? { key, view, error: "" } : current)),
      (reason: unknown) =>
        setOpened((current) => (current?.key === key ? { key, view: null, error: reason instanceof Error ? reason.message : "unavailable" } : current)),
    );
  }

  return (
    <PanelCard title="Artifact analyses" action={<span className="font-mono text-caption text-muted-foreground">{snapshot.entries.length}</span>}>
      <div className="space-y-2" data-testid="artifact-analyses-panel">
        <div>
          {snapshot.entries.map((entry) => (
            <div key={entry.id} className="py-0.5 text-micro" data-testid={`analysis-entry-${entry.id}`}>
              <div className="flex items-baseline gap-2">
                <span className="min-w-0 flex-1 truncate text-neutral-300" title={entry.reason || entry.id}>
                  {entry.title}
                </span>
                <span className="text-muted-foreground">{entry.specs.map((spec) => spec.id).join(", ")}</span>
                <span className={entry.status === "limited_by_budget" ? "text-amber-300" : "text-muted-foreground"}>
                  {entry.status === "limited_by_budget" ? "Limited by budget" : entry.status.replaceAll("_", " ")}
                </span>
                {editable && <EntryAction entry={entry} busy={busy} onRun={run} />}
              </div>
              {entry.status === "not_available" && entry.setup_hint && <div className="text-amber-300/80">{entry.setup_hint}</div>}
            </div>
          ))}
        </div>
        {snapshot.errors.map((row) => (
          <div key={row} className="text-micro text-rose-300/80">
            {row}
          </div>
        ))}
        {snapshot.results.map((result) => (
          <ResultCard key={`${result.entry_id}:${result.spec.id}`} result={result} onOpen={open} opened={opened} />
        ))}
        {message && (
          <div className="text-micro text-neutral-300" role="status">
            {message}
          </div>
        )}
      </div>
    </PanelCard>
  );
}
