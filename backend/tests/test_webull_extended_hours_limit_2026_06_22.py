"""Regression: Webull adapter LIMIT-with-slippage branch for
extended-hours equity orders.

2026-06-22 — operator-pinned. Webull rejects MARKET orders submitted
outside Regular Trading Hours (RTH). The previous workaround required
the operator to toggle `EQUITY_EXTENDED_HOURS=OFF` manually during
pre/post-market windows. v3.1 adds a programmatic LIMIT branch:

  RTH                                 → MARKET (CORE session)
  Extended hours (RoadGuard approved) → LIMIT with slippage band
                                        (ALL session)

This file pins the branch logic in `_extended_hours_branch` in
`shared/broker/webull.py` without standing up the actual Webull SDK.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, "/app/backend")


def _branch(**kwargs):
    """Convenience wrapper — imports the static method fresh each
    call so env-var changes are picked up. The helper is pure
    (no I/O), so import cost is negligible."""
    from shared.broker.webull import WebullAdapter
    return WebullAdapter._extended_hours_branch(**kwargs)


# ── RTH path ──────────────────────────────────────────────────────


def test_rth_equity_returns_limit_core():
    """Inside RTH → LIMIT / CORE / extended_hours_trading=False.
    2026-02-26 doctrine flip (operator-pinned): equity always LIMIT
    because Webull rejects MARKET+AMOUNT/QTY combos with HTTP 417
    'The time you sent is not supported.' RTH branch uses the tighter
    50bps default slippage."""
    with patch("shared.market_hours.is_equity_rth", return_value=True), \
         patch.dict(os.environ, {"WEBULL_LIMIT_SLIPPAGE_BPS": "50"}):
        order_type, limit_str, session, ext_flag = _branch(
            lane="equity", last_price=150.0, side="BUY",
        )
    assert order_type == "LIMIT"
    # 150 * (1 + 50/10000) = 150.75
    assert limit_str == "150.75"
    assert session == "CORE"
    assert ext_flag is False


# ── Crypto path ───────────────────────────────────────────────────


def test_crypto_lane_always_market():
    """Crypto trades 24/7 — Webull crypto MARKET orders fill outside
    equity RTH boundaries. The branch must never promote crypto to
    LIMIT, regardless of clock."""
    with patch("shared.market_hours.is_equity_rth", return_value=False):
        order_type, limit_str, session, ext_flag = _branch(
            lane="crypto", last_price=68000.0, side="BUY",
        )
    assert order_type == "MARKET"
    assert limit_str is None
    assert ext_flag is False


# ── Extended-hours LIMIT branch ───────────────────────────────────


def test_extended_hours_buy_promotes_to_limit_above_last():
    """Outside RTH + equity lane → LIMIT. For a BUY, the limit price
    walks above last_price by the configured slippage band so the
    order can fill against the inside ask during the thinner pre/post
    session."""
    with patch("shared.market_hours.is_equity_rth", return_value=False), \
         patch.dict(os.environ, {
             "WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS": "50",  # 0.5%
             "WEBULL_EXTENDED_HOURS_SESSION": "ALL",
         }):
        order_type, limit_str, session, ext_flag = _branch(
            lane="equity", last_price=100.00, side="BUY",
        )
    assert order_type == "LIMIT"
    # 100 * (1 + 50/10000) = 100.50
    assert limit_str == "100.50", (
        f"BUY limit must walk above last_price by slippage band. "
        f"Got {limit_str!r}"
    )
    assert session == "ALL"
    assert ext_flag is True


def test_extended_hours_sell_promotes_to_limit_below_last():
    """Mirror of the BUY case: SELL limit walks BELOW last_price."""
    with patch("shared.market_hours.is_equity_rth", return_value=False), \
         patch.dict(os.environ, {
             "WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS": "100",  # 1%
             "WEBULL_EXTENDED_HOURS_SESSION": "ALL",
         }):
        order_type, limit_str, session, ext_flag = _branch(
            lane="equity", last_price=200.00, side="SELL",
        )
    assert order_type == "LIMIT"
    # 200 * (1 - 100/10000) = 198.00
    assert limit_str == "198.00", (
        f"SELL limit must walk below last_price by slippage band. "
        f"Got {limit_str!r}"
    )
    assert session == "ALL"
    assert ext_flag is True


def test_extended_hours_slippage_env_override():
    """The slippage band is fully tunable via env — no redeploy."""
    with patch("shared.market_hours.is_equity_rth", return_value=False), \
         patch.dict(os.environ, {
             "WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS": "25",  # 0.25%
             "WEBULL_EXTENDED_HOURS_SESSION": "ALL",
         }):
        _, limit_str, _, _ = _branch(
            lane="equity", last_price=400.00, side="BUY",
        )
    # 400 * (1 + 25/10000) = 401.00
    assert limit_str == "401.00"


def test_extended_hours_session_env_override():
    """If Webull changes the documented extended-hours session value,
    we can flip without a redeploy."""
    with patch("shared.market_hours.is_equity_rth", return_value=False), \
         patch.dict(os.environ, {
             "WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS": "50",
             "WEBULL_EXTENDED_HOURS_SESSION": "EXT",
         }):
        _, _, session, _ = _branch(
            lane="equity", last_price=100.00, side="BUY",
        )
    assert session == "EXT"


# ── Defensive paths ───────────────────────────────────────────────


def test_zero_last_price_falls_back_to_market():
    """If we somehow reach the helper with no reference price, fall
    back to MARKET so the broker's clear-error path (417) surfaces
    rather than sending a degenerate limit_price of 0."""
    with patch("shared.market_hours.is_equity_rth", return_value=False):
        order_type, limit_str, session, ext_flag = _branch(
            lane="equity", last_price=0.0, side="BUY",
        )
    assert order_type == "MARKET"
    assert limit_str is None
    assert session == "CORE"
    assert ext_flag is False


def test_invalid_slippage_env_falls_back_to_default():
    """Garbage in the slippage env var must not raise — fall back
    to the documented default for the branch. Extended-hours default
    is 100 bps (wider band because the pre/post book is thinner)."""
    with patch("shared.market_hours.is_equity_rth", return_value=False), \
         patch.dict(os.environ, {
             "WEBULL_EXTENDED_HOURS_SLIPPAGE_BPS": "not-a-number",
             "WEBULL_EXTENDED_HOURS_SESSION": "ALL",
         }):
        _, limit_str, _, _ = _branch(
            lane="equity", last_price=100.00, side="BUY",
        )
    # Default 100 bps → 100 * 1.01 = 101.00
    assert limit_str == "101.00"


def test_rth_helper_exception_prefers_safe_rth_limit_path():
    """If `is_equity_rth` raises, the branch assumes RTH (safer:
    tighter 50bps slippage band). 2026-02-26 doctrine: equity always
    LIMIT — the previous MARKET/CORE fallback was retired because
    Webull rejects MARKET+AMOUNT with HTTP 417."""
    with patch("shared.market_hours.is_equity_rth",
               side_effect=RuntimeError("clock fail")), \
         patch.dict(os.environ, {"WEBULL_LIMIT_SLIPPAGE_BPS": "50"}):
        order_type, limit_str, session, ext_flag = _branch(
            lane="equity", last_price=100.00, side="BUY",
        )
    assert order_type == "LIMIT"
    # RTH-assumed fallback → 50bps slippage → 100 * 1.005 = 100.50
    assert limit_str == "100.50"
    assert session == "CORE"
    assert ext_flag is False
