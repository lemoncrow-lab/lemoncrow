import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FEEDBACK_MAIL, SendFeedbackDialog, feedbackMailto } from "./SendFeedbackDialog";

const api = vi.hoisted(() => ({ authenticatedFetch: vi.fn() }));
vi.mock("../reviewApi", () => api);

const answer = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function mount() {
  const onOpenChange = vi.fn();
  render(<SendFeedbackDialog open onOpenChange={onOpenChange} />);
  return onOpenChange;
}

afterEach(() => { cleanup(); api.authenticatedFetch.mockReset(); });

describe("Send feedback", () => {
  it("posts the message, the optional email and the source the server accepts, and says Sent", async () => {
    api.authenticatedFetch.mockResolvedValue(answer(200, { ok: true, delivered: "remote" }));
    const user = userEvent.setup();
    mount();
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Feedback" }), "  The Map is great  ");
    await user.type(screen.getByRole("textbox", { name: "Contact email (optional)" }), "me@example.com");
    await user.click(send);
    expect(await screen.findByTestId("feedback-sent")).toHaveTextContent("Sent. Thank you.");
    expect(api.authenticatedFetch).toHaveBeenCalledTimes(1);
    const [path, init] = api.authenticatedFetch.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/feedback");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ message: "The Map is great", email: "me@example.com", source: "review" });
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("keeps the message and offers the mail fallback when the server could not send it", async () => {
    const user = userEvent.setup();
    for (const failure of [
      () => api.authenticatedFetch.mockResolvedValue(answer(502, { detail: "feedback_not_delivered (network)" })),
      () => api.authenticatedFetch.mockRejectedValue(new TypeError("Failed to fetch")),
      () => api.authenticatedFetch.mockResolvedValue(answer(200, { ok: true, delivered: "failed" })),
    ]) {
      failure();
      mount();
      await user.type(screen.getByRole("textbox", { name: "Feedback" }), "Please add X");
      await user.click(screen.getByRole("button", { name: "Send" }));
      const alert = await screen.findByTestId("feedback-failed");
      const link = screen.getByRole("link", { name: `email it to ${FEEDBACK_MAIL}` });
      expect(link).toHaveAttribute("href", feedbackMailto("Please add X", ""));
      expect(alert).toHaveTextContent("Your message is still here");
      expect(screen.getByRole("textbox", { name: "Feedback" })).toHaveValue("Please add X");
      expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
      cleanup();
    }
  });

  it("says Sending while the request runs and sends once", async () => {
    let resolve: (value: Response) => void = () => undefined;
    api.authenticatedFetch.mockImplementation(() => new Promise<Response>((done) => { resolve = done; }));
    const user = userEvent.setup();
    mount();
    await user.type(screen.getByRole("textbox", { name: "Feedback" }), "Hi");
    await user.click(screen.getByRole("button", { name: "Send" }));
    const sending = screen.getByRole("button", { name: "Sending…" });
    expect(sending).toBeDisabled();
    await user.click(sending);
    expect(api.authenticatedFetch).toHaveBeenCalledTimes(1);
    resolve(answer(200, { delivered: "remote" }));
    await waitFor(() => expect(screen.getByTestId("feedback-sent")).toBeInTheDocument());
  });

  it("names the contact email in the fallback mail, or says none was given", () => {
    const withContact = decodeURIComponent(feedbackMailto("Hello", "me@example.com"));
    expect(withContact).toMatch(/^mailto:contact@lemoncrow\.com\?subject=LemonCrow Review feedback&body=Hello/);
    expect(withContact).toContain("Contact email: me@example.com");
    expect(decodeURIComponent(feedbackMailto("Hello", ""))).toContain("Contact email: not provided");
  });

  it("Cancel closes and clears the form", async () => {
    const user = userEvent.setup();
    const onOpenChange = mount();
    await user.type(screen.getByRole("textbox", { name: "Feedback" }), "Draft");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(screen.getByRole("textbox", { name: "Feedback" })).toHaveValue("");
  });
});
