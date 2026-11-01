import { lazy, type ComponentType } from "react";

/**
 * A chunk import that tries once more after a short pause before failing. A
 * dropped connection (a laptop changing networks, a Docker interface coming up,
 * which Chromium reports as ERR_NETWORK_CHANGED even on loopback) fails every
 * load in flight once; a reviewer should not have to press Retry for that.
 */
export function importOnceMore<T>(load: () => Promise<T>, pauseMs = 400): () => Promise<T> {
  return () =>
    load().catch(
      () => new Promise<T>((resolve, reject) => {
        setTimeout(() => load().then(resolve, reject), pauseMs);
      }),
    );
}

export interface RetryableLazyHandle<P extends object> {
  current(): ComponentType<P>;
  reset(): void;
}

/**
 * React.lazy permanently remembers a rejected import promise. Keep the lazy
 * component stable until the owner explicitly resets it; an ErrorBoundary can
 * then show the first failure and make its Retry action create a fresh import.
 */
export function retryableLazy<P extends object>(
  load: () => Promise<{ default: ComponentType<P> }>,
): RetryableLazyHandle<P> {
  const attempt = importOnceMore(load);
  let Loaded = lazy(attempt) as unknown as ComponentType<P>;
  return {
    current: () => Loaded,
    reset: () => {
      Loaded = lazy(attempt) as unknown as ComponentType<P>;
    },
  };
}
