# RiseDual Public Integration Kit

> **What this is**: drop-in TypeScript client + types + per-page swap notes that point your existing risedual.ai frontend at Mission Control's `/api/public/*` namespace.
>
> **What this is NOT**: MC does not handle billing, credits, tiers, or user accounts. You already have those. MC is intelligence-only.

## Architecture (Direction C — Two faces, one brain)

```
┌────────────────────────────────┐
│ risedual.ai                    │
│ • Stripe + credits + tiers     │
│ • User auth                    │
│ • Frontend UI (keep as-is)     │
└──────────────┬─────────────────┘
               │ HTTPS
               │ X-RiseDual-Token        ← shared secret (MC env)
               │ X-RiseDual-User-Tier    ← propagated from your user model
               ▼
┌────────────────────────────────┐
│ Mission Control                │
│ /api/public/*                  │
│ • signals / digest / scanner   │
│ • agent-activity / models-mind │
│ • heatmap / sectors            │
└────────────────────────────────┘
```

## Trust contract

Two headers, validated as a unit by MC:

| Header | Required | Purpose |
|---|---|---|
| `X-RiseDual-Token` | ✓ | Shared bearer secret. MC sets `RISEDUAL_PUBLIC_TOKEN` env var; you store the same value in risedual.ai's backend env and never expose to clients. |
| `X-RiseDual-User-Tier` | ✓ | One of `free \| starter \| pro \| pro_max`. MC uses this to gate sanitization (locked rows on free-tier digest, etc.). Both `free` and `starter` are treated as non-paid. |

If `X-RiseDual-Token` is missing or wrong → MC returns **401**.
If `X-RiseDual-User-Tier` is unknown → MC returns **422**.
If MC env var isn't set → MC returns **503**.

**Never call MC directly from the browser.** Always proxy through your own backend so the token doesn't leak.

## Files

| File | Drop into |
|---|---|
| `types.ts` | `frontend/src/lib/types/mc.ts` (or wherever your types live) |
| `mcPublicClient.ts` | `frontend/src/lib/mcPublicClient.ts` |
| `python_types.py` | `backend/services/mc_types.py` (if your backend re-shapes responses) |
| `SWAP_NOTES.md` | reference for which page calls which endpoint |
| `ENV_CHECKLIST.md` | env vars to add on your side |

## Endpoint quick reference

| Endpoint | Replaces | Returns |
|---|---|---|
| `GET /api/public/signals` | Active Signals tile + AI Consensus hero | list of signal cards + aggregate consensus |
| `GET /api/public/signals/{id}` | War Room (single signal detail) | adversarial + governance views in one payload |
| `GET /api/public/digest` | Daily Market Digest | predictions / smart_money / alerts, capped per tier |
| `GET /api/public/scanner/presets` | Scanner preset list | 10 presets (MACD cross, BB squeeze, RSI extremes, etc.) |
| `GET /api/public/scanner/scan?preset_id=…` | Scanner results | matches array with strength + detail |
| `GET /api/public/agent-activity/feed?since=…&limit=…` | Agent Activity / Workspace tape | polled feed, ~10s cadence |
| `GET /api/public/models-mind/{symbol}` | Model's Mind feature panel | 10 features per symbol, normalized to 0-100 |
| `GET /api/public/heatmap` | Market Heatmap | per-symbol 24h % change + color band |
| `GET /api/public/sectors` | Sector Rotation | sector ETF universe; marks `degraded:true` until ETFs are fed |

## Tier behavior summary

| Tier | Digest predictions | Digest smart_money | Digest alerts | Other endpoints |
|---|---|---|---|---|
| `free` | 2 + 1 locked CTA | 2 + 1 locked CTA | 1 + 1 locked CTA | full access |
| `starter` | 2 + 1 locked CTA | 2 + 1 locked CTA | 1 + 1 locked CTA | full access |
| `pro` | up to 25 | up to 25 | up to 25 | full access |
| `pro_max` | up to 25 | up to 25 | up to 25 | full access |

> Free/starter tiers receive locked-row placeholders (`{locked: true, kind, upgrade_to, cta}`) when there's more data available. Your UI renders these as the existing `_locked_more_row` CTA.

> War Room and AI Chat themselves are NOT gated by MC — your existing tier gating handles that. MC's tier header only governs *content sanitization*.

## When MC's data is incomplete

MC returns sane partial responses rather than hard 500s when underlying data hasn't been fed yet:

- `heatmap` with `degraded:true` → no feeders configured yet
- `sectors` with `degraded:true` → sector ETFs aren't in the feed yet
- `models-mind` features with `coverage:"not_wired"` → MC doesn't compute that feature yet (e.g., `earnings_proximity`, `sector_rs`). Render greyed-out or hide.

## Rollout

1. Add `RISEDUAL_PUBLIC_TOKEN` env var on MC (already done).
2. Add the same value as `MC_PUBLIC_TOKEN` on risedual.ai's backend env.
3. Set `MC_BASE_URL=https://mc.<your-domain>` on risedual.ai's backend env.
4. Drop `mcPublicClient.ts` into your frontend; route one page through it (we recommend "Daily Market Digest" first — smallest blast radius).
5. Compare outputs to the existing fake data. Fix any UI assumptions on YOUR side.
6. Cut over the rest page-by-page using `SWAP_NOTES.md`.
