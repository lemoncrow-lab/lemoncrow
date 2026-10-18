import { ChevronRight, Wrench } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  IntelligenceOverview,
  type BackgroundBudgetStatus,
  type IntelligenceRouteProjection,
} from "../components/IntelligenceSettings";
import { Card, Chip, PageFrame } from "../components/WorkbenchUI";
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
} from "../lib/RuntimeCapabilities";
import Integrations from "./Integrations";
import ReviewDevices from "./ReviewDevices";
import System from "./System";

type SettingsSection = "products" | "ai" | "devices" | "integrations" | "editor" | "diagnostics" | "telemetry" | "advanced";

/** This machine's effective AI route and locally persisted, non-secret preferences. */
export const LOCAL_INTELLIGENCE_PATH = "/api/review-intelligence";
const LOCAL_BUDGET_EDIT_SOURCE = "this machine's LemonCrow settings";

type IntelligencePreferences = {
  "intelligence.enabled": boolean;
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

function AiSettingsEditor({ data, onSaved }: {
  data: LocalIntelligenceSettings;
  onSaved: (next: LocalIntelligenceSettings) => void;
}) {
  const [settings, setSettings] = useState<IntelligencePreferences>(data.preferences);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const change = <K extends keyof IntelligencePreferences>(key: K, value: IntelligencePreferences[K]) => {
    setSettings((previous) => ({ ...previous, [key]: value }));
    setSaved(false);
  };
  const fieldClass = "mt-1 block h-9 w-full max-w-sm rounded-md border border-border bg-background px-2 text-body text-foreground";
  const labelClass = "block text-caption font-medium text-foreground";

  return (
    <form
      className="space-y-4 rounded-md border border-border p-4"
      aria-label="Configure local AI"
      onSubmit={(event) => {
        event.preventDefault();
        setSaving(true);
        setError("");
        setSaved(false);
        void requestBrowserJson<LocalIntelligenceSettings>(LOCAL_INTELLIGENCE_PATH + "/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ settings }),
        })
          .then((result) => { onSaved(result); setSettings(result.preferences); setSaved(true); })
          .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not save AI settings."))
          .finally(() => setSaving(false));
      }}
    >
      <div>
        <h3 className="text-body font-semibold text-foreground">Configure AI on this machine</h3>
        <p className="mt-1 text-caption text-muted-foreground">
          Choose a provider already configured on this machine. API keys and provider credentials stay outside the browser.
        </p>
      </div>
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
          <span className="mt-1 block text-micro font-normal text-muted-foreground">
            Use auto for the provider default; an explicit model requires an explicit provider.
          </span>
        </label>
        <div>
          <label className={labelClass} htmlFor="ai-run-on">Run background work</label>
          <Select id="ai-run-on" value={settings["intelligence.run_on"]}
            onValueChange={(value) => change("intelligence.run_on", value)}>
            <SelectItem value="auto">Auto (this machine)</SelectItem>
            <SelectItem value="local">This machine</SelectItem>
          </Select>
        </div>
        <div className="space-y-3">
          <label className="flex items-center gap-2 text-caption text-foreground">
            <Checkbox checked={settings["intelligence.enabled"]}
              onChange={(event) => change("intelligence.enabled", event.target.checked)} />
            Enable AI intelligence
          </label>
          <label className="flex items-center gap-2 text-caption text-foreground">
            <Checkbox checked={settings["intelligence.background.enabled"]}
              onChange={(event) => change("intelligence.background.enabled", event.target.checked)} />
            Run background intelligence
          </label>
        </div>
        <label className={labelClass} htmlFor="ai-monthly">
          Monthly model-call limit
          <Input id="ai-monthly" type="number" min={-1} max={1000000000} step={1}
            className={fieldClass} value={settings["intelligence.background.monthly_model_calls"]}
            onChange={(event) => change("intelligence.background.monthly_model_calls", Number(event.target.value))} />
          <span className="mt-1 block text-micro font-normal text-muted-foreground">−1 means unlimited.</span>
        </label>
        <label className={labelClass} htmlFor="ai-daily">
          Daily model-call limit
          <Input id="ai-daily" type="number" min={-1} max={1000000000} step={1}
            className={fieldClass} value={settings["intelligence.background.daily_model_calls"]}
            onChange={(event) => change("intelligence.background.daily_model_calls", Number(event.target.value))} />
          <span className="mt-1 block text-micro font-normal text-muted-foreground">−1 means no daily limit.</span>
        </label>
      </div>
      {error && <p role="alert" className="text-caption text-destructive">{error}</p>}
      {saved && <p role="status" className="text-caption text-foreground">Saved to this machine. Effective AI status updated below.</p>}
      <Button type="submit" variant="default" disabled={saving}>
        {saving ? "Saving…" : "Save AI settings"}
      </Button>
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
        <div>
          <h2 className="text-heading font-semibold text-foreground">AI &amp; background</h2>
          <p className="mt-1 text-body text-muted-foreground">The model route Review uses on this machine and the budget for automatic work.</p>
        </div>
        {data ? (
          <>
            {data.intelligence.editable && (
              <AiSettingsEditor data={data} onSaved={(result) => setData(result)} />
            )}
            <IntelligenceOverview
              scope="local"
              route={data.intelligence}
              background={data.background}
              lifetime={data.lifetime}
              budgetEditSource={LOCAL_BUDGET_EDIT_SOURCE}
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
      <h2 className="text-heading font-semibold text-foreground">LemonCrow Review is not installed</h2>
      <p className="max-w-xl text-body leading-6 text-muted-foreground">AI and background intelligence settings belong to Review.</p>
      <Link className="text-caption font-medium text-neutral-300 hover:text-foreground" to="/settings/products">
        View installed products
      </Link>
    </PageFrame>
  );
}

function productLabel(product: ProductId): string {
  if (product === "review") return "Review";
  if (product === "runtime") return "Runtime";
  return "Core";
}

function ProductsSettings() {
  const capabilities = useProductCapabilities();
  return (
    <PageFrame className="space-y-4">
      <section className="max-w-3xl space-y-3">
        <div>
          <h2 className="text-heading font-semibold text-foreground">Products</h2>
          <p className="mt-1 text-body text-muted-foreground">
            LemonCrow Review and Runtime are independent products behind the same <code className="text-foreground">lc</code> command.
          </p>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {(["review", "runtime"] as const).map((product) => {
            const state = capabilities.products[product];
            const label = productLabel(product);
            const detail = state.available
              ? `${state.mode} · ready`
              : state.installed
                ? "Installed · currently unavailable"
                : "Not installed";
            return (
              <Card key={product} className="p-4" data-testid={`product-state-${product}`}>
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-xs font-semibold text-foreground">LemonCrow {label}</div>
                    <div className="mt-1 text-caption text-muted-foreground">{detail}</div>
                  </div>
                  <Chip tone={state.available ? "success" : "neutral"}>{state.available ? "Available" : "Unavailable"}</Chip>
                </div>
                <p className="mt-3 text-caption leading-5 text-muted-foreground">
                  {product === "review"
                    ? "Understand, verify, comment on, and revisit code changes without requiring Runtime."
                    : "Code intelligence, agent sessions, MCP tools, and explicit Claude/Codex/OpenCode integration."}
                </p>
              </Card>
            );
          })}
        </div>
      </section>
    </PageFrame>
  );
}

function RuntimeSettingsUnavailable() {
  return (
    <PageFrame className="space-y-3">
      <h2 className="text-heading font-semibold text-foreground">LemonCrow Runtime is not installed</h2>
      <p className="max-w-xl text-body leading-6 text-muted-foreground">
        This settings page belongs to Runtime. Review remains available independently.
      </p>
      <Link className="text-caption font-medium text-neutral-300 hover:text-foreground" to="/settings/products">
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
          <h2 className="text-heading font-semibold text-foreground">Editor</h2>
          <p className="text-body leading-6 text-muted-foreground">
            Hosted LemonCrow reads source from the synchronized repository view. Opening VS Code or Cursor is a local-machine action; run the local LemonCrow UI when you want browser-to-editor handoff.
          </p>
        </section>
      </PageFrame>
    );
  }

  const selected = capability ? selectedEditor(capability) : null;
  return (
    <PageFrame className="space-y-4">
      <section className="max-w-xl space-y-3">
        <div>
          <h2 className="text-heading font-semibold text-foreground">Editor</h2>
          <p className="mt-1 text-body text-muted-foreground">Choose which local editor opens source files. VS Code is the default.</p>
        </div>
        {error && <p role="alert" className="text-body text-destructive">{error}</p>}
        {capability && (
          <>
            <div className="space-y-1.5">
              <label id="preferred-editor-label" className="block text-label font-medium text-foreground">Preferred editor</label>
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
const ADVANCED = [
  ["Hosts", "/system/hosts", "Raw host adapter status and imports.", null],
  ["Agents", "/system/agents", "Internal agent definitions.", null],
  ["Skills", "/system/skills", "Installed runtime skills.", null],
  ["MCP", "/system/mcp", "Internal MCP/runtime capability detail.", null],
  ["Watchdogs", "/system/watchdogs", "Runtime watchdogs for agent sessions.", "Coming soon"],
  ["Projection", "/system/projection", "Low-level projection inspector.", null],
] as const;

export default function Settings() {
  const { section } = useParams<{ section?: string }>();
  const productCapabilities = useProductCapabilities();
  const runtimeCapabilities = useRuntimeCapabilities();
  const active = (section as SettingsSection) || "products";

  if (active === "products") return <ProductsSettings />;
  if (active === "ai") return productCapabilities.products.review.available ? <AiBackgroundSettings /> : <ReviewSettingsUnavailable />;
  if (active === "devices") return productCapabilities.products.review.available ? <ReviewDevices /> : <ReviewSettingsUnavailable />;
  if (!productCapabilities.products.runtime.available) return <RuntimeSettingsUnavailable />;
  if (active === "integrations") return <Integrations />;
  if (active === "editor") return <EditorSettings />;
  if (active === "diagnostics") return <System forcedSection="health" />;
  if (active === "telemetry") return <System forcedSection="telemetry" />;

  if (!runtimeCapabilities.localDiagnostics) {
    return (
      <PageFrame className="space-y-3">
        <h2 className="text-heading font-semibold text-foreground">Advanced</h2>
        <p className="max-w-xl text-body leading-6 text-muted-foreground">
          Host adapters, installed agent files, local MCP process inspection, watchdog process state, and projection internals belong to a developer machine. Hosted operational health is managed by the LemonCrow service rather than exposed as local runtime state.
        </p>
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
            <Card className="h-full p-3 transition hover:bg-muted/50">
              <div className="flex items-start gap-3">
                <Wrench size={13} className="mt-0.5 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <div className="text-xs font-medium text-neutral-200">{label}</div>
                    {badge && <Chip tone="info">{badge}</Chip>}
                  </div>
                  <p className="mt-1 text-caption leading-4 text-muted-foreground">{description}</p>
                </div>
                <ChevronRight size={13} className="mt-0.5 shrink-0 text-muted-foreground" />
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </PageFrame>
  );
}
