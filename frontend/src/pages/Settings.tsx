import { ChevronRight, Wrench } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";

import {
  IntelligenceOverview,
  type BackgroundBudgetStatus,
  type IntelligenceRouteProjection,
} from "../components/IntelligenceSettings";
import { Chip, PageFrame, PageHeader } from "../components/WorkbenchUI";
import { Hunk, HunkBody, HunkHead, HunkLabel } from "../components/ui/hunk";
import { Button } from "../components/ui/button";
import { Checkbox } from "../components/ui/checkbox";
import { Input } from "../components/ui/input";
import { Select, SelectItem } from "../components/ui/select";
import { api, type CodeMapEditorCapability } from "../api";
import { requestBrowserJson } from "../review/reviewApi";
import { getEditorPreference, selectedEditor, setEditorPreference, type EditorId } from "../lib/editorPreference";
import {
  useProductCapabilities,
  useRuntimeCapabilities,
  type ProductId,
  type ProductMode,
} from "../lib/RuntimeCapabilities";
import Integrations from "./Integrations";
import ReviewDevices from "./ReviewDevices";
import { parseAiBudget, setAiBudget } from "../review/aiBudget";
import { ReviewSettingsScreen } from "../review/settings/ReviewSettingsScreen";
import System from "./System";

type SettingsSection = "products" | "review" | "review-flow" | "org" | "ai" | "devices" | "integrations" | "editor" | "diagnostics" | "telemetry" | "advanced";

/** This machine's effective AI route and locally persisted, non-secret preferences. */
export const LOCAL_INTELLIGENCE_PATH = "/api/review-intelligence";
const LOCAL_BUDGET_EDIT_SOURCE = "this machine's LemonCrow settings";

type IntelligencePreferences = {
  /** The AI budget is edited by `AiBudgetField`, which saves it on its own; this form never writes it. */
  "intelligence.monthly_budget_usd": number | null;
  "intelligence.driver": string;
  "intelligence.model": string;
  "intelligence.run_on": string;
  "intelligence.background.enabled": boolean;
  "intelligence.background.monthly_model_calls": number;
  "intelligence.background.daily_model_calls": number;
};

type LocalIntelligenceSettings = {
  scope: "local";
  intelligence: IntelligenceRouteProjection;
  background: BackgroundBudgetStatus;
  preferences: IntelligencePreferences;
  lifetime: string;
};

/**
 * What this form writes. The AI budget has its own control in Review flow, and where background work runs
 * (`intelligence.run_on`) is Review flow's "Where AI jobs run": a local install offers only this machine.
 */
const FORM_KEYS = [
  "intelligence.driver",
  "intelligence.model",
  "intelligence.background.enabled",
  "intelligence.background.monthly_model_calls",
  "intelligence.background.daily_model_calls",
] as const satisfies readonly (keyof IntelligencePreferences)[];

type CallLimitKey = "intelligence.background.monthly_model_calls" | "intelligence.background.daily_model_calls";

/** The shipped monthly ceiling (`DEFAULT_MONTHLY_MODEL_CALLS`), offered when a limit is first turned back on. */
const DEFAULT_MONTHLY_CALLS = 200;

/** A model-call limit: a whole number, or "Unlimited" (stored as -1). An empty field is NaN until filled. */
function CallLimitField({ id, label, unlimitedLabel, value, fallback, onChange }: {
  id: string;
  label: string;
  unlimitedLabel: string;
  value: number;
  /** Filled in when Unlimited is cleared and this field never held a limit; null leaves it empty. */
  fallback: number | null;
  onChange: (next: number) => void;
}) {
  const [lastLimit, setLastLimit] = useState<number | null>(value >= 0 ? value : null);
  const unlimited = value < 0;
  return (
    <div>
      <label className="block text-meta font-medium text-foreground" htmlFor={id}>{label}</label>
      <Input id={id} type="number" min={0} max={1000000000} step={1} required={!unlimited} disabled={unlimited}
        className="mt-1 block w-full max-w-sm" value={unlimited || Number.isNaN(value) ? "" : value}
        onChange={(event) => {
          const next = event.target.value === "" ? Number.NaN : Number(event.target.value);
          if (next >= 0) setLastLimit(next);
          onChange(next);
        }} />
      <label className="mt-2 flex items-center gap-2 text-ui text-foreground">
        <Checkbox checked={unlimited}
          onChange={(event) => onChange(event.target.checked ? -1 : lastLimit ?? fallback ?? Number.NaN)} />
        {unlimitedLabel}
      </label>
    </div>
  );
}

function AiSettingsEditor({ data, onSaved }: {
  data: LocalIntelligenceSettings;
  onSaved: (next: LocalIntelligenceSettings) => void;
}) {
  const [settings, setSettings] = useState<IntelligencePreferences>(data.preferences);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(false), 4000);
    return () => window.clearTimeout(timer);
  }, [saved]);
  const change = <K extends keyof IntelligencePreferences>(key: K, value: IntelligencePreferences[K]) => {
    setSettings((previous) => ({ ...previous, [key]: value }));
    setSaved(false);
  };
  const fieldClass = "mt-1 block w-full max-w-sm";
  const labelClass = "block text-meta font-medium text-foreground";
  const limit = (key: CallLimitKey) => settings[key];
  const monthly = limit("intelligence.background.monthly_model_calls");

  return (
    <form
      className="hunk"
      aria-label="Configure local AI"
      onSubmit={(event) => {
        event.preventDefault();
        if (Number.isNaN(monthly) || Number.isNaN(limit("intelligence.background.daily_model_calls"))) {
          setError("Enter a model-call limit, or choose unlimited.");
          return;
        }
        setSaving(true);
        setError("");
        setSaved(false);
        void requestBrowserJson<LocalIntelligenceSettings>(LOCAL_INTELLIGENCE_PATH + "/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ settings: Object.fromEntries(FORM_KEYS.map((key) => [key, settings[key]])) }),
        })
          .then((result) => { onSaved(result); setSettings(result.preferences); setSaved(true); const budget = parseAiBudget(result); if (budget) setAiBudget(budget); })
          .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not save AI settings."))
          .finally(() => setSaving(false));
      }}
    >
      {/* A settings card, not a diff hunk: a plain heading, no @@ label (review-23.4). */}
      <div className="border-b border-border px-4 py-3">
        <h3 className="text-label font-semibold text-foreground">Configure AI on this machine</h3>
      </div>
      <div className="space-y-4 p-4">
      <p className="text-meta text-muted-foreground">
        Choose a provider already configured on this machine. API keys and provider credentials stay outside the browser.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className={labelClass} htmlFor="ai-provider">Provider</label>
          <Select id="ai-provider" value={settings["intelligence.driver"]}
            onValueChange={(value) => change("intelligence.driver", value)}>
            {[
              ["auto", "Auto (environment default)"],
              ["claude", "Claude Code"],
              ["codex", "Codex"],
              ["opencode", "OpenCode"],
              ["ollama", "Ollama"],
              ["openai", "OpenAI-compatible API"],
              ["litellm", "LiteLLM"],
            ].map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}
          </Select>
        </div>
        <label className={labelClass} htmlFor="ai-model">
          Model
          <Input id="ai-model" className={fieldClass} value={settings["intelligence.model"]}
            maxLength={128} onChange={(event) => change("intelligence.model", event.target.value)}
            placeholder="auto" />
          <span className="mt-1 block text-meta font-normal text-muted-foreground">
            Use auto for the provider default; an explicit model requires an explicit provider.
          </span>
        </label>
        <div className="sm:col-span-2">
          <label className="flex items-center gap-2 text-ui font-medium text-foreground">
            <Checkbox checked={settings["intelligence.background.enabled"]}
              onChange={(event) => change("intelligence.background.enabled", event.target.checked)} />
            Run background work on this machine
          </label>
          <p className="mt-1 text-meta text-muted-foreground">
            Automatic AI Review, Project Knowledge narration and discussion summaries. When this is off, AI you ask for still runs.
            These limits count model calls for this background work only. The monthly AI budget still applies to it.
          </p>
        </div>
        <CallLimitField id="ai-monthly" label="Monthly model-call limit" unlimitedLabel="Unlimited"
          value={monthly} fallback={DEFAULT_MONTHLY_CALLS}
          onChange={(next) => change("intelligence.background.monthly_model_calls", next)} />
        <CallLimitField id="ai-daily" label="Daily model-call limit" unlimitedLabel="No daily limit"
          value={limit("intelligence.background.daily_model_calls")} fallback={null}
          onChange={(next) => change("intelligence.background.daily_model_calls", next)} />
      </div>
      {error && <p role="alert" className="text-meta text-p0">{error}</p>}
      {saved && <p role="status" className="text-meta text-foreground">AI settings saved.</p>}
      <Button type="submit" variant="brand" disabled={saving}>
        {saving ? "Saving…" : "Save AI settings"}
      </Button>
      </div>
    </form>
  );
}

function AiBackgroundSettings() {
  const [data, setData] = useState<LocalIntelligenceSettings | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setError("");
    requestBrowserJson<LocalIntelligenceSettings>(LOCAL_INTELLIGENCE_PATH)
      .then((result) => {
        if (active) setData(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error && reason.message ? reason.message : "Could not read AI settings.");
      });
    return () => {
      active = false;
    };
  }, [attempt]);

  return (
    <PageFrame className="space-y-4">
      <section className="max-w-3xl space-y-4">
        <PageHeader
          eyebrow="Settings"
          title="AI & background"
          description="The model route Review uses on this machine and the limits for automatic background work."
        />
        {data ? (
          <>
            {/* The dollar budget has one control, in Review flow › AI help; this page links to it rather than repeat it. */}
            <p className="text-meta text-muted-foreground">
              The monthly AI budget, in dollars and covering all AI help, is set in{" "}
              <Link className="font-medium text-foreground underline decoration-foreground/40 underline-offset-4 hover:decoration-foreground" to="/settings/review#ai-help">
                Review flow › AI help
              </Link>. The limits below count model calls, and only for automatic background work.
            </p>
            {data.intelligence.editable && <AiSettingsEditor data={data} onSaved={(result) => setData(result)} />}
            <IntelligenceOverview
              scope="local"
              route={data.intelligence}
              background={data.background}
              lifetime={data.lifetime}
              budgetEditSource={LOCAL_BUDGET_EDIT_SOURCE}
              editedAbove={data.intelligence.editable}
            />
          </>
        ) : error ? (
          <div role="alert" className="space-y-2">
            <p className="text-body text-muted-foreground">Could not read AI settings: {error}</p>
            <Button variant="outline" size="xs" type="button" onClick={() => setAttempt((value) => value + 1)}>
              Retry
            </Button>
          </div>
        ) : (
          <p role="status" className="text-body text-muted-foreground">Loading AI settings…</p>
        )}
      </section>
    </PageFrame>
  );
}

function ReviewSettingsUnavailable() {
  return (
    <PageFrame className="space-y-3">
      <PageHeader eyebrow="Settings" title="LemonCrow Review is not installed" description="AI and background intelligence settings belong to Review." />
      <Link className="inline-flex text-meta font-medium text-foreground underline decoration-foreground/40 underline-offset-4 hover:decoration-foreground" to="/settings/diagnostics">
        View installed products
      </Link>
    </PageFrame>
  );
}

function productLabel(product: ProductId, hosted: boolean): string {
  if (product === "review") return "LemonCrow Review";
  if (product === "runtime") return hosted ? "Workspaces Pro" : "Workspaces Lite";
  return "LemonCrow Core";
}

/** Where an available product runs, in words (the deployment mode itself is an internal enum). */
const MODE_DETAIL: Record<ProductMode, string> = {
  local: "Runs on this machine",
  hosted: "Hosted",
  hybrid: "Hosted, with this machine",
  none: "Ready",
};

/** The installed products, a panel of Diagnostics (it was its own Products tab). */
function ProductsPanel() {
  const capabilities = useProductCapabilities();
  return (
    <section id="products" aria-labelledby="products-title" className="max-w-3xl scroll-mt-24 space-y-3">
        <div>
          <h2 id="products-title" className="text-title font-semibold text-foreground">Installed products</h2>
          <p className="mt-1 text-meta text-muted-foreground">
            LemonCrow Review and Workspaces are independent products behind the same <code className="text-foreground">lc</code> command.
          </p>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {(["review", "runtime"] as const).map((product) => {
            const state = capabilities.products[product];
            const label = productLabel(product, capabilities.products.core.mode === "hosted");
            const detail = state.available
              ? MODE_DETAIL[state.mode]
              : state.installed
                ? "Installed · currently unavailable"
                : "Not installed";
            return (
              <Hunk key={product} data-testid={`product-state-${product}`}>
                <HunkHead>
                  <HunkLabel>{label}</HunkLabel>
                  <Chip tone={state.available ? "success" : "neutral"}>{state.available ? "Available" : "Unavailable"}</Chip>
                </HunkHead>
                <HunkBody className="p-4">
                  <div className="text-meta text-muted-foreground">{detail}</div>
                  <p className="mt-3 text-ui text-subtle">
                    {product === "review"
                      ? "Understand, verify, comment on, and revisit code changes without requiring Workspaces."
                      : "Code intelligence, agent sessions, MCP tools, and explicit Claude/Codex/OpenCode integration."}
                  </p>
                </HunkBody>
              </Hunk>
            );
          })}
        </div>
    </section>
  );
}

/** Diagnostics: the local server's health (with Workspaces) and the installed products (always). */
function DiagnosticsSettings() {
  const capabilities = useProductCapabilities();
  if (capabilities.products.runtime.available) return <System forcedSection="health" after={<ProductsPanel />} />;
  return (
    <PageFrame className="space-y-4">
      <PageHeader eyebrow="Settings" title="Diagnostics" description="Which LemonCrow products this installation has." />
      <ProductsPanel />
    </PageFrame>
  );
}

function RuntimeSettingsUnavailable() {
  return (
    <PageFrame className="space-y-3">
      <PageHeader eyebrow="Settings" title="Workspaces Lite is not installed" description="This settings page belongs to Workspaces. Review remains available independently." />
      <Link className="inline-flex text-meta font-medium text-foreground underline decoration-foreground/40 underline-offset-4 hover:decoration-foreground" to="/settings/diagnostics">
        View installed products
      </Link>
    </PageFrame>
  );
}

function EditorSettings() {
  const runtimeCapabilities = useRuntimeCapabilities();
  const [capability, setCapability] = useState<CodeMapEditorCapability | null>(null);
  const [preference, setPreference] = useState<EditorId>(getEditorPreference);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!runtimeCapabilities.localEditor) return;
    let active = true;
    api.codeMapEditorCapability().then((result) => {
      if (active) setCapability(result);
    }).catch(() => {
      if (active) setError("Could not detect local editors.");
    });
    return () => { active = false; };
  }, [runtimeCapabilities.localEditor]);

  if (!runtimeCapabilities.localEditor) {
    return (
      <PageFrame className="space-y-4">
        <section className="max-w-xl space-y-2">
          <PageHeader
            eyebrow="Settings"
            title="Editor"
            description="Hosted LemonCrow reads source from the synchronized repository view. Opening VS Code or Cursor is a local-machine action; run the local LemonCrow UI when you want browser-to-editor handoff."
          />
        </section>
      </PageFrame>
    );
  }

  const selected = capability ? selectedEditor(capability) : null;
  return (
    <PageFrame className="space-y-4">
      <section className="max-w-xl space-y-3">
        <PageHeader eyebrow="Settings" title="Editor" description="Choose which local editor opens source files. VS Code is the default." />
        {error && <p role="alert" className="text-body text-p0">{error}</p>}
        {capability && (
          <>
            <div className="space-y-1.5">
              <label id="preferred-editor-label" className="block text-meta font-medium text-foreground">Preferred editor</label>
              <Select
                aria-labelledby="preferred-editor-label"
                value={selected?.id ?? ""}
                disabled={!capability.available}
                onValueChange={(value) => {
                  if (value !== "vscode" && value !== "cursor") return;
                  setEditorPreference(value);
                  setPreference(value);
                }}
              >
                {capability.editors.map((editor) => <SelectItem key={editor.id} value={editor.id}>{editor.label}</SelectItem>)}
              </Select>
            </div>
            {!capability.available && <p className="text-body text-muted-foreground">Install VS Code or Cursor to open source files locally.</p>}
            {selected && preference !== selected.id && <p className="text-body text-muted-foreground">{preference === "vscode" ? "VS Code" : "Cursor"} is unavailable. Using {selected.label}.</p>}
            <p className="text-caption text-muted-foreground">Saved in this browser. The editor must be installed on the machine running LemonCrow.</p>
          </>
        )}
      </section>
    </PageFrame>
  );
}

// A badge, not a disabled card: the page behind this link is readable today, so
// the card stays live and only its availability is called out.
// A badge, not a disabled card: the page behind this link is readable today, so
// the card stays live and only its availability is called out.
// Watchdogs (unbuilt) and Projection (a developer inspector) are hidden from this list; their
// /system/* routes still answer a direct link.
const ADVANCED = [
  ["Hosts", "/system/hosts", "The coding tools LemonCrow works with, what it adds to each, and how to install it.", null],
  ["Agents", "/system/agents", "Specialist agents LemonCrow adds to Claude Code, with the tools and instructions each one uses.", null],
  ["Skills", "/system/skills", "The skills LemonCrow adds to your coding agents.", null],
  ["MCP", "/system/mcp", "The tools LemonCrow gives your coding agents, and whether each one is available.", null],
] as readonly (readonly [string, string, string, string | null])[];

const REVIEW_SECTION_ADDRESSES: ReadonlySet<string> = new Set(["reading", "notifications", "keyboard", "look", "more", "policy"]);

export default function Settings() {
  const { section } = useParams<{ section?: string }>();
  const productCapabilities = useProductCapabilities();
  const runtimeCapabilities = useRuntimeCapabilities();
  const active = (section as SettingsSection) || "diagnostics";

  // Products folded into Diagnostics: an old link lands on its panel.
  if (active === "products") return <Navigate to="/settings/diagnostics#products" replace />;
  if (active === "diagnostics") return <DiagnosticsSettings />;
  if (active === "review") return productCapabilities.products.review.available ? <ReviewSettingsScreen /> : <ReviewSettingsUnavailable />;
  // The organization page exists only on a hosted workspace (SPEC 7.1 P0-2): a local install has none, so it is absent.
  if (active === "org") return <Navigate to="/settings/review" replace />;
  // The retired "Review flow" page: an old bookmark lands on the Review settings.
  if (active === "review-flow") return <Navigate to="/settings/review" replace />;
  // A Review settings section by its own address (`/settings/look`): the Review page, scrolled to that section.
  if (REVIEW_SECTION_ADDRESSES.has(active)) return <Navigate to={`/settings/review#${active}`} replace />;
  if (active === "ai") return productCapabilities.products.review.available ? <AiBackgroundSettings /> : <ReviewSettingsUnavailable />;
  if (active === "devices") return productCapabilities.products.review.available ? <ReviewDevices /> : <ReviewSettingsUnavailable />;
  if (!productCapabilities.products.runtime.available) return <RuntimeSettingsUnavailable />;
  if (active === "integrations") return <Integrations />;
  if (active === "editor") return <EditorSettings />;
  if (active === "telemetry") return <System forcedSection="telemetry" />;

  if (!runtimeCapabilities.localDiagnostics) {
    return (
      <PageFrame className="space-y-3">
        <PageHeader
          eyebrow="Settings"
          title="Advanced"
          description="Host adapters, installed agent files, local MCP process inspection, watchdog process state, and projection internals belong to a developer machine. Hosted operational health is managed by the LemonCrow service rather than exposed as local runtime state."
        />
      </PageFrame>
    );
  }

  return (
    <PageFrame className="space-y-4">
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {ADVANCED.filter(
          // The projection inspector browses the code map, which a basic local Dashboard leaves hosted-only.
          ([, to]) => to !== "/system/projection" || productCapabilities.features["runtime.code"],
        ).map(([label, to, description, badge]) => (
          <Link key={to} to={to}>
            <Hunk className="h-full transition-colors duration-fast hover:border-input">
              <div className="flex items-start gap-3 p-3">
                <Wrench size={14} className="mt-0.5 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <div className="text-ui font-semibold text-foreground">{label}</div>
                    {badge && <Chip tone="neutral">{badge}</Chip>}
                  </div>
                  <p className="mt-1 text-meta text-muted-foreground">{description}</p>
                </div>
                <ChevronRight size={14} className="mt-0.5 shrink-0 text-muted-foreground" />
              </div>
            </Hunk>
          </Link>
        ))}
      </div>
    </PageFrame>
  );
}
