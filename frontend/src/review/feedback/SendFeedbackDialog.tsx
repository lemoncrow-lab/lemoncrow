import { useRef, useState, type FormEvent } from "react";
import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogLede, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import { authenticatedFetch } from "../reviewApi";

/** Where a message goes when this server could not send it (the Classic "Make a wish" fallback). */
export const FEEDBACK_MAIL = "contact@lemoncrow.com";
/** The server's limits (core/service/feedback.py MAX_MESSAGE_CHARS and MAX_EMAIL_CHARS). */
export const FEEDBACK_MAX_CHARS = 4000;
const EMAIL_MAX_CHARS = 254;

export type FeedbackState = "idle" | "sending" | "sent" | "failed";

/** The same words, for the person's own mail client, when the server could not send them. */
export function feedbackMailto(message: string, contact: string): string {
  const body = [message, "", contact ? `Contact email: ${contact}` : "Contact email: not provided", "", "Sent from LemonCrow Review."].join("\n");
  return `mailto:${FEEDBACK_MAIL}?subject=${encodeURIComponent("LemonCrow Review feedback")}&body=${encodeURIComponent(body)}`;
}

/**
 * POST /api/feedback (api.py `post_product_feedback`): `{ message, email, source }`. The server forwards it whatever
 * telemetry is set to and answers 502 when nothing was sent, so only `delivered: "remote"` counts as sent.
 */
export async function sendProductFeedback(message: string, email: string): Promise<boolean> {
  try {
    const response = await authenticatedFetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, email, source: "review" }),
    });
    if (!response.ok) return false;
    const body = (await response.json().catch(() => null)) as { delivered?: string } | null;
    return body?.delivered === "remote";
  } catch {
    return false;
  }
}

/** Send feedback: a message, an optional contact email, and a mail fallback when the server could not send it. */
export function SendFeedbackDialog({ open, onOpenChange }: { open: boolean; onOpenChange(open: boolean): void }) {
  const [text, setText] = useState("");
  const [email, setEmail] = useState("");
  const [state, setState] = useState<FeedbackState>("idle");
  // A send that lands after the dialog closed (or after another send began) does not touch the fresh form.
  const attempt = useRef(0);
  const change = (next: boolean) => {
    if (!next) { attempt.current += 1; setText(""); setEmail(""); setState("idle"); }
    onOpenChange(next);
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const message = text.trim();
    if (!message || state === "sending") return;
    const mine = ++attempt.current;
    setState("sending");
    const sent = await sendProductFeedback(message, email.trim());
    if (attempt.current === mine) setState(sent ? "sent" : "failed");
  };
  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogContent data-testid="send-feedback-dialog">
        <form onSubmit={(event) => void submit(event)} className="flex min-h-0 flex-1 flex-col">
          <DialogHeader><DialogTitle>Send feedback</DialogTitle></DialogHeader>
          <DialogLede>Your message goes to the LemonCrow team with the LemonCrow version, nothing else.</DialogLede>
          <DialogBody className="space-y-3">
            {state === "sent" ? <p role="status" className="text-body text-ok" data-testid="feedback-sent">Sent. Thank you.</p> : <>
              <Textarea value={text} onChange={(event) => setText(event.target.value)} aria-label="Feedback" maxLength={FEEDBACK_MAX_CHARS} rows={5}
                placeholder="What would make reviewing easier?" disabled={state === "sending"} autoFocus />
              <Input type="email" value={email} onChange={(event) => setEmail(event.target.value)} aria-label="Contact email (optional)" maxLength={EMAIL_MAX_CHARS}
                placeholder="Contact email (optional)" disabled={state === "sending"} />
              <p className="text-meta text-muted-foreground">Add your email only if you are happy to be contacted about this.</p>
              {state === "failed" && <p role="alert" className="text-meta text-p1" data-testid="feedback-failed">
                Couldn't send it from here. Your message is still here; <a className="underline" href={feedbackMailto(text.trim(), email.trim())}>email it to {FEEDBACK_MAIL}</a> instead.
              </p>}
            </>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => change(false)}>{state === "sent" ? "Close" : "Cancel"}</Button>
            {state !== "sent" && <Button type="submit" variant="solid" disabled={!text.trim() || state === "sending"}>{state === "sending" ? "Sending…" : "Send"}</Button>}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
