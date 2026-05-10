import { useEffect, useState, useMemo } from "react";
import { api, type GranularToolUsage } from "../api";
import { MetricCard } from "../components/WorkbenchUI";

const AGENTS = ["Claude", "Codex", "Copilot", "Opencode", "Gemini"];
const CATEGORIES = [
  "Native / Unoptimized",
  "Atelier Optimized",
  "Other Third-Party / Minor",
  "Miscellaneous",
  "Token Usage",
];

// Cost Drivers Chart - shows cost attribution by token type and tool
function CostDriversChart({
  data,
  stats,
}: {
  data: GranularToolUsage[];
  stats: any;
}) {
  const baseInput = data
    .filter((d) => d.event_type === "prompt")
    .reduce((acc, d) => acc + d.input_tokens, 0);
  const cachedInput = data
    .filter((d) => d.event_type === "cached_prompt")
    .reduce((acc, d) => acc + d.input_tokens, 0);
  const cacheCreate = data
    .filter((d) => d.event_type === "cache_create")
    .reduce((acc, d) => acc + d.input_tokens, 0);

  const totalGrossInputTokens = baseInput + cachedInput + cacheCreate || 1;
  const contextWindowShare = (cachedInput / totalGrossInputTokens) * 100;

  const totalOutputTokens = stats.totalOutputTokens || 1;

  // Top output token drivers
  const toolOutputs = defaultdict_int();
  data
    .filter((d) => d.event_type === "tool_call")
    .forEach((d) => {
      toolOutputs[d.tool_name] += d.output_tokens;
    });

  const topTools = Object.entries(toolOutputs)
    .map(([name, tokens]) => ({
      name,
      tokens,
      share: (tokens / totalOutputTokens) * 100,
    }))
    .sort((a, b) => b.tokens - a.tokens)
    .slice(0, 5);

  return (
    <section className="border border-neutral-800 bg-neutral-950/70 p-5 space-y-4">
      <div className="text-[11px] uppercase tracking-widest text-neutral-400 font-bold">
        Cost Drivers
      </div>
      <div className="space-y-3">
        {/* Context Window */}
        <div className="space-y-1">
          <div className="flex justify-between text-[10px]">
            <span className="text-neutral-300">Context Window Usage</span>
            <span className="font-mono text-neutral-400">
              {contextWindowShare.toFixed(1)}% of input
            </span>
          </div>
          <div className="h-2 bg-neutral-900 overflow-hidden rounded">
            <div
              className="h-full bg-red-500/40"
              style={{ width: `${Math.min(contextWindowShare, 100)}%` }}
            />
          </div>
        </div>

        {/* Top output token tools */}
        {topTools.map((tool, i) => (
          <div key={i} className="space-y-1">
            <div className="flex justify-between text-[10px]">
              <span className="text-neutral-300">{tool.name}</span>
              <span className="font-mono text-neutral-400">
                {tool.share.toFixed(1)}% of output
              </span>
            </div>
            <div className="h-2 bg-neutral-900 overflow-hidden rounded">
              <div
                className="h-full bg-orange-500/60"
                style={{ width: `${tool.share}%` }}
              />
            </div>
          </div>
        ))}
      </div>

      <div className="text-[9px] text-neutral-500 pt-2 border-t border-neutral-800">
        <p>
          💡 <span className="font-semibold">Key insight:</span>{" "}
          {contextWindowShare.toFixed(1)}% of context is cached. Top 5 tools
          generate {topTools.reduce((acc, t) => acc + t.share, 0).toFixed(1)}%
          of output tokens.
        </p>
      </div>
    </section>
  );
}

// Optimization alerts
function OptimizationCards({ data }: { data: GranularToolUsage[] }) {
  const baseInput = data
    .filter((d) => d.event_type === "prompt")
    .reduce((acc, d) => acc + d.input_tokens, 0);
  const cachedInput = data
    .filter((d) => d.event_type === "cached_prompt")
    .reduce((acc, d) => acc + d.input_tokens, 0);
  const cacheCreate = data
    .filter((d) => d.event_type === "cache_create")
    .reduce((acc, d) => acc + d.input_tokens, 0);

  const totalGrossInputTokens = baseInput + cachedInput + cacheCreate || 1;
  const contextWindowTokens = cachedInput;
  const contextWindowShare =
    (contextWindowTokens / totalGrossInputTokens) * 100;

  const highOutputTools = defaultdict_int();
  const toolCalls = defaultdict_int();

  data
    .filter((d) => d.event_type === "tool_call")
    .forEach((d) => {
      highOutputTools[d.tool_name] += d.output_tokens;
      toolCalls[d.tool_name] += d.call_count ?? 1;
    });

  const toolsPerCall = Object.entries(highOutputTools)
    .map(([name, tokens]) => ({
      name,
      tokensPerCall: tokens / (toolCalls[name] || 1),
      calls: toolCalls[name],
      totalTokens: tokens,
    }))
    .sort((a, b) => b.tokensPerCall - a.tokensPerCall);

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Context Window Alert */}
      <section className="border border-red-900/30 bg-red-950/20 p-4">
        <div className="text-[10px] uppercase tracking-widest text-red-400 font-bold mb-2">
          ⚠️ Context Window Alert
        </div>
        <div className="space-y-2">
          <div className="text-2xl font-mono text-red-300">
            {(contextWindowTokens / 1_000_000).toFixed(1)}M
          </div>
          <div className="text-[10px] text-red-400/80">
            {contextWindowShare.toFixed(1)}% of all input tokens
          </div>
          <div className="text-[9px] text-red-300/60 leading-relaxed pt-2">
            Recommendation: Add summarization, file chunking, and context
            pruning to reduce context window size.
          </div>
        </div>
      </section>

      {/* Noisy Tool Output Alert */}
      <section className="border border-orange-900/30 bg-orange-950/20 p-4">
        <div className="text-[10px] uppercase tracking-widest text-orange-400 font-bold mb-2">
          ⚠️ Noisy Tool Output
        </div>
        <div className="space-y-2">
          <div className="text-[10px] text-orange-400/80 font-mono">
            Top offenders:
          </div>
          {toolsPerCall
            .filter((t) => t.tokensPerCall > 100_000)
            .slice(0, 3)
            .map((tool, i) => (
              <div key={i} className="text-[9px] text-orange-300/70">
                {tool.name}: ~{(tool.tokensPerCall / 1000).toFixed(0)}k per call
              </div>
            ))}
          <div className="text-[9px] text-orange-300/60 leading-relaxed pt-2">
            Recommendation: Add max output length, log truncation, and
            preview-only modes.
          </div>
        </div>
      </section>

      {/* High Cost Per Call */}
      <section className="border border-amber-900/30 bg-amber-950/20 p-4">
        <div className="text-[10px] uppercase tracking-widest text-amber-400 font-bold mb-2">
          💰 Most Expensive Calls
        </div>
        <div className="space-y-2">
          {toolsPerCall.slice(0, 3).map((tool, i) => (
            <div key={i} className="text-[9px]">
              <div className="text-amber-300/80 font-mono">{tool.name}</div>
              <div className="text-amber-300/60">
                ~{(tool.tokensPerCall / 1000).toFixed(0)}k tokens/call
              </div>
            </div>
          ))}
          <div className="text-[9px] text-amber-300/60 leading-relaxed pt-2">
            Show stderr/stdout size and truncate repeated logs.
          </div>
        </div>
      </section>
    </div>
  );
}

export default function Analytics() {
  const [data, setData] = useState<GranularToolUsage[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  // Filters
  const [agentFilter, setAgentFilter] = useState("all");
  const [modelFilter, setModelFilter] = useState("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [dateRange, setDateRange] = useState({ days: 30 });

  useEffect(() => {
    setLoading(true);
    api
      .granularAnalytics(undefined, undefined, 5000, dateRange.days)
      .then((usageData) => {
        setData(usageData);
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [dateRange.days]);

  const filteredData = useMemo(() => {
    const agentMatch = agentFilter.toLowerCase();
    const modelMatch = modelFilter.toLowerCase();

    return data.filter((item) => {
      const itemAgent = (item.agent || "").toLowerCase();
      const itemModel = (item.model || "").toLowerCase();

      if (agentFilter !== "all" && itemAgent !== agentMatch) return false;
      if (modelFilter !== "all" && itemModel !== modelMatch) return false;
      if (categoryFilter !== "all" && item.category !== categoryFilter)
        return false;

      if (search) {
        const s = search.toLowerCase();
        return (
          item.tool_name.toLowerCase().includes(s) ||
          (item.sub_command?.toLowerCase() || "").includes(s)
        );
      }

      return true;
    });
  }, [data, agentFilter, modelFilter, categoryFilter, search]);

  const models = useMemo(() => {
    const set = new Set<string>();
    data.forEach((d) => {
      if (d.model) set.add(d.model);
    });
    return Array.from(set).sort();
  }, [data]);

  // Cost and token calculations
  const stats = useMemo(() => {
    const totalOutputTokens = filteredData
      .filter((d) => ["result", "thinking", "tool_call"].includes(d.event_type))
      .reduce((acc, item) => acc + item.output_tokens, 0);

    const toolCalls = filteredData
      .filter((d) => d.event_type === "tool_call")
      .reduce((acc, item) => acc + (item.call_count ?? 1), 0);
    const uniqueTools = new Set(
      filteredData
        .filter((d) => d.event_type === "tool_call")
        .map((item) => item.tool_name)
    ).size;

    // Token breakdown
    const userTypedTokens = filteredData
      .filter((d) => d.event_type === "user_string")
      .reduce((acc, item) => acc + item.input_tokens, 0);

    const baseContextTokens = filteredData
      .filter((d) => d.event_type === "prompt")
      .reduce((acc, item) => acc + item.input_tokens, 0);

    const cachedPromptTokens = filteredData
      .filter((d) => d.event_type === "cached_prompt")
      .reduce((acc, item) => acc + item.input_tokens, 0);

    const cacheCreateTokens = filteredData
      .filter((d) => d.event_type === "cache_create")
      .reduce((acc, item) => acc + item.input_tokens, 0);

    const toolOutputTokens = filteredData
      .filter((d) => d.event_type === "tool_call")
      .reduce((acc, item) => acc + item.output_tokens, 0);
    const thinkingTokens = filteredData
      .filter((d) => d.event_type === "thinking")
      .reduce((acc, item) => acc + item.output_tokens, 0);
    const assistantResponseTokens = filteredData
      .filter((d) => d.event_type === "result")
      .reduce((acc, item) => acc + item.output_tokens, 0);

    // Cost calculation - exclude tool_call from grand total (attributed in drivers table)
    const totalCost = filteredData.reduce((acc, item) => {
      if (
        [
          "prompt",
          "cached_prompt",
          "cache_create",
          "result",
          "thinking",
        ].includes(item.event_type)
      ) {
        return acc + (item.cost || 0);
      }
      return acc;
    }, 0);

    const estimatedMonthlyCost = totalCost * (30 / (dateRange.days || 1));

    // Find top cost driver
    const toolCosts = defaultdict_int();
    filteredData.forEach((item) => {
      toolCosts[item.tool_name] += item.cost || 0;
    });
    const topCostDriver =
      Object.entries(toolCosts).sort((a, b) => b[1] - a[1])[0]?.[0] || "—";

    return {
      totalCost,
      estimatedMonthlyCost,
      topCostDriver,
      toolOutputTokens,
      thinkingTokens,
      assistantResponseTokens,
      userTypedTokens,
      baseContextTokens,
      cachedPromptTokens,
      cacheCreateTokens,
      toolCalls,
      uniqueTools,
      totalOutputTokens,
    };
  }, [filteredData, dateRange.days]);

  // Data for Host / Model table
  const hostModelStats = useMemo(() => {
    const grouped: Record<string, any> = {};

    filteredData.forEach((item) => {
      const key = `${item.agent}|${item.model || "unknown"}`;
      if (!grouped[key]) {
        grouped[key] = {
          agent: item.agent,
          model: item.model || "unknown",
          userTyped: 0,
          baseContext: 0,
          cachedPrompt: 0,
          cacheCreate: 0,
          billableOutput: 0,
          toolOutput: 0,
          thinking: 0,
          cost: 0,
          toolCalls: 0,
        };
      }

      if (item.event_type === "user_string")
        grouped[key].userTyped += item.input_tokens;
      if (item.event_type === "prompt")
        grouped[key].baseContext += item.input_tokens;
      if (item.event_type === "cached_prompt")
        grouped[key].cachedPrompt += item.input_tokens;
      if (item.event_type === "cache_create")
        grouped[key].cacheCreate += item.input_tokens;
      if (item.event_type === "tool_call") {
        grouped[key].toolOutput += item.output_tokens;
        grouped[key].toolCalls += item.call_count ?? 1;
      }
      if (item.event_type === "thinking")
        grouped[key].thinking += item.output_tokens;
      if (item.event_type === "result") {
        grouped[key].billableOutput += item.output_tokens;
      }

      // Cost attribution logic - include all events for row-level visibility
      grouped[key].cost += item.cost || 0;
    });

    return Object.values(grouped).sort(
      (a, b) => (b as any).cost - (a as any).cost
    );
  }, [filteredData]);

  // Data for cost drivers chart
  const costDriversData = useMemo(() => {
    const toolCosts = defaultdict_int();
    const toolCalls = defaultdict_int();
    const toolTokens = defaultdict_int();

    filteredData
      .filter((d) => d.event_type === "tool_call")
      .forEach((d) => {
        toolCosts[d.tool_name] += d.cost || 0;
        toolCalls[d.tool_name] += d.call_count ?? 1;
        toolTokens[d.tool_name] += d.output_tokens;
      });

    return Object.entries(toolCosts)
      .map(([tool, cost]) => ({
        tool,
        cost,
        calls: toolCalls[tool],
        tokens: toolTokens[tool],
        costPerCall: cost / (toolCalls[tool] || 1),
      }))
      .sort((a, b) => b.cost - a.cost)
      .slice(0, 10);
  }, [filteredData]);

  // Enhanced table data
  const tableData = useMemo(() => {
    return filteredData
      .map((item) => {
        const cost = item.cost || 0;
        const calls = item.call_count || 1;
        const outPerCall = item.output_tokens / calls;
        const inPerCall = item.input_tokens / calls;
        const costPerCall = cost / calls;
        const pctOfTotal =
          (item.output_tokens / (stats.totalOutputTokens || 1)) * 100;

        return {
          ...item,
          outPerCall,
          inPerCall,
          cost,
          costPerCall,
          pctOfTotal,
        };
      })
      .sort((a, b) => b.cost - a.cost);
  }, [filteredData, stats.totalOutputTokens]);

  if (err) return <div className="text-red-400 p-6">Error: {err}</div>;
  if (loading && data.length === 0)
    return (
      <div className="text-neutral-400 p-6 italic animate-pulse">
        Loading analytics...
      </div>
    );

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-8 bg-black min-h-screen text-neutral-200 font-sans">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-neutral-800 pb-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            Cost & Efficiency
          </h1>
          <p className="text-neutral-500 text-sm mt-1">
            Real-time token attribution and economic breakdown.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase font-bold text-neutral-500">
              Days
            </span>
            <input
              type="number"
              value={dateRange.days}
              onChange={(e) =>
                setDateRange({ days: parseInt(e.target.value) || 30 })
              }
              className="w-16 bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs font-mono text-neutral-300 focus:outline-none focus:border-emerald-500"
            />
          </div>

          <div className="h-4 w-px bg-neutral-800 mx-1 hidden md:block" />

          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase font-bold text-neutral-500">
              Agent
            </span>
            <select
              value={agentFilter}
              onChange={(e) => {
                setAgentFilter(e.target.value);
                setModelFilter("all");
              }}
              className="bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs text-neutral-300 focus:outline-none"
            >
              <option value="all">All Agents</option>
              {AGENTS.map((a) => (
                <option key={a} value={a.toLowerCase()}>
                  {a}
                </option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase font-bold text-neutral-500">
              Model
            </span>
            <select
              value={modelFilter}
              onChange={(e) => setModelFilter(e.target.value)}
              className="bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs text-neutral-300 focus:outline-none max-w-[150px]"
            >
              <option value="all">All Models</option>
              {models.map((m) => (
                <option key={m} value={m.toLowerCase()}>
                  {m}
                </option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase font-bold text-neutral-500">
              Category
            </span>
            <select
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
              className="bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs text-neutral-300 focus:outline-none"
            >
              <option value="all">All Categories</option>
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Overall Summary */}
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Total Estimated Cost"
          value={`$${stats.totalCost.toFixed(2)}`}
          tone="emerald"
        />
        <MetricCard
          label="Projected Month-End"
          value={`$${stats.estimatedMonthlyCost.toFixed(2)}`}
          tone="emerald"
        />
        <MetricCard
          label="Total Tool Calls"
          value={stats.toolCalls.toLocaleString()}
          tone="cyan"
        />
        <MetricCard
          label="Unique Tools"
          value={stats.uniqueTools.toString()}
          tone="cyan"
        />
      </section>

      {/* Host / Model Breakdown */}
      <section className="border border-neutral-800 bg-neutral-950/40 overflow-hidden">
        <div className="bg-neutral-900/80 border-b border-neutral-800 p-4 flex items-center justify-between">
          <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
            Host / Model Overview
          </div>
          <div className="text-[9px] text-neutral-600 font-mono">
            {filteredData.length} records in aggregate
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-500 font-mono bg-neutral-900/50">
                <th className="px-4 py-3">Host (Agent)</th>
                <th className="px-4 py-3">Model</th>
                <th className="px-4 py-3 text-right">User Typed (k)</th>
                <th className="px-4 py-3 text-right">Base Context (M)</th>
                <th className="px-4 py-3 text-right">Cached Prompt (M)</th>
                <th className="px-4 py-3 text-right">Cache Create (M)</th>
                <th className="px-4 py-3 text-right">Billable Out (M)</th>
                <th className="px-4 py-3 text-right">Tool Out (M)</th>
                <th className="px-4 py-3 text-right">Thinking (M)</th>
                <th className="px-4 py-3 text-right">Calls</th>
                <th className="px-4 py-3 text-right">Cost</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-900">
              {hostModelStats.length === 0 ? (
                <tr>
                  <td
                    colSpan={11}
                    className="px-4 py-8 text-center text-neutral-600 italic"
                  >
                    No data available for the selected filters.
                  </td>
                </tr>
              ) : (
                hostModelStats.map((row: any, idx) => (
                  <tr
                    key={idx}
                    className="hover:bg-neutral-800/20 transition-colors"
                  >
                    <td className="px-4 py-2 font-mono text-cyan-300/80">
                      {row.agent}
                    </td>
                    <td className="px-4 py-2 font-mono text-neutral-400">
                      {row.model}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-emerald-300/80">
                      {(row.userTyped / 1000).toFixed(1)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-emerald-400/80">
                      {(row.baseContext / 1_000_000).toFixed(1)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-red-400/80">
                      {(row.cachedPrompt / 1_000_000).toFixed(1)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-orange-400/80">
                      {(row.cacheCreate / 1_000_000).toFixed(1)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-violet-400/80">
                      {(row.billableOutput / 1_000_000).toFixed(1)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-amber-400/80">
                      {(row.toolOutput / 1_000_000).toFixed(1)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-cyan-400/80">
                      {(row.thinking / 1_000_000).toFixed(1)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-neutral-400">
                      {row.toolCalls.toLocaleString()}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-emerald-300 font-bold">
                      ${row.cost.toFixed(2)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Cost Drivers Chart */}
      <CostDriversChart data={filteredData} stats={stats} />

      {/* Optimization Cards */}
      <OptimizationCards data={filteredData} />

      {/* Cost Drivers Table */}
      <section className="border border-neutral-800 bg-neutral-950/40">
        <div className="bg-neutral-900/80 border-b border-neutral-800 p-4">
          <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
            Cost Drivers Ranking
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-500 font-mono bg-neutral-900/50">
                <th className="px-4 py-3">Rank</th>
                <th className="px-4 py-3">Tool</th>
                <th className="px-4 py-3 text-right">Calls</th>
                <th className="px-4 py-3 text-right">Output (M)</th>
                <th className="px-4 py-3 text-right">Out/Call</th>
                <th className="px-4 py-3 text-right">Est. Cost</th>
                <th className="px-4 py-3 text-right">Cost/Call</th>
                <th className="px-4 py-3 text-right">% Total</th>
                <th className="px-4 py-3">Optimization Hint</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-900">
              {costDriversData.length === 0 ? (
                <tr>
                  <td
                    colSpan={9}
                    className="px-4 py-8 text-center text-neutral-600 italic"
                  >
                    No tool usage found for this period.
                  </td>
                </tr>
              ) : (
                costDriversData.map((item, i) => (
                  <tr
                    key={i}
                    className="hover:bg-neutral-800/20 transition-colors"
                  >
                    <td className="px-4 py-3 font-mono text-neutral-600">
                      {i + 1}
                    </td>
                    <td className="px-4 py-3 font-medium text-neutral-300">
                      {item.tool}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-neutral-400">
                      {(item.calls || 0).toLocaleString()}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-neutral-400">
                      {(item.tokens / 1_000_000).toFixed(1)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-neutral-400">
                      {item.tokens / (item.calls || 1) > 10_000
                        ? `${(item.tokens / (item.calls || 1) / 1000).toFixed(0)}k`
                        : (item.tokens / (item.calls || 1)).toFixed(0)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-amber-300/80">
                      ${item.cost.toFixed(2)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-amber-300/80">
                      ${item.costPerCall.toFixed(4)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <span className="font-mono text-[10px] text-neutral-500">
                          {(
                            (item.tokens / (stats.toolOutputTokens || 1)) *
                            100
                          ).toFixed(1)}
                          %
                        </span>
                        <div className="w-12 h-1 bg-neutral-900 rounded-full overflow-hidden">
                          <div
                            className="h-full bg-amber-500/50"
                            style={{
                              width: `${
                                (item.tokens / (stats.toolOutputTokens || 1)) *
                                100
                              }%`,
                            }}
                          />
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-[10px] text-neutral-500 italic">
                      Review output size
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Detail Table */}
      <section className="border border-neutral-800 bg-neutral-950/40">
        <div className="bg-neutral-900/80 border-b border-neutral-800 p-4 flex items-center justify-between">
          <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
            Full Data Table
          </div>
          <div className="relative">
            <input
              type="text"
              placeholder="Search Tool / Sub-command"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="bg-neutral-900 border border-neutral-700 px-3 py-1.5 text-xs text-neutral-300 focus:outline-none focus:border-emerald-500 w-64 pl-8"
            />
            <svg
              className="absolute left-2.5 top-2 w-3.5 h-3.5 text-neutral-600"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
              />
            </svg>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-500 font-mono bg-neutral-900/50">
                <th className="px-4 py-3">Agent</th>
                <th className="px-4 py-3">Model</th>
                <th className="px-4 py-3">Category</th>
                <th className="px-4 py-3">Tool</th>
                <th className="px-4 py-3">Sub-command</th>
                <th className="px-4 py-3 text-right">Calls</th>
                <th className="px-4 py-3 text-right">In (M)</th>
                <th className="px-4 py-3 text-right">Out (M)</th>
                <th className="px-4 py-3 text-right">Out/Call</th>
                <th className="px-4 py-3 text-right">Est. Cost</th>
                <th className="px-4 py-3 text-right">Cost/Call</th>
                <th className="px-4 py-3 text-right">% Total</th>
                <th className="px-4 py-3">Date Range</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-900">
              {tableData.length === 0 ? (
                <tr>
                  <td
                    colSpan={13}
                    className="px-4 py-8 text-center text-neutral-600 italic"
                  >
                    No records found.
                  </td>
                </tr>
              ) : (
                tableData.map((item, i) => {
                  const dateRange =
                    item.first_seen && item.last_seen
                      ? `${new Date(item.first_seen).toLocaleDateString(
                          "en-GB"
                        )} – ${new Date(item.last_seen).toLocaleDateString(
                          "en-GB"
                        )}`
                      : "—";

                  return (
                    <tr
                      key={i}
                      className="hover:bg-neutral-800/20 transition-colors group"
                    >
                      <td className="px-4 py-2 font-mono text-neutral-400">
                        {item.agent}
                      </td>
                      <td className="px-4 py-2 font-mono text-neutral-500 text-[10px]">
                        {item.model || "—"}
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`text-[9px] px-1.5 py-0.5 border ${
                            item.category.includes("Optimized")
                              ? "border-emerald-900/50 text-emerald-400 bg-emerald-950/20"
                              : "border-neutral-800 text-neutral-500 bg-neutral-900/20"
                          }`}
                        >
                          {item.category}
                        </span>
                      </td>
                      <td className="px-4 py-2 font-medium text-neutral-300">
                        {item.tool_name}
                      </td>
                      <td className="px-4 py-2 text-neutral-500 font-mono italic">
                        {item.sub_command || "—"}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-neutral-400">
                        {(item.call_count ?? 1).toLocaleString()}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-neutral-400">
                        {(item.input_tokens / 1_000_000).toFixed(1)}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-neutral-400">
                        {(item.output_tokens / 1_000_000).toFixed(1)}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-neutral-500">
                        {(item.outPerCall / 1000).toFixed(0)}k
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-emerald-500/80">
                        ${(item.cost || 0).toFixed(2)}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-neutral-500">
                        $
                        {((item.cost || 0) / (item.call_count || 1)).toFixed(4)}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-neutral-500">
                        {item.pctOfTotal.toFixed(1)}%
                      </td>
                      <td className="px-4 py-2 text-neutral-600 text-[10px]">
                        {dateRange}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function defaultdict_int() {
  return new Proxy({} as Record<string, number>, {
    get: (target, name: string) => (name in target ? target[name] : 0),
  });
}
