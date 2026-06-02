"""Reference brain-side universe client (2026-02-19).

Drop this file into your brain repo (alpha / camaro / chevelle /
redeye) and import `BrainUniverseClient` from your strategist loop.

## Why this exists

MC enforces a `symbol_in_universe` gate (`shared/execution.py`).
Every intent's `symbol` MUST be in MC's `patterns_universe` collection
with a `lane` that matches the intent's lane. If your strategist
proposes off-universe symbols (e.g., NVDA when MC only has crypto
pairs active), those intents are rejected at MC's gate chain and
never reach the broker.

This client makes your brain compliant by:
  1. Pulling the lane-filtered universe from MC on a schedule
  2. Caching it in memory with a "last-known-good" fallback
  3. Exposing a simple `allowed_symbols()` API for the strategist
  4. Failing SAFELY when MC is unreachable (use cache, don't propose
     symbols that haven't been confirmed in the last N hours)

## Contract

`GET /api/admin/runtime/{brain}/universe`
  Headers:
    X-Brain-Id: <your brain id>
    X-Runtime-Token: <your per-brain runtime token>
  Response:
    {
      "brain": "camaro",
      "lanes": ["equity", "crypto"],
      "symbols": [
        {"symbol": "BTC/USD", "lane": "crypto"},
        {"symbol": "NVDA",    "lane": "equity"},
        ...
      ],
      "count": int,
      "served_at": iso8601,
    }

## Usage in your strategist

    client = BrainUniverseClient(
        brain_id="camaro",
        mc_base_url=os.environ["MC_BASE_URL"],
        runtime_token=os.environ["CAMARO_RUNTIME_TOKEN"],
    )
    await client.start()  # spawns the refresh task

    # In your strategist loop:
    universe = client.allowed_symbols(lane="crypto")
    for symbol in universe:
        if my_strategist_likes(symbol):
            await emit_intent(symbol=symbol, lane="crypto", ...)

## Doctrine

- Brains MUST consult MC's universe. The only valid reason to use
  a stale cache is an MC failure (5xx, timeout, connection error).
- If the cache is stale > MAX_CACHE_AGE_SEC AND MC is unreachable,
  the strategist should enter "no new intents" safe mode rather
  than propose against possibly-deprecated symbols.
- This client is READ-ONLY. It never writes to MC. It is purely
  a constraint reader.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx


logger = logging.getLogger("brain.universe_client")


# How long the cache is considered fresh enough to use.
# Above this age, AND if MC is unreachable, the strategist should
# refuse to propose new intents until MC is reachable again.
MAX_CACHE_AGE_SEC = 6 * 60 * 60  # 6 hours

# How often we refresh the cache on the happy path.
REFRESH_INTERVAL_SEC = 5 * 60  # 5 minutes

# Network timeout per refresh attempt.
REQUEST_TIMEOUT_SEC = 10.0


@dataclass
class _CacheEntry:
    symbols_by_lane: dict[str, set[str]] = field(default_factory=dict)
    lanes_held: list[str] = field(default_factory=list)
    served_at_epoch: float = 0.0
    last_success_epoch: float = 0.0
    last_error: Optional[str] = None
    last_attempt_epoch: float = 0.0
    fetch_count: int = 0
    error_count: int = 0

    @property
    def age_sec(self) -> float:
        if self.last_success_epoch == 0.0:
            return float("inf")
        return time.time() - self.last_success_epoch

    @property
    def is_fresh(self) -> bool:
        return self.age_sec < MAX_CACHE_AGE_SEC

    @property
    def has_data(self) -> bool:
        return self.last_success_epoch > 0.0


class BrainUniverseClient:
    """Pulls MC's universe on a schedule, caches it locally, and
    exposes a query API to the strategist loop.

    Thread/asyncio-safe for read; only one refresh task runs at a
    time. Strategist loop should call `allowed_symbols(...)`
    synchronously — it reads from the in-memory cache and never
    blocks on network.
    """

    def __init__(
        self,
        brain_id: str,
        mc_base_url: str,
        runtime_token: str,
        refresh_interval_sec: float = REFRESH_INTERVAL_SEC,
        timeout_sec: float = REQUEST_TIMEOUT_SEC,
    ):
        self.brain_id = brain_id.lower().strip()
        self.mc_base_url = mc_base_url.rstrip("/")
        self._runtime_token = runtime_token
        self._refresh_interval_sec = refresh_interval_sec
        self._timeout_sec = timeout_sec
        self._cache = _CacheEntry()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    # ─── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the refresh loop. Calling start() twice is a no-op
        (we keep the existing task)."""
        if self._task is not None and not self._task.done():
            return
        # Fetch once synchronously so the cache is warm before the
        # strategist loop spins up.
        await self._refresh_once()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="universe_refresh")

    async def stop(self) -> None:
        """Signal the refresh loop to exit. Safe to call multiple times."""
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    # ─── public read API ──────────────────────────────────────────────

    def allowed_symbols(self, lane: Optional[str] = None) -> set[str]:
        """Return the set of symbols the brain may propose against.

        Args:
            lane: optional lane filter ("equity" | "crypto"). If None,
                returns the union across all lanes the brain holds.

        Returns:
            Set of symbols. Empty set means either:
              - Brain holds no seats in the requested lane, OR
              - Cache is unwarmed AND MC was never reachable, OR
              - Cache is stale (> MAX_CACHE_AGE_SEC) AND MC is down

            The strategist MUST treat an empty set as "do not emit any
            new intents" — it's the safe-mode signal.
        """
        if not self._cache.has_data:
            return set()
        if not self._cache.is_fresh:
            logger.warning(
                "universe cache is stale (age=%ds, max=%ds) and MC has "
                "not refreshed it; strategist should enter safe mode",
                int(self._cache.age_sec), MAX_CACHE_AGE_SEC,
            )
            return set()
        if lane is None:
            out: set[str] = set()
            for v in self._cache.symbols_by_lane.values():
                out.update(v)
            return out
        lane_norm = lane.lower().strip()
        return set(self._cache.symbols_by_lane.get(lane_norm, set()))

    def has_lane(self, lane: str) -> bool:
        """True if the brain currently holds a seat in this lane
        (per MC's roster)."""
        return lane.lower().strip() in self._cache.lanes_held

    def status(self) -> dict:
        """Operator-visible status snapshot. Surface this on your
        brain's /status endpoint so operators can see whether the
        brain is in compliance with MC's universe."""
        return {
            "brain_id": self.brain_id,
            "mc_base_url": self.mc_base_url,
            "lanes_held": list(self._cache.lanes_held),
            "symbol_counts_by_lane": {
                k: len(v) for k, v in self._cache.symbols_by_lane.items()
            },
            "cache_age_sec": (
                round(self._cache.age_sec, 1)
                if self._cache.has_data else None
            ),
            "cache_is_fresh": self._cache.is_fresh,
            "last_success_epoch": self._cache.last_success_epoch,
            "last_attempt_epoch": self._cache.last_attempt_epoch,
            "last_error": self._cache.last_error,
            "fetch_count": self._cache.fetch_count,
            "error_count": self._cache.error_count,
            "doctrine": "brain_universe_client_v1",
        }

    # ─── internals ────────────────────────────────────────────────────

    async def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._refresh_interval_sec,
                    )
                    return  # stop_event set
                except asyncio.TimeoutError:
                    pass  # normal — time to refresh
                await self._refresh_once()
        except asyncio.CancelledError:
            return

    async def _refresh_once(self) -> None:
        self._cache.last_attempt_epoch = time.time()
        url = (
            f"{self.mc_base_url}"
            f"/api/admin/runtime/{self.brain_id}/universe"
        )
        headers = {
            "X-Brain-Id": self.brain_id,
            "X-Runtime-Token": self._runtime_token,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                self._cache.last_error = (
                    f"mc_returned_{resp.status_code}: {resp.text[:200]}"
                )
                self._cache.error_count += 1
                logger.warning(
                    "universe refresh failed: %s", self._cache.last_error,
                )
                return
            payload = resp.json()
        except httpx.TimeoutException:
            self._cache.last_error = "mc_timeout"
            self._cache.error_count += 1
            logger.warning("universe refresh timed out after %ss", self._timeout_sec)
            return
        except httpx.ConnectError as exc:
            self._cache.last_error = f"mc_connect_failed:{type(exc).__name__}"
            self._cache.error_count += 1
            logger.warning("universe refresh connect failed: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._cache.last_error = f"mc_unexpected:{type(exc).__name__}"
            self._cache.error_count += 1
            logger.warning("universe refresh unexpected error: %s", exc)
            return

        # Success — replace the cache atomically.
        new_by_lane: dict[str, set[str]] = {}
        for row in payload.get("symbols", []):
            lane = (row.get("lane") or "equity").lower()
            sym = row.get("symbol")
            if sym:
                new_by_lane.setdefault(lane, set()).add(sym)
        self._cache.symbols_by_lane = new_by_lane
        self._cache.lanes_held = list(payload.get("lanes") or [])
        self._cache.served_at_epoch = time.time()
        self._cache.last_success_epoch = time.time()
        self._cache.last_error = None
        self._cache.fetch_count += 1
        logger.info(
            "universe refreshed: brain=%s lanes=%s counts=%s",
            self.brain_id,
            self._cache.lanes_held,
            {k: len(v) for k, v in new_by_lane.items()},
        )
