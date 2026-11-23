import { afterEach, describe, expect, it } from "vitest";

import { isColdLoad, noteRouterLocation, resetBootLocationForTest } from "./reviewBootLocation";

const at = (pathname: string, key = "default", search = "", hash = "") => ({ key, pathname, search, hash });

describe("cold load", () => {
  afterEach(resetBootLocationForTest);

  it("is nothing until the app renders its first location", () => {
    expect(isColdLoad()).toBe(false);
  });

  it("is the first location of the page load, however often it renders", () => {
    noteRouterLocation(at("/r/shop/done"));
    noteRouterLocation(at("/r/shop/done"));
    expect(isColdLoad()).toBe(true);
  });

  it("ends when the router moves, and a later Back to the first entry is not cold", () => {
    noteRouterLocation(at("/r/shop"));
    noteRouterLocation(at("/r/shop/done", "k1"));
    expect(isColdLoad()).toBe(false);
    noteRouterLocation(at("/r/shop"));
    expect(isColdLoad()).toBe(false);
  });

  it("is not fooled by entries that share the key \"default\" (the Reader writes them with no state)", () => {
    noteRouterLocation(at("/r/shop"));
    noteRouterLocation(at("/r/shop/done", "default", "?view=brief"));
    expect(isColdLoad()).toBe(false);
    noteRouterLocation(at("/r/shop"));
    expect(isColdLoad()).toBe(false);
  });

  it("ends for a move that changes only the fragment", () => {
    noteRouterLocation(at("/r/shop/all", "default", "", "#a"));
    noteRouterLocation(at("/r/shop/all", "default", "", "#b"));
    expect(isColdLoad()).toBe(false);
  });
});
