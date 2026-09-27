import { Check } from "lucide-react";
import type { CodeMapFacet } from "../api";

function FacetList({
  title,
  facets,
  selected,
  onToggle,
  onAll,
}: {
  title: string;
  facets: CodeMapFacet[];
  selected: ReadonlySet<string>;
  onToggle: (id: string) => void;
  onAll: () => void;
}) {
  const allSelected = facets.length > 0 && selected.size === facets.length;
  return (
    <div className="border-b border-neutral-800 p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-[10px] font-semibold uppercase tracking-widest text-neutral-200">{title}</h3>
        <button type="button" onClick={onAll} className="text-[10px] text-neutral-300 hover:text-white">
          {allSelected ? "Clear" : "All"}
        </button>
      </div>
      <div className="max-h-52 space-y-0.5 overflow-y-auto pr-1">
        {facets.map((facet) => {
          const active = selected.has(facet.id);
          return (
            <button
              key={facet.id}
              type="button"
              onClick={() => onToggle(facet.id)}
              aria-pressed={active}
              className="flex w-full items-center gap-2 px-1.5 py-1.5 text-left text-[11px] text-neutral-300 transition hover:bg-neutral-900 hover:text-white"
            >
              <span className={`flex h-3.5 w-3.5 items-center justify-center border ${active ? "border-neutral-500 bg-neutral-700" : "border-neutral-700"}`}>
                {active && <Check size={10} />}
              </span>
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: facet.color || "#737373" }} />
              <span className="min-w-0 flex-1 truncate">{facet.label}</span>
              <span className="text-neutral-400">{facet.count.toLocaleString()}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function CodeMapFilters({
  groups,
  fileTypes,
  languages,
  selectedGroups,
  selectedFileTypes,
  selectedLanguage,
  onToggleGroup,
  onToggleAllGroups,
  onToggleFileType,
  onToggleAllFileTypes,
  onLanguageChange,
}: {
  groups: CodeMapFacet[];
  fileTypes: CodeMapFacet[];
  languages: CodeMapFacet[];
  selectedGroups: ReadonlySet<string>;
  selectedFileTypes: ReadonlySet<string>;
  selectedLanguage: string;
  onToggleGroup(id: string): void;
  onToggleAllGroups(): void;
  onToggleFileType(id: string): void;
  onToggleAllFileTypes(): void;
  onLanguageChange(language: string): void;
}) {
  return (
    <>
      <FacetList title="Groups" facets={groups} selected={selectedGroups} onToggle={onToggleGroup} onAll={onToggleAllGroups} />
      <FacetList title="File types" facets={fileTypes} selected={selectedFileTypes} onToggle={onToggleFileType} onAll={onToggleAllFileTypes} />
      <div className="border-b border-neutral-800 p-3">
        <label className="text-[10px] font-semibold uppercase tracking-widest text-neutral-200" htmlFor="map-language">
          Language
        </label>
        <select
          id="map-language"
          value={selectedLanguage}
          onChange={(event) => onLanguageChange(event.target.value)}
          className="mt-2 h-9 w-full border border-neutral-700 bg-neutral-900 px-2 text-xs text-neutral-200"
        >
          <option value="all">All languages</option>
          {languages.map((language) => (
            <option key={language.id} value={language.id}>
              {language.label} · {language.count}
            </option>
          ))}
        </select>
      </div>
    </>
  );
}
