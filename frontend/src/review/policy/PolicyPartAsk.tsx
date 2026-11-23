import { Button } from "../../components/ui/button";
import { showFlowToast } from "../flowToast";
import type { PolicyEditGate } from "./policyEdit";
import { askedOwnerLabel, askOwnerLabel, askPolicyOwner, POLICY_GATE_NOTE } from "./policyEdit";

/** Under a policy file's own part: why Looks good and Needs a change are off, who may approve it, and the ask (SPEC 4.19). */
export function PolicyPartAsk({ reviewId, gate }: { reviewId: string; gate: PolicyEditGate }) {
  return (
    <div className="mt-2 space-y-2" data-testid="policy-part-block">
      <p id="focus-policy-reason" className="text-meta text-muted-foreground" data-testid="policy-part-reason">{gate.reason}</p>
      <p className="text-meta text-muted-foreground">{POLICY_GATE_NOTE}</p>
      <div className="flex flex-wrap gap-2">
        {gate.needs.filter((owner) => owner.key).map((owner) => (
          <Button
            key={owner.key}
            variant="outline"
            size="sm"
           
            data-testid="policy-part-ask"
            disabled={owner.asked === true}
            onClick={() => void askPolicyOwner(reviewId, owner.key!).catch((failure: unknown) => showFlowToast({ text: failure instanceof Error && failure.message ? failure.message : "Couldn't ask. Try again.", tone: "alert" }))}
          >
            {owner.asked ? askedOwnerLabel(owner.name) : askOwnerLabel(owner.name)}
          </Button>
        ))}
      </div>
    </div>
  );
}
