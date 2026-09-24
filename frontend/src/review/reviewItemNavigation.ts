export type DedicatedReviewItem =
  | { kind: "file"; path: string }
  | { kind: "surface"; provider: string; id: string };

const ITEM_KIND = "review-item";
const ITEM_PATH = "review-path";
const ITEM_PROVIDER = "review-provider";
const ITEM_SURFACE = "review-surface";

export function readDedicatedReviewItem(): DedicatedReviewItem | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const kind = params.get(ITEM_KIND);
  if (kind === "file") {
    const path = params.get(ITEM_PATH) ?? "";
    return path ? { kind: "file", path } : null;
  }
  if (kind === "surface") {
    const provider = params.get(ITEM_PROVIDER) ?? "";
    const id = params.get(ITEM_SURFACE) ?? "";
    return provider && id ? { kind: "surface", provider, id } : null;
  }
  return null;
}

export function dedicatedReviewItemUrl(item: DedicatedReviewItem): string {
  const url = new URL(window.location.href);
  url.searchParams.delete(ITEM_PATH);
  url.searchParams.delete(ITEM_PROVIDER);
  url.searchParams.delete(ITEM_SURFACE);
  url.searchParams.set(ITEM_KIND, item.kind);
  if (item.kind === "file") {
    url.searchParams.set(ITEM_PATH, item.path);
  } else {
    url.searchParams.set(ITEM_PROVIDER, item.provider);
    url.searchParams.set(ITEM_SURFACE, item.id);
  }
  return url.toString();
}

export function openDedicatedReviewItem(item: DedicatedReviewItem): void {
  window.open(dedicatedReviewItemUrl(item), "_blank", "noopener,noreferrer");
}
