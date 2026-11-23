/**
 * Whether the page is still on the location it loaded with. A cold load is the
 * first location the router renders in this page load (a typed address, a
 * followed link, a reload); once the router moves, every later location, a Back
 * or Forward included, is inside the session (SPEC 3.1 R5; 7.1 P1-3 D).
 *
 * The router's entry key cannot tell them apart: a fresh tab's first entry and
 * every entry the Reader writes with no state are both keyed "default".
 */

interface Seen {
  key: string;
  address: string;
}

let boot: Seen | null = null;
let left = false;
let pathLeft = false;
let bootPath = "";
let current = "";
let previous = "";

/** Called on every render of the app with the router's current location. */
export function noteRouterLocation(location: { key: string; pathname: string; search: string; hash: string }): void {
  const seen = { key: location.key, address: `${location.pathname}${location.search}${location.hash}` };
  if (seen.address !== current) {
    previous = current;
    current = seen.address;
  }
  if (boot === null) {
    boot = seen;
    bootPath = location.pathname;
    return;
  }
  if (location.pathname !== bootPath) pathLeft = true;
  if (!left && (seen.key !== boot.key || seen.address !== boot.address)) left = true;
}

export function isColdLoad(): boolean {
  return boot !== null && !left;
}

/**
 * Whether the page has moved to another path since it loaded. Unlike `isColdLoad` it survives the Reader rewriting its
 * own query and fragment after it loads, which counts as leaving the boot location there.
 */
export function hasLeftBootPath(): boolean {
  return boot !== null && pathLeft;
}

/**
 * Whether the page was on one of this review's own screens (`/r/<review>/…`) just before the current location.
 * The Overview uses it to tell a move inside the review (Back, "Overview") from an arrival (SPEC 4.11, SET-2).
 */
export function cameFromReview(reviewRef: string): boolean {
  const own = `/r/${encodeURIComponent(reviewRef)}`;
  return previous.startsWith(`${own}/`) || previous === own;
}

/** Test seam: forget the recorded location. */
export function resetBootLocationForTest(): void {
  boot = null;
  left = false;
  pathLeft = false;
  bootPath = "";
  current = "";
  previous = "";
}
