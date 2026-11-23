import { createContext, useCallback, useContext } from "react";

/**
 * The one table that turns internal codes (degraded reasons, kinds, sources,
 * statuses) into words a reviewer reads. Anything the Review UI prints from a
 * server enum goes through `plainCode`; an unknown code still never shows as
 * snake_case.
 */
const CODE_WORDS: Readonly<Record<string, string>> = {
  // Architecture / code-index degradation.
  current_index_unavailable: "no code index for this repository",
  current_index_checkout_differs_from_snapshot: "your checkout has moved past this revision",
  current_index_generation_differs_from_snapshot: "the code index was built from different code",
  current_index_building: "the code index is still building",
  current_index_stale: "the code index is out of date",
  current_index_missing: "no code index for this repository",
  current_index_failed: "the code index failed to build",
  current_index_pending: "the code index is still building",
  snapshot_architecture_comparison_unavailable: "no before-and-after architecture comparison",
  packet_unavailable: "the change analysis is missing",
  // Review part kinds.
  hunk: "changed lines",
  symbol: "declaration",
  document_section: "top-level code",
  file: "file",
  dependency: "dependency",
  // Evidence kinds.
  playwright_trace: "browser trace",
  live_preview: "live preview",
  vulnerability_scan: "vulnerability scan",
  premerge_check: "pre-merge check",
  sbom: "SBOM",
  // Evidence sources.
  ci: "CI",
  // Check results.
  PASS: "passed",
  FAIL: "failed",
  NOT_RUN: "did not run",
  UNKNOWN: "no result",
  // Attestation classes.
  provider_ci: "provider CI",
  lemoncrow_verification: "LemonCrow check",
  trusted_security_feed: "security feed",
  signed_attestation: "signed attestation",
  human_observation: "seen by a person",
  human_waiver: "waived by a person",
  // Relationship and delta kinds.
  type_reference: "type use",
  not_changed: "unchanged",
};

/** Plain words for an internal code; unknown codes lose their separators instead of printing raw. */
export function plainCode(code: string | null | undefined): string {
  const raw = String(code ?? "").trim();
  if (!raw) return "";
  const known = CODE_WORDS[raw];
  if (known !== undefined) return known;
  if (raw.startsWith("current_index_")) return `code index ${raw.slice("current_index_".length).replace(/[_-]+/g, " ")}`;
  return raw.replace(/[_]+/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
}

/** Several codes as one clause: "a", "a and b", "a, b and c"; repeats collapse. */
export function plainCodeList(codes: readonly string[]): string {
  const words = [...new Set(codes.map(plainCode).filter(Boolean))];
  if (words.length <= 1) return words[0] ?? "";
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/** `plainCode` with its first letter capitalized, for a code that starts a line. */
export function plainCodeSentence(code: string | null | undefined): string {
  const words = plainCode(code);
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
}

/** A review part's stable key ("path::symbol", "path#0") as a reader names that place; never the raw key. */
export function unitKeyLabel(key: string | null | undefined): string {
  const raw = String(key ?? "").trim().replace(/^unit:/, "");
  const [path = "", symbol = ""] = raw.split("::");
  const file = path.replace(/#\d+$/, "");
  const name = symbol.replace(/#\d+$/, "");
  return name ? `${name} in ${file}` : file;
}

/** The principal ids that mean "whoever runs this local Review". */
const LOCAL_PRINCIPALS: ReadonlySet<string> = new Set(["local", "user_local"]);

/** Who the viewer is, so a principal id can read "you"; the Reader provides it from its session. */
export const ViewerContext = createContext<string>("");

export function useViewerId(): string {
  return useContext(ViewerContext);
}

/** Stored display names by principal id: what a host that records names (hosted) supplies; empty locally. */
export type PrincipalNames = Readonly<Record<string, string>>;

export const PrincipalNamesContext = createContext<PrincipalNames>({});

/** Names a principal as a sentence needs it: `(id, fallback?) => label`. */
export type PrincipalLabeler = (id: string | null | undefined, fallback?: string) => string;

/**
 * A person's or agent's name as a reviewer reads it: "you" for the viewer (and
 * the local principal), else the stored display name when `names` has one, else
 * the id's readable part; an opaque id is never printed.
 */
export function principalLabel(
  id: string | null | undefined,
  viewerId: string,
  fallback = "a reviewer",
  names?: PrincipalNames,
): string {
  const raw = String(id ?? "").trim();
  if (!raw) return fallback;
  if (raw === viewerId || LOCAL_PRINCIPALS.has(raw)) return "you";
  const stored = names?.[raw]?.trim();
  if (stored) return stored;
  const name = raw.replace(/^(user|principal|usr|member)[_:-]/i, "");
  // Opaque ids (hex, uuids, long digit runs, provider-prefixed tokens) carry no name.
  if (/^[0-9a-f-]{8,}$/i.test(name) || /\d{6,}/.test(name) || /[:/|]/.test(name)) return fallback;
  return name.replace(/[_]+/g, " ");
}

/** `principalLabel` bound to the Reader's viewer and the host's stored names. */
export function usePrincipalLabel(): PrincipalLabeler {
  const viewerId = useViewerId();
  const names = useContext(PrincipalNamesContext);
  return useCallback((id, fallback) => principalLabel(id, viewerId, fallback, names), [viewerId, names]);
}
