export type Theme = "light" | "dark";

const STORAGE_KEY = "lemoncrow-theme";

function systemTheme(): Theme {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

/** The reviewer's saved light/dark choice; with none saved (Auto), the system's. */
export function getInitialTheme(): Theme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* localStorage unavailable */
  }
  return systemTheme();
}

/** Puts *theme* on the page. ``persist``: it is the reviewer's explicit choice; ``"auto"`` forgets any saved one. */
export function applyTheme(theme: Theme, persist: boolean | "auto" = true): void {
  const root = document.documentElement;
  root.classList.remove("light", "dark");
  root.classList.add(theme);
  if (!persist) return;
  try {
    if (persist === "auto") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* localStorage unavailable */
  }
}
