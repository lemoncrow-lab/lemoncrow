import type { Annotation, ReviewTarget, RevisionInfo } from "../types";

/** Test builders for the Notes list, the revision menu and the top bar. */
export function note(id: string, extra: Partial<Annotation> = {}): Annotation {
  return {
    id, review_id: "rev", revision_id: "s1", parent_id: "", kind: "comment", state: "open", body: `body ${id}`, created_by: "me",
    created_by_actor: "human", source: "human", created_at: "2026-11-20T10:00:00Z", updated_at: "", anchor_method: "identical_blob",
    anchor_method_label: "Same file", anchor_exact: true, anchor_detail: "", anchored: true, path: "src/a.py", side: "new",
    start_line: 10, end_line: 12, unit_key: "u-a", symbol: "apply", origin_symbol: "",
    ...extra,
  };
}

export function target(id: string, extra: Partial<ReviewTarget> = {}): ReviewTarget {
  return {
    target_id: id, unit_key: `u-${id}`, path: `src/${id}.py`, symbol: id, label: id, start_line: 1, end_line: 20, state: "unreviewed",
    spans: [{ side: "new", start_line: 1, end_line: 20, hunk_ordinal: 0 }],
    ...extra,
  } as ReviewTarget;
}

export function revision(n: number, extra: Partial<RevisionInfo> = {}): RevisionInfo {
  return {
    id: `s${n}`, ref: `rr/s${n}`, revision_number: n, range_mode: "", base_sha: "", head_sha: "", dirty: false, capture_kind: "revision",
    degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "", created_at: "2026-11-20T10:00:00Z",
    ...extra,
  };
}
