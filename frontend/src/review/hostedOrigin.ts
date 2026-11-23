/**
 * Whether this page is the hosted bundle (hosted-15.1). Its origin has no local pairing claim and no local-only routes
 * such as `/api/review-settings`, even when a hosted preview runs on loopback, so nothing here may ask them. The hosted
 * entry (enterprise `main.tsx`) calls `configureHostedReview()` once, before anything renders.
 */
let hosted = false;

export function configureHostedReview(value = true): void {
  hosted = value;
}

export function isHostedReview(): boolean {
  return hosted;
}
