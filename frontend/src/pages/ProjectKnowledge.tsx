/**
 * Project Knowledge (UX0b): what LemonCrow derived from committed repository
 * history, under the existing Code/Knowledge surface.
 *
 * Reading starts no AI or background work. An AI narrative can be hidden
 * (deterministic history never moves) or regenerated (one queued background
 * job). A correction becomes a new repository document, handed back as a patch
 * to land through a reviewed Change; nothing here writes to the repository.
 */
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";

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
import {
  fetchKnowledgeListing,
  fetchKnowledgeOverview,
  hideNarrative,
  proposeCorrection,
  regenerateNarrative,
  type CorrectionProposal,
  type KnowledgeEntry,
  type KnowledgeListing,
  type KnowledgeOverview,
  type KnowledgeRepositoryRow,
} from "./projectKnowledgeApi";

const VOCABULARY: { term: string; meaning: string }[] = [
  { term: "Project Knowledge", meaning: "what the repository's committed history says: advisory context for Brief, Ask and AI Review" },
  { term: "Agent Memory", meaning: "what your agents recall across sessions" },
  { term: "Playbooks", meaning: "reusable procedures learned from past runs" },
  { term: "Review rules", meaning: "enforceable policy, kept in the repository's .lemoncrow/review.yaml" },
];

function message(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
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

export default function ProjectKnowledge() {
  const [listing, setListing] = useState<KnowledgeListing | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState("");

  useEffect(() => {
    let active = true;
    fetchKnowledgeListing()
      .then((result) => {
        if (!active) return;
        setListing(result);
        setSelected((current) => current || result.repositories[0]?.repository_id || "");
      })
      .catch((reason: unknown) => {
        if (active) setError(message(reason, "Could not read Project Knowledge."));
      });
    return () => {
      active = false;
    };
  }, []);

  const repositories = listing?.repositories ?? [];
  return (
    <div className="space-y-4" data-testid="project-knowledge">
      <SectionHeader
        eyebrow="Project Knowledge"
        title="What your repositories' history says"
        description="Derived from committed source and landed Changes. AI narratives cite the sources they were shown. Advisory context, never Review policy."
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
      {error && (
        <div role="alert">
          <Alert tone="destructive" description={error} />
        </div>
      )}
      {listing && repositories.length === 0 && (
        <EmptyState
          title="Nothing synced yet"
          description={
            <>
              Run <code className="break-all">{listing.sync_command}</code> in a checkout. Sync reads committed
              Git objects only and makes no model call.
            </>
          }
        />
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
      {listing && <Alert title="Legacy review overlay" description={listing.legacy_overlay_note} />}
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
        <h3 className="text-sm font-semibold text-foreground">AI narratives</h3>
        {overview.narratives.length === 0 ? (
          <p className="text-caption text-muted-foreground">
            No AI narratives. <code>lc knowledge project sync --narrate</code> queues them; the source-backed context
            below stands on its own.
          </p>
        ) : (
          overview.narratives.map((entry) => (
            <EntryCard key={entry.entry_id} repositoryId={repositoryId} entry={entry}>
              <Button size="xs" disabled={Boolean(busy)} onClick={() => hide(entry)}>
                Hide
              </Button>
              <Button
                size="xs"
                disabled={Boolean(busy) || !overview.regenerate.available}
                tooltip={overview.regenerate.available ? "Queue a fresh narrative" : overview.regenerate.reason}
                onClick={() => regenerate(entry)}
              >
                Regenerate
              </Button>
            </EntryCard>
          ))
        )}
        {overview.narratives_truncated && (
          <p className="text-caption text-muted-foreground">Showing the newest narratives only.</p>
        )}
      </section>

      <section className="space-y-2" aria-label="Source-backed context">
        <h3 className="text-sm font-semibold text-foreground">Source-backed context</h3>
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
        <h3 className="text-sm font-semibold text-foreground">Recent evolution</h3>
        <ol className="space-y-1 text-caption">
          {overview.evolution.map((event) => (
            <li key={`${event.generation_id}-${event.observed_at}`} className="flex flex-wrap gap-2">
              <Chip>{event.label}</Chip>
              <span className="text-muted-foreground">{when(event.observed_at)}</span>
              <code className="text-muted-foreground">{short(event.generation_id)}</code>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

function EntryCard({
  repositoryId,
  entry,
  children,
}: {
  repositoryId: string;
  entry: KnowledgeEntry;
  children?: ReactNode;
}) {
  const [correcting, setCorrecting] = useState(false);
  return (
    <Card className="space-y-2 p-3" data-testid="project-knowledge-entry">
      <div className="flex flex-wrap items-center gap-2 text-caption">
        <Chip tone={entry.support === "ai_narrated" ? "info" : "neutral"}>{entry.support_label}</Chip>
        {entry.event && <Chip>{entry.event.label}</Chip>}
        <span className="min-w-0 break-all font-medium text-foreground">{entry.subject.key}</span>
        <span className="text-muted-foreground">{entry.knowledge_kind}</span>
      </div>
      <p className="whitespace-pre-wrap break-words text-body text-neutral-200">
        {entry.body}
        {entry.body_truncated && <span className="text-muted-foreground"> …</span>}
      </p>
      <details className="text-caption">
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
            {entry.event ? `${entry.event.label} ${when(entry.event.observed_at)}` : "Generation"} ·{" "}
            <code>{short(entry.generation)}</code>
          </li>
        </ul>
      </details>
      <div className="flex flex-wrap gap-2">
        {children}
        <Button size="xs" onClick={() => setCorrecting((value) => !value)} aria-expanded={correcting}>
          Propose correction
        </Button>
      </div>
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
