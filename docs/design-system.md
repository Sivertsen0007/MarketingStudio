# Marketing Studio design system

Light, calm and quick to read. The look is taken from the *Daymark* dashboard design: white cards on a pale grey canvas,
soft shadows instead of hard borders, one strong blue, Manrope type.

The tokens live once, in `engine/index.html` (`:root`). Area files in `engine/static/*.js` never hard-code a colour,
radius or shadow: they use the tokens below.

## Foundations

### Colour

| Token | Value | Use |
|---|---|---|
| `--bg` | `#f4f5f8` | App canvas behind cards and panels |
| `--panel` | `#fff` | Cards, panels, top bar, inputs, buttons |
| `--head` | `#f8f9fb` | Table headers, quiet bars |
| `--sunken` | `#f1f2f5` | Keyboard hints, tracks, empty wells |
| `--hover` | `#f4f5f8` | Hover on rows, menu items, ghost buttons |
| `--line` / `--line2` | `#e6e8ee` / `#eff0f4` | Dividers and input borders / row lines |
| `--line-strong` / `--line-stronger` | `#d6d9e1` / `#b9bdc8` | Input border on hover, scrollbar thumb / thumb on hover |
| `--text` | `#111318` | Headings, values, primary text |
| `--text2` | `#3d4049` | Body text |
| `--muted` | `#5f6370` | Labels, secondary text, inactive tabs, filter counts (5.99:1 on white, 5.35:1 on `--sunken`) |
| `--faint` | `#767b87` | Placeholders and chart axis ticks only, never body text (4.24:1 on white) |
| `--accent` | `#0a46e0` | Brand blue: links, active states, positive amounts, main chart series |
| `--accent-2` | `#2f6bf0` | Hover on blue, gradient end |
| `--accent-bg` / `--accent-soft` | `#eef2ff` / `#d3e1fb` | Selected rows, info tint / progress track, chart fill |
| `--violet` / `--violet-bg` | `#a56bf0` / `#f4ecfe` | Second series, Claude and draft accents |
| `--ink` / `--ink-2` | `#17181c` / `#2a2c33` | Primary (dark) button, toast / primary button on hover |
| `--on-accent` | `#fff` | Text and icons on a blue or coloured fill |
| `--ok` / `--ok-bg` | `#1c9a4c` / `#e3f7e9` | Live, success, up |
| `--warn` / `--warn-bg` | `#b45309` / `#fff4e0` | Warnings |
| `--warn-dot` | `#f5a524` | Amber status dot (connections, data warning, legend) |
| `--idle` | `#a9acb4` | Paused / unknown status dot |
| `--bad` / `--bad-bg` | `#e5484d` / `#fdecec` | Problems, errors, down, notification dot, bars and dots (fills, not text) |
| `--bad-text` | `#c8303a` | Red text: danger buttons (`button.danger`, `button.link.danger`, dialog danger buttons), `.chip.bad`, the alerts counter (5.33:1 on white) |
| `--draft` / `--draft-bg` | `#8b4fe0` / `#f4ecfe` | In draft |
| `--c1` ... `--c6` | blue, violet, green, orange, light blue, red | Chart series, in that order |
| `--scrim` | `rgba(17,19,24,.35)` | Dialog backdrop |
| `--glass` | `rgba(255,255,255,.22)` | Counter badge on a filled (blue) button |

### Type

Manrope (Google Fonts, 400/500/600/700), `--font`. Numbers use `font-variant-numeric: tabular-nums` in tables and charts.

| Role | Size / weight | Colour |
|---|---|---|
| Page title | 26 / 600 | `--text` |
| Page subtitle | 14 / 500 | `--muted` |
| Card title | 16-18 / 600 | `--text` |
| Big number | 28-40 / 600, tabular | `--text` |
| Body | 13.5 / 500 | `--text2` |
| Label, tab, button | 12.5-13 / 600 | `--text` or `--muted` |
| Small, meta, axis | 10.5-11.5 / 500 | `--muted` |
| Breadcrumb | 12 / 500 | `--muted` |

### Shape, depth and motion

| Token | Value |
|---|---|
| `--r-xs` `--r-sm` `--r` `--r-md` `--r-lg` `--r-xl` `--r-pill` | 4, 6, 8, 10, 12, 16, 999 px |
| `--sh-1` | Buttons, inputs, tabs, small tiles |
| `--sh-card` | Cards and panels (soft shadow plus a 1px hairline) |
| `--sh-pop` | Menus, dropdowns, tooltips, popovers |
| `--sh-dlg` | Dialogs |
| `--ring` | Focus ring on focused inputs. Keyboard focus everywhere is a 2px `--accent` outline, offset 2px (`:focus-visible`, `!important` so no component can cancel it) |
| `--ease`, `--t` | 160 ms ease-out for hover, focus and open |

Spacing steps: 4, 8, 12, 16, 20, 24, 36. Cards pad 18-24, gaps between cards 12.

## Components

**Top bar.** White, no bottom border, a 1px `--line2` hairline under it. Brand left: a 28px blue gradient mark and
Marketing **Studio** (18). Then the pill tabs: Overview first, then the areas. 36 high, radius 9, padding 0/14, 13/600; inactive
`--muted` on transparent, active `--text` on white with `--sh-1`. The Alerts tab carries a small red counter for unacknowledged
alerts (no separate button). Actions on the right are secondary buttons 34 high (`.tb`): Refresh data (one menu for every
source and account), Push live, Connections, Setup, Settings. Push live turns blue with a counter when the draft has changes.
Under 1380px the brand name and the Setup/Settings icons hide; under 1240px the Refresh data and Push live icons hide too.

**Data bar.** Sits on the canvas, not in a card: account picker (select without a border, `--sh-1`), freshness chips as white
chips with `--sh-1` (status tints when they warn), and the legend right: white chips with a small coloured square.

**Work panes (Ads).** Tree, grid and Edit/Draft are three white cards on the canvas with 12px gaps. The tree uses rounded
rows (radius 6) with the selected row in `--sel` and blue text. MANAGE is a list of tabs: the open one is a white tab with
`--sh-1` and a blue count pill. The grid toolbar has a search with an icon, filter pills as a segmented control (`--sunken`
track, white active pill) and 34px selects and buttons. The Edit/Draft switch is the same segmented pill style.

**Buttons.** Height 32-34, radius `--r`, padding 0 12-14, 12.5-13/600, optional 14px icon with gap 8.
- Secondary (default): white, `--sh-1` plus a 1px `--line-strong` edge so it reads on white cards.
- Primary: `--ink` with white text. The one action that commits something on a screen.
- Blue (`.blue`): brand blue, for Push live and the main call to action.
- Ghost: no fill or shadow until hover. Link: blue text only.
- Icon button: 32x32, radius `--r`, white, `--sh-1`.
- Danger: `--bad-text` on the secondary button. A quiet danger next to a main action is a ghost danger (`.ghost.danger`),
  placed left of the main action, e.g. the Ads draft: Clear draft (ghost danger, left) and Check draft (blue, main).
  In a bulk bar the destructive action sits after a small gap (`.bsep`).

**Inputs and selects.** Height 34, radius `--r`, `--line` border, white, placeholder `--faint`, focus = blue border + `--ring`.
Selects carry a small chevron. Search inputs have a 14px search icon and may show a `⌘K` key hint (`--sunken`, radius 4).

**Dropdowns and menus.** White, radius `--r-lg`, `--sh-pop`, padding 6. Items 32 high, radius `--r-sm`, hover `--hover`,
selected item `--accent-bg` with `--accent` text. Group labels 11/600 `--muted`.

**Chips and badges.** Radius 5-6, padding 3/8, 11/600. Status tints: ok, warn, bad, draft, info (`--accent-bg`), as
`.chip.ok` and so on, text in the status colour. Trend chip `.chip.trend` (`.down` for red): `+4.8%` in `--ok` on `--ok-bg` with a
small arrow. Counter badge on a button: pill, `--accent` on `--accent-bg`, or white on `--glass` inside a blue button.
Status pill (`.pill`): pill radius, tinted by status (Enabled/Active green, Paused grey, Removed red), 7px dot.

**Cards.** White, radius `--r-lg`, `--sh-card`, padding 18-24. Header row: title (16-18/600) left, small icon buttons
(`+`, `···`), a secondary button or a neutral chip right. KPI card: label 13/500 `--text2`, big number, trend chip.
A `.tile` on its own is a small KPI card; inside an `.ov-card` the tiles sit side by side, split by `--line2` hairlines.
Key figures (`.tile.hi`) show the number in `--accent`.

**Overview.** Breadcrumb (12/500 `--muted`), greeting as page title, subtitle, tools right (freshness, period select, dark
Refresh). Then one card per channel with five KPIs, a bar chart and a share list built from the destination rows, the area
cards (Email, SEO, SoMe), and the two tables as cards with the table running edge to edge under the card header.

**Tables and grids.** Header `--head`, 11.5/600 `--muted`, sticky. Rows 38-40 high, `--line2` dividers, hover `--hover`,
selected `--sel`. Numbers right-aligned, tabular. In-draft cells keep the draft tint; problems and warnings keep their tints.
In the Ads grid the tick box, Status and the name column stay put when the grid scrolls sideways (sticky, the name capped
at 260px with the full name in the tooltip). Numbers and dates are en-GB (`1,234.5`, `£2,773.16`, `18 Jun 2024`).
Filter pills and small icon buttons are at least 32px high. Below 1360px the Edit/Draft panel starts closed (opening a row opens it).

**Lists.** 40px icon tile (radius `--r`, `--sh-1`), title 14/500, meta 11/500 `--muted`, value right 14-16/600. Positive values
in `--accent`.

**Progress.** Track 10-14 high, radius pill, `--accent-soft`. Fill gradient `--accent-soft` to `--accent-2`; second colour
violet. Optional white knob with a 3px coloured ring at the end.

**Charts.** No chart frame. Gridlines `--line2`, dashed; axis labels 10.5 `--faint`. Series in `--c1` ... `--c6`. Bars radius 3
on top, gradient from the series colour to 35% of it. Lines 2px with a soft area fill (series colour at 12% to 0). Tooltip
= white card, radius 6, `--sh-pop`, date 10.5/500 and values 14/600.

**Dialogs.** White, radius `--r-xl`, `--sh-dlg`, padding 24, title 18/600, footer actions right (secondary then primary).
Backdrop `rgba(17,19,24,.35)` with a light blur.

**Toasts.** `--ink` background, white text, radius `--r`, `--sh-pop`, bottom centre.

**Tooltips.** `--ink` small tooltip for icons; white card tooltip for charts.

**Empty and loading states.** Muted text centred in the card; skeleton bars (`.skel`) in `--sunken` with a soft shimmer.

**Notes.** `.prob` (red on `--bad-bg`) and `.warnl` (amber on `--warn-bg`), radius 6, 12/500. Draft items are small cards
(`--sh-card`, radius 10) with who/time, the summary in 600 and any notes under it.

**Update banner.** White card bottom right, `--sh-pop`, a blue dot and a blue Update now button.

A live showcase of every component is `docs/design-system.html` (its CSS is a copy of the first `<style>` block in
`engine/index.html`; refresh the copy when the tokens change).

## Rules

- No new hex values in area files: add a token here first.
- Light only: no `prefers-color-scheme: dark` rules.
- One primary (`--ink`) button per view; Push live is blue.
- Shadows, not borders, separate surfaces. Borders are for inputs and table rows.
- Keep the information density: Studio is a work tool. Rows stay compact; cards get air.
- British English in every label.
