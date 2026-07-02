"""Tests for the multi-ticker plural config helpers (2026-07-03).

Doctrine: depth over breadth. Each lane can now be configured with
N tickers (default N=1 via backward-compat fallback to the singular
env var). All four brains score the same instruments so their
dissent/accuracy tracks compare on equal data.

Backward-compat is the key promise: any deploy that only sets the
singular env vars (`TRADER_EQUITY_TICKER`, `TRADER_CRYPTO_PAIR`) must
continue behaving identically after this upgrade.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app")

from trader import config


# ─── crypto_pairs() ─────────────────────────────────────────────

def test_crypto_pairs_falls_back_to_singular_when_unset(monkeypatch):
    monkeypatch.delenv("TRADER_CRYPTO_PAIRS", raising=False)
    monkeypatch.setenv("TRADER_CRYPTO_PAIR", "XBTUSD")
    assert config.crypto_pairs() == ("XBTUSD",)


def test_crypto_pairs_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("TRADER_CRYPTO_PAIRS", "XBTUSD,SOLUSD")
    assert config.crypto_pairs() == ("XBTUSD", "SOLUSD")


def test_crypto_pairs_tolerates_whitespace(monkeypatch):
    monkeypatch.setenv("TRADER_CRYPTO_PAIRS", "  XBTUSD ,  SOLUSD  , ")
    assert config.crypto_pairs() == ("XBTUSD", "SOLUSD")


def test_crypto_pairs_uppercase_normalization(monkeypatch):
    monkeypatch.setenv("TRADER_CRYPTO_PAIRS", "xbtusd,solusd")
    assert config.crypto_pairs() == ("XBTUSD", "SOLUSD")


def test_crypto_pairs_empty_string_falls_back(monkeypatch):
    """Empty plural env must fall back to singular — same shape as
    an unset plural. Prevents an accidentally-blank env from disabling
    the whole lane."""
    monkeypatch.setenv("TRADER_CRYPTO_PAIRS", "")
    monkeypatch.setenv("TRADER_CRYPTO_PAIR", "XBTUSD")
    assert config.crypto_pairs() == ("XBTUSD",)


def test_crypto_pairs_all_commas_falls_back(monkeypatch):
    """`,,,` after strip filters to nothing — treat as unset."""
    monkeypatch.setenv("TRADER_CRYPTO_PAIRS", " , , , ")
    monkeypatch.setenv("TRADER_CRYPTO_PAIR", "XBTUSD")
    assert config.crypto_pairs() == ("XBTUSD",)


# ─── equity_tickers() ───────────────────────────────────────────

def test_equity_tickers_falls_back_to_singular_when_unset(monkeypatch):
    monkeypatch.delenv("TRADER_EQUITY_TICKERS", raising=False)
    monkeypatch.setenv("TRADER_EQUITY_TICKER", "TSLA")
    assert config.equity_tickers() == ("TSLA",)


def test_equity_tickers_stage1_picks(monkeypatch):
    """Live check against the operator's Stage-1 selection."""
    monkeypatch.setenv("TRADER_EQUITY_TICKERS", "NVDA,SPY")
    assert config.equity_tickers() == ("NVDA", "SPY")


def test_equity_tickers_tolerates_whitespace_and_case(monkeypatch):
    monkeypatch.setenv("TRADER_EQUITY_TICKERS", " nvda , SPY ")
    assert config.equity_tickers() == ("NVDA", "SPY")


def test_equity_tickers_empty_string_falls_back(monkeypatch):
    monkeypatch.setenv("TRADER_EQUITY_TICKERS", "")
    monkeypatch.setenv("TRADER_EQUITY_TICKER", "AAPL")
    assert config.equity_tickers() == ("AAPL",)


# ─── invariant: run_cycle iterates exactly what config exposes ─

def test_run_cycle_symbol_universe_matches_config_helpers(monkeypatch):
    """Regression guard: main.run_cycle picks its per-lane universe
    from `crypto_pairs()`/`equity_tickers()`. If a future refactor
    reintroduces the singular helpers, this test catches it."""
    from trader import main
    src = open(main.__file__).read()
    assert "config.crypto_pairs()" in src, (
        "run_cycle must call crypto_pairs() (plural)"
    )
    assert "config.equity_tickers()" in src, (
        "run_cycle must call equity_tickers() (plural)"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
