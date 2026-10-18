import { Button as UiButton } from "../components/ui/button";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog";
import { useEffect, useMemo, useState } from "react";
import { Hexagon } from "lucide-react";
import {
  ApiError,
  api,
  type MemoryBlock,
  type MemoryFact,
  type MemoryPassage,
  type MemoryRecallPassage,
  type TraceSummary,
} from "../api";
import MemoryBlockCard from "../components/MemoryBlockCard";
import ArchivalSearchBox from "../components/ArchivalSearchBox";
import {
  Alert,
  Card,
  EmptyState,
  MetricCard,
  SectionHeader,
  ToggleGroup,
  cx,
} from "../components/WorkbenchUI";

interface EditDraft {
  block: MemoryBlock;
  nextValue: string;
}

const DEFAULT_MEMORY_AGENTS = ["lc"];

function hostTag(agentId: string): string {
  const raw = agentId.trim().toLowerCase();
  if (!raw) return "unknown";
  if (raw.includes("copilot")) return "copilot";
  if (raw.includes("codex")) return "codex";
  if (raw.includes("gemini")) return "gemini";
  if (raw.includes("lemoncode")) return "lemoncode";
  if (raw.includes("opencode")) return "opencode";
  if (raw.startsWith("lc") || raw.includes("claude")) return "claude";
  return raw;
}

function safeHref(ref: string): string | null {
  try {
    const url = new URL(ref, window.location.origin);
    if (url.protocol === "http:" || url.protocol === "https:") {
      return url.href;
    }
  } catch {
    return null;
  }
  return null;
}

function dedupeById<T extends { id: string }>(items: T[]): T[] {
  const seen = new Map<string, T>();
  for (const item of items) {
    if (!seen.has(item.id)) {
      seen.set(item.id, item);
    }
  }
  return [...seen.values()];
}

export default function Memory() {
  const [tab, setTab] = useState<"cross-vendor" | "knowledge">("cross-vendor");
  const [facts, setFacts] = useState<MemoryFact[] | null>(null);
  const [factsErr, setFactsErr] = useState<string | null>(null);
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [blocks, setBlocks] = useState<MemoryBlock[]>([]);
  const [recentPassages, setRecentPassages] = useState<MemoryPassage[]>([]);
  const [recallResults, setRecallResults] = useState<MemoryRecallPassage[]>([]);
  const [loadingBlocks, setLoadingBlocks] = useState(false);
  const [loadingPassages, setLoadingPassages] = useState(false);
  const [loadingRecall, setLoadingRecall] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null);
  const [conflictMessage, setConflictMessage] = useState<string | null>(null);

  // Load cross-vendor facts
  useEffect(() => {
    api
      .memoryFacts()
      .then(setFacts)
      .catch((e) => setFactsErr(String(e)));
  }, []);

  // Group facts by vendor
  const factsByVendor = useMemo(() => {
    if (!facts) return {};
    const groups: Record<string, MemoryFact[]> = {};
    for (const f of facts) {
      if (!groups[f.vendor]) groups[f.vendor] = [];
      groups[f.vendor].push(f);
    }
    return groups;
  }, [facts]);

  const VENDOR_COLORS: Record<string, string> = {
    anthropic: "text-orange-300",
    claude: "text-orange-300",
    openai: "text-green-300",
    codex: "text-green-300",
    google: "text-blue-300",
    gemini: "text-blue-300",
    copilot: "text-brand-400",
    opencode: "text-cyan-300",
    lemoncode: "text-yellow-300",
  };

  useEffect(() => {
    api
      .traces(200, 0)
      .then((data) => {
        setTraces(data.items);
      })
      .catch((err) => setError(String(err)));
  }, []);

  const visibleAgentIds = useMemo(() => {
    const all = [
      ...traces.map((trace) => trace.agent),
      ...DEFAULT_MEMORY_AGENTS,
    ]
      .map((value) => value.trim())
      .filter(Boolean);
    return [...new Set(all)].sort((left, right) => left.localeCompare(right));
  }, [traces]);

  const visibleHosts = useMemo(
    () => [...new Set(visibleAgentIds.map((agentId) => hostTag(agentId)))],
    [visibleAgentIds]
  );

  useEffect(() => {
    if (visibleAgentIds.length === 0) return;

    setLoadingBlocks(true);
    setConflictMessage(null);
    Promise.allSettled(
      visibleAgentIds.map(async (agentId) => {
        const result = await api.memoryBlocks(agentId);
        const items = Array.isArray(result) ? result : result ? [result] : [];
        return items;
      })
    )
      .then((results) => {
        const merged = results
          .flatMap((result) =>
            result.status === "fulfilled" ? result.value : []
          )
          .sort(
            (left, right) =>
              Date.parse(right.updated_at) - Date.parse(left.updated_at)
          );
        setBlocks(dedupeById(merged));
      })
      .catch((err) => {
        setError(String(err));
        setBlocks([]);
      })
      .finally(() => {
        setLoadingBlocks(false);
      });
  }, [visibleAgentIds]);

  useEffect(() => {
    if (visibleAgentIds.length === 0) return;

    setLoadingPassages(true);
    Promise.allSettled(
      visibleAgentIds.map((agentId) => api.memoryPassages(agentId, 25))
    )
      .then((results) => {
        const merged = results
          .flatMap((result) =>
            result.status === "fulfilled" ? result.value : []
          )
          .sort(
            (left, right) =>
              Date.parse(right.created_at) - Date.parse(left.created_at)
          );
        setRecentPassages(dedupeById(merged).slice(0, 24));
      })
      .catch((err) => {
        setError(String(err));
        setRecentPassages([]);
      })
      .finally(() => {
        setLoadingPassages(false);
      });
  }, [visibleAgentIds]);

  const pinnedBlocks = useMemo(
    () => blocks.filter((block) => block.pinned),
    [blocks]
  );

  const recentBlocks = useMemo(
    () =>
      [...blocks]
        .filter((block) => !block.pinned)
        .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
        .slice(0, 10),
    [blocks]
  );

  const runRecallSearch = (query: string) => {
    if (!query || visibleAgentIds.length === 0) {
      setRecallResults([]);
      return;
    }

    setLoadingRecall(true);
    Promise.allSettled(
      visibleAgentIds.map((agentId) =>
        api.memoryRecall({
          agent_id: agentId,
          query,
          top_k: 10,
        })
      )
    )
      .then((results) => {
        const merged = results.flatMap((result) =>
          result.status === "fulfilled" ? result.value.passages : []
        );
        const deduped = dedupeById(merged).sort((left, right) => {
          const rightDate = Date.parse(right.created_at ?? "") || 0;
          const leftDate = Date.parse(left.created_at ?? "") || 0;
          return rightDate - leftDate;
        });
        setRecallResults(deduped.slice(0, 20));
      })
      .catch((err) => {
        setError(String(err));
        setRecallResults([]);
      })
      .finally(() => {
        setLoadingRecall(false);
      });
  };

  const openEdit = (block: MemoryBlock) => {
    setConflictMessage(null);
    setEditDraft({ block, nextValue: block.value });
  };

  const submitEdit = async () => {
    if (!editDraft) return;

    try {
      const result = await api.memoryUpsertBlock({
        agent_id: editDraft.block.agent_id,
        label: editDraft.block.label,
        value: editDraft.nextValue,
        expected_version: editDraft.block.version,
        pinned: editDraft.block.pinned,
        description: editDraft.block.description,
        read_only: editDraft.block.read_only,
        limit_chars: editDraft.block.limit_chars,
      });

      setBlocks((prev) =>
        prev.map((block) =>
          block.id === editDraft.block.id
            ? {
                ...block,
                value: editDraft.nextValue,
                version: result.version,
                updated_at: new Date().toISOString(),
              }
            : block
        )
      );
      setEditDraft(null);
      setConflictMessage(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setConflictMessage(
          "Version conflict detected (409). Refresh memory blocks and retry your edit."
        );
        return;
      }
      setError(String(err));
    }
  };

  return (
    <div className="space-y-4">
      <ToggleGroup
        aria-label="Memory view"
        variant="underline"
        size="sm"
        options={[
          { value: "cross-vendor", label: "Cross-vendor" },
          { value: "knowledge", label: "Lessons" },
        ]}
        value={tab}
        onChange={(value) =>
          setTab(value as "cross-vendor" | "knowledge")
        }
      />

      {/* Cross-vendor tab */}
      {tab === "cross-vendor" && (
        <div className="space-y-4">
          {factsErr && <Alert tone="destructive" description={factsErr} />}
          {facts === null && !factsErr && (
            <EmptyState title="Loading cross-vendor memory…" className="p-6" />
          )}
          {facts !== null && facts.length === 0 && (
            <EmptyState
              icon={<Hexagon size={32} />}
              title="No cross-vendor facts yet"
              description="Facts are shared across vendors after they are written to the memory registry."
            />
          )}
          {Object.entries(factsByVendor).map(([vendor, vfacts]) => (
            <Card key={vendor}>
              <div
                className={cx(
                  "border-b border-border px-4 py-2 text-xs font-bold uppercase tracking-widest",
                  VENDOR_COLORS[vendor] ?? "text-neutral-400"
                )}
              >
                {vendor}
              </div>
              <ul className="divide-y divide-neutral-800">
                {vfacts.map((f) => (
                  <li key={f.fact_id} className="px-4 py-3 text-xs">
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <p className="font-semibold text-neutral-200">{f.source_kind}</p>
                        <p className="mt-0.5 text-neutral-400">{f.content}</p>
                        {f.source_path && (
                          <p className="mt-0.5 font-mono text-neutral-400">{f.source_path}{f.line_number != null ? `:${f.line_number}` : ""}</p>
                        )}
                      </div>
                      <span className="shrink-0 text-neutral-400">{f.fact_id.slice(0, 8)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          ))}
        </div>
      )}

      {/* Knowledge Blocks tab — existing content */}
      {tab === "knowledge" && (
        <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3">
        <MetricCard
          label="Visible agents"
          value={String(visibleAgentIds.length)}
          detail="Aggregated across trace history plus shared archival agents."
          tone="success"
        />
        <MetricCard
          label="Hosts"
          value={String(visibleHosts.length)}
          detail="Host tags derived from the merged agent set."
          tone="warning"
        />
      </section>

      {error && <p className="text-xs text-red-300">{error}</p>}

      <section className="grid gap-4 md:grid-cols-3">
        <MetricCard
          label="Recent blocks"
          value={String(recentBlocks.length)}
          detail="Most recently updated core memory entries across all visible agents."
          tone="neutral"
        />
        <MetricCard
          label="Recall hits"
          value={String(recallResults.length)}
          detail="Merged archival search result count across visible agents."
          tone="info"
        />
        <MetricCard
          label="Archived passages"
          value={String(recentPassages.length)}
          detail="Recent long-term passages currently visible in the merged view."
          tone="neutral"
        />
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <section className="rounded-lg border border-border/70 bg-background/60 p-4">
          <div className="pb-3 border-b border-border">
            <SectionHeader
              eyebrow="Static memory"
              title="Core blocks"
              description="These pinned and recent blocks are merged across the visible agents instead of hidden behind a single-agent selector."
            />
            <div className="mt-3 flex flex-wrap gap-2 text-caption font-mono uppercase tracking-widest">
              {visibleAgentIds.map((agentId) => (
                <span
                  key={agentId}
                  className="px-2 py-1 border border-border text-neutral-400"
                >
                  {hostTag(agentId)} · {agentId}
                </span>
              ))}
            </div>
          </div>

          {loadingBlocks && (
            <p className="text-xs text-neutral-400 pt-3">
              Loading memory blocks...
            </p>
          )}

          {!loadingBlocks && (
            <div className="pt-3 space-y-4">
              <div>
                <h3 className="text-label uppercase tracking-widest text-neutral-400">
                  Pinned
                </h3>
                {pinnedBlocks.length === 0 ? (
                  <p className="text-xs text-neutral-400 mt-2">
                    No pinned blocks have been saved for the visible agents yet.
                  </p>
                ) : (
                  pinnedBlocks.map((block) => (
                    <MemoryBlockCard
                      key={block.id}
                      block={block}
                      onEdit={openEdit}
                      badges={[hostTag(block.agent_id), block.agent_id]}
                    />
                  ))
                )}
              </div>

              <div>
                <h3 className="text-label uppercase tracking-widest text-neutral-400">
                  Recent
                </h3>
                {recentBlocks.length === 0 ? (
                  <p className="text-xs text-neutral-400 mt-2">
                    No editable core blocks are present yet. Archived passages
                    can still exist below.
                  </p>
                ) : (
                  recentBlocks.map((block) => (
                    <MemoryBlockCard
                      key={block.id}
                      block={block}
                      onEdit={openEdit}
                      badges={[hostTag(block.agent_id), block.agent_id]}
                    />
                  ))
                )}
              </div>
            </div>
          )}
        </section>

        <section className="rounded-lg border border-border/70 bg-background/60 p-4">
          <div className="pb-3 border-b border-border">
            <SectionHeader
              eyebrow="Long-tail recall"
              title="Archived knowledge"
              description="This surface now shows recent archived passages directly and searches across all visible agents instead of only one selected agent."
            />
            <ArchivalSearchBox
              loading={loadingRecall}
              onSearch={runRecallSearch}
            />
          </div>

          <div className="pt-3 space-y-5">
            <div>
              <h3 className="text-label uppercase tracking-widest text-neutral-400">
                Recent archived passages
              </h3>
              {loadingPassages ? (
                <p className="text-xs text-neutral-400 mt-2">
                  Loading archived passages...
                </p>
              ) : recentPassages.length === 0 ? (
                <p className="text-xs text-neutral-400 mt-2">
                  No archived passages are visible yet.
                </p>
              ) : (
                <ul className="mt-3 space-y-3">
                  {recentPassages.map((passage) => (
                    <li
                      key={passage.id}
                      className="py-2 border-b border-border"
                    >
                      <div className="flex flex-wrap items-center gap-2 text-caption font-mono uppercase tracking-widest text-neutral-400">
                        <span className="px-2 py-0.5 border border-border text-cyan-300">
                          {hostTag(passage.agent_id)}
                        </span>
                        <span className="px-2 py-0.5 border border-border text-neutral-400">
                          {passage.agent_id}
                        </span>
                        <span className="px-2 py-0.5 border border-border text-neutral-400">
                          {passage.source}
                        </span>
                      </div>
                      <p className="mt-2 text-xs text-neutral-300 whitespace-pre-wrap leading-relaxed">
                        {passage.text}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <h3 className="text-label uppercase tracking-widest text-neutral-400">
                Search results
              </h3>
              {recallResults.length === 0 ? (
                <p className="text-xs text-neutral-400 mt-2">
                  No archival search results yet.
                </p>
              ) : (
                <ul className="mt-3 space-y-3">
                  {recallResults.map((passage) => (
                    <li
                      key={passage.id}
                      className="py-2 border-b border-border"
                    >
                      <div className="flex flex-wrap items-center gap-2 text-caption font-mono uppercase tracking-widest text-neutral-400">
                        {passage.agent_id && (
                          <>
                            <span className="px-2 py-0.5 border border-border text-cyan-300">
                              {hostTag(passage.agent_id)}
                            </span>
                            <span className="px-2 py-0.5 border border-border text-neutral-400">
                              {passage.agent_id}
                            </span>
                          </>
                        )}
                        {passage.source && (
                          <span className="px-2 py-0.5 border border-border text-neutral-400">
                            {passage.source}
                          </span>
                        )}
                      </div>
                      <p className="mt-2 text-xs text-neutral-300 whitespace-pre-wrap leading-relaxed">
                        {passage.text}
                      </p>
                      <div className="mt-1">
                        {passage.source_ref &&
                        safeHref(passage.source_ref) ? (
                          <a
                            href={safeHref(passage.source_ref) ?? undefined}
                            target="_blank"
                            rel="noreferrer"
                            className="text-xs text-amber-300 underline"
                          >
                            Source
                          </a>
                        ) : (
                          <span className="text-xs text-neutral-400">
                            No source
                          </span>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </section>
      </div>

      {editDraft && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open) setEditDraft(null);
          }}
        >
          <DialogContent className="max-w-3xl">
            <DialogHeader>
              <DialogTitle>Review memory block update</DialogTitle>
              <DialogDescription>
                Label: {editDraft.block.label}
              </DialogDescription>
            </DialogHeader>
            <DialogBody>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <h3 className="text-label uppercase tracking-widest text-neutral-400 mb-1">
                    Current
                  </h3>
                  <pre className="min-h-40 whitespace-pre-wrap rounded-md border border-border/70 bg-surface-code p-3 text-xs text-neutral-300">
                    {editDraft.block.value}
                  </pre>
                </div>
                <div>
                  <label
                    htmlFor="memory-next-value"
                    className="text-label uppercase tracking-widest text-neutral-400 mb-1 block"
                  >
                    New
                  </label>
                  <UiTextarea
                    id="memory-next-value"
                    aria-label="Edit memory block value"
                    value={editDraft.nextValue}
                    onChange={(event) =>
                      setEditDraft((prev) =>
                        prev ? { ...prev, nextValue: event.target.value } : prev
                      )
                    }
                    className="min-h-40 font-mono"
                  />
                </div>
              </div>

              {conflictMessage && (
                <p className="text-xs text-red-300 mt-3">{conflictMessage}</p>
              )}
            </DialogBody>
            <DialogFooter>
              <UiButton
                type="button"
                aria-label="Cancel memory block edit"
                onClick={() => setEditDraft(null)}
                size="sm"
                variant="outline"
              >
                Cancel
              </UiButton>
              <UiButton
                type="button"
                aria-label="Save memory block edit"
                onClick={submitEdit}
                size="sm"
                variant="default"
              >
                Save
              </UiButton>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
        </div>
      )}
    </div>
  );
}
