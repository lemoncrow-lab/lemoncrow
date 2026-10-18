import * as DialogPrimitive from "@radix-ui/react-dialog";
import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef, type ComponentPropsWithoutRef, type ElementRef } from "react";

import { cn } from "../../lib/utils";
import { DialogOverlay, DialogPortal, useReturnFocus } from "./dialog";

export {
  Dialog as Sheet,
  DialogTrigger as SheetTrigger,
  DialogClose as SheetClose,
  DialogHeader as SheetHeader,
  DialogBody as SheetBody,
  DialogFooter as SheetFooter,
  DialogTitle as SheetTitle,
  DialogDescription as SheetDescription,
} from "./dialog";

const sheetVariants = cva(
  "fixed z-50 flex flex-col overflow-hidden border-border/80 bg-background text-popover-foreground shadow-2xl shadow-black/35 outline-none data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:duration-150 data-[state=open]:duration-200",
  {
    variants: {
      side: {
        right: "inset-y-0 right-0 h-full w-[min(500px,100%)] border-l data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
        left: "inset-y-0 left-0 h-full w-[min(500px,100%)] border-r data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left",
        top: "inset-x-0 top-0 max-h-[85dvh] border-b data-[state=closed]:slide-out-to-top data-[state=open]:slide-in-from-top",
        bottom: "inset-x-0 bottom-0 max-h-[85dvh] border-t data-[state=closed]:slide-out-to-bottom data-[state=open]:slide-in-from-bottom",
      },
    },
    defaultVariants: { side: "right" },
  }
);

export interface SheetContentProps
  extends ComponentPropsWithoutRef<typeof DialogPrimitive.Content>,
    VariantProps<typeof sheetVariants> {
  overlayClassName?: string;
}

/** Edge-anchored modal panel (Review sheets). Width via className, e.g. `w-[min(640px,100%)]`. */
export const SheetContent = forwardRef<ElementRef<typeof DialogPrimitive.Content>, SheetContentProps>(
  ({ side, className, overlayClassName, children, onCloseAutoFocus, ...props }, ref) => (
    <DialogPortal>
      <DialogOverlay className={cn("bg-black/45 backdrop-blur-0", overlayClassName)} />
      <DialogPrimitive.Content
        ref={ref}
        aria-modal="true"
        onCloseAutoFocus={useReturnFocus(onCloseAutoFocus)}
        className={cn(sheetVariants({ side }), className)}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPortal>
  )
);
SheetContent.displayName = "SheetContent";
