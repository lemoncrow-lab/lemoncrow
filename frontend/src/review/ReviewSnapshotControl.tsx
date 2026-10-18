import { ChevronDown, RefreshCw } from "lucide-react";
import { Button } from "../components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "../components/ui/dropdown-menu";

export default function ReviewSnapshotControl({ files, detail, compareHref, busy, onCapture, onHistory, onHide }: {
  files: number;
  detail: string;
  compareHref: string;
  busy: boolean;
  onCapture(): void;
  onHistory(): void;
  onHide?: () => void;
}) {
  return (
    <div className="flex shrink-0 items-center" data-testid="source-snapshot-control">
      <Button variant="outline" size="compact" disabled={busy} onClick={onCapture} aria-label="Capture current source" tooltip={busy ? undefined : detail} className="rounded-r-none border-amber-600/70 bg-amber-800/45 text-amber-50 hover:bg-amber-700/55 hover:text-amber-50">
        <RefreshCw size={12} aria-hidden="true" />
        <span>New Snapshot</span>
        <span className="text-caption tabular-nums text-current opacity-80">{files}</span>
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="compact" aria-label="New Snapshot options" className="rounded-l-none border-l-0 border-amber-600/70 bg-amber-800/45 px-1 text-amber-50 hover:bg-amber-700/55 hover:text-amber-50">
            <ChevronDown size={12} aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72">
          <DropdownMenuLabel>Newer source</DropdownMenuLabel>
          <div className="px-2 py-1.5 text-caption leading-4 text-muted-foreground">{detail}</div>
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild><a href={compareHref}>Compare source</a></DropdownMenuItem>
          <DropdownMenuItem onSelect={onHistory}>Snapshots</DropdownMenuItem>
          {onHide && <DropdownMenuItem onSelect={onHide}>Hide until source changes again</DropdownMenuItem>}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
