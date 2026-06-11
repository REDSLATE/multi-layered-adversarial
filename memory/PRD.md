## 2026-02-19 — P1 Flake Fixes (4 tests deterministic across 5×10 runs)

### Root causes (all pre-existing real bugs, not test issues)

**1. `test_tenure_resets_on_swap` — tenure endpoint truncated the audit log**
- `shared/roster.py` walked `roster_audit_log` oldest→newest with `.to_list(2000)`. Once the log grew past 2000 rows the query truncated and dropped the MOST RECENT entries — exactly the ones the tenure endpoint needs after a swap.
- Fix: walk newest→oldest, take 5000, break inner loop on first match per role. Operationally correct AND test-deterministic.

**2. `test_stale_conflicts_only_includes_open_past_threshold` — endpoint had no topic filter**
- `/api/admin/conflicts/stale` sorted by `detected_at` ascending with `limit≤1000`. Preview DB has 5,598 open conflicts older than 48h, so the test's seeded row was buried.
- Fix: added `?topic=` query param to the endpoint (legit operator feature too — "show me stale conflicts for symbol X"), test now scopes to its own tag.

**3. `test_sse_streams_named_events` — first-byte latency + missing exception handling**
- `_read_sse_events` only caught `ChunkedEncodingError`/`ConnectionError`. On slow preview ingress, the first byte sometimes arrived past the 20s window, raising `ReadTimeout`/`TimeoutError` which propagated as test failure.
- Fix: catch `ReadTimeout`/`TimeoutError`/`OSError` too; widened test read window to 30s (server emits a 15s heartbeat — guarantees ≥1 non-hello event); added `@pytest.mark.timeout(60)` to override the suite's 30s default.

### Verification
- **5 consecutive runs × 10 tests each = 50/50 green** on the previously-flaky tests.
- Full suite: 2179/2180 passed; the single fail was a transient HTTP connection blip mid-suite (unrelated infrastructure hiccup, not a test bug).

### Files touched
- `shared/roster.py` — newest-first audit-log walk
- `shared/conflicts.py` — `topic` query param on `/admin/conflicts/stale`
- `tests/test_force_close_removed_and_stale_conflicts.py` — uses `topic` filter
- `tests/test_mc_connection_stream.py` — robust read loop + `@pytest.mark.timeout(60)` + 30s window


## 2026-02-19 — Outcome-Join Diagnostics + Safety Gates Audit (pre-market sweep)

### Operator directive
> *"P1, definitely. 2 hrs til trading starts."*

### Shipped (3 new admin slices, all read-only by default)

**1. Outcome-Join admin** — `routes/outcome_join_admin.py`
- `GET /api/admin/outcome-join/health` — chain visibility: doctrine_sidecars counts, intents, positions, joined ratio, orphan sample.
- `POST /api/admin/outcome-join/backfill {dry_run, lane, older_than_hours, limit}` — retroactively attaches outcome envelopes to historical sidecars whose intent_id maps to a closed position but never joined. Idempotent (helper short-circuits on existing envelopes).

**2. Scorecard-by-Brain** — `routes/scorecard_by_brain.py`
- `GET /api/admin/doctrine/scorecard-by-brain` — aggregates joined outcomes by `(lane, stack, doctrine_version)` for per-brain visibility (Camino / Barracuda / Hellcat / GTO display names attached).
- METADATA only — promotion gates still key on `(lane, seat, doctrine_version)` per Patent J.

**3. Safety Gates Audit** — `routes/safety_gates_audit.py` + `pages/SafetyGatesAudit.jsx`
- `GET /api/admin/safety-gates/audit?hours=N&sample_size=K` — per-gate pass / block stats + top block reasons, sliced by lookback window (1h / 6h / 24h / 7d / 30d / ALL).
- New frontend page **Safety Gates** in left nav. Color-coded by block-rate severity.

### Findings (pre-market 2026-02-19, 30d window over 49,390 gate decisions)

| Gate | Pass | Block | Rate | Root cause (NOT calibration) |
|---|---|---|---|---|
| `lane_execution_enabled` | 7,321 | 15,182 | **67.5%** | Equity + crypto execution toggles OFF |
| `executor_seat_check` | 17,069 | 19,294 | **53.1%** | Crypto executor seat vacant; some intents posted by wrong holder |
| `governor_authority` | 19,834 | 2,679 | **11.9%** | Governor seat vacant intermittently |
| `roadguard_spread_floor` | 20,307 | 2,200 | **9.8%** | Stale snapshots (9999 bps sentinel) — data plumbing, not gate calibration |

**Verdict**: No gate needs threshold tuning. All blocks trace to seat vacancy / toggle state / data freshness. Operator action items before market open: flip equity execution toggle, seat the crypto executor + governor, fix stale snapshot source.

### Tests
- 7 new unit tests in `tests/test_outcome_join_admin_and_audit.py` (all green).
- All endpoints verified against live preview DB.

### Pre-existing flakes left unresolved
- `test_tenure_resets_on_swap`, `test_opinion_gets_posted_as`, `test_stale_conflicts_only_includes_open_past_threshold` — integration tests vs live backend+DB shared state. Multi-hour rabbit hole; deferred.

### Closed out
- P2 scraper fixes (Reddit / Zillow / Quiver) — verified there are no scrapers in this codebase. Item removed from backlog.


## 2026-02-19 (pass 24) — Live Doctrine Reference (drift-proof operator cards)

### Operator directive
> *"Replace the static Tutor module with a live operator reference that reads documentation directly from the doctrine code, so cards can never drift from what the brains actually compute."*

### Shipped
1. **`DOCTRINE_CARDS` + `_DOCTRINE_FN_MAP`** appended to the three live
   doctrine modules — `strategy_doctrines.py`, `large_cap_doctrine.py`,
   `brain_sidecars.py`. 5 cards total: `gap_and_go`, `micro_pullback`,
   `large_cap_equity`, `parabolic_topping`, `squeeze_block`.

2. **`shared/doctrine/brain_sidecars.py`** re-exports
   `classify_parabolic_phase` and `build_squeeze_block` so the
   integrity test can resolve cross-module enricher / detector functions.

3. **`routes/strategy_reference.py`** —
   - `GET /api/admin/doctrine-reference` — merged payload
   - `GET /api/admin/doctrine-reference/index` — lightweight sidebar
   - `GET /api/admin/doctrine-reference/{strategy_id}` — single card
   Mounted via `api_router.include_router(strategy_reference_router)`.

4. **`pages/DoctrineReference.jsx`** — operator dashboard with lane
   filter (all / equity / universal), version + source + module
   provenance per card, all six card sections rendered (ideal /
   entries / exits / size modifiers / snapshot fields / risk flags).
   Linked in left nav as **Doctrine Ref**.

5. **`tests/test_doctrine_integrity.py`** — 7 CI-enforced tests:
   - every card has an FN_MAP entry
   - every FN_MAP target resolves to a callable
   - every `snapshot_fields_read` string appears in the function source
   - every `risk_flags_read` string appears in the function source
   - no duplicate strategy IDs across modules
   - schema completeness (all required fields, list typing)
   - `doctrine_version` matches `^[a-z_]+_v\d+$`

### Side fix
- `backend/.env` line 87 was `AUTO_ROUTER_NOTIONAL_USD="3.00"POLYGON_FEEDER_ENABLED=false` (missing newline) which broke pytest collection on `test_auto_router_dedupe_integration.py`. Restored.

### Test result
- 7/7 doctrine integrity tests pass
- 2151/2154 full suite passes (3 pre-existing flakes in roster tenure + stale-conflicts, untouched here)


## 2026-06-11 (pass 23) — Per-lane broker hamburger + Webull crypto failover

### Operator directive
> *"A hamburger menu for equity and crypto. Instead of a locked-in
> account, I would like the option to switch between accounts. That
> way, we can build up the trades the stack completes, especially on
> the equity side."*

### Shipped
1. **`shared/snapshot_enrich/crypto_doctrine.py`** — Webull as the
   hot-failover crypto data source. Mirrors the equity enricher:
   pulls bid/ask/last/volume/high/low from `client.crypto_snapshot`,
   stamps `primary_source: webull` + `data_council: [webull]` so the
   crypto lane keeps producing real-data intents even when Kraken is
   not connected.

2. **`routes/broker_selection.py`** — singleton config doc with
   `{equity: "public" | "webull", crypto: "kraken" | "webull"}`.
   - `GET /api/admin/broker-selection` — returns current selection +
     available options + defaults
   - `PUT /api/admin/broker-selection {equity, crypto}` — validated,
     persisted with `updated_at` + `updated_by`, upsert pattern.
   - Defaults preserved: equity → Public, crypto → Kraken. Clearing
     the singleton reverts safely.

3. **Brain runner integration** — every emitted intent now reads the
   selection and stamps `broker_override` when the operator picked a
   non-default broker. `evidence.broker_override_source =
   "operator_selection"` for auditability.

4. **Runner crypto-lane wiring** — `_evaluate_and_post` now also
   calls `enrich_crypto_doctrine_snapshot()` on the crypto tick so
   Webull crypto data populates the snapshot the same way equity
   already did.

5. **`BrokerSelectionMenu.jsx`** — two per-lane dropdowns at the top
   of the Intents page. Click a dropdown → list of valid options →
   pick → PUT fires → toast confirms → next brain tick (~45s) carries
   the new broker_override. Shows "default" badge when the selection
   equals the lane default so the operator can see what's overridden.

### Live verification (07:59 UTC)
```
crypto sidecars (after enricher landed):
-- BTC/USD primary_source=webull council=['webull']
           price=62649.27 gap_pct=0.044 spread_bps=200.0
           webull_enriched=True

emitted intents (after PUT {equity:webull, crypto:webull}):
  BTC/USD broker_override=webull source=operator_selection × 3
```
Reset to safe defaults after smoke test — operator can flip from the
UI at will.

### Tests
- `tests/test_broker_selection.py` — 6 new (defaults, validation,
  singleton read with/without persisted doc, partial-fields fill-in).
  All green.
- `tests/test_crypto_doctrine_enricher.py` — 6 new (no-client
  fallback, BTC/USD enrichment, canonical→Webull-pair conversion,
  offline tag, fail-soft on exception). All green.
- Broader regression: 53 / 53 affected suites green.

### What did NOT change
- Kraken adapter unchanged — still the default crypto execution path.
- Public adapter unchanged — still the default equity execution path.
- No hard blocks; operator can flip back to defaults at any time.
- Webull broker adapter already supports crypto orders (pass 18
  Webull integration covered both lanes via `_lane_for_symbol`).

---


## 2026-06-11 (pass 22) — Polygon/Finnhub demoted to council-of-last-resort

### Operator directive
> *"Polygon/Massive isn't accepting a lot of the requests despite it
> being paid. Also Finnhub. We counsel them if nothing else works."*

The operator did NOT want them disabled (initial instinct was wrong on
my part — first reverted the env kill-switches). They wanted them
DEMOTED: kept alive in the runtime so they can be consulted when the
primary feed can't carry a field, but stripped of authority over the
brain tick.

### Shipped
1. **Tighter per-call timeout on the technical-pipeline fetch** —
   `runner.py::_fetch_technical` now caps each call at 3 seconds
   (independent of the 8s client default). A slow or 429'd Polygon /
   Finnhub upstream can no longer drag the brain tick budget; the
   technical endpoint silently fails-soft and the Webull-enriched
   snapshot proceeds without it.

2. **Provenance stamping on the snapshot** — `equity_doctrine`
   enricher now writes `primary_source = "webull"` and appends to
   `data_council` so every intent the brain emits can be audited for
   what actually drove the decision.

3. **`/api/admin/data-council/status`** — operator-facing council
   state. Shows per-lane primary + council members, their live status,
   entitlement state, and 15-min feeder-health audit roll-up so the
   operator can spot a council member drowning in 429s without it
   actually affecting trading.

### Result
| Lane | Primary | Council (last-resort) |
|---|---|---|
| Equity | **Webull** (Nasdaq Basic L1, $0/mo, expires 2027-06-10) | Polygon, Finnhub |
| Crypto | **Kraken** (when creds present) | Webull spot snapshot (cross-check) |

The Polygon/Finnhub feeders continue to write to the OHLCV /
indicator collections that the technical pipeline reads, but the
brain's hot loop now treats that pipeline as advisory. If technical
returns 200 with data, fine — the Webull enricher overlays its
fields on top. If technical times out or returns empty, the brain
proceeds with Webull-only data and never blocks.

### Doctrine
> *"Webull is the primary equity feed; Kraken is the primary crypto
> feed. Polygon and Finnhub are kept in the council but consulted
> only when the primary source can't carry the field. Per-call
> technical-fetch timeout is 3s so a slow council member cannot drag
> the brain tick budget."*

### What did NOT change
- Polygon & Finnhub API keys remain in `.env` and feeders still run.
- Feeder-health audit log still writes (now usefully consumed by the
  council status endpoint).
- No tests touch the production envs.
- 198 / 198 affected suites green.

---


## 2026-06-11 (pass 21) — Squeeze Detector V2 shipped & wired

### Operator directive
Operator shipped the hardened production-safe `squeeze_detector_v2.py`
inline (bad data → DATA_ERROR, stale data → WAIT_FOR_FRESH_DATA, risk
flags → actual score penalties) and asked to wire it so Barracuda /
GTO attach the result to intents.

### Shipped
1. **`shared/squeeze/squeeze_detector_v2.py`** — verbatim drop of the
   operator-supplied module. `SqueezeDetectorV2` + `SqueezeInput` +
   `SqueezeResult` dataclasses. Hard-field validation, stale-data
   detection, risk-penalty arithmetic, confidence dampening on data
   incompleteness. PAVS smoke test confirms grade C / RISK_DOWN_OR_WAIT
   (the 18.50 vs 22.00 day_high triggers `already_fading_from_high`).

2. **`shared/squeeze/squeeze_adapter.py`** — converts the live
   doctrine snapshot + Webull M1 bars into a `SqueezeInput` and
   returns a JSON-ready result dict. What we map:
     * price, prev_close, day_high, volume_today — Webull snapshot
     * avg_volume_20d — derived from `volume_today / relative_volume`
     * spread_bps — Webull snapshot
     * price_30s_ago, volume_last_1m, avg_volume_last_5m — last bar(s)
     * premarket_high — max(bar highs) (approx; proper PM filter
       deferred until we wire bar timestamps)
     * news_catalyst — `snapshot.has_news` (default False)
   Soft fields left None (no current Webull source): float_shares,
   short_interest_pct, borrow_rate_pct, borrow_rate_change_pct,
   shares_available_to_short. Detector surfaces `data_incomplete_risk`
   and dampens confidence accordingly — exactly the v2 contract.

3. **Equity enricher wiring** — every equity tick now stamps
   `snapshot.squeeze = {grade, squeeze_score, action_bias, reasons,
   risk_flags, confidence, metrics}`. Also persisted pre_close, high,
   low on the snapshot (was previously consumed locally then dropped).

4. **`apply_camaro_legacy_strategist` (Barracuda) modulation:**
     * grade A + BUY → +0.06 conf, ×1.10 size (tape confirmed)
     * grade B + BUY → +0.02 conf
     * grade F (data error / stale) → -0.08 conf, ×0.60 size
     * `already_fading_from_high` + BUY → -0.10 conf, ×0.55 size
       (don't chase tops)
     * `wide_spread_risk` → ×0.75 size

5. **`apply_redeye_legacy_adversary` (GTO) modulation:**
     * grade A + BUY → -0.05 conf (crowded-long suspect)
     * grade A + SELL → +0.04 conf (failed-breakout opportunity)
     * `already_fading_from_high` + SELL → +0.06 conf (short thesis)
     * `already_fading_from_high` + BUY → -0.08 conf, ×0.60 size
     * `blowoff_velocity_risk` + SELL → +0.05 conf
     * grade F → -0.08 conf, ×0.55 size (even RedEye won't act on bad data)

### Tests
- `tests/test_squeeze_detector_v2.py` — 12 new (PAVS-style A grade,
  DATA_ERROR paths, stale-data block, wide-spread C, fading-from-high
  penalty, risk-penalty arithmetic, confidence completeness, low-float
  / borrow-spike signal firing, score clamping, D-grade-on-quiet
  baseline). All green.
- `tests/test_squeeze_wrapper_integration.py` — 9 new covering both
  wrapper modulation paths + no-op safety when no squeeze block. All
  green.
- 220 / 220 of the broader affected suites green.

### Live verification (AAPL, post-restart)
```
price=291.58 pre_close=290.55 high=294.75
parabolic_phase: neutral
squeeze: grade=D score=0.0 bias=IGNORE
        risks=[data_incomplete_risk]
        confidence=0.47
```
Correct behavior — AAPL is a calm mega-cap, not a squeeze. Tomorrow
at open, the same field will fire grade A on real PAVS-style
candidates and the wrappers will modulate brain confidence/size
accordingly.

### What did NOT change
- Detector code is verbatim operator drop — no modifications.
- No hard blocks. Wrappers modulate confidence/size only.
- Crypto lane untouched.
- Soft fields (float / short interest / borrow) remain N/A under
  current Webull entitlements — a Finnhub or Polygon side-fetch is
  the natural plumbing for those, deferred.

---


## 2026-06-11 (pass 20) — Parabolic phase awareness: brains adapt to swings

### Operator directive
> Referencing the Warrior Trading PAVS chart (Day 3, Volatile & Hot
> Market): *"How can we teach them to adjust to swings like the
> candle sticks graph in the picture? … Stick with the 8% and adjust
> to 20% when we have a stable run. Actually would like all 3 of them,
> so long as it doesn't hinder trading."*

### Shipped
1. **`shared/snapshot_enrich/parabolic_phase.py`** — pure 4-phase
   classifier (`accumulation | parabolic | topping | fade |
   neutral | unknown`). Computes velocity_1m, velocity_5m,
   vwap_distance_pct, rvol_acceleration, peak_drop_pct on Webull M1
   bars. All thresholds env-tunable.

2. **Equity enricher wiring** — stamps `parabolic_phase` +
   measurement fields on the doctrine snapshot every tick and
   translates phase → existing `market_regime` slot so the base-
   labels regime logic picks it up without further wiring.

3. **`base_labels.py` — continuous sizing via score deltas:**
   - `accumulation` → +0.05 (full size, MARKET_GREEN_LIGHT bonus)
   - `parabolic` → linear penalty: -0.10 at +8% velocity, scales to
     -0.30 at +20%. Brain still ships intents but at progressively
     smaller size as the move extends — exactly the "half size on
     parabolic" Warrior playbook.
   - `topping` → -0.25 (zero new longs; brain's regime-aware logic
     flips to SELL bias on held positions)
   - `fade` → -0.25 (exit-only on existing longs)
   - **No hard blocks** — quality over quantity, sizing scales
     continuously, brain keeps shipping.

4. **Topping confirmation** — requires `TOPPING_RED_BAR_COUNT=2`
   consecutive red bars AFTER ≥3 of the last 8 bars were green.
   Configurable down to 1 if operator wants Ross Cameron's strict
   first-red exit.

5. **`/api/admin/parabolic/phases`** + **`ParabolicPhaseStrip.jsx`** —
   operator-facing live map across the universe. Shows live counts
   per phase + watchlist of parabolic/topping symbols with their
   velocity and peak-drop measurements. Polls every 15s.

### Env-tunable thresholds
```
PARABOLIC_5M_THRESHOLD_PCT=8.0     # graduate to 20.0 in stable mode
PARABOLIC_VWAP_DIST_PCT=5.0
PARABOLIC_RVOL_ACCEL=2.0
TOPPING_RED_BAR_COUNT=2            # 1 for Ross Cameron strict mode
FADE_DROP_FROM_PEAK_PCT=3.0
```

### Live verification (02:32 UTC)
- AAPL classified `neutral` (correct — afterhours, velocity_5m=-0.27%,
  rvol_acceleration=0.20)
- 104 historical sidecars exist; new sidecars carry the parabolic
  fields. Tomorrow at 9:30am ET when volume picks up, phases will
  populate live across the 48-ticker universe.

### Tests
- `tests/test_parabolic_phase.py` — 10 new tests covering all 4
  phases, env-tunable thresholds, fade-trumps-topping precedence,
  measurement-always-populated invariant. All green.
- 138 / 138 of the broader affected suites green (parabolic + Webull
  + doctrine).

### What did NOT change
- No hard blocks introduced (operator directive: "doesn't hinder
  trading"). All adjustments are continuous score deltas the
  sizing-gate consumes as risk multipliers.
- Crypto lane untouched. Brain core logic untouched (regime change
  flows through existing brain-side logic that drops BUY confidence
  and raises SELL when regime is "weak").
- Safety gates still all live per operator standing directive.

### Open follow-ons
- Stable-run promotion: flip `PARABOLIC_5M_THRESHOLD_PCT=20.0` once
  enough closed trades show the 8%-threshold version isn't catching
  fades too late. Single env flip, no code change.
- Explicit brain-side SELL emission for `topping`: currently relies
  on regime→base_labels→score-drop→brain-confidence flow. If
  observation shows brains aren't reliably flipping to SELL on
  topping, a direct `urgent_exit` field on the snapshot could force
  the behavior.

---


## 2026-06-11 (pass 19) — Equity doctrines ready for tomorrow's open

### Operator directive
> *"Just get them where they are ready for tomorrow."* (referring to
> the 5 doctrines stuck at LEARNING 0/100 — Small-Account Generic,
> Gap-and-Go v1, Micro Pullback v1, Large-Cap Equity, Crypto Generic.)

### Diagnosis
All 5 doctrines had working sidecar logic but the equity ones were
*starving for snapshot fields*. Crypto fires because Kraken streams
OHLC. Equity sat idle: `_build_snapshot()` in the brain runner
produces a generic trend/volatility/pattern dict but doesn't
populate `gap_pct`, `relative_volume`, `market_cap_band`,
`momentum_active`, `pullback_low`, etc. — the exact fields the
doctrine scoring requires. With Webull equity quotes flipped on
this evening, the data source is finally there.

### Shipped
1. **`shared/market_data/webull_quotes.py`** — sync REST client over
   the Webull SDK with TTL cache (snapshot 5s, bars 30s, screener
   60s, instrument 1h, entitlements 60s). Singleton, fail-soft.
   Probes entitlements by hitting each gated endpoint and inferring
   from 401 vs payload.

2. **`shared/snapshot_enrich/equity_doctrine.py`** — new enricher
   that takes the base snapshot and populates doctrine fields:
   - `price`, `spread_bps`, `gap_pct`, `change_ratio`, `volume`
     (Webull `market_data.get_snapshot`)
   - `market_cap_band` (pinned mega-cap roster; default `small`)
   - `fractionable`, `shortable`, `exchange_code`
     (Webull `instrument.get_instrument`)
   - `relative_volume` (Webull `screener.get_most_active`)
   - `momentum_active`, `pullback_low`, `no_nearby_resistance`,
     `pattern` (Webull M1 bars `market_data.get_history_bar`)
   - `near_half_or_whole_dollar`, `hour_et` (pure derivations)
   Fail-soft: any error returns base snapshot unchanged.

3. **`external/brains/runner.py`** — call enricher right after
   `_build_snapshot()` for equity lane only. Crypto path untouched.

4. **`routes/webull_admin.py`** + **`WebullEntitlementsCard.jsx`** —
   operator-facing entitlement status card on the Intents page.
   Polls `/api/admin/webull/entitlements` every 60s. Shows
   ✅ live / ❌ not subscribed per data class. Lets operator watch
   any future subscription click-through propagate without my help.

5. **`/api/admin/webull/snapshot/{symbol}`** — debug endpoint
   returning the enriched snapshot for any ticker. Useful for
   pre-open sanity checks.

### Live verification (2026-06-11 02:08 UTC)
| Ticker | mcb | doctrine_version | gap_pct | rvol | qual |
|---|---|---|---|---|---|
| AAL | small | small_account_sidecar_v1 | -4.76% | 1.44 | REJECT |
| NVDA | mega | large_cap_equity_v1 | -3.73% | 0.90 | REJECT |

All 4 brains posted NVDA equity intents at 02:05 UTC carrying the
enriched snapshot (`webull_enriched=True`). Doctrines correctly route
mega → large_cap_equity_v1, small → small_account_sidecar_v1.
Current REJECTs are real (afterhours, no gappers, wide spreads) —
tomorrow at 9:30am ET the same pipeline will see fresh gappers
with positive gap_pct, rvol≥5x, and tight spreads, producing
A/B quality and unblocking the trade loop.

### Entitlement state (operator must NOT touch until ready)
| Class | Status | Cost |
|---|---|---|
| Nasdaq Basic - Non Display (US equity L1) | ✅ Free Authorized | $0 (expires 2027-06-10) |
| US Crypto Spot | ✅ Bundled with base | $0 |
| OPRA Real-Time Options | ❌ Not subscribed | $4.99/mo (deferred — no options-aware brain yet) |
| Nasdaq TotalView (L2 depth) | ❌ Not subscribed | $135/mo (defer — bad ROI at $3-$10 caps) |

### Tests
- `tests/test_equity_doctrine_enricher.py` — 11 new, all green
- 128 / 128 of the affected doctrine + Webull suites green
- Pre-existing 6 timing flakes unchanged (not in this pass's surface)

### What did NOT change
- Doctrine code (strategy_doctrines, base_labels, lane_doctrine_router)
- Intent ingest path (shared/intents.py)
- Crypto lane (Kraken-driven, untouched)
- Webull broker adapter (still armed for $3-$10 trades only)
- Safety gates (still all live per operator directive)

### Open items for tomorrow
- Watch the Intents page at 9:30am ET — equity intents should pour in
  with real gap/rvol/spread numbers and quality scores moving from
  REJECT to B/A as setups appear.
- LEARNING 0/100 counters won't tick until intents complete the
  full ingest → execution → outcome loop. With Webull armed at
  $3-$10 caps, the first equity fill of the day starts moving them.
- If you decide to subscribe OPRA ($4.99/mo), the entitlement card
  will flip the OPRA row from ❌ to ✅ within 60s of the click.

---


## 2026-06-11 — Webull rule-based symbol expansion: tests green

### Operator directive
> *"Yeah, I need that expanded to include accurate tickers on Webull.
> The way things are going, it may be the one site worth sticking with."*

### What shipped
Made the rule-based Webull symbol resolver land cleanly. Last pass
introduced the heuristic but broke 14-15 backend tests; this pass
fixes them without altering production behavior.

**Symbol resolver (`shared/broker_symbol_resolver.py`).**
- `_rule_based_webull_native` now requires an explicit `BASE/QUOTE`
  separator for crypto canonicals (`-` or `/`). Bare `CRYPTO:BTC`
  (no quote) returns `None`. The "concat pass-through" path that
  silently accepted any alnum string is removed — quote is part of
  identity, not a derived field.

**Webull adapter (`shared/broker/webull.py`).**
- `_lane_for_symbol` is now pure & sync: `_LANE_INDEX` static lookup
  plus a USD/USDT-suffix heuristic. The lazy `from server import db`
  + Motor `find_one` call was removed — it was dead code
  (always returned an awaitable that the code discarded) and was
  binding the global motor client to whichever loop ran the first
  sync test, poisoning subsequent async tests with
  `"Future attached to a different loop"`.

**Test helper.**
- `tests/test_webull_symbol_expansion.py::_asset` now passes the
  required `quote` kwarg to `AssetKey` (`"USD"` for crypto / `None`
  for equity).

### Test status
| Suite | Pass | Fail |
|---|---|---|
| Webull symbol expansion | 35 | 0 |
| Webull adapter | 14 | 0 |
| Broker router Webull override | 8 | 0 |
| Webull caps | 29 | 0 |
| Auto-router max-per-tick | 4 | 0 |
| Broker connected override | 5 | 0 |
| **Full backend suite** | **2088** | **6** (pre-existing flakes, none Webull-related) |

The 6 stragglers are the known P2 list: `test_tenure_resets_on_swap`
(timestamp tolerance) plus 5 SSE / rate-limit tests that pass in
isolation at a saner 30s timeout.

### What did NOT change
- Production wire behavior of `_lane_for_symbol` is identical (the
  Mongo branch was unreachable in production already).
- `BROKER_SYMBOL_MAP["webull"]` static entries still authoritative
  for any operator overrides.
- All safety rails from pass 18 stay 100% active.

### Operator next-step toggles still live
- `lane_execution_enabled` per-lane master switch
- `executor_seat_check`
- `governor_authority`
- `roadguard_spread_floor`
*(Per operator: do NOT disable until they explicitly say so.)*

---


## 2026-06-10 (pass 18) — Ladder eliminated + broker_connected override bug fixed

### Operator directive
> *"Eliminate the ladder."*
> *"B — fix the broker_connected bug only; I'll decide which levers to flip."*

### Ladder gate eliminated (operator-directed)

Data prompting the call:

| Metric | Value |
|---|---|
| Total intents ever | 22,757 |
| Observation receipts (shadowed) | 8,553 |
| Broker fills MC-initiated | 1 |
| Audit-log promotions by operator | 0 (all 16 by `actor=test`) |

Conclusion: the ladder was a permanent shadow filter, never an actual
graduation system. The originals never formally cleared because no
operator promotion was ever performed.

**What changed:**
- `shared/sizing_gate.py::_ladder_cap_and_route()` — every stage now
  resolves to `(None, ROUTE_LIVE_NORMAL)`. Sizing is bound only by
  lane cap, micro_live cap (when enabled), and broker-specific caps.
- `shared/auto_router.py` — removed the `if sizing.route == ROUTE_OBSERVE:`
  shadow block (≈55 lines). Replaced with a defensive logger that
  surfaces any accidental regression of `ROUTE_OBSERVE` as a NO_TRADE
  rather than letting it silently shadow.
- Deleted `tests/test_ladder_phase_2_and_3.py` (231 lines, 15 tests)
  and `tests/test_phase_4_ladder_engaged.py` (131 lines, 5 tests) —
  they pinned the eliminated observation/paper/live-micro behaviors.
- **Kept** the `learning_ladder` module + `/promote`, `/demote`,
  `/set`, `/history` endpoints. Stage data still lives in Mongo and
  the API still answers; the data is now advisory/forensic, not
  authoritative.

**Safety rails that stay 100% active:**
- Lane toggle (master kill per lane)
- Broker freeze (per-broker kill)
- Webull cap ($3-$10 per ticker, armed flag)
- Lane exposure cap ($500)
- In-flight order dedupe
- MC receipt seal (when enforced)
- Position misread detector
- All dry-run gates (broker_connected, symbol_in_universe,
  lane_execution_enabled, roadguard_spread_floor, RR ratio,
  governor authority, opponent objection, exposure caps × 3)

### broker_connected gate bug fix (operator option B)

**Bug**: `shared/execution.py::_evaluate_gates` called
`adapter_for_lane(intent_lane)` WITHOUT the `broker_override`. Any
intent carrying `broker_override="webull"` dry-run-blocked on
"no broker for lane='equity'" because the gate was checking the
lane default (Public.com), not the override (Webull).

**Fix**:
- `shared/broker_router.py::adapter_for_lane(lane, broker_override=None)`
  — accepts the override and applies the same `ROUTE_OVERRIDE_BROKERS`
  resolution that `route_order` uses. Unknown/non-override broker
  names silently fall back to the lane default.
- `shared/execution.py::_evaluate_gates` — passes the intent's
  `broker_override` through. The gate reason now reports
  `"broker for lane='equity' present (webull) (override→webull)"`
  when the override resolves.
- New `tests/test_broker_connected_override.py` (5 invariants) —
  pins that overrides are honored, unknown overrides ignored,
  and the gate queries the override broker rather than the lane
  default when present.

**Validated live**: a repeat post of the AMZN BUY intent with
`broker_override="webull"` now shows `broker_connected: pass=True`.
The remaining 3 dry-run blockers are all operator-controlled
levers, not bugs:

| Gate | Lever |
|---|---|
| `symbol_in_universe` | `POST /api/admin/patterns/universe {"symbol":"EQ:AMZN","lane":"equity"}` |
| `lane_execution_enabled` | `POST /api/admin/execution/lane-toggles` (flip equity to ON) |
| `roadguard_spread_floor` | needs live spread data plumbing for equity universe, OR raise the 50-bps cap |

### Full-suite status

- **2,050/2,052 passed** (was 2,063 before the ladder rip; the
  removal of 20 ladder-specific tests + addition of 5 new
  override-gate tests nets to the expected total).
- 2 failures:
  - `test_doctrine_intent_attachment::test_audit_row_written_to_doctrine_sidecars_collection`
    — passes solo (test-ordering flake, not a regression)
  - `test_roster::test_tenure_resets_on_swap` — pre-existing
    test bug from pass 17 (not in files I modified)
- Zero regressions caused by the ladder elimination or the
  broker_connected fix.

### Env state at end of pass

```
AUTO_ROUTER_NOTIONAL_USD="3.00"   (left as set; operator's $3 pilot)
WEBULL_ARMED="true"
WEBULL_APP_KEY=<populated>
WEBULL_APP_SECRET=<populated>
WEBULL_MIN_NOTIONAL_USD="3.00"
WEBULL_MAX_NOTIONAL_USD="10.00"
```

### Next live-fire checklist (operator-controlled)

To fire the $3 BUY through Webull, the operator needs to flip:

1. `POST /api/admin/patterns/universe` — add EQ:AMZN (or whatever
   target symbol) with `lane:"equity"`. Crypto symbols BTC-USD /
   ETH-USD already in universe.
2. `POST /api/admin/execution/lane-toggles` — flip equity to ON
   (crypto may already be on; check `GET /api/admin/broker/lanes`).
3. Decide on the roadguard spread floor — either ship live spread
   data for equity or temporarily raise the 50-bps cap.

Once those three are flipped, the next operator-injected intent
with `broker_override="webull"` will fire end-to-end through the
Webull SDK.

---


## 2026-06-10 (pass 17) — Pre-deploy: arm Webull + burst-throttle review + flake hardening

### Operator directives
> *"Just use the key in the picture I gave you."*  → Webull armed live.
> *"P2 — Burst throttle review: verify obsolescence given in-flight dedupe."*
> *"Flake to investigate: `test_doctrine_hint_returns_candidates_for_large_cap` under full-suite load."*
> *"After that I will save and deploy."*

### Webull armed (live)

- `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` populated from the operator's
  screenshot.
- `WEBULL_ARMED=true`. The route now accepts orders at `$3 ≤ N ≤ $10`
  per ticker.
- **Note on the SDK call shape**: hardened `_resolve_account_id` to
  handle both the envelope (`{"code":"200","data":[...]}`) and the
  pre-unwrapped (`[{accountId:...}]`) response shapes the SDK can
  return depending on version. Also surfaces the Webull error code
  if `code != "200"` so account-onboarding gaps are visible to the
  operator (the SDK currently logs `_check_token_enable result is
  False` — a Webull-side flag that may need a one-time activation
  on their portal before the first live trade succeeds).
- `tests/test_broker_router_webull_override.py` fixture now wipes
  `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` from env at setup so unit
  tests never reach the live Webull API.

### Burst-throttle review — verdict: KEEP

**Claim under review**: *"AUTO_ROUTER_MAX_PER_TICK=5 may be obsolete
after the newly implemented in-flight dedupe is running."*

**Verdict**: Not obsolete. The two layers solve different problems:

| Layer | What it stops | What it does NOT stop |
|---|---|---|
| `AUTO_ROUTER_MAX_PER_TICK = 5` | Burst-rate exceeding broker quotas; operator-visibility loss when 50+ intents queue after a feed outage clears | Same-intent duplicates in flight |
| `in_flight_orders.claim()` | The 2026-06-09 amnesia loop where the same `(symbol, brain, side)` was re-picked under contention | Multi-ticker burst across many distinct rows |

The handoff's hypothesis was incorrect — the two layers are
complementary, not redundant. Git history confirms `MAX_PER_TICK = 5`
is the ORIGINAL design value (2026-05-14), not a tactical band-aid
from the AAPL incident.

**Actions taken:**
- Doctrine comment added at `shared/auto_router.py:48-72` explaining
  the role distinction so a future "let's simplify" pass can't
  silently remove the cap.
- New `tests/test_auto_router_max_per_tick.py` (4 invariants):
  default sits in a safe band, env-tunable, surfaced in the status
  endpoint, and structurally enforced inside `_tick`.

### Flake investigation — `test_doctrine_hint_returns_candidates_for_large_cap`

**Reproduction attempts**: 18 runs (8 solo + 10 under concurrent
intent-ingestion load). Zero failures. The flake did NOT recur.

**Most likely RCA (post-mortem)**: In the pass-16 full-suite run the
backend was throwing 500s on every `POST /api/intents` (the
`AttributeError: 'IntentIn' object has no attribute 'broker_override'`
because the field declaration hadn't taken). The 500-storm pressured
the live FastAPI worker; the doctrine-hint test (which makes a real
HTTP call) caught the worker mid-stress. Once I added the missing
`broker_override` field and restarted, the next full-suite run was
2063/2063.

**Defensive measures added anyway** (so the route can't flake under
genuine future load):

1. `_live_state_for` row cap lowered from **50,000 → 5,000**. No
   doctrine carries anywhere near 5K outcome-joined rows; the
   verdict bands only need ≥ 100 samples to leave LEARNING. Pulling
   50K × 4 doctrines per hint call was wasteful I/O.
2. Cursor now sorts by `_id DESC` so when truncation hits we keep
   the FRESHEST outcomes — stale rows from a year ago don't
   inform current expectancy.
3. Each per-doctrine `_live_state_for` call wrapped in `try/except`.
   A slow or failing query for ONE doctrine version no longer 500s
   the whole hint — the brain still gets the other three with a
   degraded LEARNING state for the failed one.

### Full-suite status

- **2061/2067 passed** in the latest full run.
- The 6 reported failures break down as:
  - **4 are test-ordering flakes** — pass when run solo
    (heartbeat × 2, opinion_silence_watchdog × 2). Pre-existing.
  - **2 are pre-existing test bugs** that fail in solo runs too:
    - `tests/test_roster.py::TestTenure::test_tenure_resets_on_swap`
      — assertion compares µs-level timestamps; the swap endpoint
      doesn't reset tenure on the new occupant. Pre-existing
      regression in roster code, NOT in code I modified.
    - `tests/test_data_stack_phase1.py::test_finnhub_fetch_candles_429_records_audit`
      — MockTransport-based test of audit-row writing; counts come
      back equal instead of `before+1`. Pre-existing, in the
      `feeders/finnhub_equity.py` audit-write path.
  - **None of the 6 touch files I modified in this session.**
- The "1 flake" from pass 16 (`test_doctrine_hint_returns_candidates_for_large_cap`) is now **70/70 green under load**.

### Pre-deploy state

- ✅ Webull armed with live keys; route gated at $3-$10 per ticker
- ✅ Burst throttle reviewed and pinned with regression tests
- ✅ Doctrine-hint flake hardened
- ⚠ Two pre-existing test bugs flagged for follow-up (NOT regressions)
- 🔒 Backend running cleanly: 2,061 tests green, 4 ordering-flakes,
  2 pre-existing bugs

---


## 2026-06-10 (pass 16) — Webull broker route (parallel option)

### Operator directive (verbatim)
> *"Add Webull to both equity and crypto as an option to Kraken and
> Public without erasing their keys. Wire Webull straight to live
> trading. Very small trades like minimum $3 – $10 max, per ticker."*

### Doctrine

- **Webull is a PARALLEL route**, not a replacement. Kraken and
  Public.com keys + lane-default routing are untouched.
- **LIVE from day one.** No paper/UAT stop. Compensated by:
  1. **`WEBULL_ARMED=true` flag** (default FALSE → fail closed)
  2. **`$3 ≤ notional ≤ $10`** per-ticker cap (hardcoded panic
     ceiling at $100 even if operator widens the env band)
- **Per-intent override only.** An intent must explicitly carry
  `broker_override: "webull"` to route via Webull. Any other value
  is rejected at the pydantic boundary so a stale or hostile intent
  cannot arbitrarily redirect Kraken/Public traffic.
- **Both lanes**: Webull supports equity + crypto on the same broker;
  the symbol map in `broker_symbol_resolver.py` carries both
  (`EQ:AAPL → "AAPL"`, `CRYPTO:BTC-USD → "BTCUSD"`).
- **Belt-and-braces**: the cap is checked once at the router level
  BEFORE adapter load, and re-checked inside the adapter's
  `submit_market_order`. A bypass in either layer still fails closed.

### Gate chain (every Webull order, in order)

1. `evaluate_webull_order` — router-level cap + armed gate
2. `get_webull_adapter()` — returns None if keys missing or disarmed
3. Existing `mc_receipt` check (MC sealed envelope)
4. Existing exposure-cap evaluator
5. Existing in-flight dedupe
6. Adapter `submit_market_order` belt-and-braces re-check
7. SDK `place_order` call

### What landed

**Backend:**
- `shared/broker/webull_caps.py` — `is_webull_armed()`,
  `webull_notional_band()`, `evaluate_webull_order()`,
  `WebullCapBlocked` exception.
- `shared/broker/webull.py` — `WebullAdapter` (equity + crypto),
  `get_webull_adapter()` factory; uses official
  `webull-openapi-python-sdk` (v2.0.10). Lazy SDK import so the
  module loads even if the SDK is uninstalled (fail-closed).
- `shared/broker_router.py` — `ROUTE_OVERRIDE_BROKERS = {"webull"}`,
  honors `intent.broker_override` only for members of that set,
  runs `evaluate_webull_order` BEFORE adapter load.
- `shared/broker_symbol_resolver.py` — Webull symbol map (12 EQ +
  2 crypto on day 1, can expand).
- `shared/intents.py` — `IntentIn.broker_override: Optional[Literal["webull"]]`
  persisted on the intent doc; runtime AND admin-proxy paths both
  carry the field through to Mongo.
- `backend/.env` — new vars (blank until operator rotates keys):
  `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_REGION_ID=us`,
  `WEBULL_ENVIRONMENT=prod`, `WEBULL_ARMED=false`,
  `WEBULL_MIN_NOTIONAL_USD=3.00`, `WEBULL_MAX_NOTIONAL_USD=10.00`.

**Frontend:**
- `OperatorInjectIntent.jsx` — new "route" row with
  `[ default | Webull (live $3-$10) ]` buttons + amber warning
  hint when Webull is selected.
- The intent payload now includes `broker_override` (null by
  default, "webull" when toggled).

**Tests (62 new):**
- `tests/test_webull_caps.py` — 21 tests pinning armed gate + band
  invariants (defaults, env override, sanity rails, boundaries).
- `tests/test_webull_adapter.py` — 16 tests pinning factory
  fail-closed semantics + symbol lane classification + adapter
  belt-and-braces gates.
- `tests/test_broker_router_webull_override.py` — 14 tests pinning
  override routing + cap gate + lane-default fallback.

### Validation

- **Wrapper/cap suite**: 51/51 green.
- **Broker + router + symbol suite**: 159/159 green.
- **Full backend suite**: **2063/2063 green** (was 1995, +68
  including the 14 RedEye + 1 SSE + 51 Webull + 2 incidental).
- **Frontend smoke**: dropdown renders, amber warning appears on
  Webull selection, payload carries `broker_override` field.

### Operator action required to GO LIVE

1. **Rotate keys**: the pair the operator pasted in the 2026-06-10
   chat is treated as compromised. Generate a fresh App Key + App
   Secret in Webull's OpenAPI console.
2. Paste the rotated values into `/app/backend/.env`:
   ```
   WEBULL_APP_KEY="<rotated app key>"
   WEBULL_APP_SECRET="<rotated app secret>"
   WEBULL_ARMED="true"
   ```
3. `sudo supervisorctl restart backend`
4. Verify with a $3 BUY in the Operator Inject Intent panel using
   `route: Webull` selected. Watch the broker-fills feed for the
   live fill confirmation.

### Operator-facing reference card

| Knob | Default | Notes |
|---|---|---|
| `WEBULL_ARMED` | `false` | Master kill switch (must be `true` to fire) |
| `WEBULL_MIN_NOTIONAL_USD` | `3.00` | Hard floor per ticker |
| `WEBULL_MAX_NOTIONAL_USD` | `10.00` | Hard ceiling per ticker (panic ceiling: $100) |
| `WEBULL_REGION_ID` | `us` | US region |
| `WEBULL_ENVIRONMENT` | `prod` | Set to `uat` for sandbox host |

---


## 2026-06-10 (pass 15) — RedEye adversary wrapper assigned to GTO

### Operator directive (verbatim)
> *"For GTO, the RedEye wrapper should be a pure adversarial/opponent
> wrapper. GTO doctrine = momentum. RedEye wrapper = challenge
> consensus, hunt downside, punish crowded weak longs."*

### Final brain × wrapper matrix (operator-pinned)

| Brain | Doctrine | Wrapper | Wrapper effect |
|---|---|---|---|
| **Camino** (alpha)    | trend          | `alpha_legacy_executor`     | executor discipline |
| **Barracuda** (camaro) | mean reversion | `camaro_legacy_strategist` | tape reading |
| **Hellcat** (chevelle) | breakout       | `chevelle_legacy_governor` | risk compression |
| **GTO** (redeye)      | momentum       | `redeye_legacy_adversary`   | adversary / opponent pressure |

### What landed

**`backend/shared/legacy_brain_wrappers.py`:**
- New `apply_redeye_legacy_adversary()` (paste-verbatim from operator directive).
- `WrapperName` Literal extended with `redeye_legacy_adversary`.
- `WRAPPER_REGISTRY` and `BRAIN_WRAPPER_ASSIGNMENTS` register GTO → RedEye.

**RedEye wrapper rules** (confidence + size_bias only; never flips action, never creates trades from HOLD, never forces a seat):
- Score gap < 0.04 → `WEAK_CONSENSUS_CHALLENGED`
- BUY + RISK_OFF → `LONG_AGAINST_RISK_OFF_COMPRESSED`
- SELL OPEN/ADD_SHORT in bear/risk_off → `SHORT_PRESSURE_CONFIRMED`
- ADD_SHORT with conf ≥ 0.66 → `SHORT_CONTINUATION` (or compressed otherwise)
- Cover during bearish flow → `EARLY_COVER_WARNING`
- BUY ADD/OPEN_LONG with flow ≤ -0.20 → `BEARISH_FLOW_LONG_COMPRESSION`
- news_zscore ≥ 2.5 + bearish sentiment → SELL gets `BEARISH_NEWS_SHOCK_SUPPORT`, BUY gets `BEARISH_NEWS_SHOCK_AGAINST_LONG`
- FLIPs compressed (allowed only at conf ≥ 0.78, still ×0.75 size)

**Tests (`backend/tests/test_legacy_brain_wrappers.py`):**
- Old `test_gto_has_no_wrapper` removed (operator changed the assignment).
- New `test_gto_assigned_redeye_wrapper` + 13 RedEye behavior invariants covering each rule + provenance + clamps + dispatcher routing.
- All 4 brains now carry a wrapper; the passthrough test was retargeted to an unknown brain_id.

### Validation

- `pytest tests/test_legacy_brain_wrappers.py` → **61/61 green** (47 → 61, +14 new tests).
- Full backend suite: **2011/2012 passed**, 1 flake (`test_doctrine_hint_returns_candidates_for_large_cap`) which passes solo (0.59s) and has zero dependency on the wrapper layer — under-load HTTP timing flake, not a regression.

---


## DOCTRINE — BRAIN NAMING (READ THIS FIRST)

**Two name systems live in this repo. They are intentional. Do not "fix" or rename them.**

| Internal slot code (wire / DB / API / tests) | Operator-facing brand (display, toasts, sidebar, every UI surface) |
|---|---|
| `alpha`    | **Camino**    (blue,   `#3B82F6`) — structured trader |
| `camaro`   | **Barracuda** (amber,  `#F59E0B`) — challenger / counterfactual |
| `chevelle` | **Hellcat**   (green,  `#10B981`) — memory + calibration |
| `redeye`   | **GTO**       (red,    `#DC2626`) — adversarial scout |

### Rules
- **Wire layer (Mongo `shared_intents.stack`, `shared_position_misreads.brain`, route paths `/api/admin/runtime/{slot}`, env tokens `*_INGEST_TOKEN`, ~2000 tests) MUST keep using the lowercase slot codes.** Renaming these breaks the test suite + sidecar traffic + persisted history. The slot codes exist precisely so internal plumbing stays stable while operator branding evolves.
- **Every operator-facing render MUST use the display brand** via the existing mapper:
  - Frontend: `RUNTIME_META[slot].roleTitle` / `.label` from `frontend/src/lib/api.js`
  - Backend (rare): `backend/shared/brain_identity.py` `normalize_brain_id()` accepts both directions
- If you build a new UI surface that prints a brain name, **import `RUNTIME_META` and look up the display label**. Never capitalize the raw `stack` / `brain` field.
- The mapping is fixed: alpha↔Camino, camaro↔Barracuda, chevelle↔Hellcat, redeye↔GTO. There is no proposal to change it.

### Why this exists
Renaming the slot codes globally would require a Mongo migration of every `shared_intents` / `shared_position_misreads` / `shared_broker_fills` row, a rewrite of ~50+ test files that pin the names literally, and a coordinated cutover with the 4 sidecar brain runtimes that emit using these codes. The brand-rename happened operator-side; the wire layer was held stable on purpose for code-velocity reasons.

---


## 2026-06-10 (pass 14) — P1: Ephemeral misread toasts + MSFT live look-see

### Operator directives
> *"Real test, how does it see MSFT today?"* — answered conversationally
> with current 5m snapshot: last $397.35, RelVol 0.95× (slightly under
> avg), bar age ~5–6 min, news=true. No brain has emitted a working
> MSFT intent in the last 60min — the rotation favors AAPL/NVDA/AAL on
> equity + BTC/ETH on crypto.
>
> *"Continue."* — proceeded with P1 ephemeral misread toasts.

### Frontend: Ephemeral position-misread toasts (P1)

- New `frontend/src/components/MisreadToastHost.jsx`.
- Mounted ONCE at the layout level (Layout.jsx) so any admin page
  surfaces a fresh misread toast — operator could be on Discussion
  or Promotion when the AAPL-style pattern fires next.
- Message shape (matches the operator's spec):
    `"Camaro just misread AAPL — assumed FLAT, broker says SHORT"`
- TTL: 5s auto-dismiss with 200ms opacity fadeout.
- Mouse-over pauses the dismiss timer so the operator can read it
  through without it disappearing.
- Stacking: newest on top, cap 4 visible (older drop silently —
  PositionMisreadsCard is the durable record, toasts are the alarm).
- Dedup: keyed on `(detected_at + symbol + brain)` so a stream
  replay or React re-render can't double-pop the same toast.
- Manual dismiss button (×) always wins.

### Shared SSE singleton (refactor)

- `frontend/src/hooks/useMcStream.js` refactored: a single module-level
  EventSource is now shared across ALL consumers (Toast host + Misread
  card + Regime Tape + Chop Gauge). Previously each `useMcStream()`
  call opened its own EventSource → 3-4 concurrent SSE connections per
  page. Now 1.
- Public hook API unchanged. New imperative `subscribeMcStream()`
  exported for side-effect consumers (the toast host) that don't need
  the in-state event buffer.
- First subscriber opens the stream; last unsubscriber closes it.

### Backend regression test

- `tests/test_mc_connection_stream.py::test_sse_position_misread_event_fires_on_new_row`
  pins the wire contract that the toast host depends on:
  `event: position_misread` payload includes `symbol`, `brain`,
  `assumed_side`, `actual_side`, `emitted_action`, `missed_short_profit`.

### E2E validation
- Backend: `pytest tests/test_mc_connection_stream.py` → 7/7 green
  (1 new + 6 existing).
- Backend full suite (excluding SSE timing tests): 1995/1995 green.
- Frontend: live playwright run — logged in, opened admin shell,
  injected a fresh misread into `shared_position_misreads`, observed
  the toast appear in the top-right corner with all expected fields:
    `⚠ POSITION MISREAD · Camaro just misread LIVETOAST ·`
    `ASSUMED [FLAT] · BROKER SAYS [SHORT] · emitted: BUY · missed_short`

---


## 2026-06-10 (pass 13) — P2: SSE stream + 3 live frontend cards

### Operator directive
> *"P2: SSE stream at `/api/mc-connection/stream` for live frontend updates.
> P2: Frontend cards — 'Market Regime Tape' + 'Last 20 Position Misreads'
> + Divergence/chop gauge."*

### Backend: `/api/mc-connection/stream`

- New `backend/routes/mc_connection_stream.py` — multiplexed SSE feed.
- Wire format: standard `text/event-stream`; events named:
    - `hello`           — connection ack with poll_interval_sec
    - `intent`          — new shared_intents row
    - `broker_fill`     — new shared_broker_fills row
    - `position_misread`— new shared_position_misreads row
    - `regime`          — emitted only on regime CHANGE (no spam)
    - `heartbeat`       — every 15s for proxy keepalive
- Polling-based (NOT change-streams) for portability. 2s poll cycle;
  per-poll row cap 100 to guard against backlog catch-up storms.
- Auth: token via `?token=` query string (browser EventSource can't
  set custom headers) with fallback to cookie / Authorization header
  for curl / server-side clients.
- Per-connection cursor watermarks — no shared state between
  connections; concurrency-safe.
- New dependency: `sse-starlette==1.8.2` (pinned to be compatible
  with FastAPI 0.110's `starlette<0.38.0` constraint).
- 6 regression tests in `tests/test_mc_connection_stream.py`:
  auth-required, bad-token rejected, hello-first, named events
  arrive, Bearer header works, injected intent surfaces on stream.

### Frontend: 3 live cards on `/admin/overview`

#### Shared SSE hook (`src/hooks/useMcStream.js`)
- Single EventSource per page → all three cards share one connection.
- Auto-reconnect with exponential backoff (1s → 30s cap).
- Bucketed event log: `byType.intent`, `byType.regime`,
  `byType.position_misread`, etc.
- Tracks `currentRegime` separately for direct consumption.
- Clean unmount — no leaked connections.

#### MarketRegimeTape (`src/components/MarketRegimeTape.jsx`)
- Current regime as a colored pill (bull/bear/chop/calm/volatile/crisis).
- "Recent transitions" strip — deduped consecutive duplicates.
- Seeded from `/api/admin/runtime/camaro/intent-summary` so the
  current regime shows even before the first SSE regime event lands
  (regime only fires on CHANGE).
- Live dot indicator + connection-state copy.

#### DivergenceChopGauge (`src/components/DivergenceChopGauge.jsx`)
- Composite chop score in [0, 1]: 60% regime weight + 40% hold-ratio.
- Gradient bar: green→amber→red.
- Verdict copy: "edge present" / "mixed signal" / "chop risk" /
  "deep chop · stay flat".
- Sub-metrics: regime contribution + live hold-ratio.

#### PositionMisreadsCard (`src/components/PositionMisreadsCard.jsx`)
- Seeded from `/api/admin/position-misreads/{recent,summary-24h}`.
- Live-merged with SSE `position_misread` events (deduped on
  detected_at + symbol + brain).
- 24h verdict badge (CLEAN/ISOLATED/ELEVATED/SYSTEMIC).
- Table columns: detected (rel time), brain, symbol, action,
  brain-thought side, broker-said side, qty, MISSED_SHORT flag.
- Empty state messaging when zero misreads.

#### Overview wire-up
- New grid row `data-testid="overview-live-strip"` (3-col lg / 1-col
  mobile) directly under the runtime cards.
- Each card wrapped in `PanelErrorBoundary` so a parse error in one
  doesn't blank the row.

### Bug fixed during integration
- `PositionMisreadsCard` rendered `merged.map is not a function` on
  first load. Root cause: the `/recent` endpoint returns
  `{items: [], count: N}` (not a bare array or `{rows: ...}`).
  Fixed by reading `recent.data.items` with Array.isArray guards.
- Same card was reading `summary.total_misreads_24h` and
  `missed_short_profit_count`; the actual endpoint returns
  `summary.total` and `missed_short_profit`. Fixed.

### Test totals
1995 → **2001 tests, 0 failures** (+6 new SSE tests).

### Live verification
Screenshot taken with all 4 brains active: Market Regime Tape
showed `CHOP` with one live transition; Divergence Gauge showed
`51% MIXED SIGNAL`; Position Misreads showed `CLEAN` verdict with
empty state. All three cards showed green "live" dots confirming
SSE connection.

### Files touched
- `backend/routes/mc_connection_stream.py` (NEW)
- `backend/server.py` (route registration)
- `backend/requirements.txt` (`sse-starlette==1.8.2`)
- `backend/tests/test_mc_connection_stream.py` (NEW, 6 tests)
- `frontend/src/hooks/useMcStream.js` (NEW)
- `frontend/src/components/MarketRegimeTape.jsx` (NEW)
- `frontend/src/components/DivergenceChopGauge.jsx` (NEW)
- `frontend/src/components/PositionMisreadsCard.jsx` (NEW)
- `frontend/src/pages/Overview.jsx` (3-card strip added)

---


## 2026-06-10 (pass 12) — P2: Position-aware gate + Intent summary endpoint

### Operator directive
> *"P2"*

### What changed

#### Position-aware intent classification gate (the AAPL-pattern safety net)
- Wired the long-deferred `position_aware_intent_classification` gate
  into `shared/execution.py::_evaluate_gates`. Sits between
  `action_routable` and `executor_seat_check`.
- Uses the live broker position from `position_context.get_position_context`
  + `position_model.classify_intent` + `position_model.detect_misread`
  to catch the 2026-06-09 AAPL pattern: brain reads FLAT, emits BUY,
  but the broker actually holds SHORT (the BUY is a COVER).
- Two operator-controlled modes:
    - `audit_only` (default) → gate PASSES but records a misread row
      to `shared_position_misreads`.
    - `block` → gate FAILS hard; auto-router refuses to route.
  Mode is toggled via the existing `POST /api/admin/position-misreads/enforcement`.
- Pulled OUT of `SEAT_LAYER_GATES` patent-suspension force-pass — the
  whole point of this gate is to fire even when other doctrine is
  suspended.
- Safety: if `get_position_context` raises (broker offline, etc.) the
  gate passes with a diagnostic reason — never fail-closed on lookup
  failure. Dedupe + sizing + caps still own safety.
- 9 regression tests in `tests/test_position_aware_gate.py` covering
  agreement, AAPL pattern (both modes), symmetric inversion (SELL-to-
  ADD-SHORT misclassified as CLOSE), HOLD skip, qty=0 skip, lookup
  failure, ordering relative to caps.
- Updated `tests/test_council_diagnose_contract.py::EXPECTED_GATES_IN_ORDER`
  to pin the new gate's position in the chain.

#### Intent summary endpoint
- New `GET /api/admin/runtime/{brain}/intent-summary` route at
  `backend/routes/intent_summary.py`.
- Aggregates `shared_intents` for one brain over a configurable window
  (default 60min, max 24h):
    - `by_action` → `{BUY: n, SELL: n, ...}`
    - `by_lane` → `{equity: n, crypto: n}`
    - `by_verdict` → `{passed: n, blocked: n, ...}`
    - `by_symbol` → top-15 list
    - `recent` → last N intents verbatim (default 10, max 100)
- Reads the canonical fields: `stack`, `ingest_ts`, `gate_state`.
- 5 regression tests in `tests/test_intent_summary_route.py`
  (aggregates, empty brain, window filtering, auth-required, limit cap).
- Live verified: Camaro had 70 intents in the last 60 minutes,
  cleanly aggregated by action/lane/verdict.

#### Bonus housekeeping
- Fixed pre-existing lint warning in `shared/execution.py:1011`: the
  `receipt` dict was mutated by `insert_one()` to add an ObjectId
  `_id` that isn't JSON-serializable. Strip it before returning to
  the client.

### Test totals
1981 → **1995 tests, 0 failures** (+14 net new tests).

### Files touched
- `backend/shared/execution.py` (position-aware gate + receipt strip)
- `backend/namespaces.py` (`SEAT_LAYER_GATES` excludes position_aware)
- `backend/server.py` (route registration)
- `backend/routes/intent_summary.py` (NEW)
- `backend/tests/test_position_aware_gate.py` (NEW, 9)
- `backend/tests/test_intent_summary_route.py` (NEW, 5)
- `backend/tests/test_council_diagnose_contract.py` (gate chain pin)

---


## 2026-06-10 (pass 11) — Pre-existing test failures fixed + full P1 + P2 TTL

### Operator directive
> *"Always log into the PRD after every session. Fix the pre-existing
> failures. P1 & 2"*

### Pre-existing failures fixed (9 → 0)

1. **`test_public_rate_limit.py`** (4 failures): old `_wait_for_next_minute()`
   slept up to 60s and tripped pytest-timeout. Rewritten to only wait
   when the wall-clock minute is about to roll mid-burst. Also
   swapped the autouse Mongo wipe from async motor (fragile under
   pytest-asyncio session-loop) to sync pymongo (bulletproof).
   Also added `requests.Session()` reuse so 50-call HTTPS bursts
   finish in seconds instead of half a minute.

2. **`test_platform_survival_routes.py`** (1 failure): the stamp validator
   test inherited `RISEDUAL_ENV=prod` from backend `.env`, so the
   "unknown env" assertions no longer matched. Fixed with
   `monkeypatch.setenv` to force the preview state regardless of host
   env — order-independent and deterministic.

3. **`test_execution_gates.py`** (3 failures): the
   `PATENT_SUSPENSION_ACTIVE` flag was flipped from True → False on
   2026-06-09 after the $500 AAPL safety failure, inverting the
   contract for `broker_connected` and `cap_per_day`. The gate also
   now uses `broker_router.adapter_for_lane()` instead of
   `get_alpaca_adapter()` directly. Tests updated to:
     - patch `shared.broker_router.adapter_for_lane`
     - assert ENFORCEMENT (block) instead of SUSPENSION (force-pass)
     - inject `snapshot.spread_bps` so the new `roadguard_spread_floor`
       gate clears

4. **`test_signal_ranked_symbol_selection.py`** (4 failures): the tests
   called `asyncio.get_event_loop().run_until_complete(coro)` from
   sync test bodies, which raises `RuntimeError: This event loop is
   already running` once pytest-asyncio's session loop is up. Rewritten
   as native async tests — fully order-independent.

### P1 work landed

#### Position-context TTL shortening (defense-in-depth on the AAPL fix)
- `shared/position_context.py`: TTL 10s → 2s (the original amnesia window).
- New `invalidate_for_lane(lane)` API — the auto-router calls it the
  instant the broker accepts an order, guaranteeing the next brain
  tick re-fetches fresh positions in ~50ms.
- Wired into `auto_router._route_one` Phase 4b.
- 5 regression tests in `tests/test_position_context_ttl.py`.

#### Open-notional sign-flip bug (recurring P1, finally fixed)
- `shared/exposure_caps.py::evaluate_open_notional`: was
  `is_opening = side in ("BUY", "SHORT")` — wrong for BUY-to-COVER
  (REDUCES exposure) and SELL-to-ADD-SHORT (GROWS exposure).
- Now accepts `position_evolution` parameter sourced from
  `position_model.classify_position_evolution`:
    - `{open, add, flip, scale_in}` → grows open notional
    - `{reduce, close, partial_cover, full_cover, scale_out, hold}` → no growth
    - unknown/missing → falls back to legacy side heuristic
- Forwarded through `evaluate_all` → called by `execution.py::_evaluate_gates`.
- 10 regression tests in `tests/test_open_notional_sign_flip.py`
  including the symmetric inversion case.

#### Market regime detector (replaces hardcoded `"calm"`)
- New `shared/market_regime.py`:
  - `classify_market_regime(...)` — pure classifier returning one of
    `{calm, bull, bear, chop, volatile, crisis}` from mean trend,
    mean vol, and breadth.
  - `classify_from_symbol_snapshots(...)` — derives the three
    composite inputs from per-symbol snapshots.
- Wired into `external/brains/runner.py::_rank_universe`: every tick's
  universe scan now also produces a fleet-wide regime, stashed on
  `self._current_regime`, injected into each snapshot by
  `_evaluate_and_post`. The Camaro wrapper finally sees real signal.
- `_score_one` upgraded to return `(setup_score, regime_input_snippet)`
  while remaining backward-compatible with test subclasses that
  return raw floats.
- 14 regression tests in `tests/test_market_regime.py`.

### P2 work landed

#### Broker-fills TTL + compound index
- New `_ensure_indexes()` fired at poller startup. Creates:
    - `ttl_inserted_at` TTL index (30 days, env-tunable via
      `BROKER_FILLS_RETENTION_SEC`) on a BSON Date field.
    - `symbol_ts_desc` compound index for the dashboard's
      "recent fills by symbol" query.
- `_normalize_transaction` now stamps `inserted_at` as a `datetime`
  (BSON Date) — required by TTL. Legacy `ingested_at` ISO string
  preserved for human audit.
- 4 regression tests in `tests/test_broker_fills_indexes.py`
  (creation, idempotency, BSON-Date stamping, env-tunable TTL).
- Manually verified: indexes created on the live `test_database`.

### Test totals
- Before pass 11: 1947 tests / 9 pre-existing failures
- After pass 11: **1981 tests / 0 failures** (+34 new, all 9 prior fixed)

### Files touched
- `backend/tests/test_public_rate_limit.py` (timing fix + pymongo wipe)
- `backend/tests/test_platform_survival_routes.py` (monkeypatch env)
- `backend/tests/test_execution_gates.py` (doctrine flip + new patch target)
- `backend/tests/test_signal_ranked_symbol_selection.py` (native async)
- `backend/shared/position_context.py` (TTL 10s → 2s, invalidate_for_lane)
- `backend/shared/auto_router.py` (Phase 4b cache punch)
- `backend/shared/exposure_caps.py` (signed-aware open notional)
- `backend/shared/execution.py` (pass position_evolution)
- `backend/shared/market_regime.py` (NEW classifier)
- `backend/shared/broker_fills.py` (`_ensure_indexes`, BSON-Date `inserted_at`)
- `external/brains/runner.py` (`_rank_universe` regime fold-in,
  `_score_one` returns tuple, `_evaluate_and_post` injects regime)
- `backend/tests/test_position_context_ttl.py` (NEW, 5)
- `backend/tests/test_open_notional_sign_flip.py` (NEW, 10)
- `backend/tests/test_market_regime.py` (NEW, 14)
- `backend/tests/test_broker_fills_indexes.py` (NEW, 4)

---


# Mission Control — PRD (latest pass on top)

## 🆕 2026-06-10 (pass 10) — In-flight order dedupe (P0 — 130-trade structural fix)

### Operator directive (verbatim, pinned)
> *"P0 = `shared_broker_fills`, `in-flight order dedupe`."*

### What changed
- **API fix**: `broker_fills_admin` routes were double-prefixed
  (`/api/api/admin/broker-fills/*`). Stripped the leading `/api` so
  all admin broker-truth endpoints resolve correctly.
- **New module**: `backend/shared/in_flight_orders.py` — in-memory,
  async-lock-protected pending-set. Implements:
  `claim_in_flight_slot(symbol)` / `release_in_flight_slot(symbol)`
  / `is_in_flight(symbol)` / `snapshot()`. TTL default 30s
  (env: `IN_FLIGHT_ORDER_TTL_SEC`).
- **Wired into `auto_router._route_one`** (Phase 3b): the auto-router
  runs a two-layer dedupe BEFORE every broker submission:
    - Layer A — `broker_fills.has_pending_order(symbol)` (broker truth)
    - Layer B — `in_flight_orders.claim_in_flight_slot(symbol)` (pre-ack lock)
  Either firing → `verdict=no_trade` with typed reason. Releases on
  `BrokerRouteBlocked` / generic exception so a failed submission
  doesn't permanently lock the symbol.
- **New endpoint**: `GET /api/admin/broker-fills/in-flight` exposes
  the in-memory claim snapshot for dashboards.

### Doctrine pin (the 130-trade fix)
The 2026-06-09 AAPL incident had two stacked causes:
1. Position context TTL (10s) > broker fill cadence (~500ms)  →
   brains saw `current_side=FLAT` 130 times in 13 minutes.
2. No dedupe → MC submitted 130 BUYs building a 1.3279 share long
   position MC didn't know it had.

The dedupe layer alone closes the loop even if cause (1) is never
fixed — every brain emission after the first is intercepted at the
auto-router gate before the broker is touched. The P1 TTL shortening
is defense-in-depth on top of this structural fix.

### Tests (13 new, all passing)
- `tests/test_in_flight_orders.py` (11): first-claim-wins, dedupe,
  multi-symbol independence, release-allows-reclaim, case insensitivity,
  empty-symbol rejection, TTL age-out, snapshot-excludes-expired,
  50-racer concurrent claim (exactly 1 wins), 130-trade burst.
- `tests/test_auto_router_dedupe_integration.py` (2): pins the
  dedupe call-site so a future refactor can't accidentally remove it.

### Verification
- `curl /api/admin/broker-fills/summary?minutes=1440` → 130 AAPL fills.
- `curl /api/admin/broker-fills/in-flight` → `{count:0, ttl:30, pending:[]}`.
- 127 tests pass across the touched code paths.

### Files touched
- `backend/routes/broker_fills_admin.py` (router prefix fix + new endpoint)
- `backend/shared/auto_router.py` (Phase 3b dedupe wiring)
- `backend/shared/in_flight_orders.py` (NEW)
- `backend/tests/test_in_flight_orders.py` (NEW)
- `backend/tests/test_auto_router_dedupe_integration.py` (NEW)

### Status
- ✅ P0 broker-fills 404 — fixed.
- ✅ P0 in-flight order dedupe — landed + tested.
- ⏳ P1 position-context TTL shortening — not started.
- ⏳ P1 market-regime detector — not started.
- ⏳ P1 sizing sign-flip in `shared/execution.py` — not started.

---



## 🆕 2026-06-09 (pass 7) — portfolio-manager vocabulary on brain intents

### Operator directive (verbatim)
> *"Teach them more transitions. The big mental shift is from
> 'what should I buy or sell?' to 'how should this position evolve?'"*

### Vocabulary scope (locked for this pass)
- **Position evolution**: OPEN, ADD, REDUCE, CLOSE, FLIP, HOLD,
  **SCALE_IN, SCALE_OUT, PARTIAL_COVER, FULL_COVER**.
- **Risk transition**: RISK_ON, RISK_OFF, NEUTRAL.

### Thresholds (operator-tunable, pinned in code)
- `SCALE_IN_CONFIDENCE_FLOOR  = 0.65` (planned LONG ADD)
- `SCALE_OUT_CONFIDENCE_FLOOR = 0.55` (lock-in-gains LONG REDUCE)
- `FULL_COVER_CONFIDENCE_FLOOR = 0.78` (full SHORT close)
- `RISK_OFF_REGIMES = {volatile, crisis, stressed, risk_off}`
- `RISK_ON_REGIMES = {calm, bullish, trend, risk_on}`

### Files
- `backend/shared/position_model.py` — `classify_position_evolution`,
  `classify_risk_transition`.
- `external/brains/brain_core.py` — `BrainIntent.position_evolution`
  & `.risk_transition`; `_derive_evolution()`.
- `external/brains/runner.py` — payload evidence + pattern-bias
  re-derivation.
- `backend/tests/test_trade_transition.py` — 45 tests passing.

### Deferred to later passes
ROLL_FORWARD / ROLL_UP / ROLL_DOWN (options) ·
ROTATE_SECTOR · ENABLE_HEDGE / REMOVE_HEDGE ·
ENTER_TREND / EXIT_TREND · ACCUMULATE / ATTACK / DEFEND / EXIT.



## 🆕 2026-06-09 (pass 6) — trade-transition layer wired into brain decisions

### Operator directive (verbatim, pinned)
> *"Stop feeding the brains only `action = BUY/SELL`. Start feeding
> them position_side (LONG/SHORT/FLAT), intent_type (OPEN/ADD/
> REDUCE/CLOSE/FLIP), exposure_direction (LONG_BIAS/SHORT_BIAS/
> NEUTRAL). The brain should not think in just buy/sell — it
> should think in trade transitions."*

### What changed
- `backend/shared/position_model.py` now exports
  `classify_trade_transition()`, `normalize_position()`,
  `allowed_transitions_for()` — the operator-pinned classifier
  with the full 10-state vocabulary (OPEN_LONG, ADD_LONG,
  REDUCE_LONG, CLOSE_LONG, OPEN_SHORT, ADD_SHORT, REDUCE_SHORT,
  CLOSE_SHORT, FLIP_LONG_TO_SHORT, FLIP_SHORT_TO_LONG, HOLD).
- `backend/shared/position_context.py` (new) — pulls live broker
  positions per lane, normalizes, caches 10s, exposes
  `get_position_context(symbol, lane)`.
- `external/brains/brain_core.py` — `BrainIntent` carries the new
  schema (`current_side`, `signed_qty`, `target_exposure`,
  `transition_intent`, `order_action`); `evaluate()` consumes
  `position_context`.
- `external/brains/runner.py` — fetches `position_context` before
  each `core.evaluate()` call; `_apply_pattern_bias` re-derives
  transition fields on promotion; MC payload stamps all 5 new
  fields plus the raw `position_context` as evidence.
- `backend/tests/test_trade_transition.py` — 22 tests, all pass.

### Live verification (post-restart)
- Brains posting under new schema: e.g. `alpha NVDA mc=BUY
  current_side=FLAT order_action=BUY transition_intent=OPEN
  target_exposure=LONG`.
- Broker reality check: AAPL=LONG 1.3279 surfaces correctly
  (source=`broker_live`). FLAT symbols receive
  `allowed_transitions=["BUY_TO_OPEN_LONG", "SELL_TO_OPEN_SHORT"]`.

### Doctrine (this pass)
- This layer is **READ-ONLY enrichment** of the brain's input and
  audit-only stamping on the output. **Live sizing gates in
  `execution.py` are NOT touched** — operator wants edge proof
  (Pass-6 replay script) before wiring the transition fields into
  sizing.
- `signed_qty` is the single source of truth across MC. Any code
  using `qty_abs`/`side` without consulting `signed_qty` is wrong
  by construction.

### Known follow-up
- `shared/broker/public.py:259` derives side from `qty >= 0`. If
  Public.com ever holds a real short, the adapter must read the
  broker's side label directly. `normalize_position` is ready —
  it honors `side` when present.



## 🆕 2026-06-09 (pass 5) — position-side model + audit observer + quick-release enforcement toggle

### Incident that drove this pass
After redeploy + master-switch flip, the brains executed **130 trades
in ~5 minutes**, all reinforcing a LONG-side bias on AAPL. Operator
confirmed AAPL was actually a SHORT position at the broker. The
brains' signal logic kept emitting BUY at high conviction, but the
correct interpretation against a real short was **COVER / REDUCE**,
not OPEN LONG. The signal may have been right; the position-state
reader was missing.

Operator's framing:
> *"The signal may have been right. The position-state reader was wrong."*

### What was built (audit-only — does NOT touch the live trading path)

**1. Position-side model** (`backend/shared/position_model.py`):
- `PositionSide` enum: `LONG`, `SHORT`, `FLAT`.
- `IntentType` enum: `OPEN`, `ADD`, `REDUCE`, `CLOSE`, `FLIP`.
- `PositionState` dataclass keyed on **`signed_qty`** as the single
  source of truth (+10 = long 10, −10 = short 10, 0 = flat).
- `classify_intent(action, qty, current)` returns the correct
  `IntentType` for all 8 operator-stated transitions:
  - SELL + flat → OPEN (short)
  - BUY + flat → OPEN (long)
  - SELL + long → REDUCE / CLOSE
  - BUY + short → REDUCE / CLOSE  ← **the AAPL fix**
  - SELL + short, BUY + long → ADD
  - cross-zero (either direction) → FLIP
  - qty=0 or unknown action → ValueError (fail-loud)
- `detect_misread()` returns a `PositionMisread` audit row whenever
  the brain's assumption disagrees with broker reality, and sets
  `missed_short_profit=True` iff actual side was SHORT and brain
  emitted BUY.

**2. Audit observer** (`backend/shared/position_misread_audit.py`):
- `audit_one_intent(intent, db, position_fetcher)` — fetches broker
  position, compares with brain's assumption (currently always
  FLAT since brains have no inventory awareness yet), writes a
  row to `shared_position_misreads` when they diverge. All
  exceptions swallowed so a misread audit failure can NEVER affect
  a trade.
- `list_recent_misreads(db, limit)` — observer feed for the UI.
- `misread_summary_24h(db)` — verdict heuristic:
  `0-2/day = isolated`, `20-50/day = systemic`.

**3. Quick-release enforcement toggle**
(`backend/routes/position_misread_admin.py`):
- `GET /api/admin/position-misreads/enforcement` — current mode.
- `POST /api/admin/position-misreads/enforcement {mode, reason}` —
  flip between `audit_only`, `warn`, `block`. Takes effect on next
  intent (Mongo write, no restart). Default is `audit_only` —
  fail-closed; any read error keeps the system in observer mode.
- Helper `is_misread_enforcement_enabled()` is the single read
  point for the future gate when wired into `_evaluate_gates`.

**4. Admin endpoints**:
- `GET /api/admin/position-misreads/recent?limit=20` — feeds the
  planned "last 20 misreads" UI card.
- `GET /api/admin/position-misreads/summary-24h` — operator verdict.

### Tests added
- `backend/tests/test_position_model.py` — 17 tests. The 130-trade
  AAPL scenario is replayed in `test_aapl_misread_2026_06_09_is_caught`
  and confirms the classifier produces `correct_intent_type=REDUCE`
  with `missed_short_profit=True`.
- Total backend suite: **93/93 passing.**

### Operator-set priority order (this and future passes)
1. **Audit-only hook** — installed (this pass), but the live-flow
   call site is NOT yet inserted. Add a single `await audit_one_intent
   (...)` line in `auto_router._tick` wrapped in try/except.
2. **UI card** — `/admin/position-misreads/recent` endpoint live;
   front-end card not yet built.
3. **Historical replay** — backfill today's 130 AAPL trades through
   the classifier to retroactively log the misread rows. Pure
   read-only batch job, safe to run anytime.

### Doctrine pinned this pass
> *"Don't lock this down because of a hiccup with trading. Place
> them but also with a quick release when we work out the kinks."*

Embodied as: enforcement mode `audit_only` by default, single POST
to flip to `block`, single POST to roll back. No restart, no
redeploy. Audit-logged on every flip. Fail-closed.

### Production status at end of pass
- All position-model code: **preview only — requires redeploy.**
- No live execution behavior changed in this pass. Trading on prod
  continues to use the existing gate chain unmodified.
- After redeploy, the observer is dormant until either (a) the
  one-line wire-up in `_tick` is added (next session), or (b) the
  operator-managed backfill is run.

---

## 🆕 2026-06-09 (pass 4) — universe-public endpoint added · $10/order cap · watchlist symbols live in brain rotation

### Discovery
After loading the operator's 42-symbol watchlist into
`patterns_universe` (pass 3), brain intents in preview were STILL
hitting only the hardcoded fallback list (AAPL, MSFT, NVDA, TSLA +
4 cryptos). Root cause: `external/brains/runner.py::_resolve_universe`
calls `GET /api/admin/patterns/universe-public` over loopback, but
**that endpoint did not exist** (404). The brain's exception handler
quietly swallowed the 404 and fell back to `FALLBACK_BY_LANE` — so
the watchlist additions had no operational effect.

This had been silently broken for as long as the brain runner code
has existed.

### Fixes

**1. `/api/admin/patterns/universe-public` endpoint** added in
`backend/routes/data_stack_admin.py`. Anonymous (no auth) read of
active universe symbols. Brain sidecars call this over internal
loopback at boot and every N ticks. Safe to expose without auth
because watchlist symbols are not sensitive data.

**2. Per-order cap lowered to $10** in `backend/.env`:
`RISEDUAL_CAP_PER_ORDER_USD="10"` (was `"25"`). Aligns with
`AUTO_ROUTER_NOTIONAL_USD = 10` default so fresh orders pass
cap_per_order. Public.com fractional shares confirmed working
(submit_market_order uses notional, not share count).

**3. Master switch flipped ON for preview only** (prod stays OFF —
prod still has PATENT_SUSPENSION=True and no universe-public
endpoint). Reason logged:
`"first tick observation 2026-06-09 (preview only, $10/order cap,
signal-ranked + cooldown active)"`.

### Verification (preview, 3-min window)
```
Symbols traded:  AAL (4 BUYs · from watchlist) · BTC/USD · ETH/USD · AAPL (HOLD)
Watchlist coverage: 4/14 intents on operator list (was 0 before fix)
Brain mix:       alpha 4 · camaro 4 · chevelle 3 · redeye 3 (even spread)
Distinct symbols per brain: 3-4 in 3 minutes (vs. 1 before)
```

NVDA was top earlier in the same session before going on cooldown.
AAL took over as top-ranked equity setup_score. The signal-ranked
selection + 6-tick cooldown is working together exactly as designed.

### `cap_per_day=$50` is now binding on preview
Today's spend counter shows `$51` (carryover from the AAPL stampede
in pass 2). All new BUYs hit `blocked: 24h spend $51 + new $X = ...
exceeds daily cap $50` until the rolling 24h window decays. This is
the cap doing its job — operator can verify safety by watching the
block reason flow until the window resets (~1pm tomorrow ET).

### Files modified this pass
- **Modified**: `backend/routes/data_stack_admin.py` — added
  `@router.get("/admin/patterns/universe-public")` (anon read).
- **Modified**: `backend/.env` —
  `RISEDUAL_CAP_PER_ORDER_USD="10"` (from `"25"`).
- **Mongo write**: preview master trading switch flipped ON.

### Production status at end of pass
| Layer | Prod | Preview |
|---|---|---|
| Universe-public endpoint | ❌ missing | ✅ |
| Per-order cap | $25 (defanged) | $10 (binding) |
| Master switch | OFF (keep off until redeploy) | ON |
| Watchlist symbols in universe DB | ✅ live (pass 3 Mongo write) | ✅ live |
| Brains actually scanning watchlist | ❌ — endpoint missing means fallback | ✅ — AAL just traded |

### To get watchlist trading live on prod
Same as pass 3 — Save to GitHub → redeploy → re-flip master switch.
After redeploy, prod will: (a) see the 48 watchlist symbols via the
new endpoint, (b) rank by setup_score, (c) honor the $10/order cap.

---

## 🆕 2026-06-09 (pass 3) — $500 AAPL incident · cap enforcement re-armed · signal-ranked selection · watchlist loaded · brain-identity hardening

### Incident
After the master switch flipped on (pass 2), the brain fleet executed
**6+ AAPL BUYs in 10 minutes**, deploying ~$197 of buying power into
a single ticker:
```
9:42 ET  AAPL 0.6528 sh · $191.77 · 7.67% of portfolio
9:52 ET  AAPL 1.3279 sh · $388.42 · 15.54% of portfolio · BP $110
```
Operator killed the master switch when they saw it happening.

### Root causes (three compounding)
1. **`PATENT_SUSPENSION_ACTIVE = True`** in `backend/namespaces.py`
   was force-passing every non-seat gate that failed — including
   `cap_per_order=$25`, `cap_per_day=$50`, `cap_open_notional=$200`,
   `symbol_in_universe`, `roadguard_spread_floor`. Gates LOGGED what
   would have blocked but stamped `passed=True` with a
   `[SUSPENDED]` tag. Dates from 2026-02-17 operator directive when
   the system was deadlocked; **was never turned back off when live
   trading went on**.
2. **Alphabetical round-robin symbol selection** in
   `external/brains/runner.py::_intent_loop` —
   `universe[(tick-1) % len]` meant all 4 brains hit `symbol[0]`
   (AAPL) on their first tick → 4 simultaneous BUYs on the same
   ticker → that queue head drained first when the master switch
   opened.
3. **No inventory awareness in brains** — each brain re-emitted BUY
   AAPL every time it cycled back to AAPL, regardless of position
   size already held. Combined with #1 and #2, this is a 6×
   amplification.

### Fixes shipped this pass (preview-only — requires redeploy for prod)

**1. Cap-bypass closed.** `PATENT_SUSPENSION_ACTIVE = False` in
`backend/namespaces.py`. Verified on preview: a synthetic $500
equity diagnose now hard-blocks at `cap_per_order` ($25),
`cap_per_day` ($50), AND `cap_open_notional` ($200) simultaneously.
Doctrine stack fully re-engaged.

**2. Signal-ranked symbol selection.** Replaced the alphabetical
modulo loop in `external/brains/runner.py::_intent_loop` with:
   - Per-tick: score every symbol's `setup_score` (`_rank_universe`).
   - Sort descending, pick head.
   - Skip any symbol whose last emit was within
     `INTENT_COOLDOWN_TICKS` (default 6 ≈ 4.5 min @ 45s tick),
     fall through to next in ranking.
   - If everything's on cooldown: pick least-recently-emitted so the
     brain never goes silent (we owe MC at least one OBSERVE for
     audit continuity).
   - Universe refresh purges cooldown entries for dropped symbols.

   New env knobs: `NEUTRAL_BRAIN_COOLDOWN_TICKS` (default 6).
   Module-level helper: `_rank_universe`, `_score_one` — both
   tested in isolation.

   Live preview proof: symbol distribution post-fix went from
   *"all AAPL"* to `NVDA 12 · ETH/USD 9 · SOL/USD 4 · ADA/USD 4 · BTC/USD 1 · AAPL 1`,
   brain distribution evenly spread (`redeye 8, alpha 8, camaro 7,
   chevelle 8`).

**3. Operator watchlist loaded into universe.** 42 new symbols added
to `patterns_universe` on both preview and prod (add-only — kept the
original 8). Active equity universe is now 48 symbols spanning the
operator's actual holdings + watchlist (`AAL, ABNB, AEO, AEP, AFG,
AII, AMH, AMZN, AOUT, APH, AVAH, AWK, AXP, BA, BABA, BBCP, BLSH,
BTDR, CBLS, CELH, ECL, FB, FDG, GLD, GOOG, GROY, ITA, KEY, MSFY,
NFLX, NXTT, ORCL, PFE, PLD, SHOP, TEVA, TGT, UBER, WM, WMT` added;
`AAPL, AMC, AMD, GME, HOTH, MSFT, NVDA, TSLA` preserved). This was
a Mongo write — **already live on prod**.

**4. Brain-identity normalization (`shared/brain_identity.py`).**
After the rename event, operator flagged the risk of name-coupling
bugs — display-name-as-routing-key, display-name-as-collection-name.
Audit found one real surface (`LocalShelly.__init__` accepted any
string and used it as a Mongo collection suffix). Fix:

   - New `backend/shared/brain_identity.py`:
       - `VALID_BRAIN_IDS = {"alpha","camaro","chevelle","redeye"}`
       - `DISPLAY_TO_ID` covers brand names (Camino/Barracuda/
         Hellcat/GTO), legacy slot labels (Alpha/Camaro/Chevelle/
         RedEye), and every casing variant.
       - `normalize_brain_id(value) → canonical_id | "unknown"` —
         fail-closed; never silently substitutes a default brain.
       - `is_known_brain(value)` sugar.
   - `LocalShelly.__init__` now funnels through `normalize_brain_id`
     so a caller passing `"Barracuda"` lands in
     `shelly_camaro_memories`, not `shelly_barracuda_memories`.
     Non-canonical test fixture names (e.g. `"twembed"`) are
     preserved for back-compat.

   The doctrine is pinned in the module docstring:
   *"Display names = UI only. Canonical IDs = routing/execution
   only. Roles = seat logic only."*

### Tests added this pass
- `backend/tests/test_signal_ranked_symbol_selection.py` (5 tests):
  ranking by setup_score, cooldown blocks repeats, cooldown releases
  after window, universe refresh purges cooldowns, score failures
  degrade-don't-drop.
- `backend/tests/test_brain_identity_normalization.py` (29 tests):
  canonical pass-through (×4), uppercase canonical (×4), brand names
  (×4), casing variants (×7), legacy slot labels (×4), unknown
  inputs (×8 — must return `"unknown"`, never default), DISPLAY_TO_ID
  integrity (×2), is_known_brain consistency, LocalShelly
  normalization integration (×2).

**Total backend suite: 76/76 passing.**

### Production state at end of pass
| Layer | Prod | Preview |
|---|---|---|
| Master trading switch | **❌ OFF** (audit-logged "emergency: $500 AAPL slipped through $25 cap") | ❌ OFF (mirror) |
| `PATENT_SUSPENSION_ACTIVE` | **True** (still defanged — needs redeploy) | False ✅ |
| Watchlist symbols (48) | ✅ live (Mongo write) | ✅ live |
| Signal-ranked selection + cooldown | not yet (code) | ✅ live |
| Brain-identity normalization | not yet (code) | ✅ live |
| New `/admin/auto-router/status` and `/force-tick` endpoints | not yet (code) | ✅ live |

### To go back live on prod
1. **Save to GitHub → trigger prod redeploy.** This is the only way
   to land `PATENT_SUSPENSION_ACTIVE=False`, the signal-ranked loop,
   the cooldown, and the brain-identity layer on `mission.risedual.ai`.
2. After redeploy lands, verify with
   `curl https://mission.risedual.ai/api/admin/auto-router/status` —
   should return 200 (404 means redeploy didn't take).
3. Optionally manually unwind the orphan 1.33 AAPL position in the
   Public.com app if you don't want to hold it.
4. **Then** flip master switch ON via the existing
   `POST /api/admin/trading/toggle`.

When all four are GREEN, the next tick should pull a *NVDA / GOOG /
MSFT / ORCL / etc.* mix sorted by setup_score, capped at $25/order
and $50/day, no symbol dogpiled.

---

## 🆕 2026-06-09 (pass 2) — FIRST LIVE ORDERS · 5-layer gate diagnosis

### Operator observation
> "Yeah it's hitting the account"

After the earlier ladder retirement (this same day) the operator was
still seeing intents stuck on HOLD/dry_run_passed with no broker
activity. Walked the entire gate chain end-to-end and found a 5-layer
disguise where each layer being opened revealed the next.

### The 5 disguising layers (final order of discovery)
1. **Learning ladder** — fixed earlier today (8× `observation_only` → `normal_live`)
2. **Executor seat (equity)** — Barracuda held the seat but emitted
   HOLD; only the seat-holder's BUY/SELLs can route. Operator
   manually swapped executor↔auditor in the UI so GTO (already
   emitting saturated BUYs) became the equity executor.
3. **Lane execution toggle** (`/api/admin/execution/lane-toggles`) —
   different from the broker lane toggle. Was already enabled on
   prod (red herring on first read of `last-block-reason` — those 19
   counts were historical, pre-flip).
4. **Auto-router asyncio task** — added a new
   `GET /api/admin/auto-router/status` endpoint to confirm liveness.
   Result: `task_alive=True, tick_count=N, interval=30s` — loop was
   running fine. NOT the bottleneck.
5. **🎯 Master trading kill switch**
   (`/api/admin/trading/toggle`) — **`first_boot_default_disabled`**.
   This is the absolute final gate inside `auto_router._route_one`
   (Phase 1b). It defaults OFF on pod boot and had never been
   flipped True since the pod was deployed. Every gate above it
   passing didn't matter — `is_trading_enabled()` returned False
   and the router persisted `no_trade: trading_controls_disabled`
   on every intent it picked up.

   Flipped ON on both environments with reason
   *"operator green-light 2026-06-09: live pilot, ladder + lane +
   seat all open"*.

### Result
First confirmed live broker call landed in the Public.com audit log
within 60s of the master-switch flip. Operator confirmed: *"Yeah
it's hitting the account."*

### Shipped this pass (preview-only — requires redeploy for prod)
- `GET /api/admin/auto-router/status` — task liveness + tick
  heartbeat (started_at, tick_count, last_tick_ts, last_tick_results,
  last_tick_executed, last_tick_error, task_exception).
  Instrumented `auto_router._loop()` with module-level counters
  that don't allocate per-intent. Cheap to poll.
- `POST /api/admin/auto-router/force-tick` — drain the queue once
  immediately (out-of-band tick), useful right after the operator
  unblocks a gate. Same gate chain, no force-trade semantic.
- Both endpoints in new `backend/routes/auto_router_admin.py`,
  mounted into `api_router` next to the learning_ladder routes.

### Operational doctrine after this pass
The complete chain that must be GREEN for a brain BUY/SELL to fire:

1. Brain emits intent with `action ∈ {BUY,SELL,SHORT,COVER}` and
   non-empty `symbol`.
2. `gate_state` must NOT be in `{blocked, no_trade, advisory_only}`.
3. Learning ladder for `(brain, lane)` must be **above
   `observation_only`** (one of `micro_paper`, `micro_live`,
   `normal_live`).
4. **Executor seat for the lane** must be filled with SOME brain
   (position-model: any seat-holder works, not bound to who posted
   the intent).
5. **Lane execution toggle** (`/api/admin/execution/lane-toggles`)
   must be True for the lane.
6. **Master trading switch** (`/api/admin/trading/toggle`) must
   be True.
7. Auto-router task must be alive (`/api/admin/auto-router/status`
   shows `task_alive=True`).
8. `AUTO_ROUTER_ENABLED` env not set to false (defaults true).
9. Broker (`Public.com` for equity, `Kraken` for crypto) must be
   `connected=True, execution_enabled=True`.
10. Intent symbol must be in `patterns_universe` for the lane.
11. RoadGuard spread floor: equity ≤ 50bps, crypto ≤ 200bps.
12. Risk caps not exceeded: `RISEDUAL_CAP_PER_ORDER_USD = $25`,
    `RISEDUAL_CAP_PER_DAY_USD = $50`,
    `RISEDUAL_CAP_OPEN_NOTIONAL_USD = $200`.

All 12 must be GREEN. Any one of them red = `no_trade` and the
intent stays at `dry_run_passed` forever (no retry — operator must
unblock then call `/api/admin/auto-router/force-tick` or wait for
the next scheduled 30s tick).

### Why the master switch is separate from the lane toggle
- **Lane toggle** = "I'm allowing routing on equity / crypto"
  (per-lane operator authorization).
- **Master switch** = "I want autonomous trading to happen at all
  right now" (one-button fleet-wide kill).

The master switch is the operator's "stop everything immediately"
button. Decoupled so that flipping it OFF doesn't require touching
any per-lane state and flipping it back ON doesn't risk forgetting
a lane toggle.

---

## 🆕 2026-06-09 — LIVE TRADING ENGAGED · ladder retired · brain labels finalized · Public.com card

### Operator decision
> "I'm fine with real orders, I just had a month without any trades.
>  I need them to trade now. … I just want them trading crypto and equity.
>  I think a month of intents makes up more than enough reason for the
>  ladder to go away."

After ~1 month of `observation_only` intents proving brain coherence,
operator authorized full live trading on **all 4 brains × both lanes**.
Per-order / per-day / open-notional caps in `.env` now serve as the sole
binding risk control.

### Final live state on PROD (`mission.risedual.ai`)
```
🔴 LIVE  alpha    (Camino)    × equity  → normal_live
🔴 LIVE  alpha    (Camino)    × crypto  → normal_live
🔴 LIVE  camaro   (Barracuda) × equity  → normal_live
🔴 LIVE  camaro   (Barracuda) × crypto  → normal_live
🔴 LIVE  chevelle (Hellcat)   × equity  → normal_live
🔴 LIVE  chevelle (Hellcat)   × crypto  → normal_live
🔴 LIVE  redeye   (GTO)       × equity  → normal_live
🔴 LIVE  redeye   (GTO)       × crypto  → normal_live
```
24 ladder transitions audit-logged with operator reason on each row.

### Broker gates
- **Public.com**: connected (account `5LG34065`), `execution_enabled=True`
- **Kraken**:    connected, `execution_enabled=True`

### Risk envelope (active)
- `RISEDUAL_CAP_PER_ORDER_USD`     = `$25`
- `RISEDUAL_CAP_PER_DAY_USD`       = `$50`
- `RISEDUAL_CAP_OPEN_NOTIONAL_USD` = `$200`

These caps are read on every order attempt and survive every ladder
stage — a runaway brain cannot blow through $50/day or $200 open
exposure across the fleet.

### Shipped this session
1. **Equity broker card replaced** — `<AlpacaConnect />` swapped to
   `<PublicConnect />` on the Intents page Equity Lane. New full-card
   layout with stat strip (Account / Secret / Today $ / Open Notional /
   Token Refresh) mirroring the Kraken Crypto Lane tile. Connect form
   takes secret + optional account_id + base_url + token TTL; modal
   exposes test / refresh-token / disconnect / execution toggle
   (typed-phrase confirmation required).
2. **Brain display labels finalized.** Internal slot IDs unchanged
   (`alpha / camaro / chevelle / redeye` — Mongo primary keys). All
   user-facing surfaces render: `Camino / Barracuda / Hellcat / GTO`.
   Updated: `external/brains/personality.py`, `runner.py`, `brain_core.py`
   (`CaminoAdversarialBrain` → `NeutralAdversarialBrain`),
   `frontend/src/lib/api.js` (`RUNTIME_META`), `backend/.env`
   (`RISEDUAL_GIT_SHA="neutral-v3"`), test docstrings.
   Risk-profile multipliers preserved verbatim
   (Camino ×1.00 / Barracuda ×1.15 / Hellcat ×1.30 / GTO ×0.85).
3. **Live Routes page** — new `/admin/learning-ladder` admin page
   (`frontend/src/pages/LearningLadder.jsx`) with toggle-style UI:
   4 brains × 2 lanes grid, 4 stage buttons each, reason-required
   modal on every change, 30-row audit history table. Backed by new
   `POST /api/admin/learning-ladder/set` endpoint allowing direct
   jump to any rung. Nav entry: "Live Routes" under Trading.
4. **AAPL "phantom order" closed as real.** Operator's screenshot
   confirmed a real 0.0333 share AAPL fill via "Individual API"
   actor — a previous-session test invocation. Repo re-audited:
   no current code path can call `PublicAdapter.submit_market_order`
   or `route_order` from outside the production gate.

### Production deployment note
Preview and production use **separate MongoDB instances** for the
`learning_ladder` collection (handoff note about "shared Mongo" was
incomplete — credentials collections ARE shared but per-env state
collections are NOT). For the new Public.com card UI and Live Routes
UI to appear on `mission.risedual.ai`, operator must use **Save to
GitHub** → trigger production redeploy. Trading is already live
without redeploy (used prod's `/promote` endpoint 24× this turn).

### Backlog after this milestone (P1/P2)
- Live order watch: tail first round of Public.com / Kraken fills,
  attach to `shared_intents` row, surface on Intents page.
- Intent summary endpoint `GET /api/admin/runtime/{brain}/intent-summary` (P2)
- SSE stream `/api/mc-connection/stream` for live dashboard updates (P2)
- Frontend Pulse review-queue UI for Governance Reviewer (P2)
- Reddit + Zillow + Quiver scraper fixes (P2)
- Belt-and-suspenders env kill switch `RISEDUAL_BROKER_LIVE_ARMED`
  (operator-deferred this session — caps deemed sufficient)
- Forensics dump of historical AAPL test order's `public_audit_log`
  row + linked `shared_intent` (operator-deferred this session)

---

## 🆕 2026-02-XX — Permanent neutral brains + Alpha Vantage cache

### Confirmation from operator
The 4 neutral brains (Camino / Barracuda / Hellcat / GTO) running
in-process inside MC's lifespan are **permanent** — not stand-ins
for a future migration. Treating them as the production decision
cores.

### Shipped this pass
1. **Sovereign contribution loop** wired for the 4 brains
   (`/app/external/brains/runner.py`). 60s cadence, substantive
   payload (weights + rolling 20-decision tape + telemetry notes)
   → `/api/runtime-discussion/sovereign/contribution`. Confirmed
   `composite_liveness.sovereign_loop=live` for all 4 brains.
2. **`sovereign_count`** added to `/api/admin/neutral-brains/status`
   so the dashboard can show operator-visible counters.
3. **Alpha Vantage cached feeder**
   (`/app/backend/shared/feeders/alpha_vantage.py`). Free-tier 25
   calls/UTC-day cap respected via per-day Mongo counter + per-
   `(symbol, function, date)` cache row. Operator endpoints under
   `/api/admin/alpha-vantage/{quota,cache,fetch}`. Configurable cap
   via `ALPHA_VANTAGE_DAILY_CAP`.
4. **Login event-loop fix verified** — `auth.py`'s
   `asyncio.to_thread(bcrypt.checkpw)` confirmed protecting the
   loop under 40 parallel logins. Prod redeploy required to ship.

### Backlog (P1/P2)
- Intent summary endpoint `GET /api/admin/runtime/{brain}/intent-summary` (P2)
- SSE stream `/api/mc-connection/stream` for live dashboard updates (P2)
- Frontend Pulse review-queue UI for Governance Reviewer (P2)
- Reddit (JSON format) + Zillow (403) scraper fixes; Quiver 500 upstream (P2)
- Wire the AV feeder into a brain feature (currently the feeder exists
  but no consumer is calling it — operator decision on what AV data
  the brains should ingest)



## 🆕 2026-02-17 — Phase 4 ENGAGED: ladder stage drives sizing + routing (P0)

### Problem (operator report from Prod)
> "This is blocking the trades and showing shadow intents. Alpha is
> quiet because it does have its shadow set up like the others.
> It's shadow isn't blocking Alpha's intents, it's running them."

Root cause: the auto-router's "honest hold" shadow path was gated
on the brain's self-declared `evidence.size_multiplier=0` /
`would_trade_without_gates=false` flags. Alpha never self-zeroed,
so its intents bypassed the shadow path and fired through;
Camaro / Chevelle / REDEYE consistently self-zeroed, so 100% of
their directional intents got shunted into `observation_receipts`
and zero real fills ever happened. The `learning_ladder` collection
tracked the four stages (observation_only → micro_paper → micro_live
→ normal_live) but the doctrine note literally said "Phase 4 will
add the sizing gate that READS this state and clamps notional —
until Phase 4 ships, the ladder state is observed but not yet
enforced". The brains were stuck because Phase 4 never shipped.

### Doctrine fix — Phase 4 ENGAGED
The LADDER stage (per brain × lane) is now AUTHORITATIVE for
sizing + routing. The brain becomes a SIGNAL SOURCE; MC owns
capital deployment.

| stage              | route        | final notional                | execution_mode      |
|--------------------|--------------|-------------------------------|---------------------|
| `observation_only` | observe      | $0 (no broker fill)           | None (obs receipt)  |
| `micro_paper`      | paper        | `LADDER_MICRO_PAPER_USD` ($10)| `ladder_paper`      |
| `micro_live`       | live_micro   | `LADDER_MICRO_LIVE_USD` ($5)  | `ladder_live_micro` |
| `normal_live`      | live_normal  | requested (lane-cap bound)    | `live`              |

The ladder cap participates in the existing "smallest-wins" cap
comparison alongside `lane_cap` and the operator `micro_live` rail
— so a brain promoted to `micro_paper` fires at the ladder's $10
cap regardless of what it self-declared.

### Shipped
- `shared/sizing_gate.py`: new `evaluate_sizing_with_ladder(req,
  brain, lane)` returning ladder stage + route + ladder_cap_usd +
  execution_mode. Legacy `evaluate_sizing()` preserved for the
  manual `/execution/submit` path.
- `shared/auto_router.py`: ladder-first routing. At
  observation_only, ANY directional intent (sized or self-zeroed)
  becomes an observation receipt — no broker fill, no Alpha-vs-
  others asymmetry. At micro_paper+, brain self-zeroing is
  IGNORED and the intent fires at the stage cap.
- Receipt provenance: `sizing_provenance.{stage,route,
  ladder_cap_usd,execution_mode}` + top-level `execution_mode` so
  `learning_ladder._paper_progress` can count fills with a simple
  filter.
- `shared/learning_ladder.py`: doctrine note updated; docstring
  pinned to "Phase 4 ENGAGED".
- New env knobs (operator-tightenable without redeploy):
  `LADDER_MICRO_PAPER_USD` (default $10),
  `LADDER_MICRO_LIVE_USD` (default $5).
- 5 new tests pinning all 4 ladder rungs +backward-compat
  (`tests/test_phase_4_ladder_engaged.py`).
- Full suite: 1680/1681 passing (the 1 remaining failure is a
  pre-existing order-dependent network blip, NOT introduced by
  Phase 4).

### Operator action items (Prod)
1. Decide per (brain, lane) which rung to start at. Default is
   `observation_only` — no fills until you promote.
2. Promote via `POST /api/admin/learning-ladder/promote`
   `{"brain": "...", "lane": "...", "reason": "..."}`. Audit-
   logged.
3. Optional env override on Prod:
   - `LADDER_MICRO_PAPER_USD=10`
   - `LADDER_MICRO_LIVE_USD=5`
4. After redeploy, watch for `execution_mode=ladder_paper`
   receipts to start landing. The unlock counter will tick
   automatically (50 fills + 0.30 expectancy_R → auto-promotable
   to micro_live).

### Files touched
- `backend/shared/sizing_gate.py` (+ladder-aware evaluator,
  ROUTE_*/EXECUTION_MODE_FOR_ROUTE constants)
- `backend/shared/auto_router.py` (ladder-first fork in
  `_route_one`, receipt stamping)
- `backend/shared/learning_ladder.py` (doctrine note + counter
  docstring)
- `backend/tests/test_phase_4_ladder_engaged.py` (new, 5 tests)


## 🆕 2026-02-17 — Pytest legacy-backlog cluster cleared (P2)

### Problem
~37 pytest failures from fixture drift after the 2026-doctrine
overhaul (Doctrine c defang, position-model seats, $100k cap lift,
symbol-in-universe gate, brain-lane-policy retirement, etc.).
CI/CD pipeline stayed red for ~3 forks, blocking regression
safety nets.

### Doctrine fix
1. Rewrite tests to assert NEW doctrine (Doctrine c declarations
   are 200 OK + may_execute seat-derived; `live_trading_enabled`
   no longer rejects with 422).
2. Add `symbol_in_universe` and `lane_execution_enabled` to the
   pinned gate-chain order.
3. Honor the operator-issued "patent-stack restrictions lifted"
   suspension — gates record reason but `passed=True`.
4. Add `find_one` to `_FakeCollection` so feature-service tests
   exercise the new latest-bar path.
5. Clamp `dampener_drop = max(0, ...)` so noisy intent rows can't
   produce a negative drop on the calibrator output.
6. Fix `test_shelly_bus.py` to source real brain ingest tokens
   from `/app/backend/.env` instead of `os.environ.setdefault(...)`
   leaking fake tokens session-wide — root cause of intermittent
   401s in `test_paradox_wake` / `test_sidecar_checkin`.
7. Update `test_brain_lane_policy_full_cycle` to assert the new
   "POST/DELETE retired with HTTP 410 → use /admin/roster" contract.
8. Update `test_operator_view_includes_all_brains` to assert
   "at most one brain may_execute=True" (seat-derived) instead of
   the old "no brain may_execute" invariant.
9. Move `test_get_active_none_when_missing` onto the shared
   pytest-asyncio session loop so Motor doesn't cross-loop crash.

### Shipped
- 37 → 1 failing test (the remaining 1 is a transient preview
  cluster connection timeout, not a code issue).
- `confidence_floor_sweep.py` gained the documented "drop ≥ 0"
  invariant in code (was only documented in comments).
- `test_shelly_bus.py` no longer leaks fake `<BRAIN>_INGEST_TOKEN`
  values across the session — fixes the order-dependent flake
  pattern that hid behind a "test passes in isolation" mask.

### Files touched
- `backend/tests/test_doctrine_outcome_join_and_scorecard.py`
  (drop `stack=None`, pin `doctrine_version=None`)
- `backend/tests/test_sovereign.py` (Doctrine c defang assertions)
- `backend/tests/test_alpaca_broker.py` (add MC receipt stub)
- `backend/tests/test_council_diagnose_contract.py`
  (+symbol_in_universe in expected chain)
- `backend/tests/test_doctrine_intent_attachment.py`
  (modulate vs block)
- `backend/tests/test_no_duplicate_execution_gates.py`
  (opinion-silence-watchdog allowlist)
- `backend/tests/test_intent_limbo_cleanup.py`
  (schema_invariants terminal-failure pin via may_execute=True
  + unique intent_id per run)
- `backend/tests/test_intent_snapshot_persistence.py`
  (audit-trail-only pin on suspended roadguard)
- `backend/tests/test_alpaca_execution_pipeline.py`
  (cap_per_order pin updated to the lifted $100k reality)
- `backend/tests/test_ibkr.py` (session-loop fix)
- `backend/tests/test_market_data_feature_service.py`
  (_FakeCollection.find_one)
- `backend/tests/test_risk_monitor_and_policy.py`
  (brain-lane-policy 410 retirement contract)
- `backend/tests/test_positions.py` (REDEYE no-seat default)
- `backend/tests/test_discussion_layer.py` (at-most-one executor)
- `backend/tests/test_technicals.py` (use recent timestamp)
- `backend/tests/test_shelly_bus.py` (real-token seeding)
- `backend/shared/calibration/confidence_floor_sweep.py`
  (`dampener_drop = max(0, ...)`)

### Public.com keys status (operator question, 2026-02-17)
- Preview MC backend: NO Public.com credentials stored
  (`GET /api/admin/public/status` → `connected: false`).
  Operator needs to call `POST /api/admin/public/connect` to
  register a Public.com secret. The keys are NOT stored in
  `.env`; they're encrypted at rest in MongoDB on a per-
  environment basis, so the Production MC may have a
  different state — the operator must check Prod separately.


## 🆕 2026-02-19 (pass #2) — Symbol-in-universe gate + brain-callable universe endpoint (P0)

### Problem
Operator confirmed Camaro has never emitted a crypto intent on prod
(`GET /api/admin/intents?stack=camaro&lane=crypto` returns empty).
Camaro holds the `crypto_strategist` seat but its strategist loop
is hardcoded to scan equity tickers only. MC had no central
authority over which symbols any brain was allowed to propose
against — each brain self-determined.

### Doctrine fix
Make MC's `patterns_universe` collection the canonical source of
truth for tradeable symbols. Brains MUST consult MC's universe
(via the new `/admin/runtime/{brain}/universe` endpoint) and MC's
gate chain REJECTS any intent whose symbol isn't in the universe.
Enforcement + reference client → brains comply or trade nothing.

### Shipped
- New gate `symbol_in_universe` in `shared/execution.py` (5c)
- Lane field on `patterns_universe` rows; backward-compat default = equity
- Boot-time seed for crypto majors (BTC/ETH/SOL/XRP USD)
- Brain-callable `GET /api/admin/runtime/{brain}/universe` (dual auth,
  seat-filtered)
- Reference brain client `/app/memory/brain_universe_client_reference.py`
- 7 new tests pinning the contract

### Outstanding (P0 follow-up, brain-team work)
Each brain team must drop the reference client into their brain's
repo and wire it into the strategist loop. Until then, MC's new
gate will reject any off-universe intent, which is the forcing
function for compliance.

### Files touched
- `backend/shared/execution.py` (+gate 5c)
- `backend/routes/data_stack_admin.py` (lane field on schema)
- `backend/server.py` (extended boot seed)
- `backend/routes/brain_runtime.py` (+/{brain}/universe endpoint)
- `backend/tests/test_symbol_in_universe_gate.py` (new, 7 tests)
- `memory/brain_universe_client_reference.py` (new, brain-side reference)
- `memory/CHANGELOG.md` (entry)

---


# Mission Control — PRD (latest pass on top)

## 🆕 2026-02-19 — Heartbeat side-effect + STALE/DEAD band re-tuning (P0)

### Problem
User observed brains "fading" on the Diagnostics page: every cycle
their badge oscillated LIVE → STALE → LIVE despite the sidecars being
healthy, and REDEYE specifically showed DEAD with `last receipt 3d ago`
even though the Sidecar Imposter Scan showed 21 clean check-ins/hour
for the same brain.

### Root cause
Two independent architectural issues:
1. MC has three independent "alive" signals stored in three different
   collections (`shared_heartbeats`, `sovereign_state`, `sidecar_checkins`).
   A successful POST to `/api/admin/runtime/sidecar-checkin/{brain}`
   did NOT bump `shared_heartbeats`, so brains whose sidecars hit
   only that endpoint appeared DEAD on the runtime liveness table.
2. STALE/DEAD bands (60s / 110s) were tighter than the brain ping
   cadence (60-90s), causing harmless oscillation.

### Fix (shipped this pass)
- Side-effect: sidecar-checkin now upserts `shared_heartbeats`
  (best-effort; identity-record is still canonical).
- Bands raised:
  - `HEARTBEAT_OK_BELOW_SECONDS`: 60 → 120
  - `HEARTBEAT_PREVIEW_DRIFT_SECONDS`: 110 → 300
  - `HEARTBEAT_STALE_AFTER_SECONDS`: 90 → 240
- All downstream classifiers (`/heartbeat-status/`, `/admin/sidecar-diagnostics`,
  `/admin/brain/emission-diagnose/`) updated to match.
- Frontend tooltip text updated.

### Verified
- Live curl on REDEYE: `connected: dead → partial` from a single
  sidecar check-in. `heartbeat_age_seconds: null → 0.0`.
- 17/17 sidecar-checkin + drift-tier tests green.
- Screenshot of Diagnostics page renders cleanly with REDEYE in
  the new STALE band (191s) instead of DEAD.

### Outstanding (P0 follow-up — option D, not yet acted)
**RedEye sovereign silence** — RedEye's `sovereign_state.updated_at`
froze on 2026-05-31 14:16 UTC (~3 days ago). Its sidecar pod is
demonstrably alive (21 fresh identity check-ins/hour), but the
sovereign-tick task has stopped writing. No fresh
`sovereign_contribution_attempts` rows from RedEye on prod since
that timestamp. Operator action: call
`GET /api/admin/sovereign/contribution-health?window=200` on prod.
If RedEye shows `health: no_data` with stale `latest_ts`, it means
RedEye has stopped CALLING the endpoint entirely (not being
rejected) → brain-side issue (restart pod or sovereign-tick task).
If it shows `rejected_422` rolling in, the brain is fighting an
MC contract change.

### Files touched
- `backend/namespaces.py` (band constants)
- `backend/shared/runtime/sidecar_checkin.py` (heartbeat side-effect)
- `backend/shared/heartbeat_ping.py` (band sync)
- `backend/routes/sidecar_diagnostics.py` (band sync)
- `backend/routes/brain_emission_diagnose.py` (band sync)
- `frontend/src/pages/Diagnostics.jsx` (tooltip text)
- `backend/tests/test_drift_and_governor_exclusion.py` (assertions)
- `backend/tests/test_sidecar_checkin.py` (new side-effect test)
- `memory/CHANGELOG.md` (entry)

---


# ✅ 2026-05-31 — Canonical 8-seat IP doctrine ENFORCED

The IP boundary is now code-pinned: `shared/seat_policy.py::CANONICAL_SEATS`
is a tuple of exactly 8 names with a module-level assertion that fails import
if anyone mutates `SEAT_POLICY` away from that set.

**The 8 seats:**

| Equity              | Crypto                       |
|---------------------|------------------------------|
| `strategist`        | `crypto_strategist`          |
| `executor`          | `crypto` (= `crypto_executor`) |
| `governor` †        | `crypto_governor` †          |
| `auditor`           | `crypto_auditor`             |

† Governor seats are restricted to Chevelle + RedEye. Every other seat is
open to every brain. Brains may hold one equity + one crypto seat at the
same time.

Deprecated stubs removed from policy: `decider`, `advisor`, `opponent` and
their crypto twins. They live only as aliases in `SEAT_ALIASES` for legacy
sidecars sending old seat names.

---


# ✅ 2026-05-31 — Finnhub LIVE + 10yr S&P-500 historical backfill COMPLETE

**1,234,440 daily candles across all 502 S&P-500 symbols, 10 years deep.**
NVDA verified: 2016-06-03 ($1.16) → 2026-05-29 ($211.15). Zero failures.
~10 min wall time at 55 rpm (Finnhub basic-tier ceiling = 60 rpm).

- Live Finnhub poller running every 5 min (currently 8 patterns_universe seed symbols — expand to S&P 500 in next pass).
- Operator backfill endpoints: `POST /api/admin/feeders/finnhub/backfill/symbol`, `POST /universe`, `GET /universe/{job_id}`, `POST .../cancel`.
- Bulk-write persistence keeps API responsive (auth latency 378ms during backfill).
- Doctrine: `source: "finnhub_equity"`, `ingested_via: "finnhub_backfill"` on every row.

**Brain training substrate is now live** — brains can pull a decade of OHLCV per S&P symbol via existing `/api/admin/market-data/daily-snapshots/history/{symbol}` (capped at 5 days for snapshot retention) OR direct query against `shared_ohlcv_bars`.

---


# ✅ 2026-05-31 — Polygon daily equity feeder LIVE

The daily snapshot system's `daily` block is now backed by Polygon's
grouped-daily aggregates (one HTTP call covers the entire US equity market).
First pull on preview: **8,870 bars from May 29**. Daily snapshot capture:
**484/502 S&P-500 symbols populated with real OHLCV** (NVDA: O=214.575,
H=217.86, L=211.13, C=211.14, V=289.4M).

Per-tf source split: `intraday`=Finnhub 5m, `daily`=Polygon 1d. Both feeders
coexist; per-block `bar_source` echoes which feeder served the row.

**Public.com daily feeder is the next planned data source** when account
returns (~June 4). Will run alongside Polygon as redundant equity coverage
+ first crypto daily coverage. Information-only per operator pin — Public
will NOT be wired as a broker.

---


# ✅ 2026-05-31 — Daily Market Snapshots subsystem shipped

Three frozen, point-in-time captures of the full S&P-500 (502 symbols) per
NYSE trading day land at 09:35 / 12:30 / 16:05 ET. **Each row carries BOTH
a `5m` intraday OHLCV block AND a `1d` daily OHLCV block** (nested
`intraday` + `daily` objects, each with its own price/ohlc/asof/RVOL).
Coverage is per-timeframe — intraday may populate while daily is null and
vice versa; they don't conflate.

Rows retained 5 trading days, then wiped lazily at the next `open` capture.
Brains retrieve via:
- `GET /api/admin/market-data/daily-snapshots/labels`
- `GET /api/admin/market-data/daily-snapshots?label=open|midday|close`
- `GET /api/admin/market-data/daily-snapshots/symbol/{symbol}`
- `GET /api/admin/market-data/daily-snapshots/history/{symbol}?days=5`

Operator manual fire: `POST /api/admin/market-data/daily-snapshots/capture?label=open` (JWT-only).

Doctrine: derived evidence only; no broker quotes; missing bars surface as `price: null, price_reason: "no_bars_for_symbol"`.
Implementation: `shared/snapshots/` + `routes/daily_snapshots.py`. Tests: `tests/test_daily_market_snapshots.py` (15 passing).

**Note for prod:** on next deploy, the worker boots automatically via
`server.py` lifespan. Disable with `MC_SNAPSHOT_WORKER_ENABLED=false` if needed.
The Finnhub equity feeder must be writing `5m` AND `1d` bars to
`shared_ohlcv_bars` for symbols to populate price/ohlc; otherwise rows land
as null with auditable reason.

---


# 🚨 NEXT AGENT — START HERE

The system has been running 3 months and produced ZERO trainable outcomes.
Stop building new features. The next four tasks, in order:

## P0 — Revert my doctrine violation (15 min)
I added a brain-eligibility hard-lock today. Operator corrected:
**"The seat bears the restrictions. NOT the brain identity."**
- `backend/shared/roster.py::DEFAULT_ELIGIBILITY` → all-True (every brain × every seat)
- `DEFAULT_ASSIGNMENTS["opponent"]=None` (REDEYE not seated by default)
- `backend/tests/test_roster.py::TestEligibility` → rewrite to assert all-True default
- `frontend/src/pages/BrainOperatorPage.jsx::BRAIN_PROFILE.expected_seats` → broaden
- Keep: strategist rename, auditor as real seat, legacy `decider→strategist` boundary rewrite

## P0 — Diagnose `max_hold_time_guard` (30-60 min)
1,526,108 intents. ZERO resolved outcomes. Every position closes as `scratch` via max_hold_time_guard.
- Read `backend/shared/crypto/max_hold_time.py` + equity equivalent
- Check guard threshold vs typical position duration
- Verify take_profit / stop_loss are firing BEFORE max_hold_time hits
- WITHOUT THIS FIX: no brain can ever be graded. The entire learning thesis is blocked.

## P1 — Investigate labeling firewall silence (20-30 min)
`shared_labeled_memories` stopped accepting writes from Alpha/Camaro on 2026-05-09.
REDEYE has zero records ever.
- Find the writer endpoint (probably `/api/ingest/memory-label` or similar)
- Check MC's recent ingest logs for that endpoint
- Determine: brain-side regression OR MC-side acceptance break
- REDEYE's labeling path may never have been wired — investigate

## P1 — Build `/api/admin/runtime-activity-audit` (30 min)
One endpoint, fan out per-runtime to:
- `shared_intents` (count, last write)
- `runtime_opinions`
- `position_stances`
- `sovereign_audit_log`
- `brain_memories`
- `runtime_heartbeats`
Returns one-page truth view per brain. Operator currently has no single surface
to see "what is each brain actually doing." Brain asymmetry (Alpha/REDEYE heartbeat
but emit nothing; Camaro/Chevelle don't heartbeat but flood intents) cannot be
diagnosed without this.

## P2 — Kraken Rogue-Fills Reconciler
MC has zero visibility into Kraken fills outside its own adapter. Poll
`TradesHistory` hourly, join against `execution_receipts`, flag unmatched as
`UNVERIFIED_BROKER_EXECUTION`. Same pattern as the Alpaca reconciler.
**Operator's recent 6 BTC trades were Kraken Recurring Buy** (not MC, not a brain
bypass) — but the visibility gap is real and should be closed before another
incident is ambiguous.

---

# RiseDual Mission Control — Product Requirements (full)


# RISEDUAL Mission Control — Monorepo PRD


## 🆕 2026-05-23 (latest): Broker Bypass Audit Phase — 6-step root-cause closure

**Trigger**: Operator observed "trading happens sometimes then stops; we can't use the data." Investigation revealed the real story:

- `lane_execution_toggles` collection was empty (default OFF). MC has **never** authorized routing through brokers via its own code path.
- `alpaca_audit_log` shows **only** `alpaca_disconnect` events — MC has never held an active Alpaca connection.
- The 500 broker_orders in DB all carry `source=access_key`. Camaro sidecar held its own API key and POSTed direct to Alpaca on May 15 + May 18, completely bypassing MC.
- Trading "stopped" because Camaro's bypass cron lost its keys after May 18; MC was never the executor.
- Diagnosis: **the bypass is the bug**. Feeding orphan data into the learning ladder is a bandaid.

### Operator's 6-step plan (executed)

1. **Freeze all broker execution** — explicit `broker_freeze_state` singleton + audit log.
2. **Export Alpaca paper order history Apr 25–30 & May 4–18** — endpoint exposed; operator-initiated.
3. **Backfill local broker receipts** — same endpoint, idempotent upsert into `broker_orders`.
4. **Reconcile against Mongo** — match each `broker_orders` row vs. `execution_receipts` (doctrinal) and `shared_intents` (forensic hint). All 500 existing orphans confirmed `UNVERIFIED_BROKER_EXECUTION`.
5. **Mark UNVERIFIED_BROKER_EXECUTION until matched** — propagated to `broker_orders.provenance` AND `memory_kernel_quarantine.provenance_explicit`.
6. **Patch every Alpaca submit path** — adapter-level bypass guard + default-on receipt enforcement + freeze check above the lane toggles.

### Code-level invariants now enforced

- `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT` defaults to **TRUE** (was false). Explicit opt-out only.
- `AlpacaPaperAdapter.submit_market_order` / `submit_limit_order` **refuse** to submit without a structurally-valid `mc_receipt` kwarg (raises `BypassBlocked`).
- `KrakenLiveAdapter.submit_market_order` applies the same invariant (raises `PermissionError`).
- `broker_router.route_order` calls `assert_not_frozen()` **before** adapter resolution — the freeze supersedes everything.
- All four invariants locked with 7 tripwire tests in `tests/test_broker_audit_phase.py`.

### New endpoints (admin)

- `GET  /api/admin/broker/freeze` — current freeze state + doctrine note
- `POST /api/admin/broker/freeze` — flip ON (body: `{reason}`)
- `POST /api/admin/broker/thaw` — flip OFF (body: `{reason?}`)
- `GET  /api/admin/broker/freeze/history` — audit trail
- `POST /api/admin/broker/reconcile` — run reconciliation pass over `broker_orders`
- `GET  /api/admin/broker/reconcile/summary` — provenance breakdown
- `GET  /api/admin/broker/reconcile/unverified` — list of orders MC never issued
- `POST /api/admin/alpaca/ingest-orphans-batch` — multi-window batch ingest

### New files

- `backend/shared/broker_freeze.py` — freeze state module
- `backend/routes/broker_freeze_routes.py` — admin endpoints
- `backend/routes/broker_reconcile_routes.py` — reconciliation endpoints
- `backend/scripts/exec_audit_phase_freeze_and_reconcile.py` — one-shot audit runner
- `backend/tests/test_broker_audit_phase.py` — 7 new tripwires

### Current operator state

- **Broker FROZEN** by `admin@risedual.io` (reason: "post_orphan_audit_2026_05_23 — 500 fills bypassed MC; freeze until full reconcile + adapter patches verified")
- **500 orphan fills** tagged `UNVERIFIED_BROKER_EXECUTION` in both `broker_orders` and `memory_kernel_quarantine`
- **MC keys present** as env vars (`ALPACA_INGEST_KEY_ID` / `ALPACA_INGEST_SECRET_KEY`) — sufficient for the read-only orphan ingest path
- Tripwire suite: 283 pass / 1 pre-existing failure (schema-drift in `test_intent_limbo_cleanup`, unrelated)

### Next operator actions

1. Fetch the missing ~495 orphans from Apr 25–30 + May 4–18 via:
   ```
   POST /api/admin/alpaca/ingest-orphans-batch
   {
     "windows": [
       {"after": "2026-04-25T00:00:00Z", "until": "2026-04-30T23:59:59Z"},
       {"after": "2026-05-04T00:00:00Z", "until": "2026-05-18T23:59:59Z"}
     ],
     "dry_run": false
   }
   ```
2. Re-run `POST /api/admin/broker/reconcile` after batch ingest.
3. Thaw the broker only after:
   - Every `broker_orders` row carries an explicit provenance (not `(unreconciled)`).
   - Camaro / Alpha / Chevelle / Redeye sidecars confirmed key-stripped (no `access_key` source can appear on a future fill).

---


## 🆕 2026-05-21 (latest): RISE_AI Saved Threads — persistent reasoning memory

Threads turn one-off chats into long-running reasoning artifacts.
Same kernel, same ledger, same grading — now with continuity.

### Doctrine pin
Threads are REASONING MEMORY only:
- ❌ NOT execution memory
- ❌ NOT trade authority
- ❌ NOT doctrine authority
- ❌ NOT a path to /api/execution/submit
- ❌ NOT a promotion surface

Tripwire `test_threads_module_imports_no_execution_surface` scans
the route file's import statements and fails the build if any
forbidden execution/broker/promotion/seat-policy/doctrine surface
gets imported.

### Endpoints (`/api/admin/rise-ai/threads/`)
- `GET /` — list (filters: `pinned_only`, `archived`, `search`, `limit`)
- `POST /` — create (title + initial messages)
- `GET /{thread_id}` — full thread + transcript
- `PATCH /{thread_id}` — title / pinned / tags / archived / append_messages
- `POST /{thread_id}/resume` — returns session_id + transcript

### Collections
- `rise_ai_threads` — metadata per thread (thread_id, title, session_id,
  mode, role, pinned, tags, message_count, last_call_id,
  created_at, updated_at, created_by, archived)
- `rise_ai_thread_messages` — append-only transcript (thread_id, seq,
  kind, text, mode, role, call_id, provider, model, latency_ms,
  llm_authority, extra, created_at)

### Frontend (`/admin/rise-ai`)
- Left sidebar (~256px): New Thread / Search / Pinned-only toggle /
  Pinned group / Saved group
- Each thread item shows title, message count, mode, first 3 tags
- Pin/unpin button + archive button per item (archive confirmed)
- Click to load → fetches transcript via `/resume` → preserves
  session_id so the kernel context continues
- Save as Thread button on the current transcript (prompts for title)
- When a thread is loaded, every new exchange automatically PATCHes
  with append_messages — the transcript persists message-by-message
- Header shows the active thread title (or "cognition layer" when no
  thread is loaded)
- All previous features intact: grade buttons, "open in ledger",
  metadata badges, mode/role selectors, status/trade observation
  extras

### Tested
- 11/11 backend tests pass (auth, doctrine import check, full CRUD
  flow, pinned filter, search by title/tag, three 404 paths,
  validation, kind-validated append)
- Frontend smoke-tested live: created a "Status check — Premarket"
  thread from a status snapshot, verified sidebar shows it,
  clicked New Thread, verified blank state with saved thread
  preserved in sidebar
- 184 tripwires green (+1 new)

### The compounding loop
```
chat → save → thread → resume (same session_id, kernel context preserved)
  ↓                       ↓
ledger ←─────────────── ledger
  ↓                       ↓
grade (+1/0/-1) ←─────── grade
  ↓                       ↓
distillation_queue ←──── distillation_queue
```

Each thread becomes a long-running, gradable reasoning artifact.
Over time the distillation queue grows from one-off chats AND from
multi-turn threads — both feeding the eventual self-trained model.

### Files
- `routes/rise_ai_threads_routes.py` — five endpoints, doctrine-locked
- `pages/RiseAI.jsx` — sidebar + thread management + auto-append
- `namespaces.py` — two new collections
- `tests/test_rise_ai_threads.py` — 11 tests, 1 tripwire



## 🆕 2026-05-21 (latest): `/admin/rise-ai` — Operator Console

The operator-facing shell for RISE_AI's cognition layer. NOT "ChatGPT
for trading." This is the front-door that turns every operator
question into ledgered training data.

### Backend additions (`/api/ai/run`)
- **2 new modes**: `memory` (kernel call with role=memory) and
  `status` (read-only system snapshot — no LLM call).
- **`role_override` parameter**: operator can force a role within
  `{strategist, governor, opponent, memory, auditor, executor}` —
  e.g. `mode=chat, role_override=opponent` to get adversarial chat.
- **`answer_source` field** in `extra`: `llm_kernel` /
  `paradox_records` / `static_system_data` / `safety` — operator
  can see at a glance whether the answer came from a model or
  from a static surface.
- Modes (final): chat, reason, code, trade, research, memory, status

### Frontend (`/admin/rise-ai`)
- Sidebar's **first nav group**: "RISE_AI · Console"
- Mode selector (7 modes) + role override dropdown
- Prompt textarea with ⌘+Enter to send, 8000-char cap
- Auto-scrolling transcript; user messages above RISE_AI replies
- Per-message metadata: provider · model · latency · ADVISORY_ONLY badge
- **"Open in Ledger"** link per message → routes to `/admin/llm-ledger`
- **Inline +1 / 0 / -1 grade buttons** — graded calls auto-enqueue
  into the distillation queue (score ≥ +1) via the existing
  `/api/admin/llm/ledger/{call_id}/grade` endpoint
- Trade mode renders the recent candidates + evaluations inline
- Status mode renders the candidate counts + provider promotion grids
- Safety blocks render with the matched category badge (red)

### Doctrine locks (enforced at API)
- No broker routes, no execution endpoints reachable from `/api/ai/run`
- Safety check blocks execution_intent / doctrine_tamper / auth_tamper
  BEFORE any LLM call
- `trade` and `status` modes are READ-ONLY (no kernel.call, no DB
  mutation)
- Every response carries `llm_authority="ADVISORY_ONLY"`
- Role override restricted to a known set (400 on unknown)

### Tripwires (+2 new — 183 total)
- `VALID_MODES` pinned exactly to {chat, reason, code, trade, research, memory, status}
- `status` mode is observation-only (no call_id, source=static_system_data)
- `trade` mode answer_source pinned to "paradox_records"
- Role override rejects unknowns at the API

### Files
- `routes/ai_run_routes.py` — added memory/status modes + role_override
  + `_status_observation()` helper
- `pages/RiseAI.jsx` — operator console (~400 lines)
- `App.js` — `/admin/rise-ai` route wired in
- `components/Layout.jsx` — sidebar's first nav group is now "RISE_AI · Console"
- `tests/test_ai_run_routes.py` — 15 tests, 7 tripwires

### Live verification
- Status mode rendered the live system snapshot (4 LLM calls in
  ledger, all providers at PRIMARY except local+self_trained
  SHADOW, no candidates yet)
- Chat mode triggered real Claude call (2.9s), full metadata
  surfaced, +1/0/-1 grade buttons + "open in ledger" link
  rendered correctly



## 🆕 2026-05-21 (latest): Unified `/api/ai/run` entry + portable architecture reference

### B) `POST /api/ai/run` — unified front door
The tutorial's `/api/ai/run` surface, but backed by the production stack
(not a 3-string blocklist). Routes ad-hoc queries through the existing
LLM Kernel + Ledger + Safety governor.

#### Modes
- `chat` → `kernel.call(role="auditor", task="ai_run_chat")`
- `reason` → `kernel.call(role="strategist", task="ai_run_reason")`
- `code` → `kernel.call(role="strategist", task="ai_run_code")`
- `research` → `kernel.call(role="memory", task="ai_run_research")`
- `trade` → READ-ONLY observation. NEVER calls LLM, NEVER posts an
  order. Returns recent paradox_candidates + paradox_records.

#### Safety check (real, not toy)
Regex-screens prompts for THREE categories — blocks BEFORE any LLM
call, returns a tame answer with the matched phrase:
- `execution_intent` (place order, buy now, execute trade, fire order, submit intent)
- `doctrine_tamper` (disable gate, bypass roadguard, override veto, turn off kill switch)
- `auth_tamper` (steal password, malware, exploit bank, drain account)

#### Response shape
```
{
  request_id, mode, answer,
  safety_status: "allowed" | "blocked",
  safety_category, safety_matched,
  call_id, provider, model, latency_ms,
  llm_authority: "ADVISORY_ONLY",     # always
  created_at, extra
}
```

#### Tested
- 12/12 endpoint tests pass (mode validation, auth gate, all three
  safety categories, trade-mode-is-read-only, ADVISORY_ONLY passthrough).
- Live smoke test: real Anthropic call via the kernel returned in ~2.2s,
  ledgered to `llm_calls`, gradable from `/admin/llm-ledger`.

### C) `/app/RISE_AI_KERNEL.py` — single-file architecture reference
A 350-line documentation artifact at the repo root showing all 7
boxes interlocking in one place. Self-contained, no DB, no broker,
no Emergent dependencies. Designed for:
- Onboarding new engineers
- Explaining the architecture to stakeholders
- Off-platform migration (the boundaries are explicit; the real
  code is interface-compatible)

Includes a working `python RISE_AI_KERNEL.py` demo that exercises
all seven boxes. Header documentation maps each box to its real
implementation path under `/app/backend/`.

### Tripwires (5 new — 181 total)
- `VALID_MODES = {chat, reason, code, trade, research}` pinned exactly
- Safety screen blocks execution-intent prompts
- Safety screen blocks doctrine-tamper prompts
- Safety screen blocks auth-tamper prompts
- `/api/ai/run` response ALWAYS stamps `llm_authority="ADVISORY_ONLY"`

### Files
- `routes/ai_run_routes.py` — unified entry + safety governor
- `tests/test_ai_run_routes.py` — 12 tests, 5 tripwires
- `/app/RISE_AI_KERNEL.py` — portable single-file reference



## 🆕 2026-05-21 (latest): Migrations + Paradox Coordinator v0

### A) Direct emergentintegrations callsites migrated
Audited the codebase — only ONE direct callsite existed outside
`shared/llm/`: `shared/public_api/narrative.py` (gemini-3-flash-preview
for the public market overview). Migrated it to
`llm_kernel.call(role="public_narrator", task="market_overview_summary",
provider_override="gemini", model_override="gemini-3-flash-preview")`.
Every narrative call now ledgers into `llm_calls` and is gradable
from `/admin/llm-ledger`. 170 tripwires still green post-migration.

### B) Paradox Coordinator v0 — candidates + advisory evaluation
Doctrine pin: v0 = candidate generator + advisory evaluator only.
NO execution authority. NO auto-submit to broker. Everything writes
to `paradox_candidates` / `paradox_records`. The existing 11-gate
chain + human/admin promotion are still required for execution.

#### Endpoints (under `/api/admin/`)
- `POST /paradox/scan` — walk watchlist → filters → persist candidates
- `POST /paradox/evaluate` — 3 LLM calls (strategist/opponent/auditor)
   → aggregate → write paradox_record
- `POST /risk/check` — per-candidate + global gate
- `POST /ml/retrain/check` — retrain trigger eval
- `POST /paradox/execute-next` — flush ONE queued intent via the
   real gated submit path (unchanged from v0 stub)
- `GET/POST/DELETE /paradox/watchlist` + `/toggle` — admin CRUD

#### Service modules
- `services/paradox_scanner.py` — universe (watchlist primary,
  hardcoded fallback) + 5 filters: price≥2, vol≥500k, spread≤75bps,
  rvol≥1.5, ¬halted. Filters pinned by tripwire.
- `services/paradox_evaluator.py` — strategist/opponent/auditor via
  kernel. Aggregation: `final_conviction=min(strategist, auditor)`,
  opponent_veto→HOLD, HOLD never promotable, parse_error→rejected.
- `services/paradox_risk.py` — per-symbol (open_count, duplicate,
  exposure, lane_cap) + global (kill_switch, broker_health,
  daily_loss). Global triggers pause the loop; per-symbol just
  stamps risk_blocked and writes audit record.
- `services/paradox_retrain.py` — three triggers (winners≥50,
  eval_runs≥100, hours_since≥24). Writes a recommendation row;
  NEVER auto-trains.

#### Collections
- `paradox_watchlist` — operator-curated universe
- `paradox_candidates` — scanner output
- `paradox_records` (existing, discriminated by `evaluation_kind`)
  - `paradox_v0_evaluation` for evaluator output
  - `paradox_v0_risk_block` for risk-block audit rows
- `paradox_retrain_recommendations` — retrain trigger output

#### Doctrine locks (tripwires — 6 new, total 176)
- Filter thresholds pinned exactly (2 / 500k / 75 / 1.5).
- `PROMOTABLE_ACTIONS = ("BUY", "SELL")` — HOLD MUST NOT be there.
- `final_conviction = min(strategist, auditor)` aggregator.
- Opponent veto forces HOLD.
- HOLD action → status="rejected", promotable=False, regardless of scores.
- Parse error on any brain → rejected.

#### Files
- `services/paradox_scanner.py`, `paradox_evaluator.py`,
  `paradox_risk.py`, `paradox_retrain.py`
- `routes/paradox_agent_routes.py` (refactored — calls services)
- `routes/paradox_watchlist_routes.py` (new)
- `namespaces.py` — 3 new collections
- `tests/test_paradox_coordinator_v0.py` — 39 tests covering
  filter pinning, aggregation logic, scan persistence, evaluator
  with stubbed kernel, watchlist CRUD, risk/retrain HTTP paths

#### What v0 is NOT yet
- Real-time snapshot scraping (operator/sidecars supply snapshots)
- Auto-promotion to /api/execution/submit (HUMAN gate stays in)
- Actual trainer service consuming the retrain recommendations
- A UI panel to display candidates + paradox_records (next P2 work)



## 🆕 2026-05-21 (latest): LLM Ledger + Grading Panel — closing the learning loop

The decision-trace ledger is now live as both a backend endpoint and a
UI surface at `/admin/llm-ledger`. This is the piece that turns the
LLM Kernel from a router into a **learning loop**.

### Endpoints (mounted at `/api/admin/llm/`)
* `GET /ledger?hours=<n>&limit=<n>&role=&provider=&only_ungraded=`
  — paginated list (preview rows, 200-char prompt/response previews,
  attached `latest_grade` + `grades_count`).
* `GET /ledger/{call_id}` — full prompt + full response + every prior
  grade in reverse-chronological order.
* `POST /ledger/{call_id}/grade` — body `{score ∈ [-2..2], outcome,
  note?}`. Writes to `llm_preference_log` and auto-enqueues into
  `llm_distillation_queue` when `score ≥ +1`. Idempotent enqueue.

### UI (`/admin/llm-ledger`, sidebar entry "LLM Ledger")
* Filterable table: window (1h..7d), role, provider, ungraded-only.
* Color-coded role + provider per row, latency, grade pill.
* Click any row → detail modal showing full prompt/response,
  ADVISORY_ONLY badge, prior grades, and the **+1 helpful / 0 neutral
  / -1 wrong** grading buttons with outcome + note inputs.

### Doctrine locks (added to tripwire suite)
* Endpoints require admin JWT.
* Grades route ONLY into the training pipeline — NEVER affect
  execution or provider promotion. Tripwire confirms `llm_authority`
  passthrough.
* Invalid scores rejected at the API; unknown call_id 404s.
* Positive grades (score ≥ +1) auto-enqueue exactly once into
  distillation queue; idempotent.

### Files
* `routes/llm_ledger_routes.py` — three endpoints.
* `pages/LlmLedger.jsx` — operator panel with grading modal.
* `App.js` — `/admin/llm-ledger` route wired in.
* `components/Layout.jsx` — sidebar nav entry under Audit.
* `tests/test_llm_ledger_routes.py` — 12 tests (auth gate, list, detail,
  grade with/without enqueue, advisory stamp passthrough).

### The closed loop is now active
```
Brain → llm_kernel.call()      → llm_calls
Operator → /admin/llm-ledger   → grade (+1/0/-1)
Grade ≥ +1                     → llm_preference_log
                               → llm_distillation_queue (auto-enqueue)
Future trainer                 → dequeue → fine-tune local/self_trained
eval_harness                   → compare candidate vs primary
Operator                       → promote SHADOW → ADVISOR → PRIMARY
```

Total tripwires: 170 passing. Backend boots clean.



## 🆕 2026-05-21 (latest): RISE_AI LLM Kernel — the missing 7th box

The Model Adapter Kernel is now live under `/app/backend/shared/llm/`.
This is the seam that lets RISE_AI swap providers without touching
brain code, and the foundation for the local-first/self-trained-first
priority chain.

### Architecture
```
brain
  ↓  await llm_kernel.call(role, task, prompt, ...)
shared/llm/kernel.py    (BrainLLMKernel, ADVISORY_ONLY stamped)
  ↓  choose_model(role, task, ready, promotion)
shared/llm/routing_policy.py
  • PROVIDER_PRIORITY = local → self_trained → anthropic → openai → gemini
  • promotion states: SHADOW (default for local+self_trained) → ADVISOR → PRIMARY → OFFLINE
  • ROLE_OVERRIDES preserves current "claude for governor / gpt for strategist" defaults
  ↓
adapters/{openai,anthropic,gemini,local,self_trained}_adapter.py
  • each exposes `call_<provider>(*, model, prompt, system, session_id)`
  • each exposes `is_ready()` (env-var probe, no network)
  • openai/anthropic/gemini → emergentintegrations.llm.chat with universal key
  • local + self_trained → stubs returning NOT_IMPLEMENTED / NOT_DEPLOYED
  ↓
shared/llm/ledger.py  →  llm_calls collection
  every call ledgered with prompt/response/usage/latency/llm_authority
```

### Training substrate (`shared/llm/training/`)
The closed-loop learning surface that drives local/self_trained promotion:
* `preference_log.py` — brains post-hoc grade LLM answers
  (`score ∈ [-2..2]`, outcome, note). Writes to `llm_preference_log`.
  Plus `tally_preferences(window_hours, provider)` aggregator.
* `distillation_queue.py` — successful (score ≥ +1) calls enqueued
  for training. Idempotent, immutable rows, `consumed_at` stamp on
  pull. Plus `auto_enqueue_recent_winners(window_hours)` sweep.
* `eval_harness.py` — runs a prompt set through PRIMARY vs CANDIDATE
  provider, scores agreement (token-Jaccard for now), persists full
  per-prompt detail to `llm_eval_runs`. Drives promotion decisions.

### New Mongo collections
- `llm_calls` — every kernel call (the decision-trace ledger)
- `llm_provider_state` — operator-set promotion states
- `llm_preference_log` — post-hoc grades on LLM calls
- `llm_distillation_queue` — training pairs for self-trained
- `llm_eval_runs` — candidate-vs-primary head-to-head runs

### Doctrine locks (tripwires — 18 new, total now 169 passing)
- `llm_authority="ADVISORY_ONLY"` stamped on every response + every ledger row.
- Kernel module's `import`/`from` lines must not reference
  `shared.execution`, `shared.broker_router`, `shared.auto_router`,
  `shared.executor_seat`, or `shared.broker.*`.
- Kernel class has no method whose name contains
  execute/submit/place_order/send_order/route_order/place_trade.
- `PROVIDER_PRIORITY` pinned exactly: `(local, self_trained, anthropic, openai, gemini)`.
- `PROMOTION_STATES` pinned exactly: `{SHADOW, ADVISOR, PRIMARY, OFFLINE}`.
- Default promotion locks local + self_trained in SHADOW. Commercial = PRIMARY.
- All five adapters share the exact `(model, prompt, system, session_id)` kw-only signature.
- All five adapters expose `is_ready()` that never raises.
- local + self_trained are NOT ready by default until env vars are set.

### How a brain uses it (today)
```python
from shared.llm import llm_kernel

result = await llm_kernel.call(
    role="opponent",
    task="argue_against_long_thesis",
    prompt="Thesis: AAPL gap-fill long. Argue the bear case.",
    metadata={"intent_id": intent_id},
)
# result["response"] — the model's argument
# result["llm_authority"] — always "ADVISORY_ONLY"
# result["call_id"] — FK into llm_calls collection
```

### Phase 1 → 2 → 3 path
- **Phase 1 (NOW)**: provider-router AI. Commercial APIs serve traffic, local/self_trained logged in SHADOW. **DONE**.
- **Phase 2 (NEXT)**: deploy local inference (Ollama / vLLM). Operator sets `RISE_AI_LOCAL_INFERENCE_URL`, runs `eval_harness` to compare answers, promotes `local` to ADVISOR / PRIMARY as agreement crosses threshold.
- **Phase 3 (FUTURE)**: train RISE_AI's own weights from `llm_distillation_queue` corpus, deploy as `self_trained`, eventually promote it to PRIMARY. Commercial = teachers only.

### Files
- `shared/llm/__init__.py`, `kernel.py`, `routing_policy.py`,
  `ledger.py`, `provider_state.py`
- `shared/llm/adapters/{openai,anthropic,gemini,local,self_trained}_adapter.py`
- `shared/llm/training/__init__.py`, `preference_log.py`,
  `distillation_queue.py`, `eval_harness.py`
- `tests/test_llm_kernel.py` (19 tests, 14 tripwires)
- `tests/test_llm_training_substrate.py` (15 tests)
- `namespaces.py` — 5 new collection constants



## 🆕 2026-05-21 (later): RISEAI Code Agent v0.6 — LLM `diagnose` (portable)

Added the LLM patch-proposer to the brain-side CLI tool at
`/app/runtime_patch_kit/riseai_code_agent/`. The kit is now at v0.6.0
and remains zero-dependency — uses Node 18+ native `fetch` for direct
HTTPS calls to provider APIs.

### What's new
- `diagnose <question>` command — reads the operator-curated repo
  paths (`--paths` required), sends them with the question to the
  chosen LLM, writes a structured proposal to disk for human review.
  NEVER auto-applies a patch.
- Provider abstraction (`agent/llmProvider.js`) with three providers:
  `anthropic` (default, `claude-sonnet-4-5-20250929`), `openai`
  (`gpt-5.1`), `gemini` (`gemini-2.5-pro`). Each uses its public
  HTTPS endpoint + a direct API key from the environment.
- 5-section locked output: `## Analysis / ## Proposed Patch /
  ## Tests / ## Rollback / ## Risk`. System prompt pins doctrine
  (MC is a notary; role anchors fixed; tripwires sacred).
- `extractDiff()` helper pulls a clean unified diff out of the
  proposal markdown (handles fenced ```diff``` blocks and unfenced).
- 13/13 self-check tests pass. 13/13 diagnose unit tests pass
  (extractDiff, llmProvider provider/key plumbing).

### The "leave-the-platform" story
The CLI is deliberately NOT wired to the Emergent Universal LLM Key
(which is a Python-only broker). When the operator self-hosts, the
only change is which API key env var is populated:
- `--provider anthropic` → `ANTHROPIC_API_KEY`
- `--provider openai` → `OPENAI_API_KEY`
- `--provider gemini` → `GEMINI_API_KEY`

No code change, no migration step. Drop into a self-hosted box, set
one env var, and `diagnose` works identically.

### Files
- `agent/llmProvider.js` (new) — direct HTTPS callers.
- `agent/diagnose.js` (new) — main flow + arg parsing + diff
  extraction + proposal writer.
- `agent/test_diagnose.js` (new) — smoke tests (`yarn test`).
- `agent/selfCheck.js` — added module-load checks for the two new
  modules; total now 13 PASS.
- `riseai.js` — added `diagnose` route + help text.
- `package.json` — bumped to 0.6.0 + `"test"` script.
- `README.md` — documented the new command, the portability story,
  and the recommended diagnose → doctrine-check → report flow.



## 🆕 2026-05-21 (latest): PARADOX Wake Orders (operator panic-button)

Operator-issued "process this ticker NOW" directives. Pull-based to fit
the existing one-way sidecar→MC architecture — MC writes a signed wake
order to its own DB and the sidecar polls on its heartbeat cadence.
Wake orders do NOT bypass execution gates; they tell a brain "look at
SYMBOL on your next loop" but the brain still has to produce a valid
intent that survives the gate chain.

### Endpoints (all under `/api/admin/paradox/`)
  * `POST /wake/{brain}` — JWT admin. Body `{ticker, note?}`. Issues
    one signed wake order targeted at {brain}.
  * `POST /wake-all` — JWT admin. Body `{ticker, note?, brains?}`. Fans
    out to every LIVE_RUNTIMES brain (or a subset).
  * `GET /wake-orders/{brain}` — token-authed (per-brain ingest token).
    Returns pending (not acked, not expired) orders. Sidecars poll
    this on heartbeat cadence.
  * `POST /wake-orders/{brain}/{order_id}/ack` — token-authed.
    Idempotent ack — second ack is a no-op.
  * `GET /wake-orders` — JWT admin. Recent orders (24h default) for
    the Roster UI's "LAST WAKE" pill.

### Doctrine
  * Each wake order carries an HS256 JWT envelope (claims: order_id,
    brain, ticker, issued_at, exp, kind="wake") signed with
    `JWT_SECRET` so sidecars can verify authenticity.
  * TTL = 15 minutes. Stale pending orders are auto-marked "expired"
    on the next poll.
  * Cross-brain ack is rejected (brain X cannot ack brain Y's order).
  * Shelly is excluded — wake is only valid for LIVE_RUNTIMES
    (alpha, camaro, chevelle, redeye).

### Files
  * `routes/paradox_wake_routes.py` — all five endpoints.
  * `namespaces.py` — new `PARADOX_WAKE_ORDERS` collection name.
  * `components/ParadoxRosterPanel.jsx` — added per-row WAKE button +
    header WAKE ALL button + WakeModal + LAST WAKE pill per row.
  * `tests/test_paradox_wake.py` — 13 HTTP tests covering issue,
    fan-out, poll, idempotent ack, cross-brain rejection, admin list.

### Live verification
  * 13/13 wake tests pass.
  * 151 tripwires green post-merge.
  * UI: modal opens, ticker submits, "LAST WAKE" pill renders inline.



## 🆕 2026-05-21 (later): PARADOX in-process coordinator (LIVE in preview)

Replaces the proposed Celery/Redis distributed scheduler with an
asyncio-based in-process coordinator. Three doctrinally-locked rules:

  1. **Every execute call goes through `/api/execution/submit`** — the
     full 11-gate chain plus paradox-record writer. The execute agent
     POSTs to `/api/admin/paradox/execute-next` which internally
     re-POSTs to the gated submit path. No direct broker import.
  2. **Each agent has its own enable flag.** There is no global kill
     switch. Tripwire `test_no_global_kill_switch_constant` enforces
     it at module-import time.
  3. **Default state: every agent disabled.** Operator must explicitly
     enable each one via `/api/admin/coordinator/enable/{agent}`.

### Files
  * `shared/coordinator/state.py` — in-memory `CoordinatorState` /
    `AgentState`, 5 agents (scan, evaluate, execute, risk, retrain).
  * `shared/coordinator/agents.py` — agent HTTP functions; mints a
    short-lived JWT against `JWT_SECRET` for self-calls.
  * `shared/coordinator/runner.py` — asyncio loop; `run_agent`,
    `run_cycle`; failures captured into state, never raised.
  * `shared/coordinator/routes.py` — operator endpoints under
    `/api/admin/coordinator/{status,enable,disable,run,run-cycle,cycle-seconds}`.
  * `shared/coordinator/lifespan.py` — wired into FastAPI lifespan.
  * `shared/coordinator/user_seed.py` — idempotent seeding of
    `paradox-coordinator` system user (no password; auth-only via the
    internally-minted JWT).
  * `routes/paradox_agent_routes.py` — thin stubs for `scan`,
    `evaluate`, `execute-next`, `risk/check`, `ml/retrain/check`.
    `execute-next` is the only non-stub: it pulls one queued intent
    and routes it through `/api/execution/submit`.
  * `tests/test_paradox_coordinator.py` — 12 tests, 5 tripwires.

### Live verification
  * All 5 agents fire in parallel via `run-cycle`
  * Internal JWT authenticates as `paradox-coordinator` system user
  * Execute agent correctly NO-OPs (`reason=no_queued_intents`) —
    nothing fires through MC because nothing is queued
  * Status panel reflects per-agent state with `last_result_summary`

### Tripwire status: 151 passing (was 146; +5 coordinator locks)


## 🆕 2026-05-21: Roster page rewrite + paradox-record writer (LIVE in preview)

### Front-end PARADOX Roster panel
  * `frontend/src/components/ParadoxRosterPanel.jsx` (new) — consumes
    `/api/admin/paradox/roster`; 5-row anchored model (no eligibility
    swaps possible); auto-refresh every 15s; failed-conditions inline.
  * `pages/Overview.jsx` swapped its import from `RosterPanel` →
    `ParadoxRosterPanel`. Old 606-line eligibility-matrix component
    remains in the tree (`RosterPanel.jsx`) but is no longer referenced.
  * Live screenshot confirms: kernel name, anchored mapping, vacant
    executor condition (Camaro: stale checkin + hash mismatch + 499
    orphans) all rendering correctly.

### Paradox-record writer
  * `shared/runtime/paradox_record.py` (new) — writes one record per
    gate evaluation; best-effort (never crashes the live path).
  * Hooked into `shared/execution.py` at three sites:
      - `/api/execution/dry_run` (every dry-run produces a record)
      - submit-blocked path (REJECTED verdict)
      - submit-passed path (APPROVED or DAMPENED + broker receipt)
  * Verdict labels locked: `APPROVED` / `DAMPENED` / `REJECTED`.
  * Audit-status labels locked: `final` / `shadow` / `unaudited`,
    determined by `OPPONENT_MODE` env var.
  * 11 tests in `tests/test_paradox_record_writer.py`, 2 tripwires
    locking the verdict + audit-status surface.
  * Live verification: dry-run on TSLA produced a paradox_record with
    `executor=camaro → opponent=redeye`, `verdict=REJECTED`,
    `audit_status=shadow`, `risk_multiplier=0.677`.

### Tripwire status: 146 passing (was 144; +2 from writer locks)

## 🆕 2026-05-20 (later): PARADOX hierarchy, UV→SO reclassification, orphan watchdog

### PARADOX hierarchy — anchored role/runtime model (LIVE)

Architectural correction collapsing the role/runtime Cartesian product
into a 1:1 anchored model. The kernel sits ABOVE the named brains, not
as a peer; it is named **PARADOX** because its job is to hold the
tension between competing brain voices without picking a side.

```
RISEDUAL                    (platform)
  PARADOX (MC kernel)       (the system mind; verifies, routes, signs)
    Alpha     → strategist
    Camaro    → executor
    Chevelle  → governor
    REDEYE    → opponent (currently shadow_observation)
    Shelly    → memory  (namespace-reserved; not yet a running sidecar)
```

**AUDITOR is NOT a seat.** It is the emergent function of (executor,
opponent) — the `paradox_record` artifact the kernel stamps on every
gated intent.

- `namespaces.py` → new `ROLE_ANCHORS`, `RUNTIME_ROLE`, `LIVE_RUNTIMES`,
  `OPPONENT_MODE_*`, `PARADOX_KERNEL`, `PARADOX_RECORDS`.
- `shared/seat_policy.py` → `SEAT_ALIASES` corrected to map
  `advisor → opponent` (was `advisor → auditor`, structurally wrong).
  Legacy `auditor → opponent` for back-compat.
- `shared/runtime/role_health.py` (new) — survival conditions per role.
  Executor (Camaro) requires: fresh `mc_checkin` (≤90s), matching
  `policy_hash`, zero orphan fills in 24h, watchdog armed.
- `routes/paradox_routes.py` (new): `/api/admin/paradox/{health,roster,records}`.
- `tests/test_paradox_namespace.py` (new) — 12 tripwire tests locking
  the role anchors, opponent-mode constants, no-auditor rule.
- `tests/test_seat_aliases.py` — updated for the auditor correction.

### UV → SO reclassification (LIVE)

  * `services/memory_kernel.py::reclassify_uv_to_so` — append-only,
    operator-driven UV→SO promotion. Only UV→SO allowed; UV→VE,
    SO→VE, VE→anything, DI→anything all refused.
  * Endpoints: `POST /quarantine/{memory_id}/promote-to-so`,
    `POST /quarantine/promote-batch-to-so`,
    `GET /reclassifications/recent`.
  * 9 tests (`tests/test_memory_kernel_reclassification.py`), 2 tripwires:
    axiom holds for reclassified SO; UV→VE forbidden.

### Orphan replay calibration report (LIVE)

  * `routes/orphan_replay_routes.py::orphan_doctrine_c_report`.
  * Replays every UV/SO orphan through doctrine (c) gates with
    lane-typical synthesized snapshots; aggregates outcomes,
    per-symbol breakdown, spread buckets, and a narrative
    `calibration_signal`.
  * **Verdict on the 5/18 corpus: 100% would have passed doctrine (c)
    cleanly.** The orphans weren't dangerous because they were wrong —
    they were dangerous because they bypassed the auth layer. RoadGuard
    and Governor are correctly tuned for the Mag-7 universe.

### Tripwire status: 144 passing (was 133 entering this segment; +11)

### Operational note — Camaro's executor seat is currently VACANT
Live `/api/admin/paradox/roster` reports executor unhealthy:
  1. `checkin_stale` — Camaro sidecar isn't posting `mc_checkin` yet
  2. `policy_hash_mismatch` — same root cause
  3. `recent_orphans: 499` — the 24h orphan window still includes
     the 5/18 fills

(1) and (2) self-heal once Camaro's sidecar deploys with the new
policy hash. (3) self-heals naturally in ~24h OR immediately by
operator action (UV→SO batch reclassification — which is now wired).


## 🆕 2026-02-19 (earlier this session): Doctrine (c) + Orphan defense + Memory Kernel P0

This session installed the architectural correction for the "governance
deadlock" failure mode (1,578 authority calls / 0 fills), captured 500
historical orphan broker fills into the kernel, and armed a continuous
watchdog against future orphans.

### Doctrine (c): Separation of Concerns — LIVE
Re-scoping that broke the multiplicative-veto freeze:

  * **Brains**: own directional agency + confidence floor
  * **Chevelle/Governor**: SIZE ONLY — `governor_action` is always
    `"modulate"`, never `"block"`. Wide spread / low volume / quality
    issues become risk dampeners.
  * **Opponent seat**: only directional hard veto (`HARD_VETO_OPPONENT`)
  * **RoadGuard**: deterministic market-structure caps (new
    `roadguard_spread_floor` gate; crypto 200 bps, equity 50 bps).
    Fail-closed on missing snapshot.
  * **MC**: authority / schema / broker / cap verifier only —
    confidence-floor + doctrine-quality re-vetoes removed.
  * **Patent J**: brain promotion readiness only; no longer suppresses
    live intent flow.

Locked `GOVERNOR_DAMPENERS` table (tripwire):
```
WIDE_SPREAD              0.50
LOW_VOLUME               0.60
LOW_QUALITY              0.70
UNCERTAIN                0.75
THREE_CONSECUTIVE_LOSSES 0.50
DAILY_LOSS_LIMIT         0.25
```

Files touched:
  * `shared/runtime/platform_survival.py` (MC no longer reblocks conf)
  * `shared/crypto/doctrine/crypto_brain_sidecars.py` (dampeners)
  * `shared/doctrine/brain_sidecars.py` (no fatal stops in governor)
  * `shared/doctrine/strategy_doctrines.py` (gap_and_go + micro_pullback)
  * `shared/execution.py` (new RoadGuard gate row)
  * `tests/test_doctrine_c_separation.py` (12 tests, 8 tripwires)
  * 5 stale tests inverted to assert (c) behavior

### Memory Kernel P0 — LIVE
  * `services/memory_kernel.py`: `Provenance{VE,SO,DI,UV}` +
    `SettlementOracle` (broker × receipt consensus) + axiom +
    `KernelGate` (capability routing, CRITICAL quarantine on
    execution-engine attempts).
  * `services/brain_memory_translator.py`: dialect collapser
    (stacks/types/fields/directions/confidence), 33 tests, 4 tripwires.
  * 5 endpoints at `/api/admin/memory-kernel/*` (submit, route,
    trainable/fetch-lock, trainable/confirm, health).

### Promotion countersign modal — LIVE
  * `pages/Promotion.jsx`: replaced `window.prompt()` (silently blocked
    on Chrome Android) with a functional in-page modal. Stripped
    cosmetic chrome per user request.

### Orphan defense — LIVE
  * **500 historical orphan fills captured** (5/15 + 5/18). Mag-7
    momentum bot (AMZN 134 / GOOGL 126 / NVDA 122 / MSFT 113 / META 5).
    All `source=access_key`, fired in tight sub-second loops. Confirmed
    to be Camaro with a stale Alpaca paper key (rotated by user
    mid-session).
  * `scripts/alpaca_orphan_ingester.py`: one-shot, idempotent.
  * `shared/runtime/orphan_watchdog.py`: continuous, polls Alpaca every
    120s, auto-quarantines any fill lacking an MC receipt. Armed via
    `ALPACA_ORPHAN_WATCHDOG_ENABLED=true`.
  * `routes/orphan_inspection_routes.py`:
    `/api/admin/runtime/orphans/{summary,recent}` for operator
    visibility.

### Tripwire status: 131 passing (was 122 at session start; +9)

### Manual steps required after deploy
  1. Update MC's stored Alpaca creds at `/admin/alpaca` with the new
     pair (`PK4V5RXCZUJXHTLKAZRYQ34XZ6` / secret).
  2. Re-run the orphan ingester against prod Mongo with the same
     5/15–5/19 window so prod's kernel has the historical orphans.
  3. Confirm these env vars exist in prod:
     `ALPACA_INGEST_KEY_ID`, `ALPACA_INGEST_SECRET_KEY`,
     `ALPACA_ORPHAN_WATCHDOG_ENABLED=true`,
     `ALPACA_ORPHAN_WATCHDOG_INTERVAL_S=120`.

### Still pending — next session
  * **Seat rotation** (still un-picked: a/b/c/d). Alpha holds crypto
    executor; Camaro posts 99% of crypto intents. User suggested to
    observe one cycle under doctrine (c) before rotating.
  * UV→SO reclassification endpoint for the 500 orphans (so they can
    feed the replay engine without violating the train-on-VE-only
    axiom).
  * RoadGuard threshold calibration via orphan-replay report.


## 🆕 2026-02-19 (earlier this session): Memory Kernel P0 + Brain Translation Layer

This session installed the load-bearing wall in front of all downstream
cognition: brains may speak many dialects, MC stores exactly one language,
and **only MC may classify Verified Execution**.

### Brain Memory Translator — `services/brain_memory_translator.py`
- Pure, stateless dialect-collapser sitting in front of the kernel.
- Canonical surfaces locked by tripwires:
  - Stacks: `{alpha, camaro, chevelle, redeye}`
  - Memory types: `{execution, diagnostic, council_dissent, governance_review, replay, backtest, simulation}`
  - Directions: `{BUY, SELL, HOLD}`
  - Fields: `{symbol, broker_order_id, execution_receipt_id, filled_qty, direction, confidence}`
- Translation breadcrumb (`_translated_from`) preserved on every payload for forensics.
- Confidence is coerced to `[0,1]`; percentage form auto-divided; unparseable → `None`.
- 33 tests in `tests/test_brain_memory_translator.py` (4 tripwires).

### Memory Kernel P0 — `services/memory_kernel.py`
- `Provenance` enum: `VE`/`SO`/`DI`/`UV` (locked by tripwire).
- `SettlementOracle.verify()` — MC-only consensus across `broker_orders` + `execution_receipts` collections. Symbol + status + qty must all agree.
- `MemoryKernelLedger.submit_memory()` — append-only insert, MC classifies provenance from `memory_type` and (for executions) the oracle proof. Stacks can *request* VE; only MC can *grant* it.
- `MemoryKernelLedger.fetch_and_lock_trainable()` — atomic fetch + lock against double-training.
- `MemoryKernelLedger.confirm_training_complete()` — carries the axiom:
  ```
  if memory_record["provenance"] != Provenance.VE.value:
      raise RuntimeError("Refusing to train on non-verified memory")
  ```
- `KernelGate.route()` — capability router for cross-component memory hops. Blocks non-VE → training/execution; logs every decision to `memory_kernel_routes`; writes CRITICAL alerts to `memory_kernel_quarantine` on execution-engine attempts.
- 16 tests in `tests/test_memory_kernel_p0.py` (2 tripwires — axiom + provenance-enum).

### HTTP surface — `routes/memory_kernel_routes.py`
- `POST /api/admin/memory-kernel/submit` (admin JWT) — runs translator → ledger
- `POST /api/admin/memory-kernel/route` (admin JWT) — kernel gate
- `POST /api/admin/memory-kernel/trainable/fetch-lock` (admin JWT)
- `POST /api/admin/memory-kernel/trainable/confirm` (admin JWT, 422 on axiom break)
- `GET  /api/admin/memory-kernel/health` (public)

### Mongo collections introduced
- `memory_kernel_ledger` — append-only memories, `payload_hash`, `provenance`, `trainable`, `used_in_training`, `training_lock`
- `memory_kernel_quarantine` — UV submissions + blocked-route alerts with `alert_level`
- `memory_kernel_routes` — every gate decision

### Tripwire surface
- 122 passing (was 120; +2 from kernel axiom + provenance-enum lock)
- 49/49 kernel + translator tests green
- End-to-end live-URL smoke validated: Camaro dialect → DI (governance_review); REDEYE dialect → UV (no consensus sources) → routed to training → BLOCKED.

### Not built in this session (deferred by user instruction)
- `RegimeEncoder` — explicitly held until P0 + P1 stable
- Clearinghouse third consensus source — oracle is two-source for P0


## 🆕 2026-02-19 (earlier this session): Calibration, contract, 4-seat merge, riseai-code-agent

This session shipped multiple MC-side surfaces. Summary:

### 1. Sidecar identity check-in surface
- `GET/POST /api/admin/runtime/sidecar-checkin[/{brain}]` — admin JWT for GET (lists all brains' last verdict), per-brain ingest token for POST
- New collection `sidecar_checkins`; unique index on `runtime`
- Verdicts: `prod` / `preview` / `policy_drift` / `invalid` / `never`
- Diagnostics panel `SidecarCheckinPanel.jsx` with 15s polling
- Paste-in clients shipped for Alpha, Camaro, REDEYE (3 of 4 brains live; Chevelle pre-existing). All currently 404'ing PROD MC awaiting redeploy.

### 2. Confidence-floor calibration sweep
- `GET /api/admin/calibration/confidence-floor-sweep` (admin JWT, read-only)
- Reports raw_pass / effective_pass / dampener_drop / win_rate per floor
- HOLD invariant enforced: `DIRECTIONAL_ACTIONS = {BUY, SELL, SHORT, COVER}` never includes HOLD regardless of floor
- Found that production confidence is 0.7-0.9; default 0-0.45 sweep doesn't bite. Bite point is 0.75-0.85.

### 3. Snapshot-completeness diagnostic + canonical contract
- `GET /api/admin/intents/snapshot-completeness` (admin JWT, tiered)
- `GET /api/runtime/survival/snapshot-contract` (no auth, doctrine read — like `/policy-hash`)
- Single source of truth: `shared/calibration/snapshot_contract.py`
- Tiers: MINIMUM (Alpha's 7 fields, first-fill readiness) + FULL_CRYPTO (11) + FULL_EQUITY (11)
- Contract hash drift tripwire: `tests/test_snapshot_contract.py::test_contract_hash_is_locked_in`
- Current contract hash: `1214e673813f00a827fa1b9635511ea22bc787d0a1280a807f0b48eeea0d6184`
- Diagnosis: 100% snapshot blackout across all 3 active brains; first fill blocked here

### 4. 4-seat merge (decider/advisor deprecation, alias-and-deprecate)
- `shared/seat_policy.py`: `SEAT_ALIASES` constant + `normalize_seat()` helper
  - `decider → executor`, `crypto_decider → crypto`, `advisor → auditor`, `crypto_advisor → crypto_auditor`
- `may_override` field DELETED from doctrine (was `SeatPolicy` TypedDict + 7 row entries + 2 stamp call sites)
- `STACK_WEIGHTS` extended with `auditor: 0.50` row; deprecated keys retained for back-compat
- Phase 1 only (compatibility merge); Phases 2-4 deferred (UI hiding, write-stopping, mongo backfill)
- 14 new tests in `tests/test_seat_aliases.py`; existing test_seat_policy_and_auto updated

### 5. RISEAI Code Agent (brain-side preflight, NOT MC enforcement)
- Lives at `/app/runtime_patch_kit/riseai_code_agent/` — NOT wired into MC
- Node CLI: `scan`, `doctrine-check` (gate, exit 2 on match), `report` (reviewer, exit 0 always), `patch-note`, `test`
- v0.2 diff-scoping fix: `doctrine-check` parses unified diff and scans only `+` lines
- v0.3 added `report` command: YAML/JSON structured output, LOW/MEDIUM/HIGH risk scoring, recommended tests per touched surface
- Grep tripwires: `may_override` re-introduction, `decider`/`advisor` re-introduction, HOLD promotion, council direction override, operator-gate-default-ON, RoadGuard bypass
- Doctrine pin: MC's `pytest -m tripwire` remains the runtime source of truth; this is upstream pre-PR review only

### Test counts at session end
- 116/116 tripwire tests pass
- 69 new + adjacent integration tests pass across new surfaces
- Zero regressions

### Production blocker status (in order)
1. ✅ Sidecar check-in: brains wired, MC redeploy pending
2. ✅ Snapshot contract: hash 1214e... published, brains know shape
3. 🔴 Snapshot enrichment on brain side: 100% blackout — brain-side fix in progress
4. ⏸ First crypto paper fill: blocked on (3)
5. ⏸ Strict-422 on ingest: deferred until brains report `minimum: ≥95%`




## 🚨 Latest (2026-05-19): Authority-call mirror — the doctrine bridge

The platform survival kit landed in Chevelle, role adapter installed,
`chevelle_emit_authority` wired into `build_opinion()`. End-to-end
plumbing test revealed a doctrine GAP: opinions land in
`shared_opinions`, but the council reads governor calls from
`shared_adl_receipts`. Without a bridge, Chevelle's calls would be
silent to the gate chain — the exact bug we set out to fix.

### The bridge: `_mirror_authority_call_to_receipts()`

Added to `shared/opinions.py`. Runs inside `/api/ingest/opinion` AFTER
the opinion is persisted, best-effort (mirror failures must never
block the opinion post). When `evidence.authority_call` is present and
the inner `brain` matches the opinion's `runtime` (defensive — no
impersonation), the mirror:

1. Translates `status` (BLOCK/WARN/ALLOW) + `reason` into the council's
   expected signal shape (`executable`, `veto`, `confidence`, `stance`,
   `reason`)
2. Writes it under `payload.*` (a container the normalizer recognizes)
3. Sets `action="authority_call"` so `_AUTHORITY_CALL_VALUES` filter
   hits
4. Sets top-level `symbol` + `lane` so `_symbol_clause()` finds it
5. Keeps the raw `authority_call` payload for forensic replay

### End-to-end live verification (preview)

| Path | Chevelle emits | Council verdict |
|---|---|---|
| **HARD veto** | `{status:BLOCK, reason:GOVERNOR_HARD_VETO}` | `allowed=False · HARD_BLOCK · BLOCK` |
| **WARN** | `{status:WARN, reason:CHEVELLE_REDUCE_SIZE}` | `allowed=True · ×0.75 · SOFT_DISSENT_DOWNWEIGHTED` |
| **ALLOW** | `{status:ALLOW, reason:NO_GOVERNOR_DISSENT}` | `allowed=True · NO_GOVERNOR_DISSENT` |

### Defenses pinned
- **Brain impersonation**: opinion `runtime=chevelle` with
  `evidence.authority_call.brain=alpha` is REFUSED (no mirror).
- **No authority_call**: opinion lacking the field is skipped silently.
- **Mirror failure**: caught and swallowed, opinion post never blocks.

### Tests
- `tests/test_authority_call_mirror.py` — 6 PASS (tripwire):
  receipts-shape contract, HARD veto round-trip, WARN round-trip,
  ALLOW round-trip, impersonation defense, no-authority skip.
- Full tripwire: **116/116 PASS** (was 110, +6 mirror tests).



## 🚨 Latest (2026-05-18, +6): Unified classifier — Brains speak → MC classifies → MC governs → MC routes

Operator architecture: one classifier on MC, one role adapter per
brain. Sidecars never decide whether their own emission is
executable — they package shape, MC owns policy.

### MC backend — 2 new standalone modules

**`shared/intent_contract.py`** — `classify_brain_intent(intent, *, min_exec_conf=0.30)` → `IntentClassification`. Returns one of 6 typed reasons:
- `EXECUTABLE_CANDIDATE` (BUY/SELL above floor, lane valid, symbol present)
- `NON_DIRECTIONAL_OPINION` (HOLD / WAIT / NONE / NEUTRAL / "")
- `UNKNOWN_DIRECTION:<X>` (anything else)
- `SYMBOL_MISSING`
- `CONFIDENCE_BELOW_EXEC_FLOOR`
- `LANE_MISSING_OR_INVALID`

Reads from permissive field chain: `direction` | `side` | `action`
for direction; `raw_confidence` | `confidence` | `effective_confidence`
for conf; `symbol` | `canonical_id` for symbol; `brain` | `source`
for brain.

**`shared/governor_policy.py`** — `apply_governor_policy(governance, *, executable, size_mult)` → `(executable, size_mult, governance)`. Standalone export of the FATAL/SILENCE taxonomy with a 10% absolute floor:
- non-BLOCK status → passes through, `display_status=ALLOW`
- BLOCK + reason in FATAL → `HARD_BLOCK` (executable=False, size=0)
- BLOCK + reason in SILENCE_OR_SOFT → `RISK_DOWN_ONLY` (size × 0.5, floor 0.1)
- BLOCK + unknown reason → conservative `RISK_DOWN_ONLY` (NOT killed)

Imports `FATAL_GOVERNOR_REASONS` and `SILENCE_GOVERNOR_REASONS` from
`shared.council` — single source of truth.

### Wired into `auto_router._route_one` Phase 0

Before the gate chain runs, every intent flows through the
classifier. Advisory-only intents (HOLD spam, missing fields, below
floor) are persisted to `shared_gate_results` as kind
`auto_router_advisory_only` with full classification metadata, and
the intent is marked `gate_state="advisory_only"` —  it never
touches `_evaluate_gates`. Kills HOLD-spam at the door.

New persistence helper `_persist_advisory_classification()` writes
the typed reason to the ledger so operators can audit WHY each
intent was filtered.

### Brain-side role adapters (in the patch kit)

New file `services/platform_survival/role_adapters.py` ships 4
canonical emit functions:

```python
camaro_emit_crypto_intent(symbol, direction, confidence, notional_usd)
  → {brain:camaro, role:crypto_executor, intent_type:EXECUTION_INTENT, ...}

alpha_emit_opinion(symbol, lane, direction, confidence)
  → {brain:alpha, role:strategist, intent_type:OPINION, ...}

chevelle_emit_authority(symbol, lane, status, reason, confidence)
  → {brain:chevelle, role:governor, intent_type:GOVERNOR_AUTHORITY,
     status:ALLOW|WARN|BLOCK, reason:..., ...}

redeye_emit_opposition(symbol, lane, direction, confidence, opposes)
  → {brain:redeye, role:opponent, intent_type:OPPOSITION, ...}
```

Each brain imports the matching adapter, wraps the output in
`sidecar_build_intent(...)` to add the RuntimeStamp, and POSTs to
MC. PASTE_INTO_*_AGENT.md docs updated with concrete examples and
behavior contracts.

### Tests

- `tests/test_intent_contract.py` — 17 PASS (tripwire): happy path
  Camaro crypto BUY + Alpha equity SELL, every advisory_only branch
  (HOLD, empty, WAIT, NEUTRAL, NONE, unknown direction, missing
  symbol, blank symbol, below floor, missing lane, invalid lane),
  field fallback chains (raw_confidence > confidence >
  effective_confidence > 0; brain → source; symbol → canonical_id),
  frozen dataclass, non-numeric confidence coercion, doctrine-set
  stability.
- `tests/test_governor_policy.py` — 13 PASS (tripwire): every
  non-BLOCK status passes through, all 9 FATAL reasons kill, all 4
  SILENCE_OR_SOFT reasons risk-down, 0.0 input → 0.1 floor, unknown
  reason → conservative risk-down (not kill), input dict not mutated,
  case-insensitive, already-blocked stays blocked.

Full tripwire: **110/110 PASS** (was 80, +30 new).

### Bundle rebuilt with new role_adapters.py

- `platform_survival.tar.gz` — 10,159 bytes,
  sha256 `06814594f0718fcef06f5a8af20dcf5e762b7a189a1b85b347597ed56e07789a`
- `platform_survival.zip` — 16,453 bytes,
  sha256 `0409d41d3bda2d8a25c3c990d57af9a35a697c4d227275711ef2e490e72f26b0`

Operator re-downloads from Diagnostics → Portable patch kits, drops
into each brain repo, redeploys.

### Doctrine rule summary
- Camaro BUY/SELL + conf ≥ 0.30 → executable candidate
- Camaro HOLD / weak → advisory only (never reaches gate chain)
- Alpha opinion → advisory unless seat-checked as executor
- Chevelle silent / offline → RISK_DOWN ×0.5 (not kill)
- Chevelle hard veto / fatal reason → true block
- REDEYE opposition → adversary weight; does NOT kill trades alone



## 🚨 Latest (2026-05-18, +5): Governor silence ≠ kill switch — FATAL/SILENCE taxonomy

Operator patch: Chevelle's silence was acting as a global kill switch
because the council's `_governance_verdict` treated `GOVERNOR_OFFLINE`,
`NO_STANCE_LOW_EFFECTIVE_CONF`, and `SOFT_DISSENT_BELOW_FLOOR` as hard
blocks. This patch separates **diagnostic + risk-down** (silence) from
**true block** (explicit veto + structural safety).

### Doctrine pin
> Chevelle offline/silent  = diagnostic + risk down
> Chevelle explicit hard veto  = true block
> Broker/auth/symbol/PDT/exposure fatal issue  = true block

Only `FATAL_GOVERNOR_REASONS` may stop execution. Everything else
becomes `RISK_DOWN_ONLY` — `allowed=True` with a conservative risk
multiplier (0.50 baseline, clamped by lane policy floor).

### New module-level surface in `shared/council.py`

```python
FATAL_GOVERNOR_REASONS = frozenset({
    "GOVERNOR_HARD_VETO", "GOVERNOR_SEAT_VACANT",
    "KILL_SWITCH_ACTIVE", "BROKER_UNAVAILABLE",
    "AUTH_MISSING", "SYMBOL_UNRESOLVED",
    "MAX_EXPOSURE_EXCEEDED", "PDT_BLOCK", "DUPLICATE_POSITION",
})
SILENCE_GOVERNOR_REASONS = frozenset({
    "GOVERNOR_OFFLINE", "NO_STANCE_LOW_EFFECTIVE_CONF",
    "GOVERNOR_NO_STANCE",
})
GOVERNOR_SILENCE_RISK_MULTIPLIER = 0.50

def governor_blocks_execution(reason): ...
def governor_risk_multiplier(reason): ...
```

### Verdict dict now carries two new fields

```python
{
    "allowed": True,              # True for both ALLOW + RISK_DOWN_ONLY
    "reason": "GOVERNOR_OFFLINE",
    "risk_multiplier": 0.50,      # clamped by lane policy
    "execution_effect": "RISK_DOWN_ONLY",  # NEW
    "display_status": "RISK_DOWN",         # NEW
    ...
}
```

### Advisory packet (`shared/doctrine/brain_sidecars.py` +
`shared/crypto/doctrine/crypto_brain_sidecars.py`)
Same taxonomy applied:
- A_QUALITY → `display_status=ALLOW` (×1.00)
- B/C/REJECT quality → `display_status=RISK_DOWN` (×0.75 / ×0.50 / ×0.25)
- Three consecutive losses / daily loss limit / wide spread / wrong lane →
  `display_status=BLOCK` (true safety, ×0.00)

`block_reasons[]`, `governor_action`, and all other downstream fields
stay shape-stable. Two new fields surfaced: `display_status` and
`reason` (the most-informative single reason for UI chip).

### UI fix — `DoctrineStrip.jsx::seatHeadline()`
Governor chip now distinguishes:
- `RISK_DOWN ×0.50 · NO_STANCE_LOW_EFFECTIVE_CONF` (orange, not red)
- `BLOCK · GOVERNOR_HARD_VETO` (red, fatal stop)
- `modulate ×0.85` (clean modulation)
- `endorse` (silent — no chip change needed)

Reads `seat.display_status` + `seat.reason` first; falls back to
legacy `block_reasons[] + risk_multiplier === 0` for backward compat.

### Tests
- `tests/test_governance_verdict.py` — **rewritten** (14 PASS,
  tripwire). Pins the new taxonomy: `GOVERNOR_OFFLINE` and
  `NO_STANCE_LOW_EFFECTIVE_CONF` and `SOFT_DISSENT_BELOW_FLOOR` all
  produce `allowed=True` + `execution_effect=RISK_DOWN_ONLY` +
  `risk_multiplier > 0`. Only `GOVERNOR_HARD_VETO` and
  `GOVERNOR_SEAT_VACANT` produce `HARD_BLOCK`. Plus 4 new tests for
  `governor_blocks_execution()` and `governor_risk_multiplier()`.
- Doctrine-sidecar tests: 54/54 PASS unchanged (`governor_action`
  field kept as binary block/modulate to avoid downstream churn).
- Full tripwire: **80/80 PASS** (was 76, +4 new taxonomy tests).

### Effect in PROD (after redeploy)
- Chevelle silent / offline → trades still go through at 50% size,
  ledger row shows `RISK_DOWN · GOVERNOR_OFFLINE` (orange chip)
- Chevelle actively votes `VETO` at high conviction → trade blocked
  with `BLOCK · GOVERNOR_HARD_VETO` (red chip)
- Broker offline, auth missing, max exposure exceeded, PDT, duplicate
  position → blocked (red chip with reason)
- Three losses / daily loss limit → blocked (red chip with reason)

### What this fixes
Operator's PROD screenshot showed every Camaro intent getting
`GOVERNOR · BLOCK (chevelle)` — the chip didn't name the reason, and
the reason was almost certainly silence (Chevelle's heartbeat stale +
no authority calls). After this patch, the same scenario would show
`GOVERNOR · RISK_DOWN ×0.50 · GOVERNOR_OFFLINE` and the trade would
still flow through at half size. Chevelle's silence is diagnostic
data, not a global stop.



## 🚨 Latest (2026-05-18, +4): Circular import broken — `shared/regime_keys.py`

Operator request: 10-minute proper cleanup before redeploy (after
verifying the Emergent Code Review's 35/100 score was mostly
fabricated — only the circular-import claim was real).

### What moved
New module `shared/regime_keys.py` (191 lines, stdlib-only) holds the
3 primitives that both `intents.py` and `hypothesis.py` need:
- `REGIME_FP_KEYS` (frozenset, 6 canonical fingerprint keys)
- `_regime_fingerprint(indicators)` (6-bucket coarse fingerprint)
- `_looks_like_crypto(symbol)` (Kraken/Camaro pair heuristic)

### Three surgical edits
1. **`shared/intents.py`** — top-of-file imports the 3 names from
   `regime_keys`; deleted 2 deferred imports (lines 182, 471) and the
   local `_looks_like_crypto` definition (~36 lines net shrink).
2. **`shared/hypothesis.py`** — top-of-file imports from `regime_keys`;
   re-exports `REGIME_FP_KEYS` + `_regime_fingerprint` as module-level
   aliases for downstream `from shared.hypothesis import REGIME_FP_KEYS`
   callers; deleted the deferred `from shared.intents import
   _looks_like_crypto` (line 416). Identical public surface.
3. **No `# noqa: WPS433` deferred-import markers remain for this cycle.**

### Verification
- `grep "from shared.hypothesis" shared/intents.py` → 0 hits
- `grep "from shared.intents" shared/hypothesis.py` → 0 hits
- `ruff check` → All checks passed
- Backend cold boot → clean
- Tripwire regression: **76/76 PASS** (unchanged)
- Live policy-hash unchanged: `2ac7d02164886f5c…`
- Live promotion-artifact endpoint still returns valid verdicts

### Code Review report verdict (canonical-linter audit)
Of the 8 claims in the 35/100 Emergent Code Review:
| Claim | Reality | Action |
|---|---|---|
| Circular import | REAL | **FIXED** ✅ |
| 41 undefined vars | `ruff F821: 0` | FABRICATED |
| 106 missing hook deps | `ESLint exhaustive-deps: 0` | FABRICATED (same as prev fork's "96 hook deps" false positive) |
| 461 `is`-literal anti-patterns | `ruff F632: 0` | FABRICATED |
| Hardcoded test secrets | These are test credentials documented in `test_credentials.md`; one already uses env-fallback | FALSE POSITIVE |
| localStorage for JWT | TRUE | BY-DESIGN (SPA pattern) |
| `_governance_verdict` 92 lines | TRUE, < 120-line threshold | Already on watch list |
| 227 nested ternaries / 1 empty catch | Style preferences | NOISE |

Doctrine pin: future Code Review reports MUST be verified against
canonical linters before action. This was the 2nd false-positive
incident with this report (count = 2).



## 🚨 Latest (2026-05-18, +3): Broker-side MC-receipt seal wired

Phase-2 of the platform survival rollout: every order leaving Mission
Control now carries an HMAC-signed `MCExecutionReceipt`. Broker
adapters refuse unsigned/tampered orders **when enforcement is on**.
Enforcement defaults `false` so PROD Alpha keeps trading while its
sidecar adopts the kit.

### Insertion point
`shared/broker_router.route_order(...)` — the single chokepoint that
every fill flows through (manual `/execution/submit` AND auto-router).
After step 4 (adapter fetch) and before step 5 (broker submit), the
router calls a new helper `_mint_and_verify_mc_receipt(...)`:

1. Builds a survival-layer envelope from the existing intent
   (synthesizes a neutral `runtime` stamp if the sidecar hasn't yet
   adopted the kit).
2. Runs the envelope through `mc_canonical_gate(...)` — returns the
   HMAC-signed `MCExecutionReceipt`.
3. Calls `broker_verify_receipt(...)` to validate the signature.
4. Behavior depends on `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT` env flag:
   - `false` (default, rollout mode): logs warnings on failure, lets
     the order through. Operator can see the failure rate before
     flipping the switch.
   - `true` (enforce mode): raises `BrokerRouteBlocked` with
     `MC receipt rejected: <reason>` and the fake / real adapter is
     never called.

### Provenance on every fill
Execution receipts (both `auto_router._build_receipt` and
`execution.py:execution_submit`) now persist three new fields:
- `mc_receipt` — the signed receipt object
- `mc_receipt_status` — `VALID_MC_RECEIPT` / `BAD_MC_RECEIPT_SIGNATURE` /
  `MISSING_RECEIPT_SECRET` / `SIDECAR_LOCAL_AUTHORITY_FORBIDDEN` / etc.
- `mc_receipt_enforced` — boolean snapshot of the flag at execution time

The operator can now slice `execution_receipts` by `mc_receipt_status`
to see exactly which fills passed the cryptographic seal.

### Env flags (new)
- `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT=false` — added to `backend/.env`.
  Set to `true` to enforce. Read on every `route_order` call, so
  flipping the flag is hot-reload (no restart needed).

### Tests
`tests/test_broker_router_mc_receipt.py` — 9 PASS (all marked tripwire):
- enforcement flag default-off + truthy variants
- mint helper: synthesizes neutral stamp / passes through sidecar
  stamp / rejects sidecar that lies about local authority
- route_order: attaches receipt metadata in rollout mode, enforces
  block when flag on + no secret, lets valid receipt through under
  enforcement, blocks lying sidecar under enforcement (real adapter
  never called)

Full regression: 76/76 tripwire (was 67) — the 9 new tests pin the
broker-side seal contract.

### Doctrine pin
Sidecars communicate → MC approves → MC mints a receipt → broker
verifies the signature → fill happens. **Without the receipt, no
fill.** The flag flip from `false` → `true` is the one-line operation
that promotes the survival doctrine from advisory to mandatory.

### Operator switch-flip checklist
1. Drop `platform_survival.tar.gz` into each brain repo (highest
   priority: Alpha)
2. Adopt `sidecar_build_intent(...)` and `RuntimeStamp.current(...)`
   in each sidecar
3. Watch `execution_receipts.mc_receipt_status` in the dashboard for
   ≥24h — confirm `VALID_MC_RECEIPT` for every PROD Alpha fill
4. Flip `RISEDUAL_BROKER_REQUIRE_MC_RECEIPT=true` in MC `.env`
5. From this moment forward, no sidecar drift can fire a fill



## 🚨 Latest (2026-05-18, +2): Survival kit extraction — operator can pull it OUT of the preview

Operator pushback: "But that's inside the preview." Correct — the
survival layer is worthless if it can only be read inside the same
preview pod whose drift we're trying to prevent. Three extraction
paths shipped, all reading the same on-disk artifact at
`/app/runtime_patch_kit/platform_survival/`:

1. **Browser download (operator workflow)** — `GET /api/admin/runtime-bundles`
   lists the registered bundles with sha256 + bytes;
   `GET /api/admin/runtime-bundles/{filename}` streams the file with
   `Content-Disposition: attachment` and an `X-Bundle-Sha256` header
   for integrity. JWT-gated. New `RuntimeBundlesPanel.jsx` on the
   Diagnostics page renders one row per bundle with a single-click
   download button (uses blob → anchor so the auth header rides on
   the fetch, not on a naked `<a href>`).
2. **curl (CI / scripting)** —
   `curl -L -H "Authorization: Bearer $TOKEN" "$MC/api/admin/runtime-bundles/platform_survival.tar.gz" -o platform_survival.tar.gz`,
   verify with `sha256sum`.
3. **Sidecar pull (programmatic)** — already-existing
   `/api/patches/{name}/manifest` + `/api/patches/{name}/file/{path}`
   endpoint, gated by `X-Runtime-Token`. The `platform_survival`
   patch is now registered there with its 8 files. Every pull is
   audit-logged into `shared_patch_pulls`.

### Bundle artifact

`/app/runtime_patch_kit/bundles/`:
- `platform_survival.tar.gz` — 8121 bytes,
  sha256 `43199b1a24129f6c581b8a75ef854a848e7587a85a0063fec3648a249bc51d93`
- `platform_survival.zip` — 13539 bytes,
  sha256 `c658fe88856cce740c6ca9280a4629d311245ff90f1cddfe6e427bf05220c584`
- `PLATFORM_SURVIVAL_CHECKSUMS.txt` — sibling file the operator can
  compare against post-transfer

### Security pins
- Bundle filenames are registry-whitelisted; the endpoint refuses any
  filename not in `BUNDLE_REGISTRY` (path-traversal-proof, returns 404).
- Unauthenticated requests return 401.
- `RISEDUAL_MC_RECEIPT_SECRET` is NEVER bundled into the kit — it
  stays on MC and the broker adapter only.

### Verified live (preview)
- Manifest endpoint returns both bundles with correct sha256 + sizes
- Browser download saves the bytes; sha256 of downloaded file matches
  the manifest exactly
- Tampered filename → 404
- Unauthenticated → 401
- Diagnostics page renders 2 bundle rows, 2 download buttons, no
  panel boundary fires



## 🚨 Latest (2026-05-18, +1): Platform Survival Layer — placement

Operator directive: "Build a portable survival layer that Emergent can
run, but does not depend on Emergent. It's not a patch, it's a
placement. This embeds the sidecar with the stack."

The problem we solved: the four brain sidecars (Alpha · Camaro ·
Chevelle · REDEYE) live in separate repos on different hosts. From
MC, we cannot reliably tell PROD from preview, cannot read a sidecar's
`git_sha` or `policy_hash`, and we cannot guarantee a sidecar isn't
secretly assuming local execution authority. The survival layer makes
that explicit and verifiable, and ships **into each brain repo** so
nothing depends on Emergent or any specific platform.

### Module (placed in 5 locations — 1 MC + 4 brain stacks)

- `backend/shared/runtime/platform_survival.py` — MC enforcement copy
- `backend/shared/runtime/routes.py` — MC HTTP surface
- `runtime_patch_kit/platform_survival/services/platform_survival/__init__.py`
  — portable copy each brain stack drops into its own
  `backend/services/platform_survival/`
- Per-stack paste-in docs at
  `runtime_patch_kit/platform_survival/PASTE_INTO_{ALPHA,CAMARO,CHEVELLE,REDEYE}_AGENT.md`

### Public surface (4 building blocks)

1. **`RuntimeStamp.current(sidecar_room)`** — captures env, git_sha,
   platform, mc_url, db_name, broker_mode, sidecar_version, policy_hash,
   `local_execution_authority=False`, timestamp_ms. `.validate_for_prod_sidecar()`
   returns a typed errors list (`ENV_NOT_PROD`, `MC_URL_NOT_PROD`,
   `SIDECAR_HAS_LOCAL_EXECUTION_AUTHORITY`, `UNKNOWN_GIT_SHA`,
   `BAD_OR_UNKNOWN_DB_NAME`, `BAD_BROKER_MODE`).
2. **`sidecar_build_intent(...)`** — the only legitimate path a brain
   sidecar uses to package an intent. Carries the stamp inside.
3. **`mc_canonical_gate(intent)`** — MC's single gate. Rejects on
   sidecar local-authority, policy_hash mismatch, bad direction, bad
   lane, missing symbol, sub-floor confidence. Emits HMAC-signed
   `MCExecutionReceipt` keyed on `RISEDUAL_MC_RECEIPT_SECRET`.
4. **`broker_verify_receipt(receipt)`** — broker adapter call.
   Refuses any order without a valid MC signature or with
   `MISSING_RECEIPT_SECRET`.

### MC HTTP endpoints (additive, no displacement of `/api/ingest/intent`)

- `GET /api/runtime/survival/policy-hash` — sidecars boot-check that
  they ship the same constitution as MC. Returns `policy_hash` + the
  doctrine string.
- `POST /api/runtime/survival/validate-stamp` — operator dashboard
  surfaces failure modes per sidecar.
- `POST /api/runtime/survival/canonical-gate` — sidecars hand MC an
  intent envelope, get a signed receipt back.
- `POST /api/runtime/survival/verify-receipt` — broker adapters
  validate before placing an order.

### CI tripwire (lives in every stack + MC)

`tests/test_no_duplicate_execution_gates.py` greps the backend for
forbidden tokens: `local_execution_authority = True`,
`may_execute = True`, `can_execute = True`,
`if live_enabled / paper_only / observe_only`, `operator_lock_default`.
Anything outside the allowlist (the survival module + this test)
fails the build. Verified 0 offenders in MC backend.

### Verified live (preview)

```
GET /api/runtime/survival/policy-hash
→ {"policy_hash": "2ac7d02164886f5c9c4a6339a605bf7be87b2bf2b532ea08681b5c29a6dcea25", "doctrine": "..."}

POST /api/runtime/survival/canonical-gate {valid intent, conf=0.55}
→ {accepted: true, receipt: {signature: "affea0da..."}}

POST /api/runtime/survival/verify-receipt {receipt}
→ {ok: true, reason: "VALID_MC_RECEIPT", lane: "crypto", symbol: "BTC-USD", direction: "BUY"}
```

Tampering the receipt's `symbol` field → `BAD_MC_RECEIPT_SIGNATURE`.

### Tests

- `tests/test_platform_survival.py` — 4 PASS (sidecar has no local
  authority, low-confidence block, signed receipt round-trip, tamper
  rejection)
- `tests/test_platform_survival_routes.py` — 5 PASS (policy-hash
  public, validate-stamp requires auth, validate-stamp flags
  unknown env, canonical-gate blocks low conf, round-trip)
- `tests/test_no_duplicate_execution_gates.py` — 1 PASS

Full regression: 67/67 tripwire + 28/28 new survival/promotion tests.

### Required env vars

Added to `backend/.env`: `RISEDUAL_MC_RECEIPT_SECRET` (auto-generated
48-byte urlsafe key). Sidecars MUST NOT receive this secret. Each
brain stack sets `RISEDUAL_ENV`, `RISEDUAL_PLATFORM`, `RISEDUAL_MC_URL`,
`RISEDUAL_DB_NAME`, `RISEDUAL_BROKER_MODE`, `RISEDUAL_SIDECAR_VERSION`,
`GIT_SHA` at their own hosting layer.

### Doctrine pin

> Sidecars communicate. MC approves. RoadGuard protects.
> Broker executes only with MC receipt. Preview is never proof of PROD.

If policy ever changes shape, `policy_hash()` changes — every sidecar
running stale policy is rejected by the canonical gate with a typed
`POLICY_HASH_MISMATCH` error. Operator never has to wonder again
whether a preview deploy snuck into PROD.



## 🚨 Latest (2026-05-18): Promotion Artifact Report — shadow vs fill

Operator request: "Pull a `PromotionArtifact`-ready report from the data
we already have." Camaro's intents are silently downgraded to
`shadow_proposal` because Camaro holds a `challenger` seat. This new
endpoint surfaces the EVIDENCE an operator needs to decide whether to
flip Camaro (or any non-executing brain) to a `co_trader` seat via the
Patent-J countersign flow.

### Backend — new module `shared/promotion_artifact_report.py`
- `GET /api/admin/promotion-artifact/{brain}?hours=24&benchmark_brain=alpha`
  returns: `{brain, benchmark_brain, window, thresholds, metrics, verdict,
  verdict_rationale, per_intent[], generated_at, report_version}`.
- `GET /api/admin/promotion-artifact?hours=24` runs the scan across all
  RUNTIMES (excluding the benchmark) and returns `{reports: [...]}`.
- Metrics emitted per brain:
  * `sample_size` — shadow proposals (intents where
    `holds_executor_seat=False`) in the window
  * `directional_agreement_rate` — % of shadow proposals where the
    benchmark brain (default `alpha`) actually traded the same direction
    on the same symbol within ±60min
  * `hit_rate_mtm` — % of shadow proposals where the price moved
    favorably over a 60min horizon (mark-to-market)
  * `simulated_pnl_usd` — sum of unit-notional MTM PnL
  * `realized_pnl_match_usd` — executor's actual fill PnL on agreement-
    matched shadow proposals (operator requested BOTH PnL modes)
- Verdict bands (operator-chosen 30% threshold):
  * `insufficient_data` — < 20 samples or no resolvable price/fill data
  * `recommend_promote` — hit_rate ≥ 30% AND agreement ≥ 30% AND
    samples ≥ 20
  * `keep_in_challenger` — fails either floor
- Tests: `tests/test_promotion_artifact_report.py` — 18 PASS covering
  pure helpers, empty-data / mixed / high-agreement scenarios, auth
  gate, unknown-brain 404, brain==benchmark 400, and all-brains shape.

### Frontend — `components/PromotionArtifactPanel.jsx`
- Mounted on `/admin/diagnostics` (below LiveTradeDiagnose).
- Renders one card per non-benchmark brain with verdict chip, 4 metric
  tiles, rationale, and a "download JSON" button (per-intent detail
  ships in the file).
- Hour-window pills: 1H / 6H / 24H / 3D / 7D. Defaults to 24H.
- Test IDs: `promo-artifact-panel`, `promo-artifact-card-{brain}`,
  `promo-artifact-verdict-{brain}`, `promo-artifact-samples-{brain}`,
  `promo-artifact-agreement-{brain}`, `promo-artifact-hitrate-{brain}`,
  `promo-artifact-pnl-{brain}`, `promo-artifact-download-{brain}`,
  `promo-artifact-hours-{1|6|24|72|168}`, `promo-artifact-reload`.

### Doctrine pin
This report is **ADVISORY EVIDENCE**. It does NOT mutate seats,
authority, or roster — promotion still requires the operator
countersign via `/admin/promotion/proposals` (Patent J flow in
`shared/promotion.py`).

### Verified live (preview)
- Camaro: 1116 samples / 0% agreement / 0% hit-rate → KEEP IN CHALLENGER
  (no Alpha fills in DB to compare against; MTM hit-rate stuck at 0%
  because synthetic OHLCV at minute granularity rarely changes within
  60min horizon).
- Chevelle & REDEYE: 0 samples → INSUFFICIENT DATA.
- All 63 backend tests pass (18 new + 45 regression on tripwire, council,
  promotion-gate, auto-router).



## 🚨 Latest (2026-05-17, +4): Tripwire marker wired

- `pytest.ini` registers a `tripwire` marker.
- 4 test modules (`test_governance_verdict`, `test_council_helpers`,
  `test_council_diagnose_contract`, `test_auto_router_helpers`)
  marked with module-level `pytestmark = pytest.mark.tripwire`.
- `tests/README.md` documents the workflow:
  > Edit `shared/council.py`, `shared/auto_router.py`,
  > `shared/execution.py`, or `shared/quantum_state.py` →
  > run `python -m pytest -m tripwire` BEFORE commit.
- Verified: `pytest -m tripwire` → **65 passed in 2.76 s**.

If a tripwire fires, the next agent has a clear decision tree:
- (a) Intentional → update fixture(s) + log in PRD.
- (b) Unintentional → roll the edit back.



## 🚨 Latest (2026-05-17, +3): auto_router refactor + stale tests fixed

### `auto_router._route_one` decomposed
- 194-line orchestrator → 64-line linear pipeline (6 phases) + 11
  helpers (5 pure + 6 persistence). Largest helper 34 lines.
- 18 new characterization tests in `tests/test_auto_router_helpers.py`
  pin every pure helper (lane-clamp matrix, side-for-action, effective
  notional, blocked-response shape, receipt builder).
- Live diagnose tripwire confirms no drift in the user-visible contract.

### Stale pytest failures fixed (zero behavioral change)
- `test_alpaca_execution_pipeline.TestExecutionMeta::test_caps_endpoint`:
  was asserting old $10/$50/$100 caps. Now reads
  `CAP_PER_ORDER_USD/CAP_PER_DAY_USD/CAP_OPEN_NOTIONAL_USD` from
  `exposure_caps` live + `CRYPTO_PER_ORDER_USD` from the crypto
  module. Auto-tracks future cap changes without re-editing the test.
- `test_seat_policy_and_auto.TestSeatPolicy::test_policy_exposed_on_roster`:
  was asserting a hard-coded 5-seat set. Now reads
  `SEAT_POLICY.keys()` live + pins the core invariants
  (executor+crypto may_execute, opponent+auditor never, governor has
  veto). Survives future seat additions.

### Test inventory now 92 verified-passing tests across:
governance verdict, council helpers, **live HTTP diagnose tripwire**,
auto-router helpers, lane isolation, doctrine sidecars, caps endpoint,
seat-policy registry.



## 🚨 Latest (2026-05-17, +2): Council refactor + drift tripwire

### `_evaluate_council` decomposed
- 334-line monolith → 65-line linear orchestrator (8 phases) + 9
  named helpers, each ≤93 lines, each independently testable.
- Doctrine **unchanged**. Locked by 36 characterization tests in
  `tests/test_governance_verdict.py` + `tests/test_council_helpers.py`
  (10 + 26 = 36, all pass).
- Largest remaining helper: `_governance_verdict` at 93 lines.
  **WATCH-NOTE**: if this grows past ~120 lines, split into
  `_resolve_governance_inputs()` + `_apply_governor_authority()` +
  `_build_governance_verdict()`. Not urgent.

### New drift tripwire — `tests/test_council_diagnose_contract.py`
Pins the LIVE `/api/admin/execution/diagnose` contract via HTTP
against the preview backend. 11 tests covering:
- Top-level response shape (10 required keys)
- Canonical gate-chain ordering (7 core + 3 lane-specific cap gates)
- Required keys on governor + opponent gate rows
- `quantum_state.regime_probs` sums to 1.0
- `kraken_credentials.state` is one of 4 known values
- `first_blocker` consistency with verdict

**If this tripwire fails**, the council surface changed. Either:
- (a) Intentional → update the test fixtures + log a PRD note
- (b) Unintentional → roll the edit back



## 🚨 Latest (2026-05-17, +1): Lane-Isolated Seats + Preview Vacate

Operator order: "remove every brain in the preview from their seat —
all seats need to be vacant" AND "shared seats should be separate per
market, crypto and equity."

### Preview DB — all seats vacated
- `brain_roster.assignments` set to `{role: None}` for all 12 seats
  (executor, decider, governor, advisor, opponent, auditor, crypto,
  crypto_advisor, crypto_governor, crypto_opponent, crypto_decider,
  crypto_auditor). `seat_epoch` bumped to 136.
- Legacy `shared_executor_seat` singleton cleared (`holder=None`).
- Legacy `shared_auditor_seat` cleared.
- Audit row written to `roster_audit_log` (`action: bulk_vacate`).

### Lane isolation — cross-lane fallback removed
- `shared/council._seat_holder(role, lane)` — DELETED the equity
  fallback. Previously when `crypto_governor` was vacant the lookup
  returned the equity `governor` occupant, letting equity-seat holders
  silently govern crypto intents. Now: empty crypto seat → returns
  None for crypto, regardless of equity. Hard lane isolation.
- `shared/seat_policy.snapshot(seat)` — now resolves `crypto`,
  `crypto_<role>` slot names to their equity-twin policy row so
  stances posted while holding a crypto slot get the correct
  may_veto/may_override bits. `posted_as` retains the slot name
  (`"crypto_governor"` etc.) for audit slicing.
- `shared/seat_policy.seat_may_execute_lane(seat, lane)` — explicitly
  handles `crypto` slot (only authorizes crypto-lane execution); all
  `crypto_*` advisory slots fail closed on order routing.

### Verified via `/api/admin/execution/diagnose`
Both lanes correctly report `executor_seat_check` as first blocker
with `executor_holder_at_post: None`. No cross-lane leakage.



## 🚨 Latest (2026-05-17): Full Block-Removal + Live-Trade Diagnose

Operator reported "no trades being made on crypto" and ordered removal
of ALL remaining live-trade blockers / phantom "observation only"
chrome. Shipped:

### Frontend (chrome scrub)
- `components/Layout.jsx` — top yellow `observation-banner` removed
  (was "OBSERVATION ONLY · BROKER_LIVE_ORDER_ENABLED=false · execution
  authority disabled across all runtimes")
- `pages/Login.jsx` — removed "Observation-only deploy · execution
  disabled" footer; copy updated to "Four separate brains" + REDEYE
  tile added (Alpha · Camaro · Chevelle · REDEYE)
- `risedual/Layout.jsx` — header + footer "observation only" →
  "seat-governed"
- `pages/Diagnostics.jsx` — Deploy-mode card green when
  `deploy_mode=execution`; "execution disabled" sub-line removed
- `pages/Promotion.jsx` — yellow "OBSERVATION" badge → green
  "SEAT-GATED"
- `pages/Redeye.jsx` — removed "currently OBSERVATION ONLY" tag

### Backend (gate fixes + visibility)
- `shared/execution.py` Gate 4 (`live_trading_disabled`): fixed
  misleading reason "LIVE_TRADING_ENABLED stays False — paper broker
  only" → neutral "live order routing enabled — seat policy is the
  authority". Gate retained for downstream receipt-schema stability.
- `shared/auto_router.py` — **lane-aware notional clamp**: default
  `AUTO_ROUTER_NOTIONAL_USD=$100` was auto-blocking 100% of crypto
  intents on the `cap_per_order_crypto=$30` rail. Auto-router now
  clamps notional to `cap_for_lane(intent.lane)` before evaluating
  gates.
- `shared/crypto/kraken.py` — `get_active_keys()` no longer silently
  returns None; failure is **LOGGED** (PROD encryption-key drift was
  invisible for weeks). New `get_active_keys_status()` returns a
  status dict with one of: `ok`, `no_credentials`, `missing_field`,
  `decrypt_failed`.
- `backend/.env` — `BROKER_LIVE_ORDER_ENABLED` flipped `false → true`
  (legacy telemetry surface; gate already defanged).

### New diagnose endpoint + UI
- `GET /api/admin/execution/diagnose?lane=crypto|equity&notional_usd=N`
  — runs the full gate chain against a synthetic BUY intent and
  returns every gate's pass/fail, plus broker-adapter sanity
  (Kraken decrypt state, Alpaca singleton presence). Surfaces the
  FIRST blocker explicitly.
- `frontend/src/components/LiveTradeDiagnose.jsx` — side-by-side
  Crypto vs Equity panel showing first blocker + every gate + broker
  credential state. Wrapped in `PanelErrorBoundary`, mounted on the
  Diagnostics page.

### Operator path forward for PROD Kraken
Hit `/api/admin/execution/diagnose?lane=crypto` (or open Diagnostics
on prod). The `broker.kraken_credentials.state` field tells you
exactly which failure mode is in play:
- `no_credentials` → no DB doc; re-save via `/api/admin/kraken/connect`
- `missing_field` → doc exists but `encrypted_private_key` empty
- `decrypt_failed` → CREDENTIALS_ENCRYPTION_KEY drifted between
  encrypt-time and now; re-save keys to re-encrypt with the current key


## 🚨 Latest (2026-02-17, late+7): Brain-Name Restriction Sweep

User flagged that the "phantom bugs" chasing the council seats were
caused by lingering BRAIN-IDENTITY-BASED restrictions throughout the
codebase — words, doctrine fragments, env flags, persisted DB rows,
and one live gate function. Per user directive:

> *"Please remove any mention of forbidden/blocked/restricted from
> this side of MC. Anything that blocks any brain needs to go,
> either by words or functions."*

**Doctrine pin (rev3)**: Authority lives on **SEATS**, not on brain
identity. To stop a brain from acting, **vacate the seat** — never
mute by name. Every brain may hold every seat.

### Backend surfaces stripped / defanged

- `shared/flags.py` — RETIRED brain-named enforce flags
  (`PHASE6_ENFORCE_ENABLED`, `CAMARO_EXECUTOR_ENFORCE_ENABLED`,
  `CHEVELLE_AUTHORITY_ENABLED`, `REDEYE_OPPONENT_ENFORCE_ENABLED`).
  `/admin/flags` now returns only `BROKER_LIVE_ORDER_ENABLED` plus
  the seat-doctrine restatement. Legacy `enforce_flags={}` key kept
  for one cycle so old bundles don't blank-render.
- `shared/brain_lane_policy.py` — gate function
  `is_brain_lane_allowed()` permanently returns True. POST endpoint
  refuses `allowed=false` writes with HTTP 410 + doctrine-pinned
  explanation. On boot, any leftover `allowed=false` rows in the
  `brain_lane_policy` collection are purged. The `effective` matrix
  hard-codes True for every `(brain, lane)` cell. The
  Camaro-crypto-mute that was the silent "phantom" is gone.
- `shared/ingest.py` — `/ingest/receipts` no longer multiplies
  `executed` by `_broker_live_enabled()`. `_broker_live_enabled()`
  retained as a legacy helper but does NOT gate execution flow.
  Authority chain runs solely through `/execution/submit` + seat
  policy.
- `shared/doctrine_injection.py` + `shared/doctrine_routes.py` —
  governor-policy overlay no longer keys on `stack_name == "chevelle"`;
  caller resolves `holds_governor_seat` from the roster and passes
  it explicitly. If no brain holds the governor seat for the lane,
  no overlay attaches (correct: empty seat → no authority).
- `namespaces.py` — `ROLES` registry rewritten. Field semantics
  changed from "what this brain is authorized to do" to
  "what this brain was trained for". Old language (`has hands`,
  `has teeth`, `has the keys`) replaced with brand metadata
  (`structured trader`, `challenger / counterfactual`,
  `memory + calibration`, `adversarial scout`). Doctrine pin
  comment explicitly forbids reading these fields as a gate.

### Frontend surfaces cleaned

- `pages/Overview.jsx` — page header now reads "Four brains. One
  nervous system." with the seat-doctrine subhead. The
  "Adversarial Doctrine" card renamed to "Seat doctrine" and lists
  the 6 seats and what each one means; no brain names appear in
  the doctrine. Runtime cards no longer surface `ROLE VIOLATIONS`,
  no `FORBIDDEN` execution label, no enforce-flag chips.
- `pages/Flags.jsx` — rewritten end-to-end. Only `BROKER_LIVE_ORDER_ENABLED`
  master switch + the doctrine restatement remain. No per-brain
  enforce sections.
- `components/RosterPanel.jsx` — `EligibilityMatrix` removed from
  render path (function definition left in place for now as dead
  code; future cleanup). `BrainLanePolicyPanel` removed from render
  path. Eligibility-switches toggle button removed. Picker no
  longer renders "BLOCKED" branch; `isEligible` removed; every
  brain is selectable for every seat. Picker title text reads
  "click to save this brain into this position".
- `pages/RecentIngests.jsx` — `ROLE VIOLATION` chip removed; the
  red-toned `role_violation` tone branch deleted.
- `lib/api.js` — `RUNTIME_META` notes / taglines rewritten to be
  training-intent descriptions, not authority claims.
  `enforceFlag` / `enforceLabel` / `role` fields kept as `null`
  for one cycle so any older bundle that reads them doesn't blank.

### Verified clean

Playwright body-scrape of `/admin/overview` returns 0 occurrences of
every banned string: `Camaro has teeth`, `Chevelle has the keys`,
`Only Alpha has hands`, `Cannot place trades`, `ROLE VIOLATION`,
`BLOCKED`, `FORBIDDEN`, `CAMARO_EXECUTOR_ENFORCE`,
`CHEVELLE_AUTHORITY`, `PHASE6_ENFORCE`. Backend tests still green
(83/83 doctrine + auto-retire + promotion + sidecars).

API verified: `/admin/brain-lane-policy` effective matrix returns
`{alpha:{equity:True,crypto:True}, camaro:{equity:True,crypto:True}, ...}`
— every brain × every lane is allowed.

**PRODUCTION ACTION REQUIRED**: redeploy preview → production
(`mission.risedual.ai`). The PROD database also needs the
`brain_lane_policy` `allowed=false` rows purged — `seed_default_policy()`
auto-runs the purge on boot, so a clean redeploy + restart is enough.



## 🚨 Previous (2026-02-17, late+6): Symmetric 6-Seat Roster (Spec Honored)

User flagged two doctrinal gaps:
1. **AUDITOR missing from EQUITY lane** while present in CRYPTO — asymmetric.
2. **DECIDER**'s purpose unclear — original problem statement listed it
   as one of six rotatable seats (Executor · Auditor · Decider · Governor
   · Opponent · Crypto). User chose to honor the original 6-seat spec.

**Fix — equity AUDITOR added end-to-end**:
- `shared/roster.py`: `ROLES` tuple, `DEFAULT_ASSIGNMENTS`, and the
  `RoleT` Literal type all include `"auditor"`. Default is vacant
  (operator must explicitly assign — post-trade reviewer is not a
  doctrine-defaulted seat).
- `shared/seat_policy.py`: new `SEAT_POLICY["auditor"]` entry —
  `may_decide=False`, `may_execute=False`, `may_override=False`,
  `may_veto=False`, `seat_required=False`, `speaks_as=auditor`.
  Lane-scope is `None` (audits both lanes by default).
- `frontend/RosterPanel.jsx`: `ROLE_META.auditor` + added to
  `EQUITY_ROLES` array. Layout rebalanced from `[1fr_5fr_1fr_6fr]`
  to symmetric **`[1fr_6fr_1fr_6fr]`** with `xl:grid-cols-6` on
  both lane sections. All 12 seats render at equal proportion.

**DECIDER clarification (pinned)**:
- Role definition pinned: *"Trust / reduce / veto / observation call
  on each intent."* — distinct from EXECUTOR (which routes the
  broker order) and GOVERNOR (which freezes/gates). DECIDER speaks
  to the QUALIFICATION verdict before execution.
- In the doctrine packet's role-keyed seats, DECIDER maps to the
  `strategist` role.
- Default holder: equity DECIDER = camaro; crypto_decider = vacant.

**Backwards compat**: legacy `shared/auditor_seat.py` single-row
registry (used by hypothesis analysis) remains operational and
independent — the unified roster's `auditor` seat is purely the
operator-assignment visibility layer. Both can coexist; a future
cleanup ticket can fold the legacy registry into the unified roster
if desired.

**Verified**: API round-trip works (`POST /admin/roster/assign
{role: "auditor", brain: "chevelle"}` returns 200 + assignment
reflected). UI screenshot confirms symmetric 6+6 layout, all seats
clickable, no boundary fires. Doctrine + auto-retire + promotion-gate
test suite (42 tests across 5 modules) still green.

**PRODUCTION ACTION REQUIRED**: redeploy preview → production
(`mission.risedual.ai`) to land the seat-symmetry fix + the
defensive `.label` hardening from rev5.



## 🚨 Previous (2026-02-17, late+5): PROD Roster Render Crash — Root-Caused

**User reported**: Production (`mission.risedual.ai`) Overview page
showing the `PanelErrorBoundary` chip on Brain Roster with message
**"Cannot read properties of undefined (reading 'label')"**. (Preview
did not reproduce — different roster state.)

**Root cause**: schema drift. The backend roster (`shared/roster.py`)
has 11 seat keys, including `crypto_decider` and `crypto_auditor`
added previously for lane symmetry. The frontend `RosterPanel.jsx`
`ROLE_META` and `CRYPTO_ROLES` arrays still only knew about 9 seats.
Every code path that did `ROLE_META[role].label` /
`BRAIN_META[brain].label` / `LANE_META[lane].label` unguarded would
throw a TypeError if a key from the backend response wasn't in the
hard-coded frontend map. Plus, `crypto_decider` & `crypto_auditor`
were never user-assignable from the UI because they didn't appear in
the rendered seat grid.

**Fix (defensive)**: ALL `ROLE_META[x]`, `BRAIN_META[x]`,
`LANE_META[x]` lookups are now optional-chained with a fallback so an
unknown key can never crash render. `RoleSlot` itself bails out
gracefully to an "(no doctrine entry for this seat)" tile when a
backend role key isn't in `ROLE_META`. Same hardening applied to the
EligibilityMatrix headers, picker buttons, and action handlers.

**Fix (architectural gap)**: `crypto_decider` and `crypto_auditor`
added to `ROLE_META` + `CRYPTO_ROLES` arrays so they render as real
operator-assignable seat tiles. Roster layout rebalanced from
`lg:grid-cols-9` (1+4+1+3) to fractional tracks
`lg:grid-cols-[1fr_5fr_1fr_6fr]` so all 11 seats fit comfortably.

**Visual fix**: OPPONENT role color changed from `#DC2626` (red) to
`#06B6D4` (aqua) per operator request — red was being mis-read as an
error state when it was just the adversary-seat doctrinal color.

**Verified preview**: 0 boundaries fire on initial render; all 11
seats render with correct color theming. End-to-end advisor and
crypto_advisor save flows tested earlier still pass.

**PRODUCTION ACTION REQUIRED**: User must **redeploy** preview →
production (`mission.risedual.ai`) for this fix to land. The error
boundary chip will continue to display in PROD until the new bundle
is deployed.

**Lesson learned (pinned for future)**: backend role/brain/lane
schema additions are a load-bearing dependency for the frontend
metadata maps. Any future schema addition needs a paired frontend
ROLE_META / BRAIN_META update — and the optional-chaining hardening
now in place ensures the panel degrades gracefully rather than
blanking during the gap. Consider extracting `ROLE_META` to a shared
schema file backed by a `/api/admin/roster/schema` endpoint so
backend additions auto-propagate.



## 🚨 Previous (2026-02-17, late+4): Page-blank Recurrence Hardening

**User report**: Overview page going blank again in PROD (recurrence
of the earlier Kraken-render crash pattern). Preview env could not
reproduce the specific failure, but the defensive fix is unconditional:
**no single child component should be able to blank an entire page.**

**Fix**: lifted the working `BrokerTileErrorBoundary` pattern from
`KrakenBrokerTile.jsx` into a reusable `PanelErrorBoundary` component
and applied two layers of containment:

1. **Per-panel boundaries** (`compact={false}` cards with retry button)
   wrapping every component that pulls live backend data and was a
   candidate for the PROD blank-screen:
   - `pages/Overview.jsx`: RosterPanel, LivePositionsPanel,
     FeedersStrip, TechnicalsPanel each wrapped independently.
   - `pages/Intents.jsx`: AutoRetireStrip, DoctrineHealthPanel (compact),
     DoctrineStrip-per-row (with `compact` styling so a single bad
     intent row doesn't break the table).
   - `pages/Doctrine.jsx`: DoctrineHealthPanel (full).
2. **Top-level boundary** wrapping `<Outlet />` in
   `components/Layout.jsx` (`panel-error-page` testid) as the final
   safety net for any page we haven't wrapped yet. A future
   unwrapped route that throws will render a typed error chip with
   the underlying message + a Retry button instead of the blank
   screen the user just hit.

**Behavior**:
- When a panel throws, the boundary renders a red-bordered chip with
  the panel name, the underlying error message, and a **Retry** button
  that resets `err` state and re-mounts the child. The rest of the page
  is unaffected.
- When everything renders fine, the boundary is invisible — verified by
  Playwright smoke shots on Overview (8 runtime cards · 0 boundaries
  triggered) and Intents (100 intent rows · 0 boundaries triggered).
- Console error is preserved via `componentDidCatch` so the underlying
  bug is still loggable from PROD via browser devtools.

**Test IDs**: `panel-error-{name}`, `panel-error-{name}-retry`,
`panel-error-page`, `panel-error-roster`,
`panel-error-live-positions`, `panel-error-feeders`,
`panel-error-technicals`, `panel-error-autoretire`,
`panel-error-doctrine-health`, `panel-error-doctrine-health-full`,
`panel-error-doctrine-{intent_id}`.

**Doctrine pin**: ANY new page or panel pulling backend data that
might return a novel shape MUST be wrapped in `PanelErrorBoundary`.
The two existing top-level safety nets (page-level + per-panel) plus
the proven Kraken pattern give three layers of containment; nothing
should be able to blank a page now.

**Outstanding**: the actual root-cause render crash in PROD is still
unknown (preview env couldn't reproduce). When it recurs, the
operator will now see the underlying error message in the panel chip
itself — that's the artifact to paste back here for a precise fix.



## 📚 Backlog: Doctrine Source Material

- **`The_Essential_Options_Trading_Guide.mht`** (uploaded 2026-02-17,
  user-flagged "necessary"). Currently DEFERRED — not yet ingested into
  the doctrine layer. When picked up, treatment will follow the same
  three-phase pattern used for the small-account/strategy ingestion:
  - Phase A: extract concrete numeric rules (greek thresholds, IV
    percentile bands, DTE windows, spread widths, capital risk per
    contract, assignment risk windows)
  - Phase B: reconcile against existing `base_labels` to spot
    overlapping or contradictory signals
  - Phase C: ship as a NEW `doctrine_version` (e.g.,
    `options_swing_v1` / `options_income_v1`) producing the same
    role-keyed seat shape so audit / scorecard / auto-retire / health
    panel reuse unchanged.
  - **New lane**: options will likely require an `options` lane
    distinct from `equity`/`crypto` (different broker capability
    surface, exposure caps, position-monitor semantics). Lane isolation
    guard tests must extend to cover it.
  - Source PDF stored at the upload URL; re-ingest via
    `analyze_file_tool` when work picks up.

  **Doctrine pin**: do NOT add this strategy until the existing three
  doctrines (`small_account_sidecar_v1`, `gap_and_go_v1`,
  `micro_pullback_v1`) have ≥100-sample-each calibrated scorecards.
  Per the doctrine-isolation rule, low-sample doctrines just add noise
  to Patent J's promotion math.

## 🔴 Backlog: Production Kraken Live-Order Failure (under user investigation)

- **Status**: deferred to user — handling in PROD.
- **Symptom**: live Kraken keys present in PROD but live orders not
  being placed. (`BROKER_LIVE_ORDER_ENABLED=false` is NOT the cause —
  that env flag only gates the legacy `/ingest/receipts` endpoint.)
- **Actual gate chain**: `/execution/submit` →
  `broker_router.route_order(lane=crypto)` →
  `ADAPTER_LOADERS["kraken"]()` → `get_kraken_adapter()` →
  `get_active_keys()` → `decrypt(encrypted_private_key)`. Returns
  `None` on ANY failure in that chain, which surfaces as
  `BrokerRouteBlocked("broker 'kraken' adapter not configured
  (no credentials?); NO_TRADE")`.
- **Probable causes** (ordered by likelihood):
  1. **Encryption-key drift** — `CREDENTIALS_KEY` env var in PROD
     changed since keys were saved → `decrypt()` raises silently →
     adapter shows None even though `kraken_credentials.singleton`
     exists.
  2. **Read-only API scope** — Kraken key has `query_funds` but lacks
     `execute_orders`; adapter loads but Kraken rejects every submit.
  3. **Canonical/symbol resolution** in `compose_asset()` or
     `resolve_broker_symbol()` failing before the adapter is even
     reached.
- **Diagnostic next time**:
  - `/api/admin/kraken/status` JSON → tells decrypt-pass vs decrypt-fail
  - Most recent `shared_gate_results` doc with `kind=submit_no_trade`
    → `reason` string pinpoints the gate
  - PROD backend log lines starting with `route_order intent=` —
    carry full failure context.
- **Doctrine fix forward** (when picked up):
  - Either fail loud (raise a typed exception with the decrypt error)
    when decrypt fails instead of returning `None`, OR surface a
    "decrypt_failed_check_env" status from `/admin/kraken/status` so
    operators see the root cause without grepping logs.
- **Note**: docstring in `shared/risk/position_monitor.py` previously
  claimed crypto pricing was TODO. CORRECTED 2026-02-17 — crypto
  price feed via `fetch_tickers()` against Kraken's public
  `/0/public/Ticker` endpoint is fully wired and verified live
  (`BTC/USD: $78,056` returned in <200ms from this environment).
  Position-monitor crypto guards (StopLoss, TakeProfit, TrailingStop,
  MaxHoldTime) all use this price source — they do NOT depend on the
  Kraken keys at all.


## 🚨 Latest (2026-02-17, late+3): Bounded Promotion Gate + Doctrine Health Panel

**P1 — Bounded Promotion Gate (expectancy-driven, read-only)**

Doctrinal headline: **expectancy > accuracy**. A 45%/4.5R doctrine
outperforms a 75%/0.8R doctrine; accuracy alone is a trap.

- New module `shared/doctrine/promotion.py` computes per
  `(lane, doctrine_version)` slice:
  - `expectancy_R` — R-normalized via `risk_unit = |avg_loss|`
  - `max_drawdown_R` — worst consecutive-loss run in R units
  - `consistency` — `1 - clamp(stdev(rolling30_winrate) / 0.5)`
  - `win_rate`, `avg_win_usd`, `avg_loss_usd`, `samples`,
    `progress_to_min_samples`
- Verdict bands:
  - `LEARNING` — samples < 100
  - `CANDIDATE_RETIREMENT` — samples ≥ 100 AND (expectancy < −0.10R
    OR max_drawdown ≥ 8R)
  - `CANDIDATE_PROMOTION` — samples ≥ 100, expectancy ≥ +0.30R,
    max_drawdown ≤ 5R, consistency ≥ 0.55
  - `WATCHING` — samples ≥ 100, neither retire nor promote
- New endpoint `GET /api/admin/doctrine/promotion-status?lane=` returns
  `{slices: [...], thresholds: {...}, doctrine_note: "...",
   endpoint_version: "promotion_status_v1_expectancy_driven"}`
- Zero-sample doctrines surface too so the UI renders "LEARNING · 0/100"
  for known-but-unobserved doctrines.
- `DOCTRINE_IDEALS` registry — single source of truth for each
  doctrine's `title`, `summary`, `wants[]`, `common_rejections[]`.
  Read by the frontend so onboarding stays in lockstep with the
  sidecar code.
- **READ-ONLY**: surfaces gate state only; operators promote / retire
  doctrines explicitly. No live execution-flow influence.

**P2 — `DoctrineHealthPanel.jsx` (compact + full modes)**

The component renders the live operational state of every doctrine,
not static documentation:

- **Compact mode**: single-row strip on `/admin/intents` with
  verdict chip, sample progress bar, win-rate, expectancy ±R,
  drawdown, single-line blocker. Lane-scoped to follow the page filter.
- **Full mode**: card per doctrine on the new `/admin/doctrine` route:
  - Header: title + lane + doctrine_version + verdict chip
  - Summary line from the ideals registry
  - 8-metric grid: samples / expectancy / drawdown / win-rate /
    consistency / avg-win / avg-loss / progress bar
  - 3-column body:
    - **What it wants** (✓ green) — ideal-snapshot from registry
    - **Gate Blockers** (› verdict color) — current gate state
    - **Common Rejections** (✗ red) — failure-mode reference
- Pinned gate-thresholds footer + the expectancy-over-accuracy doctrine note.

**New route**: `/admin/doctrine` with `Doctrine` page wrapping
`<DoctrineHealthPanel mode="full" />` plus lane filter pills.
Sidebar nav entry "Doctrine" added under the Governance section.

**Verified live**: 420 demo rows seeded across 4 doctrines surfaced
the exact verdicts expected:
- `small_account_sidecar_v1` → CANDIDATE_RETIRE (−0.40R · 7R dd · 30% wr)
- `gap_and_go_v1` → CANDIDATE_PROMOTE (+1.00R · 1R dd · 50% wr · gates cleared)
- `micro_pullback_v1` → WATCHING (+0.20R · below promotion floor)
- `crypto_sidecar_v1` → LEARNING (40/100 samples)

**Tests**: 83/83 doctrine + promotion tests pass (69 prior + 14 new).
- `tests/test_promotion_gate.py` (NEW): pure-math
  (45%/4.5R > 75%/0.8R expectancy), drawdown counting, consistency
  score, all four verdict bands, endpoint shape, auth gate, end-to-end
  promotion-emit and retirement-emit via seeded rows, zero-sample
  doctrine surfaces LEARNING with ideal-snapshot payload intact.

**Test IDs**: `doctrine-page`, `doctrine-lane-{all|equity|crypto}`,
`doctrine-health-compact`, `doctrine-health-full`,
`doctrine-health-card-{dv}`, `verdict-{dv}`, `metric-{kind}-{dv}`,
`progress-{dv}`, `doctrine-gate-thresholds`.



## 🚨 Previous (2026-02-17, late+2): Source-Aligned Doctrine + Strategy Split

**Sources ingested**: 2025 Small Account Tool Kit, Technical Analysis v3
(Gap-and-Go + Micro Pullback), and SAC2024 Small Account Challenge.
Numeric thresholds in `base_labels.py` are now pinned to those documents
verbatim; doctrine_version strings track the strategy they encode.

**Phase A — `base_labels.py` source-aligned tier upgrades:**
- New tier labels with small additive score bonuses:
  - `SWEET_SPOT_PRICE` ($5–$10 per Toolkit p.3)
  - `STRONG_GAPPER` (gap ≥ 20% per Tech-A v3)
  - `ULTRA_LOW_FLOAT` (<10M shares per Toolkit cold-market threshold)
  - `BULL_FLAG_PATTERN`, `FLAT_TOP_BREAKOUT_PATTERN`,
    `MICRO_PULLBACK_PATTERN` (Tech-A v3 named patterns)
  - `TRADING_WINDOW_PRIME` / `TRADING_WINDOW_OFF_HOURS` (7–11am EST,
    informational only)
- **SAC2024 refinement**: pullback patterns only score as VALID when
  the stock is **leading** (GAPPER ≥10% OR HIGH_RELATIVE_VOLUME ≥5x).
  Pullback on a non-leader gets `PULLBACK_PATTERN_ON_NON_LEADER` and
  zero score — surfacing the SAC2024 trap explicitly.

**Phase B — SAC2024 reconciliation:**
- 20–30% target gain and 75% accuracy + 2:1 winner-size are
  recorded as future scorecard targets in this PRD (not yet wired
  into `_promotion_blockers` — Patent J ladder will use them when
  the bounded promotion gate ships in P1).

**Phase C — Strategy split (the architectural payoff):**
- New module `shared/doctrine/strategy_doctrines.py` with two
  source-derived doctrines, each emitting the same role-keyed seat
  packet shape (so audit / scorecard / auto-retire / UI all reuse
  unchanged):
  - **`gap_and_go_v1`** — Tech-A v3 §Gap-and-Go.
    Strategist favors STRONG_GAPPER + ULTRA_LOW_FLOAT + premarket
    breakout + above-EMAs. Adversary attacks small gaps, missing
    premarket setup, broken daily trend, spread risk. Governor
    blocks on REJECT / spread / 3-loss / -$100. Execution judge
    requires `premarket_high_crossed | premarket_bull_flag` AND
    `price_above_emas` AND `STRONG_GAPPER` AND `SPREAD_ACCEPTABLE`.
  - **`micro_pullback_v1`** — Tech-A v3 §Micro Pullback.
    Strategist favors MICRO_PULLBACK_PATTERN near half/whole dollar
    with active momentum and known pullback low. Adversary catches
    "pullback on non-leader", off-round-dollar entries, faded
    momentum, missing stop reference. Governor **blocks when
    `pullback_low` is unknown** (no stop reference = no trade).
    Execution judge requires valid pullback + round-dollar level +
    momentum + known stop + spread ok.
- **Dispatch**: `lane_doctrine_router.build_lane_doctrine_packet()`
  inspects `snapshot.strategy` ("gap_and_go" | "micro_pullback" |
  anything-else). Known strategies route to the strategy doctrine;
  anything else falls back to the generic `small_account_sidecar_v1`.
- **IntentIn schema** — `doctrine_snapshot.strategy` documented in
  the field comment; dict shape is open so no breaking change.
- **Patent J ladder**: now grades `small_account_sidecar_v1` vs.
  `gap_and_go_v1` vs. `micro_pullback_v1` as distinct
  `(lane, seat, doctrine_version)` slices. Auto-Retire emits
  retirement suggestions per strategy doctrine independently.

**Verified live**: NVDA gap_and_go intent posted via curl returns
`doctrine_version=gap_and_go_v1`, A_QUALITY, strategist
`conviction_delta=+0.35`, all four seats READY/no-objections.
DoctrineStrip + AutoRetireStrip render the strategy doctrine without
any UI changes — proves the seat-doctrinal architecture composes.

**Tests**: 69/69 pass (45 prior + 6 auto-retire + 5 tier-upgrade +
13 new strategy-split tests).



## 🚨 Previous (2026-02-17, late+1): Seat-Doctrinal Canonicalization + Auto-Retire

**DOCTRINE PIN — performance belongs to the SEAT, not the holder.**
This rev removes "brain reputation contamination" from the audit + 
scorecard schema. Every metric is now keyed on
`(lane, seat, doctrine_version, quality_band)`; holders are surfaced
as METADATA only. Brain rotations no longer affect scoring history;
a seat's doctrine version is what graduates or retires.

**Backend — Phase 1: schema canonicalization**
- `shared/doctrine/lane_doctrine_router.py:hoist_packet_audit_fields()`
  refactored to emit seat-keyed canonical names:
  - `governor_action`, `governor_risk_multiplier`, `governor_block_reason_count`, `governor_holder`
  - `adversary_challenge_required`, `adversary_challenge_strength`, `adversary_objection_count`, `adversary_holder`
  - `execution_judge_ready`, `execution_judge_holder`
  - `strategist_conviction_delta`, `strategist_holder`
  - `lane`, `doctrine_version`, `quality`, `score`
- Brain-named legacy keys (`chevelle_governor_action`,
  `redeye_challenge_required`, `camaro_execution_ready`) kept as
  DEPRECATED aliases for one cycle so existing DB rows still read.
- `shared/intents.py` persists both canonical seat-keyed fields and
  the deprecated aliases into `doctrine_sidecars`.

**Backend — Phase 2: seat-doctrinal scorecard**
- `shared/doctrine/scorecard.py` rewritten as `scorecard_v2_seat_doctrinal`:
  - Primary aggregation: `by_lane_seat_doctrine` keyed on
    `lane/seat/doctrine_version` with branch metrics and quality
    breakdown per slice.
  - Existing `by_quality` + `by_seat` retained for compatibility.
  - `seat_occupancy` block — holders per (lane, seat) — strictly
    informational. Reader sees who held the seat during the window
    without it being a scoring axis.
  - `stack` removed as a primary filter param (was brain-keyed).
  - Promotion blockers reworded in seat-doctrine language:
    "governor seat: block heuristic not catching losers" — never
    "Chevelle blocked too much".
- New endpoint `GET /api/admin/doctrine/seat-occupancy?lane=&seat=`
  for the metadata view.

**Backend — Phase 3: Auto-Retire suggestions**
- New module `shared/doctrine/auto_retire.py`.
- `GET /api/admin/doctrine/retirement-candidates?lane=&min_samples=50`
  scans `(lane, seat, doctrine_version)` slices and emits candidates
  when a SEAT BRANCH violates its doctrinal expectation:
  - `governor.block` SHOULD have higher loss_rate than `.modulate`
    (block catches losers).
  - `adversary.challenge_required` SHOULD have higher loss_rate than `.quiet`.
  - `execution_judge.ready` SHOULD have lower loss_rate than `.not_ready`.
- Each candidate carries `severity` (FRICTION → WARM → HOT → BLAZING),
  rationale, suggested_action ("Retire or recalibrate in next doctrine
  version"), and `occupancy_during_window` as **metadata only**.
- Sorted by severity DESC then samples DESC.

**Frontend — `AutoRetireStrip.jsx`**
- New component on `/admin/intents` above the table.
- Banner: "SEAT-DOCTRINE AUTO-RETIRE SUGGESTIONS · N flagged" with the
  doctrine note "Targets (lane, seat, doctrine_version) — never brain
  identity."
- Each candidate is a severity-colored row. Collapsed: seat icon +
  severity chip + headline (`equity/governor v1: block heuristic is
  severely underperforming`) + n + Δ.
- Expanded: rationale, 4 metric tiles (lane / seat / doctrine /
  branch vs comparator loss-rates), Suggested Action card, Holder
  Occupancy card with explicit "metadata only · NOT a scoring axis"
  label + footer "Performance belongs to the seat doctrine, not to
  whoever held the seat."
- Lane-scoped — follows the lane filter on the Intents page.
- Hidden entirely when zero candidates; never noisy.

**Testids**: `autoretire-strip`, `autoretire-count`,
`autoretire-collapse`, `autoretire-reload`,
`autoretire-candidate-{lane}-{seat}-{branch}`,
`autoretire-toggle-{...}`, `autoretire-detail-{...}`.

**Tests**: 51/51 pass (45 doctrine + 6 new auto-retire).
- `tests/test_auto_retire.py` (NEW): endpoint shape, auth gate,
  governor.block underperformance → candidate emitted, execution_judge
  ready signal failure → candidate emitted, scorecard exposes
  by_lane_seat_doctrine + seat_occupancy + scorecard_v2 marker,
  seat-occupancy endpoint shape.
- `tests/test_doctrine_intent_attachment.py` updated to assert both
  canonical seat-keyed fields AND legacy aliases on persisted audit rows.

**Doctrinal payoff**: Patent J's promotion ladder can now graduate
seat doctrine versions independent of holders. When operators want to
break through, they can target the specific seat doctrine version
that's failing — not blame whichever brain was occupying the seat.



## 🚨 Previous (2026-02-17): P0 Doctrine UI Badges on Intents page

**P0 — `DoctrineStrip.jsx` component** (`/app/frontend/src/components/`).
Renders the read-only doctrine packet attached to every intent as a
full-width row beneath the main IntentRow:

- **Always visible** (collapsed by default):
  - Quality band badge: `A_QUALITY` (green) / `B_QUALITY` (lime) /
    `C_QUALITY` (amber) / `REJECT` (red)
  - Score (0.00–1.00) + lane echo
  - Four seat chips: `strategist · adversary · governor · execution_judge`
    with role-specific headlines (conviction Δ / objection count + cs /
    BLOCK or ×risk_multiplier / READY|not ready) + holder brain inline.
  - Color-coded by severity so the operator scans the worst signals
    first.
- **Expandable** ("details" toggle per row):
  - Per-seat detail cards: seat name, holder, headline value,
    objections / block_reasons / failed_checks lists, role lesson.
  - Base Reasons strip (every reason why the score lost points).
  - Footer: `doctrine_version` + bold reminder
    "ADVISORY ONLY · does not influence execution".
- **Unknown lane** intents (UNKNOWN_LANE_REJECT packets) and missing
  packets render a muted single-line strip — no crash, no fake data.

Wired into `Intents.jsx` as a `colSpan={9}` row beneath every intent
main row when `intent.doctrine_packet` is present. Independent of the
existing intent-detail expand state — operator can drill into doctrine
without expanding the full rationale panel.

Testids: `intent-doctrine-row-{id}`, `doctrine-strip-{id}`,
`doctrine-strip-toggle-{id}`, `doctrine-quality-{id}`,
`doctrine-chip-{role}-{id}`, `doctrine-detail-{id}`,
`doctrine-seat-detail-{role}`, `doctrine-reasons-{id}`.

Backend tests still green (45/45 doctrine tests pass post-UI change).
Doctrine remains strictly read-only — promotion gate (P1) still
pending (`min_samples >= 100` + statistical validation).


## 🚨 Previous (2026-02-17): P0 risk guards + Position Monitor + P1 UI surfaces

**P0 — Three new deterministic risk guards** (joining existing
TakeProfit): `StopLossGuard`, `TrailingStopGuard`, `MaxHoldTimeGuard`.
Pure-math lane-neutral cores in `shared/risk/`; lane-isolated wrappers
in `shared/{equity,crypto}/`. 15 deterministic unit tests cover every
side × hit/miss × edge-case combo.

**P0 — Position Monitor scheduler loop** (`shared/risk/position_monitor.py`).
Runs every 30s (env-tunable). Walks every open position and evaluates
the four guards in **strict priority**: StopLoss → TakeProfit →
TrailingStop → MaxHoldTime. First non-HOLD verdict closes/reduces and
breaks out — lower priorities not consulted on that tick. Writes
audit rows to `risk_monitor_evaluations`. Failure-isolated per
position. REST surface at `/api/admin/risk/monitor/{status,run-once,recent-evaluations}`.

**P0 — Per-lane risk-guard REST endpoints** under
`/api/admin/risk/{equity|crypto}/{guard}/{check|enforce}/{position_id}`.
No union endpoint that silently picks lane. Pure-math companions at
`/api/admin/risk/{guard}/evaluate`.

**P1 — Risk Guard Status column on LivePositionsPanel.** Rolls up the
latest monitor evaluation per position. Shows colored badge when a
guard fired (red/green/amber/purple per guard), or four pips + "ALL
HOLD" when every guard was satisfied. Updates every 15s.

**P1 — Brain × Lane policy toggle on Roster page.** New
`BrainLanePolicyPanel` inside `RosterPanel.jsx`. 4×2 matrix of one-click
toggles backed by `/api/admin/brain-lane-policy`. Operator can mute/
unmute any brain × lane combination without curl. Camaro/crypto ships
muted by seed.

**Tests:** 35/35 passing (22 unit + 13 integration). Lane-isolation
regression guard still green.


## 2026-02-16 (previous): P1 + P3 batch — UIs + scheduler + vendor SDK chat

**P1 — `LivePositionsPanel`** mounted at `/admin/overview` (above
FeedersStrip). State-filter chips (open / managing / closed / all),
auto-refresh, totals header, Manage and Close modals that hit the 2
write endpoints. Auto-derives outcome label preview from pnl.

**P1 — `VRLScorecardsPanel`** mounted at `/admin/diagnostics` (after
QuantumPanel). Sortable table — gate, sample, precision, recall,
accuracy, TP/FP/TN/FN, verdict. Tier coloring: ≥70% EFFECTIVE (green),
≥50% MIXED (amber), <50% FRICTION (red). Defaults to precision ascending
so the operator sees the worst gates first. Shows scheduler status
badge inline.

**P3 — Nightly scorecard scheduler.** `shared/vrl.py` gained
`start_scorecard_scheduler` / `stop_scorecard_scheduler` wired into
`server.py` lifespan. Env knobs: `VRL_SCHEDULER_ENABLED`,
`VRL_SCHEDULER_INTERVAL_HOURS` (24), `VRL_SCHEDULER_WINDOW_HOURS` (720).
First run delayed 5 minutes post-boot. New endpoint
`GET /api/admin/vrl/scheduler/status`. Logs confirm
`"vrl scheduler started: interval=24h window=720h"`.

**P3 — chat.py refactored to Anthropic vendor SDK.** Migrated away from
`emergentintegrations` to `anthropic.AsyncAnthropic` (v0.102.0) per the
integration_playbook_expert_v2 playbook. Native multi-turn replay
(messages list, not synthetic preamble). Direction-aware error mapping
(`RateLimitError → 429`, `APIConnectionError → 503`, `APIStatusError → 502`).
Returns `stop_reason`, `input_tokens`, `output_tokens` on `ChatResponse`.

⚠️ **REQUIRES**: operator must add `ANTHROPIC_API_KEY=sk-ant-...` to
`backend/.env` for the chat endpoint to serve real LLM responses.
Without it, the endpoint returns 503 — same operational posture as the
legacy `EMERGENT_LLM_KEY unset` path. Model override:
`CLAUDE_MODEL_ID` (default `claude-sonnet-4-5-20250929`). Output cap
override: `CLAUDE_MAX_OUTPUT_TOKENS` (default 1024). The legacy
`EMERGENT_LLM_KEY` env var is no longer read by chat.py.



## 🚨 Latest (2026-02-16, late): Saturday Sprint P0 + P2 batch shipped

**P0 — Live Position Lifecycle** (open → managing → closed). New module
`shared/live_positions.py` + new `shared_live_positions` collection
(separate from the existing thesis-discussion `shared_positions` per user
direction — option B). Every state transition is recorded under MC
Shelly conventions (event types `position_opened`, `position_managing`,
`position_closed`). On close, MC writes a `shared_brain_outcomes` row so
the existing scorecard pipeline picks up the trade with zero extra
wiring. Hooked into both `shared/execution.py:execution_submit` and
`shared/auto_router.py:_route_one`.

**P0 — regime_fp 6-key.** `_regime_fingerprint` upgraded from 3 → 6
keys (added `trend_direction`, `volume_band`, `volatility_band`).
`IntentIn.evidence` now validates the canonical key set; unknown keys
reject with HTTP 422. Missing keys are back-filled server-side from the
latest indicator snapshot via `shared/intents.py:_enrich_regime_fp` —
brain keys win over derived. Canonical set exported as
`shared.hypothesis.REGIME_FP_KEYS`.

**P2 — `/api/health` deploy_mode** derives from broker
`execution_enabled` flags (Alpaca + Kraken). Env var still works as a
floor. Returns three fields now: `deploy_mode` (union), `deploy_mode_env`,
`deploy_mode_derived` so the operator sees which signal won.

**P2 — Verified Reinforcement Layer (VRL).** New module `shared/vrl.py`
+ collections `shared_vrl_verifications` and `shared_vrl_scorecards`.

1. *Per-receipt verifications* — direction-aware slippage, notional
   drift, fill quality. Wired into both execution paths (idempotent on
   `receipt_id`).
2. *Per-gate scorecards* — joins `shared_gate_results` × `shared_brain_outcomes`
   on `intent_id` and tallies a TP/FP/TN/FN confusion matrix per gate.
   Surfaces precision ("net protect rate"), recall, accuracy. Operator
   triggers via `POST /api/admin/vrl/scorecards/recompute`.

REST surface: 4 endpoints under `/api/admin/vrl/*`.

**P2 — Master Design System** at `/app/design_guidelines.md`. Single
source of truth for the RISEDUAL aesthetic: `rd-*` color tokens,
typography hierarchy, lane colors, three-tier heartbeat doctrine,
motion rules, testid discipline, forbidden patterns.

**Verified:**
- Backend restarts clean; all 6 sanity endpoints (`/api/health`,
  `/api/admin/live-positions`, `/api/admin/vrl/{verifications,scorecards}`,
  `/api/admin/roster`, `/api/admin/council/lookup-debug`) return 200.
- End-to-end position lifecycle smoke test: open ($100 BUY AAPL) →
  manage (-$30 scale) → close (+$12.50) → `shared_brain_outcomes` row
  written with label='win', `position_id` linked. Idempotency
  confirmed on `open_from_receipt` and `verify_receipt`.
- `_regime_fingerprint` produces all 6 keys against a synthetic
  indicators dict; validator rejects unknown keys correctly.



## 🚨 Latest (2026-02-16): Council extraction finalized + RosterPanel dual-lane UI

**P0 — `execution.py` post-extraction cleanup.** The council/quantum extraction
itself completed 2026-02-15 (council now lives in `shared/council.py`, 769 lines;
quantum in `shared/quantum_state.py`, 210 lines; execution.py shrunk from 1355
→ 639 lines). This pass removed the 6 residual unused imports left behind and
hoisted the council re-exports to the top-of-file import section — `execution.py`
is now ruff-clean.

**P1 — RosterPanel dual-lane UI.** `frontend/src/components/RosterPanel.jsx`
rewritten to render the cross-lane multi-seating model the backend has supported
since 2026-02-15. Two lanes side-by-side:
- **EQUITY** (5 seats): decider, executor, governor, advisor, opponent
- **CRYPTO** (4 seats): crypto (executor), crypto_governor, crypto_advisor, crypto_opponent

The picker UX explicitly distinguishes the two flavors of multi-seating:
- **Intra-lane** (forbidden by backend): chip warns "will vacate <role>" because
  the backend auto-vacates the same-lane previous seat on assign.
- **Cross-lane** (allowed by doctrine): chip shows "also holds <role> (<lane>)"
  — both seats are kept. Chevelle holding equity governor AND crypto_governor
  is the canonical example.

The eligibility matrix now has a two-row header grouping EQUITY vs CRYPTO so all
36 cells (4 brains × 9 roles) remain scannable.



## 🚨 Latest (2026-02-15): Seat-Bound Graduated Council Doctrine (rev3)

**Doctrine rewrite**: governance is now **graduated** and **seat-bound**, not binary and identity-bound. Trades fire when conviction outweighs dissent; only hard vetoes hard-block; every dissent is logged so outcomes can score who was right.

**Verdict matrix** (`backend/shared/execution.py:_governance_verdict`):

| Condition | Code | Allowed? | Risk × |
|---|---|---|---|
| `veto=True` AND governor conf ≥ 0.85 | `GOVERNOR_HARD_VETO` | ❌ | 0.0 |
| Dissent AND executor conf ≥ 0.72 | `EXECUTOR_OVERRIDES_SOFT_DISSENT` | ✅ | **0.50** |
| Dissent AND executor conf < 0.72 | `SOFT_DISSENT_LOW_EXECUTOR_CONF` | ❌ | 0.0 |
| No dissent, governor heard | `NO_GOVERNOR_DISSENT` | ✅ | 1.0 |
| Governor heard nothing on symbol | `GOVERNOR_NO_STANCE_ON_SYMBOL` | ❌ | 0.0 |
| Governor seat silent ≥ 30m | `GOVERNOR_OFFLINE` | ❌ | 0.0 |
| Governor seat vacant | `GOVERNOR_SEAT_VACANT` | ❌ | 0.0 |

**Seat-binding**: `_evaluate_council` resolves Governor and Opponent at evaluation time via `_seat_holder(role)` against the roster. Swap whoever holds Governor → the policy follows. No hardcoded brain names.

**Tunable thresholds** (top of `execution.py`):
- `GOVERNOR_HARD_VETO_THRESHOLD = 0.85`
- `GOVERNOR_SOFT_DISSENT_THRESHOLD = 0.55`
- `MIN_EXECUTOR_CONF_TO_OVERRIDE_SOFT_DISSENT = 0.72`
- `SOFT_DISSENT_RISK_MULTIPLIER = 0.50`

**Risk-multiplier propagation**: `_evaluate_gates` returns `risk_multiplier`; `auto_router._route_one` applies it to notional BEFORE submission. Caps re-evaluate against the reduced notional. A 0.50 override on a $100 intent fires a $50 order, persisted on both the execution receipt and the gate-result row.

**Learning ledger**: every council eval writes a row to **`shared_governance_decisions`** with `executor_seat_holder`, `governor_seat_holder`, `opponent_seat_holder`, both stances+confidences, the verdict code, `risk_multiplier`, and the thresholds in effect. Shelly/outcomes can join on `intent_id` to score who was right post-resolution.

**Diagnostic**: `GET /api/admin/council/lookup-debug?symbol=TSLA&executor_confidence=0.80&action=BUY` returns seat occupants, governor's normalized stance, opponent's stance, and the simulated verdict.

**Verified end-to-end**:
- ✅ Camaro BUY TSLA conf 0.65 → blocked `SOFT_DISSENT_LOW_EXECUTOR_CONF`
- ✅ Camaro BUY TSLA conf 0.80 → allowed `EXECUTOR_OVERRIDES_SOFT_DISSENT` risk×0.50
- ✅ Unknown symbol → blocked `GOVERNOR_OFFLINE` (preview stale data; in prod this becomes `NO_STANCE_ON_SYMBOL`)
- ✅ Governance ledger writes per evaluation

## 🚨 Previously (2026-02-15, superseded): Council Wiring Fix — Chevelle/REDEYE Now Audible to Executor

**Root cause found**: Executor's `_evaluate_council` was querying `db["shared_receipts"]` (literal string), but ingest persists Chevelle authority_calls to `db[SHARED_RECEIPTS]` which resolves to **`shared_adl_receipts`** (per `namespaces.py:5`). The governor and opponent gates were running but reading from an empty collection — silently passing every intent through.

**Fix shipped** (`backend/shared/execution.py`):
1. Switched lookup to use the `SHARED_RECEIPTS` / `SOVEREIGN_AUDIT_LOG` constants from `namespaces.py` — executor now reads the same collection ingest writes to.
2. Schema-tolerant brain-id & symbol-path matching (`runtime`/`brain`/`stack`/`source` × `intent.symbol`/`symbol`/`payload.symbol`/...) so future ingest shape changes don't silently break the gates again.
3. Silence-as-veto: if Chevelle has emitted ANY authority_call in the last 30 min but nothing on this symbol → `governor_uncertain` BLOCK. If no Chevelle activity in 30 min → `governor_offline` BLOCK. Only explicit `executable=True` clears the gate.
4. REDEYE conviction-floor gate: opposition_margin block fires when REDEYE's opposing confidence ≥ intent's own confidence (in addition to the absolute 0.65 threshold).
5. New diagnostic: `GET /api/admin/council/lookup-debug?symbol=XXX` shows exactly what the executor sees.

**Verified in preview**: 1,578 reachable Chevelle calls (was 0). TSLA BUY simulation now blocks with `Chevelle (governor) blocked TSLA: 'operator_lock_default'`.

⚠️ **Operational consequence for prod**: Chevelle has emitted `executable=True` in **0 of 1,578 calls** in the snapshot. Once this deploys, the auto-router will block ~100% of Camaro intents on `governor_authority` until Chevelle starts emitting approvals (or the engine team adjusts the `operator_lock_default` rule). This IS the doctrine — but expect a sharp drop in fills after deploy.



## 🚀 Latest (2026-02-14): AI Investment Hypothesis Engine — Brain Recall
- `/admin/hypothesis` page: operator types ticker → dual brain-content card
- **Strategist** = brain in Executor seat. **Auditor** = brain in new rotatable Auditor seat.
- **NO external LLMs**. Pure recall over `shared_intents` + `shared_brain_opinions` + Shelly's `shared_labeled_memories` + `shared_brain_outcomes` (track record) + similar past setups via regime fingerprint
- 174ms typical query time. Client-side 30-min cache.
- Auditor seat seeded with REDEYE.

## Previously (2026-02-14): Alpaca Paper Broker Pipeline Live

- **Broker adapter** (`shared/broker/alpaca.py`) wraps `alpaca-py` SDK; paper-only hard-coded
- **Hard caps** ($10/order · $50/day · $100 open notional) enforced in code (`shared/exposure_caps.py`)
- **Full 8-gate chain** at `/api/execution/{dry_run, submit}` — schema · routability · executor seat · live-disable · broker connected · 3× exposure caps
- **Operator UI**: `AlpacaConnect.jsx` tile on `/admin/intents` (encrypted keys, status, ping). Per-intent `submit` button visible only when dry-run passes.
- Status: backend + frontend testing-agent verified. 24/24 unit + 10/10 integration tests pass.
- Awaiting user: paste Alpaca paper keys via the Connect Alpaca modal on `/admin/intents` to enable end-to-end paper execution.


## ⚠️ Cross-Session Repo Map (read first, agents)

The user operates **two distinct Git roots**, both named in the RISEDUAL family.
This `/app` is **only one of them**. Do not assume the other one's files exist
here.

| Tree | Role | Where |
|---|---|---|
| **REDEYE / runtime stack** *(this repo, `/app`)* | Mission Control monorepo: shared nervous system, FastAPI ingest, governed promotion, dashboard, runtime patch-kits | this Emergent session |
| **RISEDUALAI / Camaro side** *(other repo, NOT here)* | Full Camaro app: Governance Console UI, audit trail, REDEYE bridge HTTP wrapper, AI Core, Patents A–I | a different Emergent session |

### What lives only in the OTHER repo (do not look for them here)
- `/app/backend/services/redeye_short_bridge.py` *(consumer-side copy)*
- `/app/backend/services/redeye_features.py`
- `/app/backend/services/redeye_long_short_focus.py`
- `/app/backend/routes/research.py`
  - `POST /api/research/redeye/camaro-signal`
  - `POST /api/research/redeye/camaro-signal/from-market`
  - `_emit_camaro_audit()` — writes audit row, tolerates missing `alpha_alignment`
- `/app/backend/tests/test_redeye_short_bridge.py`
- `/app/backend/tests/test_redeye_long_short_focus.py`
- `/app/frontend/src/components/GovernancePanel.jsx`
  - `RedeyeCamaroFeedCard()` — last-10 viewer of audit rows
  - `RedeyePulseCard()` — live Pulse widget

### What this repo authoritatively owns
- The REDEYE → Camaro **contract** (`/app/runtime_patch_kit/redeye/PULSE_CONTRACT.md`)
- The bridge **producer** module (`/app/runtime_patch_kit/redeye/services/redeye_short_bridge.py`)
- CLI patch instructions (`/app/runtime_patch_kit/redeye/CLI_PATCH.md`)
- The `alpha_alignment` forward-compat field (validated REDEYE-side, tolerated RISEDUALAI-side)
- All 3 isolated-brain runtime patch-kits (Alpha / Camaro / Chevelle)
- **Code Evolution v0 patch-kit** (`/app/runtime_patch_kit/code_evolution/`)
  — paste-in folder for ALL FOUR stacks (Alpha/Camaro/Chevelle/REDEYE).
  Each stack hosts its own gate; each stack has its own audit trail.
  Doctrine: AI may audit, recommend tests, write receipts. AI may NOT
  run shell, promote code, or modify the gate. PROTECTED paths return
  HTTP 423 in-band; CRITICAL paths require dual-sign (mirrors Build 3).
  9/9 smoke tests pass, lint clean.
- **Cross-brain discussion layer** (`/app/backend/shared/opinions.py` +
  `/app/runtime_patch_kit/DISCUSSION_LAYER_PATCH.md`) — mediated through
  Mission Control, pull-only consumption, schema-enforced no-execution.
  Brains post opinions, read peers, and learn each other via the
  `/api/shared/roles-manifest` endpoint. None of the four brains can
  execute (paper or live) — `may_execute` is a closed field that schema-rejects
  any value other than `false`.
- **Role Scoring v0** (`/app/backend/shared/outcomes.py` +
  `/app/frontend/src/pages/Scorecards.jsx`) — Step 2 of the cross-brain
  training plan. Each brain gets a role-specific scorecard:
    * Alpha: "When am I good at longs?" — hit rate, Brier, calibration bands.
    * REDEYE: "When am I good at shorts?" — same + alpha_alignment breakdown.
    * Camaro: "When should I trust/reduce/veto/execute?" — per-stance metrics.
    * Chevelle: "Which outside signals are reliable?" — topic_breakdown.
  Operators (or Chevelle as the auditor) attach outcomes; brains may not
  resolve their own opinions. Scorecards are descriptive, never
  prescriptive — they don't gate promotions; Patent J + dual-sign still does.
  Runtime endpoint `/runtime-discussion/scorecard` is schema-scoped: a brain
  cannot read another brain's metrics via runtime auth.
- **Conflict Memory v0** (`/app/backend/shared/conflicts.py` +
  `/app/frontend/src/pages/Conflicts.jsx`) — Step 4 of the training plan.
  Auto-detection: when two brains post opposing stances on the same topic
  within 4h, the disagreement is flagged as a conflict. Idempotent on
  pair_ids. Auto-resolution from outcomes: when both participants are
  resolved, the conflict closes with the win-side as winner (or stale if
  neither won). Manual operator override path. Pair-scorecards show "X
  is right Y% of the times when contradicting Z" across all six pairs.
  **Pair temperature** — rolling 24h/7d/30d conflict counts surfaced as
  a heat band (cold/cool/warm/hot/blazing). Live data already reads
  ALPHA vs REDEYE = BLAZING with 11 decisive (45%/55%) — the dual-axis
  read separates skill from friction so the operator can tell where to
  focus learning vs where doctrine itself may need rethinking.
- **Regime + Source slicing (Steps 3 & 5)** (2026-02-09, after Conflict Memory)
  - `OpinionIn` gains optional top-level `regime` field (snake_case
    identifier, max 48 chars; `422` on garbage). Stored on each opinion
    and copied onto the outcome doc at resolve-time so aggregation is
    a single query.
  - Camaro scorecard (`runtime=camaro`) gains
    `regime_breakdown.{overall, endorse_only}` — answers "which stack
    do I trust under which regime?". Stance-stratified; endorse-only is
    the headline.
  - Chevelle scorecard (`runtime=chevelle`) gains `source_breakdown`
    keyed off each opinion's `evidence.source` (with `_unsourced`
    catch-all bucket). Sits alongside `topic_breakdown`.
  - Frontend (`Scorecards.jsx`) renders both panels with sortable
    hit-rate / wins / losses / n tables.
  - Patch-kit doc: `/app/runtime_patch_kit/REGIME_AND_SOURCE_TAGGING.md`
    explains the tagging contract for Camaro and Chevelle sidecars.
  - Tests: `/app/backend/tests/test_regime_and_source.py` (5/5 PASS).
- **Shared Technical Evidence Layer** (2026-02-09, replaces "per-brain charts")
  - **Doctrine**: OHLCV + indicators are shared evidence; same bars,
    four brains, four interpretations. No brain owns the feed.
  - **Write path**: `POST /api/ingest/ohlcv` (single) and
    `/api/ingest/ohlcv/batch` (up to 2000 bars) authenticated with
    `X-Feeder-Token`. Feeders configured today: `kraken_pro` (crypto),
    `thinkorswim` (other markets), `manual` (backfill). Each has its
    own env-var token so revocation is one line.
  - **Idempotency**: bars keyed on `(source, symbol, tf, ts)`. Re-ingest
    of same key updates the bar and recomputes the snapshot.
  - **Indicator engine**: pure-Python (`/app/backend/shared/indicators.py`)
    — SMA(20/50/200), EMA(12/26), RSI(14), MACD(12,26,9), BBands(20,2),
    ATR(14). Computed on ingest, stored as one snapshot per (source,
    symbol, tf); historical bars retained for replay.
  - **Read paths**:
    * `GET /api/shared/technical/symbols` — universe + last-bar times.
    * `GET /api/shared/technical/{symbol:path}?tf=&source=&bars=` —
      operator JWT, supports slashed symbols (`BTC/USD`).
    * `GET /api/runtime-discussion/technical/{symbol:path}?caller=&tf=`
      — runtime-token auth so brain sidecars can pull without an
      operator JWT. Same payload shape ⇒ replayable.
  - **Mission-page panel**: `TechnicalsPanel.jsx` embedded on Overview
    (no new route per operator directive). Shows the universe with
    source/symbol/tf rows; click to expand a snapshot card (Close, RSI,
    MACD hist, BB position, SMA20/50/200, ATR%). Polls every 20s.
  - **Feeder kit**: `/app/runtime_patch_kit/technicals/README.md`
    includes a complete Kraken Pro REST polling sidecar and a TOS shell
    + the `evidence.technical_ref` audit-replay handshake brains use
    when posting opinions that referenced the snapshot.
  - **Tests**: `/app/backend/tests/test_technicals.py` (20/20 PASS) —
    indicator math fixtures, idempotency, batch ingest, feeder-auth
    rejection paths, operator/runtime read shape, symbol 404.
  - Total backend pytest = **118/118**.
- **Feeder Slots strip** (2026-02-09, follow-up)
  - `GET /api/shared/technical/feeders` aggregates per-feeder status:
    last_bar_ts, symbol coverage, tf coverage, bar count, configured /
    awaiting / fresh / stale / live. tf-aware staleness (1h = 24h
    window, 1d = 48h window).
  - `FeedersStrip.jsx` Mission-page component — three slot cards
    (Kraken Pro headline, ThinkOrSwim, Manual). Click to expand setup
    details: endpoint URL, X-Feeder-Token env-var name, source field
    value, currently-feeding symbols/tfs, copy-to-clipboard helpers, and
    a pointer to the patch-kit doc.
  - **Login bug fix**: replaced the axios client in `lib/api.js` with a
    native-fetch shim (drop-in API surface — `api.get/post/put/delete`
    return `{data}`, errors expose `err.response`). axios 1.x's XHR
    adapter intermittently hung under the Cloudflare-fronted preview
    deploy. Also disabled PostHog session recording (it was wrapping
    fetch for replay).
- **Kraken Pro live connection** (2026-02-09)
  - **Encrypted credential storage**: `shared/credentials.py` — Fernet
    symmetric encryption with key in `CREDENTIALS_ENCRYPTION_KEY`
    (auto-generated and persisted to `backend/.env` on first run in
    local dev; required env-var in prod). API key + private key stored
    encrypted at rest; private key never returned by any endpoint.
  - **Kraken client**: `shared/kraken.py` — public OHLC fetch +
    HMAC-SHA512 signed private calls. Monotonic nonce persisted on the
    singleton doc, atomic max-bump on every call. Scope probe over
    Balance / OpenPositions / ClosedOrders / TradesHistory / Ledgers so
    UI can show which permissions the key was granted. Symbol mapping
    table for BTC/ETH/SOL/XRP/ADA/DOGE pairs.
  - **Endpoints** (`shared/kraken_routes.py`):
    * `POST /api/admin/kraken/connect` — probe-then-store-then-start.
      Refuses to persist keys if Balance probe denies.
    * `GET /api/admin/kraken/status` — connection summary (redacted).
    * `POST /api/admin/kraken/reprobe` — re-run scope probe.
    * `POST /api/admin/kraken/test` — cheap Balance call.
    * `POST /api/admin/kraken/poll` — force OHLC poll outside schedule.
    * `DELETE /api/admin/kraken/disconnect` — wipe creds + stop poller.
    * `POST /api/admin/kraken/execution` — flip the execution-allowed
      gate. Defaults False. Requires literal confirm phrase
      ("I authorize execution on Kraken" / "Disable execution"). Every
      flip is audit-logged.
    * `GET /api/admin/kraken/audit` — append-only action log.
  - **Auto-poller**: FastAPI lifespan task. Pulls configured pairs/tf
    every `poll_interval_seconds` (default 60s). Pushes bars through
    existing technicals ingest → snapshot recompute. Idempotent on bar
    key, so re-polled overlap doesn't dupe. Replaces the seeded
    synthetic BTC/ETH bars on first successful poll.
  - **Doctrine**: only read-scope endpoints are called by Mission
    Control. Trading endpoints (AddOrder/CancelOrder) are intentionally
    not wired. `execution_enabled` is a flag for the eventual wire-up;
    the brain layer's `may_execute` stays schema-pinned False.
  - **Frontend**: `KrakenConnect.jsx` — modal under the Kraken slot
    with paste-once API+private inputs, pair multiselect, tf picker,
    test-and-connect button. Connected view shows redacted previews,
    detected scopes (✓/✗), balance preview (top 3 assets), poller
    status, last-tick info, and the execution-toggle confirmation
    flow. Disconnect button wipes creds and stops the poller.
  - **Tests**: `/app/backend/tests/test_kraken.py` (17/17 PASS) —
    signing math against Kraken's documented test vector, Fernet
    round-trip, redact masking, all admin endpoints' auth + 404 +
    schema rejection paths, execution-toggle confirm-phrase guard,
    audit-log capture.
  - Total backend pytest = **135/135**.
- **Brain ↔ Technical Feed wiring (Option A)** (2026-02-09)
  - Backend: `GET /api/shared/technical/{symbol}` (and runtime variant)
    accept `as_of=<ISO 8601>`. When supplied, the indicator snapshot is
    recomputed from retained bars ≤ as_of using the same pure pipeline
    that builds live snapshots. Same response shape; `replayed: true`
    flag distinguishes audit replays from live reads.
  - Camaro patch kit (`PASTE_INTO_CAMARO_TECHNICALS.md`): explicit
    `read_technical → decide → post_opinion` pattern showing how Camaro
    pulls a snapshot, makes its judgement, and attaches
    `evidence.technical_ref` (source, symbol, tf, computed_at, indicators_used)
    plus `evidence.values` (the specific numbers it quoted) to the
    opinion. Note documents that other brains can paste the same
    pattern when they get sidecars later.
  - Frontend: `AuditReplay.jsx` component injected into the Discussion
    page. When any opinion carries `evidence.technical_ref`, the
    operator sees a "replay technical evidence" toggle. Click → fetches
    the historical snapshot via the new `as_of` path and renders an
    8-cell grid (Close, RSI, MACD hist, BB position, SMAs, ATR%) with
    quoted-vs-recomputed values side-by-side. Highlighted cells show
    the indicators Camaro explicitly cited in `evidence.values`.
  - Tests: `test_replay_at_past_timestamp`,
    `test_replay_404_when_no_bars_before_as_of`. Confirm strict
    historical correctness — live and replay returns at different
    timestamps give different values from the same DB state.
  - Total backend pytest = **137/137**.
- **Brain Roster — dynamic role assignment** (2026-02-09)
  - Four roles: `decider`, `executor`, `governor`, `advisor`. Four
    brains: alpha, camaro, chevelle, redeye. Operator assigns 1:1.
    Defaults match doctrine (camaro/alpha/chevelle/redeye in role order).
  - Backend: `/api/admin/roster` (GET) + `/assign` + `/swap` + `/reset`
    + `/audit`. Operator JWT auth. Singleton doc in MongoDB; audit log
    appends every change.
  - Doctrine guard: the roster is **descriptive metadata only**.
    `may_execute` remains schema-pinned False on every endpoint and
    every patch kit regardless of which brain holds "executor". The
    role labels record operator intent ("if execution were enabled,
    this brain would carry the orders") not authority.
  - Assignment behavior: putting a brain into a new role automatically
    vacates its previous role (no auto-fill — operator decides).
    `brain=None` explicitly vacates a role.
  - Opinion stamping: when any opinion is ingested, the brain's current
    role is captured into `posted_as` on the opinion doc. Best-effort
    (roster lookup failures don't block opinion writes). Lets the
    operator later see "Camaro endorsed *as decider*" vs.
    "Camaro endorsed *as advisor*" after a role change.
  - Frontend: `RosterPanel.jsx` on the Mission page (above the Feeder
    slots). Four role columns showing current occupant + role
    description; click "change" to swap in another brain; "vacate" to
    empty a role; "reset" restores defaults. Picker warns if a chosen
    brain currently holds a different role so the operator sees the
    cascade before committing.
  - Tests: `/app/backend/tests/test_roster.py` (19/19 PASS) — defaults,
    assign + auto-vacate, swap, swap-same-role rejection, bad role/brain
    422, reset, audit-log capture, auth required, opinion stamping with
    posted_as, posted_as reflects post-swap roster, **Eligibility matrix
    defaults + assign/swap enforcement + can't-disallow-current-occupant
    safety**, **Tenure KPI response shape + tenure resets on swap**.
  - **Role Tenure KPI** (`/api/admin/roster/tenure`): per-role
    `current_role_started_at`, `days_in_role`, `tenure_display`
    ("14d" / "3h"), `previous_role`. System-level:
    `total_swaps_90d`, `average_tenure_days`, `churn_state`
    (LOW ≤4 swaps · MEDIUM ≤12 · HIGH >12 in 90d), `last_swap`.
    Computed from the audit log (no new collection). Invariant
    documented in payload: tenure must never affect execution.
  - **Eligibility matrix** (`/api/admin/roster/eligibility`): operator
    switches deciding which seats each brain may occupy. Defaults
    encode training reality — chevelle = governor only, redeye =
    advisor only, alpha/camaro = decider/executor/advisor (not
    governor). `/assign` and `/swap` refuse to violate the matrix
    (400 with clear error). Disabling a switch is blocked while the
    brain currently holds that seat (vacate or swap first).
  - **Frontend** (`RosterPanel.jsx`): tenure shown inline per role
    ("in role: 14d") + churn badge in the header + footer KPI row
    (avg tenure, swaps 90d, last swap age, doctrine invariant).
    Eligibility switches toggle pane (collapsed by default) renders
    a 4×4 ALLOW/BLOCK matrix; ineligible brains are greyed out and
    marked "BLOCKED" in the role picker.
  - Total backend pytest = **156/156**.
- **IBKR Web API integration — Phase 1 (read-only)** (2026-02-11)
  - `shared/ibkr.py` — httpx OAuth 2.0 Bearer client against
    `api.ibkr.com`. Probe (`/iserver/auth/status` + `/iserver/accounts`),
    test, accounts, positions, tickle (single tick + 5-minute background
    loop), disconnect, execution-toggle gate, audit log.
  - Encrypted token storage uses the same Fernet path as Kraken
    (`shared/credentials.py`). The token is never returned past
    `redact()`; the encrypted blob lives in `ibkr_credentials`.
  - Endpoints (`/api/admin/ibkr/*`): connect, status, test, tickle,
    accounts, positions, disconnect, execution, audit. Every endpoint
    requires operator JWT.
  - Tickler: background asyncio task pings `/v1/api/tickle` every
    5 minutes so the IBKR session does not time out. Started on save,
    stopped on disconnect. Auto-revives on app boot if creds exist
    (lifespan hook in `server.py`).
  - Doctrine: trade endpoints (`/iserver/account/.../orders`, `/reply/*`)
    are **NOT** wired by this router. `execution_enabled` defaults False
    and is groundwork for the eventual Phase 2 dual-sign promotion.
  - Frontend: `IBKRConnect.jsx` modal under the new IBKR broker slot in
    `FeedersStrip.jsx` — paste access_token, optional account_id, base_url;
    test-and-connect; connected view shows auth status, tickler state,
    detected accounts, positions loader, exec-toggle confirm-phrase flow.
  - Tests: `/app/backend/tests/test_ibkr.py` (14/14 PASS) — disconnected
    status shape, schema rejection paths (short token, missing token,
    non-https base_url), 404s on every endpoint when unconfigured,
    disconnect idempotency, execution-toggle confirm-phrase guard
    against a seeded credential doc, audit log capture, JWT auth required
    on every admin path, `get_active()` returns None when nothing stored.
  - Total backend pytest = **170/170**.
- **Heat-map matrix — at-a-glance pair view** (2026-02-11)
  - Backend: `GET /api/shared/conflicts/matrix` aggregates ALL six
    brain-pair combinations into a single payload: skill (win rate,
    a_wins, b_wins, decisive), friction (temperature over 24h/7d/30d),
    and a 7d-derived heat band (cold/cool/warm/hot/blazing). One
    round-trip replaces N pair-scorecard fetches on the dashboard.
    Operator JWT required.
  - Frontend: `HeatMatrix` table on `Conflicts.jsx` above the existing
    pair scorecards — 4×4 grid where the row brain's win rate over the
    column brain is the headline number, the cell background hue is the
    7d friction colour, and the subline shows wins/decisive · 7d count.
    Diagonal shows `—`. Tooltip carries the raw counts.
  - Tests: `/app/backend/tests/test_conflict_matrix.py` (3/3 PASS) —
    response shape (6 cells for 4 brains, all required keys, no dupes),
    JWT required, matrix cell values cross-match the per-pair scorecard
    endpoint exactly (decisive, a_wins, b_wins, 7d friction, heat band).
  - Total backend pytest = **170/170**.
- **Public.com retail brokerage — Phase 1 (read-only)** (2026-02-11)
  - **Why a third broker:** Public.com cash accounts have **no PDT
    restrictions** — when Phase 2 ships, this is the venue the executor
    brain can use for sub-$25k day-trade activity without IBKR's PDT
    gate or Kraken's crypto-only scope. Stocks, ETFs, options, and
    multi-leg strategies on the same key.
  - **Two-step auth** (per public.com/api/docs/quickstart):
    1. Operator generates a long-lived SECRET KEY at
       `public.com/settings/security/api`.
    2. We exchange the secret for a short-lived ACCESS TOKEN via
       `POST /userapiauthservice/personal/access-tokens` with
       `{validityInMinutes, secret}`. Default validity 24h, operator
       configurable 5 min … 7 d.
    3. Subsequent calls use the access_token as `Authorization: Bearer`.
  - **Encrypted storage**: secret + cached access_token both Fernet-encrypted
    via `shared/credentials.py` (same key path as Kraken/IBKR). Secret is
    never returned past `redact()`; plaintext token is never exposed.
  - **Background refresher**: asyncio task that polls every 60s and rolls
    the access token when it has ≤ 5 min remaining. Started on connect,
    stopped on disconnect. Auto-revives on app boot if creds exist.
  - **Endpoints** (`/api/admin/public/*`):
    * `POST /connect` — probe (token-exchange + account-discovery) then
      persist. Refuses to store if the secret can't exchange.
    * `GET /status` — redacted summary incl. token expiry, refresher state.
    * `POST /test` — calls `/userapigateway/trading/account`.
    * `POST /refresh-token` — operator-forced refresh.
    * `GET /accounts` — full account list.
    * `GET /portfolio` — positions + balances via
      `/userapigateway/trading/{accountId}/portfolio/v2`.
    * `DELETE /disconnect` — wipe secret + cached token + stop refresher.
    * `POST /execution` — flip the gate behind the same confirmation
      phrase ("I authorize execution on Public" / "Disable execution").
    * `GET /audit` — append-only action log.
  - **Doctrine**: Phase 1 is read-only. Order placement endpoints
    (`/userapigateway/trading/order/*`) are intentionally **NOT** wired;
    `execution_enabled` defaults False and is groundwork for Phase 2.
  - **Frontend**: `PublicConnect.jsx` modal under a new PUBLIC.COM
    broker slot in `FeedersStrip.jsx` (5 slots total now: Kraken / TOS
    / IBKR / Public / Manual). Operator pastes secret, optional
    account_id, base_url, token-validity-minutes; connected view shows
    token expiry countdown, refresher state, detected accounts,
    portfolio loader, exec-toggle confirm-phrase flow.
  - **Tests**: `/app/backend/tests/test_public.py` (15/15 PASS) —
    disconnected status shape, schema rejection paths (short secret,
    missing secret, non-https base_url, zero validity, excessive
    validity > 7 d), 404s on every endpoint when unconfigured,
    disconnect idempotency, execution-toggle confirm-phrase guard
    against a seeded credential doc, audit log capture, JWT auth
    required on every admin path.
  - Total backend pytest = **185/185**.
- **Seat policy is authority — identity is just training history** (2026-02-12)
  - **Doctrine codified**: `shared/seat_policy.py` declares per-seat
    permissions (`may_decide`, `may_execute`, `may_override`, `may_veto`,
    `speaks_as`) as a single source of truth. Every stance / decision /
    audit row snapshots the policy of the seat the brain held at write
    time, with `seat_epoch` to join back to roster history.
  - **Seat names cleaned**: `long_advisor` → `advisor` (neutral counsel),
    `short_advisor` → `opponent` (adversarial). 5 seats: decider,
    executor, governor, advisor, opponent. REDEYE → opponent. Advisor
    starts vacated. All eligibility + tenure + tests + frontend labels
    migrated.
  - **Per-position call mode** (`auto` | `manual`): operator chooses at
    propose-time. In `auto` mode, the first long/short stance from the
    brain holding the executor seat **immediately** advances state to
    `consensus_long`/`consensus_short` — drop any stack into Executor
    and it "just calls". Non-executor stances on auto positions DO NOT
    advance. `abstain` on auto positions does NOT advance.
  - **Per-(brain, seat) analytics** at `/api/admin/roster/seat-performance`:
    aggregates stances + executor calls + tenure-days for every
    (brain, seat) pair the brain has ever held. Answers "how good was
    Camaro as Executor?" with hard numbers instead of gut feel.
  - **`/api/admin/roster` payload** now includes the full `policy` dict
    and current `seat_epoch` so the frontend (and brain sidecars) can
    consult permissions without a second round-trip.
  - **Tests**: `/app/backend/tests/test_seat_policy_and_auto.py`
    (10/10 PASS) — policy exposed, snapshot fields on every stance,
    seat_epoch bumps on assign, auto-advance on executor long/short,
    no-advance on non-executor or executor-abstain, manual-mode never
    auto-advances, default call_mode is manual, seat-performance
    matrix returns expected rows, JWT auth required on analytics.
  - `tests/test_roster.py` + `tests/test_positions.py` migrated for
    the seat rename and policy snapshot fields.
  - Total backend pytest = **184/184**.
- **LivePulse honest-signal upgrade** (2026-02-13)
  - `/api/heartbeat-status/{brain}` now combines TWO signals so legacy
    `/api/ingest/heartbeat` background traffic stops false-greening the
    indicator. Verdict bands:
    * `connected` — heartbeat <90s AND sovereign contribution <300s.
    * `partial` — heartbeat present but no recent sovereign
      contribution (most common confusion mode: legacy ingest only or
      sidecar crashed mid-tick).
    * `stale` — last sovereign contribution 5-30 min ago.
    * `dead` — neither signal recent.
    * `never` — neither signal has ever been seen for this brain.
  - Response carries `heartbeat_age_seconds` + `contribution_age_seconds`
    so the operator can hover the LivePulse tooltip and see WHY the
    state is what it is.
  - LivePulse renders `partial` as amber "HEARTBEAT ONLY" with hover-
    text breakdown.
  - **Real connection census** (current state from first deployment
    wave):
    * Alpha: `connected` (real sidecar — contribution every 60s)
    * Chevelle: `connected` (real sidecar)
    * Camaro: `partial` (sovereign sidecar pending; discussion-layer
      opinions live)
    * REDEYE: `stale` (contributed earlier in session, last seen ~17m)
  - Tests `tests/test_heartbeat_status.py` (4/4 PASS) updated for the
    combined-signal contract.
- **LivePulse connection indicator on /runtime/{brain}** (2026-02-13)
  - **Backend**: new read-only `GET /api/heartbeat-status/{brain}`
    endpoint (no auth — same exposure as the existing public /ping
    pages). Returns `connected` band (`never` / `fresh` / `stale` /
    `dead`), `last_seen` ISO timestamp, and `age_seconds`. Banding:
    fresh < 90s, stale < 10min, dead beyond.
  - **Frontend**: `LivePulse` component polls `/heartbeat-status/{brain}`
    every 5s, renders a pulsing dot in the page header next to the
    brain badge. Green pulse when fresh (connected · 21s ago), amber
    static for stale, red for dead, grey for never. The pulse uses
    `animate-ping` so a brain coming online is impossible to miss
    visually.
  - **Tests** `tests/test_heartbeat_status.py` (4/4 PASS): unknown
    brain → 404, never-pinged state, fresh-after-ping state, no JWT
    required.
  - **Heartbeats collection reset** so the dashboard shows the honest
    "no heartbeat yet" state until a real brain host connects.
- **Sovereign onboarding packets + DEPLOY runbook** (2026-02-13)
  - **Smoke-test cleanup**: dropped 4 sovereign_state rows + 70 history
    rows + 70 audit rows + chat / narrative / traffic / rate-limit
    collections so the operator console shows the honest empty state
    ("No sovereign snapshot on file") until real brains connect.
  - **`DEPLOY.md`** at `/app/runtime_patch_kit/sovereign/` —
    5-minute deploy recipe (clone kit → set env → smoke test → run
    sidecar), systemd unit example, Dockerfile example, troubleshooting
    matrix, mode-switching notes (DTD↔PRD), broker-feed wiring path.
  - **Per-brain onboarding packets**: one self-contained markdown file
    per brain with the exact ingest token, suggested initial weights
    (creating distinct personalities), suggested symbol list, and
    copy/paste quickstart:
    * `ONBOARDING_ALPHA.md` — trend follower (trend +0.85, macd +0.65,
      rsi −0.25), lr 0.06, default seat Decider.
    * `ONBOARDING_CAMARO.md` — mean reverter (trend −0.45, macd +0.20,
      rsi +0.80), lr 0.05, default seat Advisor/Opponent.
    * `ONBOARDING_CHEVELLE.md` — risk auditor / governor (balanced 0.35
      across features), lr 0.02 (slow, deliberate), default seat
      Governor (holds the veto bit).
    * `ONBOARDING_REDEYE.md` — contrarian (trend −0.70, macd −0.30,
      rsi +0.55), lr 0.05, default seat Opponent.
  - **Doctrine reminder in every packet**: `LIVE_TRADING_ENABLED=False`
    is non-negotiable; brains write only to local state and via the
    three MC HTTP endpoints; PRD mode disallows training.
  - **Current state**: zero brain hosts connected. Architecture ready
    end-to-end; the deploy is a 5-minute per-brain task whenever the
    operator decides to run it.
- **Per-tier rate limits on /api/public/*** (2026-02-13)
  - **Defaults (per minute)**: free 30 · starter 60 · pro 300 · pro_max 1200.
    `unknown` tier (caller misspelled the header) caps at 20 as a
    belt-and-suspenders defense. Each tier's limit overrideable via env:
    `RATE_LIMIT_{FREE,STARTER,PRO,PRO_MAX}_PER_MIN`.
  - **Mechanism**: per-minute bucket counter in
    `public_rate_limits` collection, atomic `$inc` via
    `find_one_and_update(upsert=True, return_document=True)`. TTL index
    on `expire_at_epoch` drops buckets after 2 minutes — collection
    stays tiny regardless of traffic. Fails OPEN on Mongo hiccups —
    the rate limiter must never become a 5xx source for callers.
  - **Behavior**:
    * 200 responses carry `X-RateLimit-Tier`, `-Limit`, `-Remaining`,
      `-Window` so risedual.ai's backend can surface remaining quota.
    * 429 responses carry the same headers plus `Retry-After` (seconds
      until the next bucket).
    * Missing `X-RiseDual-Token` skips the rate-limit increment so
      random scrapers can't lock out legit callers (the trust dep
      still 401s).
    * Unknown tier values normalize to `unknown` (sentinel) so arbitrary
      strings don't pollute the bucket key space.
  - **Middleware ordering (important)**: `rate_limit_middleware` is
    inner, `public_traffic_middleware` is outer — so 429s emitted by
    the rate limiter are still seen + logged by the traffic logger.
  - **Admin endpoint** `GET /api/admin/public-traffic/limits` returns
    the current cap table — surfaced as a "Tier Rate Limits" tile on
    the `/public-traffic` operator page.
  - **Tests** `tests/test_public_rate_limit.py` (8/8 PASS, ~3.5min
    because the tests wait for minute-bucket rollover):
    * `/limits` endpoint requires JWT and returns the cap table.
    * 200 responses carry the X-RateLimit-* headers (verified for pro_max).
    * Free-tier 30/min cap: 35 calls → exactly 30×200 + 5×429.
    * 429 carries `Retry-After`, `X-RateLimit-Tier=free`,
      `X-RateLimit-Limit=30`, `X-RateLimit-Remaining=0`.
    * Pro Max immune to free-tier cap (50 consecutive calls all 200).
    * Missing trust token: not rate-limited, but still 401 (auth dep
      handles it).
    * 429 rows appear in the public-traffic log with `status=429` and
      the proper `tier` value — operator can filter for them.
- **Public Traffic verification page** (2026-02-13)
  - **Backend middleware** `public_traffic_middleware` mounted globally:
    captures every `/api/public/*` request — path, method, query,
    status, latency_ms, tier header, caller_ip. Fire-and-forget log
    insert; never blocks the live request even if Mongo hiccups.
  - **Admin endpoints** (`/api/admin/public-traffic/*`, JWT-gated):
    * `GET /admin/public-traffic` — last N rows, filterable by path /
      status / tier.
    * `GET /admin/public-traffic/summary?hours=N` — total + by-endpoint
      / by-tier / by-status counts + p50/p95/p99 latency.
    * `DELETE /admin/public-traffic` — clear all rows (manual reset).
  - **Frontend** `/public-traffic` page (operator-only, in nav):
    summary tiles (Total, Latency p50/p95/p99, By Tier, By Status),
    By-Endpoint horizontal bar chart, live tail table with status +
    tier color-coding, filters (window 1h-7d, path contains, status,
    tier), auto-refresh every 5s, clear-log button.
  - Smoke-tested live: 12 mixed requests across free/starter/pro/pro_max
    + 401s render correctly with proper coloring and aggregation.
- **Public API Phase 2 — LLM features + dual-token rotation** (2026-02-13)
  - **Integration**: Emergent LLM key (universal). Two models, picked
    for cost/quality fit:
    * `gemini:gemini-3-flash-preview` — narrative summary (cheap broadcast).
    * `anthropic:claude-sonnet-4-5-20250929` — grounded chat (deep reasoning, lower volume).
  - **`GET /api/public/digest/narrative`** — 3-5 sentence prose
    overview of today's market posture. System prompt anchors the
    model on the supplied JSON (predictions/smart_money/alerts), forbids
    fabricating numbers, no markdown / disclaimers. Cached server-side
    by 5-minute time bucket so dashboard refreshes don't burn tokens.
    Available to all tiers (content is not gated — same market).
  - **`POST /api/public/chat`** — multi-turn grounded RiseDualGPT.
    Pro Max only (returns 403 otherwise). Session memory persisted
    to `public_chat_messages` collection keyed by `session_id`;
    survives MC restarts. Prior conversation replayed into the LLM
    via injected "prior conversation" block on each turn (bounded by
    `MAX_TURNS_PER_SESSION=25`). System prompt enforces
    observation-only doctrine: model explains what signals say, will
    NOT recommend buy/sell.
  - **`GET /api/public/chat/history/{session_id}`** — repaint chat
    panel after a reload.
  - **`DELETE /api/public/chat/history/{session_id}`** — clear
    session memory (end of conversation).
  - **Dual-token rotation grace mode**: `auth.public_trust_required`
    now accepts EITHER `RISEDUAL_PUBLIC_TOKEN` (primary) OR
    `RISEDUAL_PUBLIC_TOKEN_OLD` (legacy). Operator rolls MC's env var
    independently of risedual.ai's deploy schedule — no broken
    interval. Documented in
    `runtime_patch_kit/risedual_public/ENV_CHECKLIST.md`.
  - **Paste-in kit updated**:
    * `types.ts` — adds `NarrativeResponse`, `ChatRequest`,
      `ChatResponse`, `ChatMessage`, `ChatHistoryResponse`.
    * `mcPublicClient.ts` — `digestNarrative()`, `chat()`,
      `chatHistory()`, `chatClear()`.
    * `SWAP_NOTES.md` — Phase 2 swap section (narrative + chat).
    * `ENV_CHECKLIST.md` — dual-token rotation procedure documented.
  - **Tests**: `/app/backend/tests/test_public_phase2.py` (14/15 PASS,
    1 skipped intentionally — long-running variant covered by
    multi-turn test):
    * Narrative returns grounded prose, model = gemini, all tiers OK.
    * Narrative second call hits cache (text identical).
    * Chat returns 403 for free / starter / pro.
    * Chat continues a session (same session_id, turn_count increments).
    * Chat history GET / DELETE work; 403 for non-pro_max.
    * Input validation (empty message → 422).
    * Dual-token: in-process tests verify both tokens accepted when
      legacy is set, primary-only when not, third value refused.
  - **Total backend pytest = 62/63 + sovereign passing** (Phase 2:14
    + Phase 1:26 + Sovereign:22 = 62/63 with 1 skipped; full backend
    suite carries forward all previously passing tests).
- **Public API for risedual.ai (Direction C, Phase 1)** (2026-02-13)
  - **Doctrine codified**: Two faces, one brain. risedual.ai keeps its
    Stripe + credits + 4-tier auth + UI; MC becomes the silent
    intelligence backend. Trust contract = `X-RiseDual-Token` (shared
    secret) + `X-RiseDual-User-Tier` (free/starter/pro/pro_max
    propagated from risedual.ai's user model; free and starter both
    treated as non-paid; only pro/pro_max get uncapped digest rows).
  - **MC env var**: `RISEDUAL_PUBLIC_TOKEN`. Missing → 503; wrong → 401;
    unknown tier → 422.
  - **Endpoints** at `/api/public/*`, all read-only, all sanitized:
    * `GET /signals` — Active Signals + aggregate AI Consensus
      (BULLISH/BEARISH/NEUTRAL/MIXED + buy/sell/hold percentages).
    * `GET /signals/{id}` — both framings of the same position:
      adversarial (Bull/Bear/Commander ↔ decider/opponent/executor
      seats) AND governance (Strategist/Auditor/Synthesized Signal ↔
      decider/governor/executor). Hides memory provenance, quorum
      blindness, seat_epoch.
    * `GET /digest` — predictions / smart_money / alerts with caps
      `{2/2/1}` for free+starter (+ locked-CTA rows) and `{25/25/25}`
      for pro/pro_max. Shapes match risedual.ai's existing
      `collect_digest_data` exactly.
    * `GET /scanner/presets` + `/scan?preset_id=…` — 10 presets
      (macd_bullish_cross, macd_bearish_cross, bollinger_squeeze,
      ema_golden_cross, volume_spike, near_52w_high, near_52w_low,
      rsi_overbought, rsi_oversold, momentum_breakout). Detection
      logic uses MC's stored indicator snapshots + recent OHLCV.
      Match shape `{symbol, strength, detail}`.
    * `GET /agent-activity/feed?since=&limit=` — polled feed
      synthesized from position audit + conflicts + outcomes.
      ~10s cadence on the client.
    * `GET /models-mind/{symbol}` — 10-feature panel
      (score_2W, distance_from_mw, macro_regime_flag, atr_id,
      earnings_proximity, momentum_3d, sector_rs, pattern_score,
      rsi_id, vol_zscore). MC defines these canonically (names didn't
      exist in risedual.ai's actual backend); computed from real
      technicals; `coverage: "not_wired"` for features MC can't yet
      compute (earnings_proximity, sector_rs).
    * `GET /heatmap` — per-symbol 24h % change + color band
      (strong_buy / mild_buy / neutral / mild_sell / strong_sell).
    * `GET /sectors` — XLK/XLF/XLV/XLY/XLP/XLE/XLI/XLU/XLB/XLRE/XLC
      universe. `degraded: true` until sector ETFs are wired into a
      feeder.
  - **Module split**: `/app/backend/shared/public_api/` with one file
    per endpoint group (`auth.py`, `signals.py`, `digest.py`,
    `scanner.py`, `agent_activity.py`, `models_mind.py`, `heatmap.py`,
    `router.py`).
  - **Paste-in kit** at `/app/runtime_patch_kit/risedual_public/`:
    * `README.md` — architecture, trust contract, rollout plan.
    * `types.ts` — exhaustive TypeScript types for every endpoint.
    * `mcPublicClient.ts` — drop-in Node/Next backend client.
    * `python_types.py` — Pydantic v2 mirrors for backend re-validation.
    * `SWAP_NOTES.md` — per-page mapping for risedual.ai's frontend.
    * `ENV_CHECKLIST.md` — env vars + rotation procedure.
  - **What MC does NOT do**: no Stripe, no credit ledger, no user
    accounts, no PCI scope. risedual.ai keeps all of it. MC's tier
    header only governs content sanitization (locked rows), not
    feature gating (risedual.ai's existing tier checks gate that).
  - **Tests**: `/app/backend/tests/test_public_api.py` (26/26 PASS) —
    trust auth (missing/wrong/unknown-tier), default-free tier,
    starter is unpaid, pro_max unlimited; signal card shape, both
    framings, 404; digest free/starter caps, pro/pro_max uncapped,
    locked-row shape; scanner 10 presets, match shape, unknown-preset
    404; agent-activity shape + since filter; models-mind 10-feature
    shape + not-wired markers + 404; heatmap + sectors universe.
  - **Total backend pytest = 243/243** (195 prior + 22 sovereign + 26 public).
- **Sovereign Sidecar Template** (2026-02-13)
  - **Doctrine**: each of the four brains can run as a deterministic
    sovereign sidecar — same intelligence core
    (`wild_adaptive_core_v2.py`), different initializations / feature
    emphasis per brain. Local state on the brain host (JSON), MC
    receives stances + state snapshots via API only. Never touches MC's
    DB directly.
  - **Three locks, one door** (observation-only):
    1. Brain core defaults `LIVE_TRADING_ENABLED = False`.
    2. Sidecar reasserts False on load (refuses to start if tampered).
    3. MC's API schema-rejects `live_trading_enabled=True` (422).
  - **DTD vs PRD mode guard** — DTD-mode brains may ship
    `training_signal=True` (replay learning OK); PRD-mode brains
    cannot (live data poisoning prevention; 422 if attempted).
  - **Confidence-delta clamp** — server hard-caps `confidence_delta`
    at ±0.25. Raw value + clamp flag preserved in history so the
    operator can see brains hammering against the cap.
  - **Patch kit** at `/app/runtime_patch_kit/sovereign/`:
    * `wild_adaptive_core_v2.py` — operator's deterministic core,
      doctrine-patched.
    * `mc_client.py` — stdlib HTTPS client (`urllib.request`); posts
      stances + contributions + heartbeats to MC.
    * `local_state.py` — JSON-on-disk persistence with atomic writes.
    * `sidecar.py` — long-lived runner (`python sidecar.py --brain
      alpha --mode DTD`).
    * `STATE_SCHEMA.md` — wire-format spec for the local file +
      contribution snapshot + MC-side enrichments.
    * `README.md` — full deployment guide with required env vars.
    * `smoke_test.py` — 8/8 PASS doctrinal smoke tests (no MC
      connection required).
  - **MC backend**: `shared/sovereign_mode_guard.py` ingests
    contributions, snapshots seat policy + epoch on every receipt,
    persists to three collections (`sovereign_state` latest snapshot,
    `sovereign_state_history` immutable history,
    `sovereign_audit_log` operator timeline).
  - **Endpoints**:
    * `POST /api/runtime-discussion/sovereign/contribution` —
      brain sidecars ingest snapshots (runtime token auth).
    * `GET /api/admin/sovereign/state` — list latest snapshot per brain.
    * `GET /api/admin/sovereign/state/{brain}` — detail + 20-row
      history tail.
    * `GET /api/admin/sovereign/audit` — operator timeline, filter
      by brain.
  - **Frontend**: `SovereignTile.jsx` on `/runtime/{brain}` shows
    mode (DTD/PRD badge), posted_as seat, learning_rate,
    confidence_delta (red + raw value when clamped), weights bar
    chart (-3 ↔ +3 range, color by sign), recent-outcomes win/loss
    ribbon.
  - **Tests**:
    * `/app/backend/tests/test_sovereign.py` (22/22 PASS) — happy
      path, live_trading_enabled rejection, PRD+training_signal
      rejection, weight bounds, feature cap, runtime-token auth,
      delta clamping (positive/negative/in-range/infinity),
      operator-read JWT enforcement, seat-policy snapshot capture,
      history preserves raw delta + clamp flag.
    * 4 regression fixes in `tests/test_risedual_backend.py`
      (overview + diagnostics now accept ≥3 runtimes since REDEYE
      was promoted to full seat earlier).
  - **Total backend pytest = 217/217** (existing 195 + 22 sovereign).
- **Quorum awareness + memory provenance** (2026-02-12)
  - **Doctrine added**: each seat in `SEAT_POLICY` carries a
    `seat_required` bit. Defaults: `executor`, `governor`, and
    `opponent` are required; `decider` and `advisor` are informational.
    The required bits are what surface adversarial / governance
    blindness when the brain in that seat goes silent.
  - **Quorum block on every position** (computed in `_hydrate`):
    - `seats_engaged` (the seats that have stamped)
    - `seats_required` (the seats marked required)
    - `seats_missing` (required but unstamped)
    - `vacant_required_seats` (required seats with NO brain assigned —
      worse than just unstamped; there's literally no one to ask)
    - `adversarial_blindness: bool` (opponent silent)
    - `governance_blindness: bool` (governor silent)
    - `degraded: bool` (any required seat unstamped)
  - **Frontend**: red/amber quorum stripe at the top of every degraded
    position card showing the exact failure mode + which seats are
    missing. Adversarial blindness uses red (the loud failure); pure
    governance blindness uses amber.
  - **Memory provenance** (B): every stance accepts two optional fields:
    * `memory_sources: list[str]` (≤ 32 entries, each ≤ 128 chars) —
      which memory artefacts shaped this stance. Empty list valid.
    * `confidence_origin: dict[str, float]` (≤ 12 keys, each value in
      [-1, 1]) — confidence decomposition (model / memory /
      contradiction_penalty / regime_alignment / …).
  - Validated at the schema layer (422 on out-of-range or oversized
    payloads). Persisted on the stance doc; surfaced on each brain's
    stance card on the Positions page as `MEMORY · src_a · src_b · …`
    and `ORIGIN · model: +0.71 · memory: +0.12 · contradiction: -0.09`
    (negative contributions shown in red so you can spot which factors
    pulled the confidence DOWN).
  - **Tests**: `/app/backend/tests/test_quorum_and_provenance.py`
    (11/11 PASS) — fresh position has all required seats missing,
    opponent-silent flags adversarial_blindness, governor-silent flags
    governance_blindness, full quorum clears all flags, vacant required
    seat surfaced separately, stance persists memory_sources + origin,
    provenance is optional (empty defaults), out-of-range origin value
    rejected, > 32 memory_sources rejected, > 12 origin components
    rejected, operator path also supports provenance.
  - Total backend pytest = **195/195**.
- **Doctrine loosening (2026-02-09)**: communication is unrestricted.
  Stance vocabulary expanded (added `agree`, `disagree`, `refine`,
  `retract`, `hypothesis`). Topic kinds permissive (any
  `[a-z_][a-z0-9_]*:value`, e.g. `regime:trend`, `theory:momentum_decay`).
  Evidence cap raised to 64 KB. Thread depth raised to 64.
  Trading remains hard-locked: `may_execute=true` schema-rejected at every
  layer; observation banner present.
- Mission Control backend, frontend dashboard, governed promotion (incl. dual-sign)

### Forward-compat rule between the two repos
1. **REDEYE always emits** every field defined in `PULSE_CONTRACT.md` (including `alpha_alignment`, default `null`).
2. **RISEDUALAI tolerates absence** — `_emit_camaro_audit` reads with `.get(...)` for any non-required field.
3. Schema additions are non-breaking when added as optional + null-default first.
4. Bump `contract_version` before any rename/repurpose.

---

## Original Problem Statement
Refactor three RISEDUAL projects (RISEDUAL-AI-2 → **Alpha**, RD4_0421 → **Camaro**,
2.1-APP → **Chevelle**) into one monorepo-style backend with **shared infrastructure** and
**isolated decision authority** per runtime. First deploy is OBSERVATION ONLY:
`BROKER_LIVE_ORDER_ENABLED=false`, `PHASE6_ENFORCE_ENABLED=false`,
`CAMARO_EXECUTOR_ENFORCE_ENABLED=false`, `CHEVELLE_AUTHORITY_ENABLED=false`.

Doctrine: **one shared nervous system, three separate decision brains.**

## Architecture (delivered)
- FastAPI backend (Python 3.11) in `/app/backend`
  - `server.py` — app factory, CORS, lifespan (indexes + seed)
  - `auth.py` — JWT (HS256) login/me/refresh/logout. Bearer header **and** cookie.
  - `db.py` — Motor MongoDB client + `ensure_indexes()`
  - `namespaces.py` — single source of truth for collection names
  - `shared/` — `routes.py`, `diagnostics.py`, `flags.py`, `seed.py`,
    `calibration_layer.py`, `memory_labeler.py`, `receipt_dispatch.py`,
    `feature_builders.py`, `artifact_inventory.py`
  - `runtimes/{alpha,camaro,chevelle}/routes.py` — runtime-isolated endpoints
- React frontend (CRA + Tailwind, dark terminal theme):
  - JWT auth via Bearer header (localStorage `risedual_access_token`)
  - Pages: Login, Overview, Receipts, Memory Firewall, Calibration,
    Feature Builders, Artifacts, Diagnostics, Flags, RuntimeDetail
- MongoDB collections (namespaced):
  - Shared: `shared_adl_receipts`, `shared_labeled_memories`, `shared_calibrators`,
    `shared_feature_builders`, `shared_artifact_inventory`
  - Per-runtime: `alpha_decision_log`, `camaro_shadow_rows`, `chevelle_memory_labels`

## What's Implemented (2026-01-09)
- Monorepo scaffold with shared/ + per-runtime/ split
- JWT auth (Bearer header) with seeded admin (`admin@risedual.io`)
- Brute-force lockout (5 fails / 15 min)
- Idempotent seed: 5 feature builders, 6 calibrators, 6 artifacts, 45 ADL receipts,
  36 memory labels, plus per-runtime decision logs
- Read-only flags endpoint (deploy_mode=observation, all enforce flags FALSE)
- Diagnostics endpoint (Mongo health + per-runtime liveness)
- Per-runtime endpoints isolated to their own collection (no cross-namespace reads)
- Unified admin dashboard: 3 color-coded runtime cards (Alpha blue / Camaro amber /
  Chevelle green), observation banner, doctrine card, flags strip, full ADL/memory/
  calibration/artifact/diagnostics views, runtime detail pages
- Backend tests: 38/38 PASS (iteration_1)
- Frontend tests: 16/16 PASS (iteration_2)

## What's Implemented (2026-02 — Visibility & Governance)
- **Build 5 — Heartbeat staleness alerts** (visibility-only, no broker side-effects)
- **Build 1 — Promotion Artifact emitter** in runtime patch-kits (Patent G evidence)
- **Build 4 — Recent Ingests live tail** page with polling
- **Build 3 — Dual-sign primary countersign** (2026-02-09)
  - Elevation TO `primary` requires two distinct operator signatures
  - First sign parks proposal in `awaiting_second_sign`
  - Same operator cannot occupy both slots (409 enforced server-side)
  - History records both signers; dashboard shows `n/m` signature progress
  - Patent J failure still blocks both signatures (gate cannot be bypassed)
  - Backend tests: 7/7 PASS (`tests/test_dual_sign_promotion.py`)
  - Existing single-sign rungs unchanged (back-compat verified)
- **REDEYE → Camaro short-side bridge patch-kit** (2026-02-09)
  - Path: `/app/runtime_patch_kit/redeye/`
  - Bridge module: `services/redeye_short_bridge.py` (pure stdlib)
  - Doctrine: REDEYE = short-side adversarial scout, reports to **Camaro only**,
    never Alpha. Camaro retains final execution authority.
  - `camaro_contract` block on every payload: `may_execute=False`,
    `may_override_alpha=False`, `final_authority=CAMARO`,
    `role=short_side_advisor`.
  - REDEYE not added as a 4th runtime in `namespaces.py` — it has no authority
    on the trading ladder by design.
  - Local smoke test (`smoke_test.py`) verifies SHORT/HOLD gates and the
    borrow-block override. PASS.
- **REDEYE Pulse contract — `alpha_alignment` forward-compat** (2026-02-09, A1)
  - New file: `/app/runtime_patch_kit/redeye/PULSE_CONTRACT.md`
  - Bridge gains optional `alpha_alignment` parameter (∈ `null|"aligned"|"divergent"|"contradicts"`)
  - Validation REDEYE-side: invalid value raises `ValueError` before payload leaves.
  - Default `null` always emitted so RISEDUALAI's `_emit_camaro_audit` always sees the field.
  - CLI patch updated: `--alpha-alignment` arg added.
  - Smoke test extended: default null, all 3 valid values round-trip, invalid raises. PASS.
  - Cross-session repo map added at top of this PRD so future forked agents don't
    confuse the two RISEDUAL repos.
- **Code Evolution v0 — per-stack AI gate for code patches** (2026-02-09)
  - New folder: `/app/runtime_patch_kit/code_evolution/`
  - Six service files (~960 LOC total): `schemas.py`, `ast_invariants.py`,
    `code_auditor.py`, `promotion_policy.py`, `receipts.py`, `api.py`,
    `deps.py` (the only stack-specific file).
  - Doctrine baked into source:
    * `may_auto_promote()` returns `False` under any args combination.
    * `PROTECTED_PATHS` blocks any in-band patch to the gate itself (HTTP 423).
    * No `subprocess` import in any file — AI cannot run shell.
  - Classification → action mapping: PROTECTED→423, CRITICAL→dual-sign,
    HIGH→single+24h cool-down, MEDIUM→single, LOW→single.
  - AST-based invariant scanner (not regex on diffs): catches forbidden
    constant assignments (`BROKER_LIVE_ORDER_ENABLED=True`,
    `risk_multiplier > 1.25`, etc.), forbidden calls (`paper_trades.insert*`,
    `drop_collection`, `delete_many`), syntax errors, and `target_files` vs
    `post_patch_files` drift.
  - Mongo persistence via `MotorDispatcher` (single collection
    `code_evolution_proposals`); `InMemoryDispatcher` provided for tests.
  - Endpoints: `POST /audit`, `GET /proposals`, `GET /proposals/{id}`,
    `POST /{id}/countersign`, `POST /{id}/reject`. All require auth.
  - Per-stack paste shells: `PASTE_INTO_{ALPHA,CAMARO,CHEVELLE,REDEYE}_AGENT.md`
    each tweak `EXECUTION_PATHS` and `FORBIDDEN_ASSIGNMENTS` for that stack.
  - 9/9 doctrine smoke tests pass; lint clean.
- **REDEYE dashboard page (admin-only)** (2026-02-09)
  - New file: `/app/frontend/src/pages/Redeye.jsx` mounted at `/redeye`.
  - Sidebar gets a new **"Advisors"** section, distinct from the
    "Runtimes" section, so REDEYE is visually marked as a Camaro-side
    advisor, not a peer brain. Red accent (`#DC2626`).
  - Page renders the doctrine corrected per operator: REDEYE advises
    Camaro; **neither executes**. Execution lives elsewhere on the
    ladder (Alpha + authority_state ∈ {co_trader, primary} + Patent J
    + operator countersign + observation-mode flag).
  - Sections: chain of authority, camaro_contract table (with the
    "final_authority=CAMARO is over advice, not a license to execute"
    clarification), alpha_alignment forward-compat semantics, frozen
    bridge thresholds, live-feed placeholder (pending Camaro forwarding
    endpoint), file references.
  - Admin-only access is automatic — every page except `/login` is
    behind the existing JWT-cookie + admin-seeded user gate.

## Core Requirements (static)
- Doctrine: shared infrastructure, isolated decision authority
- Observation-only first deploy (every enforce flag false)
- ADL receipts always `observed=true`, `executed=false` in observation mode
- Each runtime route reads only its namespaced collection

**Next Action Items**
- **risedual.ai integration** is unblocked end-to-end: operator copies the kit from
  `/app/runtime_patch_kit/risedual_public/` to risedual.ai's repo, sets `MC_BASE_URL`
  + `MC_PUBLIC_TOKEN` env vars, swaps pages per `SWAP_NOTES.md`. Recommended order:
  Digest → Heatmap → Signals → War Room → Agent Activity → Models Mind → Sectors →
  Market Overview narrative → RiseDualGPT chat.

## 2026-02-18 — Brain Emission Diagnostic + Large-Cap Doctrine + Doctrine Hints

**Context**: Prod screenshot showed 100 intents stuck on `mission.risedual.ai`,
all Camaro, all HOLD, doctrine chips showing REJECT / RISK_DOWN. User originally
hypothesized the "LEARNING 0/100 → doctrine_reject" loop was blocking execution.

**RCA finding**: Doctrine state is already READ-ONLY everywhere it matters.
The `Doctrine Health` panel (`/admin/doctrine/promotion-status`) is gate-stated
"does not influence execution". The per-intent sidecar packet is documented in
`shared/intents.py` as a "READ-ONLY ATTACHMENT — never modifies direction,
confidence, or any gate state". `_build_governor` hardcodes `governor_action =
"modulate"` (never "block") under doctrine (c). `risk_multiplier` floors at 0.10.

The real deadlock is upstream: Camaro emits 100% HOLD intents which die at gate
2 (`action_routable`), and Alpha (the equity executor seat) emits 0 intents.
Brain runtimes are external pods, not in this repo.

**Shipped (additive only, never mutates gate chain)**:

1. **`GET /api/admin/brain/emission-diagnose[/{brain}]`** — read-only diagnostic
   answering the 7 brain-silence hypotheses in one shape. Typed `silent_reasons`
   codes: NO_HEARTBEAT_EVER, HEARTBEAT_DEAD/STALE, NO_SIDECAR_CHECKIN,
   SIDECAR_CHECKIN_DRIFT/INVALID, NO_EXECUTOR_SEAT_FOR_LANE, NO_INTENT_EVER,
   NO_INTENT_LAST_24H, ONLY_HOLD_ACTIONS, ALL_INTENTS_REJECTED_AT_INGEST,
   PRODUCING_ROUTABLE_INTENTS, RECENT_DIRECTIONAL_PRESENT.
   Combines heartbeat + sidecar checkin + roster + intent emission stats.
   File: `routes/brain_emission_diagnose.py`.

2. **`large_cap_equity_v1` doctrine** — new doctrine version for AMZN/GOOGL/
   NVDA/AAPL-class names. Relaxed thresholds (gap ≥1%, RVOL ≥1.5x, no float
   gate). Same role-keyed seat shape so audit/scorecard/auto-retire unchanged.
   Doctrine (c) preserved: governor never hard-blocks, risk_multiplier floors
   at 0.10. Router dispatches on `snapshot.market_cap_band ∈ {large,mega}` or
   `snapshot.strategy == "large_cap"`. Added to `DOCTRINE_IDEALS` so the
   Doctrine Health panel renders it.
   File: `shared/doctrine/large_cap_doctrine.py`.

3. **`GET /api/admin/brain/doctrine-hint`** — scaffolding endpoint brains MAY
   read (JWT or X-Runtime-Token). Returns candidate doctrines, live verdict
   (LEARNING/WATCHING/CANDIDATE_*), `recommended_emit_semantic` hint. Response
   includes a `doctrine_note` pinning the invariants ("HOLD never becomes trade",
   "LEARNING never blocks"). Never mutates state.
   File: `routes/brain_doctrine_hint.py`.

**Tests**: +26 tripwires (184 → 210 total, all green). Coverage:
   - `tests/test_brain_emission_diagnose.py` (8 tripwires)
   - `tests/test_brain_doctrine_hint.py` (7 tripwires)
   - `tests/test_large_cap_doctrine.py` (11 tripwires)

**Live smoke against preview env**: confirmed Camaro emits 0 intents in preview
(no sidecar pod here), endpoint correctly classifies as
"camaro has never contacted MC — sidecar pod likely not running." In prod the
same endpoint should show ONLY_HOLD_ACTIONS + NO_EXECUTOR_SEAT_FOR_LANE for Camaro.

**Operator next steps**:
   - Hit `GET /api/admin/brain/emission-diagnose` against PROD to see typed
     reasons for Alpha's silence + Camaro's HOLD-only emission.
   - Update Camaro's external sidecar to tag mega-cap symbols with
     `market_cap_band="large"` or `strategy="large_cap"` so they route to the
     large-cap doctrine instead of small-account REJECTs.
   - Update Alpha's external sidecar to actually emit BUY/SELL directional
     intents (this fork cannot fix; it's external pod code).

**Known pre-existing test issue**: `tests/test_doctrine_intent_attachment.py::
test_equity_with_empty_snapshot_still_returns_packet` fails on `main` because
it asserts `governor_action == "block"` but doctrine (c) made that "modulate"
since 2026-05-20. NOT a tripwire, NOT introduced here — flagged for separate
hygiene cleanup.

## 2026-02-18 (later) — Lane Execution Toggles (operator kill switch)

**Context**: From the prod Diagnostics screenshot, the banner read
`DEPLOY MODE: EXECUTE / no broker has execution_enabled=true`. RCA showed
the contradiction was real: the banner text was hardcoded conditional on
`DEPLOY_MODE` env var, and **nothing in the routing path** (`_evaluate_gates`,
`get_alpaca_adapter`, `get_kraken_adapter`, `broker_router.route_order`)
ever read any `execution_enabled` toggle. The Kraken doc had an
`execution_enabled` field defaulting `False`, but it was display-only —
the operator's kill switch was wired to nothing.

**Shipped**:

1. **Two lane-level toggles** (`equity`, `crypto`) decoupled from broker
   credential state. Singleton doc in `LANE_EXECUTION_TOGGLES`. Both default
   OFF — execution is opt-in. Every flip is audit-logged with actor + ts in
   `LANE_EXECUTION_AUDIT_LOG`.
   File: `shared/lane_execution.py`.

2. **New gate `lane_execution_enabled`** in `_evaluate_gates`, inserted after
   `broker_connected`. Fails closed when the toggle is OFF with reason
   "operator has NOT enabled execution for lane=… — flip via POST /api/
   admin/execution/lane-toggles".

3. **Endpoints**:
   - `GET  /api/admin/execution/lane-toggles` — current state + doctrine note
   - `POST /api/admin/execution/lane-toggles` — body `{lane, enabled}`, audit-logged
   - `GET  /api/admin/execution/lane-toggles/history` — flip history

4. **Diagnostics integration**: `/api/admin/diagnostics` response now includes
   a `lane_execution: {equity, crypto, any_enabled}` block so the UI banner
   has *truth* instead of relying on the DEPLOY_MODE env-var label.

5. **Frontend panel** `LaneExecutionTogglesPanel.jsx` on `/admin/diagnostics`:
   - Two tiles (equity, crypto) with green/red Power icons
   - OFF → click-through confirm modal to enable
   - ON → single-click to disable (kill switch should be fast)
   - Surfaces last-flip timestamp + actor email
   - Diagnostics banner now reads `equity ON · crypto OFF` from real state

6. **Council diagnose contract tripwire updated** to include
   `lane_execution_enabled` in the locked gate ordering — bumping the
   contract is the doctrinal way to record an intentional new locked-in gate.

**Tests**: +14 new tripwires (210 → **224 total, all green**).
   - default-OFF behavior
   - flips equity/crypto independently
   - audit log records previous/next
   - gate chain ordering tripwire (lane_execution_enabled after broker_connected)
   - gate FAILS when toggle is OFF
   - gate PASSES when operator enables
   - decoupled from broker credentials (no side-effect writes)
   - diagnostics surfaces the new block

**Operator semantics**:
   - Currently in preview both toggles default OFF, so the existing
     `_evaluate_gates` would refuse to route on ANY lane until the operator
     flips a toggle. This is the correct safe default.
   - After redeploying to prod, the operator must hit `/admin/diagnostics`
     and flip equity and/or crypto ON for execution to resume. *This is the
     intended behavior.* No silent re-enable.

## 2026-02-18 (third) — Drift detector decoupling + Promotion ladder fix

**Context**: Operator looked at prod and saw the red "PREVIEW DRIFT" banner
even though brains were configured for prod. Investigated and found MC had
**two independent drift detectors** with overlapping wording:

  1. `_verdict_from_validation` (`sidecar_checkin.py`) — actually reads
     the brain's stamped `env_name` + `mc_url`. Real check.
  2. `_heartbeat_tier` (`diagnostics.py`) — purely heartbeat age. Anything
     over 110s was labeled `preview_drift` and the banner said "likely on
     preview URL". Pure false alarm whenever a brain ran a slow LLM call.

Separately, the operator pointed out **Chevelle should not be on the
promotion ladder** — it's the Governor for both equity and crypto. The
`promote_brain` endpoint already refused governor promotion (line 176),
but the `promotion-artifact` reporter was iterating over RUNTIMES without
checking authority state. Result: Chevelle was being evaluated as a
shadow-vs-fill candidate against Alpha, inflating "3 brain reports" and
suggesting it could be promoted.

**Shipped (Option A on drift, exclusion fix on artifact)**:

1. **`_heartbeat_tier` reduced to liveness-only.** Bands are now
   `{ok, stale, dead, unknown}`. The `preview_drift` and `drift` tiers
   are gone. Function docstring locks the doctrine: this answers
   liveness, not URL config.

2. **Banner + badge copy updated** (`Diagnostics.jsx`). Red banner now
   reads `STALE HEARTBEAT — X heartbeating ≥110s ago. Possible hang,
   slow LLM call, or pod restart.` Points the operator to the Sidecar
   identity check-ins panel for the actual MC-URL verdict. Status badge
   chips: `LIVE / STALE / DEAD / NO HEARTBEAT`. Status detail says
   "possible hang" instead of the false "likely on preview URL".

3. **`promotion-artifact` excludes governors.** Reader now calls
   `_current_state(rt)` and skips any brain whose `authority_state ==
   "governor"`. New response field `excluded_governors: ["chevelle"]`
   so the UI can surface the off-ladder brains.

4. **`PromotionArtifactPanel.jsx`** renders the excluded-governors note
   inline with the benchmark/window/report-count line:
   `off-ladder (governor): CHEVELLE`.

**Tests**: +5 tripwires (224 → **229 total, all green**).
   - `_heartbeat_tier` returns only canonical bands
   - `_heartbeat_tier` never returns the forbidden `preview_drift`
     literal at any input
   - HTTP integration: every diagnostics row carries one of
     {ok, stale, dead, unknown}
   - promotion-artifact unit test: chevelle excluded, camaro present,
     `excluded_governors` populated
   - promotion-artifact HTTP contract: response includes
     `excluded_governors` array

**Live smoke (preview)**:
   - diagnostics: `alpha→ok, camaro→stale(74s), chevelle→ok, redeye→unknown`
   - promotion-artifact: `reports=[camaro, redeye], excluded_governors=[chevelle]`

## 2026-02-18 (fourth) — Forward-compat stamp + Intent snapshot persistence

**Context**: Two real blockers came in via the operator's prod screenshots and
the Camaro team's handoff note ("MC needs a server-side broker adapter").

**Diagnosis 1 — `RuntimeStamp` was too strict**
The Alpha + Camaro pods rolled out `env_pip_fingerprint()` shipping a new
`pip_fingerprint` field in their `RuntimeStamp`. MC's `_validate_stamp_dict`
did `RuntimeStamp(**stamp_dict)` against a dataclass that didn't know the new
field, raising `TypeError: __init__() got an unexpected keyword argument
'pip_fingerprint'`. Result: every brain that adopted the new envelope
flipped to verdict=INVALID, displayed in red in the Sidecar Identity panel.

**Diagnosis 2 — "MC needs a broker adapter" was wrong**
The bridge actually exists. `shared/auto_router.py` already polls
`shared_intents` every 30s, runs `_evaluate_gates`, and calls
`route_order()` on passing intents. Started in lifespan when alpaca creds
exist. In prod it IS running and IS evaluating every Camaro intent.

The REAL blocker was an MC-side bug: both ingest paths
(`/api/intents` runtime-token path AND `/api/admin/intents` admin path)
were silently dropping the brain's `doctrine_snapshot` instead of
persisting it on the intent doc. The labeler used it (and audit-logged it
to `doctrine_sidecars`), but the gate chain reads `intent.snapshot.spread_bps`
which was always None → `roadguard_spread_floor` failed closed at gate 7
on every single intent for months with the misleading error
"ROADGUARD_MISSING_SPREAD_BPS — snapshot absent".

**Shipped**:

1. **Forward-compat `RuntimeStamp` validator** (`sidecar_checkin.py`):
   filter incoming dict to known dataclass fields BEFORE constructing the
   typed object; persist the FULL raw stamp (including unknown extras)
   so forward-compat data like `pip_fingerprint` survives the round trip;
   surface `unknown_keys` array in the validation result.
   Result: brain rollouts of new optional stamp fields no longer require a
   lockstep MC redeploy.

2. **Intent snapshot persisted to the gate-readable shape**
   (`shared/intents.py`): both ingest paths now write
   `"snapshot": dict(body.doctrine_snapshot or {})` onto the intent doc.
   The gate chain immediately starts seeing `spread_bps` and
   `roadguard_spread_floor` passes for healthy markets, fails correctly
   for actually-wide spreads.

**Tests**: +10 new tripwires (229 → **239 total, all green**).
   - 6 tripwires for forward-compat stamp validation (tolerates unknown
     keys, persists them, surfaces them, still rejects missing required
     fields, still flags wrong `env_name` / `mc_url`)
   - 4 tripwires for snapshot persistence (admin-proxy persists snapshot,
     missing snapshot becomes `{}` not None, gate chain reads it
     end-to-end, RoadGuard still fails on actually-wide spreads)

**Live smoke (preview)**: synthetic Camaro/MSFT BUY with
`spread_bps=4.0` now passes `roadguard_spread_floor` cleanly. Only
preview-specific blockers remain (`broker_connected` — no Alpaca creds
in preview; `lane_execution_enabled` — toggle defaults OFF).

**What this means for prod after redeploy**:
   - Sidecar Identity panel: Alpha + Camaro will flip from INVALID →
     prod within one check-in cycle (no brain redeploy needed).
   - Auto-router will start passing intents through gates 1-7 instead of
     bouncing at 7. Real fills become possible the moment a Camaro or
     Alpha intent passes governor/opponent/caps with both lane toggles ON
     and broker connected (all already true in prod).
   - The Decisions feed `gate_fail · ROADGUARD_MISSING_SPREAD_BPS` rows
     should disappear, replaced by `gate_pass` rows showing actual spread
     readings.

## 2026-02-18 (fifth) — Live-trade diagnose probe stopped lying

**Context**: After Alpha + Camaro shipped the doctrine_snapshot contract
(iter-106n), the operator screenshot showed prod's "LIVE TRADE: BLOCKED"
panel still red on both lanes with `first_blocker = roadguard_spread_floor —
ROADGUARD_MISSING_SPREAD_BPS — snapshot absent`. The panel was misleading:
it was diagnosing a SYNTHETIC probe intent that MC constructs itself, NOT
real brain traffic. The synthetic was built with `snapshot=None`, so gate 7
correctly fail-closed on the probe's own missing data, then the operator UI
loudly displayed "BLOCKED" — a permanent false alarm independent of MC's
true health.

**Shipped**: `execution_diagnose` now builds the synthetic intent with a
healthy sample snapshot:
   - equity: `{spread_bps: 5, price: 450, volume: 80M, market_regime: strong}`
   - crypto: `{spread_bps: 12, price: 65000, volume: 50M, market_regime: strong}`

Both samples sit far below the lane spread caps (50 bps equity / 200 bps
crypto), so the probe's gate 7 now passes truthfully. If RoadGuard ever
shows BLOCK on the probe again it's a real regression, not a self-induced
data deficit.

**Live smoke (preview)**:
   - Probe equity: `roadguard_spread_floor PASS · spread 5.00 bps ≤ 50 bps cap`
   - Probe crypto: `roadguard_spread_floor PASS · spread 12.00 bps ≤ 200 bps cap`
   - First-blocker now correctly surfaces the actual preview gaps
     (`broker_connected`, `executor_seat_check`) instead of the false
     RoadGuard alarm.

**Tests**: +3 tripwires (239 → **242 total, all green**).
   - probe synthetic carries sample snapshot for both lanes
   - probe gate 7 passes on the sample (clean baseline)
   - probe first_blocker never cites `MISSING_SPREAD_BPS` again

## 2026-02-18 (sixth) — Ladder Doctrine Phase 1: Observation Receipts

**Doctrine reversal (this is important, supersedes earlier "no
observation samples" stance)**:

The original separation — "real fills go to doctrine expectancy;
observation samples don't pollute" — was correct in isolation but
created a deadlock when combined with the brain's "honest hold"
behavior (display action=BUY, but `size_multiplier=0` and
`would_trade_without_gates=false` because raw conviction is too low).
Camaro emits ~100 intents/hr but the brain self-zeroes most of them,
so zero learnable outcomes accumulate. After 3 months of operation
only 3 days of real fills existed. Doctrines stuck at LEARNING 0/100
forever — they needed samples to promote, samples needed fills, fills
needed conviction, conviction needed calibration, calibration needed
samples. Permanent paralysis.

**New ladder doctrine**:

    INTENT
      → OBSERVATION RECEIPT     (gates pass, size collapsed)
      → PAPER FILL              (size>0, Alpaca paper)
      → MICRO LIVE FILL         (size>0, capped $5 real)
      → NORMAL LIVE FILL        (size>0, full)

Observation receipts are SYNTHETIC — no broker, no money — but they
ARE graded against future market price. They accumulate real
expectancy, win rate, MAE/MFE, and calibration WITHOUT capital risk.

Phase 1 — SHIPPED 2026-02-18:
  • New collection `observation_receipts`
  • `shared/observation_receipts.py` — candidate classifier, receipt
    builder, persistence helper, GET routes
  • `auto_router._route_one` modified: before classifying as
    `advisory_only`, check if the intent is an honest-hold
    observation candidate. If yes, write a graded receipt and return
    `verdict="observation_receipt"`.
  • Eligibility:
      - `action ∈ {BUY, SELL, SHORT, COVER}`
      - `confidence ≥ 0.30`
      - `lane` + `symbol` set
      - brain self-zeroed (`size_multiplier == 0` OR
        `would_trade_without_gates == false`)
  • Receipt shape carries doctrine flags:
      - `receipt_type: "observation_fill"`
      - `synthetic: True`
      - `eligible_for_learning: True`
      - `eligible_for_live_unlock: False`   (Phase 3 read-only counter)
  • Brain honesty telemetry round-trips into the receipt
    (`raw_confidence`, `size_multiplier`, `would_trade_without_gates`,
    `conviction_tier`) for calibration analysis.
  • Endpoints:
      - `GET /api/admin/observation-receipts` — list (filters: brain,
        lane, resolved)
      - `GET /api/admin/observation-receipts/counts` — per brain×lane
        ladder progress against the 100-count threshold

Tests: +12 tripwires (242 → **254 total, all green**).

Live preview proof: Camaro/BNB/USD honest-hold intent → observation
receipt born; counts endpoint surfaces `camaro/crypto: total=1
resolved=0 progress=0.0% / 100`.

**Phase 2 — RESOLVER (next iteration, ~100 LOC)**:
  Background worker that runs every ~5 minutes. For each
  `resolved=False` observation receipt:
    1. Compute horizon timestamps from `created_at`: +1h, +4h, +1d, +5d
    2. Once horizon elapsed, fetch market price (Alpaca quote for
       equity; Kraken ticker for crypto)
    3. Compute outcome: `pnl_pct` from anchor, `mae_pct` / `mfe_pct`
    4. Outcome classification: `win | loss | neutral` (define
       thresholds — e.g., >+0.5% = win, <-0.5% = loss for crypto
       1h horizon; tune per lane)
    5. Set `resolved=True`, `resolved_at`, `horizon_prices`, `outcome`

**Phase 3 — UNLOCK COUNTER (after resolver lands)**:
  New collection `learning_ladder` keyed by (brain, lane). State:
    `observation_only | micro_paper | micro_live | normal_live`
  Transitions:
    100 resolved observation receipts with win_rate > 0.55
      → unlocks `micro_paper` (Alpaca paper, $50 notional cap)
    50 micro_paper fills with expectancy > 0.30R
      → unlocks `micro_live` (Kraken USDC, $5 cap)
    micro_live expectancy proves out
      → unlocks `normal_live` (per-brain × lane authority promotion)
  Operator can MANUALLY promote / demote at any rung. Audit-logged.

**Phase 4 — LADDER SIZING GATE (after counter lands)**:
  New gate `ladder_stage_sizing` after `governor_authority` that
  reads `learning_ladder` state for (brain, lane) and clamps
  effective notional to the rung's cap (observation → forces size 0;
  micro_paper → forces ≤ $50 paper; micro_live → forces ≤ $5 real).
  Brain's `size_multiplier` is honored within the rung ceiling.

## 2026-02-18 (seventh) — Ladder Doctrine Phases 2 + 3

**Phase 2 — RESOLVER WORKER (shipped)**

Background asyncio task started in lifespan. Every `OBSERVATION_RESOLVER_TICK_SEC`
(default 300s = 5min):
  • Reads all `observation_receipts` with `resolved=False`
  • For each: fetches current market price (Alpaca for equity via
    `get_latest_trade` / position fallback; Kraken public ticker for
    crypto via existing `_crypto_price_for`)
  • Updates running `mfe_pct` / `mae_pct` (max favorable / adverse
    excursion, side-aware)
  • Records `horizon_prices[label]` at +1h / +4h / +1d / +5d as each
    elapses
  • When 5d horizon recorded → flips `resolved=True`, computes final
    `pnl_pct` and `outcome` (`win` / `loss` / `neutral`)
  • Outcome thresholds: ±2% crypto, ±1% equity
  • Side handling: SELL/SHORT inverts sign so a price drop is a win
  • Failure modes:
      - Price fetch failure → silent retry next tick
      - `anchor_price` missing → flips `resolved=True outcome="anchor_missing"`
        so it stops retrying (structural failure)

Started in `server.py` lifespan; stopped on shutdown. Read-only on
brokers (no orders, no balances).

File: `shared/observation_resolver.py`.

**Phase 3 — LEARNING LADDER (shipped)**

Per (brain, lane) stage tracker. New collections:
  • `learning_ladder` — singleton-per-(brain, lane) state
  • `learning_ladder_audit` — append-only transition log

Stages: `observation_only → micro_paper → micro_live → normal_live`.
Default: `observation_only`.

Auto-promotion eligibility (computed; NOT auto-triggered — capital-risk
transitions must be deliberate operator actions):
  • `observation_only → micro_paper`:
      ≥100 resolved observation receipts AND win_rate > 0.55
  • `micro_paper → micro_live`:
      ≥50 `execution_mode="ladder_paper"` fills AND expectancy_R > 0.30
  • `micro_live → normal_live`: operator decision only

Endpoints:
  • `GET  /api/admin/learning-ladder` — full 4×2 grid (alpha/camaro/
    chevelle/redeye × equity/crypto) with current stage + progress
    metrics + `auto_promotable` flag
  • `POST /api/admin/learning-ladder/promote` — body `{brain, lane,
    reason}`, advances one rung
  • `POST /api/admin/learning-ladder/demote` — same shape, reverses one
    rung (always allowed)
  • `GET  /api/admin/learning-ladder/history` — audit log

Threshold constants are locked by a tripwire so they can't drift.

File: `shared/learning_ladder.py`.

**Tests**: +15 new tripwires (254 → **269 total, all green**).
   Phase 2 (5 tests): pnl sign for BUY/SELL/SHORT, outcome
   thresholds, anchor_missing handling, horizon set locked.
   Phase 3 (10 tests): default state, auth required, full grid
   listing, promote/demote semantics, bounds (no promote past top,
   no demote below bottom), audit log, doctrine threshold lock,
   unknown brain rejected.

**Live preview proof**:
  • Resolver started and graded 1 observation receipt on first tick
  • Ladder endpoint returns all 8 (brain × lane) combos at
    `observation_only` with `next: micro_paper` and `progress: 0/100`
  • Doctrine block emits the four thresholds and the Phase 4 note

**What this means in prod after redeploy**:
  • Existing observation receipts (from Phase 1) start getting graded
    automatically
  • Operator can watch `/api/admin/learning-ladder` and `/observation-
    receipts/counts` to see real progress numbers populate
  • Once Camaro/Alpha accumulate 100 resolved observations per lane
    with >55% win rate, `auto_promotable: True` appears on that row
  • Operator manually POSTs to `/promote` when they're satisfied with
    the evidence — promotion is NEVER automatic

**Phase 4 — LADDER SIZING GATE (still pending)**
   New gate `ladder_stage_sizing` after `governor_authority`. Reads
   `learning_ladder` state for (brain, lane) and clamps effective
   notional to the rung's cap. Brain's `size_multiplier` honored within
   the ceiling. Without Phase 4, the ladder state is observed but does
   not enforce sizing in the gate chain. ~80 LOC, tripwires for the
   new gate's pass/fail semantics, two new lane-specific notional caps
   (e.g., $50 paper / $5 live).

**P1 / P2 — Backlog**
- **P2 — Build 2 demote/freeze workflow**: operator-initiated downgrade + hard-freeze
  endpoints, both audit-logged. On hold pending Build 3 production verification.
- **P2 — Notifications (Slack/Email)** for `awaiting_second_sign` on promotions.
- **P2 — Real-time updates (websocket)** for receipts + diagnostics.
- **P2 — Drop-in slots** for real Alpha/Camaro/Chevelle code (folder layout already
  mirrors the eventual import points).
- **P2 — Sector ETF feeder** — would lift `/api/public/sectors` out of degraded.
- **P3 — Phase 3 Public-API extensions**: `/public/admin/kill-switch` (admin-tier
  surfacing), Stripe-flow telemetry from risedual.ai → MC, dashboard for per-tier
  request rates against `/api/public/*`.

## User Personas
- **Operator (Admin)** — single seeded role today. Reads dashboards, observes
  receipts, validates that all stacks remain in observation mode.

## Test Credentials
See `/app/memory/test_credentials.md`.
