"""Mission Control — transient broker-account context (operator patch,
2026-06 `mc_account_context_patch`).

Reads the live lane adapter via the EXISTING `broker_router.
adapter_for_lane` resolution (same broker the live route will use) and
produces a compact, sanitized snapshot for the decision stack.

Storage doctrine (pinned by patch README):
  * Full snapshots are process-local ONLY (10s TTL cache).
  * NEVER write raw positions/account snapshots to Mongo.
  * Persist only the compact AccountFit result on the intent/receipt.

Adapter-surface deltas vs the patch draft (adapted to actual MC):
  * Kraken adapter has no `list_open_orders` → degrade to ().
  * MooMoo adapter exposes `account()` / `positions()` → resolved via
    fallback method names. No new broker connection is created.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class AccountSnapshot:
    lane: str
    broker: str
    captured_at_ms: int
    equity: float
    cash: float
    buying_power: float
    positions: tuple[dict[str, Any], ...]
    open_orders: tuple[dict[str, Any], ...]

    def model_payload(self) -> dict[str, Any]:
        return asdict(self)


_TTL_SEC = 10.0
_cache: dict[str, tuple[float, AccountSnapshot]] = {}
_locks: dict[str, asyncio.Lock] = {}


def reset_for_tests() -> None:
    _cache.clear()


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_position(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(p.get("symbol") or "").upper(),
        "qty": _f(p.get("qty")),
        "side": str(p.get("side") or ""),
        "avg_entry_price": _f(p.get("avg_entry_price")),
        "market_value": _f(p.get("market_value")),
        "unrealized_pl": _f(p.get("unrealized_pl")),
        "unrealized_plpc": _f(p.get("unrealized_plpc")),
        "current_price": _f(p.get("current_price")) or None,
    }


def _sanitize_order(o: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(o.get("symbol") or "").upper(),
        "side": str(o.get("side") or o.get("action") or "").upper(),
        "qty": _f(o.get("qty") or o.get("quantity")),
        "notional": _f(o.get("notional") or o.get("notional_usd")),
        "status": str(o.get("status") or o.get("state") or "open").lower(),
    }


async def _call_first(adapter: Any, names: tuple[str, ...], default: Any) -> Any:
    """Adapter surfaces vary (Webull/Kraken/MooMoo). Use the first
    available coroutine method; degrade to `default`."""
    for name in names:
        fn = getattr(adapter, name, None)
        if callable(fn):
            try:
                return await fn()
            except Exception:  # noqa: BLE001
                return default
    return default


async def get_account_snapshot(
    lane: str,
    *,
    broker_override: Optional[str] = None,
    force: bool = False,
) -> AccountSnapshot:
    lane = lane.lower().strip()
    key = f"{lane}:{broker_override or ''}"
    now = time.monotonic()

    hit = _cache.get(key)
    if not force and hit and (now - hit[0]) < _TTL_SEC:
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        hit = _cache.get(key)
        if not force and hit and (now - hit[0]) < _TTL_SEC:
            return hit[1]

        from shared.broker_router import adapter_for_lane  # noqa: WPS433
        adapter = await adapter_for_lane(lane, broker_override=broker_override)
        if adapter is None:
            raise RuntimeError(f"no live broker adapter for lane={lane!r}")

        account, positions, orders = await asyncio.gather(
            _call_first(adapter, ("get_account", "account"), {}),
            _call_first(adapter, ("list_positions", "positions"), []),
            _call_first(adapter, ("list_open_orders",), []),
        )

        account = account or {}
        equity = _f(account.get("equity"))
        cash = _f(account.get("cash"))
        bp = _f(account.get("buying_power"))
        if bp <= 0:
            bp = cash

        snap = AccountSnapshot(
            lane=lane,
            broker=str(getattr(adapter, "name", "unknown")),
            captured_at_ms=int(time.time() * 1000),
            equity=equity,
            cash=cash,
            buying_power=bp,
            positions=tuple(_sanitize_position(x) for x in (positions or [])),
            open_orders=tuple(_sanitize_order(x) for x in (orders or [])),
        )
        _cache[key] = (time.monotonic(), snap)
        return snap


def invalidate_account_snapshot(lane: str,
                                broker_override: Optional[str] = None) -> None:
    _cache.pop(f"{lane.lower().strip()}:{broker_override or ''}", None)
