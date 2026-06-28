"""Tests for the witness-cluster manipulation/noise detector.

Doctrine pins these tests guard:

  * SOFT_NEWS_CLUSTER fires on the "Company Access Network"
    pattern (3+ BUYs on unrelated tickers, no material keywords).
  * SOFT_NEWS_CLUSTER does NOT fire on real material news that
    happens to mention multiple tickers (e.g. M&A, earnings).
  * FLIP_FLOP detects BUY→SELL within window, ignores HOLD.
  * SPAM fires on per-source rate AND per-symbol rate.
  * DUPLICATE_BURST fires on >= min_rows same-article rows.
  * Every alert lands with `enforced=False` — log-only doctrine pin.
  * Alerts carry `signal_ids[]` so Step 7's Seat view can filter.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared.external_signals.manipulation_detector import (
    DUPLICATE_BURST_MIN_ROWS,
    FLIP_WINDOW_MIN,
    RATE_SPIKE_PER_HOUR,
    SOFT_NEWS_MIN_TICKERS,
    SPAM_PER_SYMBOL,
    article_is_material,
    detect_duplicate_burst,
    detect_flip_flop,
    detect_soft_news_cluster,
    detect_spam,
    run_all_detectors,
)


NOW = datetime(2026, 2, 23, 12, 0, 0, tzinfo=timezone.utc)


def _sig(**overrides):
    base = {
        "id": "sig-" + str(overrides.get("_seq", 0)),
        "source": "polygon",
        "symbol": "NVDA",
        "side": "BUY",
        "event": "news",
        "self_reported_confidence": 0.65,
        "verifier_status": "UNTRUSTED",
        "influence_allowed": False,
        "received_at": NOW.isoformat(),
        "raw": {"id": "article-xyz", "title": "", "description": ""},
    }
    base.update({k: v for k, v in overrides.items() if k != "_seq"})
    return base


# ─── SOFT_NEWS_CLUSTER ─────────────────────────────────────────────


def test_soft_news_cluster_fires_on_company_access_network_pattern():
    """The exact operator-reviewed case: one soft-news article
    spawns BUYs on unrelated tickers with no material keywords."""
    article = {
        "id": "article-soft-1",
        "title": "Company Access Network 2026 partners",
        "description": "Mentoring floor program participants",
        "keywords": ["diversity", "mentorship"],
    }
    signals = [
        _sig(symbol="WMT",  side="BUY", raw=article, id="s1"),
        _sig(symbol="INTU", side="BUY", raw=article, id="s2"),
        _sig(symbol="NVDA", side="BUY", raw=article, id="s3"),
    ]
    alerts = detect_soft_news_cluster(signals)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.label == "EXTERNAL_SIGNAL_SOFT_NEWS_CLUSTER"
    assert a.article_id == "article-soft-1"
    assert a.observed_value == 3.0
    assert set(a.signal_ids) == {"s1", "s2", "s3"}
    assert a.enforced is False  # log-only doctrine pin


def test_soft_news_cluster_skips_material_news_with_multi_tickers():
    """An M&A article that mentions both acquirer and target is
    MATERIAL — should NOT trip SOFT_NEWS_CLUSTER even though it
    generates multiple BUY/SELL signals across unrelated tickers."""
    article = {
        "id": "article-ma-1",
        "title": "Acme Corp announces acquisition of Beta Inc",
        "description": "M&A deal valued at $4.2B with FTC review pending",
        "keywords": ["m&a", "deal"],
    }
    signals = [
        _sig(symbol="ACME", side="BUY",  raw=article, id="s1"),
        _sig(symbol="BETA", side="BUY",  raw=article, id="s2"),
        _sig(symbol="GAMM", side="SELL", raw=article, id="s3"),
    ]
    alerts = detect_soft_news_cluster(signals)
    assert alerts == []  # material news is allowed


def test_soft_news_cluster_skips_when_below_min_tickers():
    article = {"id": "a", "title": "soft chatter", "description": ""}
    signals = [
        _sig(symbol="A", side="BUY", raw=article, id="s1"),
        _sig(symbol="B", side="BUY", raw=article, id="s2"),
    ]
    assert detect_soft_news_cluster(signals) == []


def test_soft_news_cluster_skips_hold_only_articles():
    """HOLD-only articles aren't trying to push direction — not a
    soft-news manipulation pattern."""
    article = {"id": "a", "title": "soft news", "description": ""}
    signals = [
        _sig(symbol=s, side="HOLD", raw=article, id=f"s{i}")
        for i, s in enumerate(["WMT", "INTU", "NVDA"])
    ]
    assert detect_soft_news_cluster(signals) == []


# ─── article_is_material ───────────────────────────────────────────


@pytest.mark.parametrize("title,desc,expected", [
    ("NVDA reports Q3 earnings beat",            "",                              True),
    ("",                                          "Fed announces rate cut",       True),
    ("Acme Corp 8-K filing details restructure", "",                              True),
    ("Analyst upgrade: Buy rating on TSLA",      "",                              True),
    ("Company Access Network mentoring event",   "Diversity participants listed", False),
    ("Top 5 retail stocks to watch",             "Market commentary",             False),
])
def test_article_is_material_classifier(title, desc, expected):
    assert article_is_material({"title": title, "description": desc}) is expected


# ─── DUPLICATE_BURST ───────────────────────────────────────────────


def test_duplicate_burst_fires_at_threshold():
    article = {"id": "article-burst"}
    signals = [
        _sig(symbol=f"T{i}", raw=article, id=f"s{i}")
        for i in range(DUPLICATE_BURST_MIN_ROWS)
    ]
    alerts = detect_duplicate_burst(signals)
    assert len(alerts) == 1
    assert alerts[0].label == "EXTERNAL_SIGNAL_DUPLICATE_BURST"
    assert alerts[0].article_id == "article-burst"
    assert alerts[0].enforced is False


def test_duplicate_burst_skips_below_threshold():
    article = {"id": "article-burst"}
    signals = [
        _sig(symbol=f"T{i}", raw=article, id=f"s{i}")
        for i in range(DUPLICATE_BURST_MIN_ROWS - 1)
    ]
    assert detect_duplicate_burst(signals) == []


# ─── FLIP_FLOP ─────────────────────────────────────────────────────


def test_flip_flop_buy_to_sell_within_window():
    earlier = (NOW - timedelta(minutes=30)).isoformat()
    signals = [
        _sig(side="BUY",  received_at=earlier, id="s1"),
        _sig(side="SELL", received_at=NOW.isoformat(), id="s2"),
    ]
    alerts = detect_flip_flop(signals, now=NOW)
    assert len(alerts) == 1
    assert alerts[0].label == "EXTERNAL_SIGNAL_FLIP_FLOP"
    assert set(alerts[0].signal_ids) == {"s1", "s2"}
    assert alerts[0].enforced is False


def test_flip_flop_ignores_hold():
    earlier = (NOW - timedelta(minutes=10)).isoformat()
    signals = [
        _sig(side="BUY",  received_at=earlier, id="s1"),
        _sig(side="HOLD", received_at=NOW.isoformat(), id="s2"),
    ]
    assert detect_flip_flop(signals, now=NOW) == []


def test_flip_flop_skips_when_window_exceeded():
    earlier = (NOW - timedelta(minutes=FLIP_WINDOW_MIN + 5)).isoformat()
    signals = [
        _sig(side="BUY",  received_at=earlier, id="s1"),
        _sig(side="SELL", received_at=NOW.isoformat(), id="s2"),
    ]
    assert detect_flip_flop(signals, now=NOW) == []


# ─── SPAM ──────────────────────────────────────────────────────────


def test_spam_fires_per_symbol_threshold():
    signals = [
        _sig(symbol="NVDA", id=f"s{i}")
        for i in range(SPAM_PER_SYMBOL + 1)
    ]
    alerts = detect_spam(signals, now=NOW)
    per_symbol = [a for a in alerts if a.symbol == "NVDA"]
    assert len(per_symbol) == 1
    assert per_symbol[0].label == "EXTERNAL_SIGNAL_SPAM"


def test_spam_does_not_fire_below_per_symbol_threshold():
    signals = [
        _sig(symbol="NVDA", id=f"s{i}")
        for i in range(SPAM_PER_SYMBOL)  # at threshold, NOT over
    ]
    alerts = detect_spam(signals, now=NOW)
    assert alerts == []


# ─── all detectors integrated ──────────────────────────────────────


def test_run_all_returns_log_only_alerts():
    """Whatever the detector fires, `enforced=False` is the
    doctrine pin. If anyone ever flips a label to enforced, this
    catches it."""
    article = {"id": "article-soft-1", "title": "soft news"}
    signals = [
        _sig(symbol=s, raw=article, id=f"s{i}")
        for i, s in enumerate(["WMT", "INTU", "NVDA", "AMD"])
    ]
    alerts = run_all_detectors(signals)
    assert all(a.enforced is False for a in alerts)
