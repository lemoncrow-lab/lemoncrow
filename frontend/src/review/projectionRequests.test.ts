import { describe, expect, it } from "vitest";

import { shouldApplyProjection } from "./projectionRequests";

/** The Reader's two refs around `loadProjection`: ids are issued in order, a foreground load records its id. */
function reader() {
  let issued = 0;
  let foreground = 0;
  let applied = 0;
  let ready = false;
  const begin = (background: boolean) => {
    const id = ++issued;
    if (!background) foreground = id;
    return id;
  };
  const land = (id: number) => {
    if (!shouldApplyProjection(id, foreground, applied)) return false;
    applied = id;
    ready = true;
    return true;
  };
  return { begin, land, isReady: () => ready, calls: () => issued };
}

describe("which /projection answers a Reader applies", () => {
  it("is ready from the first answer when a background refresh began behind it (one answer, not two)", () => {
    const r = reader();
    const first = r.begin(false); // the Overview asks for the plan
    const refresh = r.begin(true); // the notes load: the refresh starts while the first is still computing
    expect(r.calls()).toBe(2);
    expect(r.land(first)).toBe(true); // the first answer is applied
    expect(r.isReady()).toBe(true); // ready before the refresh has answered
    expect(r.land(refresh)).toBe(true); // and the refresh lands on top of it
  });

  it("never lets an older answer overwrite a newer plan already on screen", () => {
    const r = reader();
    const first = r.begin(false);
    const refresh = r.begin(true);
    expect(r.land(refresh)).toBe(true);
    expect(r.land(first)).toBe(false);
  });

  it("drops an answer a newer foreground load replaced (the screen asked again)", () => {
    const r = reader();
    const stale = r.begin(false);
    r.begin(false);
    expect(r.land(stale)).toBe(false);
    expect(r.isReady()).toBe(false);
  });

  it("applies a background refresh that no newer request overtook", () => {
    const r = reader();
    const first = r.begin(false);
    r.land(first);
    const refresh = r.begin(true);
    expect(r.land(refresh)).toBe(true);
  });
});
