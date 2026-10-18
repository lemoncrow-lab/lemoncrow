# LemonCrow Frontend Design Guide

LemonCrow's UI layer follows the [shadcn/ui](https://ui.shadcn.com) model: Radix primitives for behavior, `cva` variants for styling, semantic
color tokens, and `cn()` (clsx + tailwind-merge) for composition. The primitives live in `src/components/ui/*` and are owned by the app
(copied and adapted, not imported from a package), tuned for a dense developer tool.

## Interactive controls

Application code must use LemonCrow UI primitives for interactive controls. Do not render native form controls directly from pages, review
surfaces, feature components, or tests.

- Use `Button` instead of `<button>`, `Input` instead of `<input>`, `Textarea` instead of `<textarea>`, `Select` + `SelectItem` instead of
  `<select>` + `<option>`.
- Native interactive elements may only be owned by `src/components/ui/*`, where accessibility, keyboard behavior, focus, layering, styling,
  and theme behavior are centralized.

### Buttons

`Button` variants are named by intent: `default` (primary call to action), `outline` (standard control, the default), `ghost` (quiet
icon/row actions), `link`, `destructive`, `warning`, `success`. Sizes: `xs` (28px), `sm` (32px, default), `compact` (28px inline row action),
`icon` (28px square). Use `asChild` to give a router `Link` button styling, and `tooltip=` instead of `title=` for hints.

Prefer a variant plus `className` overrides (tailwind-merge makes overrides deterministic) over `unstyled`. `unstyled` is reserved for
content-specific controls whose visuals belong to the content: tree/list rows, diff gutters, map nodes, chat bubbles, cards acting as buttons.
It means the feature owns the visual treatment, never that the feature owns the native element.

### Overlays

| Need | Primitive |
|---|---|
| Modal task panel anchored to an edge | `Sheet` / `SheetContent` |
| Centered modal | `Dialog` / `DialogContent` |
| Action or settings menu | `DropdownMenu` (settings as `DropdownMenuRadioGroup`, never a `Select` inside a menu) |
| Floating content that is not a list of actions (details, view options, searchable pickers) | `Popover` |
| Hint on a control | `Button tooltip=` or `<Tip content>` |
| Single choice among a few options | `ToggleGroup` (`pill`, `underline`, `segment`) |
| Views of one region, arrow keys switch immediately | `Tabs` (`segment`, `underline`) |
| Expand/collapse | `Collapsible` / `DisclosureCard` |
| Choose from a list | `Select` |

Overlays portal into the app shell (`PortalContainerContext`) and stack at `z-50` by DOM order; do not add arbitrary `z-[NN]` layers.
`Dialog`/`Sheet` trap focus, close on Escape and outside press, hide the page from assistive tech, and return focus to the element that
opened them. Do not hand-roll `role="dialog"`, focus traps, or `<details>` menus.

A `Button` with both `disabled` and `tooltip` stays focusable as `aria-disabled` so its reason is reachable; pass `tooltip=""` rather
than `undefined` for a hint that comes and goes. Controls grow to 44px touch targets under `pointer-coarse:`.

Native `title` remains acceptable for non-interactive truncated text and for per-line controls in very large lists, where a tooltip per row
would cost too much; icon-only controls still need an `aria-label`.

### Why this is enforced

LemonCrow is a dense developer tool. Browser-default controls immediately look inconsistent beside the Reader, menus, inspectors, and command
surfaces. Centralizing controls also prevents every feature from independently re-solving focus states, disabled states, popup layering,
keyboard navigation, theme contrast, and compact sizing.

`npm run check:ui-primitives` runs automatically before frontend tests and production builds. It enforces native-control ownership,
primitive-owned overlays, the type scale, the contrast floor for text, and a small set of workbench visual invariants.

## Tokens

### Color

Use semantic tokens first; they alias the themed ramps in `src/index.css`, so one class works in light and dark.

| Token | Use |
|---|---|
| `background` / `foreground` | page surface / primary text |
| `card`, `popover` (+ `-foreground`) | raised panels / floating surfaces |
| `primary` (+ `-foreground`) | primary action |
| `secondary` (+ `-foreground`) | standard control surface |
| `muted` / `muted-foreground` | subtle surface / secondary text (≥4.5:1 on every page surface) |
| `accent` (+ `-foreground`) | hover and selected surface |
| `destructive`, `warning`, `success`, `info` (+ `-foreground`) | status |
| `border`, `input`, `ring` | dividers / field borders / focus ring |

Raw neutrals (`neutral-200`…`neutral-400`) are fine for text hierarchy between `foreground` and `muted-foreground`. Never use
`text-neutral-600`/`700` for text: they fall below WCAG AA contrast.

Color is semantic. Tones on `Badge`, `Card`, `Alert`, `ToggleGroup` and `Chip` are `neutral | info | success | warning | destructive`, not
color names. Do not use purple/violet as a generic global accent or selected state; it is allowed for explicitly differentiated
AI/chat/runtime annotations where the color carries meaning.

### Type

Use the dense scale instead of arbitrary `text-[Npx]`: `text-micro` (9px), `text-caption` (10px), `text-label` (11px), `text-body` (12px),
`text-heading` (13px), `text-title` (14px). The scale sets font size only; line height is inherited.

## Visual system

The `/r` Review/Reader visual system is the canonical LemonCrow application language. Workbench routes must use the same neutral surfaces,
density, typography, focus treatment, control sizes, and light/dark behavior.

- Panels, menus, sheets, and cards use subtle neutral borders and `rounded-lg`.
- Controls and compact actions use `rounded-md`, with 28px compact and 32px normal heights.
- Use 12–16px panel padding and 12–16px section gaps by default.
- Do not use decorative gradients, hard-coded dark backgrounds, or large shadows to create page identity. Floating menus/sheets may use the
  same restrained elevation as Review.
- Prefer semantic and `surface-*` tokens over `bg-black`, hard-coded hex neutrals, or duplicated light/dark values.
- Normal routed pages use `PageFrame`; full-canvas tools may opt out but still inherit the same control and surface language.
- Motion is short and functional; `prefers-reduced-motion` disables it globally.

When a workbench pattern and a Review pattern disagree, align the workbench to Review unless the difference is required by the content type
(for example diff semantics, charts, graph nodes, or chat bubbles).

## Review UX gate

Every Review capability declares its primary surface in `src/review/reviewUxGate.ts` before it ships. Tests fail when a header control
renders without a `REVIEW_HEADER_CONTROLS` entry, when more than three persistent views exist (Brief, Code, Map; Guided review is transient),
or when an action moved out of the header is missing from the command palette.

Place an expert action in this order: command palette, then the target context menu, then More. The header is last, and its registry entry
must say why the other three are not enough. A new persistent view or header control needs an explicit UX review.

The rendered Reader also holds these invariants: one right rail, one blocking banner, one status authority (the Status control), and one
primary action. On a phone the composition is Back, title, More; then Brief, Code, Map; then content with a bottom dock holding the primary
action. Text fields in phone Review surfaces stay at 16px so iOS does not zoom.

The Reader records local-only friction metrics in this browser's `localStorage` under `lc.review.friction.v1`: at most 50 sessions, none
older than 30 days, holding counters and durations only (no paths, symbols, comment or Ask text). Nothing is sent anywhere. To read them,
inspect that key in devtools; `summarizeReviewFriction()` in `src/review/reviewFriction.ts` turns the sessions into medians and rates, and
`clearReviewFriction()` removes them.
