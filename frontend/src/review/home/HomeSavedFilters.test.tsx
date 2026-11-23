import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InboxFilterDefinition, ReviewDirectoryRow, SavedInboxFilter } from "../types";
import HomeSavedFilters, { homeCanApply, repoIdsFor, repoNamesFor } from "./HomeSavedFilters";
import { NO_FILTERS } from "./homeModel";

const rows = [{ id: "r1", repo_id: "repo_1", project: "shop" }, { id: "r2", repo_id: "repo_2", project: "docs" }] as unknown as ReviewDirectoryRow[];
const definition = (extra: Partial<InboxFilterDefinition> = {}): InboxFilterDefinition => ({
  view: "all", status: "all", repos: ["repo_1"], teams: [], workflow_states: [], priorities: [], query: "guest", ...extra,
});
const filter = (id: string, name: string, extra: Partial<InboxFilterDefinition> = {}): SavedInboxFilter => ({
  id, name, definition: definition(extra), created_at: "", updated_at: "",
});
function api(filters: SavedInboxFilter[]) {
  return {
    list: vi.fn(async () => ({ filters, limit: 50 })),
    create: vi.fn(async () => ({ filter: filters[0] })),
    remove: vi.fn(async () => ({})),
  };
}
afterEach(cleanup);

describe("saved filters on Home", () => {
  it("maps Home's repository names to the ids a filter keeps, and back", () => {
    expect(repoIdsFor(["shop"], rows)).toEqual(["repo_1"]);
    expect(repoNamesFor(["repo_2", "gone"], rows)).toEqual(["docs"]);
  });

  it("applies only what Home can show", () => {
    expect(homeCanApply(definition())).toBe(true);
    for (const extra of [{ teams: ["t"] }, { priorities: ["p1"] }, { status: "open" }, { view: "mine" }] as Partial<InboxFilterDefinition>[]) {
      expect(homeCanApply(definition(extra))).toBe(false);
    }
  });

  it("applies a filter, refuses one Home cannot show, and deletes", async () => {
    const source = api([filter("f1", "Guest work"), filter("f2", "By team", { teams: ["t"] })]);
    const onApply = vi.fn();
    render(<HomeSavedFilters api={source} filters={NO_FILTERS} query="" rows={rows} onApply={onApply} />);
    await userEvent.click(screen.getByRole("button", { name: "Saved filters" }));
    expect(await screen.findByRole("button", { name: "By team" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Delete saved filter By team" }));
    await waitFor(() => expect(source.remove).toHaveBeenCalledWith("f2"));
    await userEvent.click(await screen.findByRole("button", { name: "Guest work" }));
    expect(onApply).toHaveBeenCalledWith(["shop"], "guest");
  });

  it("saves the current selection by repository id", async () => {
    const source = api([]);
    render(<HomeSavedFilters api={source} filters={{ ...NO_FILTERS, repos: ["docs"] }} query=" api " rows={rows} onApply={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Saved filters" }));
    await userEvent.type(await screen.findByLabelText("Name for this filter"), "Docs API{Enter}");
    await waitFor(() => expect(source.create).toHaveBeenCalledWith("Docs API", { view: "all", status: "all", repos: ["repo_2"], query: "api" }));
  });

  it("does not offer to save a chip a filter cannot hold, and says why a load failed", async () => {
    const source = api([]);
    source.list.mockRejectedValueOnce(new Error("offline"));
    render(<HomeSavedFilters api={source} filters={{ ...NO_FILTERS, cameBack: true }} query="x" rows={rows} onApply={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Saved filters" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
    expect(screen.queryByLabelText("Name for this filter")).toBeNull();
  });
});
