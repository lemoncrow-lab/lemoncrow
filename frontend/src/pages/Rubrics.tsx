import { useEffect, useState, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  X,
} from "lucide-react";
import { api, type Rubric } from "../api";
import { fmtWhen } from "../lib/format";
import { domainLabel, humanize } from "../lib/labels";
import { CopyId } from "../components/CopyId";
import {
  Alert,
  Button,
  Chip,
  DisclosureCard,
  EmptyState,
  FieldLabel,
  Select,
  SelectItem,
  SnippetCard,
} from "../components/WorkbenchUI";

/** The MCP `verify` call an agent makes for this rubric, one key per check it must report. */
export function verifyCall(rubric: Rubric): string {
  const names = [...rubric.required_checks, ...rubric.warning_checks];
  const checks = names.length ? `{${names.map((name) => `${JSON.stringify(name)}: true`).join(", ")}}` : "{}";
  return `verify(rubric_id=${JSON.stringify(rubric.id)}, checks=${checks})`;
}

export default function Rubrics() {
  const { rubricId } = useParams<{ rubricId?: string }>();
  const navigate = useNavigate();
  const [items, setItems] = useState<Rubric[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [domainFilter, setDomainFilter] = useState<string>("all");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .rubrics()
      .then((data) => {
        setItems(data);
        if (rubricId) {
          const match = data.find((r) => r.id === rubricId);
          if (match) {
            setExpandedId(match.id);
            if (match.domain) setDomainFilter(match.domain);
          }
        }
      })
      .catch((e) => setErr(String(e)));
  }, [rubricId]);

  const domains = useMemo(
    () => [...new Set(items?.map((r) => r.domain).filter(Boolean))],
    [items]
  );

  const filtered = useMemo(() => {
    if (!items) return [];
    return domainFilter === "all"
      ? items
      : items.filter((r) => r.domain === domainFilter);
  }, [items, domainFilter]);
  if (err) return <Alert tone="destructive" description={err} />;
  if (err) return <Alert tone="destructive" description={err} />;
  if (!items) return <EmptyState title="Loading rubrics…" className="p-6" />;
  if (items.length === 0) {
    return (
      <EmptyState
        title="No rubrics yet"
        description="Rubrics are the checklists agents verify their work against before calling it done. None are loaded in this workspace."
        className="p-6"
      />
    );
  }

  return (
    <div className="space-y-4">
      {rubricId && (
        <div className="flex items-center gap-3">
          <Button
            onClick={() => navigate("/knowledge/rubrics")}
            size="xs"
            icon={<ArrowLeft size={14} />}
          >
            All rubrics
          </Button>
          <FieldLabel>
            {`Rubric · ${domainLabel(items.find((r) => r.id === rubricId)?.domain || "") || "Untitled"}`}
          </FieldLabel>
        </div>
      )}

      {/* Rubrics List */}
      <div className="space-y-4">
        {/* Filter */}
        <div className="flex gap-2 items-center px-4 py-2">
          <Select
            aria-label="Filter rubrics by domain"
            value={domainFilter}
            onValueChange={setDomainFilter}
            uiSize="xs"
            className="text-muted-foreground"
          >
            <SelectItem value="all">All domains</SelectItem>
            {domains.map((d) => (
              <SelectItem key={d} value={d}>
                {domainLabel(d)}
              </SelectItem>
            ))}
          </Select>
          <FieldLabel className="ml-auto text-muted-foreground">
            {filtered.length} rubric{filtered.length !== 1 ? "s" : ""}
          </FieldLabel>
        </div>

        {/* List of Rubrics */}
        <div className="space-y-3">
          {filtered.map((r) => {
            const isExpanded = expandedId === r.id;

            return (
              <DisclosureCard
                key={r.id}
                open={isExpanded}
                onToggle={() =>
                  setExpandedId(expandedId === r.id ? null : r.id)
                }
                contentClassName="space-y-4"
                header={
                  <div className="flex min-w-0 items-start gap-3">
                    <Chip tone="neutral">{domainLabel(r.domain)}</Chip>
                    <div className="min-w-0 flex-1">
                      <div className="mb-1 flex flex-wrap items-center gap-2">
                        <ChevronRight
                          size={14}
                          className={`text-muted-foreground transition-transform ${
                            isExpanded ? "rotate-90" : ""
                          }`}
                        />
                        <h3 className="font-semibold text-foreground" title={r.id}>
                          {humanize(r.id.replace(/^rubric_/, ""))}
                        </h3>
                      </div>
                      <p className="text-body text-muted-foreground">
                        {r.required_checks.length} check
                        {r.required_checks.length !== 1 ? "s" : ""}
                      </p>
                    </div>
                  </div>
                }
              >
                    {/* ID */}
                    <div>
                      <FieldLabel className="mb-2">
                        <ChevronRight size={14} className="inline mr-1" /> ID
                      </FieldLabel>
                      <CopyId value={r.id} label="Rubric ID" />
                    </div>

                    {/* Timestamps */}
                    <div>
                      <FieldLabel className="mb-2">
                        <ChevronRight size={14} className="inline mr-1" /> created
                      </FieldLabel>
                      <p className="text-body text-muted-foreground font-mono">
                        {fmtWhen(r.created_at)}
                      </p>
                      {r.updated_at && (
                        <p className="text-body text-muted-foreground font-mono mt-1">
                          Updated: {fmtWhen(r.updated_at)}
                        </p>
                      )}
                    </div>

                    {/* Required Checks */}
                    <div>
                      <FieldLabel className="mb-2">
                        <ChevronRight size={14} className="inline mr-1" /> required checks
                      </FieldLabel>
                      <ul className="space-y-1">
                        {r.required_checks.map((check, i) => (
                          <li
                            key={i}
                            className="text-label text-subtle leading-relaxed bg-muted/40 px-2 py-1 border border-border flex items-start gap-2"
                          >
                            <Check size={14} className="text-emerald-300 flex-shrink-0 mt-0.5" />
                            <span>{check}</span>
                          </li>
                        ))}
                      </ul>
                    </div>

                    {/* Triggers & Forbidden */}
                    <div className="grid gap-3 sm:grid-cols-2 pt-2 border-t border-border">
                      {r.triggers && r.triggers.length > 0 && (
                        <div>
                          <FieldLabel className="mb-2">Triggers</FieldLabel>
                          <div className="flex flex-wrap gap-1">
                            {r.triggers.map((t, j) => (
                              <span
                                key={j}
                                className="px-2 py-0.5 bg-cyan-900/30 text-cyan-300 font-mono text-caption border border-cyan-900/40"
                              >
                                {t}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                      {r.forbidden_phrases &&
                        r.forbidden_phrases.length > 0 && (
                          <div>
                            <FieldLabel className="mb-2">
                              Forbidden (Domain Law)
                            </FieldLabel>
                            <ul className="space-y-1">
                              {r.forbidden_phrases.map((f, j) => (
                                <li
                                  key={j}
                                  className="text-label text-red-300 flex items-start gap-1"
                                >
                                  <X size={14} className="flex-shrink-0 text-red-300 font-semibold" />
                                  <span>{f}</span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                    </div>

                    {/* Related playbooks */}
                    {r.related_blocks && r.related_blocks.length > 0 && (
                      <div className="pt-2 border-t border-border">
                        <FieldLabel className="mb-2">Related playbooks</FieldLabel>
                        <div className="flex flex-wrap gap-1">
                          {r.related_blocks.map((b, j) => (
                            <Button
                              key={j}
                              onClick={() => navigate(`/knowledge/blocks`)}
                              size="xs"
                              className="normal-case"
                              title={b}
                            >
                              {humanize(b)}
                            </Button>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Usage: the real MCP call (there is no `lc run-rubric` command). */}
                    <div className="pt-2 border-t border-border">
                      <SnippetCard
                        title="How an agent runs this check"
                        caption="MCP tool call; report each check as true or false."
                        body={verifyCall(r)}
                        wrap
                      />
                    </div>
              </DisclosureCard>
            );
          })}
        </div>
      </div>
    </div>
  );
}
