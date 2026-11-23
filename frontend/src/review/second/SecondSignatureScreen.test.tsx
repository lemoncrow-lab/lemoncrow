import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchTargets, postMark } from "../reviewApi";
import type { ReviewOverview } from "../types";
import { fetchSecondReviewer, signSecond, type SecondReviewerStatus } from "./secondApi";
import { SecondSignatureScreen } from "./SecondSignatureScreen";

vi.mock("../reviewApi", () => ({ fetchTargets: vi.fn(), postMark: vi.fn() }));
vi.mock("./secondApi", () => ({ fetchSecondReviewer: vi.fn(), signSecond: vi.fn() }));
vi.mock("../focus/FocusCode", () => ({ FocusCode: () => <pre data-testid="code">code</pre> }));

const OVERVIEW = {
  title: "Change can_view_order", workspace_root: "/work/shop", session: { id: "review", ref: "shop", status: "open" },
  revision: { id: "rev1", revision_number: 0, head_sha: "4e1b9c2aaaaaaaa" },
} as unknown as ReviewOverview;
const SAM = { id: "usr_sam", name: "Sam Rivera", handle: "srivera" };
const PART = { unit_key: "hun:1", path: "app/auth.py", label: "can_view_order", start_line: 6, end_line: 9 };

function status(over: Partial<SecondReviewerStatus> = {}): SecondReviewerStatus {
  return {
    state: "waiting", role: "second", required: true, unavailable: "", area: "app/auth", decision_count: 1, revision_id: "rev1",
    reviewer: { id: "usr_maya", name: "Maya Chen", handle: "mchen" }, first_reviewer: SAM, first: null, second: null,
    signatures: { have: 1, needed: 2 }, verdict: "waiting",
    parts: [{ ...PART, state: "unreviewed" }], answered: false, agrees: false, first_answers_hidden: true, ...over,
  };
}
const readStatus = vi.mocked(fetchSecondReviewer);

function show() {
  return render(<MemoryRouter><SecondSignatureScreen apiReviewId="review" overview={OVERVIEW} /></MemoryRouter>);
}

beforeEach(() => {
  vi.mocked(fetchTargets).mockReset().mockResolvedValue({ targets: [{ unit_key: "hun:1", path: "app/auth.py" }] } as never);
  vi.mocked(postMark).mockReset().mockResolvedValue({} as never);
  vi.mocked(signSecond).mockReset();
  readStatus.mockReset();
});
afterEach(cleanup);

describe("the second-signature screen (TEAM-4)", () => {
  it("asks her to answer on her own first and shows none of the first reviewer's answers", async () => {
    readStatus.mockResolvedValue(status());
    show();
    expect((await screen.findByTestId("second-title")).textContent).toBe("Answer on your own first.");
    expect(screen.getByTestId("second-eyebrow").textContent).toBe("Second signature · Revision 1 · shop @ 4e1b9c2");
    expect(screen.getByTestId("second-lede").textContent).toBe("Sam reviewed this change. Their answers stay hidden until you answer, so they don't steer yours.");
    expect(screen.getByTestId("second-hidden").textContent).toBe("Sam's answers are hidden until you answer.");
    expect(screen.queryByTestId("second-compare")).toBeNull();
    expect(screen.queryByTestId("second-sign")).toBeNull();
  });

  it("answering saves her own mark and then the comparison appears with Sam's answer and note", async () => {
    readStatus.mockResolvedValueOnce(status());
    show();
    const good = await screen.findByTestId("second-good");
    readStatus.mockResolvedValue(status({
      answered: true, agrees: true, first_answers_hidden: false,
      parts: [{ ...PART, state: "reviewed" }],
      first_answers: [{ unit_key: "hun:1", state: "needs_changes", notes: ["Sam's note"] }],
    }));
    await userEvent.click(good);
    expect(vi.mocked(postMark)).toHaveBeenCalledWith("review", "hun:1", "reviewed", "rev1");
    await waitFor(() => expect(screen.getByTestId("second-title").textContent).toBe("Compare with Sam, then sign."));
    expect(screen.getByTestId("second-you").textContent).toBe("Looks good");
    expect(screen.getByTestId("second-them").textContent).toBe("Needs a change");
    expect(screen.getByTestId("second-their-note").textContent).toBe("Sam's note");
    expect(screen.getByTestId("second-sign").textContent).toBe("Sign · I agree");
  });

  it("signing a disagreement needs her note and says what happens", async () => {
    readStatus.mockResolvedValue(status({
      answered: true, agrees: false, first_answers_hidden: false,
      parts: [{ ...PART, state: "needs_changes" }],
      first_answers: [{ unit_key: "hun:1", state: "reviewed", notes: [] }],
    }));
    show();
    const sign = await screen.findByTestId("second-sign");
    expect(sign.textContent).toBe("Sign · needs a change");
    expect(sign).toBeDisabled();
    vi.mocked(signSecond).mockResolvedValue(status({
      state: "disagrees", answered: true, first_answers_hidden: false,
      parts: [{ ...PART, state: "needs_changes" }],
      first_answers: [{ unit_key: "hun:1", state: "reviewed", notes: [] }],
    }));
    await userEvent.type(screen.getByTestId("second-note"), "Guard it.");
    await userEvent.click(sign);
    expect(vi.mocked(signSecond)).toHaveBeenCalledWith("review", "rev1", "Guard it.");
    await waitFor(() => expect(screen.getByTestId("second-signed").textContent).toContain("The verdict becomes Request changes".replace("The verdict", "the verdict")));
  });

  it("an error is said, never swallowed", async () => {
    readStatus.mockResolvedValue(status({
      answered: true, first_answers_hidden: false, parts: [{ ...PART, state: "reviewed" }],
      first_answers: [{ unit_key: "hun:1", state: "reviewed", notes: [] }],
    }));
    show();
    vi.mocked(signSecond).mockRejectedValue(new Error("the review moved to a newer revision; reload before signing"));
    await userEvent.click(await screen.findByTestId("second-sign"));
    expect((await screen.findByTestId("second-error")).textContent).toContain("newer revision");
  });
});
