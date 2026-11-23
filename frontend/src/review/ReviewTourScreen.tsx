/**
 * The Walkthrough (`/r/<review>/walkthrough[/<step>][?view=map]`): the change one step at a time, as a list of steps
 * or played on the map (the former Tour and Change Story, one screen under one name). Steps open their part in Focus
 * (`/one/<part>`) and the flow Map's Dependencies lens; nothing here marks a part.
 */
import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";

import { Button } from "../components/ui/button";
import { ToggleGroup } from "../components/ui/toggle-group";
import { FLOW_RAIL_CLASS, FlowShortcutsDialog, FlowTopBar, repoNameOf, useShortcutsKey } from "./FlowTopBar";
import { counterFor } from "./focus/focusModel";
import { partSlug } from "./overview/overviewModel";
import { useFlowPaletteScreen } from "./palette/FlowPalette";
import { partEntries, reviewPlace } from "./palette/partEntries";
import { useReviewFlowNavigation } from "./useReviewFlowNavigation";
import type { WalkthroughView } from "./reviewRoutes";
import ReviewTour from "./ReviewTour";
import type { ChangeStoryEvidence } from "./changeStory/ChangeStory";
import { isChangedNode } from "./changeStory/changeStoryModel";
import { encodeXRayLocation, graphSnapshot, partForSelection } from "./xray/xrayModel";
import type { Annotation, EvidenceRow, ReviewArchitectureDelta, ReviewMapSelection, ReviewOverview, ReviewTarget, ReviewTourProjection } from "./types";

// The map view draws the map canvas; it loads only when the reviewer opens it.
const ChangeStory = lazy(() => import("./changeStory/ChangeStory"));

export interface ReviewTourScreenProps {
  reviewRef: string;
  apiReviewId: string;
  /** The step in the address (a tour step's semantic key); empty for `/walkthrough`. */
  step: string;
  /** Steps (the default) or On the map (`?view=map`). */
  view?: WalkthroughView;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  /** The whole parts list is loaded (the bar counts only from a complete list). */
  complete?: boolean;
  /** The tour of the revision on screen; null where the Review has no Brief capability or no plan yet. */
  tour: ReviewTourProjection | null;
  /** The plan is still loading, so the tour may yet arrive. */
  loading?: boolean;
  /** Refines the wording of steps that need it; absent when read-only or historical. */
  onSynthesize?: () => Promise<void>;
  /** The Reader's cached architecture read for this revision; absent where the map view is not offered. */
  loadStory?: () => Promise<ReviewArchitectureDelta | null>;
  /** Recorded checks on this revision; absent when the Reader has none to show. */
  checks?: readonly EvidenceRow[] | null;
  /** The Review's notes, as the map view's findings. */
  annotations?: readonly Annotation[];
  /** "Ask about this part"; absent when AI Ask is off. */
  onAsk?: (target: ReviewTarget) => void;
  /** "Ask about the change" (the top bar). */
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
}

type Story =
  | { state: "closed" }
  | { state: "loading" }
  | { state: "failed" }
  | { state: "empty" }
  | { state: "open"; delta: ReviewArchitectureDelta };

const LEGEND = "J / K move between steps · Enter open this part · Esc back to the Overview · ? all shortcuts";

export default function ReviewTourScreen(props: ReviewTourScreenProps) {
  const { reviewRef, apiReviewId, overview, targets, tour, loadStory, annotations, onAsk, onAskChange } = props;
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [story, setStory] = useState<Story>({ state: "closed" });
  // The view follows the address (Back, Forward, a link) and the switch; the map needs the Reader's architecture read.
  const [shown, setShown] = useState<WalkthroughView>(props.view ?? "steps");
  useEffect(() => setShown(props.view ?? "steps"), [props.view]);
  const [stepNow, setStepNow] = useState(props.step);
  useEffect(() => setStepNow(props.step), [props.step]);
  useShortcutsKey(() => setShortcutsOpen(true));
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    parts: partEntries(reviewRef, targets, "one"),
    place: reviewPlace(repoNameOf(overview), overview.title, "Walkthrough"),
    openShortcuts: () => setShortcutsOpen(true),
    context: { screen: "other", actions: {} },
  });

  const back = useCallback(() => goTo({ kind: "overview" }), [goTo]);
  const openPart = useCallback((target: ReviewTarget) => goTo({ kind: "one", part: partSlug(target, targets) }), [goTo, targets]);
  const openTarget = useCallback((targetId: string) => {
    const target = targets.find((item) => item.target_id === targetId);
    if (target) openPart(target);
  }, [targets, openPart]);
  const showOnMap = useCallback(() => goTo({ kind: "map", lens: "dependencies", view: "changes" }), [goTo]);
  const changeStep = useCallback((step: string) => {
    setStepNow(step);
    goTo({ kind: "walkthrough", step, view: "steps" });
  }, [goTo]);
  const changeView = useCallback((view: WalkthroughView) => {
    setShown(view);
    goTo({ kind: "walkthrough", step: stepNow, view });
  }, [goTo, stepNow]);
  const ask = useCallback((targetId: string | null) => {
    const target = targetId ? targets.find((item) => item.target_id === targetId) : undefined;
    if (target && onAsk) onAsk(target);
    else onAskChange?.();
  }, [targets, onAsk, onAskChange]);

  // Only the newest open request lands: closing, or opening again, drops an older read.
  const storyRequest = useRef(0);
  const loadStoryRef = useRef(loadStory);
  loadStoryRef.current = loadStory;
  const openStory = useCallback(() => {
    const load = loadStoryRef.current;
    if (!load) return;
    const request = ++storyRequest.current;
    setStory({ state: "loading" });
    load()
      .then((delta) => {
        if (request === storyRequest.current) setStory(delta && delta.graph.nodes.some(isChangedNode) ? { state: "open", delta } : { state: "empty" });
      })
      .catch(() => { if (request === storyRequest.current) setStory({ state: "failed" }); });
  }, []);
  const mapShown = shown === "map" && Boolean(loadStory);
  useEffect(() => {
    if (mapShown) openStory();
    else {
      storyRequest.current += 1;
      setStory({ state: "closed" });
    }
  }, [mapShown, openStory]);
  const closeStory = useCallback(() => changeView("steps"), [changeView]);
  const loadStoryEvidence = useCallback(async (): Promise<ChangeStoryEvidence> => ({ findings: annotations ? [...annotations] : null }), [annotations]);
  const openSource = useCallback((selection: ReviewMapSelection) => {
    // A file the change did not touch has no part: its place is on the Map.
    const target = partForSelection(targets, selection);
    if (target) openPart(target);
    else showOnMap();
  }, [targets, openPart, showOnMap]);

  const counter = counterFor(targets, "", props.complete !== false);
  const storyShown = story.state !== "closed";
  const viewSwitch = loadStory ? (
    <ToggleGroup
      aria-label="Walkthrough view"
      variant="segment"
      size="xs"
      options={[{ value: "steps", label: "Steps" }, { value: "map", label: "On the map" }]}
      value={mapShown ? "map" : "steps"}
      onChange={(value) => changeView(value === "map" ? "map" : "steps")}
    />
  ) : undefined;

  return (
    <>
      <FlowTopBar
        repo={repoNameOf(overview)}
        documentTitle="Walkthrough"
        title={overview.title || overview.session.title}
        revisionNumber={overview.revision.revision_number}
        back={{ text: "Overview", onClick: back }}
        counter={counter}
        status={counter ? undefined : { text: "Loading all parts…" }}
        onAsk={onAskChange}
        onShortcuts={() => setShortcutsOpen(true)}
      />
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={LEGEND} />
      <div className="flex flex-1" data-testid="flow-tour-screen">
        {/* The Change Story is a fixed-height canvas below the top bar; the walkthrough scrolls with the page. */}
        <div role="region" aria-label="Walkthrough" className={`min-w-0 flex-1 ${story.state === "open" ? "sticky top-[52px] flex h-[calc(100dvh-52px)] min-h-0 self-start" : ""}`} data-review-scroll-root="walkthrough">
          {story.state === "open" ? (
            <Suspense fallback={<p role="status" className="p-6 text-label text-muted-foreground">Opening the map…</p>}>
              <ChangeStory
                graph={story.delta.graph}
                flow={story.delta.flow}
                snapshotChecks={props.checks ?? null}
                targets={targets}
                loadEvidence={loadStoryEvidence}
                onOpenSource={openSource}
                onOpenXRay={(nodeId) => {
                  const node = story.delta.graph.nodes.find((item) => item.id === nodeId);
                  goTo({ kind: "xray", at: encodeXRayLocation({ side: node?.delta_status === "removed" ? "before" : "after", snapshot: graphSnapshot(story.delta.graph), rootId: nodeId }) });
                }}
                viewSwitch={viewSwitch}
                onClose={closeStory}
              />
            </Suspense>
          ) : storyShown ? (
            <div role="status" className="mx-auto flex max-w-3xl flex-col items-start gap-2 px-4 py-6 text-label text-muted-foreground" data-testid="tour-story-note">
              <span>
                {story.state === "loading" ? "Opening the map…"
                  : story.state === "empty" ? "This revision's architecture graph lists no changed file, so there is no change to walk through."
                    : "The change map could not load. Try again."}
              </span>
              <span className="flex gap-2">
                {story.state === "failed" ? <Button variant="outline" size="sm" onClick={openStory}>Try again</Button> : null}
                <Button variant="ghost" size="sm" onClick={closeStory}>Back to the steps</Button>
              </span>
            </div>
          ) : tour ? (
            <ReviewTour
              reviewId={apiReviewId}
              revisionId={overview.revision.id}
              tour={tour}
              targets={targets}
              activeCohortId={props.step || undefined}
              onCohortChange={changeStep}
              onBack={back}
              onOpenTarget={openTarget}
              onShowOnMap={showOnMap}
              onAsk={onAsk || onAskChange ? ask : undefined}
              onSynthesize={props.onSynthesize}
              viewSwitch={viewSwitch}
            />
          ) : (
            <div role="status" className="mx-auto flex max-w-3xl flex-col items-start gap-2 px-4 py-6 text-label text-muted-foreground" data-testid="tour-unavailable">
              <span>{props.loading ? "Preparing the walkthrough…" : "No walkthrough is available for this revision."}</span>
              {props.loading ? null : <Button variant="outline" size="sm" onClick={back}>Back to the Overview</Button>}
            </div>
          )}
        </div>
        {props.askPanel ? (
          <aside data-review-rail="ask" className={`review-secondary-rail ${FLOW_RAIL_CLASS} w-[min(32vw,400px)] shrink-0 border-l border-border bg-background`}>
            {props.askPanel}
          </aside>
        ) : null}
      </div>
    </>
  );
}
