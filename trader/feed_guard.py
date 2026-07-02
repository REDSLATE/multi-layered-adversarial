"""Input-validation guard for the trader's decision path.

Doctrine pin (2026-07-02, "no more feed-corruption trades"):
    Before an L1 quote is allowed to influence a brain decision, it
    passes through this guard. Rejections are receipts too — every
    reject writes a `quote_rejected:<reason>` receipt so the operator
    sees the tape reflecting *why* the trader stayed hands-off.

    Rejections are cheap: dict lookup + a few comparisons. No I/O.

Checks (all env-tunable):
    1. Staleness:       age_ms > TRADER_GUARD_MAX_AGE_MS (default 30_000)
    2. Absolute spread: bps > TRADER_GUARD_MAX_SPREAD_BPS (default 500)
    3. Spread anomaly:  bps > TRADER_GUARD_SPREAD_ANOM_MULT × median_last_N
    4. Price jump:      |px - median| / median > TRADER_GUARD_MAX_PX_JUMP_PCT
    5. Dual-source divergence: if we have two sources (e.g. MQTT +
       HTTP snapshot for the same equity), the newer readings must
       agree within TRADER_GUARD_DUAL_SRC_MAX_BPS bps. Divergence >
       that threshold flags a suspected feed mismatch.

Personalities preserved: this guard NEVER inspects the brains, only
the market data. Downstream — Camino, Barracuda, Hellcat, GTO — get
the exact same clean input or nothing at all.
"""
from __future__ import annotations

import os
import statistics
from typing import Optional

from trader import spread, store


def _env_float(key: str, default: float) -> float:
    try:
        v = os.environ.get(key)
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        v = os.environ.get(key)
        if v is None or v == "":
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def _median(vals: list[float]) -> Optional[float]:
    if not vals:
        return None
    try:
        return statistics.median(vals)
    except statistics.StatisticsError:
        return None


def validate_l1(symbol: str,
                quote_row: Optional[dict],
                lane: str = "equity") -> tuple[bool, str, dict]:
    """Vet an L1 reading before it reaches the brains.

    Returns `(ok, reason, details)`.
      * ok=True   → reason='ok', details describes what was checked.
      * ok=False  → reason='<check_name>:<observed>', receipt row is
                     built by the caller from `details`.

    Contract: this MUST return quickly. Falls open on missing data —
    an absent quote is not a rejection (the poller may not have
    warmed the cache yet), it just leaves the brains to work off
    whatever OHLC data they have. Only ACTIVELY BAD readings get
    rejected; silence is treated as neutral.
    """
    details: dict = {"symbol": symbol, "lane": lane}
    if not quote_row:
        return True, "no_quote_cache", details

    # 1. Staleness — most important. A stale reading is worse than
    # no reading because it looks fresh but isn't.
    max_age = _env_int("TRADER_GUARD_MAX_AGE_MS", 30_000)
    age_ms = quote_row.get("l1_age_ms") or quote_row.get("quote_age_ms")
    if age_ms is not None:
        details["age_ms"] = age_ms
        if age_ms > max_age:
            return False, f"stale_quote:{age_ms}ms>{max_age}ms", details

    # 2. Absolute spread ceiling. A spread wider than N bps is almost
    # certainly a feed error — real markets rarely quote that wide
    # outside of halts, and halt-adjacent trading is separately gated.
    spread_bps = quote_row.get("spread_bps")
    max_spread = _env_float("TRADER_GUARD_MAX_SPREAD_BPS", 500.0)
    if spread_bps is not None:
        details["spread_bps"] = spread_bps
        if spread_bps > max_spread:
            return (
                False,
                f"spread_absurd:{spread_bps:.1f}bps>{max_spread:.1f}bps",
                details,
            )

    # 3. Spread anomaly relative to recent median. This is the
    # sniper-shot against the old corruption pattern — a normal 3
    # bps TSLA spread suddenly showing 60 bps is a red flag even
    # if it's under the absolute ceiling.
    hist = store.recent_spread_ticks(pair=symbol, limit=30)
    hist_bps = [
        float(r["spread_bps"]) for r in hist
        if r.get("spread_bps") is not None
    ]
    median_bps = _median(hist_bps)
    if median_bps is not None and median_bps > 0 and spread_bps is not None:
        mult_cap = _env_float("TRADER_GUARD_SPREAD_ANOM_MULT", 5.0)
        details["spread_median_bps"] = round(median_bps, 4)
        if spread_bps > median_bps * mult_cap:
            return (
                False,
                (
                    f"spread_anomaly:{spread_bps:.2f}bps"
                    f">{mult_cap:.1f}×median({median_bps:.2f})"
                ),
                details,
            )

    # 4. Price jump vs recent median (last-price or bid/ask mid).
    px_now = quote_row.get("last")
    if px_now is None and quote_row.get("bid") and quote_row.get("ask"):
        px_now = (float(quote_row["bid"]) + float(quote_row["ask"])) / 2.0
    if px_now is not None and hist:
        px_list = []
        for r in hist:
            if r.get("last") is not None:
                px_list.append(float(r["last"]))
            elif r.get("bid") and r.get("ask"):
                px_list.append((float(r["bid"]) + float(r["ask"])) / 2.0)
        median_px = _median(px_list)
        if median_px and median_px > 0:
            jump = abs(px_now - median_px) / median_px
            max_jump = _env_float("TRADER_GUARD_MAX_PX_JUMP_PCT", 0.05)
            details["px_now"] = px_now
            details["px_median"] = round(median_px, 6)
            details["px_jump_pct"] = round(jump * 100, 4)
            if jump > max_jump:
                return (
                    False,
                    (
                        f"price_jump:{jump * 100:.2f}%"
                        f">{max_jump * 100:.2f}%"
                    ),
                    details,
                )

    # 5. Dual-source divergence. Only meaningful when we have BOTH
    # sources fresh. We compare the cache's current row (from
    # whichever source produced it last) against the most-recent
    # OTHER-source reading in the SQLite tape.
    current_src = quote_row.get("source")
    if current_src and spread_bps is not None:
        max_div = _env_float("TRADER_GUARD_DUAL_SRC_MAX_BPS", 30.0)
        max_div_age = _env_int("TRADER_GUARD_DUAL_SRC_MAX_AGE_S", 60)
        # Look at up to 20 recent ticks and pick the newest one from
        # a *different* source that's within max_div_age seconds.
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).timestamp()
        for r in hist[:20]:
            src = r.get("source")
            if not src or src == current_src:
                continue
            try:
                r_ts = datetime.fromisoformat(r["ts"]).timestamp()
            except (KeyError, ValueError, TypeError):
                continue
            if now_ts - r_ts > max_div_age:
                continue
            other_bps = r.get("spread_bps")
            if other_bps is None:
                continue
            divergence = abs(float(other_bps) - float(spread_bps))
            details["dual_src_other"] = src
            details["dual_src_other_bps"] = float(other_bps)
            details["dual_src_divergence_bps"] = round(divergence, 4)
            if divergence > max_div:
                return (
                    False,
                    (
                        f"dual_src_divergence:{divergence:.2f}bps"
                        f">{max_div:.2f}bps ({current_src}vs{src})"
                    ),
                    details,
                )
            break

    return True, "ok", details
