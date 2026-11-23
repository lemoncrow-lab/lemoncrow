import { describe, expect, it, vi } from "vitest";

import { reviewDirectoryParams } from "../reviewApi";
import type { ReviewDirectoryPage } from "../types";
import { HOME_ROWS, readHomeRows } from "./homeApi";
import { queueRow } from "./homeFixtures";

const page = (rows = [queueRow({ id: "a" })], extra: Partial<ReviewDirectoryPage> = {}): ReviewDirectoryPage =>
  ({ reviews: rows, status: "all", query: "", next_cursor: "", teams_supported: false, ...extra }) as ReviewDirectoryPage;

describe("Home's read", () => {
  it("is one request for the newest 500 reviews, asked as a Home read", async () => {
    const read = vi.fn().mockResolvedValue(page());
    const rows = await readHomeRows(read);
    expect(read).toHaveBeenCalledTimes(1);
    expect(HOME_ROWS).toBe(500);
    const query = read.mock.calls[0][0];
    expect(query).toMatchObject({ view: "all", status: "all", limit: 500, read: "home", cursor: "" });
    expect(reviewDirectoryParams(query).toString()).toBe("view=all&status=all&q=&limit=500&read=home");
    expect(rows).toMatchObject({ truncated: false, teamsSupported: false });
    expect(rows.rows.map((row) => row.id)).toEqual(["a"]);
  });

  it("says rows were left out when the server has more past 500, and never asks again", async () => {
    const read = vi.fn().mockResolvedValue(page([queueRow({ id: "a" })], { next_cursor: "more", teams_supported: true }));
    const rows = await readHomeRows(read);
    expect(read).toHaveBeenCalledTimes(1);
    expect(rows).toMatchObject({ truncated: true, teamsSupported: true });
  });

  it("does not ask for a Home read on any other page", () => {
    expect(reviewDirectoryParams({ status: "open", query: "", cursor: "", limit: 30 }).has("read")).toBe(false);
  });
});
