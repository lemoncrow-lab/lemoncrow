import { isDialogOpen } from "../components/ui/dialog";
import type { FlowSurface } from "./flowKeymap";

/** What owns the keyboard above a screen: an open dialog, sheet or menu (SPEC 3.5), so a plain key does not fire under it. */
export function openFlowSurface(): FlowSurface {
  if (isDialogOpen()) return "other";
  // Non-modal popovers and menus own the keyboard too.
  return document.querySelector('[role="menu"], [role="listbox"], [role="dialog"][data-state="open"], [data-radix-popper-content-wrapper]') ? "other" : "none";
}
