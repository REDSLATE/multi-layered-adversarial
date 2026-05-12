# Per-page swap notes

For each existing risedual.ai page, here's what to replace and what
the new MC endpoint returns. Numbers refer to the data shapes
documented in `types.ts`.

## Dashboard / Markets

| Existing widget | New source |
|---|---|
| **Active Signals** card grid | `mcPublic(tier).signals().items[]` — each item is one card. Use `direction`, `consensus`, `flagged_by_auditor`, `thesis`. |
| **AI Consensus** hero panel | `mcPublic(tier).signals().consensus` — has `buy_pct`, `sell_pct`, `hold_pct`, `label`. |
| **Win Rate / Total Wins / Flagged** stat row | Aggregate from `signals().items[]` — count `direction !== "HOLD"` and `flagged_by_auditor` flags. (Win rate requires resolved-outcome data which MC has in `scorecards` — let me know if you want a `/public/stats` endpoint.) |

## War Room

| Existing widget | New source |
|---|---|
| **STRATEGIST_AGENT** block | `mcPublic(tier).signal(id).governance.strategist` |
| **RISK_AUDITOR_AGENT** block | `mcPublic(tier).signal(id).governance.auditor` — `action: "PASS"|"VETO"`, `mode` describes why. |
| **SYNTHESIZED SIGNAL** block | `mcPublic(tier).signal(id).governance.synthesized` |
| Adversarial Bull/Bear (if your UI has it) | `mcPublic(tier).signal(id).adversarial` |

> Both framings come from the same position. They will not disagree with each other.

## Daily Market Digest

| Existing widget | New source |
|---|---|
| **TOP AI PREDICTIONS** (AMZN HOLD 66%) | `digest().predictions[]` — each has `{symbol, direction, confidence, price}`. Use `isLockedRow(row)` to render the upgrade CTA for free/starter. |
| **SMART-MONEY SHIFTS** (TSLA score 70) | `digest().smart_money[]` |
| **REGIME ALERTS** (INTC delta -12) | `digest().alerts[]` |
| **MARKET OVERVIEW** narrative | `digest().overview.summary` (plain text). If you want LLM-generated prose, ask MC to add `/public/digest/narrative` in Phase 2. |

## Market Scanner

| Existing widget | New source |
|---|---|
| Preset list (left rail) | `scannerPresets().presets[]` — all 10 presets. |
| Results panel for a preset | `scan(presetId).matches[]` — `{symbol, strength, detail}`. `scanned`/`matched` for the count badges. |
| Category filter tabs (All / Bullish / Bearish / Momentum / etc.) | Filter `presets[]` on `category` and `signal` client-side. |

## Workspace / Agent Activity feed

| Existing widget | New source |
|---|---|
| Live narrative tape (`[mean_reversion] scan complete · ...`) | `agentActivity({since, limit}).items[]` — poll every 10s with `since=<last event timestamp>`. Use `severity` for the color (info=neutral, success=green, warn=amber, error=red). |
| **Daily Market Digest preview** modal | `digest()` for the full preview; Send button is YOUR backend's Resend integration. |

## Per-symbol view (Model's Mind)

| Existing widget | New source |
|---|---|
| 10 feature bars (`score_2W`, `distance_from_mw`, etc.) | `modelsMind(symbol).features` — each is `{score: 0-100, value, coverage?}`. `coverage: "not_wired"` → grey it out or hide. |
| Last close + last update timestamp | `modelsMind(symbol).last_close` / `last_bar_ts`. |

## Heatmap / Sector Rotation

| Existing widget | New source |
|---|---|
| Market Heatmap grid | `heatmap().items[]` — `change_24h_pct` + `color_band` map directly to the existing color scheme. |
| Sector Rotation cards | `sectors().items[]` — `best` and `worst` are pre-computed convenience pointers. If a sector has `coverage: "not_wired"`, show greyed-out (MC isn't getting that ETF's bars yet). |

## What you DO NOT swap

These stay on your side — MC is intelligence-only:

- **My Watchlist** — your DB. Stays.
- **Stripe webhooks + credit ledger** — your DB. Stays.
- **AI Chat (RiseDualGPT)** — your existing implementation. Phase 2 may add a grounded chat endpoint to MC if you want it backed by MC's positions/technicals, but it's optional.
- **User auth / sessions** — your DB. Stays.
- **Onboarding / Tour / Referrals** — yours.
- **Connect Broker** — yours (you have your own broker UI; MC has separate operator-only broker connections for its own ingest).
- **Bots** — yours. MC doesn't trade in Phase 1.
- **Kill switch UI** — yours, OR proxy through MC's existing operator endpoint. If you want it surfaced publicly to admin users, ask for `/api/public/admin/kill-switch` (Phase 2).

## Suggested rollout order

1. **Daily Market Digest** — easy to compare to your existing fake data; small UI; tier behavior testable immediately.
2. **Market Heatmap** — pure read, no tier logic, instant visual feedback.
3. **Active Signals** — replaces the fake AI; biggest customer-visible impact.
4. **War Room** — most complex shape; do it after #3 so you've got the Active Signals plumbing already working.
5. **Agent Activity** — polling pattern; do it last so you've already debugged the proxy.
6. **Model's Mind** — per-symbol detail page.
7. **Sector Rotation** — currently degraded until ETF feeds are wired.
