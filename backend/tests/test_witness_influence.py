"""Unit tests for `shared/witness_influence.py`.

Covers:
    * `modifier_for_status` — pure tier→float table (no I/O)
    * `witness_modifier_for` — DB-backed read with default-hostile fallback
    * `witness_influence_snapshot` — bulk read for the admin panel
    * env-override for the three tier ceilings
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared import witness_influence


# ─────────────────────────── pure table ───────────────────────────


class TestModifierForStatus:
    def test_untrusted_is_zero(self):
        assert witness_influence.modifier_for_status("UNTRUSTED") == 0.0

    def test_watchlist_default_ceiling(self):
        assert witness_influence.modifier_for_status("WATCHLIST") == 0.05

    def test_trusted_default_ceiling(self):
        assert witness_influence.modifier_for_status("TRUSTED") == 0.15

    def test_case_insensitive(self):
        assert witness_influence.modifier_for_status("trusted") == 0.15
        assert witness_influence.modifier_for_status("Watchlist") == 0.05

    def test_none_is_zero(self):
        assert witness_influence.modifier_for_status(None) == 0.0

    def test_empty_is_zero(self):
        assert witness_influence.modifier_for_status("") == 0.0

    def test_unknown_tier_is_zero(self):
        # A future tier the ledger might invent must not silently grant
        # influence. Default-hostile.
        assert witness_influence.modifier_for_status("PROVISIONAL") == 0.0
        assert witness_influence.modifier_for_status("ADVISORY") == 0.0


class TestEnvOverride:
    def test_env_can_raise_trusted_ceiling(self, monkeypatch):
        monkeypatch.setenv("WITNESS_MODIFIER_TRUSTED", "0.30")
        assert witness_influence.modifier_for_status("TRUSTED") == 0.30

    def test_env_can_lower_watchlist_ceiling(self, monkeypatch):
        monkeypatch.setenv("WITNESS_MODIFIER_WATCHLIST", "0.01")
        assert witness_influence.modifier_for_status("WATCHLIST") == 0.01

    def test_env_clamped_to_one(self, monkeypatch):
        # Doctrine: witness ceilings never exceed 100%.
        monkeypatch.setenv("WITNESS_MODIFIER_TRUSTED", "5.0")
        assert witness_influence.modifier_for_status("TRUSTED") == 1.0

    def test_env_clamped_to_zero(self, monkeypatch):
        # Doctrine: negative env values are refused, floor at 0.
        monkeypatch.setenv("WITNESS_MODIFIER_TRUSTED", "-0.5")
        assert witness_influence.modifier_for_status("TRUSTED") == 0.0

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("WITNESS_MODIFIER_TRUSTED", "not-a-float")
        assert witness_influence.modifier_for_status("TRUSTED") == 0.15


# ─────────────────────── DB-backed reader ───────────────────────


class TestWitnessModifierFor:
    @pytest.mark.asyncio
    async def test_missing_ledger_row_is_zero(self):
        with patch.object(witness_influence, "db", new={
            "external_source_credibility": type(
                "C", (), {"find_one": AsyncMock(return_value=None)},
            )(),
        }):
            assert await witness_influence.witness_modifier_for("polygon") == 0.0

    @pytest.mark.asyncio
    async def test_trusted_returns_ceiling(self):
        with patch.object(witness_influence, "db", new={
            "external_source_credibility": type(
                "C", (), {"find_one": AsyncMock(return_value={"status": "TRUSTED"})},
            )(),
        }):
            assert await witness_influence.witness_modifier_for("polygon") == 0.15

    @pytest.mark.asyncio
    async def test_watchlist_returns_ceiling(self):
        with patch.object(witness_influence, "db", new={
            "external_source_credibility": type(
                "C", (), {"find_one": AsyncMock(return_value={"status": "WATCHLIST"})},
            )(),
        }):
            assert await witness_influence.witness_modifier_for("polygon") == 0.05

    @pytest.mark.asyncio
    async def test_untrusted_returns_zero(self):
        with patch.object(witness_influence, "db", new={
            "external_source_credibility": type(
                "C", (), {"find_one": AsyncMock(return_value={"status": "UNTRUSTED"})},
            )(),
        }):
            assert await witness_influence.witness_modifier_for("polygon") == 0.0

    @pytest.mark.asyncio
    async def test_read_error_returns_zero(self):
        # Default-hostile: if Mongo hiccups, the Governor gets 0.0 —
        # a witness cannot silently influence sizing on a bad read.
        async def _raiser(*a, **kw):
            raise RuntimeError("mongo timeout")

        with patch.object(witness_influence, "db", new={
            "external_source_credibility": type(
                "C", (), {"find_one": _raiser},
            )(),
        }):
            assert await witness_influence.witness_modifier_for("polygon") == 0.0


# ─────────────────────── snapshot builder ───────────────────────


class TestWitnessInfluenceSnapshot:
    @pytest.mark.asyncio
    async def test_missing_source_produces_hostile_default(self):
        with patch.object(witness_influence, "db", new={
            "external_source_credibility": type(
                "C", (), {"find_one": AsyncMock(return_value=None)},
            )(),
        }):
            snap = await witness_influence.witness_influence_snapshot(["polygon"])
        assert snap["polygon"]["status"] == "UNTRUSTED"
        assert snap["polygon"]["modifier_cap"] == 0.0
        assert snap["polygon"]["samples"] == 0
        assert snap["polygon"]["ledger_present"] is False

    @pytest.mark.asyncio
    async def test_present_source_reports_ledger_and_ceiling(self):
        ledger_doc = {
            "status": "TRUSTED",
            "samples": 512,
            "wins": 300,
            "losses": 212,
            "verified_alpha": 0.023,
            "orthogonal_win_rate": 0.586,
        }
        with patch.object(witness_influence, "db", new={
            "external_source_credibility": type(
                "C", (), {"find_one": AsyncMock(return_value=ledger_doc)},
            )(),
        }):
            snap = await witness_influence.witness_influence_snapshot(["polygon"])
        assert snap["polygon"]["status"] == "TRUSTED"
        assert snap["polygon"]["modifier_cap"] == 0.15
        assert snap["polygon"]["samples"] == 512
        assert snap["polygon"]["wins"] == 300
        assert snap["polygon"]["ledger_present"] is True

    @pytest.mark.asyncio
    async def test_multiple_sources(self):
        async def _find_one(query, projection=None):
            if query["source"] == "polygon":
                return {"status": "TRUSTED", "samples": 500}
            if query["source"] == "pine":
                return {"status": "WATCHLIST", "samples": 60}
            return None

        mock_coll = MagicMock()
        mock_coll.find_one = _find_one

        with patch.object(witness_influence, "db", new={
            "external_source_credibility": mock_coll,
        }):
            snap = await witness_influence.witness_influence_snapshot(
                ["polygon", "pine", "mtr"],
            )
        assert snap["polygon"]["modifier_cap"] == 0.15
        assert snap["pine"]["modifier_cap"] == 0.05
        # `mtr` has no ledger row — must still show as default-hostile.
        assert snap["mtr"]["modifier_cap"] == 0.0
        assert snap["mtr"]["ledger_present"] is False
