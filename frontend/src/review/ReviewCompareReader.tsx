import { ArrowLeft, ArrowRight, GitCompareArrows } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import "./reviewUi.css";
import type { DiffStyle } from "./diffModel";
import ReviewStream from "./ReviewStream";
import { fetchSourceComparison, reviewDirectoryHref, reviewHref, sourceCompareHref } from "./reviewApi";
import { useReviewDropdownDismissal } from "./useReviewDropdownDismissal";
import type { FileDetail, ReviewTarget, SourceComparison } from "./types";

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

export default function ReviewCompareReader() {
  useReviewDropdownDismissal();
  const params = useParams<{ "*": string; reviewRef?: string }>();
  const wildcard = params["*"] ?? "";
  const scopedReviewRef = params.reviewRef ?? "";
  const rawRoutePair = useMemo(() => decodeCompareSpec(wildcard), [wildcard]);
  const routePair = useMemo(() => scopedReviewRef
    ? { from: `rr/${rawRoutePair.from}`, to: `rr/${rawRoutePair.to}` }
    : rawRoutePair, [rawRoutePair, scopedReviewRef]);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const scope = scopedReviewRef ? `r/${scopedReviewRef}` : (searchParams.get("scope") ?? "");
  const [fromInput, setFromInput] = useState(routePair.from);
  const [toInput, setToInput] = useState(routePair.to);
  const [comparison, setComparison] = useState<SourceComparison | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [diffStyle, setDiffStyle] = useState<DiffStyle>("split");
  const [activeTargetId, setActiveTargetId] = useState("");

  useEffect(() => {
    setFromInput(routePair.from);
    setToInput(routePair.to);
    if (!routePair.from || !routePair.to) {
      setComparison(null);
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
        document.title = `Compare: ${result.from.label} → ${result.to.label}`;
      })
      .catch((reason: unknown) => {
        if (!live) return;
        setComparison(null);
        setError(String(reason instanceof Error ? reason.message : reason));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => { live = false; };
  }, [routePair.from, routePair.to, scope]);

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

  const goBack = () => {
    const historyIndex = window.history.state?.idx;
    if (typeof historyIndex !== "number" || historyIndex > 0) {
      navigate(-1);
      return;
    }
    navigate(scopedReviewRef ? reviewHref(scopedReviewRef) : reviewDirectoryHref());
  };

  return (
    <div className="flex h-screen min-h-0 flex-col bg-neutral-950 text-neutral-200">
      <header className="shrink-0 border-b border-neutral-800 bg-neutral-950/95 px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <button type="button" onClick={goBack} className="review-icon-button h-8 w-8" aria-label="Back" title="Back">
            <ArrowLeft size={14} aria-hidden="true" />
          </button>
          <GitCompareArrows size={15} className="shrink-0 text-sky-300" aria-hidden="true" />
          <span className="shrink-0 text-[11px] font-semibold text-neutral-200">Compare</span>
          <input
            aria-label="Compare from source"
            value={fromInput}
            onChange={(event) => setFromInput(event.currentTarget.value)}
            onKeyDown={(event) => { if (event.key === "Enter") runCompare(); }}
            className="review-field h-8 min-w-[180px] flex-1 font-mono text-[10px]"
            placeholder="git/HEAD~1 or rr/<revision-id>"
            spellCheck={false}
          />
          <ArrowRight size={13} className="shrink-0 text-neutral-600" aria-hidden="true" />
          <input
            aria-label="Compare to source"
            value={toInput}
            onChange={(event) => setToInput(event.currentTarget.value)}
            onKeyDown={(event) => { if (event.key === "Enter") runCompare(); }}
            className="review-field h-8 min-w-[180px] flex-1 font-mono text-[10px]"
            placeholder="git/HEAD, worktree, index, rr/<revision-id>"
            spellCheck={false}
          />
          <button type="button" onClick={runCompare} className="review-toolbar-button-primary h-8">Compare</button>
          <div className="review-segment ml-1" role="group" aria-label="Diff layout">
            {(["split", "unified"] as const).map((style) => (
              <button
                key={style}
                type="button"
                onClick={() => setDiffStyle(style)}
                className={`review-segment-button capitalize ${diffStyle === style ? "review-segment-button-active" : ""}`}
              >
                {style}
              </button>
            ))}
          </div>
        </div>
        {comparison && (
          <div className="mt-1.5 flex items-center gap-2 pl-10 text-[10px] text-neutral-600">
            <span className="font-mono text-neutral-400">{comparison.from.label}</span>
            <ArrowRight size={10} aria-hidden="true" />
            <span className="font-mono text-neutral-300">{comparison.to.label}</span>
            <span className="ml-auto">{comparison.summary.files} files · <span className="text-emerald-400">+{comparison.summary.additions}</span> <span className="text-rose-400">-{comparison.summary.deletions}</span></span>
          </div>
        )}
      </header>

      {loading && <div className="px-4 py-3 text-[11px] text-neutral-500" role="status">Comparing sources…</div>}
      {error && (
        <div className="border-b border-rose-900/50 bg-rose-950/15 px-4 py-3 text-[11px] text-rose-300" role="alert">
          {error}
        </div>
      )}
      {!loading && !error && comparison && comparison.files.length === 0 && (
        <div className="px-4 py-10 text-center text-[12px] text-neutral-500">No source differences between these snapshots.</div>
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
