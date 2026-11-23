/** P4-1: deliberate, revision-pinned previews. Discovery never renders or runs a surface.
 * The screen has one Run button (review-17.1): it renders or runs the chosen surface's Before and After in the sandbox.
 * Nothing runs in the background; a surface is ranked by change once its own run compared both sides.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, Clipboard, Code2, ExternalLink, FileCode2, LoaderCircle, Maximize2, Minimize2, Play, X } from "lucide-react";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { fetchReviewSurfaces, fetchSurfacePreview, runReviewSurface } from "../reviewApi";
import { isColdLoad } from "../reviewBootLocation";
import { FlowAskOverlay, FlowShortcutsDialog, FlowTopBar, repoNameOf, useShortcutsKey } from "../FlowTopBar";
import { screenLegend } from "../flowKeymap";
import { counterFor } from "../focus/focusModel";
import ReviewViewTabs from "../ReviewViewTabs";
import { useReviewFlowNavigation } from "../useReviewFlowNavigation";
import { useFlowPaletteScreen } from "../palette/FlowPalette";
import { partEntries, reviewPlace } from "../palette/partEntries";
import { partSlug } from "../overview/overviewModel";
import type { ReviewOverview, ReviewSurfaceInfo, ReviewSurfacePluginIssue, ReviewTarget } from "../types";
import { allEvidence, dropEvidence, evidenceScope, pruneEvidence, recordEvidence } from "./previewEvidence";
import {
  canCompare, compareText, isExecutableCheck, notChecked, pathsOutsideChange, plainLocator, previewMeaning, providerName,
  provenanceText, reachedPaths, runToResult, surfaceGroup, surfaceKey, surfaceRows, type PreviewResult, type PreviewTab, type SurfaceRow,
} from "./previewFlowModel";

interface Props {
  reviewRef: string;
  apiReviewId: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  surface: string;
  /** The whole parts list is loaded (the bar counts only from a complete list). */
  complete?: boolean;
  /** "Ask about the change" (the top bar); absent when AI Ask is off. */
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
}
type Stage = "idle" | "running" | "ready" | "failed" | "cancelled";
interface RunState {
  key: string;
  stage: Stage;
  step: number;
  total: number;
  message: string;
  /** The side that failed, for "Couldn't start the new version". */
  failedSide?: "old" | "new";
  /** Set when only one side runs, so the heading names it. */
  only?: "old" | "new";
  outputs: { old: PreviewResult | null; new: PreviewResult | null };
}
const EMPTY_OUTPUTS = (): RunState["outputs"] => ({ old: null, new: null });
const EMPTY_RUN: RunState = { key: "", stage: "idle", step: 0, total: 0, message: "", outputs: EMPTY_OUTPUTS() };
const TAB_STORAGE = "lemoncrow.review.preview.tab.v1";
const GROUPS = ["Pages", "API requests", "Components", "Tables", "Checks", "Other"] as const;
const TABS = ["old", "new", "changes"] as const;
// review-17.3: the sides are Before and After, as on the Map and the X-Ray.
const TAB_NAME: Record<PreviewTab, string> = { old: "Before", new: "After", changes: "Changes" };
const SETUP_COMMAND = "lc review --setup";

function readTab(): PreviewTab {
  try {
    const stored = window.localStorage.getItem(TAB_STORAGE);
    return stored === "old" || stored === "new" || stored === "changes" ? stored : "changes";
  } catch { return "changes"; }
}
function describeSurface(s: ReviewSurfaceInfo): string {
  return s.preview?.label || s.title || s.locator || s.id;
}
function textArtifact(result: PreviewResult) {
  if (result.display !== "json") return result.text;
  try { return JSON.stringify(JSON.parse(result.text), null, 2); } catch { return result.text; }
}
const hydrate = (scope: string): Record<string, RunState> => Object.fromEntries(allEvidence(scope).map((e) => [e.surfaceKey, {
  key: e.surfaceKey, stage: "ready" as const, step: 2, total: 2, message: "Captured sides available. Inspect them below.", outputs: { old: e.old, new: e.next },
}]));
/** One short line for the heading; the whole message stays in the log. */
const firstLine = (text: string) => { const line = text.split("\n").find((l) => l.trim()) ?? ""; return line.length > 120 ? `${line.slice(0, 119)}…` : line; };

/** Output text never wraps: a wide line scrolls inside its own box. */
const OUTPUT_BOX = "max-w-full overflow-auto whitespace-pre bg-surface-code p-4 font-mono text-label leading-5";

function PreviewPane({ result, name }: { result: PreviewResult | null; name: string }) {
  if (!result) return <div className="p-6 text-label text-muted-foreground">{name} has not been rendered for this revision.</div>;
  const base = <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border p-3 text-meta text-muted-foreground">
    <span>{name} · {result.source === "run" ? "Run result" : "Rendered artifact"}</span>
    <span>{result.durationMs !== undefined ? `${result.durationMs} ms · ` : ""}{result.runner ? `${result.runner} · ` : ""}{result.status ?? result.mime}</span>
  </div>;
  if (result.display === "image" && result.url) return <div className="min-w-0 overflow-hidden rounded-lg border border-border bg-card">
    {base}<div className="flex justify-end border-b border-border px-3 py-2"><Button variant="outline" size="sm" onClick={() => window.open(result.url, "_blank", "noopener,noreferrer")}><ExternalLink size={16} aria-hidden="true"/> Open full size</Button></div>
    <div className="max-w-full overflow-auto bg-white"><img src={result.url} alt={`${name} rendered output`} className="max-w-none" /></div>
  </div>;
  if (result.display === "html") return <div className="min-w-0 overflow-hidden rounded-lg border border-border bg-card">
    {base}<p className="px-3 pt-2 text-tag text-muted-foreground">Untrusted content · scripts, forms and popups are blocked</p>
    {/* sandbox="" blocks scripts, forms, popups and navigating the page; the CSP blocks every load and a <base>. The
        injected <base target="_blank"> is best effort and is not claimed on screen. */}
    <iframe sandbox="" title={`${name} preview, isolated`}
      srcDoc={`<base target="_blank"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; form-action 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:">${result.text}`}
      referrerPolicy="no-referrer" className="h-[380px] w-full border-0 bg-white" />
  </div>;
  if (result.display === "download") return <div className="rounded-lg border border-border bg-card">{base}<p className="p-5 text-label text-muted-foreground">
    This surface returned a downloadable artifact ({result.filename}). It cannot be safely shown inline.
  </p></div>;
  return <div className="min-w-0 overflow-hidden rounded-lg border border-border bg-card">{base}
    {result.summary && <p className="p-3 text-label">{result.summary}</p>}
    <pre className={`max-h-[520px] min-h-28 ${OUTPUT_BOX}`} tabIndex={0} aria-label={`${name} output`} data-testid={`preview-output-${result.side}`}>{textArtifact(result)}</pre>
  </div>;
}
function Difference({ old, newer }: { old: PreviewResult | null; newer: PreviewResult | null }) {
  if (!old || !newer) return <div className="rounded-xl border border-border bg-card p-6 text-label text-muted-foreground" data-testid="preview-not-compared">
    Before and after have not both run for this revision. Nothing has been compared yet.
  </div>;
  const comparison = compareText(old, newer);
  if (!comparison) return <div className="space-y-3" data-testid="preview-side-by-side">
    <p className="text-label text-muted-foreground">Two captured sides are available. A changed-pixel comparison was not run; inspect both without assuming what changed.</p>
    <div className="grid min-w-0 gap-3 lg:grid-cols-2"><PreviewPane result={old} name={TAB_NAME.old}/><PreviewPane result={newer} name={TAB_NAME.new}/></div>
  </div>;
  return <div className="space-y-3" data-testid="preview-changes">
    <p className="text-label font-medium">{comparison.changed ? "Text output differs between captured sides." : "Both captured text outputs match."} This is a text comparison, not a verified behavior check.</p>
    <div className="grid min-w-0 gap-3 md:grid-cols-2">
      <section className="min-w-0 rounded-lg border border-border bg-card">
        <h3 className="border-b border-border p-3 text-label font-medium">− Only before · {comparison.removed.length}</h3>
        <pre className={`min-h-32 max-h-[480px] ${OUTPUT_BOX}`} tabIndex={0} aria-label="Lines only before" data-testid="preview-removed-lines">{comparison.removed.join("\n") || "None"}</pre>
      </section>
      <section className="min-w-0 rounded-lg border border-border bg-card">
        <h3 className="border-b border-border p-3 text-label font-medium">+ Only after · {comparison.added.length}</h3>
        <pre className={`min-h-32 max-h-[480px] ${OUTPUT_BOX}`} tabIndex={0} aria-label="Lines only after" data-testid="preview-added-lines">{comparison.added.join("\n") || "None"}</pre>
      </section>
    </div>
  </div>;
}
export default function ReviewFlowPreviewScreen({ reviewRef, apiReviewId, overview, targets, surface: selectedFromRoute, complete = true, onAskChange, askPanel }: Props) {
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  useShortcutsKey(() => setShortcutsOpen(true));
  // ⌘K: Preview has no keys of its own, so the palette offers only the shared groups (parts, reviews, repos, settings, help).
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    parts: partEntries(reviewRef, targets, "one"),
    place: reviewPlace(repoNameOf(overview), overview.title, "Preview"),
    openShortcuts: () => setShortcutsOpen(true),
    context: { screen: "other", actions: {} },
  });
  const revisionId = overview.revision.id;
  const scope = evidenceScope(apiReviewId, revisionId);
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const [discovery, setDiscovery] = useState<{ revision: string; rows: ReviewSurfaceInfo[]; issues: ReviewSurfacePluginIssue[]; error: string; loading: boolean }>({ revision: "", rows: [], issues: [], error: "", loading: true });
  const [tab, setTab] = useState<PreviewTab>(readTab);
  // One entry per surface: a run on one surface never changes what another shows.
  const [runs, setRuns] = useState<Record<string, RunState>>(() => hydrate(scope));
  const [logOpen, setLogOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  const [learnOpen, setLearnOpen] = useState(false);
  const [full, setFull] = useState(false);
  const [compactListOpen, setCompactListOpen] = useState(true);
  // A counter per surface plus one for the whole screen (a new revision, leaving): an older answer is dropped.
  const generation = useRef<Record<string, number>>({});
  const epoch = useRef(0);
  const blobUrls = useRef<Record<string, string[]>>({});
  const heading = useRef<HTMLHeadingElement>(null);
  const stateHeading = useRef<HTMLHeadingElement>(null);
  const notSetUpHeading = useRef<HTMLHeadingElement>(null);
  const lastStage = useRef({ key: "", stage: "idle" as Stage });

  const revokeBlobs = (key?: string) => {
    for (const [k, urls] of Object.entries(blobUrls.current)) if (key === undefined || k === key) { for (const url of urls) URL.revokeObjectURL(url); delete blobUrls.current[k]; }
  };
  // A cold load keeps the browser's own start (SPEC 3.6); every move to Preview puts focus on its heading.
  useEffect(() => { if (!isColdLoad()) heading.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => {
    let live = true;
    setDiscovery({ revision: revisionId, rows: [], issues: [], error: "", loading: true });
    fetchReviewSurfaces(apiReviewId, revisionId).then((result) => {
      if (!live) return;
      if (result.revision_id !== revisionId) throw new Error("Surface discovery belongs to another revision.");
      setDiscovery({ revision: revisionId, rows: result.surfaces, issues: result.issues ?? [], error: "", loading: false });
    }).catch((error: unknown) => {
      if (live) setDiscovery({ revision: revisionId, rows: [], issues: [], error: String(error instanceof Error ? error.message : error), loading: false });
    });
    return () => { live = false; };
  }, [apiReviewId, revisionId]);
  useEffect(() => {
    epoch.current++;
    revokeBlobs();
    pruneEvidence(apiReviewId, scope);
    setRuns(hydrate(scope));
  }, [apiReviewId, revisionId, scope]);
  useEffect(() => () => { epoch.current++; revokeBlobs(); }, []);

  // A surface is ranked by its own run: the lines that differ between its captured Before and After.
  const magnitudeByKey = useMemo(() => {
    const scores: Record<string, number> = {};
    for (const [key, result] of Object.entries(runs)) {
      if (result.stage !== "ready") continue;
      const difference = compareText(result.outputs.old, result.outputs.new);
      if (difference) scores[key] = difference.magnitude;
    }
    return scores;
  }, [runs]);
  const rows = useMemo(() => surfaceRows(discovery.revision === revisionId ? discovery.rows : [], targets, magnitudeByKey), [discovery, revisionId, targets, magnitudeByKey]);
  const reached = rows.filter((row) => row.reached);
  const selected = rows.find((row) => row.key === selectedFromRoute) ?? null;
  const currentRun = selected ? runs[selected.key] ?? EMPTY_RUN : EMPTY_RUN;
  const current = selected?.surface;
  const usablePreview = current?.preview?.status !== "unavailable" ? current?.preview : null;
  const canRender = Boolean(current && (usablePreview || (current.capabilities.includes("execute") && current.runtime)));
  const availableSides = usablePreview?.sides ?? (current?.capabilities.includes("execute") ? ["old", "new"] as const : []);
  const exactOld = currentRun.outputs.old, exactNew = currentRun.outputs.new;
  const canCompareSides = Boolean(current && canCompare({ ...current, preview: usablePreview }));
  const notSetUp = !discovery.loading && !discovery.error && rows.length === 0;
  const isCheck = current ? isExecutableCheck(current) : false;

  // The default surface is the top-ranked one (SPEC 4.7); the address is replaced so Back leaves Preview.
  useEffect(() => {
    if (!selectedFromRoute && rows.length > 0) goTo({ kind: "preview", surface: rows[0].key }, { replace: true });
  }, [selectedFromRoute, rows.length > 0, goTo]); // eslint-disable-line react-hooks/exhaustive-deps -- only the first row's key matters, once rows exist.

  // Focus follows the state: the heading of the new state takes it, so it never falls to the page when Cancel goes away.
  useEffect(() => {
    const previous = lastStage.current;
    lastStage.current = { key: selected?.key ?? "", stage: currentRun.stage };
    if (previous.key === lastStage.current.key && previous.stage !== currentRun.stage && currentRun.stage !== "idle") {
      stateHeading.current?.focus({ preventScroll: true });
    }
  }, [selected?.key, currentRun.stage]);
  useEffect(() => {
    if (notSetUp && !isColdLoad()) notSetUpHeading.current?.focus({ preventScroll: true });
  }, [notSetUp]);

  const openSurface = (s: ReviewSurfaceInfo, replace = false) => {
    goTo({ kind: "preview", surface: surfaceKey(s) }, { replace });
    setCompactListOpen(false);
  };
  const cancelRun = () => {
    if (!selected) return;
    const key = selected.key;
    // The server has no cancel: a request already sent may finish. Its answer is dropped and nothing is kept.
    generation.current[key] = (generation.current[key] ?? 0) + 1;
    revokeBlobs(key);
    dropEvidence(scope, key);
    setRuns((previous) => ({ ...previous, [key]: { key, stage: "cancelled", step: 0, total: 0, message: "", outputs: EMPTY_OUTPUTS() } }));
  };
  const execute = async () => {
    if (!current || !canRender || currentRun.stage === "running") return;
    const surface = current;
    const key = surfaceKey(surface);
    const ticket = (generation.current[key] = (generation.current[key] ?? 0) + 1);
    const era = epoch.current;
    const isCurrent = () => generation.current[key] === ticket && epoch.current === era;
    revokeBlobs(key);
    dropEvidence(scope, key);
    setLogOpen(false);
    const sides: Array<"old" | "new"> = canCompareSides ? ["old", "new"] : availableSides.includes("new") ? ["new"] : ["old"];
    const outputs = EMPTY_OUTPUTS();
    const only = sides.length === 1 ? sides[0] : undefined;
    const put = (state: RunState) => setRuns((previous) => ({ ...previous, [key]: { ...state, only } }));
    put({ key, stage: "running", step: 0, total: sides.length, message: "", outputs: EMPTY_OUTPUTS() });
    for (let i = 0; i < sides.length; i++) {
      if (!isCurrent()) return;
      const side = sides[i];
      try {
        let next: PreviewResult;
        if (usablePreview) {
          const artifact = await fetchSurfacePreview(apiReviewId, surface.provider, surface.id, side, revisionId);
          if (artifact.revisionId !== revisionId) throw new Error("A preview artifact returned for a different revision.");
          if (!isCurrent()) return;
          const display = usablePreview.display;
          const text = ["text", "json", "html"].includes(display) ? await artifact.blob.text() : "";
          if (!isCurrent()) return;
          const url = ["image", "download"].includes(display) ? URL.createObjectURL(artifact.blob) : undefined;
          if (url) (blobUrls.current[key] ??= []).push(url);
          next = { source: "preview", side, revisionId, display, mime: artifact.mediaType, text, url, filename: artifact.filename };
        } else {
          const response = await runReviewSurface(apiReviewId, surface.provider, surface.id, side, revisionId);
          if (response.revision_id !== revisionId) throw new Error("A check returned for a different revision.");
          if (!isCurrent()) return;
          next = runToResult(response.result, revisionId);
        }
        outputs[side] = next;
        const done = i + 1 === sides.length;
        put({ key, stage: done ? "ready" : "running", step: i + 1, total: sides.length,
          message: done && Object.values(outputs).some((row) => row?.source === "run" && row.status !== "passed")
            ? "Captured check results include a non-passing outcome. Inspect the reported status below." : "",
          outputs: { ...outputs } });
        if (done && outputs.old && outputs.new) recordEvidence(scope, { surfaceKey: key, title: describeSurface(surface), affectedPaths: surface.affected_paths, old: outputs.old, next: outputs.new });
      } catch (error: unknown) {
        if (!isCurrent()) return;
        put({ key, stage: "failed", step: i, total: sides.length, failedSide: side,
          message: String(error instanceof Error ? error.message : error), outputs: { ...outputs } });
        return;
      }
    }
  };
  const openCode = (path: string) => {
    const target = targets.find((t) => t.path === path);
    if (target) goTo({ kind: "all", part: partSlug(target, targets) });
  };
  const onListKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("[data-surface-key]")];
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = buttons[at + step];
    if (at < 0 || !next) return;
    event.preventDefault();
    next.focus();
    const row = rows.find((r) => r.key === next.dataset.surfaceKey);
    if (row) openSurface(row.surface, true);
  };
  const onTabKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    const at = TABS.indexOf(tab);
    const next = event.key === "ArrowRight" ? (at + 1) % TABS.length : event.key === "ArrowLeft" ? (at + TABS.length - 1) % TABS.length
      : event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    chooseTab(TABS[next]);
    document.getElementById(`pv-v-${TABS[next]}`)?.focus();
  };
  const chooseTab = (next: PreviewTab) => {
    setTab(next);
    try { window.localStorage.setItem(TAB_STORAGE, next); } catch { /* The current tab still changes. */ }
  };

  const hasResults = Boolean(exactOld || exactNew);
  const name = current ? describeSurface(current) : "";
  const rowNote = (row: SurfaceRow) => {
    if (row.magnitude !== null) return `Compared here · ${row.magnitude} changed ${row.magnitude === 1 ? "line" : "lines"}`;
    return row.reached ? "Relates to changed code · not compared" : "Link to changed code not verified";
  };
  const failureHeading = currentRun.failedSide ? `Couldn't start the ${currentRun.failedSide} version: ${firstLine(currentRun.message)}` : firstLine(currentRun.message);
  const failureAlert = currentRun.failedSide === "new" && exactOld ? "The old version ran fine, so nothing was compared." : "Nothing was compared.";
  const runningHeading = `Running the ${currentRun.only ? `${currentRun.only} version` : "old and new versions"}… ${currentRun.step} of ${currentRun.total}`;
  // The single live region of the screen (SPEC 5.3): a state change after the screen is on show, spoken once. It is empty
  // on arrival (A11Y-3): the heading that takes focus already says what the screen holds, so listing surfaces or "not
  // set up" here would read the same thing twice.
  const announce = discovery.error ? "Couldn't read configured preview surfaces."
    : discovery.loading || notSetUp ? ""
      : currentRun.stage === "running" ? runningHeading
        : currentRun.stage === "ready" ? "Previews ready"
          : currentRun.stage === "failed" ? `${failureHeading}. ${failureAlert}`
            : currentRun.stage === "cancelled" ? "Cancelled. Nothing was kept."
              : "";
  // The one Run button (review-17.1); a check keeps "Run test" so it never reads as already run (D8).
  const runLabel = currentRun.stage === "failed" ? "Try again" : currentRun.stage === "cancelled" ? "Run again" : isCheck ? "Run test" : "Run";

  const counter = counterFor(targets, "", complete);

  return <section className="flex min-w-0 flex-1 flex-col" data-testid="flow-preview-screen">
    <p className="sr-only" role="status" aria-live="polite" data-testid="preview-live">{announce}</p>
<FlowTopBar
      repo={repoNameOf(overview)}
      documentTitle="Preview"
      title={overview.title}
      revisionNumber={overview.revision.revision_number}
      back={{ text: "Overview", onClick: () => goTo({ kind: "overview" }) }}
      counter={counter}
      status={counter ? undefined : { text: "Loading all parts…" }}
      onAsk={onAskChange}
      onShortcuts={() => setShortcutsOpen(true)}
    />
    <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={screenLegend("preview", { aiAsk: false })} />
    <ReviewViewTabs reviewRef={reviewRef} current="preview" previewCount={discovery.loading || discovery.error || notSetUp ? null : reached.length}/>
    <div role="region" className="min-w-0 flex-1" data-review-scroll-root="preview" aria-label="Preview">
      <div className="mx-auto max-w-[1480px] space-y-5 p-4 pb-16 sm:p-6" data-testid="preview-content">
        <div>
          <p className="font-mono text-tag tracking-wide text-muted-foreground">UNDERSTAND · OUTPUT & RELATED SURFACES</p>
          <h1 ref={heading} tabIndex={-1} className="mt-1 text-title font-semibold outline-none">Preview</h1>
          <p className="mt-1 max-w-3xl text-label text-muted-foreground">See what this change relates to. Capturing the output before and after is optional and always started by you. Seeing a preview never marks a part reviewed.</p>
        </div>
        {discovery.loading && <p className="rounded-xl border border-border bg-card p-6 text-label">Finding configured surfaces for this captured revision…</p>}
        {discovery.error && <div className="rounded-xl border border-p1-line p-5 text-label">
          <strong>Couldn't read configured preview surfaces.</strong><p className="mt-2 break-words text-muted-foreground">{discovery.error}</p><p>Your code review is unchanged.</p>
        </div>}
        {notSetUp && <div className="rounded-xl border border-border bg-card p-6" data-testid="preview-not-set-up">
          <h2 ref={notSetUpHeading} tabIndex={-1} className="text-heading font-semibold outline-none">Previews aren't set up for this repo.</h2>
          <p className="mt-2 text-label text-muted-foreground" data-testid="preview-found">What LemonCrow found: no page, request, component, table or check it can preview for this revision.</p>
          <p className="mt-2 text-label text-muted-foreground">Until then, use the Diff and Map tabs. Nothing else in the review changes.</p>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button size="sm" variant="solid" aria-expanded={setupOpen} onClick={() => setSetupOpen((v) => !v)}>Set up previews</Button>
            <Button size="sm" variant="ghost" aria-expanded={learnOpen} onClick={() => setLearnOpen((v) => !v)}>Learn what can be previewed</Button>
          </div>
          {setupOpen && <div className="mt-3 flex flex-wrap items-center gap-2" data-testid="preview-setup">
            <code className="rounded border border-border bg-muted px-3 py-2 font-mono text-label">{SETUP_COMMAND}</code>
            <Button size="sm" variant="outline" onClick={() => { void navigator.clipboard.writeText(SETUP_COMMAND).then(() => setCopied(true)).catch(() => setCopied(false)); }}>
              <Clipboard size={16} aria-hidden="true"/> {copied ? "Copied" : "Copy command"}
            </Button>
          </div>}
          {learnOpen && <p className="mt-3 text-label text-muted-foreground" data-testid="preview-learn">Pages from a Vite app, requests from a Bruno collection, component stories, tables and test or service checks. They are listed from the repository's review settings and nothing runs until you press a button.</p>}
        </div>}
        {discovery.issues.length > 0 && <div className="rounded-xl border border-p1-line p-4 text-label">
          <strong>Some preview plugins are unavailable.</strong>
          <ul className="mt-2 space-y-1 text-muted-foreground">{discovery.issues.map((issue, i) => <li key={`${issue.plugin}:${i}`}>{issue.plugin} · {issue.reason}</li>)}</ul>
        </div>}
        {rows.length > 0 && <>
          <div className="rounded-xl border border-border bg-card px-4 py-3">
            <h2 className="text-heading font-semibold">Surfaces · ranked by change</h2>
            <p className="mt-1 text-meta text-muted-foreground">
              Only what this change reaches, ranked by how much before and after differ once you run a surface.
              {rows.length - reached.length > 0 ? ` ${rows.length - reached.length} listed without a verified link to changed code.` : ""}
            </p>
          </div>
          <div className={`grid min-w-0 gap-4 ${full ? "" : "lg:grid-cols-[minmax(260px,0.8fr)_minmax(0,1.5fr)]"}`}>
            {!full && <aside className="min-w-0 rounded-xl border border-border bg-card" aria-label="Preview surfaces" data-testid="preview-surface-list" onKeyDown={onListKeyDown}>
              <div className="flex items-center justify-between border-b border-border px-3 py-3">
                <h2 className="text-label font-semibold">Surfaces</h2>
                <Button size="sm" variant="ghost" className="lg:hidden" onClick={() => setCompactListOpen((v) => !v)}
                  aria-expanded={compactListOpen}>{compactListOpen ? "Collapse" : "Show list"}</Button>
              </div>
              <div className={compactListOpen || !selected ? "" : "hidden lg:block"}>
                {GROUPS.map((group) => {
                  const items = rows.filter((row) => row.group === group);
                  if (!items.length) return null;
                  return <section key={group} aria-label={group + " surfaces"}>
                    <h3 className="border-b border-border bg-muted/30 px-4 py-2 font-mono text-tag text-muted-foreground">{group} · {items.length}</h3>
                    <ul>{items.map((row) => <li key={row.key}>
                      <Button unstyled data-testid="preview-surface-row" data-surface-key={row.key} type="button" aria-current={selected?.key === row.key ? "true" : undefined}
                        className={`flex w-full min-h-14 items-center gap-3 border-b border-border/70 px-3 py-3 text-left hover:bg-muted/60 ${selected?.key === row.key ? "bg-accent" : ""}`}
                        onClick={() => openSurface(row.surface)}>
                        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-muted"><FileCode2 size={16} aria-hidden="true"/></span>
                        <span className="min-w-0 flex-1"><span className="block truncate text-label font-semibold" title={row.surface.title}>{row.surface.title}</span>
                          <span className={`block truncate text-meta ${selected?.key === row.key ? "text-foreground" : "text-muted-foreground"}`}>{providerName(row.surface.provider)} · {rowNote(row)}</span>
                        </span>
                        <ChevronRight size={16} className="shrink-0 text-muted-foreground" aria-hidden="true"/>
                      </Button>
                    </li>)}</ul>
                  </section>;
                })}
              </div>
            </aside>}
            <div className="min-w-0" data-testid="preview-viewer">
              {!current && <div className="rounded-xl border border-border bg-card p-8" data-testid="preview-select-surface">
                <h2 className="text-heading font-semibold">Choose a captured surface</h2>
                <p className="mt-2 text-label text-muted-foreground">Select a page, request, component, table, or check to see its available output and provenance. No runtime starts until you choose to run it.</p>
              </div>}
              {current && <div className="space-y-4">
                <div className="rounded-xl border border-border bg-card p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-meta text-muted-foreground">{surfaceGroup(current)} · {providerName(current.provider)}</p>
                      <h2 className="mt-1 break-words text-heading font-semibold" data-testid="preview-surface-title">{name}</h2>
                      <p className="mt-1 break-all font-mono text-tag tracking-normal text-muted-foreground">{plainLocator(current.locator) || current.id}</p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      {isCheck && <Badge tone="neutral">Check · not run automatically</Badge>}
                      <Button variant="outline" size="sm" aria-pressed={full} data-testid="preview-full-toggle" onClick={() => setFull((v) => !v)}>
                        {full ? <><Minimize2 size={16} aria-hidden="true"/> Back to the list</> : <><Maximize2 size={16} aria-hidden="true"/> Open full size</>}
                      </Button>
                    </div>
                  </div>
                  <p className="mt-3 text-label text-muted-foreground">{previewMeaning(current)}</p>
                  <div className="mt-3 text-label" data-testid="preview-relates">
                    <span className="font-medium">Relates to</span>
                    {reachedPaths(current, targets).length > 0
                      ? <div className="mt-2 flex flex-wrap gap-2">{reachedPaths(current, targets).map((path) => <Button key={path} variant="outline" size="sm"
                        onClick={() => openCode(path)}><Code2 size={16} aria-hidden="true"/>{path}</Button>)}</div>
                      : <span className="text-muted-foreground"> · the surface discovery did not establish a link to changed code.</span>}
                    {pathsOutsideChange(current, targets).length > 0 && <p className="mt-1 text-meta text-muted-foreground" data-testid="preview-outside-change">
                      Also lists {pathsOutsideChange(current, targets).length === 1 ? "1 file" : `${pathsOutsideChange(current, targets).length} files`} this change does not touch.
                    </p>}
                  </div>
                  {(() => {
                    const missing = notChecked(current, exactOld, exactNew);
                    return missing.length > 0 ? <div className="mt-3 text-label" data-testid="preview-not-checked">
                      <span className="font-medium">Not checked</span>
                      <ul className="mt-1 list-disc pl-5 text-muted-foreground">{missing.map((line) => <li key={line}>{line}</li>)}</ul>
                    </div> : null;
                  })()}
                </div>
                <div className="min-w-0 rounded-xl border border-border bg-card p-3 sm:p-4" data-testid="preview-output-card">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h2 className="text-heading font-semibold">Before and after</h2>
                      <p className="mt-1 text-meta text-muted-foreground">Each side is rendered from the frozen revision in the configured runtime, not from your current checkout.</p>
                    </div>
                    {canRender && currentRun.stage !== "running" && <Button size="sm" onClick={() => void execute()} data-testid="preview-execute">
                      <Play size={16} aria-hidden="true"/>{runLabel}
                    </Button>}
                    {currentRun.stage === "running" && <Button variant="outline" size="sm" onClick={cancelRun} data-testid="preview-cancel"><X size={16} aria-hidden="true"/> Cancel</Button>}
                  </div>
                  {!canRender && <p className="mt-3 text-label text-muted-foreground">This provider has no available runner for the captured revision.</p>}
                  {currentRun.stage !== "idle" && <div className="mt-3 space-y-2 text-label" data-testid="preview-operation" data-stage={currentRun.stage}>
                    {currentRun.stage === "running" && <>
                      <h3 ref={stateHeading} tabIndex={-1} className="text-label font-semibold outline-none" data-pv-h>{runningHeading}</h3>
                      <div className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true"><div className="h-full bg-primary" style={{ width: `${Math.max(0, currentRun.step / Math.max(1, currentRun.total)) * 100}%` }}/></div>
                      <p className="flex items-center gap-2 text-meta text-muted-foreground"><LoaderCircle size={14} className="animate-spin" aria-hidden="true"/>Runs in a sandbox, not in your working tree.</p>
                    </>}
                    {currentRun.stage === "ready" && <>
                      <h3 ref={stateHeading} tabIndex={-1} className="text-label font-semibold outline-none" data-pv-h>Previews ready</h3>
                      {currentRun.message && <p>{currentRun.message}</p>}
                    </>}
                    {currentRun.stage === "failed" && <>
                      <h3 ref={stateHeading} tabIndex={-1} className="break-words text-label font-semibold outline-none" data-pv-h>{failureHeading}</h3>
                      <p className="rounded border border-p0-line bg-p0-bg p-3">{failureAlert}</p>
                      <Button variant="outline" size="sm" aria-expanded={logOpen} onClick={() => setLogOpen((v) => !v)}>{logOpen ? "Hide log" : "Show log"}</Button>
                      {logOpen && <pre className="max-h-44 max-w-full overflow-auto whitespace-pre rounded border border-border bg-muted p-3 font-mono text-meta" tabIndex={0} aria-label="Run log">{currentRun.message}</pre>}
                    </>}
                    {currentRun.stage === "cancelled" && <>
                      <h3 ref={stateHeading} tabIndex={-1} className="text-label font-semibold outline-none" data-pv-h>Cancelled. Nothing was kept.</h3>
                      <p className="text-meta text-muted-foreground">The sandbox may still finish what it started. Nothing from it is shown.</p>
                    </>}
                  </div>}
                  {/* The sides are drawn once a run produced one (review-17.1): before that, Run is the only control. */}
                  {hasResults && <>
                    <div role="tablist" aria-label="Preview side" className="mt-4 flex w-full flex-wrap items-center gap-1 border-b border-border pb-3" onKeyDown={onTabKeyDown}>
                      {TABS.map((choice) => <Button key={choice} id={`pv-v-${choice}`} role="tab" aria-selected={tab === choice} aria-controls="pv-vpanel"
                        tabIndex={tab === choice ? 0 : -1} variant={tab === choice ? "solid" : "ghost"} size="sm"
                        onClick={() => chooseTab(choice)} className="max-sm:flex-1">{TAB_NAME[choice]}</Button>)}
                    </div>
                    <div className="mt-3 min-w-0" id="pv-vpanel" role="tabpanel" aria-labelledby={`pv-v-${tab}`} data-testid="preview-result-pane">
                      {tab === "changes" ? <Difference old={exactOld} newer={exactNew}/> : <PreviewPane result={tab === "old" ? exactOld : exactNew} name={TAB_NAME[tab]}/>}
                    </div>
                    <p className="mt-3 text-meta text-muted-foreground" data-testid="preview-provenance">
                      {provenanceText([exactOld, exactNew], overview.revision)}.
                      {currentRun.stage === "ready" ? "" : " Partial results only, not a complete comparison."}
                      {" "}Checks without a recorded run are not presented as passing.
                    </p>
                  </>}
                </div>
              </div>}
            </div>
          </div>
        </>}
      </div>
    </div>
    <FlowAskOverlay>{askPanel}</FlowAskOverlay>
  </section>;
}
