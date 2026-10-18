import { useCallback, useEffect, useRef, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "../components/ui/sheet";
import {
  enterInteractivePreview,
  fetchInteractivePreview,
  startInteractivePreview,
  stopInteractivePreview,
  type InteractivePreviewOffer,
  type InteractivePreviewSession,
  type InteractivePreviewSide,
} from "./interactivePreviewApi";

const SIDES: readonly { side: InteractivePreviewSide; label: string }[] = [
  { side: "old", label: "Base" },
  { side: "new", label: "Head" },
];

/**
 * Each side is a separate origin (its own loopback port locally, its own host
 * under a dedicated preview domain when hosted). `allow-same-origin` lets the
 * frame keep *its own* origin so its session cookie works; it never grants the
 * Reader's origin. No top navigation, popups or downloads.
 */
export const PREVIEW_FRAME_SANDBOX =
  "allow-scripts allow-forms allow-same-origin";

function clock(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
}

interface LivePreview {
  session: InteractivePreviewSession;
  urls: Partial<Record<InteractivePreviewSide, string>>;
  deadline: number;
}

function SideBySide({
  live,
  stopping,
  onStop,
}: {
  live: LivePreview;
  stopping: boolean;
  onStop: () => void;
}) {
  const [left, setLeft] = useState(() =>
    Math.max(0, (live.deadline - Date.now()) / 1000)
  );
  useEffect(() => {
    const timer = window.setInterval(
      () => setLeft(Math.max(0, (live.deadline - Date.now()) / 1000)),
      1000
    );
    return () => window.clearInterval(timer);
  }, [live.deadline]);
  const ended = left <= 0;
  return (
    <Sheet
      open
      onOpenChange={(open) => {
        if (!open) onStop();
      }}
    >
      <SheetContent
        side="bottom"
        className="h-[85dvh]"
        data-testid="interactive-preview-frame"
      >
        <SheetHeader className="flex-row items-center gap-3">
          <SheetTitle>Interactive preview</SheetTitle>
          <SheetDescription className="min-w-0 flex-1 truncate">
            Base and head side by side · a human observation, recorded as
            evidence, never a check result
          </SheetDescription>
          <span
            className="font-mono text-caption text-muted-foreground"
            data-testid="interactive-preview-remaining"
          >
            {ended ? "Time limit reached" : `${clock(left)} left`}
          </span>
          <UiButton
            type="button"
            size="compact"
            variant="outline"
            disabled={stopping}
            onClick={onStop}
          >
            Stop preview
          </UiButton>
        </SheetHeader>
        {live.session.limited_by_budget && (
          <div className="px-4 text-micro text-amber-300/80">
            Limited by budget: the time limit was shortened to fit the remaining
            budget.
          </div>
        )}
        {!live.session.comparable && (
          <div
            className="px-4 text-micro text-amber-300/80"
            data-testid="interactive-preview-not-comparable"
          >
            Not comparable:{" "}
            {live.session.comparability ||
              "base and head ran in different environments"}
          </div>
        )}
        <div className="grid min-h-0 flex-1 grid-cols-2 gap-2 p-3">
          {SIDES.map(({ side, label }) => (
            <figure
              key={side}
              className="flex min-h-0 flex-col rounded-md border border-border/70"
            >
              <figcaption className="border-b border-border/60 px-2 py-1 text-caption text-neutral-300">
                {label}
              </figcaption>
              {ended || !live.urls[side] ? (
                <div className="flex flex-1 items-center justify-center p-4 text-caption text-muted-foreground">
                  {ended ? "This preview has ended." : "Opening…"}
                </div>
              ) : (
                <iframe
                  title={`${label} preview`}
                  src={live.urls[side]}
                  sandbox={PREVIEW_FRAME_SANDBOX}
                  referrerPolicy="no-referrer"
                  allow=""
                  className="min-h-0 w-full flex-1 bg-white"
                />
              )}
            </figure>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}

/**
 * Interactive preview entry point. Mounted inside the Brief's Behavior changes
 * section and the Checks rail; renders nothing unless the trusted base has a
 * previewable app. Not a new view or control: the frame is a transient sheet.
 */
export default function InteractivePreviewPanel({
  reviewId,
  revisionId = "",
  readOnly = false,
  openedFrom,
}: {
  reviewId: string;
  revisionId?: string;
  readOnly?: boolean;
  openedFrom: "behavior_changes" | "checks_rail";
}) {
  const [offer, setOffer] = useState<InteractivePreviewOffer | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [live, setLive] = useState<LivePreview | null>(null);
  // The one-time session token stays in memory only: never storage, never a URL.
  const token = useRef("");

  const load = useCallback(
    () =>
      fetchInteractivePreview(reviewId).then(setOffer, () => setOffer(null)),
    [reviewId]
  );

  useEffect(() => {
    let cancelled = false;
    fetchInteractivePreview(reviewId)
      .then((next) => {
        if (!cancelled) setOffer(next);
      })
      .catch(() => {
        if (!cancelled) setOffer(null);
      });
    return () => {
      cancelled = true;
    };
  }, [reviewId, revisionId]);

  if (!offer || offer.state === "not_offered") return null;
  const current = offer;
  const editable =
    !readOnly && (!revisionId || revisionId === current.revision_id);
  const running = current.sessions.find(
    (row) => row.state === "running" || row.state === "starting"
  );

  async function open() {
    setBusy(true);
    setMessage("");
    try {
      const started = await startInteractivePreview(
        reviewId,
        current.revision_id
      );
      token.current = started.token;
      const urls: Partial<Record<InteractivePreviewSide, string>> = {};
      for (const { side } of SIDES) {
        urls[side] = (
          await enterInteractivePreview(
            reviewId,
            started.session.id,
            started.token,
            side
          )
        ).url;
      }
      const seconds =
        started.session.remaining_seconds ?? started.session.ttl_seconds;
      setLive({
        session: started.session,
        urls,
        deadline: Date.now() + seconds * 1000,
      });
    } catch (reason) {
      setMessage(
        reason instanceof Error
          ? reason.message
          : "The preview could not start."
      );
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function stop(sessionId: string) {
    setBusy(true);
    try {
      await stopInteractivePreview(reviewId, sessionId, current.revision_id);
    } catch (reason) {
      setMessage(
        reason instanceof Error
          ? reason.message
          : "The preview could not be stopped."
      );
    } finally {
      token.current = "";
      setLive(null);
      setBusy(false);
      await load();
    }
  }

  const maxMinutes = Math.round(current.ttl.max_seconds / 60);
  const remaining = current.budget.remaining_minutes;
  return (
    <div
      className="rounded-md border border-border/60 px-2.5 py-2 text-caption"
      data-testid={`interactive-preview-${openedFrom}`}
    >
      <div className="flex items-center gap-2">
        <span className="font-medium text-neutral-200">
          Interactive preview
        </span>
        <span className="flex-1" />
        {current.state === "available" && editable && !running && (
          <UiButton
            type="button"
            size="compact"
            variant="outline"
            disabled={busy}
            onClick={() => void open()}
          >
            Open side by side
          </UiButton>
        )}
        {running && editable && !live && (
          <UiButton
            type="button"
            size="compact"
            variant="ghost"
            disabled={busy}
            onClick={() => void stop(running.id)}
          >
            Stop running preview
          </UiButton>
        )}
      </div>
      {current.state === "available" && (
        <div className="mt-0.5 text-micro text-muted-foreground">
          Base and head side by side for up to {maxMinutes} min ·{" "}
          {current.budget.minutes_per_minute} compute min per minute
          {remaining !== null &&
            ` · ${Math.max(0, remaining).toFixed(0)} min left in budget`}
          {current.executor_class &&
            ` · ${current.executor_class.replace("_", " ")}`}
        </div>
      )}
      {current.state === "limited_by_budget" && (
        <div
          className="mt-0.5 text-micro text-amber-300/80"
          data-testid="interactive-preview-limited"
        >
          {current.reason.startsWith("Limited by budget")
            ? current.reason
            : `Limited by budget: ${current.reason}`}
        </div>
      )}
      {current.state === "not_available" && (
        <div
          className="mt-0.5 text-micro text-muted-foreground"
          data-testid="interactive-preview-not-available"
        >
          Not available: {current.reason}
        </div>
      )}
      {running && !live && (
        <div className="mt-0.5 text-micro text-muted-foreground">
          A preview of yours is running ({clock(running.remaining_seconds ?? 0)}{" "}
          left); opening a new one replaces it.
        </div>
      )}
      {message && (
        <div className="mt-0.5 text-micro text-neutral-300" role="status">
          {message}
        </div>
      )}
      {live && (
        <SideBySide
          live={live}
          stopping={busy}
          onStop={() => void stop(live.session.id)}
        />
      )}
    </div>
  );
}
