import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { SNOOZE_CHOICES, type SnoozeOption } from "./homeModel";

/** The snooze dialog (SPEC 4.1). Keys 1 to 4 are handled by the Home keymap (`snoozePick`); the buttons do the same for a pointer. */
export function SnoozePicker({ open, count, onPick, onClose }: {
  open: boolean;
  count: number;
  onPick: (option: SnoozeOption) => void;
  onClose: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{count === 1 ? "Snooze this review" : `Snooze ${count} reviews`}</DialogTitle>
        </DialogHeader>
        <DialogBody className="space-y-3">
          <ul className="space-y-2">
            {SNOOZE_CHOICES.map((choice) => (
              <li key={choice.option}>
                <Button variant="outline" className="w-full justify-between" onClick={() => onPick(choice.option)}>
                  {choice.label}
                  <kbd className="rounded border border-border px-1 font-mono text-tag pointer-coarse:hidden">{choice.option}</kbd>
                </Button>
              </li>
            ))}
          </ul>
          <DialogDescription className="text-meta text-muted-foreground pointer-coarse:hidden">
            Press 1 to 4, or Esc to cancel.
          </DialogDescription>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
