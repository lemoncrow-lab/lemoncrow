import { afterEach, describe, expect, it } from "vitest";

import {
  REVIEW_SETTINGS_SECTIONS,
  buildReviewFlowPath,
  buildReviewSettingsPath,
  coldLoadRedirect,
  keepFlowScreen,
  legacyFlowRedirect,
  needsSentRecord,
  onFlowScreen,
  parseReviewFlowLocation,
  parseReviewSettingsPath,
  readerSearchHref,
  reviewFlowSuffix,
  type ReviewFlowFacts,
  type ReviewFlowRoute,
} from "./reviewRoutes";

const at = (pathname: string, search = "", hash = "") => parseReviewFlowLocation({ pathname, search, hash });

describe("parseReviewFlowLocation: SPEC 3.1 routes", () => {
  const cases: Array<[string, string, string, ReviewFlowRoute]> = [
    ["/r/shop", "", "", { kind: "overview" }],
    ["/r/shop/one", "", "", { kind: "one", part: "" }],
    ["/r/shop/one/can_view_order", "", "", { kind: "one", part: "can_view_order" }],
    ["/r/shop/one/order_total", "", "", { kind: "one", part: "order_total" }],
    ["/r/shop/all", "", "", { kind: "all", part: "" }],
    ["/r/shop/all", "", "#order_total", { kind: "all", part: "order_total" }],
    ["/r/shop/map", "?lens=dependencies&view=after", "", { kind: "map", lens: "dependencies", view: "after" }],
    ["/r/shop/brief", "", "", { kind: "brief" }],
    ["/r/shop/xray", "", "", { kind: "xray", at: "" }],
    ["/r/shop/xray", "?at=after~r%40v~file%3Aa", "", { kind: "xray", at: "after~r@v~file:a" }],
    ["/r/shop/map", "?lens=data-path&view=before", "", { kind: "map", lens: "data-path", view: "before" }],
    ["/r/shop/walkthrough", "", "", { kind: "walkthrough", step: "", view: "steps" }],
    ["/r/shop/walkthrough/api%20contract", "", "", { kind: "walkthrough", step: "api contract", view: "steps" }],
    ["/r/shop/walkthrough", "?view=map", "", { kind: "walkthrough", step: "", view: "map" }],
    // The walkthrough's former addresses read as the Walkthrough (and are replaced with its own, below).
    ["/r/shop/tour", "", "", { kind: "walkthrough", step: "", view: "steps" }],
    ["/r/shop/tour/api%20contract", "", "", { kind: "walkthrough", step: "api contract", view: "steps" }],
    ["/r/shop", "?view=tour&guided=api", "", { kind: "walkthrough", step: "api", view: "steps" }],
    ["/r/shop", "?map=architecture&map-story=file%3Aa", "", { kind: "walkthrough", step: "", view: "map" }],
    ["/r/shop/preview", "", "", { kind: "preview", surface: "" }],
    ["/r/shop/preview/get-orders-7", "", "", { kind: "preview", surface: "get-orders-7" }],
    ["/r/shop/wrap", "", "", { kind: "wrap" }],
    ["/r/shop/done", "", "", { kind: "done" }],
    ["/r/shop/rev/2", "", "", { kind: "rev", revision: 2 }],
    ["/r/shop/second", "", "", { kind: "second" }],
  ];
  it.each(cases)("%s%s%s", (pathname, search, hash, route) => {
    expect(at(pathname, search, hash)).toEqual({ reviewRef: "shop", route, known: true });
  });

  it("falls back to the first lens and view when the map query is missing or wrong", () => {
    expect(at("/r/shop/map")?.route).toEqual({ kind: "map", lens: "data-path", view: "changes" });
    expect(at("/r/shop/map", "?lens=nope&view=sideways")?.route).toEqual({ kind: "map", lens: "data-path", view: "changes" });
    expect(at("/r/shop/map", "?lens=dependencies")?.route).toEqual({ kind: "map", lens: "dependencies", view: "changes" });
  });

  it("decodes the review, the part and the surface", () => {
    expect(at("/r/a%2Fb/one/get%20orders")).toEqual({
      reviewRef: "a/b",
      route: { kind: "one", part: "get orders" },
      known: true,
    });
    expect(at("/r/shop/all", "", "#a%20b")?.route).toEqual({ kind: "all", part: "a b" });
    expect(at("/r/shop/one/%E0%A4%A")?.route).toEqual({ kind: "one", part: "%E0%A4%A" });
  });

  it("treats one trailing slash as the same address", () => {
    expect(at("/r/shop/")).toMatchObject({ route: { kind: "overview" }, known: true });
    expect(at("/r/shop/wrap/")).toMatchObject({ route: { kind: "wrap" }, known: true });
    expect(at("/r/shop/one/order_total/")).toMatchObject({ route: { kind: "one", part: "order_total" }, known: true });
  });

  it("reports an unknown path under a review as the Overview, flagged unknown", () => {
    for (const pathname of [
      "/r/shop/nope",
      "/r/shop/wrap/extra",
      "/r/shop/done/1",
      "/r/shop/second/1",
      "/r/shop/map/1",
      "/r/shop/all/x",
      "/r/shop/one/a/b",
      "/r/shop/tour/a/b",
      "/r/shop/walkthrough/a/b",
      "/r/shop/one//",
      "/r/shop/one/x//",
      "/r/shop//wrap",
      "/r/shop/rev",
      "/r/shop/rev/0",
      "/r/shop/rev/-1",
      "/r/shop/rev/x",
      "/r/shop/rev/2/3",
      "/r/shop/rev/1234567890",
    ]) {
      expect(at(pathname), pathname).toEqual({ reviewRef: "shop", route: { kind: "overview" }, known: false });
    }
  });

  it("leaves addresses that are not flow screens to their own readers", () => {
    for (const pathname of ["/", "/home", "/reviews", "/r", "/r/", "/rr/abc", "/reviews/abc/one", "/r/x/a..b", "/r/shop/compare/1..2", "/settings/reading"]) {
      expect(at(pathname), pathname).toBeNull();
    }
  });
});

describe("legacyFlowRedirect: the walkthrough's former addresses", () => {
  const from = (pathname: string, search = "") => legacyFlowRedirect({ pathname, search });
  it("replaces /tour, the Classic Guided review and the Classic Change Story with /walkthrough", () => {
    expect(from("/r/shop/tour")).toBe("/r/shop/walkthrough");
    expect(from("/r/shop/tour/")).toBe("/r/shop/walkthrough");
    expect(from("/r/shop/tour/api%20contract")).toBe("/r/shop/walkthrough/api%20contract");
    expect(from("/r/shop", "?view=tour")).toBe("/r/shop/walkthrough");
    expect(from("/r/shop", "?view=tour&guided=api")).toBe("/r/shop/walkthrough/api");
    expect(from("/r/shop", "?map=architecture&map-story=file%3Aa&map-xray=x")).toBe("/r/shop/walkthrough?view=map");
  });
  it("leaves every other address alone, the Walkthrough's own included", () => {
    for (const [pathname, search] of [
      ["/r/shop/walkthrough", ""], ["/r/shop/walkthrough/api", ""], ["/r/shop/walkthrough", "?view=map"],
      ["/r/shop", ""], ["/r/shop", "?view=map"], ["/r/shop/map", "?lens=dependencies&view=changes"], ["/r/shop/tour/a/b", ""], ["/home", ""],
    ]) {
      expect(from(pathname, search), pathname + search).toBeNull();
    }
  });
});

describe("buildReviewFlowPath", () => {
  const routes: ReviewFlowRoute[] = [
    { kind: "overview" },
    { kind: "one", part: "" },
    { kind: "one", part: "can_view_order" },
    { kind: "one", part: "get orders/7" },
    { kind: "all", part: "" },
    { kind: "all", part: "order_total" },
    { kind: "map", lens: "dependencies", view: "after" },
    { kind: "brief" },
    { kind: "xray", at: "" },
    { kind: "xray", at: "after~r@v~file:a b" },
    { kind: "map", lens: "data-path", view: "changes" },
    { kind: "walkthrough", step: "", view: "steps" },
    { kind: "walkthrough", step: "cohort:api/v2", view: "steps" },
    { kind: "walkthrough", step: "api", view: "map" },
    { kind: "preview", surface: "" },
    { kind: "preview", surface: "plans-csv" },
    { kind: "wrap" },
    { kind: "done" },
    { kind: "rev", revision: 2 },
    { kind: "second" },
  ];

  it("writes the addresses of SPEC 3.1", () => {
    expect(buildReviewFlowPath("shop", { kind: "overview" })).toBe("/r/shop");
    expect(buildReviewFlowPath("shop", { kind: "one", part: "" })).toBe("/r/shop/one");
    expect(buildReviewFlowPath("shop", { kind: "one", part: "order_total" })).toBe("/r/shop/one/order_total");
    expect(buildReviewFlowPath("shop", { kind: "all", part: "order_total" })).toBe("/r/shop/all#order_total");
    expect(buildReviewFlowPath("shop", { kind: "map", lens: "dependencies", view: "after" })).toBe("/r/shop/map?lens=dependencies&view=after");
    expect(buildReviewFlowPath("shop", { kind: "walkthrough", step: "", view: "steps" })).toBe("/r/shop/walkthrough");
    expect(buildReviewFlowPath("shop", { kind: "walkthrough", step: "api", view: "steps" })).toBe("/r/shop/walkthrough/api");
    expect(buildReviewFlowPath("shop", { kind: "walkthrough", step: "", view: "map" })).toBe("/r/shop/walkthrough?view=map");
    expect(buildReviewFlowPath("shop", { kind: "preview", surface: "pricing" })).toBe("/r/shop/preview/pricing");
    expect(buildReviewFlowPath("shop", { kind: "wrap" })).toBe("/r/shop/wrap");
    expect(buildReviewFlowPath("shop", { kind: "done" })).toBe("/r/shop/done");
    expect(buildReviewFlowPath("shop", { kind: "rev", revision: 2 })).toBe("/r/shop/rev/2");
    expect(buildReviewFlowPath("shop", { kind: "second" })).toBe("/r/shop/second");
  });

  it.each(routes)("parse(build(route)) returns the route: %j", (route) => {
    const built = buildReviewFlowPath("a b/c", route);
    const url = new URL(built, "http://localhost");
    expect(parseReviewFlowLocation({ pathname: url.pathname, search: url.search, hash: url.hash })).toEqual({
      reviewRef: "a b/c",
      route,
      known: true,
    });
  });
});

describe("reviewFlowSuffix", () => {
  it("is what follows the review in a known flow address", () => {
    expect(reviewFlowSuffix({ pathname: "/r/shop/one/order_total" })).toBe("/one/order_total");
    expect(reviewFlowSuffix({ pathname: "/r/shop/wrap" })).toBe("/wrap");
    expect(reviewFlowSuffix({ pathname: "/r/shop/rev/2" })).toBe("/rev/2");
    expect(reviewFlowSuffix({ pathname: "/r/shop/one/a%20b" })).toBe("/one/a%20b");
  });
  it("is empty for the Overview and for anything not a flow screen", () => {
    for (const pathname of ["/r/shop", "/r/shop/", "/r/shop/nope", "/rr/abc", "/reviews/abc", "/r/shop/compare/1..2", "/home"]) {
      expect(reviewFlowSuffix({ pathname }), pathname).toBe("");
    }
  });
});

describe("keepFlowScreen", () => {
  const origin = "http://127.0.0.1:7420";
  const keep = (canonical: string, pathname: string, hash = "") => {
    const url = new URL(canonical, origin);
    const suffix = keepFlowScreen(url, { pathname, hash });
    return { href: `${url.pathname}${url.search}${url.hash}`, suffix };
  };

  it("keeps the screen and the fragment through the Reader's rewrite of the review part", () => {
    expect(keep("/r/abc", "/r/shop/one/order_total")).toEqual({ href: "/r/abc/one/order_total", suffix: "/one/order_total" });
    expect(keep("/r/abc", "/r/shop/all", "#order_total")).toEqual({ href: "/r/abc/all#order_total", suffix: "/all" });
    expect(keep("/r/abc?base=1&target=1", "/r/shop/wrap")).toEqual({ href: "/r/abc/wrap?base=1&target=1", suffix: "/wrap" });
  });
  it("keeps the Map's lens and view as typed, and adds none that were not", () => {
    const withQuery = new URL("/r/abc", origin);
    keepFlowScreen(withQuery, { pathname: "/r/shop/map", search: "?lens=dependencies&view=after&other=1", hash: "" });
    expect(`${withQuery.pathname}${withQuery.search}`).toBe("/r/abc/map?lens=dependencies&view=after");
    const bare = new URL("/r/abc", origin);
    keepFlowScreen(bare, { pathname: "/r/shop/map", search: "", hash: "" });
    expect(`${bare.pathname}${bare.search}`).toBe("/r/abc/map");
  });
  it("changes nothing for the Overview, an unknown path or another reader's address", () => {
    for (const pathname of ["/r/shop", "/r/shop/nope", "/reviews/abc", "/rr/abc", "/r/shop/compare/1..2"]) {
      expect(keep("/r/abc", pathname, "#x"), pathname).toEqual({ href: "/r/abc", suffix: "" });
    }
  });
});

describe("readerSearchHref", () => {
  const here = { pathname: "/r/shop/all", hash: "#order_total" };
  it("keeps the fragment of a flow screen when the Reader writes its query", () => {
    expect(readerSearchHref(here, "view=brief", true)).toBe("/r/shop/all?view=brief#order_total");
    expect(readerSearchHref(here, "", true)).toBe("/r/shop/all#order_total");
  });
  it("writes no fragment off a flow screen, as the Classic Reader always has", () => {
    expect(readerSearchHref({ pathname: "/r/shop", hash: "#t=t&r=shop" }, "view=brief", false)).toBe("/r/shop?view=brief");
    expect(readerSearchHref({ pathname: "/r/shop", hash: "#t=t" }, "", false)).toBe("/r/shop");
  });
});

describe("settings sections", () => {
  it("lists the sections of SPEC 3.1 and the adopted Organization route", () => {
    expect([...REVIEW_SETTINGS_SECTIONS]).toEqual(["reading", "ai", "notifications", "keyboard", "look", "more", "policy", "org"]);
  });
  it("parses and builds /settings/<section>", () => {
    for (const section of REVIEW_SETTINGS_SECTIONS) {
      expect(parseReviewSettingsPath(`/settings/${section}`)).toBe(section);
      expect(buildReviewSettingsPath(section)).toBe(`/settings/${section}`);
    }
    expect(parseReviewSettingsPath("/settings/reading/")).toBe("reading");
  });
  it("leaves the shell's own settings pages alone", () => {
    for (const pathname of ["/settings", "/settings/products", "/settings/devices", "/settings/editor", "/settings/reading/x", "/system/reading", "/r/shop/settings/reading"]) {
      expect(parseReviewSettingsPath(pathname), pathname).toBeNull();
    }
  });
});

describe("cold-load rules", () => {
  const facts = (over: Partial<ReviewFlowFacts> = {}): ReviewFlowFacts => ({
    sentRecord: false,
    notesSentToAgent: false,
    latestRevision: 1,
    secondReviewer: false,
    ...over,
  });
  const nothingSent = facts();
  const sent = facts({ sentRecord: true });
  const changesSent = facts({ notesSentToAgent: true, latestRevision: 2 });
  const overview: ReviewFlowRoute = { kind: "overview" };
  const rev2: ReviewFlowRoute = { kind: "rev", revision: 2 };

  it("R5: a cold /done or /rev/<n> with nothing sent is replaced by the Overview", () => {
    expect(coldLoadRedirect({ kind: "done" }, nothingSent, { cold: true })).toEqual(overview);
    expect(coldLoadRedirect(rev2, nothingSent, { cold: true })).toEqual(overview);
  });
  it("R5: /done stays with a verdict recorded", () => {
    expect(coldLoadRedirect({ kind: "done" }, sent, { cold: true })).toBeNull();
  });
  it("R5 (P1-3 C): /rev/<n> needs feedback delivered to the agent and a revision n after the first that exists; no verdict", () => {
    const delivered = facts({ notesSentToAgent: true, latestRevision: 2 });
    expect(coldLoadRedirect(rev2, delivered, { cold: true })).toBeNull();
    expect(coldLoadRedirect({ kind: "rev", revision: 3 }, { ...delivered, latestRevision: 3 }, { cold: true })).toBeNull();
    // a verdict alone does not say the agent heard anything
    expect(coldLoadRedirect(rev2, { ...sent, latestRevision: 2 }, { cold: true })).toEqual(overview);
    expect(coldLoadRedirect(rev2, { ...changesSent, notesSentToAgent: false }, { cold: true })).toEqual(overview);
    // revision n does not exist yet
    expect(coldLoadRedirect(rev2, { ...delivered, latestRevision: 1 }, { cold: true })).toEqual(overview);
    expect(coldLoadRedirect({ kind: "rev", revision: 3 }, delivered, { cold: true })).toEqual(overview);
    expect(coldLoadRedirect(rev2, { ...delivered, latestRevision: 0 }, { cold: true })).toEqual(overview);
    // the first revision is Done's, not Welcome back's
    expect(coldLoadRedirect({ kind: "rev", revision: 1 }, delivered, { cold: true })).toEqual(overview);
  });
  it("R5: a link followed inside the session opens them", () => {
    expect(coldLoadRedirect({ kind: "done" }, nothingSent, { cold: false })).toBeNull();
    expect(coldLoadRedirect(rev2, nothingSent, { cold: false })).toBeNull();
  });
  it("R5 reaches only the screens that exist after a send", () => {
    const others: ReviewFlowRoute[] = [
      overview,
      { kind: "one", part: "" },
      { kind: "one", part: "order_total" },
      { kind: "all", part: "" },
      { kind: "map", lens: "data-path", view: "changes" },
      { kind: "preview", surface: "pricing" },
      { kind: "wrap" },
    ];
    for (const route of others) {
      expect(needsSentRecord(route), route.kind).toBe(false);
      expect(coldLoadRedirect(route, nothingSent, { cold: true }), route.kind).toBeNull();
    }
    expect(needsSentRecord({ kind: "done" })).toBe(true);
    expect(needsSentRecord({ kind: "rev", revision: 3 })).toBe(true);
  });
  it("R8: /second is the second reviewer's alone, cold or not", () => {
    for (const cold of [true, false]) {
      expect(coldLoadRedirect({ kind: "second" }, nothingSent, { cold })).toEqual(overview);
      expect(coldLoadRedirect({ kind: "second" }, changesSent, { cold })).toEqual(overview);
      expect(coldLoadRedirect({ kind: "second" }, facts({ secondReviewer: true }), { cold })).toBeNull();
    }
  });
  it("R8: being the second reviewer opens nothing else", () => {
    const second = facts({ secondReviewer: true });
    expect(coldLoadRedirect({ kind: "done" }, second, { cold: true })).toEqual(overview);
    expect(coldLoadRedirect({ kind: "wrap" }, second, { cold: true })).toBeNull();
  });
});

describe("onFlowScreen", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("is true on the flow's nested routes", () => {
    window.history.replaceState(null, "", "/r/shop/one/order_total");
    expect(onFlowScreen()).toBe(true);
  });

  it("is false on the review itself and on unsupported review paths", () => {
    for (const address of ["/r/shop", "/r/shop?view=brief", "/r/shop/nope", "/rr/abc", "/reviews/abc/revisions/2", "/r/shop/compare/1..2"]) {
      window.history.replaceState(null, "", address);
      expect(onFlowScreen(), address).toBe(false);
    }
  });
});
