import { Badge } from "../../components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs";
import type { GitHubPublishPreview } from "../types";
import { githubSentText, githubVerdictLabel, inlineLocation, type GitHubVerdictChoice } from "./githubModel";

export const GITHUB_NO_VERDICT = "Choose a verdict to see the review as it will appear.";
const NOT_SENT = "Not sent: private notes, AI prompts, answers you changed.";
const NO_CHECK_RUN = "No check run is posted from this machine.";

const CHECK_TONE = { passing: "success", failing: "destructive", pending: "warning" } as const;

interface Props {
  /** The plan for the verdict on screen, or null while it loads. */
  preview: GitHubPublishPreview | null;
  /** Why there is no preview, when there is none. */
  problem?: string;
  /** The verdict chosen on Wrap up; null until one is. */
  choice: GitHubVerdictChoice | null;
  /** The preview is for an earlier choice and a new one is on its way. */
  updating?: boolean;
  /** The pull request's title as the Review holds it. */
  title: string;
}

/** Why the server changed what will be posted (a verdict taken as a comment, a summary left out). Plain text, no live role. */
function Notices({ preview }: { preview: GitHubPublishPreview }) {
  if (preview.notices.length === 0) return null;
  return (
    <div className="mb-3 space-y-1" data-testid="gh-notices">
      {preview.notices.map((notice) => <p key={notice} className="text-label text-p1">{notice}</p>)}
    </div>
  );
}

/**
 * SPEC 4.17: "What you send" (the exact text) and "As it will appear on GitHub" (a generic drawing of a pull request
 * page, no GitHub logo). Everything drawn comes from the server's plan; nothing is added to make it look finished.
 */
export function GitHubPreviewTabs({ preview, problem = "", choice, updating = false, title }: Props) {
  const wait = updating ? "Updating the preview…" : problem || "Loading the pull request preview…";
  const body = (preview?.body ?? preview?.summary ?? "").split("\n\n").filter((part) => part.trim() !== "");
  const label = preview ? githubVerdictLabel(preview.event, choice) : "";
  return (
    <Tabs defaultValue="send" data-testid="gh-preview-tabs">
      <TabsList variant="underline" aria-label="Preview">
        <TabsTrigger variant="underline" value="send">What you send</TabsTrigger>
        <TabsTrigger variant="underline" value="page">As it will appear on GitHub</TabsTrigger>
      </TabsList>
      <TabsContent value="send" className="mt-3">
        {!choice ? (
          <p role="status">{GITHUB_NO_VERDICT}</p>
        ) : preview && !updating ? (
          <>
          <Notices preview={preview} />
          <pre data-testid="gh-send-text" className="max-h-[50vh] overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-background p-3 text-meta">
            {githubSentText(preview, choice)}
          </pre>
          </>
        ) : (
          <p role="status">{wait}</p>
        )}
      </TabsContent>
      <TabsContent value="page" className="mt-3">
        {!choice ? (
          <p role="status" data-testid="gh-no-verdict">{GITHUB_NO_VERDICT}</p>
        ) : preview && !updating ? (
          <>
          <Notices preview={preview} />
          <div role="region" aria-label="As it will appear on GitHub (preview)" data-testid="gh-page" className="rounded-lg border border-border bg-card p-3 text-label">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-heading font-semibold">{title}</h3>
              <span className="font-mono text-meta text-muted-foreground">{preview.pr.repo} #{preview.pr.number}</span>
            </div>
            {preview.author ? <p className="mt-1 text-meta text-muted-foreground">Opened by {preview.author}</p> : null}
            <div className="mt-3 rounded-md border border-border p-3" data-testid="gh-review">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span>
                  <strong>{preview.viewer || "you"}</strong> <span className="text-muted-foreground">reviewed just now</span>
                </span>
                <Badge tone={preview.event === "REQUEST_CHANGES" ? "warning" : preview.event === "APPROVE" ? "success" : "neutral"} data-testid="gh-verdict">{label}</Badge>
              </div>
              <div className="mt-2 space-y-2" data-testid="gh-body">
                {body.map((part, index) => <p key={index} className="whitespace-pre-wrap break-words">{part}</p>)}
              </div>
              {preview.inline.length > 0 ? (
                <ul className="mt-3 space-y-3" aria-label="Inline comments">
                  {preview.inline.map((item) => (
                    <li key={item.annotation_id} data-testid="gh-comment" className="border-t border-border pt-2">
                      <span className="break-all font-mono text-meta text-muted-foreground">{inlineLocation(item)}</span>
                      <p className="mt-1 whitespace-pre-wrap break-words">{item.body}</p>
                      {item.ai_fix ? (
                        <details data-testid="gh-ai" className="mt-1 text-meta">
                          <summary className="cursor-pointer pointer-coarse:flex pointer-coarse:items-center">AI suggested fix, included by the reviewer</summary>
                          <p className="text-muted-foreground">The reviewer ticked this fix into the comment. It is unverified and is posted with the comment.</p>
                        </details>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-meta text-muted-foreground">No inline comments.</p>
              )}
            </div>
            <div className="mt-3" data-testid="gh-checks">
              <span className="text-meta font-semibold text-muted-foreground">Checks</span>
              {preview.check_run ? (
                <p className="mt-1 flex flex-wrap items-center gap-2" data-testid="gh-check-run">
                  <Badge tone={CHECK_TONE[preview.check_run.state]}>{preview.check_run.label}</Badge>
                  <span data-testid="gh-run-text">{preview.check_run.text}</span>
                </p>
              ) : (
                <p className="mt-1 text-meta text-muted-foreground" data-testid="gh-no-check-run">{NO_CHECK_RUN}</p>
              )}
            </div>
            <p className="mt-3 text-meta text-muted-foreground">{NOT_SENT}</p>
          </div>
          </>
        ) : (
          <p role="status">{wait}</p>
        )}
      </TabsContent>
    </Tabs>
  );
}
