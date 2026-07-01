"""In-memory config cache — decouples the hot path from Mongo.

Reads (seats, master switch, lane enabled, governor multipliers)
happen ONLY against this in-memory cache. A background refresher
task pulls updates from Mongo every `TRADER_CACHE_REFRESH_SEC`
(default 60) with a short per-call timeout. If Mongo is down the
cache serves the last-known-good values — the trader keeps trading.

Resolution order on refresh (highest wins):
    1. Mongo `seat_registry` / `runtime_flags`
    2. SQLite `seat_cache` / `flags_cache` (last-good snapshot)
    3. Hard-coded DEFAULT_SEATS / DEFAULT_FLAGS

Every successful Mongo pull is persisted to SQLite so a cold boot
against a dead Mongo still gives the trader the last-known state.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from trader import config, store


logger = logging.getLogger("trader.state")


# ─── hard-coded fallbacks (same as prior /app/trader/seat.py) ──────

DEFAULT_SEATS: dict[str, dict[str, str]] = {
    "equity": {
        "strategist": "camino",     # Raziel
        "governor":   "hellcat",    # Nuriel
        "executor":   "gto",        # Paschar
        "auditor":    "barracuda",  # Sariel
    },
    "crypto": {
        "strategist": "hellcat",    # Remiel
        "governor":   "camino",     # Cassiel
        "executor":   "gto",        # Israfel
        "auditor":    "barracuda",  # Zadkiel
    },
}

DEFAULT_GOVERNOR_MULTIPLIER = 1.0

# When Mongo has no `master_trading_switch` doc, the sidecar defaults
# to DISARMED (safer). The operator explicitly arms via MC's UI.
DEFAULT_MASTER_ARMED = False
DEFAULT_LANE_ENABLED = True   # if the doc is missing, assume enabled


# ─── in-memory cache (module-level; single process) ────────────────

_seats: dict[str, dict[str, Optional[str]]] = {}
_governor_mult: dict[str, float] = {}
_master_armed: bool = DEFAULT_MASTER_ARMED
_lane_enabled: dict[str, bool] = {"equity": DEFAULT_LANE_ENABLED,
                                  "crypto": DEFAULT_LANE_ENABLED}
_last_refresh_ok_ts: Optional[str] = None
_last_refresh_error: Optional[str] = None

_refresh_task: Optional[asyncio.Task] = None
_manual_refresh_event: Optional[asyncio.Event] = None


# ─── public reads (used by risk.py + seat.py) ──────────────────────

def get_lane_seats(lane: str) -> dict[str, Optional[str]]:
    ln = (lane or "").lower()
    return dict(_seats.get(ln) or DEFAULT_SEATS.get(ln) or {})


def governor_multiplier(lane: str) -> float:
    ln = (lane or "").lower()
    v = _governor_mult.get(ln)
    return v if v is not None else DEFAULT_GOVERNOR_MULTIPLIER


def master_switch_armed() -> bool:
    return bool(_master_armed)


def lane_enabled(lane: str) -> bool:
    ln = (lane or "").lower()
    return bool(_lane_enabled.get(ln, DEFAULT_LANE_ENABLED))


def snapshot() -> dict:
    """Health probe — what does the cache currently hold?"""
    return {
        "seats": {k: dict(v) for k, v in _seats.items()},
        "governor_multiplier": dict(_governor_mult),
        "master_armed": _master_armed,
        "lane_enabled": dict(_lane_enabled),
        "last_refresh_ok_ts": _last_refresh_ok_ts,
        "last_refresh_error": _last_refresh_error,
    }


# ─── SQLite fallback hydration (used on cold boot with dead Mongo) ─

def hydrate_from_sqlite() -> None:
    """Populate the in-memory cache from the last-known-good SQLite
    snapshot. Never fails — if SQLite is empty the DEFAULT_ values
    already resolve correctly from the getters above."""
    try:
        cached_seats = store.read_seat_cache()
    except Exception as e:  # noqa: BLE001
        logger.warning("hydrate_from_sqlite: read_seat_cache failed: %s", e)
        cached_seats = {}
    for seat_id, meta in cached_seats.items():
        lane = meta.get("lane")
        role = meta.get("role")
        holder = meta.get("holder")
        rmult = meta.get("risk_multiplier")
        if not lane or not role:
            continue
        _seats.setdefault(lane, {})[role] = holder
        if role == "governor" and rmult is not None:
            _governor_mult[lane] = float(rmult)

    master = store.read_flag_cache("master_armed", None)
    if master is not None:
        globals()["_master_armed"] = bool(master)

    lane_map = store.read_flag_cache("lane_enabled", None)
    if isinstance(lane_map, dict):
        for k, v in lane_map.items():
            _lane_enabled[k.lower()] = bool(v)

    logger.info(
        "state hydrated from sqlite seats=%d gov_mults=%d master=%s "
        "lane_enabled=%s",
        sum(len(v) for v in _seats.values()),
        len(_governor_mult), _master_armed, dict(_lane_enabled),
    )


# ─── Mongo refresh (background task) ───────────────────────────────

async def _refresh_once(db) -> None:
    """One pull from Mongo. Every call has a short timeout so a hung
    Atlas can never wedge the refresher."""
    global _last_refresh_ok_ts, _last_refresh_error

    from datetime import datetime, timezone
    fresh_seats: dict[str, dict[str, Optional[str]]] = {}
    fresh_gov: dict[str, float] = {}

    # 1. seat_registry — one query per lane×role (fast, indexed on _id)
    for lane in config.LANES:
        fresh_seats.setdefault(lane, {})
        for role in config.ROLES:
            sid = f"{lane}:{role}"
            try:
                doc = await asyncio.wait_for(
                    db["seat_registry"].find_one(
                        {"_id": sid},
                        {"_id": 0, "holder": 1, "risk_multiplier": 1},
                    ),
                    timeout=2.0,
                )
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"seat_registry:{sid}: {e}") from e
            holder = (doc or {}).get("holder")
            fresh_seats[lane][role] = holder
            if role == "governor":
                raw = (doc or {}).get("risk_multiplier")
                if raw is not None:
                    try:
                        fresh_gov[lane] = max(0.0, min(2.0, float(raw)))
                    except (TypeError, ValueError):
                        pass

    # 2. master switch
    try:
        doc = await asyncio.wait_for(
            db["runtime_flags"].find_one(
                {"_id": "master_trading_switch"},
                {"_id": 0, "enabled": 1},
            ),
            timeout=2.0,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"master_trading_switch: {e}") from e
    master_armed = bool((doc or {}).get("enabled")) if doc else DEFAULT_MASTER_ARMED

    # 3. lane_enabled
    try:
        doc = await asyncio.wait_for(
            db["runtime_flags"].find_one(
                {"_id": "lane_enabled"}, {"_id": 0},
            ),
            timeout=2.0,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"lane_enabled: {e}") from e
    lane_map: dict[str, bool] = {}
    for lane in config.LANES:
        raw = (doc or {}).get(lane) if doc else None
        lane_map[lane] = DEFAULT_LANE_ENABLED if raw is None else bool(raw)

    # ─── commit to memory ─────────────────────────────────────────
    _seats.clear()
    for lane, roles in fresh_seats.items():
        # merge defaults in for any role Mongo returned as None
        merged = dict(DEFAULT_SEATS.get(lane, {}))
        for role, holder in roles.items():
            if holder:
                merged[role] = holder
        _seats[lane] = merged
    _governor_mult.clear()
    _governor_mult.update(fresh_gov)
    globals()["_master_armed"] = master_armed
    _lane_enabled.clear()
    _lane_enabled.update(lane_map)
    _last_refresh_ok_ts = datetime.now(timezone.utc).isoformat()
    _last_refresh_error = None

    # ─── persist to SQLite so a cold boot survives a dead Mongo ───
    try:
        for lane, roles in _seats.items():
            for role, holder in roles.items():
                rmult = _governor_mult.get(lane) if role == "governor" else None
                store.upsert_seat_cache(
                    f"{lane}:{role}", lane, role, holder, rmult,
                )
        store.upsert_flag_cache("master_armed", _master_armed)
        store.upsert_flag_cache("lane_enabled", dict(_lane_enabled))
    except Exception as e:  # noqa: BLE001
        logger.warning("state cache -> sqlite persist failed: %s", e)


async def refresh_loop(db) -> None:
    """Background task. Refreshes on every `interval` and on the
    manual event (POST /api/admin/trader/reload-caches)."""
    global _manual_refresh_event, _last_refresh_error
    interval = max(5, int(os.environ.get("TRADER_CACHE_REFRESH_SEC", "60")))
    if _manual_refresh_event is None:
        _manual_refresh_event = asyncio.Event()
    logger.info("state refresh_loop started interval=%ss", interval)
    while True:
        try:
            await _refresh_once(db)
        except Exception as e:  # noqa: BLE001
            _last_refresh_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "state refresh failed (serving last-known-good): %s", e,
            )
        try:
            await asyncio.wait_for(
                _manual_refresh_event.wait(), timeout=interval,
            )
            _manual_refresh_event.clear()
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            logger.info("state refresh_loop cancelled")
            raise


def request_manual_refresh() -> bool:
    """Trigger an out-of-band refresh from the API. Returns False if
    the refresh loop isn't running (nothing to poke)."""
    if _manual_refresh_event is None:
        return False
    _manual_refresh_event.set()
    return True
