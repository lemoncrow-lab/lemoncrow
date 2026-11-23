/**
 * The URL scheme of the redesigned Review flow (SPEC 3.1), as pure rules.
 *
 *   /r/<review>                       Overview
 *   /r/<review>/one[/<part>]          One at a time (a decision part or the Quick check)
 *   /r/<review>/all[#<part>]          All files, scrolled to a part
 *   /r/<review>/map?lens=&view=       Map
 *   /r/<review>/walkthrough[/<step>][?view=map]
 *                                     Walkthrough (the change one step at a time, as steps or on the map)
 *   /r/<review>/brief                 Brief (the decision-first summary of the change)
 *   /r/<review>/xray[?at=]            X-Ray (one path through a changed file, before and after)
 *   /r/<review>/preview[/<surface>]   Preview
 *   /r/<review>/wrap                  Wrap up
 *   /r/<review>/done                  Done
 *   /r/<review>/rev/<n>               Welcome back (Revision n) and Approved
 *   /r/<review>/second                Second signature (second reviewer only)
 *   /settings/<section>               Settings
 *
 * The walkthrough's former addresses (`/tour[/<step>]`, and the Classic Guided
 * review and Change Story queries `?view=tour` and `?map-story=`) are read as the
 * Walkthrough; `legacyFlowRedirect` names the address they are replaced with.
 *
 * Nothing here reads the browser, the network or the review setting. A part or
 * a surface is an opaque, percent-decoded string: how the product names a part
 * in a URL is a screen's decision, not the route layer's.
 */

export const MAP_LENSES = ["data-path", "dependencies"] as const;
export const MAP_VIEWS = ["changes", "before", "after"] as const;
export type MapLens = (typeof MAP_LENSES)[number];
export type MapView = (typeof MAP_VIEWS)[number];
/** The Walkthrough as a list of steps (the default) or played on the map. */
export type WalkthroughView = "steps" | "map";

export type ReviewFlowRoute =
  | { kind: "overview" }
  /** `part` is empty for `/one`: the first part that still needs an answer. */
  | { kind: "one"; part: string }
  /** `part` is the URL fragment; empty when the link names no part. */
  | { kind: "all"; part: string }
  | { kind: "map"; lens: MapLens; view: MapView }
  /** `step` is empty for `/walkthrough`: the step the reviewer was last on, else the first. */
  | { kind: "walkthrough"; step: string; view: WalkthroughView }
  | { kind: "brief" }
  /**
   * `at` is `side~snapshot~node`; empty for `/xray`: the whole change. On the graph picture the side may also be `both`
   * and the node is a card of the drawing or a Map node.
   */
  | { kind: "xray"; at: string }
  /** `surface` is empty for `/preview`: the first surface. */
  | { kind: "preview"; surface: string }
  | { kind: "wrap" }
  | { kind: "done" }
  | { kind: "rev"; revision: number }
  | { kind: "second" };

export type ReviewFlowRouteKind = ReviewFlowRoute["kind"];

export const OVERVIEW_ROUTE: ReviewFlowRoute = { kind: "overview" };

export interface ReviewFlowLocation {
  pathname: string;
  search?: string;
  hash?: string;
}

export interface ParsedReviewFlowLocation {
  /** The review slug exactly as it appears in the address, percent-decoded. */
  reviewRef: string;
  route: ReviewFlowRoute;
  /**
   * False when the path under the review names no screen (`/r/shop/nope`).
   * `route` is then the Overview, which is where such an address belongs.
   */
  known: boolean;
}

function decode(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function oneOf<T extends string>(allowed: readonly T[], value: string | null, fallback: T): T {
  return allowed.find((item) => item === value) ?? fallback;
}

/**
 * The path segments under `/r/<review>`; null when the pathname is not a flow
 * address. `/r/x/...` and `/r/<review>/compare/...` are the comparison reader's.
 */
function flowSegments(pathname: string): { reviewRef: string; rest: string[] } | null {
  const parts = pathname.split("/");
  if (parts[0] !== "" || parts[1] !== "r" || !parts[2]) return null;
  const reviewRef = decode(parts[2]);
  if (reviewRef === "x") return null;
  const rest = parts.slice(3);
  // One trailing slash is the same address.
  if (rest.length > 0 && rest[rest.length - 1] === "") rest.pop();
  if (rest[0] === "compare") return null;
  return { reviewRef, rest };
}

export function parseReviewFlowLocation(location: ReviewFlowLocation): ParsedReviewFlowLocation | null {
  const segments = flowSegments(location.pathname);
  if (!segments) return null;
  const { reviewRef, rest } = segments;
  const fragment = decode((location.hash ?? "").replace(/^#/, ""));
  const query = new URLSearchParams(location.search ?? "");
  const known = (route: ReviewFlowRoute): ParsedReviewFlowLocation => ({ reviewRef, route, known: true });
  const unknown: ParsedReviewFlowLocation = { reviewRef, route: OVERVIEW_ROUTE, known: false };

  if (rest.length === 0) return legacyWalkthrough(query) ?? known(OVERVIEW_ROUTE);
  const [head, arg, ...extra] = rest;
  if (extra.length > 0 || arg === "") return unknown;
  switch (head) {
    case "one":
      return known({ kind: "one", part: arg === undefined ? "" : decode(arg) });
    case "all":
      return arg === undefined ? known({ kind: "all", part: fragment }) : unknown;
    case "map":
      return arg === undefined
        ? known({
            kind: "map",
            lens: oneOf(MAP_LENSES, query.get("lens"), "data-path"),
            view: oneOf(MAP_VIEWS, query.get("view"), "changes"),
          })
        : unknown;
    case "walkthrough":
    case "tour":
      return known({ kind: "walkthrough", step: arg === undefined ? "" : decode(arg), view: query.get("view") === "map" ? "map" : "steps" });
    case "brief":
      return arg === undefined ? known({ kind: "brief" }) : unknown;
    case "xray":
      return arg === undefined ? known({ kind: "xray", at: query.get("at") ?? "" }) : unknown;
    case "preview":
      return known({ kind: "preview", surface: arg === undefined ? "" : decode(arg) });
    case "wrap":
      return arg === undefined ? known({ kind: "wrap" }) : unknown;
    case "done":
      return arg === undefined ? known({ kind: "done" }) : unknown;
    case "rev":
      return arg !== undefined && /^[1-9]\d{0,8}$/.test(arg) ? known({ kind: "rev", revision: Number(arg) }) : unknown;
    case "second":
      return arg === undefined ? known({ kind: "second" }) : unknown;
    default:
      return unknown;
  }

  function legacyWalkthrough(params: URLSearchParams): ParsedReviewFlowLocation | null {
    // The Classic Change Story (`?map-story=<node>`) and Guided review (`?view=tour&guided=<step>`) are the Walkthrough now.
    if (params.get("map-story")) return known({ kind: "walkthrough", step: "", view: "map" });
    if (params.get("view") === "tour") return known({ kind: "walkthrough", step: params.get("guided") ?? "", view: "steps" });
    return null;
  }
}

/**
 * The address an old walkthrough address is replaced with (`/tour[/<step>]`, `?view=tour`, `?map-story=`); null for
 * every other address, the Walkthrough's own included.
 */
export function legacyFlowRedirect(location: ReviewFlowLocation): string | null {
  const parsed = parseReviewFlowLocation(location);
  if (!parsed?.known || parsed.route.kind !== "walkthrough") return null;
  return flowSegments(location.pathname)?.rest[0] === "walkthrough" ? null : buildReviewFlowPath(parsed.reviewRef, parsed.route);
}

/** The address of one screen of one review, as pathname + query + fragment. */
export function buildReviewFlowPath(reviewRef: string, route: ReviewFlowRoute): string {
  const base = `/r/${encodeURIComponent(reviewRef)}`;
  switch (route.kind) {
    case "overview":
      return base;
    case "one":
      return route.part ? `${base}/one/${encodeURIComponent(route.part)}` : `${base}/one`;
    case "all":
      return route.part ? `${base}/all#${encodeURIComponent(route.part)}` : `${base}/all`;
    case "map":
      return `${base}/map?lens=${route.lens}&view=${route.view}`;
    case "walkthrough": {
      const path = route.step ? `${base}/walkthrough/${encodeURIComponent(route.step)}` : `${base}/walkthrough`;
      return route.view === "map" ? `${path}?view=map` : path;
    }
    case "brief":
      return `${base}/brief`;
    case "xray":
      return route.at ? `${base}/xray?at=${encodeURIComponent(route.at)}` : `${base}/xray`;
    case "preview":
      return route.surface ? `${base}/preview/${encodeURIComponent(route.surface)}` : `${base}/preview`;
    case "wrap":
      return `${base}/wrap`;
    case "done":
      return `${base}/done`;
    case "rev":
      return `${base}/rev/${route.revision}`;
    case "second":
      return `${base}/second`;
  }
}

/**
 * What follows `/r/<review>` in a flow address (`/one/x`, `/rev/2`), raw as
 * written; empty for the Overview and for anything that is not a known flow
 * screen. The Reader keeps this when it canonicalises the review part of its
 * address, so `/reviews/<id>/one/x`-style rewrites never swallow the screen.
 */
export function reviewFlowSuffix(location: ReviewFlowLocation): string {
  const parsed = parseReviewFlowLocation(location);
  if (!parsed || !parsed.known || parsed.route.kind === "overview") return "";
  const parts = location.pathname.split("/").slice(3);
  // A Classic walkthrough query on the Overview's address has no screen segment to keep.
  return parts.some(Boolean) ? `/${parts.join("/")}` : "";
}

/**
 * Carry the screen of `location` onto `canonical`, the Reader's own address for
 * the review (`/r/<ref>?...`). The Reader rewrites the review part of its address
 * (`/reviews/<id>` to `/r/<ref>`); this keeps `/one/x` and the fragment through
 * that rewrite, with the screen's own query (`lens`, `view`, `at`). Returns the suffix it kept,
 * "" when there was none.
 */
export function keepFlowScreen(canonical: URL, location: ReviewFlowLocation & { hash: string }): string {
  const suffix = reviewFlowSuffix(location);
  if (!suffix) return "";
  canonical.pathname += suffix;
  canonical.hash = location.hash;
  // The screen's own query (the Map's lens and view) is part of the screen, as typed.
  const typed = new URLSearchParams(location.search ?? "");
  for (const key of SCREEN_QUERY_KEYS) {
    const value = typed.get(key);
    if (value !== null) canonical.searchParams.set(key, value);
  }
  return suffix;
}

const SCREEN_QUERY_KEYS = ["lens", "view", "at"] as const;

/**
 * The address the Reader writes when it changes its own query (`?view=`, `?p=`).
 * A flow screen's fragment (`/all#<part>`) is part of its address and is kept;
 * off a flow screen the Reader has always written none, and still writes none.
 */
export function readerSearchHref(location: { pathname: string; hash: string }, query: string, onFlowScreen: boolean): string {
  return `${location.pathname}${query ? `?${query}` : ""}${onFlowScreen ? location.hash : ""}`;
}

// ---------------------------------------------------------------- settings

/**
 * `/settings/<section>` for the personal side of Settings (SPEC 3.1, 4.11).
 * `org` is the adopted Organization route (SPEC 7.1 Q4).
 */
export const REVIEW_SETTINGS_SECTIONS = [
  "reading",
  "ai",
  "notifications",
  "keyboard",
  "look",
  "more",
  "policy",
  "org",
] as const;
export type ReviewSettingsSection = (typeof REVIEW_SETTINGS_SECTIONS)[number];

export function parseReviewSettingsPath(pathname: string): ReviewSettingsSection | null {
  const match = /^\/settings\/([^/]+)\/?$/.exec(pathname);
  if (!match) return null;
  const section = decode(match[1]);
  return REVIEW_SETTINGS_SECTIONS.find((item) => item === section) ?? null;
}

export function buildReviewSettingsPath(section: ReviewSettingsSection): string {
  return `/settings/${section}`;
}

// --------------------------------------------------------------- cold load

/** What the cold-load rules may ask about the review and the viewer. */
export interface ReviewFlowFacts {
  /** Whether a verdict has been recorded for this review (R5; SPEC 7.1 P1-3 B). */
  sentRecord: boolean;
  /** A durable queued, cancelled, failed or completed send also earns the Done receipt on a cold reload. */
  flowSendExists?: boolean;
  /** Whether feedback was delivered to the agent while Revision n-1 was the latest (P1-3 C). */
  notesSentToAgent: boolean;
  /** The review's latest revision, counted as people and the address count it (Revision 1 is the first); 0 when unknown. */
  latestRevision: number;
  /** Whether the viewer is the review's second reviewer (R8). */
  secondReviewer: boolean;
}

/** Screens that exist only after a send. A cold load of one the review has not earned is replaced by the Overview (R5). */
export function needsSentRecord(route: ReviewFlowRoute): boolean {
  return route.kind === "done" || route.kind === "rev";
}

/**
 * Whether the review has earned `route` after a send. `/done` needs a verdict.
 * `/rev/<n>` needs feedback delivered to the agent for Revision n-1 and a
 * revision n that exists and is not the first: the prototype opens it only for
 * a send of kind "agent" (P1-3 C). It does not need a verdict.
 */
function sendEarned(route: ReviewFlowRoute, facts: ReviewFlowFacts): boolean {
  if (route.kind === "done") return facts.sentRecord || Boolean(facts.flowSendExists);
  if (route.kind === "rev") {
    return facts.notesSentToAgent && route.revision >= 2 && route.revision <= facts.latestRevision;
  }
  return true;
}

/**
 * The address a cold load of `route` is replaced with, or null to stay.
 *
 * R5: `/done` and `/rev/<n>` the review has not earned go to the Overview. The
 * rule is for a cold load only; a link followed inside the session opens them.
 * R8: `/second` is the second reviewer's alone, cold or not; anyone else goes
 * to the Overview.
 */
export function coldLoadRedirect(
  route: ReviewFlowRoute,
  facts: ReviewFlowFacts,
  options: { cold: boolean },
): ReviewFlowRoute | null {
  if (route.kind === "second" && !facts.secondReviewer) return OVERVIEW_ROUTE;
  if (options.cold && needsSentRecord(route) && !sendEarned(route, facts)) return OVERVIEW_ROUTE;
  return null;
}

/**
 * Whether the page is on one of the redesigned flow's screens below the review
 * (`/r/<review>/one/<part>` and the like) rather than on the review itself. The
 * Reader keeps its own address state to itself there: the route layer owns it.
 */
export function onFlowScreen(): boolean {
  return reviewFlowSuffix(window.location) !== "";
}
