import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ElementRef,
  type HTMLAttributes,
  type MutableRefObject,
} from "react";

import { focusReturnTarget } from "../../lib/focus";
import { cn } from "../../lib/utils";
import { usePortalContainer } from "./portal-container";

const ReturnFocusContext = createContext<MutableRefObject<HTMLElement | null> | null>(null);

/**
 * Dialog root. Radix only returns focus to a DialogTrigger; our dialogs are also opened from
 * toolbars, palettes and shortcuts, so the element focused when the dialog opened is recorded
 * here and restored on close (see useReturnFocus).
 */
export function Dialog({
  open,
  defaultOpen,
  onOpenChange,
  ...props
}: ComponentPropsWithoutRef<typeof DialogPrimitive.Root>) {
  const returnFocus = useRef<HTMLElement | null>(null);
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen ?? false);
  const isOpen = open ?? uncontrolledOpen;
  // Layout effect: runs before the portaled content mounts and takes focus.
  useLayoutEffect(() => {
    if (!isOpen) return;
    returnFocus.current = focusReturnTarget();
  }, [isOpen]);
  return (
    <ReturnFocusContext.Provider value={returnFocus}>
      <DialogPrimitive.Root
        open={open}
        defaultOpen={defaultOpen}
        onOpenChange={(next) => {
          setUncontrolledOpen(next);
          onOpenChange?.(next);
        }}
        {...props}
      />
    </ReturnFocusContext.Provider>
  );
}

/** onCloseAutoFocus handler that restores focus to the dialog's opener unless the caller prevents it. */
export function useReturnFocus(onCloseAutoFocus?: (event: Event) => void) {
  const returnFocus = useContext(ReturnFocusContext);
  return useCallback(
    (event: Event) => {
      onCloseAutoFocus?.(event);
      if (event.defaultPrevented) return;
      event.preventDefault();
      const target = returnFocus?.current;
      if (target?.isConnected) target.focus();
    },
    [onCloseAutoFocus, returnFocus]
  );
}
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

/**
 * True when a Dialog/Sheet from this module is currently open and portalled into the DOM.
 * Callers outside components/ui (e.g. a global keydown handler that should pause while a
 * modal owns focus) use this instead of querying for the role/aria-modal attributes
 * themselves, which the UI-primitives guard flags as a hand-rolled modal.
 */
export function isDialogOpen(): boolean {
  return document.querySelector('[role="dialog"][aria-modal="true"]') !== null;
}

export function DialogPortal(props: ComponentPropsWithoutRef<typeof DialogPrimitive.Portal>) {
  const container = usePortalContainer();
  return <DialogPrimitive.Portal container={container} {...props} />;
}

export const DialogOverlay = forwardRef<
  ElementRef<typeof DialogPrimitive.Overlay>,
  ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-50 bg-black/50 backdrop-blur-[2px] data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
      className
    )}
    {...props}
  />
));
DialogOverlay.displayName = "DialogOverlay";

export interface DialogContentProps
  extends ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  overlayClassName?: string;
}

/** Centered modal surface. Compose with DialogHeader / DialogBody / DialogFooter. */
export const DialogContent = forwardRef<ElementRef<typeof DialogPrimitive.Content>, DialogContentProps>(
  ({ className, overlayClassName, children, onCloseAutoFocus, ...props }, ref) => (
    <DialogPortal>
      <DialogOverlay className={overlayClassName} />
      <DialogPrimitive.Content
        ref={ref}
        aria-modal="true"
        onCloseAutoFocus={useReturnFocus(onCloseAutoFocus)}
        className={cn(
          "fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-lg border border-border/80 bg-background text-popover-foreground shadow-2xl shadow-black/35 outline-none",
          "data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
          className
        )}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPortal>
  )
);
DialogContent.displayName = "DialogContent";

export function DialogHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex shrink-0 items-start gap-3 border-b border-border/80 px-4 py-3", className)} {...props} />;
}

export function DialogBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("min-h-0 flex-1 overflow-auto px-4 py-4", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex shrink-0 items-center justify-end gap-2 border-t border-border/80 px-4 py-3", className)} {...props} />;
}

export const DialogTitle = forwardRef<
  ElementRef<typeof DialogPrimitive.Title>,
  ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title ref={ref} className={cn("text-heading font-semibold text-foreground", className)} {...props} />
));
DialogTitle.displayName = "DialogTitle";

export const DialogDescription = forwardRef<
  ElementRef<typeof DialogPrimitive.Description>,
  ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description ref={ref} className={cn("mt-0.5 text-caption leading-4 text-muted-foreground", className)} {...props} />
));
DialogDescription.displayName = "DialogDescription";
