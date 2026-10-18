# Design system

The app is for training managers and trainers at a driver training department. They are not data scientists. Every page should answer three questions in under 10 seconds: which courses will miss their yearly target, by how much and why, and what to do about it.

The look comes from road signage and fleet operations: calm, readable, and colour only where it means something.

## Tokens

Defined in `frontend/src/styles/tokens.css` (Tailwind v4 reads `@theme`, so there is no `tailwind.config`; each variable becomes a utility such as `bg-canvas`, `text-ink`, `rounded-panel`).

| Token | Value | Use |
|---|---|---|
| `ink` | #17212B | main text |
| `ink-muted` | #5B6875 | secondary text |
| `canvas` | #F4F6F8 | page background |
| `surface` | #FFFFFF | panels |
| `line` | #DDE2E8 | borders |
| `primary` | #0F5E6B | active nav, primary buttons, links, progress fill |
| `signal` | #F2B705 | **only** the simulated clock and the "today" marker on charts and the calendar |

Risk colours are the only reds, ambers and greens in the app. Each has a text colour and a soft background:

| Level | Text | Background |
|---|---|---|
| `risk-high` | #B42318 | #FDECEA |
| `risk-medium` | #B54708 | #FEF3E2 |
| `risk-ok` | #157F3D | #E8F5EC |
| `risk-unknown` | #5B6875 | #EEF1F4 |

Type: headings and big numbers use **Barlow Semi Condensed** (600 / 700), close to highway signage lettering. Body, tables and forms use **Public Sans** (400 / 500 / 600). Scale: 13 / 14 / 16 / 20 / 24 / 32 px (`text-xs` to `text-3xl`). Tables use 14px, prose 16px. All numbers use tabular figures (set once on `body`).

Shape and depth: 6px radius on inputs and buttons (`rounded-control`), 10px on panels (`rounded-panel`), pills fully rounded. No shadows on panels; they use the `line` border. Only dialogs and drawers get a shadow. No gradients, no decorative icons (icons only for risk, actions and navigation).

Motion happens only in response to a user action (drawer open, toast) and is switched off under `prefers-reduced-motion`.

## Components (`src/components/ui/`)

`Button` (primary / secondary / danger / ghost), `PageHeader`, `Panel`, `StatCard`, `Skeleton`, `RiskPill` and `Tag`, `ProgressBar` (optional target-pace marker, `role="progressbar"` with a text label), `InfoTip` and `Hint` (tooltips), `EmptyState`, `ErrorState` (with Retry), `DataTable` (sortable headers, sticky header, keyboard-clickable rows, optional expanded row, optional mobile cards), `Drawer`, `ConfirmDialog`, `TextField` / `SelectField` / `TextAreaField` (label, required marker, inline error). Dialogs, drawers and tooltips use Radix primitives; toasts use sonner.

Every page handles four states: loading (skeletons, not spinners), empty (say what will appear and which phase adds it), error (say what failed, offer Retry) and data.

## Copy rules

- Sentence case everywhere. No ALL CAPS labels.
- Say what it means, not what it is called internally. All wording lives in `src/lib/format.ts` and `src/lib/glossary.ts`, with unit tests in `format.test.ts`.
- Colour is never the only signal: risk is an icon plus words.

| Internal | Shown as |
|---|---|
| risk `high` / `medium` / `low` / `achieved` / `insufficient_data` | Will likely miss / At risk / On track / Target met / Not enough data |
| `pace_gap` | "Vs schedule": "5 ahead", "2 behind", "On pace" |
| projected + range | "Expected by 31 Dec": "195 of 240", "likely 167 to 223" |
| `p_hit` | "Chance of hitting target": Very unlikely (<10%), Unlikely (10-39%), Toss-up (40-60%), Likely (61-89%), Very likely (90%+), with the percentage |
| shortfall type | Problem: "Not enough seats", "Low turnout", "Not enough eligible drivers" |
| target minus projected | Shortfall: "Short by ~45 completions" |

## Shell

Left sidebar in four groups (Overview, Records, Oversight, Settings); collapsible to icons, a drawer on mobile. The top bar holds a breadcrumb, the **simulated clock** (yellow, large date, "Next day" and "Next week"; disabled with a tooltip while the planner runs) and a small API status dot. Each page has its own `h1` in the page header, so the top bar shows the breadcrumb only.

## Self-check against "generic SaaS template"

- Cards everywhere: the dashboard has four stat cards, but each shows a different kind of fact, and the main thing on the page is the course list, not a grid of cards.
- Eyebrow labels over every heading: none. Labels are sentence case and sit inside the card as a plain line.
- Decorative icons and gradients: none. The only bold colour in the shell is the yellow clock, because the simulated date drives everything in the app.

## Pages and what they need from the backend

| Page | Status |
|---|---|
| Dashboard, Course detail, Calendar, Records (drivers, trainers, courses, sessions), Simulation | Built on the current API |
| Alerts | Lists alerts and the Phase 5 plans for each. Approve and Reject are disabled ("Available once ranking is built"): there is no ranking, score, alert text or approve endpoint yet |
| Audit log, Fairness | Empty states that say what will appear. No endpoint serves audit rows or fairness numbers yet |

Older pages still use some Tailwind `slate-*` / `red-*` classes. `tokens.css` re-points those palettes at the design tokens, so they look right; new code should use the token names directly.
