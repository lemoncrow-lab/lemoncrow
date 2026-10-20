import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// The custom scales from the shared preset (src/design/lc-preset.cjs) and tailwind.config.ts. Without
// them tailwind-merge would read `text-caption` or `text-ui` as a colour and drop the element's real
// text colour, and would not let `shadow-float` or `border-l-rail` override their conflicting siblings.
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [
        {
          text: ["micro", "caption", "label", "body", "heading", "title", "tag", "meta", "ui", "small", "copy", "lead", "h3", "h2", "display", "code"],
        },
      ],
      shadow: [{ shadow: ["float", "frame", "indicator"] }],
      "border-w-l": [{ "border-l": ["rail"] }],
      duration: [{ duration: ["fast", "base", "slow"] }],
      tracking: [{ tracking: ["tag", "wordmark"] }],
    },
  },
});

/** Join class names; later Tailwind utilities override conflicting earlier ones. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
