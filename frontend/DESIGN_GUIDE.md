# LemonCrow Frontend Design Guide

LemonCrow's UI layer follows the [shadcn/ui](https://ui.shadcn.com) model: Radix primitives for behavior, `cva` variants for styling, semantic
color tokens, and `cn()` (clsx + tailwind-merge) for composition. The primitives live in `src/components/ui/*` and are owned by the app
(copied and adapted, not imported from a package), tuned for a dense developer tool.

The visual language is **Ink and Lemon**, shared with the landing site. The tokens, the Tailwind preset, the Hunk components and the fonts live
in the repository's `design/` directory (canonical) and are vendored byte-for-byte into `src/design/` (`npm run sync:design`; the UI guard
fails on drift). Read `design/README.md` before changing any of it. The public mirror carries only the vendored `src/design/`, which is all the
build needs; the guard skips the drift check when `design/` is absent. The full specification (principles, scale, motif, accent budget, contrast
table) is `research_notes/LemonCrow strategy memo assessment/design_system_spec.md`; this guide is how it applies to the app.

## The system in ten rules

1. **One motif, the Hunk.** Every container is a hairline frame with an optional mono `@@ LABEL @@` header strip and an optional 3px gutter rail
   (`Hunk`, `HunkHead`, `HunkLabel`, `HunkBody`, `HunkRow` in `components/ui/hunk.tsx`). The rail carries state (severity, kind, review status),
   is always redundant with a text badge, and is never decoration. Nest at most two deep.
2. **One accent: lemon.** `#FFDC00` is a fill or a mark: the one primary action of a view (`Button variant="brand"`), the logo, the active
   indicator and focus ring on dark surfaces, the native-control accent. Never lemon text on a light surface, never a status colour.
3. **Every other colour has one job.** Red is P0 and must-fix. Amber is P1 and still-to-do. Cyan is P2 and to-re-check. Green is fixed or
   success. Violet is AI or agent authorship, only in attribution chips. Ink and paper do everything else.
4. **Three voices.** Red Hat Display for display (the app barely uses it), Red Hat Text for reading and UI, JetBrains Mono for labels, paths,
   numbers and badges. Fonts are self-hosted and bundled under `/assets` (the local server serves nothing else).
5. **One type scale.** No arbitrary sizes, no Tailwind defaults (see Type).
6. **One button recipe.** Variants by intent, at most one `brand` per view.
7. **One badge shape.** 20px, mono, uppercase, `rounded-sm`.
8. **Two shadows.** `shadow-float` for overlays, `shadow-frame` for the one hero artefact. Cards have none.
9. **Drift is a build failure.** `npm run check:ui-primitives` enforces the rules below on every `.ts`, `.tsx` and `.css` file, and on `enterprise/frontend/src` (the hosted app, private checkout only) when it is present.
10. **Same system, denser.** 13px body, 28 and 32px controls, 12 to 16px panel padding, `rounded-lg` frames, `rounded-md` controls.

## Interactive controls

Application code must use LemonCrow UI primitives for interactive controls. Do not render native form controls directly from pages, review
surfaces, feature components, or tests.

- Use `Button` instead of `<button>`, `Input` instead of `<input>`, `Textarea` instead of `<textarea>`, `Select` + `SelectItem` instead of
  `<select>` + `<option>`.
- Native interactive elements may only be owned by `src/components/ui/*`, where accessibility, keyboard behavior, focus, layering, styling,
  and theme behavior are centralized.

### Buttons

`Button` variants are named by intent:

| Variant | Use |
|---|---|
| `brand` | The one primary call to action of a view (lemon fill, ink border on light). Reader: Send feedback or the primary action. Settings pages: the form's save. Never two in one viewport. |
| `solid` | An ink fill for a strong secondary action, and the selected state of a toggle-like button. Sheet and dialog confirmations. |
| `outline` | The standard control and the default. |
| `ghost` | Quiet icon and row actions. |
| `link` | Inline text action. |
| `destructive`, `warning`, `success` | Tinted actions, using the P0, P1 and ok tokens. |

Sizes: `xs` (28px), `sm` (32px, default), `compact` (28px inline row action), `icon` (28px square). Text is 12px (`text-meta`). Use `asChild`
to give a router `Link` button styling, and `tooltip=` instead of `title=` for hints. Buttons press with `active:translate-y-px` and show the
global focus ring.

Prefer a variant plus `className` overrides (tailwind-merge makes overrides deterministic) over `unstyled`. `unstyled` is reserved for
content-specific controls whose visuals belong to the content: tree/list rows, diff gutters, map nodes, chat bubbles, cards acting as buttons.
It means the feature owns the visual treatment, never that the feature owns the native element.

### Badges

`Badge` tones: `p0`, `p1`, `p2` (severity; they come first in a row), `new` (ink, never lemon), `carried`, `fixed`, `ai` (attribution only), and
the generic `neutral`, `secondary`, `info`, `success`, `warning`, `destructive`. A comment's kind uses `KIND_BADGE_TONES` in
`review/annotationModel.ts`: request change P0, suggestion P2, comment neutral. AI findings show P0, P1 or P2 from the `severity` the server sends.

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
| A framed object with a header strip | `Hunk` |
| Nothing to show | `EmptyState` (one recipe: optional icon, one-line title, one sentence, at most one action; `density="compact"` for a slot inside a strip, as on Home's savings band). It says what will appear and the one real route or action that gets there; never "No data" |

A `Sheet` or `Dialog` opens with the Hunk header strip: `DialogHeader` / `SheetHeader` is the 36px mono strip and its `DialogTitle` /
`SheetTitle` draws as `@@ TITLE @@` (the markers are CSS, so the accessible name is the title text and nothing else). The right slot takes
at most one meta string, badge or the close button; the one sentence that says what the overlay will and will not do goes in
`DialogLede` / `SheetLede` under the strip (it is the dialog's description), and a status line that is not a description uses `LEDE_CLASS`.
A title outside a header strip stays an ordinary heading (the command palette).

Overlays portal into the app shell (`PortalContainerContext`) and stack at `z-50` by DOM order; do not add arbitrary `z-[NN]` layers.
`Dialog`/`Sheet` trap focus, close on Escape and outside press, hide the page from assistive tech, and return focus to the element that
opened them. Do not hand-roll `role="dialog"`, focus traps, or `<details>` menus. Overlays use `shadow-float`.

A `Button` with both `disabled` and `tooltip` stays focusable as `aria-disabled` so its reason is reachable; pass `tooltip=""` rather
than `undefined` for a hint that comes and goes. Controls grow to 44px touch targets under `pointer-coarse:`, and text fields go to 16px there
so iOS does not zoom.

Native `title` remains acceptable for non-interactive truncated text and for per-line controls in very large lists, where a tooltip per row
would cost too much; icon-only controls still need an `aria-label`.

### Why this is enforced

LemonCrow is a dense developer tool. Browser-default controls immediately look inconsistent beside the Reader, menus, inspectors, and command
surfaces. Centralizing controls also prevents every feature from independently re-solving focus states, disabled states, popup layering,
keyboard navigation, theme contrast, and compact sizing.

`npm run check:ui-primitives` runs automatically before frontend tests and production builds. It enforces native-control ownership,
primitive-owned overlays, the type scale, the colour tokens, the elevation scale, the weight scale, and that `src/design/` matches `design/`.
Outside a short allow-list of content and theme files (editor and markdown themes, graph canvases, chart series) it rejects: hex and literal
`rgb()` colours, raw `neutral-*` and any hue utility outside red, amber, cyan, emerald and violet, arbitrary `text-[…]`, `rounded-[…]`,
`shadow-[…]` and `tracking-[…]`, Tailwind's default `text-xs` to `text-9xl` and `shadow-sm` to `shadow-2xl`, and `font-bold`.

## Tokens

### Color

Use the semantic tokens; they are RGB triplets defined once per theme in `src/design/lc-tokens.css` (`:root` is light, `.dark` is dark; the
app puts one class on `<html>`), so one class works in both themes and takes alpha (`bg-brand/10`).

| Token | Use |
|---|---|
| `background` / `foreground` | page surface (paper `#F8F6F1`, ink `#0A0A0A`) / primary text |
| `card`, `popover` (+ `-foreground`) | raised panels and Hunk bodies / floating surfaces |
| `muted` / `muted-foreground` | sunken surface, Hunk header strips, table heads / muted text (at least 4.5:1 on every page surface) |
| `subtle` | secondary text |
| `accent` (+ `-foreground`) | hover and selected surface |
| `border`, `input`, `ring`, `indicator` | hairlines / form-field border (3:1) / focus ring and active indicator (ink on light, lemon on dark) |
| `primary` (+ `-foreground`) | the ink fill (`Button solid`) |
| `brand` (+ `-foreground`, `-hover`, `-border`) | the lemon accent group; `brand-700` or darker for text on a light surface |
| `p0`, `p1`, `p2`, `ok`, `ai` (+ `-bg`, `-line`, `-solid`) | severity, success and attribution: text, tint, hairline and rail colour |

The app's `neutral-*`, `surface-*` and hue ramps (`red`, `amber`, `cyan`, `emerald`, `violet`) still exist in `src/index.css` for
the utilities already in code, but new code uses the semantic tokens. Raw `neutral-*` and the other hues are rejected by the guard.

**The accent is one token group** and can be swapped for purple by loading `design/lc-accent-purple.css` (see `design/README.md`). This guide
used to forbid purple as a global accent; it now forbids any accent other than the group above: lemon is the brand, violet is attribution.

**Where lemon is allowed** (the budget, exhaustive for the app): the one `brand` Button per view, the logo, the active indicator and focus ring
on dark surfaces, `::selection`, the native checkbox and radio accent. Forbidden: lemon text on a light surface, lemon as a severity or "new"
colour, as a link colour, as a border on light surfaces except the brand button's ink border, in gradients, or as a large background.

Colour is semantic. Tones on `Badge`, `Card`, `Alert`, `ToggleGroup` and `Chip` are `neutral | info | success | warning | destructive`
(plus the severity tones on `Badge`), not colour names. Sources (human, author, LemonCrow, AI review) are neutral chips; only AI and
agent authorship uses `ai`.

### Type

One scale, in `src/design/lc-preset.cjs`. The app uses the dense names, one pixel up from the old scale:

| Class | Size | Use |
|---|---|---|
| `text-micro` | 10px | uppercase mono badges only, never sentences |
| `text-caption` | 11px | mono meta, counts, dense secondary text |
| `text-label` | 12px | captions, hints, dense UI text |
| `text-body` | 13px | the app's body size |
| `text-heading` | 14px | panel and section titles |
| `text-title` | 16px | page titles, the Brief headline |
| `text-tag`, `text-meta`, `text-ui`, `text-small`, `text-copy`, `text-lead`, `text-h3`, `text-h2`, `text-display`, `text-code` | | the marketing and shared names; `text-tag` is the 11px mono eyebrow (uppercase, +0.12em), `text-meta` 12px, `text-ui` 13px, `text-lead` 18px |

Nothing interactive or primary is below 12px: buttons, inputs, tabs, menu items and nav are `text-meta` (12) or `text-ui` (13). The scale sets
font size only; line height is inherited. Use `font-mono tabular-nums` for numbers in tables and counters. Weights are 400, 500 and 600; 700 is
retired. A page opens with `PageHeader` (mono eyebrow, `text-title` title, one `text-body` lede); a Hunk opens with `HunkLabel`.

For the few CSS rules that cannot use a utility, `src/index.css` exposes `--fs-micro` to `--fs-title`.

### Motion

120ms colour transitions on hover and press, 200ms disclosure and tab indicator. No bounce, no scale above 1.02, no looping animation other
than a spinner. `prefers-reduced-motion` drops every transition and animation globally (the preset's base layer), and the typed wordmark
completes instantly.

## Visual system

The `/r` Review/Reader visual system is the canonical LemonCrow application language. Workbench routes must use the same surfaces,
density, typography, focus treatment, control sizes, and light/dark behavior.

- Panels, menus, sheets, and cards use hairline `border-border` and `rounded-lg`.
- Controls and compact actions use `rounded-md`, with 28px compact and 32px normal heights. Badges and chips are `rounded-sm`.
- Use 12–16px panel padding and 12–16px section gaps by default.
- No gradients, no hard-coded dark backgrounds, no shadows on cards. Floating menus and sheets use `shadow-float`.
- Prefer semantic tokens over `bg-black`, hex, or duplicated light/dark values.
- Normal routed pages use `PageFrame` and open with `PageHeader`; full-canvas tools may opt out but still inherit the same control and surface
  language.
- Icons are lucide, in `currentColor`, muted by default, on one scale: `size={14}` (dense rows, inline with 12 to 13px text; the default in this
  dense tool), `size={16}` (back arrows, loaders, standalone controls) and `size={20}` (empty states and feature headings). The 1.75 stroke is
  global (`svg.lucide` in `src/index.css`), so no call site sets `strokeWidth`. `check:ui-primitives` rejects any other `size={N}` and any
  `strokeWidth` on an icon. File-type glyphs in the diff outline keep their colours because they encode content, and are fixed at 14px
  (`FileTypeIcon` takes no size).
- Motion is short and functional.

The diff body (`@pierre/diffs`), Monaco, CodeMirror, the markdown preview, the graph canvases and the chart series keep their own palettes: they
are content, not chrome. Restyle the chrome around them only.

When a workbench pattern and a Review pattern disagree, align the workbench to Review unless the difference is required by the content type
(for example diff semantics, charts, graph nodes, or chat bubbles).

## Finding and comment cards

A thread is one Hunk (`AnnotationCard`): a neutral header strip with the severity badge first (AI findings), then the kind badge (human
request change or suggestion), then a neutral source chip, the file or symbol, the Snapshot and the date; the body in `text-heading`; the actions
as ghost buttons. The 3px rail is `annotationRail()`: AI findings by severity, human comments by kind. The server rates each AI finding with the product's one
severity rule (`criticality.severity_for`, the function the PR bot uses) and sends it as the optional `severity` (`p0 | p1 | p2`) on the
annotation wire; a finding with no severity evidence, an older server, and every human comment send none, and the card then shows no severity
badge and a neutral rail rather than a guess. The Reader never derives it client-side.

## Review UX gate

Every Review capability declares its primary surface in `src/review/reviewUxGate.ts` before it ships. Tests fail when a header control
renders without a `REVIEW_HEADER_CONTROLS` entry, when more than three persistent views exist (Brief, Code, Map; Guided review is transient),
or when an action moved out of the header is missing from the command palette.

Place an expert action in this order: command palette, then the target context menu, then More. The header is last, and its registry entry
must say why the other three are not enough. A new persistent view or header control needs an explicit UX review.

The rendered Reader also holds these invariants: one right rail, one blocking banner, one status authority (the Status control), and one
primary action, which is the one `brand` button. On a phone the composition is Back, title, More; then Brief, Code, Map; then content with a
bottom dock holding the primary action. Text fields in phone Review surfaces stay at 16px so iOS does not zoom.

The Reader records local-only friction metrics in this browser's `localStorage` under `lc.review.friction.v1`: at most 50 sessions, none
older than 30 days, holding counters and durations only (no paths, symbols, comment or Ask text). Nothing is sent anywhere. To read them,
inspect that key in devtools; `summarizeReviewFriction()` in `src/review/reviewFriction.ts` turns the sessions into medians and rates, and
`clearReviewFriction()` removes them.
