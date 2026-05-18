"""Regime fingerprinting + crypto symbol detection — shared primitives.

Doctrine pin (2026-05-18): this module exists SOLELY to break the
circular import between `shared/intents.py` and `shared/hypothesis.py`.
Both modules used to lazy-import these symbols from each other inside
function bodies (with `# noqa: WPS433`). Lifting them here lets both
sides import normally at module-load time.

What lives here:
  * REGIME_FP_KEYS      — canonical 6-key fingerprint key set
  * _regime_fingerprint — coarse-bucket fingerprint extractor
  * _looks_like_crypto  — heuristic for unambiguously-crypto symbols

Nothing else should accrete here. If a primitive is used by exactly one
of intents.py / hypothesis.py, keep it in that file.
"""
from __future__ import annotations

from typing import Optional  # noqa: F401  (kept for forward compat with downstream callers)


# ───────────────────────── regime fingerprinting ─────────────────────────

# Canonical regime-fingerprint key set. Brains and the server-side
# enrichment hook (see shared/intents.py) target this set; IntentIn's
# evidence validator rejects unknown keys to keep memory recall honest.
REGIME_FP_KEYS: frozenset[str] = frozenset({
    "rsi_band",
    "macd_hist_sign",
    "bb_band",
    "trend_direction",
    "volume_band",
    "volatility_band",
})


def _regime_fingerprint(indicators: dict | None) -> dict:
    """6-key coarse buckets used to find 'similar past setups' across the
    brain's history. Naive on purpose — we want a 5-row recall, not a
    research-grade similarity search.

    Doctrine (2026-02-16 rev2): upgraded from 3 → 6 keys so each setup
    points to a higher-resolution slice of memory. Misalignment on more
    than 2 keys disqualifies a recall match (see hypothesis._build_role
    `regime_fp.$or` query).

    Keys:
      rsi_band          oversold / weak / neutral / strong / overbought
      macd_hist_sign    positive / negative / flat
      bb_band           lower / mid_low / mid_high / upper
      trend_direction   up / down / flat
      volume_band       quiet / normal / high / spike
      volatility_band   calm / normal / elevated / violent

    All keys are optional — if the snapshot is missing the input metric,
    we omit the corresponding key rather than guess. A fingerprint with
    < 6 keys is acceptable and will simply match more loosely.
    """
    if not indicators:
        return {}
    fp: dict = {}

    # 1. RSI band — momentum oscillator zones.
    rsi = indicators.get("rsi14")
    if isinstance(rsi, (int, float)):
        if rsi < 30:
            fp["rsi_band"] = "oversold"
        elif rsi < 45:
            fp["rsi_band"] = "weak"
        elif rsi <= 55:
            fp["rsi_band"] = "neutral"
        elif rsi <= 70:
            fp["rsi_band"] = "strong"
        else:
            fp["rsi_band"] = "overbought"

    # 2. MACD histogram sign — momentum direction.
    macd = indicators.get("macd") or {}
    hist = macd.get("hist")
    if isinstance(hist, (int, float)):
        fp["macd_hist_sign"] = "positive" if hist > 0 else ("negative" if hist < 0 else "flat")

    # 3. Bollinger position — mean-reversion vs extension.
    bb = indicators.get("bb") or {}
    pos = bb.get("position")
    if isinstance(pos, (int, float)):
        if pos < 0.25:
            fp["bb_band"] = "lower"
        elif pos < 0.55:
            fp["bb_band"] = "mid_low"
        elif pos < 0.75:
            fp["bb_band"] = "mid_high"
        else:
            fp["bb_band"] = "upper"

    # 4. Trend direction — price vs SMA50 (preferred) or EMA20 fallback.
    #    Threshold ±0.5% so noise doesn't whip the label.
    price = indicators.get("price") or indicators.get("close")
    sma50 = indicators.get("sma50")
    ema20 = indicators.get("ema20")
    anchor = sma50 if isinstance(sma50, (int, float)) else (
        ema20 if isinstance(ema20, (int, float)) else None
    )
    if isinstance(price, (int, float)) and isinstance(anchor, (int, float)) and anchor > 0:
        delta_pct = (price - anchor) / anchor
        if delta_pct > 0.005:
            fp["trend_direction"] = "up"
        elif delta_pct < -0.005:
            fp["trend_direction"] = "down"
        else:
            fp["trend_direction"] = "flat"

    # 5. Volume band — current bar volume vs 20-day average.
    vol = indicators.get("volume")
    vol_avg = indicators.get("volume_avg20") or indicators.get("avg_volume")
    if isinstance(vol, (int, float)) and isinstance(vol_avg, (int, float)) and vol_avg > 0:
        ratio = vol / vol_avg
        if ratio < 0.6:
            fp["volume_band"] = "quiet"
        elif ratio < 1.3:
            fp["volume_band"] = "normal"
        elif ratio < 2.5:
            fp["volume_band"] = "high"
        else:
            fp["volume_band"] = "spike"

    # 6. Volatility band — ATR% (ATR / price) or rolling stddev.
    atr = indicators.get("atr14")
    if isinstance(atr, (int, float)) and isinstance(price, (int, float)) and price > 0:
        atr_pct = atr / price
        if atr_pct < 0.008:
            fp["volatility_band"] = "calm"
        elif atr_pct < 0.020:
            fp["volatility_band"] = "normal"
        elif atr_pct < 0.040:
            fp["volatility_band"] = "elevated"
        else:
            fp["volatility_band"] = "violent"

    return fp


# ───────────────────────── crypto symbol detection ─────────────────────────

_CRYPTO_BASE_SYMBOLS: frozenset[str] = frozenset({
    "BTC", "XBT", "ETH", "SOL", "BNB", "DOGE", "ADA", "AVAX",
    "MATIC", "DOT", "LTC", "LINK", "UNI", "ATOM", "TRX", "XRP",
    "XLM", "ETC", "FIL", "NEAR", "ARB", "OP", "INJ", "TIA",
})

_CRYPTO_QUOTE_SYMBOLS: frozenset[str] = frozenset({
    "USD", "USDT", "USDC", "EUR", "GBP", "JPY", "BTC", "ETH",
})

_CRYPTO_FUSED_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "USD")


def _looks_like_crypto(symbol: str) -> bool:
    """Heuristic: does `symbol` unambiguously look like a crypto pair?

    Matches the common shapes Kraken / Camaro emit:
      - BTC/USD, ETH/USDT, SOL/USD, BNB-USD, BTC-USDT
      - XBTUSD (Kraken's BTC alias), pairs with USD/USDT/USDC suffixes
    We deliberately do NOT match bare 3-letter tickers like "BTC" or "ETH"
    — too easy to collide with equity symbols, and a real ambiguity
    case the operator should resolve.
    """
    if not symbol or not isinstance(symbol, str):
        return False
    s = symbol.upper().strip()
    # Hard rule: must contain a separator OR be a known fused pair shape.
    if "/" in s or "-" in s:
        # Probably a pair like BTC/USD or BTC-USDT. Trust it as crypto if
        # the quote side looks like a fiat / stablecoin.
        sep = "/" if "/" in s else "-"
        parts = s.split(sep)
        if len(parts) != 2:
            return False
        _, quote = parts
        return quote in _CRYPTO_QUOTE_SYMBOLS
    # Kraken-style fused pairs (XBTUSD, BTCUSDT, ETHUSD). Require >=6 chars
    # and a known suffix to avoid colliding with NYSE 4-5 letter tickers.
    for suffix in _CRYPTO_FUSED_SUFFIXES:
        if len(s) >= len(suffix) + 3 and s.endswith(suffix):
            base = s[: -len(suffix)]
            if base in _CRYPTO_BASE_SYMBOLS:
                return True
    return False
