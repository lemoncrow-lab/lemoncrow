import { useEffect, useRef, type ReactNode } from "react";
import { ArrowLeft, Ellipsis, Search, Settings2, Sparkles } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle, isDialogOpen } from "../components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "../components/ui/dropdown-menu";
import { useAiGate } from "./aiBudget";
import { useFlowDocumentTitle } from "./flowDocumentTitle";
import { allShortcuts, classifyFocus, flowAction, type KeyBindings } from "./flowKeymap";
import type { Counter, CounterBar, Pip } from "./focus/focusModel";
import type { ReadingView } from "./overview/defaultView";
import { repoFolderName } from "./ReviewIdentityBar";
import { usePaletteOpener } from "./palette/FlowPalette";
import { revisionName } from "./revisionLabel";
import type { ReviewOverview } from "./types";

/**
 * The shared top bar (SPEC 3.3, 3.4): one 52 px bar. Left: labelled back control, "shop ·", the title, the revision
 * chip and, on desktop where allowed, the reading-view switch. Centre: one pip per decision with "n of N decisions" and the
 * quick checks' own small "quick checks n/N" (from the
 * complete parts list only: pass `counter` null while the list is a startup slice; Welcome back and Approved pass the
 * same counter, review-0.3), or a plain `status` text such as "Sent · Revision 1" on Done. Right: Shortcuts, Ask (only when `onAsk` is given, i.e. AI Ask is on), a phone "…" menu on
 * view-switch screens (`moreItems`), the settings gear and, on a phone only, "Search and commands" (the palette has no key there, SPEC 4.12 PAL-4). No other buttons, no lemon.
 *
 * A screen passes: `back` (visible text; the aria-label is "<text>, go back"), `view` on Overview, One at a time,
 * Quick check and All files, and on Map, Brief, X-Ray and Wrap up with no side pressed (`useReadingViewSwitch`,
 * review-0.4), `onShortcuts` (it owns the `FlowShortcutsDialog` state), and `moreItems` for the phone menu.
 * View tabs ("Diff | Map | Preview", `ReviewViewTabs`) render below the bar on All files, Map and Preview; the bar never takes tabs.
 * The top-bar Ask is named "Ask about the change"; a card's own Ask stays "Ask about this part".
 */
export interface FlowTopBarProps {
  /** The checkout folder name shown before the title, e.g. "shop ·"; empty hides it (see `repoNameOf`). */
  repo: string;
  /** The screen's name for the document title: "<documentTitle> · <repo> · LemonCrow" (SPEC 5.3). */
  documentTitle: string;
  title: string;
  revisionNumber: number;
  back: { text: string; onClick: () => void };
  /** The reading-view switch (desktop). `current` is null on a screen that is neither view (Map, Brief, X-Ray, Wrap up). */
  view?: { current: ReadingView | null; onChange: (view: ReadingView) => void };
  counter?: Counter | null;
  /** Centre text for a screen without pips (Done; "Loading all parts…" while the list is a startup slice); `tone` colours it. */
  status?: { text: string; tone?: "neutral" | "success" | "info" };
  onAsk?: () => void;
  /** True only on screens where Q opens Ask (SPEC 3.5); the button then announces the key. */
  askKey?: boolean;
  /** False when the screen's own phone dock already offers Ask: a phone then shows one Ask, not two. */
  askOnPhone?: boolean;
  onShortcuts: () => void;
  /** Phone "…" menu entries on view-switch screens. */
  moreItems?: readonly { label: string; onSelect: () => void }[];
}

/** The checkout folder's name from the trusted workspace path; empty when the server holds none (the bar then shows no repo). */
export function repoNameOf(overview: Pick<ReviewOverview, "workspace_root">): string {
  return repoFolderName(overview.workspace_root ?? "");
}

function pipClass(pip: Pip): string {
  const size = pip.quick ? "h-2 w-2" : "h-3 w-3";
  const gap = pip.quick ? "ml-1" : "";
  const look = pip.state === "good" ? "border-ok-solid bg-ok-solid"
    : pip.state === "change" ? "border-p1-solid bg-p1-solid"
      : pip.state === "later" ? "border-dashed border-muted-foreground"
        : pip.quick ? "border-border bg-muted" : "border-muted-foreground";
  return `inline-block rounded-full border-2 ${size} ${gap} ${look} ${pip.current ? "outline outline-2 outline-offset-2 outline-foreground" : ""}`;
}

// Neutral by kind: an unanswered decision is not an alarm. Red is for "needs a change" only, which the bar does not show.
const SEGMENT_TONE = { decision: "bg-foreground", quick_check: "bg-muted-foreground", accepted_by_rule: "bg-border" } as const;

/**
 * Above 12 parts (SPEC 4.15, SCALE-1): a thin bar of decisions (dark), quick checks (mid) and parts accepted by rule (light).
 * It is focusable; its accessible text is the exact counts, shown on hover and on focus (D24).
 */
function SegmentedBar({ bar }: { bar: CounterBar }) {
  return (
    <span role="img" tabIndex={0} aria-label={bar.label} className="group relative inline-flex items-center rounded-sm px-1" data-testid="flow-top-bar-segments">
      <span className="flex h-1.5 w-16 overflow-hidden rounded-full sm:w-28" aria-hidden="true">
        {bar.segments.filter((segment) => segment.count > 0).map((segment) => (
          <span key={segment.kind} data-segment={segment.kind} data-count={segment.count} className={SEGMENT_TONE[segment.kind]} style={{ flexGrow: segment.count, flexBasis: 0, minWidth: 2 }} />
        ))}
      </span>
      <span aria-hidden="true" data-testid="flow-top-bar-segments-tip" className="pointer-events-none absolute left-1/2 top-full z-20 mt-1 hidden -translate-x-1/2 whitespace-nowrap rounded border border-border bg-popover px-2 py-1 text-meta text-popover-foreground shadow-float group-hover:block group-focus:block">{bar.label}</span>
    </span>
  );
}

const STATUS_TONE = { neutral: "text-muted-foreground", success: "text-ok", info: "text-p2" } as const;

/**
 * Flow screens scroll the window under the sticky 52px top bar. A side rail stays in view beside the page and scrolls
 * on its own; it never lengthens the page.
 */
export const FLOW_RAIL_CLASS = "sticky top-[52px] h-[calc(100dvh-52px)] self-start overflow-y-auto";

/** Ask on a screen without a side rail: below the top bar, the whole width on a phone and a 400px right-hand panel from md up
 * (the width of the Ask rail on Overview, Brief and X-Ray), so the page stays in view beside it. */
export function FlowAskOverlay({ children }: { children: ReactNode }) {
  if (!children) return null;
  return <div data-review-rail="ask" className="fixed bottom-0 right-0 top-[52px] z-30 w-full bg-background md:w-[400px] md:border-l md:border-border md:shadow-float">{children}</div>;
}

export function FlowTopBar(props: FlowTopBarProps) {
  const navigate = useNavigate();
  const openPalette = usePaletteOpener();
  const ai = useAiGate();
  const { counter } = props;
  useFlowDocumentTitle(props.documentTitle, props.repo);
  return (
    <header className="sticky top-0 z-30 flex h-[52px] shrink-0 items-center gap-1 border-b border-border bg-background px-2 sm:gap-2 sm:px-3" data-testid="flow-top-bar">
      {/* flex-auto, not flex-1: the left group keeps its content width before the action group grows, so the view switch never runs under the counter. */}
      <div className="flex min-w-0 flex-auto items-center gap-2">
        {/* Phone: the arrow alone (its name stays "<text>, go back"), so the counter and the actions never overlap. */}
        <Button variant="ghost" size="sm" className="shrink-0 max-sm:px-2" aria-label={`${props.back.text}, go back`} onClick={props.back.onClick}>
          <ArrowLeft size={16} aria-hidden="true" />
          <span className="max-sm:hidden">{props.back.text}</span>
        </Button>
        {props.repo ? <>
          <span className="hidden max-w-[10rem] shrink-0 truncate font-semibold sm:inline" title={props.repo} data-testid="flow-top-bar-repo">{props.repo}</span>
          <span className="hidden shrink-0 text-muted-foreground sm:inline" aria-hidden="true">·</span>
        </> : null}
        <span className="min-w-0 truncate text-label text-foreground max-sm:sr-only" title={props.title} data-testid="flow-top-bar-title">{props.title}</span>
        <span className="shrink-0 max-sm:hidden"><Badge tone={props.revisionNumber >= 1 ? "info" : "neutral"}>{revisionName(props.revisionNumber)}</Badge></span>
        {props.view ? (
          <div role="group" aria-label="Reading view" className="ml-2 hidden shrink-0 items-center rounded-md border border-border p-0.5 md:inline-flex">
            {(["one", "all"] as const).map((value) => (
              <Button key={value} variant="ghost" size="xs" aria-pressed={props.view?.current === value} className={props.view?.current === value ? "bg-accent text-foreground" : ""} onClick={() => { if (props.view?.current !== value) props.view?.onChange(value); }}>
                {value === "one" ? "One at a time" : "All files"}
              </Button>
            ))}
          </div>
        ) : null}
      </div>
      <div className="flex items-center gap-2 max-sm:min-w-0 max-sm:shrink sm:shrink-0">
        {counter?.pips ? (
          <span role="img" aria-label={counter.label} className="hidden items-center gap-1 sm:inline-flex" data-testid="flow-top-bar-pips">
            {counter.pips.map((pip) => <span key={pip.id} className={pipClass(pip)} />)}
          </span>
        ) : null}
        {counter?.bar ? <SegmentedBar bar={counter.bar} /> : null}
        {counter ? <span className="min-w-0 truncate whitespace-nowrap text-meta text-muted-foreground" title={counter.label} data-testid="flow-top-bar-counter">{counter.text}</span> : props.status ? <span className={`text-meta ${STATUS_TONE[props.status.tone ?? "neutral"]}`}>{props.status.text}</span> : null}
        {counter?.quick ? <span className="hidden whitespace-nowrap text-tag text-muted-foreground min-[1600px]:inline" data-testid="flow-top-bar-quick">{counter.quick.text}</span> : null}
      </div>
      <div className="flex shrink-0 items-center justify-end sm:gap-1">
        {openPalette ? (
          <Button variant="ghost" size="icon" className="min-w-11 sm:hidden" aria-label="Search and commands" aria-haspopup="dialog" onClick={() => openPalette()}>
            <Search size={16} aria-hidden="true" />
          </Button>
        ) : null}
        <Button variant="ghost" size="sm" className="hidden sm:inline-flex" aria-haspopup="dialog" aria-keyshortcuts="?" onClick={props.onShortcuts}>
          Shortcuts
          <kbd className="rounded border border-border px-1 font-mono text-tag">?</kbd>
        </Button>
        {props.onAsk ? (
          <Button variant="ghost" size="sm" className={props.askOnPhone === false ? "max-sm:hidden" : ""} aria-label="Ask about the change" aria-keyshortcuts={props.askKey ? "Q" : undefined} {...ai.attrs} onClick={ai.run(props.onAsk)}>
            <Sparkles size={16} aria-hidden="true" className="text-ai" />
            <span className="max-sm:hidden">Ask</span>
          </Button>
        ) : null}
        {props.moreItems?.length ? (
          <div className="md:hidden">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon" aria-label="More"><Ellipsis size={16} aria-hidden="true" /></Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {props.moreItems.map((item) => <DropdownMenuItem key={item.label} onSelect={item.onSelect}>{item.label}</DropdownMenuItem>)}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ) : null}
        <Button variant="ghost" size="icon" aria-label="Review settings" onClick={() => navigate("/settings/review")}><Settings2 size={16} aria-hidden="true" /></Button>
      </div>
    </header>
  );
}

/**
 * The "Keyboard shortcuts" dialog (`?` and the Shortcuts button). `legend` is the screen's key line, "A good · C change · …",
 * listed first as "On this screen"; every binding of the key map (`allShortcuts`) follows, grouped.
 */
export function FlowShortcutsDialog({ open, onOpenChange, legend, bindings }: { open: boolean; onOpenChange: (open: boolean) => void; legend: string; bindings?: KeyBindings }) {
  const here = legend.split(" · ").filter((line) => line && line !== "? all shortcuts");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent aria-describedby={undefined}>
        <DialogHeader><DialogTitle>Keyboard shortcuts</DialogTitle></DialogHeader>
        <DialogBody className="flex flex-col gap-4">
          <p className="text-meta text-muted-foreground">Shortcuts do not fire while you type in a field or while a dialog is open.</p>
          {here.length > 0 ? (
            <section aria-label="On this screen" data-testid="shortcuts-here">
              <h3 className="mb-1 text-meta font-semibold text-muted-foreground">On this screen</h3>
              <ul className="space-y-1 text-label">{here.map((line) => <li key={line}>{line}</li>)}</ul>
            </section>
          ) : null}
          {allShortcuts(bindings).map((group) => (
            <section key={group.title} aria-label={group.title} data-testid="shortcuts-group">
              <h3 className="mb-1 text-meta font-semibold text-muted-foreground">{group.title}</h3>
              <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-label">
                {group.keys.map((entry) => (
                  <div key={entry.key} className="contents">
                    <dt><kbd className="rounded border border-border px-1 font-mono text-tag">{entry.key}</kbd></dt>
                    <dd>{entry.does}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}

/** `?` opens the Shortcuts dialog on a screen with no keys of its own (Map, Preview). Never while typing or while a dialog is open. */
export function useShortcutsKey(onOpen: () => void) {
  const open = useRef(onOpen);
  open.current = onOpen;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const el = document.activeElement as HTMLElement | null;
      const action = flowAction(event, {
        screen: "other",
        surface: isDialogOpen() ? "other" : "none",
        focus: el ? classifyFocus({ tagName: el.tagName, isContentEditable: el.isContentEditable, role: el.getAttribute("role") }) : "other",
      });
      if (action?.intent === "help") { event.preventDefault(); open.current(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
