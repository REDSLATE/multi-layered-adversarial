"""Universe quality knobs (2026-07-22) — screener_admit_cap +
min_price_equity override + pinned exemption."""
from __future__ import annotations

import sys

sys.path.insert(0, "/app/backend")

from shared.universe.refresher import (
    _apply_hysteresis,
    _apply_quality_filters,
    HYSTERESIS_ADMIT,
)


def _row(sym, *, pinned=False, price=10.0, volume=1_000_000.0, chg=1.0):
    # volume default clears the 2026-07-28 $3M dollar-volume floor
    # at the fixture prices — these tests pin the PRICE floor knob.
    return {
        "canonical_symbol": sym,
        "pinned": pinned,
        "price": price,
        "volume": volume,
        "change_pct": chg,
        "source_reasons": ["top_gainer"],
    }


# ── screener_admit_cap ──────────────────────────────────────────────

def test_admit_cap_zero_is_pins_only():
    ranked = [
        _row("AAPL", pinned=True),
        _row("PUMP1"),
        _row("PUMP2"),
    ]
    out = _apply_hysteresis(ranked, set(), admit_cap=0)
    assert [r["canonical_symbol"] for r in out] == ["AAPL"]


def test_admit_cap_limits_screener_rows_but_never_pins():
    ranked = [
        _row("AAPL", pinned=True),
        _row("NVDA", pinned=True),
        _row("S1"), _row("S2"), _row("S3"),
    ]
    out = _apply_hysteresis(ranked, set(), admit_cap=2)
    syms = [r["canonical_symbol"] for r in out]
    assert syms == ["AAPL", "NVDA", "S1", "S2"]


def test_admit_cap_retain_window_scales():
    # cap=1 → retain window = 16; S2 is rank 1 (past admit) but a
    # previous member inside retain → kept with hysteresis reason.
    ranked = [_row("S1"), _row("S2")]
    out = _apply_hysteresis(ranked, {"S2"}, admit_cap=1)
    syms = [r["canonical_symbol"] for r in out]
    assert syms == ["S1", "S2"]
    assert out[1]["_admit_reason"] == "hysteresis_retain"


def test_admit_cap_none_preserves_default_behavior():
    ranked = [_row(f"S{i}") for i in range(HYSTERESIS_ADMIT + 5)]
    out = _apply_hysteresis(ranked, set(), admit_cap=None)
    assert len(out) == HYSTERESIS_ADMIT


# ── min_price_equity override ───────────────────────────────────────

def test_min_price_override_drops_penny_stocks():
    rows = [_row("PENNY", price=2.5), _row("REAL", price=50.0)]
    kept, dropped = _apply_quality_filters(
        rows, "equity", min_price_override=5.0,
    )
    assert [r["canonical_symbol"] for r in kept] == ["REAL"]
    assert dropped[0]["canonical_symbol"] == "PENNY"


def test_min_price_override_exempts_pins():
    rows = [_row("CHEAP", pinned=True, price=2.5)]
    kept, dropped = _apply_quality_filters(
        rows, "equity", min_price_override=5.0,
    )
    assert kept and not dropped


def test_min_price_override_none_uses_default_dollar_floor():
    # default equity floor is $5 since 2026-07-28 ("garbage tickers")
    rows = [_row("SUB", price=3.0, volume=8_000_000.0),
            _row("OK", price=6.0, volume=8_000_000.0)]
    kept, _ = _apply_quality_filters(rows, "equity", min_price_override=None)
    assert [r["canonical_symbol"] for r in kept] == ["OK"]


def test_min_price_override_ignored_for_crypto():
    rows = [_row("SHIB/USD", price=0.00001)]
    kept, _ = _apply_quality_filters(rows, "crypto", min_price_override=5.0)
    assert kept
