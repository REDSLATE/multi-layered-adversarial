"""BUY-allowlist visibility + override scaffold (2026-07-31).

Operator directive: keep the allowlist, make it visible; prepare but
DO NOT enable an A-quality override. These tests pin:
  * symbol normalization — "SOON/USD" / "SOONUSD" / Kraken internals
    can never drift into distinct allowlist entries
  * the override ships DISABLED and never fires on doctrine score
    alone
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer.buy_allowlist import normalize_crypto_symbol  # noqa: E402

pytestmark = pytest.mark.tripwire


@pytest.mark.parametrize("raw,expected", [
    ("SOON/USD", "SOON/USD"),
    ("SOONUSD", "SOON/USD"),
    ("soon", "SOON/USD"),
    ("SOON-USD", "SOON/USD"),
    ("CRYPTO:SOON-USD", "SOON/USD"),
    ("XBT", "BTC/USD"),
    ("XXBTZUSD", "BTC/USD"),
    ("XBTUSD", "BTC/USD"),
    ("BTC/USDT", "BTC/USD"),
    ("XDG", "DOGE/USD"),
    ("", ""),
])
def test_normalize_crypto_symbol(raw, expected):
    assert normalize_crypto_symbol(raw) == expected


def test_usd_base_is_not_swallowed():
    # a hypothetical base literally named "USD-something" must not be
    # stripped into nothing
    assert normalize_crypto_symbol("USDC") == "USDC/USD"


def test_override_ships_disabled():
    from shared.risk_sizer.allowlist_override import DEFAULT_POLICY
    assert DEFAULT_POLICY["enabled"] is False, (
        "the A-quality allowlist override must ship DISABLED — "
        "operator directive 2026-07-31: visibility first, no automatic "
        "override"
    )


@pytest.mark.asyncio
async def test_override_never_applies_when_disabled(monkeypatch):
    from shared.risk_sizer import allowlist_override as mod
    monkeypatch.setattr(mod, "_cache", {"at": 0.0, "doc": None})

    async def _fake_policy():
        return dict(mod.DEFAULT_POLICY)
    monkeypatch.setattr(mod, "get_override_policy", _fake_policy)

    # a PERFECT intent — A-quality, high score, tight spread, bars on
    # file — must still be refused while the policy is disabled
    perfect = {
        "symbol": "SOON/USD", "confidence": 0.99,
        "doctrine_packet": {"base_labels": {"quality": "A_QUALITY", "score": 0.99}},
        "evidence": {"research_status": "bars_ok", "spread_bps": 2.0},
    }
    applies, receipt = await mod.override_applies(perfect)
    assert applies is False
    assert receipt["reason"] == "override_disabled"


@pytest.mark.asyncio
async def test_override_requires_more_than_doctrine_score(monkeypatch):
    """Even when ENABLED, a high doctrine score alone must not
    qualify — thin/new pairs fake strong scores from short history."""
    from shared.risk_sizer import allowlist_override as mod

    async def _enabled_policy():
        return {**mod.DEFAULT_POLICY, "enabled": True}
    monkeypatch.setattr(mod, "get_override_policy", _enabled_policy)

    score_only = {
        "symbol": "SOON/USD", "confidence": 0.99,
        "doctrine_packet": {"base_labels": {"quality": "A_QUALITY", "score": 0.99}},
        "evidence": {"research_status": "no_bars_on_file"},  # no bars, no spread
    }
    applies, receipt = await mod.override_applies(score_only)
    assert applies is False
    assert receipt["checks"]["bars_on_file"] is False
    assert receipt["checks"]["spread"] is False
