"""Sell-Point Watcher tests (2026-08-04, v3.5 plan item 5)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.exits.pattern_watch import (  # noqa: E402
    atr, detect_double_top, detect_head_shoulders, detect_rising_wedge,
    pivot_indices, tighten_stop,
)

pytestmark = pytest.mark.tripwire


def _bars(path, vols=None):
    """path = list of (h, l, c); o = prior close."""
    out, prev_c = [], path[0][2]
    for i, (h, l, c) in enumerate(path):
        out.append({"ts": f"2026-08-04T10:{i:02d}:00+00:00", "o": prev_c,
                    "h": h, "l": l, "c": c,
                    "v": (vols[i] if vols else 10.0)})
        prev_c = c
    return out


def _flat(n, px=100.0):
    return [(px + 0.3, px - 0.3, px)] * n


def test_pivots_and_atr():
    highs = [1, 2, 3, 9, 3, 2, 1, 2, 3, 8, 3, 2, 1]
    assert pivot_indices(highs, high=True) == [3, 9]
    lows = [5, 4, 1, 4, 5, 5, 5]
    assert pivot_indices(lows, high=False) == [2]
    bars = _bars(_flat(20))
    assert atr(bars) == pytest.approx(0.6)


def test_double_top_detected_and_flat_tape_clean():
    # two ~equal peaks at 110, valley 104 (neckline), confirm close 103
    path = _flat(16) + [
        (106, 103, 105), (110, 105, 109), (107, 104, 105),  # peak 1
        (105.5, 104, 104.5), (105, 104.0, 104.6),           # valley 104
        (106, 104.5, 105.5), (110.1, 105, 109.5),           # peak 2
        (108, 105, 106), (105.5, 103.9, 104.2),
        (104.5, 102.8, 103.0),                              # break neckline
    ]
    hit = detect_double_top(_bars(path))
    assert hit and hit["pattern"] == "double_top"
    assert hit["neckline"] == 104.0
    assert abs(hit["peaks"][0]["price"] - hit["peaks"][1]["price"]) <= 0.5 * hit["atr"]
    assert detect_double_top(_bars(_flat(30))) is None


def test_head_shoulders_detected():
    # shoulders ~108, head 112, neckline 104, confirm 103
    path = _flat(18) + [
        (106, 104, 105), (108, 105, 107), (106, 104.2, 105),   # L shoulder
        (105, 104.0, 104.5),                                   # low 1
        (109, 104.5, 108), (112, 108, 111), (109, 105, 106),   # head
        (105, 104.1, 104.5),                                   # low 2
        (107, 104.5, 106), (108.2, 105, 107.5), (106, 104.3, 105),  # R shoulder
        (104.8, 103.5, 104.1), (104.0, 102.7, 103.0),          # break
    ]
    hit = detect_head_shoulders(_bars(path))
    assert hit and hit["pattern"] == "head_shoulders"
    assert hit["peaks"][1]["price"] == 112  # head is the middle pivot
    assert hit["close"] < hit["neckline"]


def test_rising_wedge_needs_fade_and_break():
    # rising, converging (lows rise faster than highs), fading volume,
    # last close breaks the lower trendline
    path = []
    for i in range(29):
        low = 100.0 + i * 0.30
        high = 106.0 + i * 0.08
        # zigzag so fractal pivots exist on both series
        if i % 4 == 2:
            path.append((high + 0.6, low + 0.2, low + 0.8))
        elif i % 4 == 0:
            path.append((high - 0.4, low - 0.5, low + 0.3))
        else:
            path.append((high - 0.1, low + 0.1, (high + low) / 2))
    path.append((106.5, 104.0, 104.3))  # break below trendline
    vols = [30.0 - i * 0.8 for i in range(30)]  # fading
    hit = detect_rising_wedge(_bars(path, vols))
    assert hit and hit["pattern"] == "rising_wedge"
    assert hit["slope_lows"] > hit["slope_highs"] > 0
    assert hit["volume_fade"] < 0.8
    # same tape with FLAT volume → no wedge
    assert detect_rising_wedge(_bars(path, [30.0] * 30)) is None


def test_tighten_never_lowers_never_at_price():
    hit = {"close": 100.0, "atr": 2.0}
    # raise from 95 → 99 (close − 0.5×ATR)
    assert tighten_stop({"stop_price": 95.0}, hit, 0.5) == 99.0
    # existing stop already tighter → no change
    assert tighten_stop({"stop_price": 99.5}, hit, 0.5) is None
    # zero buffer would put stop AT price → refused
    assert tighten_stop({"stop_price": 95.0}, hit, 0.0) is None


def test_wiring():
    reg = open("/app/backend/server_modules/router_registry.py").read()
    assert "routes.sell_point_admin:router" in reg
    life = open("/app/backend/server_modules/lifespan.py").read()
    assert "sell_point_task" in life
    from shared.exits.pattern_watch import DEFAULTS
    assert DEFAULTS["mode"] == "observe"  # observe-first doctrine lock
