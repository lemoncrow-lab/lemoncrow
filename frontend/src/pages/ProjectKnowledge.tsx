/**
 * Project Knowledge (UX0b): what LemonCrow derived from committed repository
 * history, under the existing Code/Knowledge surface.
 *
 * Reading starts no AI or background work. An AI narrative can be hidden
 * (deterministic history never moves) or regenerated (one queued background
 * job). A correction becomes a new repository document, handed back as a patch
 * to land through a reviewed Change; nothing here writes to the repository.
 */
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { MoreHorizontal } from "lucide-react";

import {
  Alert,
  Button,
  Card,
  Chip,
  EmptyState,
  Input,
  SectionHeader,
  Select,
  SelectItem,
  SnippetCard,
  Textarea,
} from "../components/WorkbenchUI";
import { Button as UiButton } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import {
  fetchKnowledgeListing,
  fetchKnowledgeOverview,
  hideNarrative,
  proposeCorrection,
  regenerateNarrative,
  type CorrectionProposal,
  type KnowledgeEntry,
  type KnowledgeEvent,
  type KnowledgeListing,
  type KnowledgeOverview,
  type KnowledgeRepositoryRow,
} from "./projectKnowledgeApi";
import { ServerUnreachable, describeError, isUnreachable } from "../components/ServerUnreachable";
import { api, type CodeMapProject } from "../api";

const VOCABULARY: { term: string; meaning: string }[] = [
  { term: "Project Knowledge", meaning: "what the repository's committed history says: advisory context for Brief, Ask and AI Review" },
  { term: "Agent Memory", meaning: "what your agents recall across sessions" },
  { term: "Playbooks", meaning: "reusable procedures learned from past runs" },
  { term: "Review rules", meaning: "enforceable policy, kept in the repository's .lemoncrow/review.yaml" },
];

function message(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? describeError(reason) : fallback;
}

function short(id: string): string {
  const [, digest = id] = id.split(":");
  return digest.slice(0, 10);
}

function repositoryName(row: { repository_id: string; source: KnowledgeRepositoryRow["source"] }): string {
  return row.source?.repository_key || `repository ${short(row.repository_id)}`;
}

function when(value: string): string {
  return value ? value.slice(0, 10) : "";
}

// What each history event means, in words (the server's labels name the pipeline step).
const EVENT_TEXT: Record<string, { title: string; detail: (branch: string) => string }> = {
  baseline: { title: "First sync", detail: (branch) => `LemonCrow read ${branch} as it stood.` },
  merged_change: { title: "Change landed", detail: (branch) => `A change merged into ${branch}.` },
  source_advance: { title: "Branch moved", detail: (branch) => `New commits reached ${branch}.` },
};

function eventTitle(event: KnowledgeEvent): string {
  return EVENT_TEXT[event.kind]?.title ?? event.label;
}

function shellWord(value: string): string {
  return /^[\w@%+=:,./-]+$/.test(value) ? value : `'${value.replace(/'/g, "'\\''")}'`;
}

/**
 * The server's sync command with this checkout's values in place of `<checkout>`, `<key>` and `<branch>`:
 * the active indexed project (else the first), its main checkout, and the branch that checkout is on.
 * A value the projects cannot give keeps its placeholder.
 */
export function filledSyncCommand(template: string, projects: readonly CodeMapProject[]): string {
  const project = projects.find((row) => row.active) ?? projects[0];
  if (!project) return template;
  const repo = project.project_root || project.root;
  const mainCheckout = projects.find((row) => row.root === repo);
  const branch = [mainCheckout, project].find((row) => row?.branch && !row.detached)?.branch;
  const key = project.project || project.label || repo.split("/").filter(Boolean).pop();
  const values: Record<string, string | undefined> = { "<checkout>": repo, "<key>": key, "<branch>": branch };
  return template.replace(/<(?:checkout|key|branch)>/g, (token) => (values[token] ? shellWord(values[token]!) : token));
}

function branchName(ref: string | undefined): string {
  return ref ? ref.replace(/^refs\/heads\//, "") : "the canonical branch";
}

/**
 * A repository document's Markdown as readable text. The server stores bodies with whitespace
 * collapsed, so a heading marker is the only section break left: it becomes a line break.
 */
export function documentText(body: string): string {
  return body
    .replace(/```[\w-]*/g, "")
    .replace(/!\[([^\]]*)\]\([^)\s]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)\s]*\)/g, "$1")
    .replace(/\*\*(\S(?:.*?\S)?)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/(^|\s)#{1,6}\s+/g, (_match, _lead: string, offset: number) => (offset === 0 ? "" : "\n"))
    .trim();
}

export default function ProjectKnowledge() {
  const [listing, setListing] = useState<KnowledgeListing | null>(null);
  const [error, setError] = useState("");
  const [unreachable, setUnreachable] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState("");

  useEffect(() => {
    let active = true;
    setError("");
    setUnreachable(false);
    fetchKnowledgeListing()
      .then((result) => {
        if (!active) return;
        setListing(result);
        setSelected((current) => current || result.repositories[0]?.repository_id || "");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setUnreachable(isUnreachable(reason));
        setError(message(reason, "Could not read Project Knowledge."));
      });
    return () => {
      active = false;
    };
  }, [attempt]);

  const repositories = listing?.repositories ?? [];
  const nothingSynced = Boolean(listing) && repositories.length === 0;
  const [projects, setProjects] = useState<CodeMapProject[]>([]);
  useEffect(() => {
    if (!nothingSynced) return;
    let active = true;
    // Fill the sync command from the indexed checkout; without one it keeps its placeholders.
    api
      .codeMapProjects()
      .then((result) => {
        if (active) setProjects(result.projects ?? []);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [nothingSynced]);

  return (
    <div className="space-y-4" data-testid="project-knowledge">
      <SectionHeader
        eyebrow="Project Knowledge"
        title="What your repositories' history says"
        description="Read from committed code and landed changes. Each AI narrative lists the sources it was shown. Brief, Ask and AI Review use it as background; it never changes what Review enforces."
      />
      <Card className="p-3">
        <dl className="grid gap-2 text-caption sm:grid-cols-2" aria-label="What each kind of knowledge is">
          {VOCABULARY.map((row) => (
            <div key={row.term} className="min-w-0">
              <dt className="font-semibold text-foreground">{row.term}</dt>
              <dd className="text-muted-foreground">{row.meaning}</dd>
            </div>
          ))}
        </dl>
      </Card>
      {error && unreachable && <ServerUnreachable onRetry={() => setAttempt((n) => n + 1)} />}
      {error && !unreachable && (
        <div role="alert">
          <Alert tone="destructive" description={error} />
        </div>
      )}
      {listing && nothingSynced && (
        <>
          <EmptyState
            title="Nothing synced yet"
            description="Run this to sync this repository: its checkout, its name and the branch to follow. Sync reads committed Git objects only and makes no model call."
          />
          <SnippetCard title="Sync a checkout" body={filledSyncCommand(listing.sync_command, projects)} wrap />
        </>
      )}
      {repositories.length > 1 && (
        <div className="max-w-sm">
          <Select aria-label="Repository" value={selected} onValueChange={setSelected}>
            {repositories.map((row) => (
              <SelectItem key={row.repository_id} value={row.repository_id}>
                {repositoryName(row)} · {row.canonical_ref}
              </SelectItem>
            ))}
          </Select>
        </div>
      )}
      {listing?.truncated && (
        <p className="text-caption text-muted-foreground">Showing the 50 most recently updated repositories only.</p>
      )}
      {selected && <RepositoryKnowledge key={selected} repositoryId={selected} />}
      {/* The legacy review overlay note is no longer shown: it described an internal store, not this page. */}
    </div>
  );
}

function RepositoryKnowledge({ repositoryId }: { repositoryId: string }) {
  const [overview, setOverview] = useState<KnowledgeOverview | null>(null);
  const [error, setError] = useState("");
  const [term, setTerm] = useState("");
  const [query, setQuery] = useState({ term: "", offset: 0 });
  const [notice, setNotice] = useState<{ tone: "info" | "warning"; text: string; hidden?: KnowledgeEntry } | null>(
    null,
  );
  const [busy, setBusy] = useState("");
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let active = true;
    setError("");
    fetchKnowledgeOverview(repositoryId, query)
      .then((result) => {
        if (active) setOverview(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(message(reason, "Could not read this repository's knowledge."));
      });
    return () => {
      active = false;
    };
  }, [repositoryId, query, generation]);

  const hide = useCallback(
    async (entry: KnowledgeEntry) => {
      setBusy(entry.entry_id);
      try {
        await hideNarrative(repositoryId, entry.entry_id);
        setNotice({
          tone: "info",
          text: "Narrative hidden. It no longer feeds Brief, Ask or AI Review; the source-backed history is unchanged.",
          hidden: entry,
        });
        setGeneration((value) => value + 1);
      } catch (reason: unknown) {
        setNotice({ tone: "warning", text: message(reason, "Could not hide the narrative.") });
      } finally {
        setBusy("");
      }
    },
    [repositoryId],
  );

  const regenerate = useCallback(
    async (entry: KnowledgeEntry) => {
      setBusy(entry.entry_id);
      try {
        const result = await regenerateNarrative(repositoryId, entry.entry_id);
        setNotice(
          result.status === "queued"
            ? {
                tone: "info",
                text: "Queued. A fresh narrative replaces this one when LemonCrow's local worker runs it, within the background budget.",
              }
            : { tone: "warning", text: `Not queued: ${result.reason || "refused"}.` },
        );
      } catch (reason: unknown) {
        setNotice({ tone: "warning", text: message(reason, "Could not queue a fresh narrative.") });
      } finally {
        setBusy("");
      }
    },
    [repositoryId],
  );

  if (error) {
    return (
      <div role="alert">
        <Alert tone="destructive" description={error} />
      </div>
    );
  }
  if (!overview) return <p className="text-caption text-muted-foreground">Loading Project Knowledge…</p>;

  const source = overview.repository.source;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setQuery({ term: term.trim(), offset: 0 });
  };
  return (
    <div className="space-y-4" data-testid="project-knowledge-repository">
      <div className="min-w-0 text-caption text-muted-foreground">
        <span className="font-semibold text-foreground">
          {repositoryName({ repository_id: overview.repository.repository_id, source })}
        </span>
        {source && (
          <>
            {" "}
            · {source.canonical_ref} · <span className="break-all">synced from {source.repo_root}</span>
          </>
        )}
      </div>
      {notice && (
        <div role="status" data-testid="project-knowledge-notice">
          <Alert
            tone={notice.tone}
            description={
              <span className="flex flex-wrap items-center gap-2">
                <span>{notice.text}</span>
                {notice.hidden && overview.regenerate.available && (
                  <Button size="xs" disabled={Boolean(busy)} onClick={() => notice.hidden && regenerate(notice.hidden)}>
                    Regenerate a fresh narrative
                  </Button>
                )}
              </span>
            }
          />
        </div>
      )}

      <section className="space-y-2" aria-label="AI narratives">
        <h3 className="text-heading font-semibold text-foreground">AI narratives</h3>
        {overview.narratives.length === 0 ? (
          <p className="text-caption text-muted-foreground">
            No AI narratives. <code>lc knowledge project sync --narrate</code> queues them; the source-backed context
            below stands on its own.
          </p>
        ) : (
          overview.narratives.map((entry) => (
            <EntryCard
              key={entry.entry_id}
              repositoryId={repositoryId}
              entry={entry}
              actions={[
                { label: "Hide", disabled: Boolean(busy), onSelect: () => void hide(entry) },
                {
                  label: "Regenerate",
                  disabled: Boolean(busy) || !overview.regenerate.available,
                  hint: overview.regenerate.available ? "" : overview.regenerate.reason,
                  onSelect: () => void regenerate(entry),
                },
              ]}
            />
          ))
        )}
        {overview.narratives_truncated && (
          <p className="text-caption text-muted-foreground">Showing the newest narratives only.</p>
        )}
      </section>

      <section className="space-y-2" aria-label="Source-backed context">
        <h3 className="text-heading font-semibold text-foreground">Source-backed context</h3>
        <form className="flex max-w-md gap-2" onSubmit={submit} role="search">
          <Input
            aria-label="Search Project Knowledge"
            placeholder="Search paths and documents"
            value={term}
            onChange={(event) => setTerm(event.target.value)}
          />
          <Button size="xs" type="submit">
            Search
          </Button>
        </form>
        {overview.context.entries.length === 0 ? (
          <p className="text-caption text-muted-foreground">
            {overview.context.term ? "Nothing matches." : "No documents or source-backed context beyond the path inventory."}
          </p>
        ) : (
          overview.context.entries.map((entry) => (
            <EntryCard key={entry.entry_id} repositoryId={repositoryId} entry={entry} />
          ))
        )}
        <div className="flex gap-2">
          {overview.context.offset > 0 && (
            <Button size="xs" onClick={() => setQuery({ term: overview.context.term, offset: 0 })}>
              First page
            </Button>
          )}
          {overview.context.next_offset !== null && (
            <Button
              size="xs"
              onClick={() => setQuery({ term: overview.context.term, offset: overview.context.next_offset ?? 0 })}
            >
              More
            </Button>
          )}
        </div>
      </section>

      <section className="space-y-2" aria-label="Recent evolution">
        <h3 className="text-heading font-semibold text-foreground">Recent evolution</h3>
        <ol className="space-y-1 text-caption">
          {overview.evolution.map((event) => (
            <li
              key={`${event.generation_id}-${event.observed_at}`}
              className="flex flex-wrap items-baseline gap-x-2"
              title={`Generation ${short(event.generation_id)}`}
            >
              <span className="font-medium text-foreground">{eventTitle(event)}</span>
              {EVENT_TEXT[event.kind] && (
                <span className="text-muted-foreground">
                  {EVENT_TEXT[event.kind].detail(branchName(source?.canonical_ref))}
                </span>
              )}
              <span className="text-muted-foreground">{when(event.observed_at)}</span>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

interface EntryAction {
  label: string;
  disabled?: boolean;
  /** Why the action is unavailable, shown under it in the menu. */
  hint?: string;
  onSelect: () => void;
}

function EntryCard({
  repositoryId,
  entry,
  actions = [],
}: {
  repositoryId: string;
  entry: KnowledgeEntry;
  actions?: EntryAction[];
}) {
  const [correcting, setCorrecting] = useState(false);
  const menu: EntryAction[] = [
    ...actions,
    { label: correcting ? "Close correction" : "Propose correction", onSelect: () => setCorrecting((value) => !value) },
  ];
  const body = entry.support === "source_document" ? documentText(entry.body) : entry.body;
  return (
    <Card className="space-y-2 p-3" data-testid="project-knowledge-entry">
      <div className="flex items-start gap-2">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2 text-caption">
          <Chip tone={entry.support === "ai_narrated" ? "info" : "neutral"}>{entry.support_label}</Chip>
          <span className="min-w-0 break-all font-medium text-foreground">{entry.subject.key}</span>
          {entry.event && (
            <span className="text-muted-foreground">
              {eventTitle(entry.event)} · {when(entry.event.observed_at)}
            </span>
          )}
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <UiButton type="button" size="icon" variant="ghost" aria-label="Actions" className="h-7 w-7 shrink-0">
              <MoreHorizontal size={16} />
            </UiButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-60">
            {menu.map((action) => (
              <DropdownMenuItem key={action.label} disabled={action.disabled} onSelect={action.onSelect}>
                <span className="flex min-w-0 flex-col">
                  <span>{action.label}</span>
                  {action.disabled && action.hint && (
                    <span className="text-caption text-muted-foreground">{action.hint}</span>
                  )}
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <p className="whitespace-pre-wrap break-words text-body text-foreground">
        {body}
        {entry.body_truncated && <span className="text-muted-foreground"> …</span>}
      </p>
      <details className="text-meta">
        <summary className="cursor-pointer text-muted-foreground">Sources</summary>
        <ul className="mt-1 space-y-0.5" data-testid="project-knowledge-sources">
          {[...entry.source_refs, ...entry.review_refs, ...entry.merge_refs].map((ref) => (
            <li key={ref}>
              <code className="break-all">{ref}</code>
            </li>
          ))}
          {entry.source_refs_omitted > 0 && (
            <li className="text-muted-foreground">and {entry.source_refs_omitted} more</li>
          )}
          <li className="text-muted-foreground">
            {entry.event ? `${eventTitle(entry.event)} ${when(entry.event.observed_at)}` : "Generation"} ·{" "}
            <code>{short(entry.generation)}</code>
          </li>
        </ul>
      </details>
      {correcting && <CorrectionForm repositoryId={repositoryId} entry={entry} />}
    </Card>
  );
}

function CorrectionForm({ repositoryId, entry }: { repositoryId: string; entry: KnowledgeEntry }) {
  const [text, setText] = useState("");
  const [proposal, setProposal] = useState<CorrectionProposal | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      setProposal(await proposeCorrection(repositoryId, { text, entry_id: entry.entry_id }));
    } catch (reason: unknown) {
      setError(message(reason, "Could not prepare the correction."));
    } finally {
      setPending(false);
    }
  };

  return (
    <form className="space-y-2" onSubmit={submit} data-testid="project-knowledge-correction">
      <Textarea
        aria-label="Correction"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="What is actually true, and where it is documented"
        rows={3}
      />
      <p className="text-caption text-muted-foreground">
        This becomes a new document in the repository. Apply the patch and land it through a reviewed Change; Project
        Knowledge changes only once it is on the canonical branch.
      </p>
      <Button size="xs" type="submit" disabled={pending || !text.trim()}>
        Create patch
      </Button>
      {error && (
        <div role="alert">
          <Alert tone="destructive" description={error} />
        </div>
      )}
      {proposal && <SnippetCard title={proposal.path} caption="git apply this patch" body={proposal.patch} />}
    </form>
  );
}
