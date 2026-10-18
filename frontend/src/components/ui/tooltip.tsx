import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import {
  forwardRef,
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ElementRef,
  type ReactElement,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { cn } from "../../lib/utils";
import { usePortalContainer } from "./portal-container";

const TOOLTIP_CONTENT_CLASS =
  "z-50 max-w-xs whitespace-pre-line break-words rounded-md border border-border bg-popover px-2 py-1 text-caption leading-4 text-popover-foreground shadow-md shadow-black/30";

export function TooltipProvider({
  delayDuration = 400,
  ...props
}: ComponentPropsWithoutRef<typeof TooltipPrimitive.Provider>) {
  return <TooltipPrimitive.Provider delayDuration={delayDuration} {...props} />;
}

/** Self-contained tooltip root (carries its own provider, as in shadcn v4). */
export function Tooltip(props: ComponentPropsWithoutRef<typeof TooltipPrimitive.Root>) {
  return (
    <TooltipProvider>
      <TooltipPrimitive.Root {...props} />
    </TooltipProvider>
  );
}

export const TooltipTrigger = TooltipPrimitive.Trigger;

// Escape presses a tooltip consumed (preventDefault) to dismiss itself.
const tooltipEscapes = new WeakSet<Event>();

/**
 * True when an Escape's default was prevented only to dismiss a tooltip. A
 * tooltip is a hint that owns no key, so a page-level Escape shortcut should
 * still act. (A tooltip opened by programmatic focus, or still fading out,
 * would otherwise swallow the first Escape.)
 */
export function escapeDismissedTooltip(event: Event): boolean {
  return tooltipEscapes.has(event);
}

export const TooltipContent = forwardRef<
  ElementRef<typeof TooltipPrimitive.Content>,
  ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 4, onEscapeKeyDown, ...props }, ref) => {
  const container = usePortalContainer();
  return (
    <TooltipPrimitive.Portal container={container}>
      <TooltipPrimitive.Content
        ref={ref}
        sideOffset={sideOffset}
        className={cn(
          TOOLTIP_CONTENT_CLASS,
          "animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95",
          className
        )}
        onEscapeKeyDown={(event) => {
          tooltipEscapes.add(event);
          onEscapeKeyDown?.(event);
        }}
        {...props}
      />
    </TooltipPrimitive.Portal>
  );
});
TooltipContent.displayName = "TooltipContent";

interface TipProps {
  /** Tooltip text; when empty the child renders untouched. */
  content?: ReactNode;
  side?: ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>["side"];
  align?: ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>["align"];
  /** A single element that can hold a ref and forward pointer/focus handlers. */
  children: ReactElement;
}

/** Replacement for a `title=` hint on any focusable element: `<Tip content="…"><a …/></Tip>`. */
export function Tip({ content, side, align, children }: TipProps) {
  if (content === undefined || content === null || content === false || content === "") return children;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side={side} align={align}>
        {content}
      </TooltipContent>
    </Tooltip>
  );
}

const NATIVE_HINT_ATTRIBUTE = "data-lc-title-hint";

interface NativeTitleTooltipBridgeProps {
  /** Matches the standard LemonCrow tooltip delay. Tests may set this to zero. */
  delayDuration?: number;
}

interface NativeHintState {
  target: Element;
  content: string;
  left: number;
  top: number;
  side: "top" | "bottom";
}

function eligibleNativeHintTarget(target: EventTarget | null): Element | null {
  if (!(target instanceof Element)) return null;
  const element = target.closest(`[title],[${NATIVE_HINT_ATTRIBUTE}]`);
  // iframe title is an accessibility requirement, not merely a hover hint.
  return element?.tagName === "IFRAME" ? null : element;
}

function nativeHintContent(element: Element): string {
  return element.getAttribute("title") || element.getAttribute(NATIVE_HINT_ATTRIBUTE) || "";
}

function stripNativeHint(element: Element): string {
  const content = nativeHintContent(element);
  if (!content) return "";
  element.setAttribute(NATIVE_HINT_ATTRIBUTE, content);
  if (element.hasAttribute("title")) element.removeAttribute("title");
  return content;
}

function restoreNativeHint(element: Element | null) {
  if (!element) return;
  const content = element.getAttribute(NATIVE_HINT_ATTRIBUTE);
  if (content && !element.hasAttribute("title")) element.setAttribute("title", content);
  element.removeAttribute(NATIVE_HINT_ATTRIBUTE);
}

function positionNativeHint(target: Element, content: string): NativeHintState {
  const rect = target.getBoundingClientRect();
  const roomAbove = rect.top;
  const side = roomAbove >= 44 ? "top" : "bottom";
  return {
    target,
    content,
    left: rect.left + rect.width / 2,
    top: side === "top" ? rect.top - 6 : rect.bottom + 6,
    side,
  };
}

/**
 * Compatibility bridge for legacy/native `title=` hover hints.
 *
 * LemonCrow historically accumulated many title attributes across Code, Review,
 * Sessions and hosted surfaces. The browser tooltip is removed synchronously on
 * hover/focus and rendered with the same visual language as TooltipContent.
 * The title is restored as soon as the element is no longer hovered/focused, so
 * React keeps ownership of the underlying attribute and semantic iframe titles
 * remain untouched.
 */
export function NativeTitleTooltipBridge({ delayDuration = 400 }: NativeTitleTooltipBridgeProps) {
  const portalContainer = usePortalContainer();
  const [hint, setHint] = useState<NativeHintState | null>(null);
  const hoverTarget = useRef<Element | null>(null);
  const focusTarget = useRef<Element | null>(null);
  const pendingTarget = useRef<Element | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const clearTimer = () => {
      if (timer.current !== null) {
        window.clearTimeout(timer.current);
        timer.current = null;
      }
      pendingTarget.current = null;
    };

    const maybeRestore = (target: Element | null) => {
      if (!target || hoverTarget.current === target || focusTarget.current === target) return;
      restoreNativeHint(target);
      setHint((current) => current?.target === target ? null : current);
    };

    const schedule = (target: Element) => {
      const content = stripNativeHint(target);
      if (!content) return;
      clearTimer();
      pendingTarget.current = target;
      timer.current = window.setTimeout(() => {
        timer.current = null;
        pendingTarget.current = null;
        if (!target.isConnected) return;
        setHint(positionNativeHint(target, nativeHintContent(target) || content));
      }, Math.max(0, delayDuration));
    };

    const onPointerOver = (event: PointerEvent) => {
      const target = eligibleNativeHintTarget(event.target);
      if (!target || hoverTarget.current === target) return;
      const previous = hoverTarget.current;
      hoverTarget.current = target;
      maybeRestore(previous);
      schedule(target);
    };

    const onPointerOut = (event: PointerEvent) => {
      const target = hoverTarget.current;
      if (!target) return;
      const related = event.relatedTarget;
      if (related instanceof Node && target.contains(related)) return;
      hoverTarget.current = null;
      if (pendingTarget.current === target) clearTimer();
      maybeRestore(target);
    };

    const onFocusIn = (event: FocusEvent) => {
      const target = eligibleNativeHintTarget(event.target);
      if (!target || focusTarget.current === target) return;
      const previous = focusTarget.current;
      focusTarget.current = target;
      maybeRestore(previous);
      schedule(target);
    };

    const onFocusOut = (event: FocusEvent) => {
      const target = focusTarget.current;
      if (!target) return;
      const related = event.relatedTarget;
      if (related instanceof Node && target.contains(related)) return;
      focusTarget.current = null;
      if (pendingTarget.current === target) clearTimer();
      maybeRestore(target);
    };

    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      clearTimer();
      setHint(null);
    };

    const reposition = () => {
      setHint((current) => current && current.target.isConnected
        ? positionNativeHint(current.target, nativeHintContent(current.target) || current.content)
        : null);
    };

    document.addEventListener("pointerover", onPointerOver, true);
    document.addEventListener("pointerout", onPointerOut, true);
    document.addEventListener("focusin", onFocusIn, true);
    document.addEventListener("focusout", onFocusOut, true);
    document.addEventListener("keydown", onEscape, true);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);

    return () => {
      clearTimer();
      document.removeEventListener("pointerover", onPointerOver, true);
      document.removeEventListener("pointerout", onPointerOut, true);
      document.removeEventListener("focusin", onFocusIn, true);
      document.removeEventListener("focusout", onFocusOut, true);
      document.removeEventListener("keydown", onEscape, true);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      restoreNativeHint(hoverTarget.current);
      if (focusTarget.current !== hoverTarget.current) restoreNativeHint(focusTarget.current);
    };
  }, [delayDuration]);

  if (!hint) return null;
  const container = portalContainer ?? document.body;
  const translate = hint.side === "top" ? "translate(-50%, -100%)" : "translate(-50%, 0)";

  return createPortal(
    <div
      role="tooltip"
      className={cn(TOOLTIP_CONTENT_CLASS, "pointer-events-none fixed animate-in fade-in-0 zoom-in-95")}
      style={{
        left: hint.left,
        top: hint.top,
        transform: translate,
        maxWidth: "min(20rem, calc(100vw - 1rem))",
      }}
    >
      {hint.content}
    </div>,
    container
  );
}
