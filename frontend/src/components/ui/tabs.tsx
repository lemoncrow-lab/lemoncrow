import * as TabsPrimitive from "@radix-ui/react-tabs";
import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef, type ComponentPropsWithoutRef, type ElementRef } from "react";

import { cn } from "../../lib/utils";

/** Tabs with automatic activation: arrow keys move focus and select together. */
export const Tabs = TabsPrimitive.Root;

const tabsListVariants = cva("inline-flex items-center", {
  variants: {
    variant: {
      /** Bordered segmented control (Review segment). */
      segment: "w-max overflow-hidden rounded-md border border-border/80 bg-background",
      /** Text tabs with an active underline. */
      underline: "items-stretch gap-2",
    },
  },
  defaultVariants: { variant: "segment" },
});

const tabsTriggerVariants = cva(
  "inline-flex shrink-0 items-center gap-1.5 font-medium transition-colors disabled:pointer-events-none disabled:opacity-40 pointer-coarse:min-h-11",
  {
    variants: {
      variant: {
        segment:
          "rounded-none px-2 py-1 text-label text-neutral-400 hover:bg-muted hover:text-accent-foreground focus-visible:z-10 data-[state=active]:bg-accent data-[state=active]:text-foreground",
        underline:
          "h-8 rounded-none border-b-2 border-transparent px-0 text-caption leading-none text-muted-foreground hover:text-foreground data-[state=active]:border-neutral-500 data-[state=active]:text-foreground",
      },
    },
    defaultVariants: { variant: "segment" },
  }
);

export const TabsList = forwardRef<
  ElementRef<typeof TabsPrimitive.List>,
  ComponentPropsWithoutRef<typeof TabsPrimitive.List> & VariantProps<typeof tabsListVariants>
>(({ className, variant, ...props }, ref) => (
  <TabsPrimitive.List ref={ref} className={cn(tabsListVariants({ variant }), className)} {...props} />
));
TabsList.displayName = "TabsList";

export const TabsTrigger = forwardRef<
  ElementRef<typeof TabsPrimitive.Trigger>,
  ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger> & VariantProps<typeof tabsTriggerVariants>
>(({ className, variant, ...props }, ref) => (
  <TabsPrimitive.Trigger ref={ref} className={cn(tabsTriggerVariants({ variant }), className)} {...props} />
));
TabsTrigger.displayName = "TabsTrigger";

export const TabsContent = forwardRef<
  ElementRef<typeof TabsPrimitive.Content>,
  ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content ref={ref} className={cn("outline-none", className)} {...props} />
));
TabsContent.displayName = "TabsContent";
