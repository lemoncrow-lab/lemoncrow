import { useCallback, useEffect, useMemo, useState } from "react";
import { Bookmark, Trash2 } from "lucide-react";

import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "../../components/ui/popover";
import type { InboxFilterDefinition, ReviewDirectoryRow, SavedInboxFilter } from "../types";
import type { HomeSavedFiltersApi } from "./homeApi";
import { repoOf, type HomeFilters } from "./homeModel";

/** What Home can show of a saved filter: its repositories and search. Anything else is not silently dropped. */
export function homeCanApply(definition: InboxFilterDefinition): boolean {
  return definition.view === "all"
    && definition.status === "all"
    && definition.teams.length === 0
    && definition.workflow_states.length === 0
    && definition.priorities.length === 0;
}

/** Home's chips name repositories for reading; a saved filter keeps their ids, as the Inbox API reads them. */
export function repoIdsFor(names: readonly string[], rows: readonly ReviewDirectoryRow[]): string[] {
  const wanted = new Set(names);
  return [...new Set(rows.filter((row) => wanted.has(repoOf(row)) && row.repo_id).map((row) => row.repo_id))];
}

export function repoNamesFor(ids: readonly string[], rows: readonly ReviewDirectoryRow[]): string[] {
  const wanted = new Set(ids);
  return [...new Set(rows.filter((row) => wanted.has(row.repo_id)).map(repoOf))];
}

const message = (cause: unknown) => (cause instanceof Error && cause.message ? cause.message : "Please try again.");

/** The viewer's own saved filters on Home: a selection over reviews they can already read, never access. */
export default function HomeSavedFilters({ api, filters, query, rows, onApply }: {
  api: HomeSavedFiltersApi;
  filters: HomeFilters;
  query: string;
  rows: readonly ReviewDirectoryRow[];
  onApply: (repos: string[], query: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState<SavedInboxFilter[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");

  const refresh = useCallback(async () => {
    try {
      setSaved((await api.list()).filters);
      setProblem("");
    } catch (cause) {
      setProblem(`Saved filters could not be loaded. ${message(cause)}`);
    }
  }, [api]);
  useEffect(() => { if (open) void refresh(); }, [open, refresh]);

  const repos = useMemo(() => repoIdsFor(filters.repos, rows), [filters.repos, rows]);
  const hasSelection = repos.length > 0 || query.trim() !== "";
  // A chip a saved filter cannot hold is refused here, so what comes back is what was on screen.
  const unsaveable = filters.sources.length > 0 || filters.cameBack;

  const act = async (work: () => Promise<unknown>, failure: string) => {
    setBusy(true);
    try {
      await work();
      await refresh();
      return true;
    } catch (cause) {
      setProblem(`${failure} ${message(cause)}`);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const definition: Partial<InboxFilterDefinition> = { view: "all", status: "all", repos, query: query.trim() };
    if (await act(() => api.create(name.trim(), definition), "The filter was not saved.")) setName("");
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" className="min-w-11" aria-label="Saved filters" data-testid="home-saved-filters">
          <Bookmark size={16} aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 space-y-3">
        <h2 className="text-label font-semibold">Saved filters</h2>
        {problem ? <p role="alert" className="text-meta text-muted-foreground">{problem}</p> : null}
        {saved === null && !problem ? <p role="status" className="text-meta text-muted-foreground">Loading…</p> : null}
        {saved?.length === 0 ? <p className="text-meta text-muted-foreground">None yet. Choose repositories or search, then save it here.</p> : null}
        {saved && saved.length > 0 ? (
          <ul className="space-y-1">
            {saved.map((filter) => {
              const usable = homeCanApply(filter.definition);
              return (
                <li key={filter.id} className="flex items-center gap-1">
                  <Button
                    variant="ghost"
                    className="min-w-0 flex-1 justify-start truncate"
                    disabled={!usable}
                    title={usable ? undefined : "Uses Inbox views, teams, workflow or priority, which Home does not show"}
                    onClick={() => { onApply(repoNamesFor(filter.definition.repos, rows), filter.definition.query); setOpen(false); }}
                  >
                    {filter.name}
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="min-w-11"
                    aria-label={`Delete saved filter ${filter.name}`}
                    disabled={busy}
                    onClick={() => void act(() => api.remove(filter.id), "The filter was not deleted.")}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                  </Button>
                </li>
              );
            })}
          </ul>
        ) : null}
        {unsaveable ? (
          <p className="text-meta text-muted-foreground">Source and “came back” filters cannot be saved. Clear them to save this view.</p>
        ) : hasSelection ? (
          <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); if (name.trim()) void save(); }}>
            <Input value={name} maxLength={80} aria-label="Name for this filter" placeholder="Name this filter" onChange={(event) => setName(event.target.value)} />
            <Button type="submit" variant="outline" disabled={busy || !name.trim()}>Save</Button>
          </form>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
