import { ArrowLeft } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Textarea } from "../../components/ui/textarea";
import { FocusCode } from "../focus/FocusCode";
import { repoNameOf } from "../FlowTopBar";
import { useFlowDocumentTitle } from "../flowDocumentTitle";
import { fetchTargets, postMark } from "../reviewApi";
import { revisionName } from "../revisionLabel";
import type { ReviewOverview, ReviewTarget } from "../types";
import type { SecondPart, SecondReviewerStatus } from "./secondApi";
import { signSecond } from "./secondApi";
import { SECOND_ANSWER_FIRST, SECOND_COMPARE, SECOND_HIDDEN_LEDE, firstName, signedLine } from "./secondCopy";
import { useFlowToast } from "../flowToast";
import { useSecondReviewer } from "./useSecondReviewer";

const WORDS: Record<string, string> = {
  reviewed: "Looks good",
  needs_changes: "Needs a change",
  unreviewed: "Not answered",
  later: "Not answered",
};

export function answerWord(state: string): string {
  return WORDS[state] ?? "Not answered";
}

/** `/r/<review>/second` (SPEC 4.16): answer on your own first, then compare with the first reviewer, then sign. */
export function SecondSignatureScreen({ apiReviewId, overview }: { apiReviewId: string; overview: ReviewOverview }) {
  const navigate = useNavigate();
  const { status, failed, refresh } = useSecondReviewer(apiReviewId);
  const [targets, setTargets] = useState<readonly ReviewTarget[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [signed, setSigned] = useState<SecondReviewerStatus | null>(null);
  const toast = useFlowToast();
  const heading = useRef<HTMLHeadingElement>(null);
  const repo = repoNameOf(overview);
  useFlowDocumentTitle("Second signature", repo);
  const revision = overview.revision.id;

  useEffect(() => {
    let live = true;
    void fetchTargets(apiReviewId).then((result) => { if (live) setTargets(result.targets); }).catch(() => { if (live) setError("The change could not be loaded."); });
    return () => { live = false; };
  }, [apiReviewId, revision]);

  const view = signed ?? status;
  const parts = view?.parts ?? [];
  const answered = Boolean(view?.answered);
  const first = view?.first_reviewer ? firstName(view.first_reviewer.name) : "The first reviewer";
  const byKey = useMemo(() => new Map(targets.map((target) => [target.unit_key, target])), [targets]);
  const done = signed !== null || (view?.state === "agrees" || view?.state === "disagrees");
  const anyChange = parts.some((part) => part.state === "needs_changes");

  const answer = useCallback(async (part: SecondPart, state: "reviewed" | "needs_changes") => {
    setBusy(true);
    setError("");
    try {
      await postMark(apiReviewId, part.unit_key, state, revision);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Your answer was not saved.");
    } finally {
      setBusy(false);
    }
  }, [apiReviewId, revision, refresh]);

  const sign = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      setSigned(await signSecond(apiReviewId, revision, note));
      setEditing(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Your signature was not recorded.");
    } finally {
      setBusy(false);
    }
  }, [apiReviewId, revision, note]);

  // flowToast is the only announcer (SPEC 8): a failure and the signature are said there, and drawn here without a live role.
  useEffect(() => { if (error) toast.show({ text: error, tone: "alert" }); }, [error]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (signed) toast.show({ text: signedLine(signed.state === "agrees", first) }); }, [signed]); // eslint-disable-line react-hooks/exhaustive-deps

  const title = done ? "Signed" : answered && !editing ? SECOND_COMPARE(first) : SECOND_ANSWER_FIRST;
  useEffect(() => { heading.current?.focus(); }, [title]);

  const head = overview.revision.head_sha ? ` @ ${overview.revision.head_sha.slice(0, 7)}` : "";
  return (
    <>
      <header className="sticky top-0 z-30 flex h-[52px] shrink-0 items-center gap-2 border-b border-border bg-background px-3" data-testid="second-bar">
        <Button variant="ghost" size="sm" aria-label="Reviews, go back" onClick={() => navigate("/reviews")}>
          <ArrowLeft size={16} aria-hidden="true" />
          Reviews
        </Button>
        <span className="min-w-0 truncate text-label" title={overview.title}>{overview.title}</span>
      </header>
      <div role="region" aria-label="Second signature" data-testid="second-screen" className="min-w-0 flex-1">
        <section className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
          <p className="font-mono text-tag text-muted-foreground" data-testid="second-eyebrow">
            Second signature · {revisionName(overview.revision.revision_number)} · {repo ? `${repo}${head}` : head.trim().replace(/^@ /, "")}
          </p>
          <h1 ref={heading} tabIndex={-1} className="text-title font-semibold" data-testid="second-title">{title}</h1>
          {!view && !failed ? <p className="text-label text-muted-foreground">Loading…</p> : null}
          {failed && !view ? <p className="text-label text-p1">The second signature could not be loaded.</p> : null}
          {view && !answered && !done ? <p className="text-label text-muted-foreground" data-testid="second-lede">{SECOND_HIDDEN_LEDE(first)}</p> : null}

          {parts.map((part) => {
            const target = byKey.get(part.unit_key);
            const theirs = view?.first_answers?.find((row) => row.unit_key === part.unit_key);
            return (
              <article key={part.unit_key} className="space-y-3 rounded-lg border border-border bg-card p-4" data-testid="second-part" data-part={part.unit_key}>
                <h2 className="break-all font-mono text-label font-semibold">{part.label}</h2>
                <p className="break-all font-mono text-meta text-muted-foreground">{part.path}{part.start_line ? `:${part.start_line}-${part.end_line}` : ""}</p>
                {target ? <FocusCode reviewRef={apiReviewId} revisionId={revision} target={target} /> : null}
                {!done && (!answered || editing) ? (
                  <div role="group" aria-label={`Your answer for ${part.label}`} className="flex flex-wrap gap-2">
                    <Button variant={part.state === "reviewed" ? "solid" : "outline"} aria-pressed={part.state === "reviewed"} disabled={busy} data-testid="second-good" onClick={() => void answer(part, "reviewed")}>Looks good</Button>
                    <Button variant={part.state === "needs_changes" ? "solid" : "outline"} aria-pressed={part.state === "needs_changes"} disabled={busy} data-testid="second-change" onClick={() => void answer(part, "needs_changes")}>Needs a change</Button>
                  </div>
                ) : null}
                {!answered && !done ? <p className="text-meta text-muted-foreground" data-testid="second-hidden">{first}'s answers are hidden until you answer.</p> : null}
                {(answered || done) ? (
                  <dl className="grid grid-cols-2 gap-3 text-label" data-testid="second-compare">
                    <div><dt className="text-meta text-muted-foreground">You</dt><dd data-testid="second-you">{answerWord(part.state)}</dd></div>
                    <div>
                      <dt className="text-meta text-muted-foreground">{first}</dt>
                      <dd data-testid="second-them">{theirs ? answerWord(theirs.state) : "Not shown"}</dd>
                      {theirs?.notes.map((text) => <dd key={text} className="mt-1 whitespace-pre-wrap border-l-2 border-border pl-2 text-meta" data-testid="second-their-note">{text}</dd>)}
                    </div>
                  </dl>
                ) : null}
              </article>
            );
          })}

          {answered && !done && !editing ? (
            <div className="space-y-3">
              {anyChange ? (
                <Textarea aria-label="What needs a change" placeholder="What needs a change" value={note} className="min-h-24 text-copy" onChange={(event) => setNote(event.target.value)} data-testid="second-note" />
              ) : null}
              <div className="flex flex-wrap gap-2">
                <Button variant="brand" disabled={busy || (anyChange && !note.trim())} data-testid="second-sign" onClick={() => void sign()}>
                  {anyChange ? "Sign · needs a change" : "Sign · I agree"}
                </Button>
                <Button variant="ghost" disabled={busy} data-testid="second-edit" onClick={() => setEditing(true)}>Change my answer</Button>
              </div>
            </div>
          ) : null}
          {editing ? <Button variant="outline" className="self-start" onClick={() => setEditing(false)}>Done changing</Button> : null}

          {done ? (
            <div className="space-y-2" data-testid="second-signed">
              <Badge tone={view?.state === "agrees" ? "success" : "destructive"}>{view?.state === "agrees" ? "Agrees" : "Disagrees"}</Badge>
              <p className="text-label">{signedLine(view?.state === "agrees", first)}</p>
            </div>
          ) : null}
          {error ? <p className="text-label text-p1" data-testid="second-error">{error}</p> : null}
        </section>
      </div>
      {toast.element}
    </>
  );
}
