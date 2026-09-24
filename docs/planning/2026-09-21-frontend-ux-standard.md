# LemonCrow workbench UX standard

Status: active standard for the local workbench.

## Product shape

The Review/Reader visual system is the global LemonCrow application theme. Dashboard and operational surfaces use the same neutral surface hierarchy, restrained borders, UI typography, rounded controls, neutral active/focus states, and light/dark behavior. Purple is not a global brand/accent color; color is reserved for semantic state such as success, warning, error, or deliberately differentiated runtime/review annotations.


LemonCrow is an operating workbench, not a marketing surface. The UI should maximize useful state and actions per screen while remaining calm, legible, and predictable.

The shell owns page identity. A routed page must not repeat its own name in a hero, H1, icon/title block, or explanatory intro simply to announce where the user is.

Object detail views are different: a selected review, session, symbol, report, swarm run, or other concrete object may show its own title because that identifies the object being inspected.

## Navigation hierarchy

Primary navigation is always in the global header:

- Home
- Reviews
- Runs
- Code
- Usage

Settings/setup/diagnostic surfaces are intentionally outside the primary product hierarchy and are reached through the compact gear control in the global header.

Second-level navigation is always in the shared shell row directly below the primary header:

- Home is attention-first: reviews needing action, failed/active runs, recent work, and only compact value context. Healthy system state stays invisible.
- Reviews is the human review inbox. Local mode exposes real local workflow state only; open reviews are presented as Needs review rather than inventing hosted collaboration states.
- Runs is one history/execution surface. Agent sessions and swarm runs are run types selected with a filter, not separate product navigation.
- Code has no permanent second-level navigation. Search and symbol understanding are the default product surface; the relationship map is an explicit view and defaults to 2D. Internal knowledge routes remain directly reachable for diagnostics and development.
- Usage is one dashboard at /usage with no secondary navigation. Cost, savings, reduction, timeline, and usage breakdowns live together. Savings/reduction metrics open contextual evidence in the right-side inspector; advisor, benchmarks, and deeper evidence remain contextual rather than separate routes.
- Settings: Integrations, Diagnostics, Telemetry, Advanced.

Advanced/internal routes such as Knowledge, Hosts, Agents, Skills, MCP details, Watchdogs, and Projection may remain directly addressable but must not occupy permanent product navigation.

Third-level navigation belongs inside the content surface only when it is genuinely a subdivision of a visible second-level product surface.

Do not create a second page-local tab bar for a hierarchy level already represented by the shell.

## Page composition

Normal pages use PageFrame:

- maximum content width: 1600px
- horizontal padding: 20px, 24px on large screens
- top/bottom padding: 16px
- normal vertical section gap: 16px

Full-canvas tools such as Runs and the Code relationship map may bypass PageFrame, but their toolbars and controls must use the same density and visual language.

Preferred page order:

1. shell navigation
2. compact toolbar/filter/status row, if needed
3. metrics or primary content
4. actual content sections

Avoid introductory prose when the navigation and controls already explain the surface. Put help text next to the specific control or state it explains.

## Titles and headings

Do not add:

- page-name H1s
- marketing-style page heroes
- eyebrow + page-title + description blocks
- repeated page titles inside nested tabs

Use headings only for real content sections or selected objects.

Section headings should generally be small and functional. Prefer a 10–12px uppercase section label for dense operational groupings. Use larger headings only when the content hierarchy genuinely needs them.

## Density

Default control heights:

- compact button: 28px
- normal button: 32px
- compact input/select/tab: 32px
- normal input/select: 36px

Default surface spacing:

- cards: 12–16px internal padding
- section gaps: 12–16px
- empty states: approximately 24px padding, not large hero whitespace
- disclosure headers/content: 12–16px

Avoid large p-10, p-12, p-16, or oversized vertical whitespace unless the screen is intentionally an empty onboarding state.

## Surfaces

Cards and panels use:

- 1px subtle neutral border
- rounded-sm
- low-contrast neutral background
- semantic color only when conveying state, warning, success, or selection

Do not introduce isolated rounded-xl / rounded-2xl components into the workbench.

## Controls

Use the shared Button, Input, Select, ToggleGroup, Card, EmptyState, DisclosureCard, and PageFrame primitives instead of recreating their sizing and focus styles on each page.

Prefer compact controls for filters and toolbar actions. Primary actions should be visually stronger because of semantic importance, not because they are larger.

Focus treatment should remain subtle; avoid thick or highly saturated focus borders.

## Tables, lists, and operational data

Operational data takes precedence over decorative chrome.

- Keep table/list headers compact.
- Keep row heights only as large as needed for scanability.
- Put filters close to the data they affect.
- Show status/count metadata inline when it avoids another card or header.
- Do not wrap a table in multiple redundant cards/panels.

## State and color

Color is semantic:

- green: healthy/success/ready
- amber: warning/attention
- red: error/destructive
- brand accent: selection or primary product action
- neutral: ordinary structure and metadata

Do not color entire sections merely to distinguish them visually.

## Empty, loading, and error states

Empty/loading/error states must preserve the surrounding layout and stay compact.

- Loading text/spinners should not create a separate hero.
- Empty states should explain the next useful action when one exists.
- Errors should appear near the failed surface and should not replace unrelated navigation.

## Conditional/advanced surfaces

Rare functionality should not occupy permanent navigation space when it has no state to inspect.

Swarm execution is a Runs filter/type, not a separate navigation item. The underlying swarm capability remains available whether or not history exists.

Healthy setup/diagnostic state should remain quiet. Integrations and diagnostics live under Settings; lower-level runtime inspectors remain reachable from Settings → Advanced without dominating the common workflow.

## Review rule for new frontend work

Before adding a new frontend pattern, check whether an existing shared primitive or navigation level already solves it. A change that adds a new page wrapper, title treatment, tab style, card radius, control height, or spacing scale should be treated as a design-system change and justified at the shared component level first.
