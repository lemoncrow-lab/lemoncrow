import { useState } from "react";

import { Button } from "../../components/ui/button";
import { showFlowToast } from "../flowToast";
import type { PolicyEditGate } from "./policyEdit";
import {
  askedOwnerLabel, askOwnerLabel, askPolicyOwner, POLICY_EDIT_TITLE, POLICY_GATE_NOTE, policyEditBody, signedByLine,
} from "./policyEdit";

/**
 * The red "This change edits the review policy" banner for the Overview and Wrap up (SPEC 4.19, POL-1). A viewer who
 * may not approve it is told who may and can ask one of them ("Ask Maya Chen to review" → "Asked Maya Chen"); one who
 * may approve it is told who the approval is signed as.
 */
export function PolicyEditBanner({ files, gate, reviewId }: { files: readonly string[]; gate: PolicyEditGate | null; reviewId?: string }) {
  const [busy, setBusy] = useState("");
  if (files.length === 0) return null;
  const blocked = gate !== null && !gate.allowed;
  const ask = async (key: string) => {
    if (!reviewId) return;
    setBusy(key);
    try { await askPolicyOwner(reviewId, key); }
    catch (failure) { showFlowToast({ text: failure instanceof Error && failure.message ? failure.message : "Couldn't ask. Try again.", tone: "alert" }); }
    finally { setBusy(""); }
  };
  return (
    <section aria-label={POLICY_EDIT_TITLE} data-testid="policy-edit-banner" className="rounded-lg border border-p0-line bg-p0-bg p-4 text-p0">
      <h2 className="text-label font-semibold">{POLICY_EDIT_TITLE}</h2>
      <p className="mt-1 text-label" data-testid="policy-edit-body">{policyEditBody(files)}</p>
      {blocked ? (
        <>
          <p className="mt-2 text-label font-semibold" data-testid="policy-edit-blocked">{gate.reason}</p>
          <p className="mt-1 text-meta">{POLICY_GATE_NOTE}</p>
          {reviewId && gate.needs.some((owner) => owner.key) ? (
            <div className="mt-2 flex flex-wrap gap-2">
              {gate.needs.filter((owner) => owner.key).map((owner) => (
                <Button
                  key={owner.key}
                  variant="outline"
                  size="sm"
                 
                  data-testid="policy-ask"
                  disabled={owner.asked === true || busy === owner.key}
                  onClick={() => void ask(owner.key!)}
                >
                  {owner.asked ? askedOwnerLabel(owner.name) : askOwnerLabel(owner.name)}
                </Button>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
      {gate?.allowed && gate.signer ? <p className="mt-2 text-label" data-testid="policy-edit-signer">{signedByLine(gate.signer)}</p> : null}
    </section>
  );
}
