/**
 * The Brief's Evidence card: each recorded check with its result, detail and who recorded it, each claim beside what
 * shows it, the regressions across revisions, and where AI review stands. Read-only: nothing here runs anything.
 */
import { useState } from "react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Label } from "../overview/OverviewParts";
import type { EvidenceRow, ReviewProjectionClaim } from "../types";
import { checkSource } from "./briefModel";

/** Rows each list shows before "Show all". */
const SHOWN = 6;

const CHECK_STATUS: Readonly<Record<string, { label: string; tone: "success" | "p0" | "warning" }>> = {
  PASS: { label: "Pass", tone: "success" },
  FAIL: { label: "Fail", tone: "p0" },
  NOT_RUN: { label: "Did not run", tone: "warning" },
  UNKNOWN: { label: "No result", tone: "warning" },
};

const CLAIM_TONE: Readonly<Record<string, string>> = { contradictory: "text-p0", none: "text-p1", partial: "text-p1", inferred_failing: "text-p1" };

export interface BriefEvidenceProps {
  checks: readonly EvidenceRow[];
  claims: readonly ReviewProjectionClaim[];
  regressions: readonly { key: string; text: string }[];
  ai: string;
  /** The parts this review has: a claim on one of them opens it. */
  known: ReadonlySet<string>;
  onOpenTarget: (targetId: string) => void;
}

export function hasBriefEvidence(props: Pick<BriefEvidenceProps, "checks" | "claims" | "regressions" | "ai">): boolean {
  return props.checks.length + props.claims.length + props.regressions.length > 0 || Boolean(props.ai);
}

export default function BriefEvidence({ checks, claims, regressions, ai, known, onOpenTarget }: BriefEvidenceProps) {
  const [all, setAll] = useState(false);
  const shownChecks = all ? checks : checks.slice(0, SHOWN);
  const shownClaims = all ? claims : claims.slice(0, SHOWN);
  const hidden = checks.length - shownChecks.length + claims.length - shownClaims.length;
  return (
    <>
      {regressions.length > 0 ? (
        <ul className="flex flex-col gap-0.5 text-caption text-p0" data-testid="brief-regressions">
          {regressions.map((row) => <li key={row.key}>{row.text}</li>)}
        </ul>
      ) : null}
      {checks.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <Label>{`Checks · ${checks.length}`}</Label>
          <ul className="flex flex-col gap-1.5" data-testid="brief-evidence-checks">
            {shownChecks.map((row, index) => {
              const status = CHECK_STATUS[row.status] ?? CHECK_STATUS.UNKNOWN;
              const source = checkSource(row.source);
              return (
                <li key={`${row.name}:${row.source}:${index}`} className="flex min-w-0 flex-col gap-0.5" data-testid="brief-evidence-check">
                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <Badge tone={status.tone}>{status.label}</Badge>
                    <span className="break-all font-mono text-label text-foreground">{row.name}</span>
                    {source ? <span className="text-meta text-muted-foreground">{`· ${source}`}</span> : null}
                  </span>
                  {row.detail.trim() ? <span className="break-words text-meta text-muted-foreground">{row.detail}</span> : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      {claims.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <Label>{`Claims · ${claims.length}`}</Label>
          <ul className="flex flex-col gap-1.5" data-testid="brief-evidence-claims">
            {shownClaims.map((claim) => {
              const target = claim.target_ids.find((id) => known.has(id));
              const statement = claim.statement || claim.id;
              return (
                <li key={claim.id} className="flex min-w-0 flex-col gap-0.5" data-testid="brief-evidence-claim">
                  {target ? (
                    <Button unstyled type="button" className="min-w-0 text-left text-label text-foreground underline-offset-2 hover:underline" onClick={() => onOpenTarget(target)}>
                      {statement}
                    </Button>
                  ) : <span className="text-label text-foreground">{statement}</span>}
                  <span className="flex flex-wrap items-baseline gap-x-2 text-meta">
                    <span className={CLAIM_TONE[claim.summary] ?? "text-subtle"}>{claim.summary_text}</span>
                    {claim.result_line ? <span className="text-muted-foreground">{claim.result_line}</span> : null}
                  </span>
                  {claim.limitations.map((text) => <span key={text} className="text-meta text-p1">{text}</span>)}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      {hidden > 0 ? (
        <Button variant="ghost" size="sm" className="self-start" data-testid="brief-evidence-show-all" onClick={() => setAll(true)}>{`Show all · ${hidden} more`}</Button>
      ) : null}
      {ai ? <p className="text-caption text-muted-foreground" data-testid="brief-ai-review">{ai}</p> : null}
    </>
  );
}
