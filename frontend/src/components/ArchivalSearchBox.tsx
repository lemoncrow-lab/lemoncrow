import { useState, type FormEvent } from "react";
import { Button, Input } from "./WorkbenchUI";

interface ArchivalSearchBoxProps {
  loading: boolean;
  onSearch: (query: string) => void;
}

export default function ArchivalSearchBox({
  loading,
  onSearch,
}: ArchivalSearchBoxProps) {
  const [query, setQuery] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSearch(query.trim());
  };

  return (
    <form
      onSubmit={submit}
      className="flex items-center gap-2 pb-3 border-b border-neutral-800"
    >
      <label htmlFor="archival-query" className="sr-only">
        Search archival memory
      </label>
      <Input
        id="archival-query"
        aria-label="Archival memory query"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search archival memory"
        className="flex-1 bg-transparent"
      />
      <Button
        type="submit"
        aria-label="Search archival memory"
        disabled={loading}
      >
        {loading ? "Searching..." : "Search"}
      </Button>
    </form>
  );
}
