# RiseDual Public Site (MVP)

Lives in `/app/frontend/src/risedual/` — a self-contained module mounted
at `/r/*` inside the Mission Control frontend. When you're ready to
retire Alpha's site role, point `risedual.ai` DNS at this deploy and
remap the route in `App.js` from `/r` → `/` (and move MC operator pages
under `/admin/*`).

## Routes (live)

| Path | Page | Data source |
|---|---|---|
| `/r` | Landing — hero, council, features, CTA | static |
| `/r/signals` | Active signals + council consensus | `GET /api/public/signals` |
| `/r/digest` | Daily narrative + predictions | `GET /api/public/digest/narrative`, `GET /api/public/digest` |
| `/r/chat` | RiseDualGPT chat (Pro Max only) | `POST /api/public/chat` |

## Env

Two vars used:
- `REACT_APP_BACKEND_URL` — MC's public URL (already set)
- `REACT_APP_RISEDUAL_TOKEN` — opaque trust token sent as `X-RiseDual-Token`. Matches MC's `RISEDUAL_PUBLIC_TOKEN`.

## Tier model

The header tier selector (`Free / Starter / Pro / Pro Max`) drives the
`X-RiseDual-User-Tier` header on every API call. Persisted to
localStorage as `risedual_site_tier`. Pro Max unlocks RiseDualGPT chat.
When real billing lands, replace `TierContext` with whatever the
auth/billing system surfaces.

## Files

```
risedual/
├── Layout.jsx                    # dark fintech shell w/ tier selector
├── context/TierContext.jsx       # tier state + localStorage
├── lib/mc.js                     # public API client (typed-ish)
└── pages/
    ├── Landing.jsx
    ├── Signals.jsx
    ├── Digest.jsx
    └── Chat.jsx
```

## Aesthetic decisions (locked)

- **Dark** (#000 / zinc-950 surfaces) with emerald accent + rose for shorts + amber for governor flags.
- **Display font:** Chivo (already in `tailwind.config.js`).
- **Mono:** JetBrains Mono for labels, timestamps, tier badges.
- **Sharp lines:** soft borders (`border-zinc-900`), no shadows, generous whitespace.
- All interactive elements have `data-testid` (`rd-*` prefix).

## What it deliberately does NOT do (yet)

- No login. Tier selector is the placeholder for billing.
- No payment / credit deduction. risedual.ai's billing layer owns that.
- No scanner/heatmap/agent_activity pages — Phase 2.
- No Stripe webhook listener — Phase 2.
