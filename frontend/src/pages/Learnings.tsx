import { useEffect, useState, useMemo, type ReactNode } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Brain,
  Circle,
  CircleDashed,
  CircleDot,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Database,
  Scale,
  X,
} from "lucide-react";
import { api, type Playbook, type PlanRecord, type Cluster } from "../api";
import {
  Chip,
  MetricCard,
  SectionHeader,
  ToggleGroup,
} from "../components/WorkbenchUI";
import Memory from "./Memory";
import Rubrics from "./Rubrics";

type Section = "blocks" | "memory" | "failures" | "plans" | "rubrics";

const SECTIONS: {
  id: Section;
  label: string;
  icon: React.ElementType;
  desc: string;
}[] = [
  { id: "blocks", label: "Blocks", icon: Brain, desc: "Curated procedures" },
  {
    id: "memory",
    label: "Memory",
    icon: Database,
    desc: "Pinned + archival recall",
  },
  {
    id: "failures",
    label: "Failures",
    icon: AlertTriangle,
    desc: "Error clusters",
  },
  { id: "plans", label: "Plans", icon: ClipboardList, desc: "Plan validation" },
  { id: "rubrics", label: "Rubrics", icon: Scale, desc: "Verification gates" },
];

export default function Learnings() {
  const { section } = useParams<{ section?: string }>();
  const navigate = useNavigate();
  const active = (section as Section) || "blocks";

  const setSection = (s: Section) =>
    navigate(`/knowledge/${s}`, { replace: true });

  return (
    <div className="space-y-6">
      <section className="grid grid-cols-2 gap-3">
        <MetricCard
          label="Surfaces"
          value={String(SECTIONS.length)}
          detail="Blocks, memory, failures, plans, and rubrics."
          tone="amber"
        />
        <MetricCard
          label="Current view"
          value={SECTIONS.find((item) => item.id === active)?.label ?? "Blocks"}
          detail="Switch tabs to move from procedures to constraints."
          tone="neutral"
        />
      </section>

      <ToggleGroup
        variant="underline"
        size="sm"
        options={SECTIONS.map((s) => ({
          value: s.id,
          label: (
            <span className="flex items-center gap-1.5">
              <s.icon size={14} />
              <span>{s.label}</span>
            </span>
          ),
          title: s.desc,
        }))}
        value={active}
        onChange={(value) => setSection(value as Section)}
      />

      {active === "blocks" && <BlocksSection />}
      {active === "memory" && <Memory />}
      {active === "failures" && <FailuresSection />}
      {active === "plans" && <PlansSection />}
      {active === "rubrics" && <Rubrics />}
    </div>
  );
}

// ─── Blocks ───────────────────────────────────────────────────────────────────

function BlocksSection() {
  const [items, setItems] = useState<Playbook[] | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<
    "all" | "active" | "retired" | "deprecated"
  >("all");
  const [domainFilter, setDomainFilter] = useState<string>("all");
  const [search, setSearch] = useState("");

  useEffect(() => {
    api
      .blocks()
      .then(setItems)
      .catch((e) => setErr(String(e)));
  }, []);

  const domains = useMemo(
    () => [...new Set(items?.map((b) => b.domain).filter(Boolean))],
    [items]
  );

  const filtered = useMemo(() => {
    if (!items) return [];
    return items.filter((b) => {
      if (filter !== "all" && b.status !== filter) return false;
      if (domainFilter !== "all" && b.domain !== domainFilter) return false;
      if (search) {
        const q = search.toLowerCase();
        return (
          b.title.toLowerCase().includes(q) ||
          b.id.toLowerCase().includes(q) ||
          b.domain.toLowerCase().includes(q)
        );
      }
      return true;
    });
  }, [items, filter, domainFilter, search]);

  if (err) return <div className="text-red-300">Error: {err}</div>;
  if (!items) return <div className="text-neutral-400">Loading…</div>;

  const standingRules = items.filter(
    (block) => block.domain === "universal" || block.task_types.length >= 3
  ).length;
  const highSignal = items.filter(
    (block) => block.failure_signals.length > 0 || block.failure_count > 0
  ).length;

  return (
    <div className="space-y-6">
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Catalog size"
          value={String(items.length)}
          detail="All reviewable procedures in the runtime."
          tone="amber"
        />
        <MetricCard
          label="Standing rules"
          value={String(standingRules)}
          detail="Blocks broad enough to shape many tasks."
          tone="emerald"
        />
        <MetricCard
          label="High-signal blocks"
          value={String(highSignal)}
          detail="Blocks carrying failure or verification signals."
          tone="violet"
        />
        <MetricCard
          label="Visible now"
          value={String(filtered.length)}
          detail="Current search + filter result set."
          tone="neutral"
        />
      </section>

      <section className="border border-neutral-800 bg-neutral-950/70 p-5">
        <SectionHeader
          eyebrow="Catalog controls"
          title="Search by title, id, domain, and operating shape"
          description="Use the filters to narrow the knowledge base before you open a block. The goal is to find the right procedure quickly, then inspect why it exists."
        />
        <div className="mt-5 flex gap-2 flex-wrap items-center">
          {(["all", "active", "retired", "deprecated"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`text-[10px] px-2.5 py-1 uppercase font-bold tracking-tight font-mono transition border ${
                filter === f
                  ? "border-neutral-500 bg-neutral-800 text-neutral-100"
                  : "border-neutral-700 text-neutral-400 hover:text-neutral-300"
              }`}
            >
              {f}
            </button>
          ))}
          <select
            aria-label="Filter learnings by domain"
            value={domainFilter}
            onChange={(e) => setDomainFilter(e.target.value)}
            className="text-[10px] bg-neutral-900/50 border border-neutral-700 px-2 py-1 text-neutral-400 font-mono"
          >
            <option value="all">All domains</option>
            {domains.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <input
            type="text"
            placeholder="Search…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="ml-auto text-[11px] bg-neutral-900/50 border border-neutral-700 px-2 py-1 text-neutral-300 placeholder:text-neutral-400 w-40 font-mono"
          />
        </div>
      </section>

      <div className="space-y-3">
        {filtered.map((b) => (
          <BlockCard
            key={b.id}
            block={b}
            isExpanded={expandedId === b.id}
            onToggle={() =>
              setExpandedId((prev) => (prev === b.id ? null : b.id))
            }
          />
        ))}
        {filtered.length === 0 && (
          <div className="text-neutral-400 text-sm italic py-4 font-mono">
            No blocks match the current filters.
          </div>
        )}
      </div>

      <section className="border border-neutral-800 bg-neutral-950/70 p-5">
        <SectionHeader
          eyebrow="How to read a block"
          title="Each card is organized around application, verification, and avoidance"
          description="Atelier treats knowledge entries as explicit procedures: when to apply them, how to execute them, how to verify them, and what dead ends they are meant to prevent."
        />
      </section>
    </div>
  );
}

// ─── Failures ─────────────────────────────────────────────────────────────────

function FailuresSection() {
  const [items, setItems] = useState<Cluster[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .clusters()
      .then(setItems)
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) return <div className="text-red-300">Error: {err}</div>;
  if (!items) return <div className="text-neutral-400">Loading…</div>;

  if (items.length === 0)
    return (
      <div className="text-neutral-400 text-center py-12">
        <Check size={48} className="mx-auto mb-4 text-emerald-300" />
        <p>No failure clusters detected — agents running smoothly.</p>
      </div>
    );

  return (
    <div className="space-y-2">
      {items.map((c) => {
        const isExpanded = expandedId === c.id;
        const severityColor =
          c.severity === "high"
            ? "bg-red-900/30 text-red-300"
            : c.severity === "medium"
              ? "bg-amber-900/30 text-amber-300"
              : "bg-neutral-800/50 text-neutral-400";
        return (
          <div
            key={c.id}
            className="border border-neutral-800 bg-neutral-900/50 overflow-hidden"
          >
            <button
              onClick={() => setExpandedId(isExpanded ? null : c.id)}
              className="w-full px-5 py-4 text-left hover:bg-neutral-800/50 transition-colors flex items-start gap-3"
            >
              <span
                className={`text-[10px] px-2 py-1 font-mono font-bold uppercase flex-shrink-0 mt-0.5 ${severityColor}`}
              >
                {c.severity}
              </span>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <ChevronRight
                    size={14}
                    className={`text-neutral-400 transition-transform ${isExpanded ? "rotate-90" : ""}`}
                  />
                  <span className="font-mono font-bold text-neutral-200 text-sm">
                    {c.domain}
                  </span>
                </div>
                <p className="text-xs text-neutral-400">
                  {c.trace_ids.length} trace
                  {c.trace_ids.length !== 1 ? "s" : ""} · {c.id}
                </p>
              </div>
            </button>
            {isExpanded && (
              <div className="border-t border-neutral-800 bg-neutral-950/50 px-5 py-4 space-y-4 text-xs">
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-400 mb-2 font-mono">
                    Fingerprint
                  </div>
                  <div className="font-mono text-red-300 whitespace-pre-wrap break-words bg-neutral-950 p-2 border border-neutral-800">
                    {c.fingerprint}
                  </div>
                </div>
                {c.sample_errors && c.sample_errors.length > 0 && (
                  <div>
                    <div className="text-[10px] uppercase tracking-widest text-neutral-400 mb-2 font-mono">
                      Sample errors
                    </div>
                    <div className="space-y-1">
                      {c.sample_errors.map((e, j) => (
                        <div
                          key={j}
                          className="font-mono text-neutral-400 bg-neutral-950 p-2 border border-neutral-800"
                        >
                          {e}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {c.suggested_block_title && (
                  <div>
                    <div className="text-[10px] uppercase tracking-widest text-neutral-400 mb-2 font-mono">
                      Suggested block
                    </div>
                    <div className="text-neutral-300 bg-neutral-950 p-2 border border-neutral-800">
                      {c.suggested_block_title}
                    </div>
                  </div>
                )}
                {c.trace_ids && c.trace_ids.length > 0 && (
                  <div className="pt-2 border-t border-neutral-800">
                    <div className="text-[10px] uppercase tracking-widest text-neutral-400 mb-2 font-mono">
                      Trace IDs
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {c.trace_ids.slice(0, 10).map((t, j) => (
                        <span
                          key={j}
                          className="font-mono text-neutral-400 bg-neutral-950 px-2 py-0.5 border border-neutral-800"
                        >
                          {t}
                        </span>
                      ))}
                      {c.trace_ids.length > 10 && (
                        <span className="text-neutral-400">
                          +{c.trace_ids.length - 10} more
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Plans ────────────────────────────────────────────────────────────────────

function PlansSection() {
  const [items, setItems] = useState<PlanRecord[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .plans()
      .then(setItems)
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) return <div className="text-red-300">Error: {err}</div>;
  if (!items) return <div className="text-neutral-400">Loading…</div>;

  if (items.length === 0)
    return (
      <div className="text-neutral-400 text-center py-12">
        <ClipboardList size={48} className="mx-auto mb-4" />
        <p>No plan validation results yet.</p>
      </div>
    );

  return (
    <div className="space-y-2">
      {items.map((p) => {
        const isExpanded = expandedId === p.trace_id;
        const statusColor =
          p.status === "success"
            ? "bg-emerald-900/30 text-emerald-300"
            : "bg-red-900/30 text-red-300";
        return (
          <div
            key={p.trace_id}
            className="border border-neutral-800 bg-neutral-900/50 overflow-hidden"
          >
            <button
              onClick={() => setExpandedId(isExpanded ? null : p.trace_id)}
              className="w-full px-5 py-4 text-left hover:bg-neutral-800/50 transition-colors flex items-start gap-3"
            >
              <span
                className={`text-[10px] px-2 py-1 font-mono font-bold uppercase flex-shrink-0 mt-0.5 ${statusColor}`}
              >
                {p.status}
              </span>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <ChevronRight
                    size={14}
                    className={`text-neutral-400 transition-transform ${isExpanded ? "rotate-90" : ""}`}
                  />
                  <span className="font-mono font-bold text-neutral-200 text-sm">
                    {p.domain}
                  </span>
                </div>
                <p className="text-xs text-neutral-400 truncate">{p.task}</p>
              </div>
            </button>
            {isExpanded && (
              <div className="border-t border-neutral-800 bg-neutral-950/50 px-5 py-4 space-y-3 text-xs">
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-400 mb-2 font-mono">
                    Task
                  </div>
                  <p className="text-neutral-300">{p.task}</p>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-400 mb-2 font-mono">
                    Trace ID
                  </div>
                  <code className="font-mono text-neutral-400 bg-neutral-950 px-2 py-1 block border border-neutral-800">
                    {p.trace_id}
                  </code>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-400 mb-2 font-mono">
                    Checks
                  </div>
                  <ul className="space-y-1">
                    {p.plan_checks.map((c, i) => (
                      <li
                        key={i}
                        className={`px-2 py-1 border flex items-start gap-2 ${
                          c.passed
                            ? "text-emerald-300 bg-emerald-900/10 border-emerald-900/30"
                            : "text-red-300 bg-red-900/10 border-red-900/30"
                        }`}
                      >
                        <span className="flex-shrink-0 mt-0.5">
                          {c.passed ? <Check size={12} /> : <X size={12} />}
                        </span>
                        <div className="flex-1">
                          <div>{c.name}</div>
                          {c.detail && (
                            <div className="text-[10px] text-neutral-400 mt-0.5">
                              {c.detail}
                            </div>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Block components (copied from Blocks.tsx) ────────────────────────────────

function blockTier(block: Playbook): string {
  if (block.domain === "universal" || block.task_types.length >= 3) {
    return "standing rule";
  }
  if (block.failure_signals.length > 0 || block.failure_count > 0) {
    return "risk pattern";
  }
  return "task pattern";
}

function blockSeverity(block: Playbook): "high" | "medium" | "low" {
  if (block.failure_signals.length > 0 || block.dead_ends.length > 1) {
    return "high";
  }
  if (block.verification.length > 0 || block.when_not_to_apply.trim()) {
    return "medium";
  }
  return "low";
}

function BlockCard({
  block,
  isExpanded,
  onToggle,
}: {
  block: Playbook;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="border border-neutral-800 bg-neutral-900/50 overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full px-5 py-4 text-left hover:bg-neutral-800/50 transition-colors flex items-start gap-4"
      >
        <div className="flex-shrink-0 mt-0.5">
          {block.status === "active" ? (
            <CircleDot size={18} className="text-emerald-300" />
          ) : block.status === "retired" ? (
            <CircleDashed size={18} className="text-neutral-400" />
          ) : (
            <Circle size={18} className="text-red-300" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 mb-1 flex-wrap">
            <ChevronRight
              size={14}
              className={`text-amber-300 transition-transform ${isExpanded ? "rotate-90" : ""}`}
            />
            <h3 className="font-mono font-bold text-neutral-200 text-sm">
              {block.title}
            </h3>
            <StatusBadge status={block.status} />
            {block.domain && (
              <span className="text-[10px] px-1.5 py-0.5 bg-neutral-800 text-neutral-300 uppercase font-bold tracking-tight font-mono">
                {block.domain}
              </span>
            )}
            <Chip tone="violet">{blockTier(block)}</Chip>
            <Chip
              tone={
                blockSeverity(block) === "high"
                  ? "amber"
                  : blockSeverity(block) === "medium"
                    ? "cyan"
                    : "neutral"
              }
            >
              {blockSeverity(block)} signal
            </Chip>
          </div>
          <div className="text-[10px] text-neutral-400 font-mono">
            {block.id}
          </div>
        </div>
      </button>
      {isExpanded && (
        <div className="border-t border-neutral-800 bg-neutral-950/50 px-5 py-4">
          <BlockDetail block={block} />
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    active: "bg-emerald-900/40 text-emerald-300",
    retired: "bg-neutral-700 text-neutral-400",
    deprecated: "bg-red-900/40 text-red-300",
  };
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 font-bold uppercase tracking-tight font-mono ${map[status] || map.retired}`}
    >
      {status}
    </span>
  );
}

function BlockDetail({ block }: { block: Playbook }) {
  const total = block.usage_count;
  const successRate =
    total > 0 ? Math.round((block.success_count / total) * 100) : null;

  return (
    <div className="space-y-5 text-sm">
      <header className="pb-4 border-b border-neutral-800">
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <StatusBadge status={block.status} />
          <span className="text-[10px] px-1.5 py-0.5 bg-neutral-800 text-neutral-300 uppercase font-bold tracking-tight">
            {block.domain}
          </span>
          {block.task_types.map((t) => (
            <span
              key={t}
              className="text-[10px] px-1.5 py-0.5 bg-neutral-900 border border-neutral-700 text-neutral-400 font-mono"
            >
              {t}
            </span>
          ))}
        </div>
        <h2 className="text-base font-bold text-neutral-300 leading-snug">
          {block.title}
        </h2>
        <div className="font-mono text-[10px] text-neutral-400 mt-1">
          {block.id}
        </div>
        <div className="flex gap-2 mt-1 text-[10px] text-neutral-400">
          <span>Created {new Date(block.created_at).toLocaleString()}</span>
          {block.updated_at && (
            <span>· Updated {new Date(block.updated_at).toLocaleString()}</span>
          )}
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-3">
          <div className="border border-neutral-800 bg-neutral-950/60 p-3">
            <div className="text-[10px] uppercase tracking-widest text-neutral-400">
              Tier
            </div>
            <div className="mt-1 text-sm text-neutral-200">
              {blockTier(block)}
            </div>
          </div>
          <div className="border border-neutral-800 bg-neutral-950/60 p-3">
            <div className="text-[10px] uppercase tracking-widest text-neutral-400">
              Used by
            </div>
            <div className="mt-1 text-sm text-neutral-200">
              {block.task_types.length > 0
                ? block.task_types.join(", ")
                : "General retrieval"}
            </div>
          </div>
          <div className="border border-neutral-800 bg-neutral-950/60 p-3">
            <div className="text-[10px] uppercase tracking-widest text-neutral-400">
              Focus
            </div>
            <div className="mt-1 text-sm text-neutral-200">
              {blockSeverity(block)} signal
            </div>
          </div>
        </div>
        {total > 0 && (
          <div className="flex gap-3 mt-3">
            <Stat label="Uses" value={total} />
            <Stat
              label="✓"
              value={block.success_count}
              color="text-emerald-300"
            />
            <Stat label="✗" value={block.failure_count} color="text-red-300" />
            {successRate !== null && (
              <Stat
                label="Rate"
                value={`${successRate}%`}
                color={
                  successRate >= 70 ? "text-emerald-300" : "text-amber-300"
                }
              />
            )}
          </div>
        )}
      </header>

      <section className="grid gap-3 md:grid-cols-3">
        <div className="border border-neutral-800 bg-neutral-950/60 p-3">
          <div className="text-[10px] uppercase tracking-widest text-neutral-400">
            Detects
          </div>
          <p className="mt-2 text-xs leading-relaxed text-neutral-300">
            {block.situation.trim()}
          </p>
        </div>
        <div className="border border-neutral-800 bg-neutral-950/60 p-3">
          <div className="text-[10px] uppercase tracking-widest text-neutral-400">
            Catches
          </div>
          <p className="mt-2 text-xs leading-relaxed text-neutral-300">
            {block.dead_ends.length > 0
              ? block.dead_ends[0]
              : block.failure_signals[0] ||
                "General task drift and re-derivation."}
          </p>
        </div>
        <div className="border border-neutral-800 bg-neutral-950/60 p-3">
          <div className="text-[10px] uppercase tracking-widest text-neutral-400">
            Why it matters
          </div>
          <p className="mt-2 text-xs leading-relaxed text-neutral-300">
            {block.verification[0] ||
              block.when_not_to_apply ||
              "It gives the runtime a reusable, reviewable procedure instead of rediscovering the path in every session."}
          </p>
        </div>
      </section>

      {block.situation && (
        <section>
          <SL>When to apply</SL>
          <p className="text-neutral-300 text-[13px] leading-relaxed bg-neutral-900/40 border border-neutral-800 px-3 py-2.5">
            {block.situation.trim()}
          </p>
        </section>
      )}

      {block.procedure.length > 0 && (
        <section>
          <SL>Procedure</SL>
          <ol className="space-y-2">
            {block.procedure.map((step, i) => (
              <li
                key={i}
                className="flex gap-3 bg-neutral-900/40 border border-neutral-800 px-3 py-2.5"
              >
                <span className="shrink-0 w-5 h-5 bg-neutral-800 text-neutral-400 text-[10px] font-bold flex items-center justify-center mt-0.5">
                  {i + 1}
                </span>
                <span className="text-neutral-300 text-[13px] leading-relaxed">
                  {step}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {(block.task_types.length > 0 ||
        block.triggers.length > 0 ||
        block.required_rubrics.length > 0) && (
        <section>
          <SL>Relationships</SL>
          <div className="flex flex-wrap gap-2">
            {block.task_types.map((item) => (
              <Chip key={`task-${item}`} tone="violet">
                task {item}
              </Chip>
            ))}
            {block.triggers.map((item) => (
              <Chip key={`trigger-${item}`} tone="cyan">
                trigger {item}
              </Chip>
            ))}
            {block.required_rubrics.map((item) => (
              <Chip key={`rubric-${item}`} tone="emerald">
                rubric {item}
              </Chip>
            ))}
          </div>
        </section>
      )}

      {block.verification.length > 0 && (
        <section>
          <SL>Verification</SL>
          <ul className="space-y-1.5">
            {block.verification.map((v, i) => (
              <li
                key={i}
                className="flex gap-2 items-start text-[13px] text-emerald-300 bg-emerald-950/20 border border-emerald-900/30 px-3 py-2"
              >
                <Check size={14} className="shrink-0 text-emerald-300 mt-0.5" />
                {v}
              </li>
            ))}
          </ul>
        </section>
      )}

      {block.dead_ends.length > 0 && (
        <section>
          <SL>Dead ends — do not attempt</SL>
          <ul className="space-y-1.5">
            {block.dead_ends.map((d, i) => (
              <li
                key={i}
                className="flex gap-2 items-start text-[13px] text-red-300 bg-red-950/20 border border-red-900/30 px-3 py-2"
              >
                <X size={14} className="shrink-0 text-red-300 mt-0.5" />
                {d}
              </li>
            ))}
          </ul>
        </section>
      )}

      {block.failure_signals.length > 0 && (
        <section>
          <SL>Failure signals</SL>
          <ul className="space-y-1.5">
            {block.failure_signals.map((s, i) => (
              <li
                key={i}
                className="flex gap-2 items-start text-[13px] text-amber-300 bg-amber-950/20 border border-amber-900/30 px-3 py-2"
              >
                <AlertTriangle
                  size={14}
                  className="shrink-0 text-amber-300 mt-0.5"
                />
                {s}
              </li>
            ))}
          </ul>
        </section>
      )}

      {block.when_not_to_apply?.trim() && (
        <section>
          <SL>When NOT to apply</SL>
          <p className="text-neutral-400 text-[13px] leading-relaxed bg-neutral-900/40 border border-neutral-700 px-3 py-2.5 italic">
            {block.when_not_to_apply.trim()}
          </p>
        </section>
      )}

      {(block.triggers.length > 0 ||
        block.file_patterns.length > 0 ||
        block.tool_patterns.length > 0) && <MatchHints block={block} />}
    </div>
  );
}

function SL({ children }: { children: ReactNode }) {
  return (
    <div className="text-[10px] uppercase font-bold tracking-widest text-neutral-400 mb-2">
      {children}
    </div>
  );
}

function Stat({
  label,
  value,
  color = "text-neutral-300",
}: {
  label: string;
  value: string | number;
  color?: string;
}) {
  return (
    <div className="flex flex-col items-center bg-neutral-900/60 border border-neutral-800 px-2.5 py-1.5 min-w-[48px]">
      <span className={`text-sm font-bold ${color}`}>{value}</span>
      <span className="text-[10px] text-neutral-400 uppercase tracking-wide">
        {label}
      </span>
    </div>
  );
}

function MatchHints({ block }: { block: Playbook }) {
  const [open, setOpen] = useState(false);
  return (
    <section>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-[10px] uppercase font-bold tracking-widest text-neutral-400 hover:text-neutral-400 transition mb-2"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />} Match
        hints
      </button>
      {open && (
        <div className="space-y-2">
          {block.triggers.length > 0 && (
            <ChipRow
              label="Triggers"
              items={block.triggers}
              color="bg-blue-950/40 text-blue-300 border-blue-900/40"
            />
          )}
          {block.file_patterns.length > 0 && (
            <ChipRow
              label="File patterns"
              items={block.file_patterns}
              color="bg-brand-950/40 text-brand-300 border-brand-900/40"
              mono
            />
          )}
          {block.tool_patterns.length > 0 && (
            <ChipRow
              label="Tool patterns"
              items={block.tool_patterns}
              color="bg-neutral-800 text-neutral-300 border-neutral-700"
              mono
            />
          )}
        </div>
      )}
    </section>
  );
}

function ChipRow({
  label,
  items,
  color,
  mono = false,
}: {
  label: string;
  items: string[];
  color: string;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase text-neutral-400 mb-1">{label}</div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <span
            key={item}
            className={`text-[11px] px-2 py-0.5 border ${color} ${mono ? "font-mono" : ""}`}
          >
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}
