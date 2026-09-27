import { useEffect } from "react";

/** Close popover-style <details> menus whenever the user clicks elsewhere. */
export function useReviewDropdownDismissal(): void {
  useEffect(() => {
    const dismissOutside = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      document.querySelectorAll<HTMLDetailsElement>("details[data-review-dropdown][open]").forEach((details) => {
        if (!details.contains(target)) details.open = false;
      });
    };
    document.addEventListener("pointerdown", dismissOutside);
    return () => document.removeEventListener("pointerdown", dismissOutside);
  }, []);
}
