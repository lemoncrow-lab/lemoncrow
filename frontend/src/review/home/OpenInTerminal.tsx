import { Copy } from "lucide-react";

import { Button } from "../../components/ui/button";

/** The exact command that opens a review's place in the terminal (HOME-7). `reviewPath` is `/r/<review>`. */
export function openCommand(reviewPath: string): string {
  return `lc review open ${reviewPath}`;
}

/** SPEC 4.1: "Run this in your terminal. It opens the same place as this page." with the command and Copy. */
export function OpenInTerminal({ command, intro = "Run this in your terminal. It opens the same place as this page.", onCopy, testId = "home-terminal" }: {
  command: string;
  intro?: string;
  onCopy: (command: string) => void;
  testId?: string;
}) {
  return (
    <div data-testid={testId} className="mt-3 space-y-2 rounded-md border border-border bg-muted/40 p-3">
      <p className="text-label text-muted-foreground">{intro}</p>
      <div className="flex flex-wrap items-center gap-2">
        <code data-testid={`${testId}-command`} className="min-w-0 flex-1 break-all rounded-sm bg-card px-2 py-1 font-mono text-code">{command}</code>
        <Button variant="outline" className="min-w-11" aria-label={`Copy command: ${command}`} onClick={() => onCopy(command)}>
          <Copy size={14} aria-hidden="true" /> Copy
        </Button>
      </div>
      <p className="text-meta text-muted-foreground">Also: lc review · lc review next · lc review status --json</p>
    </div>
  );
}
