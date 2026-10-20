import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { cn } from "../../lib/utils";
import { Badge } from "./badge";
import { Button } from "./button";
import { EmptyState } from "./empty-state";
import { Hunk, HunkHead, HunkLabel, HunkRow } from "./hunk";
import { DisclosureCard } from "./disclosure-card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "./dropdown-menu";
import { Select, SelectItem } from "./select";
import { Sheet, SheetContent, SheetTitle } from "./sheet";
import { ToggleGroup } from "./toggle-group";
import { escapeDismissedTooltip } from "./tooltip";

describe("Sheet", () => {
  it("is a labelled modal that closes on Escape and returns focus to its opener", async () => {
    function Host() {
      const [open, setOpen] = useState(false);
      const [choice, setChoice] = useState("a");
      return (
        <>
          <Button onClick={() => setOpen(true)}>Open outcome</Button>
          <Sheet open={open} onOpenChange={setOpen}>
            <SheetContent aria-describedby={undefined}>
              <SheetTitle>Record outcome</SheetTitle>
              <Select aria-label="Verdict" value={choice} onValueChange={setChoice}>
                <SelectItem value="a">Approve</SelectItem>
                <SelectItem value="b">Request changes</SelectItem>
              </Select>
            </SheetContent>
          </Sheet>
        </>
      );
    }
    render(<Host />);
    const opener = screen.getByRole("button", { name: "Open outcome" });
    await userEvent.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Record outcome" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    // A Select popup inside the modal stays clickable and does not dismiss the sheet.
    await userEvent.click(screen.getByRole("combobox", { name: "Verdict" }));
    await userEvent.click(screen.getByRole("option", { name: "Request changes" }));
    expect(screen.getByRole("combobox", { name: "Verdict" })).toHaveAttribute("data-value", "b");
    expect(screen.getByRole("dialog", { name: "Record outcome" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.activeElement).toBe(opener);
  });
});

describe("DropdownMenu", () => {
  it("opens from its trigger and runs an item with the keyboard", async () => {
    const picked: string[] = [];
    render(
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="Review actions">⋮</Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem onSelect={() => picked.push("refresh")}>Refresh</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => picked.push("finish")}>Finish</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
    await userEvent.click(screen.getByRole("button", { name: "Review actions" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(picked).toEqual(["finish"]);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("Button tooltip", () => {
  it("shows its hint on keyboard focus", async () => {
    render(<Button tooltip="Download the frozen patch">Download</Button>);
    await userEvent.tab();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Download the frozen patch");
  });

  it("marks an Escape it swallowed to close itself, so a page shortcut can still act on it", async () => {
    render(<Button tooltip="Download the frozen patch">Download</Button>);
    await userEvent.tab();
    await screen.findByRole("tooltip");
    const seen: Array<{ prevented: boolean; tooltip: boolean }> = [];
    const onKey = (event: KeyboardEvent) => seen.push({ prevented: event.defaultPrevented, tooltip: escapeDismissedTooltip(event) });
    window.addEventListener("keydown", onKey);
    try {
      await userEvent.keyboard("{Escape}");
      await userEvent.keyboard("x");
    } finally {
      window.removeEventListener("keydown", onKey);
    }
    expect(seen).toEqual([{ prevented: true, tooltip: true }, { prevented: false, tooltip: false }]);
  });
});

describe("cn", () => {
  it("lets a later utility override a conflicting default", () => {
    expect(cn("h-9 px-3 text-xs", "h-8 text-caption")).toBe("px-3 h-8 text-caption");
    // The dense type scale is a font size, never a colour: both survive a merge.
    expect(cn("text-label text-muted-foreground", false, undefined, "text-caption")).toBe("text-muted-foreground text-caption");
    expect(cn("text-xs", "text-muted-foreground")).toBe("text-xs text-muted-foreground");
  });
});

describe("ToggleGroup", () => {
  it("exposes the active option as checked, not by color alone", async () => {
    function Host() {
      const [value, setValue] = useState("key");
      return (
        <ToggleGroup
          aria-label="Map links"
          value={value}
          onChange={setValue}
          options={[{ value: "key", label: "Key links" }, { value: "all", label: "All links" }]}
        />
      );
    }
    render(<Host />);

    expect(screen.getByRole("radiogroup", { name: "Map links" })).toBeTruthy();
    expect(screen.getByRole("radio", { name: "Key links" })).toHaveAttribute("aria-checked", "true");
    await userEvent.click(screen.getByRole("radio", { name: "All links" }));
    expect(screen.getByRole("radio", { name: "Key links" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radio", { name: "All links" })).toHaveAttribute("aria-checked", "true");
    // Re-clicking the active option keeps a choice (Radix would otherwise clear it).
    await userEvent.click(screen.getByRole("radio", { name: "All links" }));
    expect(screen.getByRole("radio", { name: "All links" })).toHaveAttribute("aria-checked", "true");
  });
});

describe("DisclosureCard", () => {
  it("exposes expanded state and the region it controls", async () => {
    function Host() {
      const [open, setOpen] = useState(false);
      return (
        <DisclosureCard open={open} onToggle={() => setOpen((value) => !value)} header="Details">
          Body text
        </DisclosureCard>
      );
    }
    render(<Host />);

    const trigger = screen.getByRole("button", { name: "Details" });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    await userEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(document.getElementById(trigger.getAttribute("aria-controls") ?? "")?.textContent).toBe("Body text");
  });
});


describe("Button variants", () => {
  it("is an outline control by default; the lemon fill exists only as variant brand", () => {
    render(
      <>
        <Button>Default</Button>
        <Button variant="brand">Send feedback</Button>
        <Button variant="solid">Start</Button>
      </>,
    );
    expect(screen.getByRole("button", { name: "Default" }).className).toContain("border-input");
    expect(screen.getByRole("button", { name: "Send feedback" }).className).toContain("bg-brand");
    expect(screen.getByRole("button", { name: "Start" }).className).toContain("bg-primary");
    expect(screen.getByRole("button", { name: "Default" }).className).not.toContain("bg-brand");
  });

  it("keeps a hinted disabled button focusable instead of swallowing the hint", () => {
    render(<Button disabled tooltip="Nothing to send">Send</Button>);
    const button = screen.getByRole("button", { name: "Send" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).not.toBeDisabled();
  });
});

describe("Badge", () => {
  it("has severity tones that read P0 red, P1 amber and P2 cyan, and an ink New badge", () => {
    render(
      <>
        <Badge tone="p0">P0</Badge>
        <Badge tone="p1">P1</Badge>
        <Badge tone="p2">P2</Badge>
        <Badge tone="new">New</Badge>
        <Badge tone="ai">AI</Badge>
      </>,
    );
    expect(screen.getByText("P0").className).toContain("text-p0");
    expect(screen.getByText("P1").className).toContain("text-p1");
    expect(screen.getByText("P2").className).toContain("text-p2");
    expect(screen.getByText("New").className).toContain("bg-foreground");
    expect(screen.getByText("AI").className).toContain("text-ai");
    // One shape everywhere: 20px, mono, uppercase.
    expect(screen.getByText("P0").className).toContain("h-5");
    expect(screen.getByText("P0").className).toContain("font-mono");
    expect(screen.getByText("P0").className).toContain("uppercase");
  });
});

describe("Hunk", () => {
  it("is a hairline frame with a header strip, and a row rail that carries state", () => {
    render(
      <Hunk data-testid="frame">
        <HunkHead><HunkLabel>Reviews</HunkLabel></HunkHead>
        <HunkRow rail="p1" data-testid="row">row</HunkRow>
      </Hunk>,
    );
    expect(screen.getByTestId("frame").className).toContain("hunk");
    expect(screen.getByText("Reviews").className).toContain("hunk-label");
    expect(screen.getByTestId("row").className).toContain("border-l-p1-solid");
  });
});

describe("EmptyState", () => {
  it("shows one title, one sentence and at most one action inside a Hunk", () => {
    render(
      <EmptyState
        label="No reviews"
        title="No reviews match this view"
        description="Clear the filters."
        action={<Button>Clear filters</Button>}
      />,
    );
    expect(screen.getByText("No reviews").className).toContain("hunk-label");
    expect(screen.getByText("No reviews match this view")).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("drops the frame when it sits inside another panel", () => {
    const { container } = render(<EmptyState frame={false} title="Nothing here" />);
    expect(container.querySelector(".hunk")).toBeNull();
  });
});
