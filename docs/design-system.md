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
| `--text` | `#111318` | Headings, values, primary text |
| `--text2` | `#3d4049` | Body text |
| `--muted` | `#8a8e98` | Labels, secondary text, inactive tabs |
| `--faint` | `#a9acb4` | Placeholders, axis ticks |
| `--accent` | `#0a46e0` | Brand blue: links, active states, positive amounts, main chart series |
| `--accent-2` | `#2f6bf0` | Hover on blue, gradient end |
| `--accent-bg` / `--accent-soft` | `#eef2ff` / `#d3e1fb` | Selected rows, info tint / progress track, chart fill |
| `--violet` / `--violet-bg` | `#a56bf0` / `#f4ecfe` | Second series, Claude and draft accents |
| `--ink` | `#17181c` | Primary (dark) button |
| `--ok` / `--ok-bg` | `#1c9a4c` / `#e3f7e9` | Live, success, up |
| `--warn` / `--warn-bg` | `#b45309` / `#fff4e0` | Warnings |
| `--bad` / `--bad-bg` | `#e5484d` / `#fdecec` | Problems, errors, down, notification dot |
| `--draft` / `--draft-bg` | `#8b4fe0` / `#f4ecfe` | In draft |
| `--c1` ... `--c6` | blue, violet, green, orange, light blue, red | Chart series, in that order |

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
| `--ring` | Focus ring (keyboard focus and focused inputs) |
| `--ease`, `--t` | 160 ms ease-out for hover, focus and open |

Spacing steps: 4, 8, 12, 16, 20, 24, 36. Cards pad 18-24, gaps between cards 12.

## Components

**Top bar.** White, no bottom border, a 1px `--line2` hairline under it. Brand left (Marketing **Studio**, 18/600). Area tabs are
pill tabs: 36 high, radius 9, padding 9/14, 13/600; inactive `--muted` on transparent, active `--text` on white with `--sh-1`.
Actions on the right are secondary buttons.

**Buttons.** Height 32-34, radius `--r`, padding 0 12-14, 12.5-13/600, optional 14px icon with gap 8.
- Secondary (default): white, `--sh-1`, no border.
- Primary: `--ink` with white text. The one action that commits something on a screen.
- Blue (`.blue`): brand blue, for Push live and the main call to action.
- Ghost: no fill or shadow until hover. Link: blue text only.
- Icon button: 32x32, radius `--r`, white, `--sh-1`.
- Danger: red text on the secondary button.

**Inputs and selects.** Height 34, radius `--r`, `--line` border, white, placeholder `--faint`, focus = blue border + `--ring`.
Selects carry a small chevron. Search inputs have a 14px search icon and may show a `⌘K` key hint (`--sunken`, radius 4).

**Dropdowns and menus.** White, radius `--r-lg`, `--sh-pop`, padding 6. Items 32 high, radius `--r-sm`, hover `--hover`,
selected item `--accent-bg` with `--accent` text. Group labels 11/600 `--muted`.

**Chips and badges.** Radius 5-6, padding 3/8, 11/600. Status tints: ok, warn, bad, draft, info (`--accent-bg`). Trend chip:
`+4.8%` in `--ok` on `--ok-bg` with a small arrow. Counter badge on a button: pill, `--accent` on white or white on `--accent`.

**Cards.** White, radius `--r-lg`, `--sh-card`, padding 18-24. Header row: title (16-18/600) left, small icon buttons
(`+`, `···`) or a secondary button right. KPI card: label 13/500 `--text2`, big number, trend chip.

**Tables and grids.** Header `--head`, 11.5/600 `--muted`, sticky. Rows 38-40 high, `--line2` dividers, hover `--hover`,
selected `--sel`. Numbers right-aligned, tabular. In-draft cells keep the draft tint; problems and warnings keep their tints.

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

**Empty and loading states.** Muted text centred in the card; skeleton bars in `--sunken` with a soft shimmer.

## Rules

- No new hex values in area files: add a token here first.
- One primary (`--ink`) button per view; Push live is blue.
- Shadows, not borders, separate surfaces. Borders are for inputs and table rows.
- Keep the information density: Studio is a work tool. Rows stay compact; cards get air.
- British English in every label.
