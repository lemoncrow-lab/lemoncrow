import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Archive, CheckCircle2, CircleDot, Copy, Search, Terminal } from "lucide-react";

import "./reviewUi.css";
import { PageFrame } from "../components/WorkbenchUI";
import {
  adoptBootstrapFragment,
  fetchReviewDirectory,
  reviewHref,
  type ReviewDirectoryQuery,
} from "./reviewApi";
import { reviewEnvironmentDescription, reviewEnvironmentLabel, useReviewEnvironment } from "./reviewEnvironment";
import type {
  ReviewDirectoryPage,
  ReviewDirectoryRow,
  ReviewDirectoryStatus,
} from "./types";

function statusGlyph(status: ReviewDirectoryRow["status"]) {
  if (status === "archived") return <Archive size={12} className="text-neutral-500" />;
  if (status === "finished") return <CheckCircle2 size={12} className="text-emerald-400" />;
  return <CircleDot size={12} className="text-sky-400" />;
}

function reviewRowAction(row: ReviewDirectoryRow): { label: string; tone: string } {
  if (row.status === "finished") return { label: "Finished", tone: "text-emerald-400" };
  if (row.status === "archived") return { label: "Archived", tone: "text-neutral-500" };
  const progress = row.progress;
  if (!progress || progress.target_count === 0) return { label: "Needs review", tone: "text-neutral-400" };
  if (progress.changed_since_review > 0) {
    return { label: `${progress.changed_since_review} changed`, tone: "text-sky-300" };
  }
  if (progress.unreviewed + progress.unknown > 0) {
    const pending = progress.unreviewed + progress.unknown;
    return { label: `${pending} left`, tone: "text-amber-300" };
  }
  if (progress.needs_changes > 0) {
    return { label: "Awaiting changes", tone: "text-rose-300" };
  }
  return { label: "Ready to finish", tone: "text-emerald-300" };
}

function ReviewRow({ row }: { row: ReviewDirectoryRow }) {
  const action = reviewRowAction(row);
  return (
    <a
      href={reviewHref(row.ref || row.id)}
      title={`${row.repo_id} · ${row.id}`}
      className="review-directory-grid group min-h-[50px] items-center border-t border-neutral-900/90 px-3 text-[11px] transition-colors hover:bg-neutral-900/55"
    >
      <div className="min-w-0 pr-4">
        <div className="flex min-w-0 items-center gap-2">
          {statusGlyph(row.status)}
          <span className="truncate font-medium text-neutral-200 group-hover:text-neutral-50">
            {row.title || "Untitled review"}
          </span>
        </div>
        <div className="mt-0.5 flex min-w-0 items-center gap-2 pl-5 text-[10px] text-neutral-600">
          <span className="truncate font-mono">{row.source_ref || row.id}</span>
          {row.progress && row.progress.target_count > 0 && (
            <>
              <span className="shrink-0">· {row.progress.reviewed}/{row.progress.target_count} reviewed</span>
              {row.progress.changed_since_review > 0 && (
                <span className="shrink-0 text-sky-400">· {row.progress.changed_since_review} changed</span>
              )}
              {row.progress.needs_changes > 0 && (
                <span className="shrink-0 text-rose-400">· {row.progress.needs_changes} need changes</span>
              )}
            </>
          )}
        </div>
      </div>
      <div className="review-grid-secondary min-w-0 truncate pr-3 font-mono text-[10px] text-neutral-500">
        {row.repo_id}
      </div>
      <div className={`text-[10px] font-medium ${action.tone}`}>
        {action.label}
      </div>
      <div className="review-grid-secondary text-right text-[10px] text-neutral-600">
        {row.updated_at
          ? new Date(row.updated_at).toLocaleString([], {
              month: "short",
              day: "numeric",
              hour: "2-digit",
              minute: "2-digit",
            })
          : ""}
      </div>
    </a>
  );
}

export default function ReviewDirectory() {
  const environment = useReviewEnvironment("local");
  const [status, setStatus] = useState<ReviewDirectoryStatus>("open");
  const [commandCopied, setCommandCopied] = useState(false);
  const [searchDraft, setSearchDraft] = useState("");
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<ReviewDirectoryRow[]>([]);
  const [nextCursor, setNextCursor] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const generationRef = useRef(0);

  useEffect(() => {
    adoptBootstrapFragment();
  }, []);

  const loadPage = useCallback(
    async (cursor: string, append: boolean, generation: number) => {
      if (append) setLoadingMore(true);
      else setLoading(true);
      const request: ReviewDirectoryQuery = { status, query, cursor, limit: 50 };
      try {
        const page: ReviewDirectoryPage = await fetchReviewDirectory(request);
        if (generationRef.current !== generation) return;
        setNextCursor(page.next_cursor);
        setError("");
        setRows((current) =>
          append
            ? [
                ...current,
                ...page.reviews.filter(
                  (row) =>
                    !current.some(
                      (item) => item.repo_id === row.repo_id && item.id === row.id,
                    ),
                ),
              ]
            : page.reviews,
        );
      } catch (reason) {
        if (generationRef.current !== generation) return;
        setError(String(reason instanceof Error ? reason.message : reason));
        if (!append) setRows([]);
      } finally {
        if (generationRef.current === generation) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [query, status],
  );

  useEffect(() => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    setRows([]);
    setNextCursor("");
    void loadPage("", false, generation);
  }, [loadPage]);

  const search = (event: FormEvent) => {
    event.preventDefault();
    setQuery(searchDraft.trim());
  };

  return (
    <div className="min-h-full bg-neutral-950 text-neutral-200" data-testid="review-directory">
      <PageFrame className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <form
            onSubmit={search}
            className="review-field flex h-8 min-w-[260px] max-w-[720px] flex-1 items-center p-0"
          >
            <Search size={13} className="ml-3 text-neutral-600" />
            <input
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              placeholder="Search reviews…"
              aria-label="Search reviews"
              className="min-w-0 flex-1 border-0 bg-transparent px-2.5 py-2 text-[11px] text-neutral-200 outline-none placeholder:text-neutral-700"
            />
            <button
              type="submit"
              className="h-full border-l border-neutral-800 px-4 text-[10px] font-semibold text-neutral-400 hover:bg-neutral-900 hover:text-neutral-50"
            >
              Search
            </button>
          </form>
          <select
            aria-label="Review status"
            value={status}
            onChange={(event) => setStatus(event.target.value as ReviewDirectoryStatus)}
            className="review-field h-8 py-0 text-[10px]"
          >
            <option value="open">Needs review</option>
            <option value="all">All reviews</option>
            <option value="finished">Finished</option>
            <option value="archived">Archived</option>
          </select>
          <span className="review-pill shrink-0" title={reviewEnvironmentDescription(environment)}>{reviewEnvironmentLabel(environment.mode)}</span>
          <span className="review-pill shrink-0">{rows.length} reviews</span>
        </div>

        {loading && rows.length === 0 && (
          <div
            className="border-y border-neutral-900 py-6 text-center text-[10px] text-neutral-600"
            role="status"
          >
            Loading reviews…
          </div>
        )}
        {error && (
          <div className="my-4 border-y border-rose-900/45 bg-rose-950/10 px-3 py-2.5 text-[10px] text-rose-300">
            Could not load reviews · {error}
          </div>
        )}
        {!loading && !error && rows.length === 0 && (
          status === "open" && !query ? (
            <div className="flex flex-col items-center py-8 text-center">
              <Terminal size={19} className="text-neutral-700" />
              <div className="mt-3 text-[12px] font-medium text-neutral-300">No local reviews yet</div>
              <div className="mt-1 max-w-lg text-[10px] leading-5 text-neutral-600">
                Review staged, unstaged, committed, or branch changes directly from your workspace. No PR or hosted account is required.
              </div>
              <button
                type="button"
                className="review-toolbar-button mt-3 gap-2 font-mono"
                onClick={() => {
                  const write = navigator.clipboard?.writeText;
                  if (!write) return;
                  void Promise.resolve(write.call(navigator.clipboard, "lc review")).then(() => {
                    setCommandCopied(true);
                    window.setTimeout(() => setCommandCopied(false), 1200);
                  });
                }}
                title="Copy lc review"
              >
                <span>$ lc review</span>
                <Copy size={11} />
                {commandCopied && <span className="font-sans text-emerald-400">Copied</span>}
              </button>
            </div>
          ) : (
            <div className="py-8 text-center text-[11px] text-neutral-600">No reviews match this view.</div>
          )
        )}
        {rows.length > 0 && (
          <section className="overflow-hidden rounded-md border border-neutral-800/80 bg-neutral-950/75">
            <div className="review-directory-grid h-7 items-center px-3 text-[10px] font-medium uppercase tracking-[0.06em] text-neutral-700">
              <span>Change</span>
              <span className="review-grid-secondary">Repository</span>
              <span>Action</span>
              <span className="review-grid-secondary text-right">Updated</span>
            </div>
            {rows.map((row) => (
              <ReviewRow key={`${row.repo_id}:${row.id}`} row={row} />
            ))}
          </section>
        )}
        {nextCursor && (
          <div className="mt-4 flex justify-center">
            <button
              type="button"
              disabled={loadingMore}
              onClick={() => void loadPage(nextCursor, true, generationRef.current)}
              className="review-toolbar-button"
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          </div>
        )}
      </PageFrame>
    </div>
  );
}
