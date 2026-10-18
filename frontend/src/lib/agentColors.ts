/** Shared agent identity colors for System and Review; headers encode AI source separately. */
const ROLE_COLORS: Record<string, string> = {
  code: "purple", explore: "blue", review: "yellow", plan: "cyan",
  execute: "teal", research: "green", test: "green", verifier: "green", solve: "orange", auto: "red",
  bare: "lime", general: "pink",
};

const PALETTE: Record<string, { text: string; background: string }> = {
  purple: { text: "text-violet-300", background: "bg-violet-500/15" },
  blue: { text: "text-blue-200", background: "bg-blue-500/15" },
  yellow: { text: "text-yellow-200", background: "bg-yellow-500/15" },
  cyan: { text: "text-cyan-200", background: "bg-cyan-500/15" },
  teal: { text: "text-teal-200", background: "bg-teal-500/15" },
  green: { text: "text-green-200", background: "bg-green-500/15" },
  orange: { text: "text-orange-200", background: "bg-orange-500/15" },
  red: { text: "text-red-200", background: "bg-red-500/15" },
  lime: { text: "text-lime-200 [.light_&]:text-lime-800", background: "bg-lime-500/15" },
  pink: { text: "text-pink-200 [.light_&]:text-pink-800", background: "bg-pink-500/15" },
};

export function agentColors(role: string, configuredColor?: string): { text: string; background: string } {
  const key = role.trim().toLowerCase().replace(/^lemoncrow[:.]/, "");
  const known = ROLE_COLORS[key] ?? configuredColor;
  if (known && PALETTE[known]) return PALETTE[known];
  let hash = 0;
  for (const character of key) hash = (Math.imul(hash, 31) + character.charCodeAt(0)) >>> 0;
  return Object.values(PALETTE)[hash % Object.keys(PALETTE).length];
}
