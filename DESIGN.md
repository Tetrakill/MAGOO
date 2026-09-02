---
name: Magoo
description: The Ops Console — a calm, dense industrial instrument for EVE production planning
colors:
  signal-teal: "#3fc1c9"
  teal-ink: "#04191b"
  void-black: "#0a1014"
  void-panel: "#10181e"
  void-header: "#121c23"
  hairline: "#1f2b33"
  hairline-strong: "#2f3f4a"
  frost-text: "#d2e0e6"
  slate-dim: "#7d94a0"
  profit-green: "#4ad295"
  caution-amber: "#e6b450"
  loss-red: "#f2655f"
  zebra-tint: "rgba(255,255,255,.02)"
  hover-wash: "rgba(63,193,201,.07)"
  loss-wash: "rgba(242,101,95,.08)"
typography:
  headline:
    fontFamily: "Source Sans 3, Segoe UI, system-ui, sans-serif"
    fontSize: "1.45rem"
    fontWeight: 700
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Source Sans 3, Segoe UI, system-ui, sans-serif"
    fontSize: "1.05rem"
    fontWeight: 600
  subhead:
    fontFamily: "Source Sans 3, Segoe UI, system-ui, sans-serif"
    fontSize: "0.85rem"
    fontWeight: 600
  body:
    fontFamily: "Source Sans 3, Segoe UI, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  label-mono:
    fontFamily: "JetBrains Mono, Consolas, ui-monospace, monospace"
    fontSize: "0.72rem"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.04em"
  label-caps:
    fontFamily: "Source Sans 3, Segoe UI, system-ui, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 600
    letterSpacing: "0.08em"
  figures:
    fontFamily: "JetBrains Mono, Consolas, ui-monospace, monospace"
    fontSize: "0.88rem"
    fontWeight: 500
    fontFeature: "tnum"
  stat:
    fontFamily: "JetBrains Mono, Consolas, ui-monospace, monospace"
    fontSize: "1.35rem"
    fontWeight: 600
    lineHeight: 1.1
    fontFeature: "tnum"
rounded:
  btn: "4px"
  panel: "6px"
  dialog: "8px"
  pill: "999px"
components:
  button-primary:
    backgroundColor: "{colors.signal-teal}"
    textColor: "{colors.teal-ink}"
    rounded: "{rounded.btn}"
    height: "30px"
    padding: "0 0.9rem"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.frost-text}"
    rounded: "{rounded.btn}"
    height: "30px"
    padding: "0 0.9rem"
  button-danger:
    backgroundColor: "transparent"
    textColor: "{colors.loss-red}"
    rounded: "{rounded.btn}"
    height: "30px"
    padding: "0 0.9rem"
  button-mini:
    backgroundColor: "transparent"
    textColor: "{colors.slate-dim}"
    rounded: "{rounded.btn}"
    height: "22px"
    padding: "0 0.55rem"
  nav-tab-active:
    backgroundColor: "{colors.signal-teal}"
    textColor: "{colors.teal-ink}"
    rounded: "{rounded.pill}"
    height: "26px"
    padding: "0 0.8rem"
  panel:
    backgroundColor: "{colors.void-panel}"
    rounded: "{rounded.panel}"
    padding: "0.9rem 1.2rem"
  input:
    backgroundColor: "{colors.void-black}"
    textColor: "{colors.frost-text}"
    rounded: "{rounded.btn}"
    padding: "0.3rem 0.5rem"
  table-header:
    backgroundColor: "{colors.void-header}"
    textColor: "{colors.slate-dim}"
    typography: "{typography.label-mono}"
    padding: "0.38rem 0.7rem"
  badge:
    backgroundColor: "transparent"
    textColor: "{colors.slate-dim}"
    rounded: "{rounded.btn}"
    padding: "0.05rem 0.5rem"
  pill:
    backgroundColor: "transparent"
    textColor: "{colors.slate-dim}"
    rounded: "{rounded.pill}"
    padding: "0.2rem 0.6rem"
---

# Design System: Magoo

## Overview

**Creative North Star: "The Ops Console"**

Magoo's interface is an industrial operations console: the control room of a
production line, not a website. Screens read like instrumentation — a dark,
cool blue-black ground, hairline-drawn panels, and dense tables of monospaced
figures that an industrialist scans the way a pilot scans gauges. The mood is
calm, precise, and technical. Nothing animates for delight, nothing is
decorated; restraint *is* the aesthetic. The one voice in the room is Signal
Teal, and it speaks only to say "this is live" or "this is the action."

The system is workmanlike by conviction. Controls are plain, hairline-bordered,
and obviously clickable; tables — the primary component of the entire
application — carry the real interface. Explanation lives close to the data
rather than in chrome: abbreviated figures reveal full precision on hover,
badges document the exact rule that produced them in their tooltips, and
methodology hides in collapsed disclosures until asked for. The pages are
server-rendered HTML with native elements (`<details>`, `<dialog>`) and a few
lines of vanilla JS; the design language assumes and honors that plainness.

**Key Characteristics:**
- Dark, dense, tabular — the data table is the first-class component
- One accent (Signal Teal) reserved for what is live, active, or actionable
- Every comparable numeral in JetBrains Mono, tabular-nums, right-aligned
- Flat tonal depth: hairlines and lighter panels, shadows only under floats
- Truth-on-hover: tooltips carry full precision and the rule behind every badge
- Native HTML elements and unicode glyphs (⟳ ▶ ▸) instead of icon libraries

## Colors

A cold, low-chroma void with one teal signal and three data-state hues; if
nothing is wrong and nothing is selected, the screen is gray-blue.

### Primary
- **Signal Teal** (#3fc1c9): the interface's single voice. Fills the active
  nav tab and the one primary button per page; colors links, focus outlines,
  the sorted column's chevron, the ▸ subhead marker, the brand mark, checkbox
  accents, the flash-message border, and the 3px left edge of the totals
  strip. On teal surfaces, text is always Teal Ink.
- **Teal Ink** (#04191b): near-black teal used exclusively as text on
  Signal Teal fills (active tab, primary button).

### Neutral
- **Void Black** (#0a1014): the page ground — and also the inside of inputs,
  which read as carved *into* panels rather than sitting on them.
- **Void Panel** (#10181e): one tonal step up; the surface of the nav bar,
  panels, stat strips, the save bar, the active subnav tab, and dialogs.
- **Void Header** (#121c23): a half-step above panel, used only for sticky
  table headers.
- **Hairline** (#1f2b33): the universal 1px stroke — panel borders, row
  separators, input borders, badge outlines, section rules.
- **Hairline Strong** (#2f3f4a): emphasis stroke; "off" status dots and the
  tooltip's border.
- **Frost Text** (#d2e0e6): primary text, a pale blue-white.
- **Slate Dim** (#7d94a0): the second text color — captions, table headers,
  help prose, empty states, idle nav links, ghost-button labels. Roughly half
  the words on any screen are Slate Dim. Also the interactive-affordance
  graphics — idle sort chevrons and the item-link dotted underline — so
  those cues clear the 3:1 non-text contrast floor (WCAG 1.4.11).

### Semantic
- **Profit Green** (#4ad295): positive values, "active"/"complete" badges,
  live status dots, and the left stripe on top-earner table rows.
- **Caution Amber** (#e6b450): stale data ages, warning badges and values,
  and the alert-warn panel edge. Amber means "look, but nothing is lost."
- **Loss Red** (#f2655f): negative values, bad badges, danger buttons, the
  neg-row stripe, and the alert-bad panel edge.
- **Zebra Tint** (rgba(255,255,255,.02)): even table rows.
- **Hover Wash** (rgba(63,193,201,.07)): row hover — a breath of teal.
- **Loss Wash** (rgba(242,101,95,.08)): the background of negative-margin
  table rows.

### Named Rules
**The Signal Rule.** Signal Teal marks what is live, active, focused, or
actionable — never decoration. The semantic hues speak only about data state
(profit, caution, loss), never about chrome. A screen with nothing wrong and
nothing selected shows no color beyond the void.

**The Token Purity Rule.** Every color on every page flows from the `:root`
token sheet in `base.html`. Templates contain zero hardcoded colors — inline
styles are spacing and width only. New hues require new tokens.

## Typography

**UI Font:** Source Sans 3 (with Segoe UI, system-ui fallback)
**Data Font:** JetBrains Mono (with Consolas, ui-monospace fallback)

**Character:** A quiet humanist sans for prose and labels, a crisp mono for
everything measured. The pairing does the console's work: words recede,
figures align. Loaded from Google Fonts at 400–700 (Sans) and 400–600 (Mono).

### Hierarchy
- **Headline** (700, 1.45rem, −0.01em): the page title (h1); one per page.
- **Title** (600, 1.05rem): section headings (h2), often carrying a dim
  `— N items, M slots, X ISK` qualifier inside the heading (job-table
  sections and their h3 groups render it via the `job_stats` macro; bought
  sections via `buy_stats`, which drops the slots).
- **Settings copy** (user ruling 2026-09-01): panel headers, side-nav
  links, setting labels and the Build Settings column heads are Title Case
  ("Raw Material Buffer", "Build Settings", "ME Rig"); the dim heading
  qualifiers and the `<small>` help captions stay sentences.
- **Subhead** (600, 0.85rem): category subheadings (h3.subhead), underlined
  with a hairline and prefixed with an accent ▸.
- **Body** (400, 14px/1.45): all prose. Explanatory paragraphs cap at 80ch.
- **Label Mono** (JetBrains Mono 500, 0.72rem/1.4, +0.04em): table headers —
  the console's characteristic dim mono microcaps. Also, at 11.5px, the nav
  status readouts and status pills.
- **Label Caps** (600, 0.7rem, +0.08em, uppercase): stat-strip captions;
  the only uppercase in the system.
- **Figures** (JetBrains Mono 500, 0.88rem, tabular-nums): every numeric
  table cell (`td.num`), right-aligned.
- **Stat** (JetBrains Mono 600, 1.35rem/1.1, tabular-nums): the big KPI
  values in totals strips, with an inline 0.75rem sans `.sub` suffix.

### Named Rules
**The Mono Figures Rule.** Every numeral that can be compared, summed, or
sorted is set in JetBrains Mono with tabular-nums and right-aligned in a
`.num` column. Prose numbers may stay in the UI font; data numbers never do.

**The Full-Figure Rule.** ISK renders abbreviated (`2.81B`) with the exact
figure in the `title` attribute — full precision is always one hover away,
never printed inline.

## Layout

A single centered column, max-width 1320px with 1.3rem side padding, under a
sticky 44px nav bar. Pages open with a `.pagehead` row — headline, then a dim
`·`-separated caption of live context facts, then a right-aligned action
cluster — optionally followed by a `.subnav` tab bar for views within a page.

Content is panels and tables. Side-by-side panels use a wrapping flex `.row`
(each panel `flex: 1 1 300px`); the settings page uses a two-column grid
(11rem sticky side-nav + main column). Every data table sits in a
`.tablewrap`. Density is high by design: 14px base type, 0.38rem × 0.7rem
cell padding, tables of 20+ rows expected and welcome.

There is no spacing scale. Rhythm comes from component defaults (panels
`.9rem 1.2rem`, cells `.38rem .7rem`) plus per-context rem literals in inline
styles — the incumbent, accepted practice for spacing-only tweaks.

Breakpoints: below 1100px, `.tablewrap` becomes an overflow-x scroll container
and table headers give up stickiness; below 900px the settings grid collapses
and the side-nav hides. Table headers otherwise stick at `top: 44px`, directly
under the nav.

### Named Rules
**The Sticky Context Rule.** At desktop widths a `.tablewrap` must never be an
overflow container — an `overflow-x: auto` ancestor becomes the sticky
context and pins headers onto the first data row (the v1.8 regression, fixed
2026-08-23). Overflow is a narrow-viewport concession only.

## Elevation & Depth

Flat plus tonal, by doctrine. Depth is a three-step tonal ladder — Void Black
ground, Void Panel surfaces, Void Header table heads — drawn with hairline
borders and finished with a 1px inset top highlight on panels
(`inset 0 1px 0 rgba(255,255,255,.03)`), a machined edge rather than a lift.
Drop shadows are reserved for chrome that genuinely floats over content.

A second depth channel is edge-coding: 3px left borders mark charged surfaces
(Signal Teal on the totals strip, Caution Amber / Loss Red on alert panels),
and 2px left stripes mark charged table rows (green for top earners, red for
negative margin). Structure is drawn, not cast.

### Shadow Vocabulary
- **Machined edge** (`box-shadow: inset 0 1px 0 rgba(255,255,255,.03)`): the
  standing finish on panels and totals strips; not an elevation.
- **Float** (`box-shadow: 0 -6px 18px rgba(0,0,0,.35)`): under the sticky
  save bar — the only true drop shadow in the system.
- **Backdrop** (`rgba(0,0,0,.6)`): the dialog scrim.
- **Tip** (`box-shadow: 0 4px 14px rgba(0,0,0,.45)`): under the shared
  tooltip — floating chrome, so it casts (the Float Rule).

### Named Rules
**The Float Rule.** Surfaces are flat and tonal at rest. A drop shadow appears
only under an element that floats over other content (save bar, dialog); no
hover-lift, no card shadows, no glow.

## Shapes

Tight radii, hairline strokes, and pill exceptions. Controls, badges, and
inputs round at 4px; panels at 6px; dialogs at 8px; and fully-round 999px
pills are reserved for the nav (tab links) and status chips. Only the btn
(4px) and panel (6px) radii are tokenized (`--r-btn`, `--r-panel`); the
dialog 8px and pill 999px are fixed literals by convention. Everything is
outlined in Hairline at 1px; borders are the system's drawing tool — color
borders on badges announce state, left-edge bars charge panels and rows, a
2px rail runs down the settings side-nav.

One deliberate inversion: inputs living inside table cells are borderless and
transparent at rest, showing only a hairline underline — the grid itself
reads as editable — then reacquire the 4px box on focus. Unicode glyphs
(⟳ ▶ ▸ ✓ —) do all icon work; there are no icon fonts or SVG icon sets.

## Components

### Buttons
- **Shape:** 4px radius, 30px height, `0 .9rem` padding; workmanlike and flat.
- **Ghost (default):** transparent with a hairline border; border brightens to
  Slate Dim on hover. Action labels take a glyph prefix: "⟳ Refresh prices".
- **Primary:** Signal Teal fill, Teal Ink text, 600 weight; hover is
  `filter: brightness(1.08)`. One per view — "▶ Plan index run".
- **Danger:** ghost with Loss Red text and a 45%-alpha red border that
  saturates on hover. Destructive submits add `onsubmit="return confirm(...)"`
  with the consequence restated in the confirm text.
- **Mini:** 22px / 0.75rem / Slate Dim, for in-table actions ("breakdown",
  "close", "remove").
- **Loading:** a submitting button blanks its label and overlays a spinning
  1em ring (Slate Dim track, Signal Teal head, .7s linear) until the next
  page loads; disabled under `prefers-reduced-motion`.
- **Focus (all controls):** `outline: 2px solid` Signal Teal, offset 1px.

### Inputs / Fields
- **Style:** Void Black fill (darker than the panel — carved in), hairline
  border, 4px radius, `.3rem .5rem` padding. Numeric inputs are 6.5rem wide,
  right-aligned, JetBrains Mono 0.85rem.
- **In-table:** transparent and borderless with a hairline underline; hover
  darkens the underline, focus restores box, radius, and Void Black fill.
  Editable cells auto-submit on change (`form.inline` + `onchange`).
- **Settings grid:** `.fields` label/control grid (two-pair `cols2` variant
  divided by a hairline); every label carries a `<small>` help clause in
  Slate Dim; percent inputs pair with a dim `%` unit suffix (`.withunit`).

### Tables
The system's heart. Headers are sticky Void Header bars of dim mono microcaps
with a 2px bottom rule; rows separate on hairlines, stripe with Zebra Tint,
and hover with the teal Hover Wash.
- **Column vocabulary:** `.num` right-aligned mono figures; `.cat` category /
  venue text; `.flags` / `.tagcol` a trailing badge column that absorbs slack.
- **Row states:** `.neg` (Loss Wash fill + 2px red left stripe) for negative
  margin; `.top` (2px green stripe) for the top-3 earners.
- **Variants:** `sortable` (each header's text lives in a transparent
  `button.thsort` — announced as actionable, native Enter/Space — with a
  permanent dim chevron, teal on the sorted column; ISK-aware sort parses
  `2.81B/741M/12K`; `data-sort` overrides for derived cells), `aligned`
  (+`dense`) for fixed-width stacked category tables sized by the longest
  item name, `profit` (content-packed columns, badge column absorbs slack),
  `pipelines` (the same packing for the editable pipeline grid: 4rem
  figure inputs, a 7rem `.isk` input, `mini` row actions in a
  slack-absorbing `.actions` column, and a multi-source row's two selects
  stacked so the Invention column stays one select wide),
  `classes` (the editable settings grid: selects and inputs living directly
  in cells).
- **Row states (beyond neg/top):** `superseded` (Slate Dim text — a retired
  planned run), `:target` (hover-wash fill — the chain row a Plan-tab
  "view in chain" link landed on).
- **Null cells:** an em dash (—).

### Badges
- **Style:** outline chips — 4px radius, hairline border, 0.72rem, lowercase
  text ("active", "no role", "shallow").
- **State:** neutral / `good` / `warn` / `bad` / `accent`, each tinting text
  and a 45%-alpha border of the same hue. `fill` escalates the worst cases to
  a solid fill (fill-bad: red with #1a0404 text; fill-warn: amber with
  #1a1204 text; both dark-on-hue to clear the 4.5:1 contrast floor). The
  complete sanctioned off-token ledger: those two fill-text literals, the
  Shadow Vocabulary alphas (.03 machined edge, .35 float, .45 tip, .6
  scrim), and the favicon's SVG data URI, which duplicates the Signal Teal
  hex because a data URI cannot read CSS custom properties — keep it in
  sync if the accent is ever retuned.
- **Grouping:** stacked in `span.tags` inside the trailing table column.

### Status Pills
- **Style:** 999px chips in 11.5px mono with a 7px status dot — Profit Green
  live, Caution Amber `stale`, Hairline Strong `off` — plus label and `<b>`
  value. The dashboard's data-freshness strip (`.statpills`).

### Panels / Containers
- **Panel:** Void Panel, hairline border, 6px radius, `.9rem 1.2rem`, machined
  top edge; opens with a zero-top-margin heading.
- **Alert variants:** `alert-bad` / `alert-warn` swap in a 3px colored left
  border; the h2 leads with a fill badge naming the condition ("unmet
  demand") followed by lowercase elaboration.
- **Flash:** 12%-alpha Signal Teal fill with a solid teal border, for
  post-action messages.

### Totals Strip (signature)
The `.totals` KPI roll-up: a flex row of label-over-value stats on a panel
with a 3px Signal Teal left edge. Uppercase Label Caps captions over 1.35rem
mono Stat values; values take `good`/`bad`/`warn` by sign or threshold, carry
full precision in `title`, and nest dim `.sub` suffixes ("/ 60",
"· 3 pipelines"). A stat slot can hold a warn badge instead of a figure.

### Navigation
- **Top nav:** 44px sticky Void Panel bar. Brand mark (teal square-in-square)
  + mono wordmark "M.A.G.O.O." at +0.18em with the deadpan subtitle beneath;
  center pill links (Slate Dim → Frost on hover, Signal Teal fill when
  active); right-aligned mono status readouts (ESI / PX / SDE / CORP ages,
  amber when stale).
- **Subnav:** folder-tab bar under the pagehead — 6px top radii, transparent
  until active (Void Panel fill + hairline sides), for views within a page.
- **Sidenav (settings):** a 2px hairline rail of anchor links; the active
  section's link colors Frost and lights its rail segment Signal Teal,
  tracked on scroll.

### Breakdown Dialog (signature)
Native `<dialog class="breakdown">`: Void Panel, 8px radius, 60% black
backdrop, max `min(64rem, 92vw)`. A `.dialog-head` row (zero-margin h2 +
`form method="dialog"` mini close button), a dim `·`-separated subtotal line,
then a sortable cost table. Opened per-row via a mini "breakdown" button and
`showModal()`.

### Disclosures
`<details>` does triple duty: `details.muted`/`details.help` tuck methodology
prose ("How this is computed") under a dim summary; a details block gates
destructive bulk actions behind one extra click; `details#multibuy` holds
read-only `textarea.multibuy` copy-paste blocks (10rem, mono) that
select-all on click.

### Confirmation Dialog
`dialog#confirm` (2026-09-01): every destructive or irreversible submit —
Mark executed, Reopen run, Discard run, Delete pipeline, Clear all
pipelines, Remove character — declares `data-confirm="…question…"` on its
form and is guarded by one shared native `<dialog>` in base.html, styled
like the breakdown dialogs. The confirm button borrows the submitting
button's label and tone (danger stays danger, primary stays primary);
Cancel takes focus first, Escape cancels, focus returns to the button on
cancel. Never `window.confirm()`: embedded browsers (the Claude Code
Browser pane, any WebView with dialogs suppressed) answer it false
instantly and silently cancel the submit — the failure that once killed
the pipeline delete.

### Collapsible Sections
`details.section` (2026-09-01): every h2 section and h3 category group on
the data tabs — Planning, Index Runs (Plan / Chain), Invention — is a
native `<details>` that is **open by default**; the heading itself is the
`<summary>` (native marker hidden), and its glyph shows the state: the
subhead's accent ▸ turns ▾ when open, the h2 gains a dim one. A section the
user closes stays closed for that page family (`data-key` + a
run-id-normalised path + the `?view=` tab in localStorage), so closing
"Reaction jobs" on run 62 keeps it closed on run 63 — but a run's Plan and
Chain views, which reuse keys, remember separately. Opening re-measures
`table.aligned`, which `layoutAligned()` skips while hidden. Pagehead,
totals strip, alert panels, checklist panels, dialogs, and the methodology
disclosures are never sections; the Profit views have no headed sections,
and the Dashboard, Pipelines, Settings and ESI tabs are deliberately not
collapsible.

### Save Bar
Sticky-bottom Void Panel bar (the one floating shadow) pairing a dim note —
which forms save together and which don't — with the page's primary button,
bound to its form via the `form=` attribute.

### Tooltip System (signature)
The Tooltip Ledger's delivery mechanism: one shared `.tip` element (Void
Header fill, Hairline Strong border, the Tip shadow). At load —
`convertTips()` — badges, stat values, pills and nav readouts hand their
`title` text to it, join the tab order, and gain composite aria-labels, so
the ledger shows on focus as well as hover; a 300ms grace period lets the
pointer travel onto the tip and rest there (WCAG 1.4.13), and Escape
dismisses. Content cloned into dialogs is re-converted on open.

### Item Link
`button.itemlink` — an item name that opens the shared deficit dialog on
the run Plan tab. Text stays Frost with a dotted Slate Dim underline as
the quiet affordance; teal arrives only on hover (the Signal Rule).

### Deficit Dialog
One shared `dialog.breakdown` on the run Plan tab, filled from the clicked
row's data attributes: the engine's three deficit rules rendered as an
equation that actually sums (intermediates: target + planned draw − stock
− jobs; raws: consumption + margin − stock; finals: always the full
cycle), the plan's answer line, and a "view in chain →" link to the
`:target`-highlighted chain row.

### Profit Breakdown Templates
The profit pages render each pipeline's cost breakdown into an inert
`<template>` and clone it into one shared `dialog.breakdown` on open —
detail on demand never weighs the live DOM (the run Plan tab's shared
deficit dialog proved the pattern). Cloned content is re-run through
`initSortable()` and `convertTips()`.

### Setup Checklist
`ol.setup` on the dashboard: the first-run sequence with live-derived step
states. Done steps carry a green marker plus a text `done` badge (state is
never color-only); the next actionable step takes the accent marker, full
Frost text, and `aria-current="step"`. Dismissable (localStorage), forced
back by Settings → First-run setup (`?setup=1`). Step 1 hosts the
checklist's only embedded control — the game-data download (below).

### Game-Data Progress
`.sde-fetch` in checklist step 1: the app's one background job. A ghost
button starts the download; beside it a 160×6px track (Hairline fill,
pill radius) carries a Signal Teal fill that mirrors the mono readout —
text first, the bar never means anything the readout doesn't say. The
fill jumps on each 1s poll of `/sde/status` (the UI's only JSON endpoint,
kept DB-free so the import transaction's write lock can't stall it); no
transition, per the motion rule. Done turns the fill Profit Green,
failure Loss Red with the failure text in the readout. Milestones —
started, imported, failed, stopped — go through the shared live region;
per-tick progress does not. "Imported" is announced *after* the page's
self-reload (a sessionStorage handoff) because navigation silences a
polite live region; an idle status met mid-poll means the app restarted
and lost the import — the readout says so rather than claiming currency.

### Ghost Link Button
`a.btn` — an anchor wearing the ghost button's clothes for navigations
that read as actions (SSO login, First-run setup); `a.btn.primary` mirrors
the primary fill. A button may not nest inside an anchor, hence the class.

### Inline Save
In-table edits POST via fetch (`inlineSave()`): the page keeps its scroll
and sort, the input's underline flashes Profit Green at 2px (thickness, not
color alone), and a visually-hidden live region announces "saved". Failure
falls back to a full form submit.

### Code Chip
`<code>` — the mono command chip (Void Black fill, hairline, 4px radius)
for commands and paths quoted in prose, e.g. the callback URL
`http://localhost:8765/sso/callback`. As of v1.21 the setup checklist
quotes no CLI commands at all — a packaged user has no terminal.

### Named Rules
**The Tooltip Ledger Rule.** Every badge carries the exact rule that produced
it in its `title`; every abbreviated or derived figure carries its full value
the same way. The UI never asserts a judgment it can't explain on hover.

**The One Primary Rule.** At most one Signal Teal filled button per view.
Everything else is ghost, danger-ghost, or mini.

## Do's and Don'ts

### Do:
- **Do** set every numeric column with `.num`: right-aligned, JetBrains Mono,
  tabular-nums, with full `|isk` precision in `title` over `|isk_short` text.
- **Do** give every badge a `title` tooltip stating the rule behind it, and
  group row badges in `span.tags` in the trailing column.
- **Do** wrap every data table in `.tablewrap` (except inside dialogs).
- **Do** use the em dash (—) for null cells and `·` separators for caption
  fact runs.
- **Do** prefix action buttons with plain unicode glyphs (⟳ ▶ ＋) — no icon
  fonts, no SVG icon sets.
- **Do** put methodology prose in collapsed `details.muted` disclosures and
  page intros in Slate Dim paragraphs with `<b>` on the load-bearing values.
- **Do** give empty states a dim one-liner that links to the page that
  creates the missing thing ("No pipelines yet — add one.").
- **Do** build with native elements — `<details>`, `<dialog>`, forms — and
  keep JS to small vanilla helpers.

### Don't:
- **Don't** introduce color or font via inline styles — inline styles are
  spacing/width-only, everywhere, without exception.
- **Don't** add a color outside the token sheet; a new hue means a new
  `:root` token first.
- **Don't** cast shadows at rest or on hover — shadows belong to floating
  chrome only (the Float Rule).
- **Don't** spend Signal Teal on decoration; it marks live, active, or
  actionable — nothing else (the Signal Rule).
- **Don't** add gradients, glows, background imagery, or icon libraries —
  none exist in this system.
- **Don't** place a second primary button on a view.
- **Don't** make `.tablewrap` an overflow container at desktop widths — it
  breaks sticky headers (the Sticky Context Rule).
