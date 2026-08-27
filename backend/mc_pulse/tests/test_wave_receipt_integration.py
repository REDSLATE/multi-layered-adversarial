from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mc_pulse.pulse import pulse_tick
from mc_pulse.snapshot import build_snapshot


@pytest.mark.asyncio
async def test_pulse_receipt_records_wave_summary_without_creating_intents():
    snapshot = build_snapshot(
        symbol="NVDA",
        lane="equity",
        timestamp=datetime.now(timezone.utc),
        price=Decimal("100.00"),
        indicators={},
        wave_intelligence={
            "model_version": "wave-intelligence-v1",
            "authority": "OBSERVE_ONLY",
            "symbol": "NVDA",
            "lane": "equity",
            "mode": "TREND_FOLLOW",
            "data_quality": "READY",
            "scores": {"trend": 0.8, "range": 0.1, "danger": 0.1},
        },
    )
    registry = MagicMock(__len__=lambda _: 0, for_lane=lambda _: [])

    with patch("mc_pulse.pulse.get_registry", return_value=registry), patch(
        "mc_pulse.pulse.persist_receipt", new=AsyncMock()
    ):
        receipt = await pulse_tick(
            [snapshot],
            runtime_mode="LIVE",
            compare_only=False,
            auto_arbitrate=True,
        )

    assert receipt.wave_machine_summary["observations"] == 1
    assert receipt.wave_machine_summary["mode_counts"]["TREND_FOLLOW"] == 1
    assert receipt.wave_machine_summary["authority"] == "OBSERVE_ONLY"
    assert receipt.intents_emitted == 0
    assert receipt.arbitrations_completed == 0
