import { useEffect, useRef, useState } from "react";
import { isColdLoad } from "../reviewBootLocation";
import { Link, useLocation } from "react-router-dom";
import { useFlowPaletteScreen } from "../palette/FlowPalette";

import { Button } from "../../components/ui/button";
import { Checkbox } from "../../components/ui/checkbox";
import { ToggleGroup } from "../../components/ui/toggle-group";
import { FlowShortcutsDialog } from "../FlowTopBar";
import { focusLegend, keyHint, type KeyBindings, type RebindableIntent } from "../flowKeymap";
import { requestBrowserJson } from "../reviewApi";
import {
  INTENT_LABEL,
  REBINDABLE,
  RESET_KEYS_PATCH,
  bindingsOf,
  captureKey,
  keyField,
  resetMessage,
  type ThemeChoice,
} from "./settingsModel";
import { modelLine, readRunOn, runOnOptions } from "./aiModel";
import { AiBudgetField } from "./AiBudgetField";
import { SettingsRow } from "./settingsShell";
import { SettingsPolicy } from "./SettingsPolicy";
import { SendFeedbackDialog } from "../feedback/SendFeedbackDialog";
import { applyThemeChoice, patchUserSettings, useUserSettings } from "./userSettings";
import { getReviewCodeFamily, setReviewCodeFamily, type ReviewCodeFamily } from "../reviewCodeTheme";

const isPhone = () => typeof window.matchMedia === "function" && window.matchMedia("(max-width: 639px)").matches;

const SECTIONS = [
  { id: "reading", label: "Reading" },
  { id: "ai-help", label: "AI help" },
  { id: "notifications", label: "Notifications" },
  { id: "keyboard", label: "Keyboard" },
  { id: "look", label: "Look" },
  { id: "more", label: "More settings" },
] as const;

const Row = SettingsRow;

function Switch({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (next: boolean) => void; hint?: string }) {
  return (
    <label className="flex items-start gap-3 text-label">
      <Checkbox className="mt-1 pointer-coarse:h-5 pointer-coarse:w-5" role="switch" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span>
        {label}
        {hint ? <span className="block text-meta text-muted-foreground">{hint}</span> : null}
      </span>
    </label>
  );
}

const AI_PATH = "/api/review-intelligence";

interface AiPreferences { "intelligence.run_on": string; "intelligence.driver"?: string }

/** Where AI jobs run is the machine's real `intelligence.run_on` setting (the one AI & background edits). The budget is `AiBudgetField`. */
function useAiHelp() {
  const [prefs, setPrefs] = useState<AiPreferences | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    requestBrowserJson<{ preferences: AiPreferences }>(AI_PATH)
      .then((result) => { if (active) setPrefs(result.preferences); })
      .catch(() => { if (active) setError("Couldn't read the AI settings."); });
    return () => { active = false; };
  }, []);
  const save = (change: Partial<AiPreferences>) => {
    setError("");
    const before = prefs;
    // Shown at once; the machine's answer replaces it, and a refusal puts the earlier state back.
    setPrefs((current) => (current ? { ...current, ...change } : current));
    requestBrowserJson<{ preferences: AiPreferences }>(`${AI_PATH}/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: change }),
    })
      .then((result) => setPrefs(result.preferences))
      .catch((reason: unknown) => {
        setPrefs(before);
        setError(reason instanceof Error && reason.message ? reason.message : "Couldn't save the AI setting.");
      });
  };
  return {
    ready: prefs !== null,
    runOn: readRunOn(prefs?.["intelligence.run_on"]),
    driver: prefs?.["intelligence.driver"],
    error,
    setRunOn: (next: string) => { if (next === "local" || next === "auto") save({ "intelligence.run_on": next }); },
  };
}

/** Settings › You and Review policy (SPEC 4.11). Reads and writes the server's per-user settings. */
export function ReviewSettingsScreen() {
  const { settings, status } = useUserSettings();
  const bindings = bindingsOf(settings);
  const [message, setMessage] = useState("");
  const [capturing, setCapturing] = useState<RebindableIntent | null>(null);
  const [captureError, setCaptureError] = useState("");
  const [codeFamily, setCodeFamily] = useState<ReviewCodeFamily>(getReviewCodeFamily);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const ai = useAiHelp();
  const heading = useRef<HTMLHeadingElement>(null);
  const bindingsRef = useRef<KeyBindings>(bindings);
  bindingsRef.current = bindings;

  const location = useLocation();
  // An address with a section (`/settings/review#keyboard`, from the palette) scrolls to it and focuses its heading.
  useEffect(() => {
    const id = decodeURIComponent(location.hash.slice(1));
    const title = id ? document.getElementById(`${id}-title`) : null;
    // No hash, or one naming a section that is not drawn: the page heading takes focus (SPEC 3.6).
    // SPEC 5.3: focus never moves on the first load; a link to a section still scrolls to it.
    const cold = isColdLoad();
    if (!title) { if (!cold) heading.current?.focus({ preventScroll: true }); return; }
    document.getElementById(id)?.scrollIntoView({ block: "start" });
    title.tabIndex = -1;
    if (!cold) title.focus({ preventScroll: true });
  }, [location.hash]);

  // While a key is being picked, this listener owns the keyboard (capture phase, so no other shortcut sees it).
  useEffect(() => {
    if (!capturing) return;
    const intent = capturing;
    function onKey(event: KeyboardEvent) {
      const result = captureKey(bindingsRef.current, intent, event);
      if (result.kind === "wait") return;
      event.preventDefault();
      event.stopPropagation();
      if (result.kind === "cancel") {
        setCapturing(null);
        setCaptureError("");
        return;
      }
      if (result.kind === "refused") {
        setCaptureError(result.message);
        return;
      }
      setCapturing(null);
      setCaptureError("");
      void patchUserSettings({ [keyField(intent)]: result.key }).then((saved) => setMessage(saved.ok ? result.message : saved.message));
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [capturing]);

  const save = (patch: Parameters<typeof patchUserSettings>[0]) => {
    void patchUserSettings(patch).then((saved) => setMessage(saved.ok ? "" : saved.message));
  };

  const goTo = (id: string) => {
    const el = document.getElementById(id);
    el?.scrollIntoView({ block: "start" });
  };

  useFlowPaletteScreen({
    place: { label: "Settings", sub: "You" },
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "settings",
      sections: [...SECTIONS, { id: "policy", label: "Review policy" }].filter((section) => !(section.id === "keyboard" && isPhone())).map((section) => ({ id: section.id, label: section.label, run: () => goTo(section.id) })),
      actions: {},
    },
  });

  // This screen always sits inside the app's Settings tabs, which already navigate (back included), so it adds no
  // Back bar or side column of its own: only a strip that jumps to its sections (review-23.2).
  return (
    <div className="mx-auto w-full max-w-5xl px-4 pb-16" data-testid="review-settings">
      <nav aria-label="Settings sections" className="mt-4 flex flex-wrap items-center gap-1">
        {SECTIONS.map((section) => (
          <Button key={section.id} variant="ghost" size="sm" className="shrink-0" onClick={() => goTo(section.id)}>{section.label}</Button>
        ))}
        <span className="ml-2 text-meta text-muted-foreground">For repo admins:</span>
        <Button variant="ghost" size="sm" className="shrink-0" onClick={() => goTo("policy")}>Review policy</Button>
      </nav>
      <div className="mt-4">
        <div className="min-w-0 space-y-4">
          <section aria-labelledby="you-title" className="space-y-4">
            <div>
              <h1 id="you-title" ref={heading} tabIndex={-1} className="text-title font-semibold outline-none">You</h1>
              <p className="text-label text-muted-foreground">Personal preferences. They follow you across repos.</p>
              {status === "unavailable" ? <p role="status" className="mt-1 text-meta text-muted-foreground" data-testid="settings-unavailable">This server doesn't keep personal settings. This browser keeps them instead.</p> : null}
              {status === "offline" ? <p role="status" className="mt-1 text-meta text-muted-foreground" data-testid="settings-offline">Couldn't reach this machine's server. Showing what this browser remembers; changes are kept here and sent when it answers.</p> : null}
            </div>

            <Row id="reading" title="Reading">
              <div className="space-y-1">
                <p id="default-view-label" className="text-label font-semibold">Default view</p>
                <ToggleGroup
                  aria-label="Default view"
                  variant="segment"
                  value={settings.default_view}
                  options={[{ value: "one", label: "One at a time" }, { value: "all", label: "All files" }]}
                  onChange={(value) => { if (value === "one" || value === "all") save({ default_view: value }); }}
                />
              </div>
              <div>
                <p className="text-label font-semibold">Your defaults</p>
                <Switch label="Skip the Overview for reviews with 1 decision" checked={settings.skip_overview_single_decision} onChange={(next) => save({ skip_overview_single_decision: next })} />
                <Switch label="Start in All files for changes over 30 parts" checked={settings.start_all_files_over_30_parts} onChange={(next) => save({ start_all_files_over_30_parts: next })} />
              </div>
            </Row>

            <Row id="ai-help" title="AI help">
              <AiBudgetField />
              {/* Two limits, two jobs (review-23.5): dollars for all AI help here; a call count for background work there. */}
              <p className="text-meta text-muted-foreground" data-testid="settings-ai-budget-scope">
                This covers all AI help, asked for or automatic. Automatic background work also stops at its model-call limits in AI &amp; background.
              </p>
              {!ai.ready ? (
                <p className="text-label" data-testid="settings-ai-status">{ai.error || "Checking…"}</p>
              ) : (
                <>
                  <div className="space-y-1">
                    <p className="text-label font-semibold">Where AI jobs run</p>
                    <ToggleGroup aria-label="Where AI jobs run" variant="segment" value={ai.runOn} options={runOnOptions()} onChange={ai.setRunOn} />
                    <p className="text-meta text-muted-foreground" data-testid="settings-ai-model">{modelLine(ai.driver)}</p>
                  </div>
                  {ai.error ? <p role="alert" className="text-label text-p1">{ai.error}</p> : null}
                </>
              )}
              <Button asChild variant="outline" size="sm"><Link to="/settings/ai">Open AI &amp; background settings</Link></Button>
            </Row>

            <Row id="notifications" title="Notifications">
              <p className="text-label text-muted-foreground">This machine sends no notifications yet.</p>
            </Row>

            <Row id="keyboard" title="Keyboard">
              <p className="text-label text-muted-foreground sm:hidden">Keyboard shortcuts are for a computer with a keyboard.</p>
              <div className="space-y-3 max-sm:hidden">
              <p className="text-label" data-testid="settings-key-line">{`${keyHint("looksGood", bindings)} good · ${keyHint("needsChange", bindings)} change · ${keyHint("decideLater", bindings)} decide later · J / K move · ? all shortcuts`}</p>
              <Button variant="outline" size="sm" aria-haspopup="dialog" onClick={() => setShortcutsOpen(true)}>View all shortcuts</Button>
              <p className="text-label font-semibold">Rebind 3 keys</p>
              <ul className="space-y-2">
                {REBINDABLE.map((intent) => (
                  <li key={intent} className="flex flex-wrap items-center gap-3">
                    <span className="min-w-[10rem] text-label">{INTENT_LABEL[intent]}</span>
                    <kbd className="rounded border border-border px-2 py-0.5 font-mono text-meta max-sm:hidden" aria-label={`Current key ${keyHint(intent, bindings)}`}>{keyHint(intent, bindings)}</kbd>
                    <Button
                      variant="outline"
                      size="sm"
                     
                      aria-label={`Change the key for ${INTENT_LABEL[intent]}`}
                      onClick={() => { setCapturing(intent); setCaptureError(""); setMessage(""); }}
                    >
                      {capturing === intent ? "Press a key…" : "Change"}
                    </Button>
                  </li>
                ))}
              </ul>
              {capturing ? <p role="status" className="text-label" data-testid="settings-capture">Press the new key for {INTENT_LABEL[capturing]}. Esc cancels.</p> : null}
              {captureError ? <p role="alert" className="text-label text-p1" data-testid="settings-capture-error">{captureError}</p> : null}
              <Button
                variant="ghost"
                size="sm"
               
                onClick={() => { setCapturing(null); void patchUserSettings(RESET_KEYS_PATCH).then((saved) => setMessage(saved.ok ? resetMessage() : saved.message)); }}
              >
                Reset to defaults
              </Button>
              </div>
              <p role="status" aria-live="polite" className="min-h-5 text-label" data-testid="settings-message">{message}</p>
            </Row>

            <Row id="look" title="Look">
              <ToggleGroup
                aria-label="Theme"
                variant="segment"
                value={settings.theme}
                options={[{ value: "light", label: "Light" }, { value: "dark", label: "Dark" }, { value: "auto", label: "Auto" }]}
                onChange={(value) => {
                  if (value !== "light" && value !== "dark" && value !== "auto") return;
                  applyThemeChoice({ ...settings, theme: value as ThemeChoice });
                  save({ theme: value as ThemeChoice });
                }}
              />
              <div className="mt-3 flex flex-col gap-1">
                <span className="text-label" aria-hidden="true">Code colors</span>
                <ToggleGroup
                  aria-label="Code colors"
                  variant="segment"
                  value={codeFamily}
                  options={[{ value: "lemoncrow", label: "LemonCrow" }, { value: "github", label: "GitHub" }]}
                  onChange={(value) => {
                    if (value !== "lemoncrow" && value !== "github") return;
                    setReviewCodeFamily(value);
                    setCodeFamily(value);
                  }}
                />
                <span className="text-meta text-muted-foreground">Light or dark follows the theme above.</span>
              </div>
            </Row>

            <Row id="more" title="More settings">
              <p className="text-meta text-muted-foreground">Nothing else to set yet.</p>
              <Button variant="outline" onClick={() => setFeedbackOpen(true)} data-testid="settings-send-feedback">Send feedback</Button>
            </Row>
          </section>

          <SettingsPolicy />
        </div>
      </div>
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={focusLegend({ aiAsk: false, bindings })} />
      <SendFeedbackDialog open={feedbackOpen} onOpenChange={setFeedbackOpen} />
    </div>
  );
}
