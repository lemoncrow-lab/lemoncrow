import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { hostedJson } from "../hostedOnly";
import { PolicyEditBanner } from "./PolicyEditBanner";
import { PolicyPartAsk } from "./PolicyPartAsk";
import {
  askPolicyOwner, policyEditBody, policyEditFiles, POLICY_EDIT_TITLE, policyPartBlock, policyQuestion, resetPolicyStatusesForTests,
  signedByLine, usePolicyGate, usePolicyStatus,
} from "./policyEdit";

vi.mock("../hostedOnly", () => ({ hostedJson: vi.fn() }));
const ask = vi.mocked(hostedJson);
beforeEach(() => { ask.mockReset(); resetPolicyStatusesForTests(); });
afterEach(cleanup);

const blocked = { allowed: false, reason: "You can't approve this: needs Maya Chen (CODEOWNER of .lemoncrow/**)", needs: [{ name: "Maya Chen", handle: "mchen", key: "usr_maya", asked: false }] };
const askedGate = { ...blocked, needs: [{ ...blocked.needs[0], asked: true }] };

describe("the policy-edit banner (SPEC 4.19, POL-1)", () => {
  it("says what the change edits and that it is judged by the base branch's policy, and nothing when it edits none", () => {
    const { rerender } = render(<PolicyEditBanner files={[".lemoncrow/review.yaml"]} gate={null} />);
    expect(screen.getByRole("heading", { name: POLICY_EDIT_TITLE })).toBeTruthy();
    expect(screen.getByTestId("policy-edit-body").textContent).toBe(
      "It changes .lemoncrow/review.yaml. This review is judged against the policy on the base branch, never against the change's own copy.",
    );
    expect(screen.queryByTestId("policy-edit-blocked")).toBeNull();
    rerender(<PolicyEditBanner files={[]} gate={null} />);
    expect(screen.queryByTestId("policy-edit-banner")).toBeNull();
  });

  it("names who may approve when the viewer may not, and keeps silent for the CODEOWNER", () => {
    const { rerender } = render(<PolicyEditBanner files={["CODEOWNERS"]} gate={blocked} />);
    expect(screen.getByTestId("policy-edit-blocked").textContent).toBe(blocked.reason);
    expect(screen.getByText("The policy decides who may weaken it. You can still ask for a change.")).toBeTruthy();
    rerender(<PolicyEditBanner files={["CODEOWNERS"]} gate={{ allowed: true, reason: "", needs: [] }} />);
    expect(screen.queryByTestId("policy-edit-blocked")).toBeNull();
  });

  it("joins several files", () => {
    expect(policyEditBody(["CODEOWNERS", ".lemoncrow/review.yaml"])).toContain("It changes CODEOWNERS, .lemoncrow/review.yaml.");
  });
});

describe("policyEditFiles and usePolicyGate", () => {
  it("reads the Overview's own projection, and an older server's answer as no files", () => {
    expect(policyEditFiles({ policy: { edits_policy: { files: ["CODEOWNERS"] } } } as never)).toEqual(["CODEOWNERS"]);
    expect(policyEditFiles({ policy: { authority: {} } } as never)).toEqual([]);
    expect(policyEditFiles({} as never)).toEqual([]);
  });

  it("asks nothing for a change that edits no policy file", () => {
    renderHook(() => usePolicyGate("r1", []));
    expect(ask).not.toHaveBeenCalled();
  });

  it("asks the review's gate for a policy edit, and a failed read leaves no gate (the server still refuses)", async () => {
    ask.mockResolvedValueOnce({ files: ["CODEOWNERS"], gate: blocked });
    const view = renderHook(() => usePolicyGate("r1", ["CODEOWNERS"]));
    await waitFor(() => expect(view.result.current).toEqual(blocked));
    expect(ask).toHaveBeenCalledWith("/api/reviews/r1/policy-edit");
    ask.mockRejectedValueOnce(new Error("down"));
    const failing = renderHook(() => usePolicyGate("r2", ["CODEOWNERS"]));
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(2));
    expect(failing.result.current).toBeNull();
  });

  it("a local install has no gate: the answer is none", async () => {
    ask.mockResolvedValueOnce(null);
    const view = renderHook(() => usePolicyGate("r1", [".lemoncrow/review.yaml"]));
    await waitFor(() => expect(ask).toHaveBeenCalled());
    expect(view.result.current).toBeNull();
  });
});

describe("asking the CODEOWNER (P9-5, POL-1)", () => {
  it("offers 'Ask Maya Chen to review' and, once asked, 'Asked Maya Chen' for every screen of the review", async () => {
    ask.mockResolvedValueOnce({ files: ["CODEOWNERS"], gate: blocked, questions: [] });
    const hook = renderHook(() => usePolicyGate("r5", ["CODEOWNERS"]));
    await waitFor(() => expect(hook.result.current).toEqual(blocked));
    render(<PolicyEditBanner files={["CODEOWNERS"]} gate={blocked} reviewId="r5" />);
    ask.mockResolvedValueOnce({ result: "requested", gate: askedGate });
    await userEvent.click(screen.getByRole("button", { name: "Ask Maya Chen to review" }));
    expect(ask).toHaveBeenLastCalledWith("/api/reviews/r5/policy-ask", { method: "POST", body: JSON.stringify({ key: "usr_maya" }) });
    await waitFor(() => expect(hook.result.current).toEqual(askedGate));
  });

  it("shows 'Asked Maya Chen' disabled when she already has the request, and no ask button without a person to ask", () => {
    const { rerender } = render(<PolicyEditBanner files={["CODEOWNERS"]} gate={askedGate} reviewId="r5" />);
    const done = screen.getByRole("button", { name: "Asked Maya Chen" }) as HTMLButtonElement;
    expect(done.disabled).toBe(true);
    rerender(<PolicyEditBanner files={["CODEOWNERS"]} gate={{ ...blocked, needs: [] }} reviewId="r5" />);
    expect(screen.queryByTestId("policy-ask")).toBeNull();
  });

  it("tells the CODEOWNER who the approval is signed as", () => {
    const signer = { name: "Maya Chen", handle: "mchen", scope: ".lemoncrow/**" };
    render(<PolicyEditBanner files={["CODEOWNERS"]} gate={{ allowed: true, reason: "", needs: [], signer }} reviewId="r5" />);
    expect(screen.getByTestId("policy-edit-signer").textContent).toBe("Looks good · signed by Maya Chen, CODEOWNER of .lemoncrow/**");
    expect(signedByLine(signer)).toBe("Looks good · signed by Maya Chen, CODEOWNER of .lemoncrow/**");
  });

  it("a policy file's own part is blocked only for a viewer who may not approve it", () => {
    expect(policyPartBlock(".lemoncrow/review.yaml", [".lemoncrow/review.yaml"], blocked)).toBe(blocked.reason);
    expect(policyPartBlock("app/util.py", [".lemoncrow/review.yaml"], blocked)).toBe("");
    expect(policyPartBlock(".lemoncrow/review.yaml", [".lemoncrow/review.yaml"], { allowed: true, reason: "", needs: [] })).toBe("");
    expect(policyPartBlock(".lemoncrow/review.yaml", [".lemoncrow/review.yaml"], null)).toBe("");
  });

  it("the part shows the reason, the note and the ask under its own buttons", async () => {
    ask.mockResolvedValueOnce({ result: "requested", gate: askedGate });
    ask.mockResolvedValueOnce({ files: ["CODEOWNERS"], gate: blocked });
    render(<PolicyPartAsk reviewId="r6" gate={blocked} />);
    expect(screen.getByTestId("policy-part-reason").textContent).toBe(blocked.reason);
    expect(screen.getByText("The policy decides who may weaken it. You can still ask for a change.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Ask Maya Chen to review" })).toBeTruthy();
  });

  it("reads the question the file's diff asks", async () => {
    ask.mockResolvedValueOnce({ files: [".lemoncrow/review.yaml"], gate: blocked, questions: [{ file: ".lemoncrow/review.yaml", text: "Remove `app/payments/**` from `always_decision`?" }] });
    const hook = renderHook(() => usePolicyStatus("r7", [".lemoncrow/review.yaml"]));
    await waitFor(() => expect(hook.result.current).not.toBeNull());
    expect(policyQuestion(hook.result.current, ".lemoncrow/review.yaml")).toBe("Remove `app/payments/**` from `always_decision`?");
    expect(policyQuestion(hook.result.current, "app/util.py")).toBe("");
    expect(policyQuestion(null, ".lemoncrow/review.yaml")).toBe("");
    await act(async () => { await Promise.resolve(); });
    expect(typeof askPolicyOwner).toBe("function");
  });
});

describe("the policy rows on Home (P9-5)", () => {
  it("asks the gate only for rows that edit the policy, and lists one as approvable once it is read", async () => {
    const { usePolicyApprovable } = await import("./policyEdit");
    ask.mockResolvedValueOnce({ files: ["CODEOWNERS"], gate: { allowed: true, reason: "", needs: [] } });
    const rows = [{ id: "plain" }, { id: "policy", edits_policy: true }];
    const view = renderHook(() => usePolicyApprovable(rows));
    await waitFor(() => expect([...view.result.current.approvable]).toEqual(["policy"]));
    expect(ask).toHaveBeenCalledTimes(1);
    expect(ask).toHaveBeenCalledWith("/api/reviews/policy/policy-edit");
  });
});
