"""Regression tests for the Advisor Performance table (2026-06-24,
operator pin pass 3).

Doctrine:
  * Aggregates per advisor from intent_consensus_telemetry
  * Joins with shared_brain_outcomes for win/loss
  * win_rate columns return None when there are no resolved outcomes
    (operator should see '—', not '0%' which is misleading)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from db import db
from namespaces import INTENT_CONSENSUS_TELEMETRY
from shared.advisor_performance import advisor_performance, _pct


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def clean_tables():
    await db[INTENT_CONSENSUS_TELEMETRY].drop()
    await db["shared_brain_outcomes"].delete_many({"opinion_id": {"$regex": "^test-"}})
    yield db
    await db[INTENT_CONSENSUS_TELEMETRY].drop()
    await db["shared_brain_outcomes"].delete_many({"opinion_id": {"$regex": "^test-"}})


async def _telemetry(intent_id: str, agree, disagree, minutes_ago: int = 0):
    await db[INTENT_CONSENSUS_TELEMETRY].insert_one({
        "intent_id": intent_id,
        "ts": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        "applied": bool(agree or disagree),
        "agree_brains": agree, "disagree_brains": disagree,
        "agree_count": len(agree), "disagree_count": len(disagree),
        "advisor_boost": 0.05 * (len(agree) - len(disagree)),
        "advisor_votes_used": len(agree) + len(disagree),
        "advisor_window_seconds": 900, "advisor_count": len(agree) + len(disagree),
        "base_confidence": 0.7, "effective_confidence": 0.75,
    })


async def _outcome(intent_id: str, actual: str):
    await db["shared_brain_outcomes"].insert_one({
        "opinion_id": intent_id,
        "actual": actual,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    })


class TestPctHelper:
    def test_zero_denominator_returns_none(self):
        assert _pct(0, 0) is None
        assert _pct(5, 0) is None

    def test_basic_math(self):
        assert _pct(1, 4) == 0.25
        assert _pct(0, 4) == 0.0
        assert _pct(4, 4) == 1.0


class TestAdvisorPerformance:
    async def test_empty_window(self, clean_tables):
        out = await advisor_performance(db, 24)
        assert out["n_executor_evaluations"] == 0
        assert out["advisors"] == []

    async def test_single_advisor_agree_with_win(self, clean_tables):
        await _telemetry("test-1", agree=["camino"], disagree=[])
        await _outcome("test-1", "win")
        out = await advisor_performance(db, 24)
        camino = next(a for a in out["advisors"] if a["brain_id"] == "camino")
        assert camino["agree_count"] == 1
        assert camino["disagree_count"] == 0
        assert camino["agree_pct"] == 1.0
        assert camino["agree_win_rate"] == 1.0
        assert camino["agree_resolved"] == 1
        assert camino["disagree_win_rate"] is None

    async def test_disagree_was_right_signal(self, clean_tables):
        # Camino disagreed twice, both times the executor LOST.
        # So disagree_was_right_pct == 1.0 (camino was right to disagree).
        await _telemetry("test-1", agree=[], disagree=["camino"])
        await _outcome("test-1", "loss")
        await _telemetry("test-2", agree=[], disagree=["camino"])
        await _outcome("test-2", "loss")
        out = await advisor_performance(db, 24)
        camino = next(a for a in out["advisors"] if a["brain_id"] == "camino")
        assert camino["disagree_count"] == 2
        assert camino["disagree_resolved"] == 2
        assert camino["disagree_wins"] == 0
        assert camino["disagree_win_rate"] == 0.0
        assert camino["disagree_was_right_pct"] == 1.0

    async def test_mixed_advisors_full_table(self, clean_tables):
        # Camino agreed 3 times (2 wins, 1 loss). Hellcat disagreed
        # 2 times (1 win, 1 loss).
        await _telemetry("test-1", agree=["camino"], disagree=["hellcat"])
        await _outcome("test-1", "win")
        await _telemetry("test-2", agree=["camino"], disagree=["hellcat"])
        await _outcome("test-2", "loss")
        await _telemetry("test-3", agree=["camino"], disagree=[])
        await _outcome("test-3", "win")
        out = await advisor_performance(db, 24)
        camino = next(a for a in out["advisors"] if a["brain_id"] == "camino")
        hellcat = next(a for a in out["advisors"] if a["brain_id"] == "hellcat")
        assert camino["agree_count"] == 3
        assert camino["agree_wins"] == 2
        assert camino["agree_win_rate"] == round(2/3, 4)
        assert hellcat["disagree_count"] == 2
        assert hellcat["disagree_wins"] == 1
        assert hellcat["disagree_win_rate"] == 0.5

    async def test_unresolved_outcomes_excluded_from_win_rate(self, clean_tables):
        # 3 agree appearances, only 1 resolved outcome.
        await _telemetry("test-1", agree=["camino"], disagree=[])
        await _telemetry("test-2", agree=["camino"], disagree=[])
        await _telemetry("test-3", agree=["camino"], disagree=[])
        await _outcome("test-1", "win")
        # test-2 and test-3 have no outcome row yet.
        out = await advisor_performance(db, 24)
        camino = next(a for a in out["advisors"] if a["brain_id"] == "camino")
        assert camino["agree_count"] == 3
        assert camino["agree_resolved"] == 1
        assert camino["agree_wins"] == 1
        assert camino["agree_win_rate"] == 1.0

    async def test_window_filters_old_rows(self, clean_tables):
        await _telemetry("test-recent", agree=["gto"], disagree=[], minutes_ago=10)
        await _telemetry("test-old", agree=["gto"], disagree=[], minutes_ago=60 * 30)
        await _outcome("test-recent", "win")
        await _outcome("test-old", "win")
        out_1h = await advisor_performance(db, 1)
        gto_1h = next(a for a in out_1h["advisors"] if a["brain_id"] == "gto")
        assert gto_1h["agree_count"] == 1  # only the recent one
        out_72h = await advisor_performance(db, 72)
        gto_72h = next(a for a in out_72h["advisors"] if a["brain_id"] == "gto")
        assert gto_72h["agree_count"] == 2

    async def test_advisors_sorted_by_appearances_desc(self, clean_tables):
        # camino: 3 appearances, hellcat: 1, gto: 5
        for i in range(3):
            await _telemetry(f"test-c{i}", agree=["camino"], disagree=[])
        await _telemetry("test-h1", agree=["hellcat"], disagree=[])
        for i in range(5):
            await _telemetry(f"test-g{i}", agree=["gto"], disagree=[])
        out = await advisor_performance(db, 24)
        order = [a["brain_id"] for a in out["advisors"]]
        assert order == ["gto", "camino", "hellcat"]
