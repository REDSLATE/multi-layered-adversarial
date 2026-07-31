"""Entry Timing Gate — hard late-entry veto (2026-07-31 doctrine).

Operator directive: "stop buying after the momentum is done."

The parabolic/late-entry signal used to be an ADVISORY score-nudge in
`doctrine/base_labels.py` — it shrank the order instead of refusing
it, so the system kept buying tops in miniature. It is now a HARD
gate (`shared/auto_router_stages.py::_gate_entry_timing`) sitting
between Risk approval and broker submission, with a fresh-price
revalidation at submit so a stale intent can't execute after the move
has already run.

What this file pins:
    * early reclaim / accumulation → ALLOWED (gate returns None)
    * parabolic / topping / fade    → HARD BLOCK (not a size cut)
    * velocity / VWAP ceilings are per-universe-class, and small-cap
      momentum is STRICTER than large-cap / ETF
    * fresh price extended beyond the emit-time snapshot price → block
    * SELL / COVER / SHORT and non-equity lanes → never blocked
    * missing `parabolic_phase` (thin bars) or missing fresh price →
      FAIL OPEN, never a block
    * `ENTRY_TIMING_GATE_ENABLED` OFF (the default, shadow posture) →
      the stage evaluates and stamps but NEVER blocks
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")

from shared import entry_timing  # noqa: E402
from shared.auto_router_helpers import RouteContext  # noqa: E402
from shared.doctrine.universe_classifier import UniverseClass  # noqa: E402


# ─── Helpers ────────────────────────────────────────────────────────

def _snapshot(**overrides):
    base = {
        "symbol": "HOTH",
        "price": 5.00,
        "market_cap_band": "small",
        "parabolic_phase": "accumulation",
        "velocity_1m": 0.4,
        "velocity_5m": 1.2,
        "vwap_distance_pct": 1.0,
        "rvol_acceleration": 1.4,
        "peak_drop_pct": 0.0,
    }
    base.update(overrides)
    return base


def _intent(**overrides):
    base = {
        "intent_id": "test-entry-timing",
        "symbol": "HOTH",
        "action": "BUY",
        "lane": "equity",
        "stack": "camino",
        "ingest_ts": "2026-07-31T14:00:00+00:00",
        "requested_notional_usd": 10.0,
        "snapshot": _snapshot(),
    }
    base.update(overrides)
    return base


def _ctx(intent):
    ctx = RouteContext(intent=intent)
    ctx.finalize_inputs()
    ctx.notional_raw = 10.0
    ctx.notional_source = "brain_legacy"
    ctx.final_notional = 10.0
    ctx.sd = SimpleNamespace(
        verdict="fire", reason="strategist_proposes", lane=ctx.lane,
        intent_brain="camino", strategist="camino", governor="hellcat",
        executor="camino", auditor="barracuda",
        angels={"strategist": "Raziel"}, risk_multiplier=1.0,
    )
    ctx.rc = SimpleNamespace(ok=True, reason="ok", notional_usd=10.0)
    return ctx


@pytest.fixture
def gate_scaffold(monkeypatch):
    """Fake db + executions module, and a stubbed fresh-price fetch so
    no test ever touches Webull."""
    from shared import auto_router as ar
    from shared import auto_router_stages as stages

    updated_docs: list[dict] = []
    coll = MagicMock()

    async def _update_one(query, update):
        updated_docs.append({"query": query, "update": update})

    coll.update_one = _update_one
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)

    executions_mod = MagicMock()
    executions_mod.record = AsyncMock(return_value="exec-row-id")

    monkeypatch.setattr(ar, "db", fake_db)
    import shared as _shared_pkg
    monkeypatch.setattr(_shared_pkg, "executions", executions_mod, raising=False)
    monkeypatch.setitem(sys.modules, "shared.executions", executions_mod)

    # Gate ARMED for most tests; the shadow test flips it back off.
    monkeypatch.setenv("ENTRY_TIMING_GATE_ENABLED", "true")

    state = {
        "fresh_price": 5.00,
        "updated_docs": updated_docs,
        "executions_mod": executions_mod,
        "stages": stages,
    }

    async def _fake_fetch(symbol, lane):  # noqa: ARG001
        return state["fresh_price"]

    monkeypatch.setattr(stages, "_fetch_fresh_price", _fake_fetch)
    return state


def _stamps(updated_docs, key="gate_state"):
    return [
        d["update"]["$set"] for d in updated_docs
        if key in d["update"].get("$set", {})
    ]


# ═══════════════════════════════════════════════════════════════════
# 1. Allowed entries
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_accumulation_early_reclaim_is_allowed(gate_scaffold):
    """Early run / healthy expansion is exactly the setup we WANT —
    the gate must pass it through untouched."""
    s = gate_scaffold
    ctx = _ctx(_intent())

    verdict = await s["stages"]._gate_entry_timing(ctx)

    assert verdict is None
    assert _stamps(s["updated_docs"]) == []
    s["executions_mod"].record.assert_not_awaited()


@pytest.mark.asyncio
async def test_neutral_phase_with_calm_metrics_is_allowed(gate_scaffold):
    s = gate_scaffold
    ctx = _ctx(_intent(snapshot=_snapshot(
        parabolic_phase="neutral", velocity_5m=0.5, vwap_distance_pct=0.3,
    )))

    assert await s["stages"]._gate_entry_timing(ctx) is None


# ═══════════════════════════════════════════════════════════════════
# 2. Phase vetoes are HARD blocks (not size reductions)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.tripwire
@pytest.mark.parametrize("phase,reason", [
    ("parabolic", entry_timing.PARABOLIC_CHASE_RISK),
    ("topping", entry_timing.LATE_MOMENTUM_ENTRY),
    ("fade", entry_timing.ENTRY_WINDOW_EXPIRED),
])
@pytest.mark.asyncio
async def test_late_phases_hard_block(gate_scaffold, phase, reason):
    """parabolic / topping / fade must terminate the intent — the old
    advisory behaviour (ship it smaller) is gone."""
    s = gate_scaffold
    ctx = _ctx(_intent(snapshot=_snapshot(parabolic_phase=phase)))

    verdict = await s["stages"]._gate_entry_timing(ctx)

    assert verdict is not None, "phase veto must short-circuit the chain"
    assert verdict["verdict"] == "blocked"
    assert verdict["reason"] == reason
    assert verdict["broker_error_bucket"] == "entry_timing"

    stamps = _stamps(s["updated_docs"])
    assert len(stamps) == 1
    assert stamps[0]["gate_state"] == "blocked"
    assert stamps[0]["broker_reason"] == reason
    assert stamps[0]["broker_error_bucket"] == "entry_timing"
    assert phase in stamps[0]["broker_error_detail"]
    # The price the decision was made on is stamped for the audit trail.
    assert stamps[0]["entry_timing_market_price"] == 5.00

    # Exactly one executions row, ok=False. Size was NOT reduced —
    # the notional on the audit row is the full risk-approved size.
    s["executions_mod"].record.assert_awaited_once()
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["notional_usd"] == 10.0
    assert kwargs["broker_status"] == "blocked_by_entry_timing"


# ═══════════════════════════════════════════════════════════════════
# 3. Per-universe-class thresholds
# ═══════════════════════════════════════════════════════════════════

def test_small_cap_thresholds_are_stricter_than_large_cap_and_etf():
    small = entry_timing.thresholds_for(UniverseClass.SMALL_CAP_MOMENTUM)
    large = entry_timing.thresholds_for(UniverseClass.LARGE_CAP)
    etf = entry_timing.thresholds_for(UniverseClass.ETF)

    assert small.max_velocity_5m_pct < large.max_velocity_5m_pct
    assert small.max_vwap_distance_pct < large.max_vwap_distance_pct
    assert small.max_extension_pct < large.max_extension_pct
    # ETF/large-cap must NOT inherit the small-cap ceilings.
    assert etf.max_velocity_5m_pct != small.max_velocity_5m_pct
    # Crypto carries its own profile.
    crypto = entry_timing.thresholds_for(UniverseClass.CRYPTO)
    assert crypto.max_vwap_distance_pct != small.max_vwap_distance_pct


def test_thresholds_are_env_configurable_per_universe_class(monkeypatch):
    monkeypatch.setenv(
        "ENTRY_TIMING_MAX_VELOCITY_5M_PCT_SMALL_CAP_MOMENTUM", "99.0",
    )
    small = entry_timing.thresholds_for(UniverseClass.SMALL_CAP_MOMENTUM)
    large = entry_timing.thresholds_for(UniverseClass.LARGE_CAP)
    assert small.max_velocity_5m_pct == 99.0
    # The override is scoped to ONE class.
    assert large.max_velocity_5m_pct != 99.0


@pytest.mark.asyncio
async def test_velocity_blocks_small_cap_but_not_large_cap(gate_scaffold):
    """A 6%/5min run is a chase on a $5 small-cap runner and merely a
    fast tape on a mega-cap. Same number, different verdict."""
    s = gate_scaffold
    small_snap = _snapshot(
        parabolic_phase="neutral", velocity_5m=6.0, vwap_distance_pct=1.0,
    )
    verdict = await s["stages"]._gate_entry_timing(
        _ctx(_intent(snapshot=small_snap)),
    )
    assert verdict is not None
    assert verdict["reason"] == entry_timing.MISSED_ENTRY_CHASE_RISK
    assert verdict["universe_class"] == "SMALL_CAP_MOMENTUM"

    large_snap = _snapshot(
        symbol="AAPL", market_cap_band="mega", parabolic_phase="neutral",
        velocity_5m=6.0, vwap_distance_pct=1.0,
    )
    assert await s["stages"]._gate_entry_timing(
        _ctx(_intent(symbol="AAPL", snapshot=large_snap)),
    ) is None


@pytest.mark.asyncio
async def test_vwap_distance_ceiling_blocks(gate_scaffold):
    s = gate_scaffold
    ctx = _ctx(_intent(snapshot=_snapshot(
        parabolic_phase="neutral", velocity_5m=0.5, vwap_distance_pct=9.0,
    )))

    verdict = await s["stages"]._gate_entry_timing(ctx)

    assert verdict["reason"] == entry_timing.TOO_FAR_ABOVE_VWAP


# ═══════════════════════════════════════════════════════════════════
# 4. Stale-intent / fresh-price revalidation
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_price_extended_beyond_emit_snapshot_is_blocked(gate_scaffold):
    """The snapshot said $5.00; by submit time it's $5.20 (+4%). The
    move ran while the intent sat in the queue — refuse it."""
    s = gate_scaffold
    s["fresh_price"] = 5.20
    ctx = _ctx(_intent())

    verdict = await s["stages"]._gate_entry_timing(ctx)

    assert verdict is not None
    assert verdict["reason"] in (
        entry_timing.MOVE_ALREADY_EXTENDED, entry_timing.STALE_BUY_INTENT,
    )
    assert verdict["market_price"] == 5.20
    stamps = _stamps(s["updated_docs"])
    assert stamps[0]["broker_error_bucket"] == "entry_timing"
    assert "since emit" in stamps[0]["broker_error_detail"]


@pytest.mark.asyncio
async def test_price_drifting_down_since_emit_is_not_blocked(gate_scaffold):
    """Only an UPWARD run is a chase. A pullback since emit is the
    setup improving, not decaying."""
    s = gate_scaffold
    s["fresh_price"] = 4.90
    assert await s["stages"]._gate_entry_timing(_ctx(_intent())) is None


def test_extension_reason_code_splits_on_intent_age():
    thresholds = entry_timing.thresholds_for(UniverseClass.SMALL_CAP_MOMENTUM)
    fresh_intent = entry_timing.evaluate_entry_timing(
        snapshot=_snapshot(), thresholds=thresholds,
        fresh_price=5.30, emit_price=5.00, intent_age_sec=35.0,
    )
    old_intent = entry_timing.evaluate_entry_timing(
        snapshot=_snapshot(), thresholds=thresholds,
        fresh_price=5.30, emit_price=5.00, intent_age_sec=600.0,
    )
    assert fresh_intent["reason"] == entry_timing.MOVE_ALREADY_EXTENDED
    assert old_intent["reason"] == entry_timing.STALE_BUY_INTENT
    # Age NEVER blocks on its own — a young intent at a flat price
    # passes, which is why there is no sub-tick wall-clock age limit.
    calm = entry_timing.evaluate_entry_timing(
        snapshot=_snapshot(), thresholds=thresholds,
        fresh_price=5.00, emit_price=5.00, intent_age_sec=99999.0,
    )
    assert calm["block"] is False


# ═══════════════════════════════════════════════════════════════════
# 5. Scope — exits and non-equity lanes are untouched
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.tripwire
@pytest.mark.parametrize("action", ["SELL", "COVER", "SHORT", "HOLD"])
@pytest.mark.asyncio
async def test_non_buy_actions_are_never_blocked(gate_scaffold, action):
    """Exits must never be gated on entry timing — refusing to SELL a
    topping stock is the exact opposite of the intent here."""
    s = gate_scaffold
    ctx = _ctx(_intent(
        action=action, snapshot=_snapshot(parabolic_phase="topping"),
    ))

    assert await s["stages"]._gate_entry_timing(ctx) is None
    assert _stamps(s["updated_docs"]) == []


@pytest.mark.asyncio
async def test_crypto_lane_passes_through(gate_scaffold):
    s = gate_scaffold
    ctx = _ctx(_intent(
        lane="crypto", symbol="BTC/USD",
        snapshot=_snapshot(parabolic_phase="parabolic", lane="crypto"),
    ))

    assert await s["stages"]._gate_entry_timing(ctx) is None


# ═══════════════════════════════════════════════════════════════════
# 6. Fail-open on missing data
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.tripwire
@pytest.mark.asyncio
async def test_missing_parabolic_phase_fails_open(gate_scaffold):
    """The classifier needs ≥10 M1 bars; below that it stamps nothing.
    Absence of data must never hard-block a trade."""
    s = gate_scaffold
    snap = _snapshot()
    snap.pop("parabolic_phase")
    ctx = _ctx(_intent(snapshot=snap))

    assert await s["stages"]._gate_entry_timing(ctx) is None
    assert _stamps(s["updated_docs"]) == []


@pytest.mark.asyncio
async def test_unknown_phase_from_thin_bars_fails_open(gate_scaffold):
    s = gate_scaffold
    ctx = _ctx(_intent(snapshot=_snapshot(parabolic_phase="unknown")))

    assert await s["stages"]._gate_entry_timing(ctx) is None


@pytest.mark.asyncio
async def test_missing_snapshot_entirely_fails_open(gate_scaffold):
    s = gate_scaffold
    ctx = _ctx(_intent(snapshot=None))

    assert await s["stages"]._gate_entry_timing(ctx) is None


@pytest.mark.asyncio
async def test_no_fresh_price_fails_open(gate_scaffold):
    """Even a parabolic phase passes when we couldn't resolve a fresh
    price — we refuse to hard-block on data we don't have."""
    s = gate_scaffold
    s["fresh_price"] = None
    ctx = _ctx(_intent(snapshot=_snapshot(parabolic_phase="parabolic")))

    assert await s["stages"]._gate_entry_timing(ctx) is None
    assert _stamps(s["updated_docs"]) == []


@pytest.mark.asyncio
async def test_evaluation_exception_fails_open(gate_scaffold, monkeypatch):
    s = gate_scaffold

    def _boom(**_kwargs):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(entry_timing, "evaluate_entry_timing", _boom)
    ctx = _ctx(_intent(snapshot=_snapshot(parabolic_phase="parabolic")))

    assert await s["stages"]._gate_entry_timing(ctx) is None


# ═══════════════════════════════════════════════════════════════════
# 7. Shadow posture — the SHIPPED default
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.tripwire
def test_gate_defaults_off(monkeypatch):
    monkeypatch.delenv("ENTRY_TIMING_GATE_ENABLED", raising=False)
    assert entry_timing.gate_enabled() is False


@pytest.mark.asyncio
async def test_disabled_gate_never_blocks_but_stamps_shadow(gate_scaffold,
                                                            monkeypatch):
    """With the flag OFF the stage still computes the verdict and
    stamps `entry_timing_shadow` (so the operator can measure hit rate
    before arming it) — but it must NEVER short-circuit the chain or
    touch `gate_state`."""
    s = gate_scaffold
    monkeypatch.setenv("ENTRY_TIMING_GATE_ENABLED", "false")
    ctx = _ctx(_intent(snapshot=_snapshot(parabolic_phase="parabolic")))

    assert await s["stages"]._gate_entry_timing(ctx) is None

    assert _stamps(s["updated_docs"]) == []
    shadow = _stamps(s["updated_docs"], key="entry_timing_shadow")
    assert len(shadow) == 1
    stamp = shadow[0]["entry_timing_shadow"]
    assert stamp["enabled"] is False
    assert stamp["would_block"] is True
    assert stamp["reason"] == entry_timing.PARABOLIC_CHASE_RISK
    assert stamp["universe_class"] == "SMALL_CAP_MOMENTUM"
    s["executions_mod"].record.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
# 8. Reason-code inventory
# ═══════════════════════════════════════════════════════════════════

def test_all_seven_reason_codes_exist():
    assert entry_timing.REASON_CODES == {
        "ENTRY_WINDOW_EXPIRED", "STALE_BUY_INTENT", "MOVE_ALREADY_EXTENDED",
        "MISSED_ENTRY_CHASE_RISK", "TOO_FAR_ABOVE_VWAP",
        "LATE_MOMENTUM_ENTRY", "PARABOLIC_CHASE_RISK",
    }


# ═══════════════════════════════════════════════════════════════════
# 9. Learning capture is PRESERVED
# ═══════════════════════════════════════════════════════════════════
# The old advisory rationale for shipping late entries small was
# "we learn by trading it". The replacement is the counterfactual: a
# blocked late entry is still distilled into `counterfactual_signals`
# and resolved MISSED_WIN / CORRECT_BLOCK, which is what validates
# (or falsifies) these thresholds without risking capital.

@pytest.mark.tripwire
@pytest.mark.asyncio
async def test_entry_timing_block_still_distills_counterfactual(gate_scaffold):
    from db import db as real_db
    from namespaces import COUNTERFACTUAL_SIGNALS
    from shared import counterfactuals as cf

    s = gate_scaffold
    intent_id = "entry-timing-cf-test-1"
    intent = _intent(
        intent_id=intent_id, snapshot=_snapshot(parabolic_phase="topping"),
    )
    ctx = _ctx(intent)

    await real_db[COUNTERFACTUAL_SIGNALS].delete_many({"signal_id": intent_id})
    try:
        verdict = await s["stages"]._gate_entry_timing(ctx)
        assert verdict["verdict"] == "blocked"

        # The gate stamped the intent as a blocked directional intent,
        # which is exactly the distiller's predicate.
        assert cf.should_create_counterfactual(intent) is True
        assert await cf.distill_intent_to_signal(intent, real_db) is True

        row = await real_db[COUNTERFACTUAL_SIGNALS].find_one(
            {"signal_id": intent_id},
        )
        assert row is not None
        assert row["direction"] == "BUY"
        assert row["blocked_reason"] == entry_timing.LATE_MOMENTUM_ENTRY
        assert row["entry_reference_price"] == 5.00
        assert row["experience_type"] == "counterfactual"
        assert row["status"] == "tracking"
        # Learning evidence only — never re-routable to a broker.
        assert row["may_execute"] is False
    finally:
        await real_db[COUNTERFACTUAL_SIGNALS].delete_many(
            {"signal_id": intent_id},
        )


# ═══════════════════════════════════════════════════════════════════
# 10. base_labels demotion — the other half of the change
# ═══════════════════════════════════════════════════════════════════
# Once the gate owns the "too late" veto, the advisory score deltas in
# `doctrine/base_labels.py` are double-counting: they dilute the
# quality score with a signal another layer already enforces. Labels
# and reasons stay (observability); the score mutation goes.

def _labels_for(phase, monkeypatch, **env):
    from shared.doctrine import base_labels

    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    snap = {
        "symbol": "HOTH", "price": 5.0, "gap_pct": 6.0,
        "relative_volume": 4.0, "spread_bps": 20.0, "pattern": "bull_flag",
        "market_regime": "trend", "parabolic_phase": phase,
        "velocity_5m": 14.0,
    }
    return base_labels.build_doctrine_labels(snap)


@pytest.mark.parametrize("phase,label", [
    ("parabolic", "PARABOLIC_LATE_ENTRY_RISK"),
    ("topping", "TOPPING_DISTRIBUTION_STARTED"),
    ("fade", "FADE_LOWER_HIGHS_LOWER_LOWS"),
    ("accumulation", "ACCUMULATION_HEALTHY_EXPANSION"),
])
def test_phase_labels_survive_the_demotion(monkeypatch, phase, label):
    """Whatever the flag says, the LABEL is always emitted — the
    operator and the bucket analyzer read these."""
    demoted = _labels_for(
        phase, monkeypatch, PARABOLIC_SCORE_DELTA_ENABLED="false",
    )
    legacy = _labels_for(
        phase, monkeypatch, PARABOLIC_SCORE_DELTA_ENABLED="true",
    )
    assert label in demoted.labels
    assert label in legacy.labels
    assert demoted.reasons == legacy.reasons


def test_demotion_stops_score_mutation_but_keeps_label(monkeypatch):
    baseline = _labels_for(
        "neutral", monkeypatch, PARABOLIC_SCORE_DELTA_ENABLED="false",
    )
    demoted = _labels_for(
        "parabolic", monkeypatch, PARABOLIC_SCORE_DELTA_ENABLED="false",
    )
    legacy = _labels_for(
        "parabolic", monkeypatch, PARABOLIC_SCORE_DELTA_ENABLED="true",
    )

    assert demoted.score == baseline.score, "score must not be diluted"
    assert legacy.score < baseline.score, "legacy advisory delta still bites"
    assert "PARABOLIC_LATE_ENTRY_RISK" in demoted.labels
    assert any("parabolic_5m_velocity" in r for r in demoted.reasons)


def test_score_delta_default_tracks_the_gate_flag(monkeypatch):
    """The two systems are never both half-managing the same risk:
    arming the gate demotes the advisory deltas, and vice versa."""
    from shared.doctrine import base_labels

    monkeypatch.delenv("PARABOLIC_SCORE_DELTA_ENABLED", raising=False)

    monkeypatch.delenv("ENTRY_TIMING_GATE_ENABLED", raising=False)
    assert base_labels.parabolic_score_deltas_enabled() is True

    monkeypatch.setenv("ENTRY_TIMING_GATE_ENABLED", "true")
    assert base_labels.parabolic_score_deltas_enabled() is False

    # Explicit override wins either way.
    monkeypatch.setenv("PARABOLIC_SCORE_DELTA_ENABLED", "true")
    assert base_labels.parabolic_score_deltas_enabled() is True
