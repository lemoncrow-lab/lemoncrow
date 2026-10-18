import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// The dense type scale from tailwind.config.ts; without this, tailwind-merge would read
// `text-caption` as a colour and drop the element's real text colour.
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: ["micro", "caption", "label", "body", "heading", "title"] }],
    },
  },
});

/** Join class names; later Tailwind utilities override conflicting earlier ones. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
