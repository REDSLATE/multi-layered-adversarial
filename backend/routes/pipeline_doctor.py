"""Pipeline Doctor — one-shot stage-by-stage trading-loop diagnosis.

GET /api/admin/pipeline-doctor

Built 2026-07-19 to diagnose the prod `SNAPS=0 / no_data 100%` outage.
Reports, in ONE bounded call, exactly where the data pipeline breaks:

    Stage 1  universe    — live_universe / patterns_universe / env fallback
    Stage 2  feeders     — latest feeder_health_audit row per provider
    Stage 3  freshness   — per sampled symbol: latest bar age vs the SAME
                           max-age gate `snapshot_service._build_one` applies
    Stage 4  pulse       — last receipts (snaps, silence reasons)

Every Mongo read is indexed + `max_time_ms`-bounded; no collection scans.
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from mc_pulse.snapshot_service import (
    BAR_WINDOW,
    MAX_BAR_AGE_SECONDS,
    _discover_universe,
    _parse_iso,
)

router = APIRouter(prefix="/admin/pipeline-doctor", tags=["pipeline-doctor"])

_SAMPLE_PER_LANE = 5
_TF_PREFERENCE = {
    "equity": ["1m", "5m"],
    "crypto": ["1m", "5m", "1d"],
}
_MAX_AGE_BY_TF = {
    "1m": MAX_BAR_AGE_SECONDS,
    "5m": MAX_BAR_AGE_SECONDS * 3,
    "1d": 3 * 86400,
}


def _age_s(ts, now: datetime) -> float | None:
    dt = _parse_iso(ts)
    return round((now - dt).total_seconds(), 1) if dt else None


async def _stage_universe(now: datetime) -> dict:
    live: dict = {}
    try:
        from shared.universe.live_universe import read_all_universes
        docs = await read_all_universes()
        for lane, doc in docs.items():
            syms = doc.get("symbols") or []
            tradable = [s for s in syms if s.get("tradable", True)]
            live[lane] = {
                "total": len(syms),
                "tradable": len(tradable),
                "built_at": doc.get("built_at") or doc.get("updated_at"),
                "built_age_s": _age_s(doc.get("built_at") or doc.get("updated_at"), now),
                "sample": [s.get("canonical_symbol") for s in tradable[:5]],
            }
    except Exception as exc:  # noqa: BLE001
        live = {"error": str(exc)[:300]}

    patterns_count = None
    try:
        patterns_count = await db["patterns_universe"].count_documents(
            {"active": True}, maxTimeMS=3000,
        )
    except Exception as exc:  # noqa: BLE001
        patterns_count = f"error: {str(exc)[:120]}"

    effective = await _discover_universe()
    return {
        "live_universe": live,
        "patterns_universe_active_count": patterns_count,
        "effective_universe": {lane: len(s) for lane, s in effective.items()},
        "effective_sample": {lane: s[:_SAMPLE_PER_LANE] for lane, s in effective.items()},
        "env_defaults": {
            "MC_UNIVERSE_EQUITY": os.environ.get("MC_UNIVERSE_EQUITY", ""),
            "MC_UNIVERSE_CRYPTO": os.environ.get("MC_UNIVERSE_CRYPTO", ""),
        },
        "_effective": effective,
    }


async def _stage_feeders(now: datetime) -> dict:
    latest_by_provider: dict[str, dict] = {}
    try:
        cursor = db["feeder_health_audit"].find(
            {}, {"_id": 0, "provider": 1, "endpoint": 1, "status_code": 1,
                 "error_type": 1, "message": 1, "ts": 1},
        ).sort([("ts", -1)]).max_time_ms(5000).limit(60)
        async for row in cursor:
            prov = row.get("provider") or "unknown"
            if prov not in latest_by_provider:
                latest_by_provider[prov] = {
                    "ts": row.get("ts"),
                    "age_s": _age_s(row.get("ts"), now),
                    "status_code": row.get("status_code"),
                    "error_type": row.get("error_type"),
                    "endpoint": row.get("endpoint"),
                    "message": str(row.get("message") or "")[:200],
                }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    return {
        "latest_audit_per_provider": latest_by_provider,
        "env_flags": {
            "WEBULL_OHLC_FEEDER_ENABLED": os.environ.get("WEBULL_OHLC_FEEDER_ENABLED", "(default true)"),
            "KRAKEN_OHLC_FEEDER_ENABLED": os.environ.get("KRAKEN_OHLC_FEEDER_ENABLED", "(default true)"),
            "KRAKEN_OHLC_INTRADAY_ENABLED": os.environ.get("KRAKEN_OHLC_INTRADAY_ENABLED", "(default true)"),
            "FINNHUB_ENABLED": os.environ.get("FINNHUB_ENABLED", ""),
            "POLYGON_FLATFILES_ENABLED": os.environ.get("POLYGON_FLATFILES_ENABLED", ""),
        },
    }


async def _probe_symbol(lane: str, symbol: str, now: datetime) -> dict:
    """Replay the EXACT gate `snapshot_service._build_one` applies."""
    per_tf = []
    verdict = "no_bars_any_tf"
    for tf in _TF_PREFERENCE[lane]:
        latest_ts = None
        count = 0
        source = None
        try:
            docs = await db["shared_ohlcv_bars"].find(
                {"symbol": symbol, "tf": tf},
                {"ts": 1, "source": 1, "_id": 0},
                sort=[("ts", -1)],
            ).max_time_ms(3000).limit(BAR_WINDOW).to_list(BAR_WINDOW)
            count = len(docs)
            if docs:
                latest_ts = docs[0].get("ts")
                source = docs[0].get("source")
        except Exception as exc:  # noqa: BLE001
            per_tf.append({"tf": tf, "error": str(exc)[:120]})
            continue
        age = _age_s(latest_ts, now)
        max_age = _MAX_AGE_BY_TF.get(tf, MAX_BAR_AGE_SECONDS)
        entry = {
            "tf": tf, "bars_in_window": count, "source": source,
            "latest_bar_ts": latest_ts, "age_s": age, "max_age_s": max_age,
            "fresh_enough": age is not None and age <= max_age,
        }
        per_tf.append(entry)
        if count and verdict == "no_bars_any_tf":
            # snapshot_service picks the FIRST tf with any coverage,
            # then applies the age gate — later tfs never get a shot.
            verdict = (
                "would_build_snapshot" if entry["fresh_enough"]
                else f"rejected_stale_{tf}_age_{age}s_gt_{max_age}s"
            )
    return {"symbol": symbol, "lane": lane, "verdict": verdict, "per_tf": per_tf}


async def _stage_freshness(effective: dict[str, list[str]], now: datetime) -> dict:
    probes = []
    for lane, symbols in effective.items():
        for sym in symbols[:_SAMPLE_PER_LANE]:
            probes.append(await _probe_symbol(lane, sym, now))
    return {"sampled_symbols": probes}


async def _stage_pulse(now: datetime) -> dict:
    receipts = []
    try:
        cursor = db["mc_pulses"].find(
            {}, {"_id": 0, "started_at": 1, "snapshot_count": 1,
                 "brains_completed": 1, "brains_silent": 1,
                 "intents_emitted": 1, "runtime_mode": 1},
        ).sort([("started_at", -1)]).max_time_ms(5000).limit(5)
        async for d in cursor:
            silences = Counter(
                (b.get("reason") or "unknown") for b in (d.get("brains_silent") or [])
            )
            receipts.append({
                "started_at": d.get("started_at"),
                "age_s": _age_s(d.get("started_at"), now),
                "snapshot_count": d.get("snapshot_count"),
                "brains_completed": d.get("brains_completed"),
                "intents_emitted": d.get("intents_emitted"),
                "runtime_mode": d.get("runtime_mode"),
                "silence_reasons": dict(silences),
            })
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    return {"last_receipts": receipts}


def _verdict(universe: dict, feeders: dict, freshness: dict, pulse: dict) -> str:
    eff = universe.get("effective_universe") or {}
    if not any(eff.values()):
        return "STAGE 1 BROKEN: effective universe is EMPTY — no symbols to snapshot. Check live_universe refresher and patterns_universe."
    probes = freshness.get("sampled_symbols") or []
    buildable = [p for p in probes if p["verdict"] == "would_build_snapshot"]
    no_bars = [p for p in probes if p["verdict"] == "no_bars_any_tf"]
    stale = [p for p in probes if p["verdict"].startswith("rejected_stale")]
    if probes and not buildable:
        if len(no_bars) == len(probes):
            return "STAGE 2 BROKEN: universe symbols have ZERO bars in shared_ohlcv_bars — feeders are not writing these symbols at all. Check latest_audit_per_provider ages/errors."
        if stale:
            worst = max((p["per_tf"][0].get("age_s") or 0) for p in stale if p["per_tf"])
            return f"STAGE 3 BROKEN: bars exist but are STALE (worst sampled age ~{int(worst)}s). Feeders stopped writing fresh data — check latest_audit_per_provider."
        return "STAGE 2/3 BROKEN: no sampled symbol can build a snapshot. Inspect sampled_symbols per_tf detail."
    receipts = (pulse.get("last_receipts") or [])
    if buildable and receipts and all((r.get("snapshot_count") or 0) == 0 for r in receipts):
        return "STAGE 4 SUSPECT: fresh bars exist and snapshots SHOULD build, but pulse receipts show 0 snaps — pulse worker may be down or reading a different DB."
    if not receipts:
        return "STAGE 4 BROKEN: no pulse receipts at all — pulse worker is not running (RISEDUAL_MC_PULSE_ENABLED?)."
    return f"PIPELINE OK: {len(buildable)}/{len(probes)} sampled symbols build snapshots; latest pulse snaps={receipts[0].get('snapshot_count')}."


@router.get("")
async def pipeline_doctor(_user: dict = Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    universe = await _stage_universe(now)
    effective = universe.pop("_effective")
    feeders = await _stage_feeders(now)
    freshness = await _stage_freshness(effective, now)
    pulse = await _stage_pulse(now)
    return {
        "generated_at": now.isoformat(),
        "verdict": _verdict(universe, feeders, freshness, pulse),
        "stage_1_universe": universe,
        "stage_2_feeders": feeders,
        "stage_3_freshness": freshness,
        "stage_4_pulse": pulse,
    }
