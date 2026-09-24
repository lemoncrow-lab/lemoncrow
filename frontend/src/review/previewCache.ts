export const RICH_PREVIEW_CACHE_LIMIT = 96;

export function touchCacheEntry<T>(cache: Map<string, T>, key: string): T | undefined {
  const value = cache.get(key);
  if (value === undefined) return undefined;
  cache.delete(key);
  cache.set(key, value);
  return value;
}

export function setBoundedCacheEntry<T>(
  cache: Map<string, T>,
  key: string,
  value: T,
  onEvict?: (value: T) => void,
  limit = RICH_PREVIEW_CACHE_LIMIT,
): void {
  if (cache.has(key)) cache.delete(key);
  cache.set(key, value);
  const boundedLimit = Math.max(1, limit);
  while (cache.size > boundedLimit) {
    const oldest = cache.entries().next().value as [string, T] | undefined;
    if (!oldest) break;
    cache.delete(oldest[0]);
    onEvict?.(oldest[1]);
  }
}
