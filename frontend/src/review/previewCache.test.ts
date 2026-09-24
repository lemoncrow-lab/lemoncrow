import { describe, expect, it, vi } from "vitest";

import { setBoundedCacheEntry, touchCacheEntry } from "./previewCache";

describe("previewCache", () => {
  it("evicts the least-recently-used entry at the bound", () => {
    const cache = new Map<string, string>();
    const onEvict = vi.fn();
    setBoundedCacheEntry(cache, "a", "A", onEvict, 2);
    setBoundedCacheEntry(cache, "b", "B", onEvict, 2);
    expect(touchCacheEntry(cache, "a")).toBe("A");
    setBoundedCacheEntry(cache, "c", "C", onEvict, 2);

    expect([...cache.keys()]).toEqual(["a", "c"]);
    expect(onEvict).toHaveBeenCalledWith("B");
  });

  it("replacing an entry does not evict it", () => {
    const cache = new Map<string, string>();
    const onEvict = vi.fn();
    setBoundedCacheEntry(cache, "a", "A", onEvict, 1);
    setBoundedCacheEntry(cache, "a", "A2", onEvict, 1);
    expect(cache.get("a")).toBe("A2");
    expect(onEvict).not.toHaveBeenCalled();
  });
});
