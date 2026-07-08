"""Unit tests for `verifier/witness_resolver_runner.py`.

Focus:
    * `start_worker` is idempotent
    * `start_worker` is a no-op when disabled by env
    * `_resolve_once` handles resolver success + failure + persists state
    * env-config parsing (sources csv, tick_sec, horizon_hours)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from verifier import witness_resolver_runner as runner


class TestEnvParsing:
    def test_env_bool_true(self, monkeypatch):
        monkeypatch.setenv("WITNESS_RESOLVER_ENABLED", "true")
        assert runner._env_bool("WITNESS_RESOLVER_ENABLED", False) is True

    def test_env_bool_false(self, monkeypatch):
        monkeypatch.setenv("WITNESS_RESOLVER_ENABLED", "false")
        assert runner._env_bool("WITNESS_RESOLVER_ENABLED", True) is False

    def test_env_bool_default_when_missing(self, monkeypatch):
        monkeypatch.delenv("WITNESS_RESOLVER_ENABLED", raising=False)
        assert runner._env_bool("WITNESS_RESOLVER_ENABLED", True) is True

    def test_env_int_valid(self, monkeypatch):
        monkeypatch.setenv("WITNESS_RESOLVER_TICK_SEC", "300")
        assert runner._env_int("WITNESS_RESOLVER_TICK_SEC", 900) == 300

    def test_env_int_falls_back_on_junk(self, monkeypatch):
        monkeypatch.setenv("WITNESS_RESOLVER_TICK_SEC", "not-a-number")
        assert runner._env_int("WITNESS_RESOLVER_TICK_SEC", 900) == 900

    def test_env_csv_parses_and_trims(self, monkeypatch):
        monkeypatch.setenv("WITNESS_RESOLVER_SOURCES", "polygon, pine ,,mtr")
        assert runner._env_csv("WITNESS_RESOLVER_SOURCES", "") == [
            "polygon", "pine", "mtr",
        ]

    def test_env_csv_default_when_missing(self, monkeypatch):
        monkeypatch.delenv("WITNESS_RESOLVER_SOURCES", raising=False)
        assert runner._env_csv("WITNESS_RESOLVER_SOURCES", "polygon") == ["polygon"]


class TestStartWorker:
    def setup_method(self):
        # Ensure clean global state between tests.
        runner._worker_task = None

    def teardown_method(self):
        # Cancel any accidentally-started task so pytest doesn't leak
        # background tasks between tests.
        if runner._worker_task is not None and not runner._worker_task.done():
            runner._worker_task.cancel()
        runner._worker_task = None

    def test_disabled_via_env_is_noop(self, monkeypatch):
        monkeypatch.setenv("WITNESS_RESOLVER_ENABLED", "false")
        runner.start_worker()
        assert runner._worker_task is None

    def test_second_start_is_noop(self, monkeypatch):
        # Under an event loop so create_task works.
        async def _run():
            monkeypatch.setenv("WITNESS_RESOLVER_ENABLED", "true")
            # First call: schedules a task.
            with patch.object(runner, "_loop", new=AsyncMock()):
                runner.start_worker()
                first = runner._worker_task
                assert first is not None
                # Second call while task is still pending: must NOT
                # replace it. Idempotent.
                runner.start_worker()
                assert runner._worker_task is first
                # Clean up.
                first.cancel()

        asyncio.run(_run())


class TestResolveOnce:
    @pytest.mark.asyncio
    async def test_success_persists_summary(self):
        mock_summary = MagicMock(
            source="polygon",
            rows_examined=100,
            rows_resolved=87,
            rows_undetermined=3,
            rows_skipped_price_missing=10,
            rows_skipped_too_recent=0,
            aggregate_before={"samples": 0},
            aggregate_after={"samples": 87, "wins": 45},
            status_before="UNTRUSTED",
            status_after="WATCHLIST",
            status_changed=True,
        )

        with patch("verifier.witness_resolver.resolve_source",
                   new=AsyncMock(return_value=mock_summary)), \
             patch("verifier.price_fetcher.price_from_ohlcv_bars",
                   new=AsyncMock()), \
             patch.object(runner, "_record_tick_result",
                          new=AsyncMock()) as mock_record:
            await runner._resolve_once("polygon", horizon_hours=24, limit=100)

        # Recorded a success (error=None) with the summary fields.
        assert mock_record.await_count == 1
        args, kwargs = mock_record.await_args
        assert args[0] == "polygon"
        summary_arg = args[1]
        assert summary_arg["status_after"] == "WATCHLIST"
        assert summary_arg["status_changed"] is True
        assert kwargs.get("error") is None

    @pytest.mark.asyncio
    async def test_resolver_error_recorded_but_not_raised(self):
        with patch("verifier.witness_resolver.resolve_source",
                   new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("verifier.price_fetcher.price_from_ohlcv_bars",
                   new=AsyncMock()), \
             patch.object(runner, "_record_tick_result",
                          new=AsyncMock()) as mock_record:
            # MUST NOT raise — the runner must survive bad ticks.
            await runner._resolve_once("polygon", horizon_hours=24, limit=100)

        assert mock_record.await_count == 1
        args, kwargs = mock_record.await_args
        assert kwargs.get("error") is not None
        assert "boom" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        # `asyncio.CancelledError` must NOT be swallowed — that would
        # break graceful shutdown by keeping the runner alive after
        # the lifespan calls stop_worker().
        with patch("verifier.witness_resolver.resolve_source",
                   new=AsyncMock(side_effect=asyncio.CancelledError())), \
             patch("verifier.price_fetcher.price_from_ohlcv_bars",
                   new=AsyncMock()), \
             patch.object(runner, "_record_tick_result",
                          new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await runner._resolve_once("polygon", horizon_hours=24, limit=100)
