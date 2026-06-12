"""Bracket outcome resolver (P1, 2026-02-19).

Background worker that:
  1. Reads open brackets from `webull_bracket_intents` (status="open").
  2. For each open bracket, fetches the live snapshot price.
  3. Resolves the bracket as:
       - `tp_hit`   if price has reached the target (favorable side)
       - `sl_hit`   if price has reached the stop (adverse side)
       - `timeout`  if `now > expires_at` and neither threshold breached
  4. Writes the resolved bracket row back AND mirrors the outcome into
     `doctrine_sidecars.outcome_join` so the existing scorecard /
     auto_retire / wrapper-dampener machinery picks up the cleaner
     label with NO downstream changes.

Doctrine pin:
  * The resolver NEVER cancels broker orders — those are handled by
    the brain's own SELL intent or Webull's UI. The resolver's job
    is purely TRAINING-SIGNAL LABEL ASSIGNMENT.
  * Once written, an outcome is immutable. A bracket can only be
    resolved once.
  * The resolver fails closed: on a price-fetch error it skips the
    bracket and retries next tick. Brackets never "disappear" — they
    eventually time out.
  * Cadence is jittered so multiple MC pods (if scaled out) don't
    stampede the quotes API together.

Activation: master-gated on `RISEDUAL_BRACKET_OUTCOMES_ENABLED`
(same env as the bracket recorder). When OFF, the worker still
starts but logs a single "disabled" message and idles.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from typing import Any, Optional

from db import db
from namespaces import WEBULL_BRACKET_INTENTS, DOCTRINE_SIDECARS
from shared.broker.webull_brackets import bracket_outcomes_enabled

logger = logging.getLogger(__name__)


RESOLVER_INTERVAL_SEC = float(
    os.environ.get("RISEDUAL_BRACKET_RESOLVER_SEC", "30"),
)
# Watchdog: bail on any single tick that exceeds this. The quotes
# client is sync + cached; one tick should complete in well under
# 10s even for 50+ open brackets.
RESOLVER_TICK_TIMEOUT_SEC = float(
    os.environ.get("RISEDUAL_BRACKET_RESOLVER_TICK_TIMEOUT_SEC", "30"),
)


# ── outcome resolution math ───────────────────────────────────────


def _resolve_outcome(
    side: str,
    last_price: float,
    target: float,
    stop: float,
    expired: bool,
) -> Optional[str]:
    """Pure logic — given the current price + bracket params, return
    `tp_hit` / `sl_hit` / `timeout` / None (unresolved).

    Extracted as a free function so the test suite can exhaustively
    pin every directional case without spinning up the worker.
    """
    side_u = (side or "").upper()
    if side_u in ("BUY", "COVER"):
        # Long bracket: favorable direction is UP (price >= target).
        # Adverse is DOWN (price <= stop). TP wins ties on simultaneous
        # breach (doctrine: optimistic resolution for the brain's
        # thesis — the brain published a CONVICTION, we honor it on
        # ambiguity).
        if last_price >= target:
            return "tp_hit"
        if last_price <= stop:
            return "sl_hit"
    elif side_u in ("SELL", "SHORT"):
        # Short bracket: favorable is DOWN.
        if last_price <= target:
            return "tp_hit"
        if last_price >= stop:
            return "sl_hit"
    else:
        # Unknown side — never resolve.
        return None
    if expired:
        return "timeout"
    return None


def _pnl_for_resolution(
    side: str, qty: float, entry: float, resolved_at_price: float,
) -> float:
    """Compute realized $ PnL for the resolved bracket. Used so
    `outcome_join.pnl_usd` stays consistent with the categorical label —
    downstream consumers that key on pnl_usd (e.g., the calibration
    sweep) still get a coherent number."""
    side_u = (side or "").upper()
    if side_u in ("BUY", "COVER"):
        return (resolved_at_price - entry) * qty
    if side_u in ("SELL", "SHORT"):
        return (entry - resolved_at_price) * qty
    return 0.0


# ── price source (pluggable, defaults to Webull quotes) ───────────


async def _fetch_last_price(symbol: str, lane: str) -> Optional[float]:
    """Sync quotes client wrapped in `to_thread` so we don't block the
    event loop. Returns None on any failure — the resolver treats
    that as "skip this tick".
    """
    try:
        from shared.market_data.webull_quotes import get_quotes_client
        client = get_quotes_client()
        if client is None:
            return None
    except Exception:  # noqa: BLE001
        return None

    def _sync():
        try:
            if (lane or "").lower() == "crypto":
                snap = client.crypto_snapshot(symbol) or {}
            else:
                snap = client.equity_snapshot(symbol) or {}
            p = (
                snap.get("price")
                or snap.get("last_price")
                or snap.get("ask")
            )
            return float(p) if p is not None else None
        except Exception:  # noqa: BLE001
            return None

    try:
        return await asyncio.to_thread(_sync)
    except Exception:  # noqa: BLE001
        return None


# ── outcome_join mirror ───────────────────────────────────────────


async def _mirror_to_outcome_join(bracket_doc: dict) -> None:
    """Write the resolved label into `doctrine_sidecars.outcome_join`
    for the original intent_id. Downstream consumers (auto_retire,
    scorecard, wrapper-dampener telemetry) already key on this field —
    they inherit the cleaner label for free.

    Idempotent: uses an upsert keyed on `intent_id` so a re-resolved
    bracket can overwrite a stale outcome_join entry without
    duplication.
    """
    intent_id = bracket_doc.get("intent_id")
    if not intent_id:
        return
    outcome_join = {
        "joined_at": bracket_doc["resolved_at"],
        "outcome_label": bracket_doc["outcome_label"],
        "pnl_usd": float(bracket_doc.get("pnl_usd") or 0.0),
        "resolved_via": "bracket_outcome_resolver",
        "bracket_id": bracket_doc["bracket_id"],
        "entry_price": bracket_doc["entry_price"],
        "resolved_price": bracket_doc["resolved_price"],
    }
    try:
        await db[DOCTRINE_SIDECARS].update_one(
            {"intent_id": intent_id},
            {"$set": {"outcome_join": outcome_join}},
            upsert=False,  # only update existing sidecar rows
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "outcome_join mirror failed intent_id=%s: %s", intent_id, e,
        )


# ── one tick of the resolver loop ─────────────────────────────────


async def resolve_open_brackets_once(
    price_fetcher=_fetch_last_price,
) -> dict:
    """Run one resolution pass over all open brackets. Returns a
    summary dict so the test suite + the diagnostics endpoint can
    observe progress.

    `price_fetcher` is injectable for tests (the default hits the
    Webull quotes client).
    """
    cursor = db[WEBULL_BRACKET_INTENTS].find({"status": "open"}, {"_id": 0})
    open_brackets = await cursor.to_list(length=500)

    now = datetime.now(timezone.utc)
    counts = {"tp_hit": 0, "sl_hit": 0, "timeout": 0, "skipped": 0, "kept_open": 0}

    for bracket in open_brackets:
        try:
            expires_at = datetime.fromisoformat(bracket["expires_at"])
            expired = now >= expires_at
        except Exception:  # noqa: BLE001
            expired = True  # bad timestamp = treat as expired, fail closed

        last_price = await price_fetcher(
            bracket["symbol"], bracket.get("lane") or "equity",
        )
        if last_price is None or last_price <= 0:
            counts["skipped"] += 1
            continue

        label = _resolve_outcome(
            side=bracket["side"],
            last_price=last_price,
            target=float(bracket["target_price"]),
            stop=float(bracket["stop_price"]),
            expired=expired,
        )
        if label is None:
            counts["kept_open"] += 1
            continue

        pnl = _pnl_for_resolution(
            side=bracket["side"],
            qty=float(bracket.get("qty") or 0.0),
            entry=float(bracket["entry_price"]),
            resolved_at_price=last_price,
        )

        update = {
            "status": "resolved",
            "outcome_label": label,
            "resolved_at": now.isoformat(),
            "resolved_price": last_price,
            "pnl_usd": pnl,
        }
        try:
            await db[WEBULL_BRACKET_INTENTS].update_one(
                {"bracket_id": bracket["bracket_id"], "status": "open"},
                {"$set": update},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "bracket resolve write failed bracket_id=%s: %s",
                bracket["bracket_id"], e,
            )
            counts["skipped"] += 1
            continue

        await _mirror_to_outcome_join({**bracket, **update})
        counts[label] += 1
        logger.info(
            "bracket resolved bracket_id=%s symbol=%s side=%s "
            "label=%s last=%.4f target=%.4f stop=%.4f pnl=%.4f",
            bracket["bracket_id"], bracket["symbol"], bracket["side"],
            label, last_price,
            float(bracket["target_price"]), float(bracket["stop_price"]),
            pnl,
        )

    return counts


# ── long-running task ─────────────────────────────────────────────


_resolver_task: Optional[asyncio.Task[Any]] = None


async def _resolver_loop() -> None:
    """The persistent loop. Awaits the master kill-switch flip
    between ticks so an operator can disable + re-enable without
    restarting MC."""
    logger.info(
        "bracket_outcome_resolver started (cadence=%ss, enabled=%s)",
        RESOLVER_INTERVAL_SEC, bracket_outcomes_enabled(),
    )
    while True:
        # Re-read the env on every tick so the operator can flip the
        # kill-switch live.
        if not bracket_outcomes_enabled():
            await asyncio.sleep(RESOLVER_INTERVAL_SEC)
            continue
        try:
            await asyncio.wait_for(
                resolve_open_brackets_once(),
                timeout=RESOLVER_TICK_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "bracket_outcome_resolver tick exceeded %ss — abandoning",
                RESOLVER_TICK_TIMEOUT_SEC,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("bracket_outcome_resolver tick error: %s", e)
        # Jittered sleep so multi-pod deployments don't synchronize.
        await asyncio.sleep(
            RESOLVER_INTERVAL_SEC + random.uniform(-3, 3),
        )


def start_resolver_task() -> None:
    """Spawn the background resolver task. Called from `server.py`
    startup. Idempotent — calling twice is a no-op."""
    global _resolver_task
    if _resolver_task is not None and not _resolver_task.done():
        return
    _resolver_task = asyncio.create_task(_resolver_loop())


def get_resolver_task() -> Optional[asyncio.Task[Any]]:
    """Expose the task for tests / health checks."""
    return _resolver_task
