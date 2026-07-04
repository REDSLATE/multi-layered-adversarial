# Sign-off: equity spread enricher — Webull SDK schema drift + spread sanity guards

**Status:** DRAFT — awaiting operator sign-off AND Monday-market-open verification. Not applied.  
**Scope:** `backend/shared/snapshot_enrich/equity_doctrine.py` only.  
**Blast radius:** equity lane only. Crypto lane untouched (separate fallback path in `spread_enrichment.py:297`).  
**Doctrine coverage:** none of the three fixes touches gate logic, ladder state, sizing, or brain hypothesis parameters. Enrichment field-mapping and spread sanity only.

## 0. BLOCKING PREREQUISITE — Monday-market-open live verification

The diagnostic dump backing this sign-off was pulled **Saturday 2026-07-04 06:22 UTC**. US markets have been closed since **Thursday 07-02 20:00 UTC** (Friday 07-03 was Independence Day observed; Saturday is weekend). Every symbol probed showed `last_trade_time` at 07-02 20:00:00–20:00:01 UTC — the exact Thursday RTH close tick.

**What this means for the field-name fix:** the current dump proves the SDK *carries* `last_trade_time` and `quote_time` fields, and proves the parser probes the wrong names. But it does **NOT** prove `last_trade_time` advances intraday during a live trading session — only that the fields exist and hold a session-close value at rest. If Webull's SDK only populates these fields at session-close snapshots (not tick-by-tick during trading), then the field-name fix would still leave every intraday quote tagged `stale` because the extracted age would still exceed the 15s stale threshold on every fetch.

**Falsifiable claim to verify before shipping:** on Monday 07-06, during a window between 13:35 UTC (5 min after open) and 19:55 UTC (5 min before close), a raw SDK dump on the Barracuda universe should show `last_trade_time` values within seconds-to-minutes of `now()`, not hours-old. Specifically:
- Expected: `now - last_trade_time` < 60s for high-liquidity names (NVDA, SPY) during RTH
- Expected: `now - quote_time` < 15s for high-liquidity names during RTH
- Failure mode: `last_trade_time` still shows a value hours-old during RTH → the field is populated at session-close only, and the field-name fix is insufficient. Different fix needed (probably a `received_at`-clock proxy, which has different staleness semantics per your earlier flag).

Command to run Monday 13:35 UTC:
```bash
cd /app/backend && set -a && source .env && set +a && python3 -c "
import sys, time
from datetime import datetime, timezone
sys.path.insert(0, '/app/backend')
from shared.market_data.webull_quotes import get_quotes_client
c = get_quotes_client()
now = time.time()
# Tiered by liquidity so we can distinguish uniform-fail from liquidity-tiered results:
# TIER_MEGA — sub-cent spread expected, should tick every second
# TIER_ETF  — highly liquid basket, should also tick tightly
# TIER_MID  — mid-cap single names, may tick less frequently
# TIER_THIN — thinner names, may have gappy ticks even during RTH
UNIV = [
    ('MEGA', ['NVDA','MSFT','AAPL','AMZN','META','GOOGL','TSLA']),
    ('ETF',  ['SPY','QQQ','IWM','DIA']),
    ('MID',  ['AAL','F','PLTR']),
    ('THIN', ['AMC','GME','SPCE']),
]
for tier, syms in UNIV:
    for sym in syms:
        s = c.equity_snapshot(sym)
        if not s:
            print(f'{tier:5} {sym:6}: SDK returned None'); continue
        ltt = s.get('last_trade_time'); qt = s.get('quote_time')
        def age(v): return (now - float(v)/1000.0) if v else float('inf')
        # Fix #2 evidence: also capture bps + bid/ask now that book is open
        bps = s.get('bps'); bid = s.get('bid'); ask = s.get('ask'); price = s.get('price')
        print(f'{tier:5} {sym:6}: ltt_age={age(ltt):>7.1f}s  qt_age={age(qt):>7.1f}s  '
              f'bps={bps!r:>10}  bid={bid!r:>10}  ask={ask!r:>10}  price={price!r}')
"
```

**Interpretation matrix (three distinct outcomes to distinguish):**

| Outcome | Pattern | Fix scope |
|---|---|---|
| **Uniform pass** | All tiers show `ltt_age < 60s`, `qt_age < 15s` during RTH | Fix #1 as drafted, ships alone first |
| **Liquidity-tiered pass** | MEGA/ETF show sub-minute ages; MID/THIN show gappy or stale ages | Fix #1 ships with a caveat: `stale_threshold_sec` should become liquidity-tiered (e.g., 15s for MEGA/ETF, 60s for MID, 300s for THIN) rather than one global 15s threshold. Non-trivial doctrine addition — needs its own sub-review before shipping. |
| **Uniform fail** | Even NVDA/SPY show hours-old timestamps during RTH | Field-name-drift theory is falsified. `last_trade_time`/`quote_time` are session-close-only fields, not intraday. Whole sign-off package pivots to `received_at`-clock proxy design. Different investigation. |

**Also captured in the same Monday pull:** `bps`, `bid`, `ask`, `price` per symbol. Reason: the Saturday probe showed `bps=0` on all 9 ETFs and negative on AAL/AMC. Those may be closed-session artifacts (the SDK returning last-good stale bid + a placeholder ask that was never re-quoted during the shutdown) rather than intrinsic bugs. If Monday's open-hours pull shows those same symbols reporting sane positive `bps` values, then fix #2's plausibility gate becomes optional defense-in-depth rather than required-for-correctness — and can be deferred out of the initial patch. If they persist during RTH, fix #2 is confirmed necessary and stays in.

**Do not ship fixes #1/#2/#3 until this verification passes.** If verification fails, the sign-off package needs to be rewritten around a different mechanism (likely a `received_at` clock proxy for freshness estimation, plus a stricter subscription-entitlement audit).

### 0b. Confounder to watch during Monday verification — preview-pod hibernation

Separately traced during this session's crypto investigation: preview backend pods on this cluster get spun down after ~30–90 min of idle time, then cold-start on new traffic. Observation window `07-02 06:00 → 07-04 05:38 UTC` contains **four separate multi-hour outages** (up to 14.4h each) where the backend process was NOT running — cross-referenced against `/var/log/supervisor/supervisord.log` lifecycle events. During those windows, zero intents get emitted from either lane, zero logs get written, and any staleness metric measured against wall-clock will show as `dead`.

**Implication for Monday's verification:** if the verification command is invoked cold against a hibernated preview pod, the first snapshot fetches will be against a client that's still initializing — potentially returning `None` or partial data. **Warm the pod with a preliminary hit before running the verification block:**

```bash
# Warm the pod first — trigger an HTTP handler so the backend is fully warm
API_URL=$(grep REACT_APP_BACKEND_URL /app/frontend/.env | cut -d '=' -f2)
curl -s "$API_URL/api/health" >/dev/null
sleep 3
# THEN run the Section 0 verification block
```

Additionally: **any hourly cadence numbers pulled from `shared_intents` for cross-checking Monday's fix will be biased by whichever pod (preview vs prod) happened to be up during the sample window.** Post-Monday, when comparing pre-fix vs post-fix `spread_quality='live'` rates, filter by `pod_hostname` (or `evidence.pod_hostname` on the intent doc) to avoid attributing hibernation gaps to the fix's efficacy or lack thereof.

Not a fix requirement — just a note to prevent misdiagnosis of the verification result.

## 0a. Red herring investigated and cleared

The initial hypothesis that `_check_token_enable result is False` (logged twice per snapshot call) might indicate a structural entitlement issue was investigated by reading the Webull SDK source at `/root/.venv/lib/python3.11/site-packages/webull/core/http/initializer/client_initializer.py:90–115`. Finding: this flag is a **server-side config toggle** that only gates whether the SDK proactively warms its token cache on init. It does **not** gate real-time entitlement, does not force delayed-data mode, and does not indicate auth failure. Real-time entitlement is verified independently via `get_app_subscriptions()` which returns `us_stock_quotes: true` for the current account. The log line is benign noise, not a signal.

Separate low-priority follow-up: the double-log per snapshot (88ms apart) suggests the client is being re-initialized more often than strictly needed. Not a correctness bug. Deferred.

---

## 1. Findings recap (traced upstream in this order)

1. Equity HOLD-collapse observed at 07-02 06:00 UTC → dampener bug hypothesized.
2. Coordinated hypothesis re-weighting confirmed (hold ↑0.13, observe ↑0.13, buy ↓0.21, sell ↓0.11), but confidence pipeline unchanged (Δ=0.0002) → bug is in hypothesis scoring, not confidence.
3. `brain_core.py` spread-quality guard shipped ~07-03 03:00 UTC and IS working on current intents (fresh AAL BUY at 07-04 06:12 UTC uses `spread_bps=25.0` substitution correctly).
4. But the *upstream* issue — 99.8% of observed equity quotes tagged `stale`/`sentinel` — persists. Ruled out feed regression / MQTT reconnect / entitlement lapse via live SDK probe.
5. Root cause: three stacked bugs in `equity_doctrine.py`, all `.get()`/validation.

Live SDK probe confirms the mechanism:

- Webull SDK returns `last_trade_time` and `quote_time` as ms-epoch integers on 24/24 sampled symbols (broad ETFs, sector ETFs, leveraged ETFs, mega-caps, mid-caps, penny stocks).
- Parser at `equity_doctrine.py:224, 232` probes only `mkTradeTimeTs`, `tradeTimeTs`, `mkTradeTime`, `tradeTime` — none present on any of the 24 symbols.
- Enricher recomputes spread from `bid`/`ask`, ignoring SDK's own `bps` field.
- `bid`/`ask` are unreliable off-hours (NVDA ask=236 = 52wk-high, DIA ask=528 vs bid=458, XLE ask=63 vs bid=49).
- SDK's own `bps` field is **also** partially broken (11/24 symbols implausible: all 9 ETFs return `bps=0`; AAL and AMC return **negative** bps).

## 2. Fix #1 — timestamp probe extension (definitive, safe)

**File:** `backend/shared/snapshot_enrich/equity_doctrine.py`  
**Function:** `_quote_age_seconds`  
**Change:** two-line addition to each probe branch.

Proposed diff (unified format):

```diff
     # ms-epoch field (preferred — least ambiguity)
-    ts_ms = snap.get("mkTradeTimeTs") or snap.get("tradeTimeTs")
+    ts_ms = (
+        snap.get("mkTradeTimeTs")
+        or snap.get("tradeTimeTs")
+        or snap.get("quote_time")       # Webull SDK 2026-07 payload
+        or snap.get("last_trade_time")  # Webull SDK 2026-07 payload
+    )
     if ts_ms:
         try:
             return max(0.0, _time.time() - float(ts_ms) / 1000.0)
         except (TypeError, ValueError):
             pass
```

- Preference order: `quote_time` before `last_trade_time` — quote_time is more recent on all 24 sampled symbols (represents session-close ask/bid time; last_trade_time is the last actual print, which stops updating after RTH close).
- ISO/string probe (`snap.get("mkTradeTime") or snap.get("tradeTime")`) left untouched — those field names may resurface with a future SDK version and there's no harm in leaving the fallback.

**Behavior change:** during market-open hours with fresh Webull data, `quote_age_sec` now populates with a real number instead of `None` → `spread_quality` tagger no longer defaults every quote to `stale`.

**Non-changes:** no gate logic, no scoring, no sizing, no persistence schema.

## 3. Fix #2 — treat `snap['bps']` as sanity signal, NOT preferred source

**Reversal from my earlier suggestion.** The verify-only sweep showed `snap['bps']` is broken on 46% of symbols (all ETFs return 0, two single-names return negative). Trusting it as authoritative would be catastrophic — SPY genuinely trades at ~1 bps spread, and if we accept `bps=0` we'd promote a 0-bps spread through to sizing, which the RoadGuard doesn't have a floor on. **Reject-and-fall-through is safer.**

**File:** `backend/shared/snapshot_enrich/equity_doctrine.py`  
**Function:** `_enrich_sync`, in the spread computation block around line 291.

Proposed logic (pseudocode, not exact diff yet — needs verified line targeting):

```
sp = _spread_bps(bid, ask, price)   # existing derivation

# NEW: cross-check against SDK's own bps field, but only trust the CROSS-CHECK,
# not the value. Detect impossibility, don't override.
sdk_bps = _to_float(snap.get("bps"))
if sdk_bps is not None:
    if sdk_bps <= 0.0:
        # SDK reports 0 or negative → the underlying quote is untrustworthy.
        # DO NOT trust the derived sp either; treat as sentinel.
        sp = None  # falls through to spread_enrichment.py's sentinel path
    elif sp is not None and sdk_bps > 0 and abs(sp - sdk_bps) / max(sp, sdk_bps) > 0.90:
        # Disagreement > 90% between derived and SDK-reported.
        # (e.g. sp=2150 derived from stale ask, sdk_bps=8 from fresh internal)
        # Log-loud, treat as untrustworthy → sentinel path.
        logger.warning(
            "spread_disagreement sym=%s derived=%.2f sdk_bps=%.2f "
            "bid=%.4f ask=%.4f price=%.4f — treating as sentinel",
            sym, sp, sdk_bps, bid, ask, price,
        )
        sp = None
```

- Chosen threshold `> 90% disagreement` is a defensive floor. On the observed NVDA case, derived=2150 vs sdk=8 → disagreement = 99.6% → sentinel. On a healthy market where both agree within 10%, no change to behavior.
- No sizing knob, no gate change. Just moves untrusted spreads to the sentinel path, which the operator-approved 07-03 guard already handles by substituting 25 bps.

**Alternate simpler form** if the disagreement-check feels like scope creep: just add the `sdk_bps <= 0` check, drop the disagreement branch. That alone catches all 9 ETFs + AAL + AMC in the current dataset.

## 4. Fix #3 — sentinel-cap on derived spread (defense in depth)

**File:** same.  
**Purpose:** if bid/ask-derived spread crosses the sentinel threshold (≥ 999 bps), don't propagate the value — set `spread_bps` to the `SPREAD_BPS_UNKNOWN` sentinel and let downstream code do its normal thing. Avoids passing "2150.59" through as a hard number that downstream heuristics might latch onto.

Proposed diff sketch:

```diff
     sp = _spread_bps(bid, ask, price)
     if sp is not None:
+        # If derived spread exceeds the sentinel threshold, the underlying
+        # bid/ask pair is untrustworthy (typically off-hours stale ask).
+        # Emit sentinel value so the quality tagger below stamps 'sentinel'
+        # and downstream substitutes the guard default (25 bps in brain_core).
+        if sp >= sentinel_threshold_bps:
+            sp = SPREAD_BPS_UNKNOWN  # imported from spread_enrichment
         out["spread_bps"] = round(sp, 2)
         # ... existing quality tagger runs on the sentinel value → 'sentinel'
```

- Behaviorally equivalent to the existing sentinel tag at line 319–320, but sets the *value* to the shared sentinel constant instead of propagating "2150.59" as if it were signal. Cleaner audit trail: every sentinel row now has the same numeric value.

**Optional — I'd hold this back unless operator asks for it.** #1 and #2 alone resolve the observed pathology; #3 is cosmetic on top.

## 5. Regression tests (golden — pin current buggy behavior first)

Rationale: before shipping the fix, land tests that document what the buggy behavior is. Two purposes: (a) if the fix ever gets reverted or refactored, the tests fail immediately; (b) the test file itself is doctrine documentation.

**File to create:** `backend/tests/test_equity_doctrine_spread_enricher.py`

Test cases (pytest, no HTTP, direct function calls):

1. **`test_current_parser_returns_none_on_current_webull_shape`** — passes a fixture matching the actual SDK payload (33-key NVDA dump). Asserts `_quote_age_seconds(snap) is None`. This pins the bug pre-fix.

2. **`test_extended_parser_reads_quote_time_ms_epoch`** — POST-FIX test. Same fixture. Asserts `_quote_age_seconds(snap)` returns a positive float within (0, 3600).

3. **`test_extended_parser_prefers_quote_time_over_last_trade_time`** — fixture with both fields, `quote_time` newer. Asserts the returned age matches the `quote_time`-derived age, not `last_trade_time`.

4. **`test_extended_parser_falls_back_to_last_trade_time_when_quote_time_missing`** — fixture with only `last_trade_time`. Asserts non-None.

5. **`test_sdk_bps_zero_triggers_sentinel_fallthrough`** — SPY-shape fixture (bid, ask, price present; `bps=0`). Asserts `_enrich_sync` results in `spread_bps == SPREAD_BPS_UNKNOWN` and `spread_quality == 'sentinel'`.

6. **`test_sdk_bps_negative_triggers_sentinel_fallthrough`** — AAL-shape fixture (`bps=-6.16`). Same assertion.

7. **`test_sdk_bps_and_derived_agree_within_tolerance`** — NVDA-shape fixture with plausible bid/ask (`sp≈8`, `snap.bps=8.07`). Asserts derived spread is used, no sentinel.

8. **`test_sdk_bps_and_derived_disagree_triggers_sentinel`** — synthetic fixture: bid/ask compute to 2150 bps but `snap['bps']=8`. Asserts sentinel fallthrough. (Only if fix #2's disagreement branch ships; skip if simpler form chosen.)

9. **`test_derived_spread_over_sentinel_threshold_capped`** — synthetic: bid=458 ask=528 (DIA off-hours shape). Asserts `spread_bps` is exactly `SPREAD_BPS_UNKNOWN`. (Only if fix #3 ships.)

10. **`test_end_to_end_open_hours_quote_tags_live`** — full `_enrich_sync` with a fixture representing a healthy open-hours snapshot (`quote_time` = current epoch-ms, tight bid/ask, plausible bps). Asserts `spread_quality == 'live'` and `quote_age_sec < 15`.

Test fixtures pulled directly from the live SDK dumps in this investigation — they represent actual production payloads, not hand-crafted approximations.

## 6. CFQS gate implication — READ BEFORE APPROVING

The three fixes above quietly correct market-data inputs that have been feeding every brain's equity hypothesis scoring since well before 07-01. The `hypothesis_hold` saturation-to-1.0 wave (07-02 06:00 → 07-03 03:00 UTC) was the loud symptom. But there's a quieter fact behind the loud one:

**Every equity fire prior to this fix has been graded against `spread_quality='stale'` or `'sentinel'` conditions with a substituted 25-bps proxy spread standing in for reality.**

Which means for the currently-locked CFQS gate:

- **30-fires-minimum + 15%-beat-margin gate on Barracuda equity:** any fires counted against pre-fix data were counted against **substituted spread inputs**, not measured ones. If Barracuda hits 30 fires between now and when this ships, those 30 fires' expectancy calculations included a fictional spread axis on every one.

- **Recommended:** flag pre-fix-window fires with a `spread_input_substituted=True` marker (or exclude them from the 30-count entirely) rather than letting them contribute silently. Otherwise the gate could unlock the next ladder rung on partially-synthetic evidence, and by the time the discrepancy surfaces, real capital will be sized against it.

- **The crypto lane is not affected** — Kraken public ticker has its own live-quote path, and the earlier hypothesis-scoring bug we chased didn't materially move crypto (`hold%` stayed 0–12% throughout the observed window). Crypto's CFQS counter is safe to keep counting.

This is the meaningful downstream effect the fixes have on operator-facing gates. Flagging it here because it belongs in the sign-off, not in the code comments.

## 7. Deploy sequencing recommendation

- **Ship fix #1 alone first.** Two-line change to timestamp probe. Once shipped, monitor `spread_quality='live'` rate during Monday 07-06 13:30 UTC (market open) — expect jump from ~1.5% → ≥90% on the Barracuda universe. That's the falsifiability check on fix #1's mechanism claim.
- **Then fix #2** (sdk_bps sanity, simpler form: reject `bps <= 0`). Shipping this before observing #1's effect risks masking whether #1 alone is sufficient.
- **Hold #3 unless observed necessary.** #1 + #2 should already route all bad spreads to sentinel; #3 is defense-in-depth. Adding it in the same review makes the fix look bigger than it is.

## 8. Files changed if approved

- `backend/shared/snapshot_enrich/equity_doctrine.py` — three targeted edits, ~10 lines total across the three fixes.
- `backend/tests/test_equity_doctrine_spread_enricher.py` — new test file, ~150–200 lines.
- **No** changes to: `brain_core.py`, `base_labels.py`, any gate logic, any brain doctrine, any env vars, any `.env` files.

## 9. Rollback plan

Fix #1 rollback: revert the four-line `or snap.get(...)` chain to the original two-name `or` chain. Effect: `spread_quality` regresses back to always-`stale` — same state as before the fix. No downstream data corruption because downstream code already tolerates `stale` gracefully via the existing 07-03 `brain_core.py` guard.

Fix #2 rollback: remove the `sdk_bps <= 0` block. Effect: enricher goes back to using potentially-negative or -zero derived spreads.

Fix #3 rollback: remove the `sp >= sentinel_threshold_bps` cap. Effect: sentinel row's `spread_bps` field goes back to holding the derived value (e.g. 2150.59) instead of the shared sentinel constant.

Rollback is per-fix; the three do not depend on each other.
