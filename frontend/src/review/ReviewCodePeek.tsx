import { useEffect, useMemo, useState } from "react";
import { Code2 } from "lucide-react";

import { api, type CodeMapNode, type CodeMapReferences } from "../api";
import { Button as UiButton } from "../components/ui/button";
import type { CodeTarget } from "../lib/codeLinks";
import { useResolvedProductCapabilities } from "../lib/RuntimeCapabilities";

interface ReviewCodePeekProps {
  projectRoot: string;
  path: string;
  symbol: string;
  line: number;
  /** Primary: go to the location inside this Review, onto the backtrail. */
  onJump?(target: CodeTarget): void;
  /** Secondary: leave for the Code surface. */
  onOpenInCode?(target: CodeTarget): void;
}

function bestDefinition(rows: readonly CodeMapNode[], path: string, symbol: string, line: number): CodeMapNode | null {
  const samePath = rows.filter((row) => row.path === path && row.node_type !== "file");
  const exactName = samePath.find((row) => row.label === symbol || row.qualified_name === symbol || row.qualified_name.endsWith("." + symbol));
  if (exactName) return exactName;
  return [...samePath].sort((a, b) => Math.abs(a.line - line) - Math.abs(b.line - line))[0] ?? null;
}

export default function ReviewCodePeek({ projectRoot, path, symbol, line, onJump, onOpenInCode }: ReviewCodePeekProps) {
  // Code Peek reads the Runtime's /v1/code-map index: until a settled manifest
  // declares the Runtime it renders nothing and requests nothing (isolation §25).
  const runtimeCode = useResolvedProductCapabilities()?.features["runtime.code"] === true;
  const [definition, setDefinition] = useState<CodeMapNode | null>(null);
  const [references, setReferences] = useState<CodeMapReferences | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!runtimeCode || !projectRoot || !symbol) {
      setDefinition(null);
      setReferences(null);
      return () => { cancelled = true; };
    }
    setLoading(true);
    setError("");
    setDefinition(null);
    setReferences(null);
    void api.codeMapSearch(symbol, projectRoot, 12)
      .then(async (payload) => {
        if (cancelled) return;
        const match = bestDefinition(payload.results, path, symbol, line);
        setDefinition(match);
        if (!match) return;
        const refs = await api.codeMapReferences(match.id, projectRoot, 20);
        if (!cancelled) setReferences(refs);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(String(reason instanceof Error ? reason.message : reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [line, path, projectRoot, runtimeCode, symbol]);

  const shownReferences = useMemo(() => references?.references.slice(0, 8) ?? [], [references]);
  // In-Review jump first; Code only when the Review cannot hold the jump.
  const go = onJump ?? onOpenInCode;

  if (!runtimeCode) return null;
  return (
    <section className="border-t border-neutral-900 pt-3" data-testid="review-code-peek">
      <div className="review-kicker mb-1.5">Code Peek</div>
      <p className="text-micro leading-4 text-muted-foreground">
        Current index context · navigation only · not Snapshot-exact Review evidence.
      </p>
      {loading ? (
        <div className="mt-2 text-caption text-muted-foreground">Finding definition and usages…</div>
      ) : error ? (
        <div className="mt-2 text-caption text-amber-300">Current index unavailable.</div>
      ) : !definition ? (
        <div className="mt-2 text-caption text-muted-foreground">No current-index definition matched this Snapshot symbol.</div>
      ) : (
        <div className="mt-2 space-y-2">
          <div className="rounded border border-border/70 px-2 py-1.5">
            <div className="text-caption font-medium text-neutral-300">{definition.qualified_name || definition.label}</div>
            <div className="font-mono text-micro text-muted-foreground">{definition.path}:L{definition.line}</div>
            <div className="mt-1 flex flex-wrap gap-x-3">
              {onJump && (
                <UiButton
                  variant="link"
                  size="compact"
                  type="button"
                  className="px-0 text-caption"
                  onClick={() => onJump({ path: definition.path, line: definition.line, endLine: definition.end_line })}
                >
                  Go to definition
                </UiButton>
              )}
              {onOpenInCode && (
                <UiButton
                  variant="link"
                  size="compact"
                  type="button"
                  className={"px-0 text-caption" + (onJump ? " text-muted-foreground" : "")}
                  onClick={() => onOpenInCode({ path: definition.path, line: definition.line, endLine: definition.end_line })}
                >
                  Open definition in Code
                </UiButton>
              )}
            </div>
          </div>
          <div>
            <div className="text-caption text-neutral-400">
              Usages{references ? " · " + references.reference_count : ""}
            </div>
            {shownReferences.length === 0 ? (
              <div className="mt-1 text-caption text-muted-foreground">No usages found in the current index.</div>
            ) : (
              <div className="mt-1 space-y-0.5">
                {shownReferences.map((reference, index) => {
                  const location = { path: reference.path, line: reference.line, endLine: reference.end_line };
                  return (
                    <div key={reference.path + ":" + reference.line + ":" + index} className="flex items-start gap-1">
                      <UiButton
                        unstyled
                        type="button"
                        onClick={() => go?.(location)}
                        disabled={!go}
                        aria-label={`${onJump ? "Go to" : "Open in Code"} ${reference.path}:L${reference.line}`}
                        className="block min-w-0 flex-1 rounded px-1.5 py-1 text-left hover:bg-muted disabled:cursor-default"
                      >
                        <div className="flex min-w-0 gap-2 text-caption">
                          <span className="min-w-0 flex-1 truncate font-mono text-neutral-300">{reference.path}</span>
                          <span className="shrink-0 font-mono text-muted-foreground">L{reference.line}</span>
                        </div>
                        {reference.snippet && <div className="truncate font-mono text-micro text-muted-foreground">{reference.snippet.trim()}</div>}
                      </UiButton>
                      {onJump && onOpenInCode && (
                        <UiButton
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="mt-0.5 h-5 w-5 shrink-0"
                          onClick={() => onOpenInCode(location)}
                          aria-label={`Open ${reference.path}:L${reference.line} in Code`}
                          tooltip="Open in Code (current index)"
                        >
                          <Code2 size={11} />
                        </UiButton>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
