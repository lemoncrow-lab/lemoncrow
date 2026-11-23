import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Select, SelectItem } from "../components/ui/select";
import { ToggleGroup } from "../components/ui/toggle-group";
import { ArrowLeft, ArrowRight, GitCompareArrows } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import "./reviewUi.css";
import type { DiffStyle } from "./diffModel";
import ReviewStream from "./ReviewStream";
import { fetchRevisions, fetchSourceComparison, reviewDirectoryHref, reviewHref, sourceCompareHref } from "./reviewApi";
import { revisionLabel } from "./revisionLabel";
import type { FileDetail, ReviewTarget, RevisionInfo, SourceComparison } from "./types";

function decodeCompareSpec(raw: string): { from: string; to: string } {
  const marker = raw.indexOf("..");
  if (marker <= 0 || marker >= raw.length - 2) return { from: "", to: "" };
  try {
    return {
      from: decodeURIComponent(raw.slice(0, marker)),
      to: decodeURIComponent(raw.slice(marker + 2)),
    };
  } catch {
    return { from: raw.slice(0, marker), to: raw.slice(marker + 2) };
  }
}

function fileTarget(path: string, additions: number, deletions: number): ReviewTarget {
  return {
    target_id: `compare:${path}`,
    unit_key: `compare:${path}`,
    kind: "file",
    path,
    label: path,
    symbol: "",
    start_line: 0,
    end_line: 0,
    hunk_ordinals: [],
    spans: [],
    state: "unknown",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: [],
    additions,
    deletions,
    fingerprint_method: "compare",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

/** The revision a review-scoped source names: its short ref, its id, or the one id that starts with the short id. */
export function revisionForSpec(revisions: readonly RevisionInfo[], spec: string): RevisionInfo | undefined {
  if (!spec.startsWith("rr/")) return undefined;
  const token = spec.slice(3);
  const exact = revisions.find((revision) => revision.ref === spec || revision.id === token);
  if (exact || !token) return exact;
  const prefixed = revisions.filter((revision) => revision.id.startsWith(token));
  return prefixed.length === 1 ? prefixed[0] : undefined;
}

const specOf = (revision: RevisionInfo): string => (revision.ref?.startsWith("rr/") ? revision.ref : `rr/${revision.id}`);

/** The server's reason in words a reviewer can act on (review-22.2): a missing side says so plainly; anything else keeps
 * the server's own text. */
export function compareErrorText(message: string): string {
  // A bare status code (non-JSON error body) tells the reviewer nothing.
  if (/^\d{3}$/.test(message)) return `The server could not compare these sources (HTTP ${message}).`;
  if (/\bnot available\b|\bno such\b|\bnot found\b/i.test(message)) {
    return "One of these two versions could not be found, so there is nothing to compare. Check both versions above, or go back to the review.";
  }
  return message;
}

export default function ReviewCompareReader() {
  const params = useParams<{ "*": string; reviewRef?: string }>();
  const wildcard = params["*"] ?? "";
  const scopedReviewRef = params.reviewRef ?? "";
  const [searchParams] = useSearchParams();
  const rawRoutePair = useMemo(() => decodeCompareSpec(wildcard), [wildcard]);
  const queryPair = useMemo(() => {
    const hasQueryPair = searchParams.has("from") || searchParams.has("to");
    return hasQueryPair
      ? { from: searchParams.get("from") ?? "", to: searchParams.get("to") ?? "" }
      : null;
  }, [searchParams]);
  const routePair = useMemo(() => scopedReviewRef
    ? { from: `rr/${rawRoutePair.from}`, to: `rr/${rawRoutePair.to}` }
    : queryPair ?? rawRoutePair, [queryPair, rawRoutePair, scopedReviewRef]);
  const navigate = useNavigate();
  const scope = scopedReviewRef ? `r/${scopedReviewRef}` : (searchParams.get("scope") ?? "");
  const [fromInput, setFromInput] = useState(routePair.from);
  const [toInput, setToInput] = useState(routePair.to);
  const [comparison, setComparison] = useState<SourceComparison | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [diffStyle, setDiffStyle] = useState<DiffStyle>("split");
  const [activeTargetId, setActiveTargetId] = useState("");
  const [attempt, setAttempt] = useState(0);
  // A review-scoped comparison picks among the review's own revisions by name, never by raw id (review-22.2). Only when
  // the review's revisions cannot be read does it fall back to the source fields.
  const [revisions, setRevisions] = useState<RevisionInfo[] | null>(null);
  const [revisionsFailed, setRevisionsFailed] = useState(false);
  const picking = Boolean(scopedReviewRef) && !revisionsFailed;

  useEffect(() => {
    setRevisions(null);
    setRevisionsFailed(false);
    if (!scopedReviewRef) return;
    let live = true;
    fetchRevisions(scopedReviewRef)
      .then((history) => {
        if (!live) return;
        const list = [...(history.revisions ?? [])].sort((a, b) => a.revision_number - b.revision_number);
        if (list.length) setRevisions(list); else setRevisionsFailed(true);
      })
      .catch(() => { if (live) setRevisionsFailed(true); });
    return () => { live = false; };
  }, [scopedReviewRef]);
  const fromRevision = revisions ? revisionForSpec(revisions, routePair.from) : undefined;
  const toRevision = revisions ? revisionForSpec(revisions, routePair.to) : undefined;
  const fromName = fromRevision ? revisionLabel(fromRevision) : comparison?.from.label ?? "";
  const toName = toRevision ? revisionLabel(toRevision) : comparison?.to.label ?? "";
  useEffect(() => {
    if (comparison) document.title = `Compare: ${fromName} → ${toName}`;
  }, [comparison, fromName, toName]);

  useEffect(() => {
    setFromInput(routePair.from);
    setToInput(routePair.to);
    if (!routePair.from || !routePair.to) {
      setComparison(null);
      // An in-flight comparison for the previous URL no longer clears this.
      setLoading(false);
      setError("Comparison URL must contain two source specs separated by '..'.");
      return;
    }
    let live = true;
    setLoading(true);
    setError("");
    void fetchSourceComparison(routePair.from, routePair.to, scope)
      .then((result) => {
        if (!live) return;
        setComparison(result);
        setActiveTargetId(result.files[0] ? `compare:${result.files[0].path}` : "");
      })
      .catch((reason: unknown) => {
        if (!live) return;
        setComparison(null);
        setError(compareErrorText(String(reason instanceof Error ? reason.message : reason)));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => { live = false; };
  }, [routePair.from, routePair.to, scope, attempt]);

  const targets = useMemo(
    () => (comparison?.files ?? []).map((file) => fileTarget(file.path, file.additions, file.deletions)),
    [comparison],
  );
  const details = useMemo(() => {
    const rows: Record<string, FileDetail> = {};
    for (const file of comparison?.files ?? []) {
      rows[file.path] = {
        path: file.path,
        old_path: file.old_path,
        status: file.status,
        additions: file.additions,
        deletions: file.deletions,
        patch: file.patch,
        renderable: file.renderable,
        refusal: file.refusal,
        detail: file.detail,
        degraded: [],
      };
    }
    return rows;
  }, [comparison]);

  const runCompare = () => {
    const from = fromInput.trim();
    const to = toInput.trim();
    if (!from || !to) return;
    navigate(sourceCompareHref(from, to, scope));
  };
  const pickRevision = (side: "from" | "to", spec: string) => {
    const from = side === "from" ? spec : fromRevision ? specOf(fromRevision) : "";
    const to = side === "to" ? spec : toRevision ? specOf(toRevision) : "";
    if (side === "from") setFromInput(spec); else setToInput(spec);
    if (from && to) navigate(sourceCompareHref(from, to, scope));
  };
  const revisionPicker = (side: "from" | "to", picked: RevisionInfo | undefined) => (
    <Select
      uiSize="xs"
      aria-label={side === "from" ? "Compare from revision" : "Compare to revision"}
      value={picked ? specOf(picked) : ""}
      placeholder={revisions ? "Choose a revision" : "Loading revisions…"}
      disabled={!revisions}
      onValueChange={(spec) => pickRevision(side, spec)}
      className="min-w-[160px] flex-1 sm:flex-none"
    >
      {(revisions ?? []).map((revision) => <SelectItem key={revision.id} value={specOf(revision)}>{revisionLabel(revision)}</SelectItem>)}
    </Select>
  );

  const goBack = () => {
    const historyIndex = window.history.state?.idx;
    if (typeof historyIndex !== "number" || historyIndex > 0) {
      navigate(-1);
      return;
    }
    navigate(scopedReviewRef ? reviewHref(scopedReviewRef) : reviewDirectoryHref());
  };

  return (
    <div className="flex h-screen min-h-0 flex-col bg-background text-foreground">
      <header className="shrink-0 border-b border-border bg-background/95 px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <UiButton variant="ghost" size="icon" type="button" onClick={goBack} className="h-8 w-8" aria-label="Back" tooltip="Back">
            <ArrowLeft size={14} aria-hidden="true" />
          </UiButton>
          <GitCompareArrows size={16} className="shrink-0 text-cyan-300" aria-hidden="true" />
          <span className="shrink-0 text-label font-semibold text-foreground">Compare</span>
          {picking ? <>
            {revisionPicker("from", fromRevision)}
            <ArrowRight size={14} className="shrink-0 text-muted-foreground" aria-hidden="true" />
            {revisionPicker("to", toRevision)}
          </> : <>
            <UiInput
              uiSize="xs"
              aria-label="Compare from source"
              value={fromInput}
              onChange={(event) => setFromInput(event.currentTarget.value)}
              onKeyDown={(event) => { if (event.key === "Enter") runCompare(); }}
              className="min-w-[180px] flex-1 font-mono text-meta"
              placeholder="git/HEAD~1 or rr/<revision-id>"
              spellCheck={false}
            />
            <ArrowRight size={14} className="shrink-0 text-muted-foreground" aria-hidden="true" />
            <UiInput
              uiSize="xs"
              aria-label="Compare to source"
              value={toInput}
              onChange={(event) => setToInput(event.currentTarget.value)}
              onKeyDown={(event) => { if (event.key === "Enter") runCompare(); }}
              className="min-w-[180px] flex-1 font-mono text-meta"
              placeholder="git/HEAD, worktree, index, rr/<revision-id>"
              spellCheck={false}
            />
            <UiButton variant="solid" type="button" onClick={runCompare} className="h-8 px-3">Compare</UiButton>
          </>}
          <ToggleGroup
            variant="segment"
            aria-label="Diff layout"
            className="ml-1"
            value={diffStyle}
            onChange={(next) => setDiffStyle(next as DiffStyle)}
            options={[
              { value: "split", label: "Split" },
              { value: "unified", label: "Unified" },
            ]}
          />
        </div>
        {comparison && (
          <div className="mt-1.5 flex items-center gap-2 pl-10 text-caption text-muted-foreground">
            {/* With the pickers above, the names are not said twice; a source field gets the server's labels. */}
            {!picking && <>
              <span className="font-mono text-muted-foreground">{comparison.from.label}</span>
              <ArrowRight size={14} aria-hidden="true" />
              <span className="font-mono text-subtle">{comparison.to.label}</span>
            </>}
            <span className="ml-auto">{comparison.summary.files} files · <span className="text-emerald-400">+{comparison.summary.additions}</span> <span className="text-red-400">-{comparison.summary.deletions}</span></span>
          </div>
        )}
      </header>

      {loading && <div className="px-4 py-3 text-label text-muted-foreground" role="status">Comparing sources…</div>}
      {error && (
        <div className="flex flex-wrap items-center gap-2 border-b border-red-900/50 bg-red-950/15 px-4 py-3 text-label text-red-300" role="alert">
          <span className="min-w-0 flex-1 break-words">{error}</span>
          {routePair.from && routePair.to && (
            <UiButton type="button" onClick={() => setAttempt((value) => value + 1)} className="h-7">Retry</UiButton>
          )}
          {scopedReviewRef && (
            <UiButton type="button" onClick={() => navigate(reviewHref(scopedReviewRef))} className="h-7">Back to review</UiButton>
          )}
        </div>
      )}
      {!loading && !error && comparison && comparison.files.length === 0 && (
        <div className="px-4 py-10 text-center text-body text-muted-foreground">No source differences between these revisions.</div>
      )}
      {!loading && !error && comparison && comparison.files.length > 0 && (
        <div className="min-h-0 flex-1">
          <ReviewStream
            targets={targets}
            details={details}
            errors={{}}
            annotations={[]}
            draft={null}
            comparisonMode
            diffStyle={diffStyle}
            activeTargetId={activeTargetId}
            busy={false}
            remainingFileCount={0}
            outstandingByPath={new Map()}
            onActiveTarget={setActiveTargetId}
            onDraft={() => {}}
            onCreate={() => {}}
            onSetAnnotationState={() => {}}
            onMark={() => {}}
            onComment={() => {}}
            onContext={() => {}}
            onBulkReview={() => {}}
            onLoadMore={() => {}}
          />
        </div>
      )}
    </div>
  );
}
