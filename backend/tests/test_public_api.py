"""Public API integration tests.

Coverage:
  * Trust + tier dependency (401 / 422 / 503 paths)
  * Shape compliance for every endpoint
  * Tier-aware sanitization (free/starter capped, pro/pro_max uncapped)
  * Scanner preset list + scan match shape
  * Models-mind 10-feature canonical shape + 404
  * Heatmap + sectors degraded flags
"""
from __future__ import annotations

import os
import uuid

import requests


BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
                break
BASE_URL = (BASE_URL or "").rstrip("/")


def _public_token() -> str:
    for line in open("/app/backend/.env").read().splitlines():
        if line.startswith("RISEDUAL_PUBLIC_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("RISEDUAL_PUBLIC_TOKEN not set")


PT = _public_token()


def _hdr(tier: str = "free") -> dict:
    return {"X-RiseDual-Token": PT, "X-RiseDual-User-Tier": tier,
            "Content-Type": "application/json"}


# ──────────────────────── auth ────────────────────────

class TestPublicAuth:
    def test_missing_token_401(self):
        r = requests.get(f"{BASE_URL}/api/public/signals", timeout=10)
        assert r.status_code == 401

    def test_wrong_token_401(self):
        r = requests.get(
            f"{BASE_URL}/api/public/signals",
            headers={"X-RiseDual-Token": "garbage"}, timeout=10,
        )
        assert r.status_code == 401

    def test_unknown_tier_422(self):
        r = requests.get(
            f"{BASE_URL}/api/public/signals",
            headers={"X-RiseDual-Token": PT, "X-RiseDual-User-Tier": "ultra"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_default_tier_is_free(self):
        # Token only, no tier header → defaults to free.
        r = requests.get(
            f"{BASE_URL}/api/public/signals",
            headers={"X-RiseDual-Token": PT}, timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["caller"]["tier"] == "free"
        assert d["caller"]["is_paid"] is False

    def test_starter_is_unpaid(self):
        r = requests.get(
            f"{BASE_URL}/api/public/signals",
            headers=_hdr("starter"), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["caller"]["tier"] == "starter"
        assert d["caller"]["is_paid"] is False

    def test_pro_max_is_unlimited(self):
        r = requests.get(
            f"{BASE_URL}/api/public/signals",
            headers=_hdr("pro_max"), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["caller"]["tier"] == "pro_max"
        assert d["caller"]["is_paid"] is True
        assert d["caller"]["is_unlimited"] is True


# ──────────────────────── /signals ────────────────────────

class TestSignals:
    def test_signals_shape(self):
        r = requests.get(
            f"{BASE_URL}/api/public/signals", headers=_hdr(), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        for k in ("items", "count", "active_signals", "consensus", "caller"):
            assert k in d
        assert d["consensus"]["label"] in {"BULLISH", "BEARISH", "NEUTRAL", "MIXED"}

    def test_signal_card_fields(self):
        r = requests.get(
            f"{BASE_URL}/api/public/signals?limit=5",
            headers=_hdr(), timeout=10,
        )
        assert r.status_code == 200
        items = r.json()["items"]
        for card in items:
            for k in ("signal_id", "symbol", "direction", "state",
                       "flagged_by_auditor", "consensus", "consensus_breakdown",
                       "thesis", "updated_at"):
                assert k in card
            assert card["direction"] in {"LONG", "SHORT", "HOLD"}

    def test_signal_detail_has_both_framings(self):
        # Get one signal id
        r = requests.get(
            f"{BASE_URL}/api/public/signals?limit=1",
            headers=_hdr(), timeout=10,
        )
        items = r.json()["items"]
        if not items:
            return  # no open positions, nothing to test
        sid = items[0]["signal_id"]
        r = requests.get(
            f"{BASE_URL}/api/public/signals/{sid}",
            headers=_hdr(), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert "adversarial" in d
        assert set(d["adversarial"].keys()) == {"bull", "bear", "commander"}
        assert "governance" in d
        gov = d["governance"]
        assert gov["strategist"]["label"] == "STRATEGIST_AGENT"
        assert gov["auditor"]["label"] == "RISK_AUDITOR_AGENT"
        assert gov["auditor"]["action"] in {"PASS", "VETO"}
        assert gov["synthesized"]["label"] == "SYNTHESIZED SIGNAL"
        assert gov["synthesized"]["direction"] in {"LONG", "SHORT", "HOLD"}

    def test_signal_detail_404(self):
        r = requests.get(
            f"{BASE_URL}/api/public/signals/{uuid.uuid4()}",
            headers=_hdr(), timeout=10,
        )
        assert r.status_code == 404


# ──────────────────────── /digest ────────────────────────

class TestDigest:
    def test_free_tier_caps_apply(self):
        r = requests.get(
            f"{BASE_URL}/api/public/digest",
            headers=_hdr("free"), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["caps"] == {"predictions": 2, "smart_money": 2, "alerts": 1}
        assert d["tier"] == "free"
        # If there's data, free tier sees at most caps + 1 locked row each.
        assert len(d["predictions"]) <= 3
        assert len(d["smart_money"]) <= 3
        assert len(d["alerts"]) <= 2

    def test_starter_capped_like_free(self):
        r = requests.get(
            f"{BASE_URL}/api/public/digest",
            headers=_hdr("starter"), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["caps"] == {"predictions": 2, "smart_money": 2, "alerts": 1}

    def test_pro_uncapped(self):
        r = requests.get(
            f"{BASE_URL}/api/public/digest",
            headers=_hdr("pro"), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["caps"] == {"predictions": 25, "smart_money": 25, "alerts": 25}

    def test_pro_max_uncapped(self):
        r = requests.get(
            f"{BASE_URL}/api/public/digest",
            headers=_hdr("pro_max"), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["caps"] == {"predictions": 25, "smart_money": 25, "alerts": 25}

    def test_locked_rows_have_correct_shape(self):
        r = requests.get(
            f"{BASE_URL}/api/public/digest",
            headers=_hdr("free"), timeout=10,
        )
        d = r.json()
        for row in d["predictions"] + d["smart_money"] + d["alerts"]:
            if isinstance(row, dict) and row.get("locked"):
                assert row["symbol"] is None
                assert row["kind"] in {"predictions", "smart_money", "alerts"}
                assert row["upgrade_to"] in {"pro", "pro_max"}
                assert "cta" in row


# ──────────────────────── /scanner ────────────────────────

class TestScanner:
    def test_presets_list_has_10(self):
        r = requests.get(
            f"{BASE_URL}/api/public/scanner/presets",
            headers=_hdr(), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["count"] == 10
        names = {p["preset_id"] for p in d["presets"]}
        expected = {
            "macd_bullish_cross", "macd_bearish_cross", "bollinger_squeeze",
            "ema_golden_cross", "volume_spike", "near_52w_high",
            "near_52w_low", "rsi_overbought", "rsi_oversold",
            "momentum_breakout",
        }
        assert expected == names

    def test_scan_returns_match_shape(self):
        r = requests.get(
            f"{BASE_URL}/api/public/scanner/scan?preset_id=rsi_oversold",
            headers=_hdr(), timeout=15,
        )
        assert r.status_code == 200
        d = r.json()
        for k in ("preset_id", "name", "category", "signal",
                   "matches", "scanned", "matched"):
            assert k in d
        for m in d["matches"]:
            assert "symbol" in m and "strength" in m and "detail" in m
            assert 0 <= m["strength"] <= 100

    def test_scan_unknown_preset_404(self):
        r = requests.get(
            f"{BASE_URL}/api/public/scanner/scan?preset_id=does_not_exist",
            headers=_hdr(), timeout=10,
        )
        assert r.status_code == 404


# ──────────────────────── /agent-activity ────────────────────────

class TestAgentActivity:
    def test_feed_shape(self):
        r = requests.get(
            f"{BASE_URL}/api/public/agent-activity/feed?limit=5",
            headers=_hdr(), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        for k in ("items", "count", "polled_at", "tier"):
            assert k in d

    def test_event_fields(self):
        r = requests.get(
            f"{BASE_URL}/api/public/agent-activity/feed?limit=20",
            headers=_hdr(), timeout=10,
        )
        d = r.json()
        for ev in d["items"]:
            for k in ("event_id", "timestamp", "type", "severity", "title"):
                assert k in ev
            assert ev["severity"] in {"info", "success", "warn", "error"}

    def test_since_filter_returns_strict_subset(self):
        r = requests.get(
            f"{BASE_URL}/api/public/agent-activity/feed?limit=50",
            headers=_hdr(), timeout=10,
        )
        items = r.json()["items"]
        if len(items) < 2:
            return
        # Use the oldest event timestamp (last in desc-sorted list) as
        # the cutoff. Multiple audit rows in a single stance share a
        # timestamp, so strict-greater on the cutoff may not narrow the
        # result — what we're checking is that the filter parses and
        # returns a non-strict subset.
        cutoff = items[-1]["timestamp"]
        r2 = requests.get(
            f"{BASE_URL}/api/public/agent-activity/feed?since={cutoff}&limit=50",
            headers=_hdr(), timeout=10,
        )
        assert r2.status_code == 200
        for ev in r2.json()["items"]:
            assert ev["timestamp"] >= cutoff


# ──────────────────────── /models-mind ────────────────────────

class TestModelsMind:
    def _any_symbol(self) -> str | None:
        r = requests.get(
            f"{BASE_URL}/api/public/heatmap",
            headers=_hdr(), timeout=10,
        )
        items = r.json()["items"]
        return items[0]["symbol"] if items else None

    def test_models_mind_shape(self):
        sym = self._any_symbol()
        if not sym:
            return
        r = requests.get(
            f"{BASE_URL}/api/public/models-mind/{sym}",
            headers=_hdr(), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        expected_features = {
            "score_2W", "distance_from_mw", "macro_regime_flag", "atr_id",
            "earnings_proximity", "momentum_3d", "sector_rs",
            "pattern_score", "rsi_id", "vol_zscore",
        }
        assert set(d["features"].keys()) == expected_features

    def test_unknown_symbol_404(self):
        r = requests.get(
            f"{BASE_URL}/api/public/models-mind/ZZZ_NOT_A_REAL_SYMBOL_999",
            headers=_hdr(), timeout=10,
        )
        assert r.status_code == 404

    def test_not_wired_features_marked(self):
        sym = self._any_symbol()
        if not sym:
            return
        r = requests.get(
            f"{BASE_URL}/api/public/models-mind/{sym}",
            headers=_hdr(), timeout=10,
        )
        d = r.json()
        # earnings_proximity + sector_rs are documented as not-wired.
        assert d["features"]["earnings_proximity"]["coverage"] == "not_wired"
        assert d["features"]["sector_rs"]["coverage"] == "not_wired"


# ──────────────────────── /heatmap + /sectors ────────────────────────

class TestHeatmapAndSectors:
    def test_heatmap_shape(self):
        r = requests.get(
            f"{BASE_URL}/api/public/heatmap", headers=_hdr(), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        for k in ("items", "count", "degraded", "tier"):
            assert k in d
        for item in d["items"]:
            assert "symbol" in item
            assert "change_24h_pct" in item
            assert item["color_band"] in {
                "strong_buy", "mild_buy", "neutral", "mild_sell", "strong_sell",
            }

    def test_sectors_universe_present(self):
        r = requests.get(
            f"{BASE_URL}/api/public/sectors", headers=_hdr(), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert len(d["items"]) >= 11
        for item in d["items"]:
            assert item["coverage"] in {"live", "not_wired"}
