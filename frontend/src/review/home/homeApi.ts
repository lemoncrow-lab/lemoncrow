import { createContext, useContext, type ReactNode } from "react";

import { fetchReviewDirectory, fetchReviewJournal, putReviewQueueState, type ReviewDirectoryQuery } from "../reviewApi";
import type {
  InboxFilterDefinition,
  ReviewDirectoryPage,
  ReviewDirectoryRow,
  ReviewJournal,
  SavedInboxFilter,
  SavedInboxFilterList,
} from "../types";

export interface HomeRows {
  rows: ReviewDirectoryRow[];
  teamsSupported: boolean;
  /** More reviews exist than were read. */
  truncated: boolean;
  /** How many reviews the source has for this viewer, when it counted them ("showing 300 of 412"). */
  total?: number;
  /** False when the source keeps no per-person queue state: Home then offers no snooze. Absent means true. */
  queueSupported?: boolean;
  /** False when the viewer's own queue was cut off too: the banner then stops saying everything that waits on them is here. */
  queueComplete?: boolean;
  /** What a source with no review at all says in place of the local first run (hosted: how to get one). */
  firstRun?: ReactNode;
}

/** The viewer's own saved filters, for a source that keeps them. */
export interface HomeSavedFiltersApi {
  list(): Promise<SavedInboxFilterList>;
  create(name: string, definition: Partial<InboxFilterDefinition>): Promise<{ filter: SavedInboxFilter }>;
  remove(id: string): Promise<unknown>;
}

/**
 * Where Home reads its reviews from and what that source can do (P9-2). The local app's source is the default; the
 * hosted web UI passes its own, fed by the hosted Inbox API. A capability the source lacks is left out of the screen,
 * never drawn inert.
 */
export interface HomeSource {
  load(): Promise<HomeRows>;
  /** Reviews matching a search the source answers itself, for when `load` left some out. Absent: the loaded rows are all there is. */
  search?(query: string): Promise<ReviewDirectoryRow[]>;
  /** What the queue-clear card says where the local wording ("lc review", "Claude Code") does not fit. */
  queueClear?: { sparse: string; dense: string };
  /** Save or clear this viewer's snooze. Absent: this source keeps none, so Home offers no snooze. */
  snooze?(id: string, until: string | null): Promise<unknown>;
  /** The viewer's saved filters. Absent: Home offers none. */
  savedFilters?: HomeSavedFiltersApi;
  /** The agent session behind a review, for the hero's "Asked" line. Absent: there is none to read. */
  journal?(id: string): Promise<ReviewJournal>;
  /** Whether "Open in terminal" is offered. */
  terminal: boolean;
  /** Where the bar's "Review settings" goes; the hosted web UI keeps personal review settings elsewhere. */
  settingsPath?: string;
}

/** Home's whole read: the newest reviews one request may carry (the server's `read=home` limit). */
export const HOME_ROWS = 500;

/**
 * One read of `GET /v1/reviews?view=all&read=home`, up to 500 rows, one row per review with this viewer's own fields.
 * The server scores every review once for the request, not once per page (five pages of 100 scored them five times).
 * lc-debt: the newest 500 reviews only, and `truncated` says so past that; a server-side queue endpoint would page by section.
 */
export async function readHomeRows(read: (query: ReviewDirectoryQuery) => Promise<ReviewDirectoryPage>): Promise<HomeRows> {
  const result = await read({ view: "all", status: "all", query: "", cursor: "", limit: HOME_ROWS, read: "home" });
  return { rows: result.reviews, teamsSupported: result.teams_supported === true, truncated: Boolean(result.next_cursor) };
}

export function fetchHomeRows(): Promise<HomeRows> {
  return readHomeRows((query) => fetchReviewDirectory(query));
}

/** Words for the queue-clear card, where the source has no wording of its own. */
export const LOCAL_QUEUE_CLEAR = {
  sparse: "Start a review from the terminal: lc review",
  dense: "Nothing waits on you. We'll bring a review back when Claude Code has a new revision.",
} as const;

/** The local app: this machine's reviews, its queue state and its agent sessions. */
export const localHomeSource: HomeSource = {
  load: () => fetchHomeRows(),
  snooze: (id, until) => putReviewQueueState(id, { snoozed_until: until }),
  journal: (id) => fetchReviewJournal(id),
  terminal: true,
};

const HomeSourceContext = createContext<HomeSource>(localHomeSource);

/** Where Home and the palette read reviews: the local app's by default, the hosted web UI provides its own. */
export const HomeSourceProvider = HomeSourceContext.Provider;

export function useHomeSource(): HomeSource {
  return useContext(HomeSourceContext);
}
