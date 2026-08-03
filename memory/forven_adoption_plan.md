# Forven Adoption Plan — parked until after Monday's live session (written 2026-08-02)

Source studied: https://github.com/judder659/Forven (AGPL v3 — REIMPLEMENT concepts,
NEVER copy code verbatim; copyleft applies to network services).
Local clone was at /tmp/forven (disposable). Key reference files:
`forven/exchange/risk.py` (kill-switch/limits/cooldowns), `forven/dataeng/quality_gate.py`
(tape quality), `forven/gauntlet/*` + `forven/robustness/engine.py` (gauntlet),
`forven/gauntlet/deflated_sharpe.py` (DSR).

Operator decision: park all of it, observe momentum scanner + entry re-arm live
Mon 2026-08-03, then drop in. Build order below is the agreed priority.

---

## A. Account Guardian (P0 — protects the account)
New module `shared/risk_sizer/account_guardian.py` + loop task in lifespan (60s tick).

1. **Drawdown kill-switch**
   - Track high-water mark of total account equity (Webull equity + Kraken balance)
     in `account_equity_state` doc {hwm, updated_at, source_breakdown}.
   - Knob `runtime_flags._id=account_guardian`: {enabled, max_drawdown_pct: 10,
     daily_loss_pct: 5, source_loss_streak: 3, source_cooldown_h: 24}.
   - Trip: equity <= hwm*(1-max_drawdown_pct/100) →
     a) `shared.broker_freeze.freeze(actor="account_guardian", reason="drawdown_killswitch")`
     b) master switch OFF (trading_controls)
     c) instruct exit monitor to close all open plans (reuse existing escalation ladder;
        consider Forven's escalating slippage tiers 300/600/1000bps for the crypto
        marketable-limit ladder — currently ours escalates to market without widening).
     d) receipt doc in `guardian_events` + red banner tile.
   - UN-trip is MANUAL ONLY (operator re-arms via tile) — never auto-resume.
2. **Daily loss limit**
   - Midnight-ET anchored session start equity snapshot; if equity <= start*(1-daily_loss_pct/100)
     → master switch OFF until next session (guardian re-enables at session roll ONLY if
     it was guardian-disabled; never overrides operator-disabled).
3. **Per-source loss cooldown**
   - Consume realized exit outcomes (exit receipts / closed plans) attributed to origin
     stack (camino/barracuda/hellcat/gto/momentum). N consecutive losses (knob) →
     write `source_cooldowns` doc; ROUTER gate `_gate_seat`-adjacent check blocks BUYs
     from that stack until expiry. SELLs/exits NEVER blocked.
4. **UI**: `AccountGuardianTile.jsx` (Overview): equity vs HWM sparkline optional later;
   v1 = numbers + armed toggle + trip history + manual re-arm button.
   Routes `GET/POST /api/admin/account-guardian` (+ manifest entry + route snapshot regen).
5. **Tests**: trip math (hwm/daily anchors), manual-rearm-only, cooldown streak counting,
   exits-never-blocked, master-switch interplay (guardian never re-enables an
   operator-disabled switch).

## A2. Tape Quality Gate (P0.5 — prevents the no_tape/NO_TIMING_DATA class)
New `shared/market_data/tape_quality.py`:
- `assess(bars, tf, window_min) -> {ok, completeness, max_gap_bars, age_sec, reason}`
- Thresholds (knob doc `tape_quality`): completeness >= 0.95, max interior gap <= 12 bars,
  staleness <= 3*tf.
- Call sites (3): momentum scanner `no_tape`→ granular reasons (thin_tape/gappy_tape/stale_tape);
  entry_timing `_load_bars` consumers (NO_TIMING_DATA receipt gains tape_quality field);
  entry_rearm `_tick` (skip-with-reason instead of silent thin-bars skip).
- Stamp a compact fingerprint {n, span, last_ts} into receipts (Forven "dataset_fingerprint" idea).
- Tests: synthetic tapes for each failure mode + pass-through.

## A3. Failed-Open Latch (P1 — double-order protection)
- When broker submit returns ambiguous (timeout / no order id), write
  `symbol_latches` doc {symbol, lane, reason:"failed_open_reconcile", expires_at (bounded 15m)}.
- Router BUY path checks latch before submit; reconciliation (existing exec reconcile)
  clears it early on confirmation either way.
- Tests: latch set on ambiguous error bucket, cleared on reconcile, expiry bound, SELLs exempt.

## B. Replay Gauntlet (P1.5 — evidence-gated knob tuning)
New `shared/replay/` package (offline, never touches live collections for writes):
1. `replay_entry_timing.py`: re-run `entry_timing.evaluate` + `entry_rearm.detect_pullback_reentry`
   over stored `shared_ohlcv_bars` history per symbol; simulate block→watch→rearm→hypothetical
   fill at reaccel close; score vs "buy at block price" baseline (chase avoided %, missed %,
   improvement %). Knob-jitter mode: run grid over extension caps ±25% (Forven param-jitter idea).
2. `replay_momentum.py`: same for scanner (momentum_score/confirmation_price over history,
   entry at transition close, exit +5/-3 → win rate, expectancy). Cost-stress mode: widen
   spreads 2x/3x (Forven cost-stress idea).
3. Endpoint `POST /api/admin/replay/run` (async job, results doc) + results panel or CLI-first.
4. Later: Deflated Sharpe on replay outcomes (selection-bias guard) before promoting knob changes.
5. Note: bars TTL limits history depth — if deeper replay wanted, add a `bars_archive`
   (no TTL, capped symbols) toggle FIRST and let it accumulate.

## Explicitly NOT adopting (agreed)
- Forven agent research team / strategy generation / evolution / bot factory
- Hyperliquid/CCXT layer, paper-trading simulator, SvelteKit dashboard, SQLite/ChromaDB,
  MCP server, Discord bot, shell tool, croniter scheduler, DuckDB parquet lake,
  OKX liquidation feed (parked as future crypto regime-signal idea), their 10 strategies.

## Sequencing agreed with operator
1. Watch Monday's live session (momentum equity lane + re-arm prod proof) FIRST.
2. Then: A → A2 → A3 → B. Each is independently testable and drop-in.
