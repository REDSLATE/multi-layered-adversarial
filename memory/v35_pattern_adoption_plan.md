# v3.5 + Pattern-Exit Adoption Plan — parked until after Mon 2026-08-03 live session
(written 2026-08-02; operator uploaded risedual_v3_5_merged.py and bearish-pattern sketch;
skipped scope questions → best-judgment decisions recorded below)

Artifact: /tmp/v35.py (2118 lines, re-download from
https://customer-assets-m6fa6gv7.emergentagent.net/job_multi-brain-backbone/artifacts/ztm8x5e9_risedual_v3_5_merged.py if gone)

## Operator's v3.5 directives (from suggestion table)
Adopt: portfolio ranking (OpportunityEngine), adaptive weights (AdaptiveKernel),
capital pressure, opportunity-first discovery.
Avoid at SIGNAL layer: min-confidence gates, council voting, multiple governors,
quality grades, mandatory agreement, HOLD-by-default.
Best-judgment interpretation (announced to operator, unobjected): SAFETY layer
(risk sizing, BUY allowlist, entry timing, broker checks) stays untouched.

## Best-judgment scope decisions (operator skipped the ask)
- Plan-only now; build after Monday observation (consistent with "park until after tomorrow").
- OpportunityEngine ships as a PARALLEL source first (stack="opportunity", like momentum),
  NOT an arbiter replacement — compare live sessions, then cut over if it outperforms.
- CapitalPressure REDUCE legs execute ONLY via the existing exit monitor machinery;
  rotation cap knob (default max 3/day) to prevent churn.

## Build units (each independently testable)
1. **AdaptiveKernel** (`shared/kernel/adaptive_weights.py` + loop or piggyback tick):
   per-stack weight 0.2–2.0 from realized outcomes in Mongo (closed exit plans /
   executions joined to origin stack): needs >=10 trades; sharpe_approx/win_rate/expectancy
   formula per v35.py lines 1988-2010. Weight doc `runtime_flags._id=adaptive_kernel`.
   Consumers: (a) OpportunityEngine rank multiply, (b) OPTIONAL later: governor multiplier
   blend for existing brains. Tile column in Mission Control.
2. **OpportunityEngine** (`shared/opportunity/engine.py` + scanner-style loop, DISARMED default):
   one pass over combined universe (crypto allowlist + equity live_universe RTH):
   collect per-stack opinions — v1 reuses existing brain opinion surfaces if callable
   in-process; otherwise derive rank_score from existing evidence docs. Weighted best
   opinion picks direction; disagreement = multiplier max(0.55, 1-opp_strength*0.4), never veto.
   Top-N (knob, default 5) emit BUY intents stack="opportunity" through submit_intent_in_process
   (full gate chain). Dedup via economic_fingerprint (below). IntentIn Literal += "opportunity".
3. **Economic fingerprint dedup** (`shared/opportunity/fingerprint.py`):
   sha256(symbol, strategy, direction, tf, round(entry/zone)), zone=max(0.25*ATR, 0.1% price);
   check recent intents (24h) before emission; ALSO offer to arbiter emit path as upgrade
   to duplicate-stance firewall (flagged, observe-first).
4. **CapitalPressure** (`shared/opportunity/capital_pressure.py`):
   on "portfolio full" (seat/max positions), compare new conviction vs weakest open
   position expected_edge (approximate edge = kernel weight * origin confidence, or realized
   unrealized-pnl trend); if gap > threshold 0.15 → REDUCE half via exit monitor
   partial-close (verify monitor supports partial qty close; else full-close smallest),
   then allow entry. Knobs: enabled(False), threshold, max_rotations_per_day=3.
   Receipts: `capital_rotations` doc.
5. **Sell-Point Watcher — bearish patterns on HELD tickers** (`shared/exits/pattern_watch.py`):
   pivot detection (fractal highs/lows, k=2) on 5m bars for symbols with active exit plans:
   - double_top: 2 pivot highs within 0.5*ATR, close < valley neckline → action
   - head_shoulders: 3 pivot highs, middle max, close < neckline(retrace lows) → action
   - rising_wedge: HH+HL converging slopes + volume fade → tighten stop to lower trendline;
     trendline break → action
   Actions (knob per pattern: off | tighten | exit; default TIGHTEN, observe-first logs):
   tighten = raise plan stop_price (never lower); exit = trigger monitor close ladder.
   Receipts with pivots/neckline/confirm bar. Tile: pattern events feed.
6. **Entry-side bearish-structure flag**: entry_timing receipt gains
   `bearish_structure_overhead` (observe-only field, no blocking until data reviewed).
7. **Re-arm watcher**: structure_breakdown verdict enriched with pattern name when one
   of the three detectors confirms (receipt-only change).

## NOT porting from v35.py
Parallel execution engine/broker router/idempotency store/perception resampler/
in-memory portfolio (RISEDUAL's receipt-audited equivalents stay the single boundary);
its 5 toy strategy models; kill-switch modes fold into planned Account Guardian (see
forven_adoption_plan.md); conversational query interface (Mission Control covers it —
could become a chat box later if operator asks).

## Sequencing (combined with Forven plan)
Mon: observe live (momentum equity lane, re-arm prod proof).
Then: Account Guardian → Tape Quality Gate → AdaptiveKernel → Sell-Point Watcher →
OpportunityEngine (parallel) → CapitalPressure → fingerprint dedup → Failed-open latch →
Replay Gauntlet. Re-order freely per operator priority; each unit stands alone.

## Addendum (2026-08-02): Candlestick confirmation vocabulary (operator cheat-sheets)
Small module `shared/market_data/candles.py` — pure OHLC classifiers, receipt-friendly:
engulfing (bull/bear), morning_star, evening_star, three_white_soldiers,
three_black_crows, doji_family (indecision flag), marubozu (conviction flag),
shooting_star, hanging_man, gravestone_doji.
SKIP (low incremental edge, agreed): harami, kicker, tweezers, piercing, dark cloud.
Wiring (all observe-first, knob-gated):
1. entry_rearm reaccel test: strong confirm (engulfing/morning_star/soldiers) vs
   weak (doji/spinning_top → keep waiting) — replaces bare green-bar check.
2. Sell-Point Watcher: candle confirm at structure peaks (shooting_star/gravestone/
   bear engulfing/evening_star at double-top 2nd peak or H&S head) upgrades
   tighten→exit; three_black_crows = standalone tighten trigger on held winners.
3. Momentum scanner transition-bar quality flag (marubozu/engulfing=conviction,
   spinning_top=fakeout candidate → skip or halve confidence).
4. entry_timing receipt field `topping_candle_at_submit` (observe-only).
