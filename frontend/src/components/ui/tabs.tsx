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
      segment: "w-max gap-0.5 rounded-md border border-border bg-muted p-0.5",
      /** Text tabs with an active underline (ink on light, lemon on dark). */
      underline: "items-stretch gap-1 border-b border-border",
    },
  },
  defaultVariants: { variant: "segment" },
});

const tabsTriggerVariants = cva(
  "inline-flex shrink-0 items-center gap-1.5 font-medium transition-colors duration-fast disabled:pointer-events-none disabled:opacity-50 pointer-coarse:min-h-11",
  {
    variants: {
      variant: {
        segment:
          "h-6 rounded-sm border border-transparent px-2 text-meta text-muted-foreground hover:text-foreground focus-visible:z-10 data-[state=active]:border-border data-[state=active]:bg-card data-[state=active]:text-foreground",
        underline:
          "relative -mb-px h-8 rounded-none px-3 text-ui leading-none text-muted-foreground hover:text-foreground data-[state=active]:text-foreground data-[state=active]:shadow-indicator",
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
