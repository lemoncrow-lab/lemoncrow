/** What Home remembers across reloads (SPEC 4.1): the sort and the filters of the dense list. Malformed state falls back field by field (ST-4). */

import { isSortKey, NO_FILTERS, type HomeFilters, type SortKey } from "./homeModel";

export const HOME_PREFS_KEY = "lemoncrow.review.home";

export interface HomePrefs {
  sort: SortKey;
  filters: HomeFilters;
}

export const DEFAULT_PREFS: HomePrefs = { sort: "waiting", filters: NO_FILTERS };

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").slice(0, 50) : [];
}

export function parsePrefs(raw: string | null): HomePrefs {
  if (!raw) return DEFAULT_PREFS;
  try {
    const value = JSON.parse(raw) as Record<string, unknown> | null;
    if (!value || typeof value !== "object") return DEFAULT_PREFS;
    const filters = (value.filters && typeof value.filters === "object" ? value.filters : {}) as Record<string, unknown>;
    return {
      sort: isSortKey(value.sort) ? value.sort : DEFAULT_PREFS.sort,
      filters: {
        repos: strings(filters.repos),
        sources: strings(filters.sources).filter((name): name is "Local" | "GitHub" => name === "Local" || name === "GitHub"),
        cameBack: filters.cameBack === true,
      },
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

export function loadPrefs(): HomePrefs {
  try {
    return parsePrefs(window.localStorage.getItem(HOME_PREFS_KEY));
  } catch {
    return DEFAULT_PREFS;
  }
}

export function savePrefs(prefs: HomePrefs): void {
  try {
    window.localStorage.setItem(HOME_PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // Storage blocked: the choice lasts until the page closes.
  }
}
