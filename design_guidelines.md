# RISEDUAL Mission Control — Design System (canonical)

_Last updated 2026-02-16. Owner: Mission Control front-end._

This document is the **single source of truth** for the RISEDUAL operator
console aesthetic. It exists because the console has grown past the point
where every component author can rediscover the conventions on their own.
Anything not specified here is open to the component author's judgment;
anything that *is* specified should not be re-invented without updating
this doc first.

---

## 1. Voice & posture

The RISEDUAL console is an **operator console for a high-trust trading
governance system**, not a marketing surface. It is read fastest by people
who already know what the words mean. We design for the second-pass
reader, not the first-pass visitor.

- **Density over breathing room.** A trader who has run the console for a
  month wants every gate, every seat, every receipt visible at once.
  Don't ration screen space — ration the *cognitive load* by using the
  type & color hierarchy below.
- **Monospace for facts. Display for headlines.** Anything that is a
  fact about the state of the system (numbers, IDs, symbols, gate names,
  brain names) is monospace. Headlines, hero copy, and titles are the
  display face. Long-form prose stays sans.
- **All-caps eyebrows. Sentence-case bodies.** Use the `label-eyebrow`
  utility for section labels — they're tracked-wide, dim, uppercase. The
  long-form text below them stays sentence case.
- **Authoritative, not exuberant.** No exclamation points. No emoji as
  icons. Use the icon libraries (Phosphor, FontAwesome) for any glyph
  that needs to render outside text flow.

---

## 2. Color palette (Tailwind `rd-*`)

Lives in `frontend/tailwind.config.js` and `frontend/src/App.css`. Update
**both** when you change a value — `App.css` is the CSS variable surface
that's also consumed by some non-Tailwind components.

| Token              | Hex      | Role                                                  |
| ------------------ | -------- | ----------------------------------------------------- |
| `rd-bg`            | `#0A0A0A`| Page background — the deepest layer                   |
| `rd-bg2`          | `#111111`| Card / panel header strip                             |
| `rd-bg3`          | `#1A1A1A`| Inline input fields, selected pickers                 |
| `rd-border`        | `#27272A`| Hairline dividers — the default border weight         |
| `rd-borderStrong`  | `#52525B`| Emphasized borders (e.g. active selection)            |
| `rd-text`          | `#FFFFFF`| Primary readable text                                 |
| `rd-muted`         | `#A1A1AA`| Secondary, still-readable text                        |
| `rd-dim`           | `#71717A`| Tertiary text. Labels, captions, metadata             |
| `rd-warn`          | `#FBBF24`| Soft warning (drift, partial state)                   |
| `rd-danger`        | `#EF4444`| Hard alarms (BLOCK, veto, offline)                    |
| `rd-success`       | `#22C55E`| Confirmations (PASS, executed, ALLOW)                 |

### Brand / runtime colors

Used to identify a specific brain across every surface. **Never** repurpose
these for non-brain UI — that's how an operator misreads a card.

| Token         | Hex      | Brain                  |
| ------------- | -------- | ---------------------- |
| `rd-alpha`    | `#3B82F6`| ALPHA (executor)       |
| `rd-camaro`   | `#F59E0B`| CAMARO (decider)       |
| `rd-chevelle` | `#10B981`| CHEVELLE (governor)    |
| `rd-redeye`   | `#DC2626`| REDEYE (opponent)      |

### Lane colors (2026-02-15)

The two execution lanes have their own anchor colors. Reuse them on lane
headers and lane-scoped controls; don't re-mix on per-component palettes.

| Lane     | Hex      | Usage                                       |
| -------- | -------- | ------------------------------------------- |
| Equity   | `#3B82F6`| EQUITY LANE badges (alias of `rd-alpha`)    |
| Crypto   | `#A855F7`| CRYPTO LANE badges, crypto-only chips       |

---

## 3. Typography

- **Display face**: `Chivo` (system-ui fallback). Use on the public-facing
  marketing surfaces and on hero copy. Not for body text.
- **Monospace face**: `JetBrains Mono` (with system mono fallback). Use
  for: numbers, IDs, brain names, gate names, code blocks, table cells.

### Size hierarchy

| Use case                                  | Class chain                              |
| ----------------------------------------- | ---------------------------------------- |
| Page H1                                   | `text-4xl sm:text-5xl lg:text-6xl`       |
| Card title (operator console)             | `text-sm uppercase tracking-widest`      |
| Body text                                 | `text-sm` (mobile) → `text-base` (md+)   |
| Eyebrow / metadata caption                | `text-[10px] uppercase tracking-widest`  |
| Inline data (number, ID, symbol)          | `font-mono text-[11px]` or `text-xs`     |

The eyebrow size of `text-[10px]` is **specifically** for label rows above
data blocks (`label-eyebrow` utility wraps this + `text-rd-dim`). Don't
shrink past that — readability collapses.

---

## 4. Spacing & layout

- **Density**: cards use `px-4 py-3` for headers, `px-4 py-3` for cells.
- **Section gap**: `mb-6` between independent panels.
- **Grid dividers**: prefer `divide-x divide-rd-border` over manual
  borders on each cell.
- **Responsive collapses**: mobile-first. `grid-cols-1 md:grid-cols-3
  lg:grid-cols-5` is the typical operator-table pattern. Always provide
  a stacked layout for sub-768 widths.
- **Asymmetric layouts**: prefer left-aligned headers + right-aligned
  metadata over center-aligned everything. Reading flow is L-to-R.

---

## 5. Components (Shadcn UI)

All primitive UI lives in `frontend/src/components/ui/`. Import path is
`@/components/ui/<component>` (e.g. `@/components/ui/button`). Most
console components instead use the lightweight `ui-bits` wrappers
(`Card`, `Badge`) which provide the RISEDUAL styling out of the box.

- **Cards**: dark background, `rd-border` hairline border, no rounding by
  default (`rounded-none`) — RISEDUAL has a sharp-edged aesthetic, not a
  rounded one.
- **Badges**: small, all-caps, with a colored leading dot. Use the
  `<Badge color="#RRGGBB">LABEL</Badge>` API.
- **Buttons**: prefer ghost buttons (text + icon) for in-card actions.
  Use a filled button only for the primary call-to-action on a page.
- **Inputs**: `bg-rd-bg3 border border-rd-border text-rd-text px-2 py-1
  font-mono text-xs`. Inputs are visibly inputs — no borderless inputs.

---

## 6. State signaling

Every interactive or auto-updating element should make its state visible.

- **Loading**: pulse on the data row, not a spinner overlay.
- **Stale data**: show "Xm ago" stamp in `rd-dim`. After 5m, fade to
  `rd-warn`. After 30m, mark `rd-danger`.
- **Three-tier heartbeat doctrine** (see `namespaces.py`):
  - `<60s` → 🟢 `rd-success` ("healthy")
  - `60–110s` → 🟡 `rd-warn` ("drift")
  - `>110s` → 🔴 `rd-danger` ("preview drift" — likely URL misconfig)
- **Doctrine annotations**: every panel that touches authority/execution
  should carry an uppercase, dim, `tracking-widest` doctrine sentence at
  the bottom — same pattern as the Roster panel. This reminds the
  operator what they're looking at without taking screen space.

---

## 7. Motion

Less is more.

- Use Tailwind `transition-colors` (≈150ms) for hover/focus.
- Don't `transition: all` — it breaks `transform` mid-animation.
- Page-level: a single staggered reveal on first paint is acceptable
  (e.g. brain row cards). Avoid recurring entrance animations on data
  refresh — they create motion sickness on a polling console.

---

## 8. `data-testid` discipline

Every interactive element and every user-facing data point must carry a
`data-testid`. Format: kebab-case, describing **function** (not
appearance). Examples:

```jsx
<Button data-testid="roster-save-btn-executor" />
<div   data-testid="roster-occupant-governor" />
<div   data-testid="roster-lane-equity" />
```

The testing agent depends on these. Components without testids are
considered un-testable and will be flagged.

---

## 9. Forbidden patterns

- ❌ Emoji as icons in component markup (acceptable in copy, never in UI
  chrome). Use Phosphor or Lucide.
- ❌ Purple/violet gradients on white. RISEDUAL is dark-only.
- ❌ Rounded-pill UI on operator data. Rounding is reserved for hero
  surfaces (marketing pages, public chat).
- ❌ Inline color hex literals. Always go through the `rd-*` tokens.
- ❌ Centered single-column page layouts. We're not a marketing site;
  the operator wants the left margin tight against authoritative data.

---

## 10. Updating this doc

Treat changes here as you would changes to `namespaces.py`: open the file,
edit the section, ship the supporting code change in the same commit.
Don't fork the design system into per-page conventions — that's how the
console gets unreadable.

If you're adding a new visual primitive (new badge variant, new
lane-aware control, new state color), document it here BEFORE you ship
it. The 30 minutes you spend writing the doc save the next agent two
hours of pattern archaeology.
