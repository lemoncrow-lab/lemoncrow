import type { ReviewMapLevel, ReviewMapProjection, ReviewMapSelection } from "./types";

/**
 * Where the Review Map stands, mirrored in the Reader URL so Back/Forward walk
 * the drill-down, a reload reopens the same view, and leaving the map for the
 * diff or Ask can come back to it.
 */
export interface ReviewMapLocation {
  level: ReviewMapLevel;
  projection: ReviewMapProjection;
  /** Focus node of the file or symbol level; empty on the change overview. */
  focus: string;
  node: string;
  edge: string;
  /** Selection hint by file path, used only when `node` is not on the graph. */
  path: string;
  /** Change Story open at this step (a node id); absent when the story is closed. */
  story?: string;
  /** Behavioral X-Ray open inside the Change Story (`side~snapshot~node`); absent when closed. */
  xray?: string;
}

const MAP = "map";
const MAP_FOCUS = "map-focus";
const MAP_NODE = "map-node";
const MAP_EDGE = "map-edge";
const MAP_PATH = "map-path";
const MAP_PROJECTION = "map-projection";
const MAP_STORY = "map-story";
const MAP_XRAY = "map-xray";

export const REVIEW_MAP_PARAMS = [MAP, MAP_FOCUS, MAP_NODE, MAP_EDGE, MAP_PATH, MAP_PROJECTION, MAP_STORY, MAP_XRAY] as const;

export const REVIEW_MAP_OVERVIEW: ReviewMapLocation = { level: "architecture", projection: "change_map", focus: "", node: "", edge: "", path: "" };

export function readReviewMapLocation(params: URLSearchParams): ReviewMapLocation | null {
  const raw = params.get(MAP);
  if (raw === null) return null;
  const focus = params.get(MAP_FOCUS) ?? "";
  const level: ReviewMapLevel = (raw === "file" || raw === "symbol") && focus ? raw : "architecture";
  const projection: ReviewMapProjection = params.get(MAP_PROJECTION) === "architecture_delta" ? "architecture_delta" : "change_map";
  const story = level === "architecture" ? params.get(MAP_STORY) ?? "" : "";
  const xray = story ? params.get(MAP_XRAY) ?? "" : "";
  return {
    level,
    projection: level === "architecture" ? projection : "change_map",
    focus: level === "architecture" ? "" : focus,
    node: params.get(MAP_NODE) ?? "",
    edge: params.get(MAP_EDGE) ?? "",
    path: params.get(MAP_PATH) ?? "",
    ...(story ? { story } : {}),
    ...(xray ? { xray } : {}),
  };
}

export function writeReviewMapLocation(params: URLSearchParams, location: ReviewMapLocation | null): URLSearchParams {
  for (const key of REVIEW_MAP_PARAMS) params.delete(key);
  if (!location) return params;
  params.set(MAP, location.level);
  if (location.level === "architecture" && location.projection === "architecture_delta") params.set(MAP_PROJECTION, location.projection);
  if (location.focus) params.set(MAP_FOCUS, location.focus);
  if (location.node) params.set(MAP_NODE, location.node);
  if (location.edge) params.set(MAP_EDGE, location.edge);
  if (location.path) params.set(MAP_PATH, location.path);
  if (location.story && location.level === "architecture") {
    params.set(MAP_STORY, location.story);
    if (location.xray) params.set(MAP_XRAY, location.xray);
  }
  return params;
}

export function reviewMapLocationKey(location: ReviewMapLocation): string {
  return [location.level, location.projection, location.focus, location.node, location.edge, location.path, location.story ?? "", location.xray ?? ""].join("\u0000");
}

/** Same drill-down step; only the selection differs. */
export function sameReviewMapLevel(a: ReviewMapLocation, b: ReviewMapLocation): boolean {
  return a.level === b.level && a.projection === b.projection && a.focus === b.focus;
}

/** The map view an Ask reference (or any persisted selection) points at. */
export function reviewMapLocationFromSelection(selection: ReviewMapSelection): ReviewMapLocation {
  const node = selection.kind === "review_map_node";
  const focus = selection.focus_node_id
    || (node && selection.exactness === "snapshot" ? selection.node_id ?? "" : "");
  const level: ReviewMapLevel = (selection.level === "file" || selection.level === "symbol") && focus ? selection.level : "architecture";
  return {
    level,
    projection: level === "architecture" && selection.projection === "architecture_delta" ? "architecture_delta" : "change_map",
    focus: level === "architecture" ? "" : focus,
    node: node ? selection.node_id ?? "" : "",
    edge: selection.kind === "review_map_edge" ? selection.edge_id ?? "" : "",
    path: node ? selection.path ?? "" : "",
  };
}
