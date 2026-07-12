import { useEffect, useState, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  X,
} from "lucide-react";
import { api, type Rubric } from "../api";
import {
  Alert,
  Button,
  Chip,
  DisclosureCard,
  EmptyState,
  FieldLabel,
  Select,
} from "../components/WorkbenchUI";

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

  if (err) return <Alert tone="danger" description={err} />;
  if (!items) return <EmptyState title="Loading rubrics…" className="p-6" />;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        {rubricId && (
          <Button
            onClick={() => navigate("/knowledge/rubrics")}
            size="xs"
            icon={<ArrowLeft size={12} />}
          >
            All rubrics
          </Button>
        )}
        <FieldLabel>
          {rubricId
            ? `Rubric · ${items.find((r) => r.id === rubricId)?.domain || rubricId}`
            : `Rubrics · ${items.length} total`}
        </FieldLabel>
      </div>

      {/* Rubrics List */}
      <div className="space-y-4">
        {/* Filter */}
        <div className="flex gap-2 items-center px-4 py-2">
          <Select
            aria-label="Filter rubrics by domain"
            value={domainFilter}
            onChange={(e) => setDomainFilter(e.target.value)}
            uiSize="xs"
            className="text-neutral-400"
          >
            <option value="all">All domains</option>
            {domains.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </Select>
          <FieldLabel className="ml-auto text-neutral-400">
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
                    <Chip tone="neutral">{r.domain}</Chip>
                    <div className="min-w-0 flex-1">
                      <div className="mb-1 flex flex-wrap items-center gap-2">
                        <ChevronRight
                          size={14}
                          className={`text-neutral-400 transition-transform ${
                            isExpanded ? "rotate-90" : ""
                          }`}
                        />
                        <h3 className="font-mono font-bold text-neutral-200">
                          {r.id}
                        </h3>
                      </div>
                      <p className="text-xs text-neutral-400">
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
                        <ChevronRight size={10} className="inline mr-1" /> ID
                      </FieldLabel>
                      <code className="text-[10px] bg-neutral-950 px-2 py-1 text-neutral-400 font-mono border border-neutral-800 block break-all">
                        {r.id}
                      </code>
                    </div>

                    {/* Timestamps */}
                    <div>
                      <FieldLabel className="mb-2">
                        <ChevronRight size={10} className="inline mr-1" /> created
                      </FieldLabel>
                      <p className="text-xs text-neutral-400 font-mono">
                        {new Date(r.created_at).toLocaleString()}
                      </p>
                      {r.updated_at && (
                        <p className="text-xs text-neutral-400 font-mono mt-1">
                          Updated: {new Date(r.updated_at).toLocaleString()}
                        </p>
                      )}
                    </div>

                    {/* Required Checks */}
                    <div>
                      <FieldLabel className="mb-2">
                        <ChevronRight size={10} className="inline mr-1" /> required checks
                      </FieldLabel>
                      <ul className="space-y-1">
                        {r.required_checks.map((check, i) => (
                          <li
                            key={i}
                            className="text-[11px] text-neutral-300 leading-relaxed bg-neutral-900/40 px-2 py-1 border border-neutral-800 flex items-start gap-2"
                          >
                            <Check size={14} className="text-emerald-300 flex-shrink-0 mt-0.5" />
                            <span>{check}</span>
                          </li>
                        ))}
                      </ul>
                    </div>

                    {/* Triggers & Forbidden */}
                    <div className="grid gap-3 sm:grid-cols-2 pt-2 border-t border-neutral-800">
                      {r.triggers && r.triggers.length > 0 && (
                        <div>
                          <FieldLabel className="mb-2">Triggers</FieldLabel>
                          <div className="flex flex-wrap gap-1">
                            {r.triggers.map((t, j) => (
                              <span
                                key={j}
                                className="px-2 py-0.5 bg-blue-900/30 text-blue-300 font-mono text-[10px] border border-blue-900/40"
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
                                  className="text-[11px] text-red-300 flex items-start gap-1"
                                >
                                  <X size={14} className="flex-shrink-0 text-red-300 font-bold" />
                                  <span>{f}</span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                    </div>

                    {/* Related Blocks */}
                    {r.related_blocks && r.related_blocks.length > 0 && (
                      <div className="pt-2 border-t border-neutral-800">
                        <FieldLabel className="mb-2">Related blocks</FieldLabel>
                        <div className="flex flex-wrap gap-1">
                          {r.related_blocks.map((b, j) => (
                            <Button
                              key={j}
                              onClick={() => navigate(`/knowledge/blocks`)}
                              size="xs"
                              className="normal-case"
                            >
                              {b}
                            </Button>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Usage */}
                    <div className="pt-2 border-t border-neutral-800">
                      <FieldLabel className="mb-2">Usage</FieldLabel>
                      <code className="text-[10px] bg-neutral-950 px-2 py-1 block text-neutral-300 break-all font-mono border border-neutral-800 mb-1">
                        lc run-rubric {r.id} --json '{"{"}...{"}"}'
                      </code>
                      <code className="text-[10px] bg-neutral-950 px-2 py-1 block text-neutral-300 break-all font-mono border border-neutral-800">
                        MCP: verify --rubric_id {r.id} --checks '{"{"}...{"}"}'
                      </code>
                    </div>
              </DisclosureCard>
            );
          })}
        </div>
      </div>
    </div>
  );
}
