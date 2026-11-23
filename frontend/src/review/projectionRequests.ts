/**
 * Which `/projection` answers a Reader may apply (P9-6).
 *
 * A screen waiting on the plan starts a foreground load; the redesigned Overview also starts a background refresh when
 * the notes load behind it. Both are heavy on the server. The foreground answer used to be dropped because the
 * refresh had started after it, so the Reader waited for two calls. An answer is dropped only when a newer
 * foreground load replaced it (the screen asked again), or a newer plan is already applied (an older answer must
 * never overwrite it); an answer a background refresh merely overtook still counts, and the refresh then lands on top.
 */
export function shouldApplyProjection(request: number, newestForeground: number, newestApplied: number): boolean {
  return request >= newestForeground && request >= newestApplied;
}
