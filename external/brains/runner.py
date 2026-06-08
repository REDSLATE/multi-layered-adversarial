"""Neutral-brain sidecar runner — runs INSIDE MC's Python process.

Each `BrainRunner` instance:
  1. Pulls the operator-owned symbol universe (both equity + crypto).
  2. Per tick, picks ONE (symbol, lane) round-robin across both lanes.
  3. Hits MC's `/api/runtime-discussion/technical/{symbol}` (runtime-
     token auth, loopback) to get bars + an MC-computed
     `setup_score` for the BASE-BREAKOUT pattern (operator-confirmed
     strategy, 2026-05-27).
  4. Synthesizes the brain's snapshot from the technical response
     (real bid/ask/spread when available; falls back to deterministic
     stubs only on cold-start).
  5. Asks `CaminoAdversarialBrain.evaluate` for a ranked verdict —
     the BUY hypothesis is biased UP by setup_score when the pattern
     is hot. The pattern is descriptive evidence, NOT a gate.
  6. POSTs to `/api/intents` over loopback.
  7. Pings `/api/admin/runtime/sidecar-checkin/<brain>` so MC's
     composite_liveness stays green.

Operator switches (all env-driven, no code change to flip):
  NEUTRAL_BRAINS_ENABLED       on/off master gate
  NEUTRAL_BRAINS_SHADOW_ONLY   when "false", brains report size>0
                               and MC's Phase 4 ladder authorizes fills
                               IF the ladder stage is micro_live+ and
                               the lane execution toggle is on. Defense
                               in depth — flipping this alone does NOT
                               fire orders.
  NEUTRAL_BRAINS_LANES         comma list ("equity,crypto"; default "both")
  NEUTRAL_BRAIN_TICK_SEC       per-brain tick cadence (default 45s)
  NEUTRAL_BRAIN_CHECKIN_SEC    per-brain heartbeat cadence (default 30s)
  NEUTRAL_BRAIN_PATTERN_BIAS   scalar 0..1 (default 0.20) — how strongly
                               setup_score lifts the BUY hypothesis

Doctrine pins:
  * Brains hold NO seat. Seat is operator-rotatable via /admin/roster.
  * Phase 4 ladder is the sizing authority — brain sizing is advisory.
  * Pattern setup_score is DESCRIPTIVE evidence — never a gate, never
    forces an action.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Optional

import httpx

from .brain_core import BrainIntent, CaminoAdversarialBrain


logger = logging.getLogger("risedual.neutral_brains")


# ── Brain identity map. INTERNAL slot ids match MC's existing 4
# runtime slots (no test churn); DISPLAY NAME is the car-name the
# operator wants surfaced in the dashboard.
BRAIN_ROSTER = [
    ("alpha",    "Camino",    "ALPHA_INGEST_TOKEN"),
    ("camaro",   "Barracuda", "CAMARO_INGEST_TOKEN"),
    ("chevelle", "Hellcat",   "CHEVELLE_INGEST_TOKEN"),
    ("redeye",   "GTO",       "REDEYE_INGEST_TOKEN"),
]


# ── Tick + cadence settings ──
MC_LOOPBACK_URL = "http://127.0.0.1:8001"
TICK_INTERVAL_SEC = float(os.environ.get("NEUTRAL_BRAIN_TICK_SEC", "45"))
CHECKIN_INTERVAL_SEC = float(os.environ.get("NEUTRAL_BRAIN_CHECKIN_SEC", "30"))
HTTP_TIMEOUT_SEC = float(os.environ.get("NEUTRAL_BRAIN_HTTP_TIMEOUT_SEC", "8"))
PATTERN_BIAS = float(os.environ.get("NEUTRAL_BRAIN_PATTERN_BIAS", "0.20"))


def _shadow_only_default() -> bool:
    """Read at runner-construction time. Operator flips to false when
    they want the brains to claim sizing > 0. Phase 4 ladder is still
    authoritative on whether the order actually fires."""
    return os.environ.get(
        "NEUTRAL_BRAINS_SHADOW_ONLY", "true",
    ).strip().lower() in ("true", "1", "yes", "on")


def _enabled_lanes() -> list[str]:
    """Operator-tunable lane filter."""
    raw = (os.environ.get("NEUTRAL_BRAINS_LANES") or "both").strip().lower()
    if raw == "both" or raw == "":
        return ["equity", "crypto"]
    return [x.strip() for x in raw.split(",") if x.strip() in ("equity", "crypto")]


# Fallback universe when MC's `/patterns/universe-public` is empty.
FALLBACK_BY_LANE = {
    "crypto": ["BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD"],
    "equity": ["AAPL", "MSFT", "NVDA", "TSLA"],
}


def _checkin_stamp(brain_id: str, display_name: str) -> dict:
    env_name = os.environ.get("BRAIN_ENV_NAME", "prod")
    mc_url = os.environ.get(
        "BRAIN_ADVERTISED_MC_URL", "https://mission.risedual.ai",
    )
    return {
        "stamp": {
            "app_name": "risedual",
            "env_name": env_name,
            "git_sha": os.environ.get("BRAIN_GIT_SHA", "neutral-camino-v1"),
            "platform": "emergent",
            "mc_url": mc_url,
            "db_name": os.environ.get("DB_NAME", ""),
            "broker_mode": "paper",
            "sidecar_room": brain_id,
            "sidecar_version": "neutral-camino-v1",
            "policy_hash": "neutral-template",
            "local_execution_authority": False,
            "display_name": display_name,
            "timestamp_ms": int(time.time() * 1000),
        }
    }


def _build_snapshot(
    symbol: str, lane: str, technical: Optional[dict],
) -> tuple[dict, float]:
    """Build the brain's snapshot from MC's technical response.

    Returns `(snapshot, setup_score)`. `setup_score` ∈ [0, 1] is the
    BASE-BREAKOUT pattern composite — used by the runner to bias the
    BUY hypothesis. Pure descriptive evidence; never a gate.
    """
    setup_score = 0.0
    last_close = None
    if technical:
        bars = technical.get("bars") or []
        signals = technical.get("signals") or technical.get("pattern_signals") or {}
        setup_score = float(signals.get("setup_score") or 0.0)
        last_close = technical.get("last_close")
        if last_close is None and bars:
            try:
                last_close = float(bars[-1].get("c") or 0)
            except Exception:  # noqa: BLE001
                last_close = None
        # Derive trend / volume / volatility from recent bars when
        # available; fall back to neutral defaults so an empty bars
        # array doesn't crash the brain.
        if len(bars) >= 20:
            closes = [float(b.get("c") or 0) for b in bars[-20:]]
            vols = [float(b.get("v") or 0) for b in bars[-20:]]
            window_high = max(closes); window_low = min(closes) or 1.0
            volatility = (window_high - window_low) / window_low
            trend_score = (closes[-1] - closes[0]) / (closes[0] or 1.0)
            avg_vol = sum(vols) / max(len(vols), 1)
            recent_vol = sum(vols[-3:]) / 3
            vol_change_pct = (
                ((recent_vol - avg_vol) / avg_vol * 100.0) if avg_vol else 0.0
            )
            # spread_bps from snapshot if MC included it; else mild default
            # for crypto on Kraken (≈8 bps) / equity on NYSE (≈3 bps).
            spread_bps = float(
                (technical.get("snapshot") or {}).get("spread_bps")
                or (8.0 if lane == "crypto" else 3.0)
            )
            snapshot = {
                "symbol": symbol,
                "price": last_close,
                "price_change_pct": round(trend_score * 100, 3),
                "volume_change_pct": round(vol_change_pct, 2),
                "rsi": 50.0,  # MC doesn't surface RSI on this endpoint
                "spread_bps": round(spread_bps, 2),
                "volatility": round(min(1.0, max(0.0, volatility * 3)), 3),
                "trend_score": round(max(-1.0, min(1.0, trend_score * 8)), 3),
                "liquidity_score": 0.85,
                "market_regime": "calm",
                "setup_score": round(setup_score, 4),
                "pattern": "base_breakout",
                "real_market_data": True,
            }
            return snapshot, setup_score

    # Cold-start fallback (MC has no bars for this symbol yet).
    seed = hash(symbol) ^ (int(time.time()) // 300)
    rng = random.Random(seed)
    base = {
        "BTC/USD": 68000, "ETH/USD": 3400, "SOL/USD": 145, "ADA/USD": 0.45,
        "AAPL": 195, "MSFT": 420, "NVDA": 140, "TSLA": 250,
    }.get(symbol, last_close or 100.0)
    drift = rng.uniform(-2.5, 2.5)
    spread_bps = 8.0 if lane == "crypto" else 3.0
    return {
        "symbol": symbol,
        "price": round(base * (1 + drift / 100), 4),
        "price_change_pct": round(drift, 3),
        "volume_change_pct": round(rng.uniform(-30, 60), 2),
        "rsi": round(rng.uniform(28, 72), 1),
        "spread_bps": round(spread_bps + rng.uniform(0, 5), 2),
        "volatility": round(rng.uniform(0.1, 0.7), 3),
        "trend_score": round(rng.uniform(-0.85, 0.85), 3),
        "liquidity_score": round(rng.uniform(0.5, 0.95), 3),
        "market_regime": "calm",
        "setup_score": 0.0,
        "pattern": "cold_start_stub",
        "real_market_data": False,
    }, 0.0


def _apply_pattern_bias(intent: BrainIntent, setup_score: float) -> BrainIntent:
    """Lift the BUY hypothesis score by `PATTERN_BIAS * setup_score`
    if the pattern is hot. If BUY then wins by enough, the intent's
    final action flips to BUY. Pure descriptive bias — never blocks.
    """
    if setup_score < 0.30:
        return intent
    buy = intent.hypothesis_scores.get("hypothesis_buy", 0.0)
    lifted_buy = min(1.0, buy + PATTERN_BIAS * setup_score)
    intent.hypothesis_scores["hypothesis_buy"] = round(lifted_buy, 4)
    intent.reasoning.append(
        f"pattern_bias: base_breakout setup_score={setup_score:.3f} → "
        f"BUY {buy:.3f} -> {lifted_buy:.3f}",
    )
    # If the lifted BUY now beats the original winner by enough to
    # cross the brain's `min_gap`, promote to BUY.
    other_max = max(
        v for k, v in intent.hypothesis_scores.items()
        if k != "hypothesis_buy"
    )
    if lifted_buy > other_max + 0.06 and lifted_buy >= 0.58:
        intent.action = "BUY"
        intent.confidence = round(lifted_buy, 4)
        intent.reasoning.append("pattern_bias: action promoted to BUY")
    return intent


def _intent_to_mc_payload(intent: BrainIntent) -> dict:
    mc_action = "HOLD" if intent.action == "OBSERVE" else intent.action
    rationale = " | ".join(intent.reasoning)[:4000]
    return {
        "stack": intent.brain_id,
        "action": mc_action,
        "symbol": intent.symbol,
        "lane": intent.lane,
        "confidence": intent.confidence,
        "rationale": rationale,
        "doctrine_snapshot": {
            **intent.snapshot,
            "display_name": intent.display_name,
        },
        "evidence": {
            "raw_confidence": intent.confidence,
            "size_multiplier": intent.size,
            "would_trade_without_gates": (
                bool(intent.size > 0) and not intent.shadow_only
            ),
            "shadow_only": intent.shadow_only,
            "neutral_template_version": "camino-v1",
            "memory_tags": intent.memory_tags,
            "hypothesis_scores": intent.hypothesis_scores,
            "display_name": intent.display_name,
            "setup_score": intent.snapshot.get("setup_score", 0.0),
            "pattern": intent.snapshot.get("pattern"),
        },
    }


# ── Per-brain runner ───────────────────────────────────────────
class BrainRunner:
    """One async task per brain. Owns the lifecycle of one slot.

    A brain spans BOTH lanes — each tick it round-robins through the
    combined (lane, symbol) universe. Phase 4 ladder is the sizing
    authority per (brain, lane) row, so the same brain can be
    micro_paper on crypto and observation_only on equity (or vice
    versa) without changing this runner.
    """

    def __init__(self, brain_id: str, display_name: str, token: str):
        self.brain_id = brain_id
        self.display_name = display_name
        self.token = token
        self._stop = asyncio.Event()
        self._tick_count = 0
        self._intent_count = 0
        self._checkin_count = 0
        self._universe: list[tuple[str, str]] = []
        # One brain core per (display_name, lane) so per-lane state
        # (shadow_only flag, min_commitment) can diverge later. Today
        # all lanes share defaults but the layout supports divergence.
        shadow_only = _shadow_only_default()
        self._cores = {
            "crypto": CaminoAdversarialBrain(
                brain_id=brain_id, display_name=display_name,
                lane="crypto", shadow_only=shadow_only,
                min_commitment=0.58, min_gap=0.06,
                max_shadow_size=1.0 if not shadow_only else 0.0,
            ),
            "equity": CaminoAdversarialBrain(
                brain_id=brain_id, display_name=display_name,
                lane="equity", shadow_only=shadow_only,
                min_commitment=0.58, min_gap=0.06,
                max_shadow_size=1.0 if not shadow_only else 0.0,
            ),
        }

    async def run(self) -> None:
        logger.info(
            "neutral_brain start brain_id=%s display=%s lanes=%s shadow_only=%s",
            self.brain_id, self.display_name, _enabled_lanes(),
            _shadow_only_default(),
        )
        await asyncio.sleep(random.uniform(0.5, 4.0))
        intent_task = asyncio.create_task(self._intent_loop())
        checkin_task = asyncio.create_task(self._checkin_loop())
        try:
            await self._stop.wait()
        finally:
            for t in (intent_task, checkin_task):
                t.cancel()
            for t in (intent_task, checkin_task):
                try: await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self._stop.set()

    @property
    def stats(self) -> dict:
        return {
            "brain_id": self.brain_id,
            "display_name": self.display_name,
            "lanes": _enabled_lanes(),
            "shadow_only": _shadow_only_default(),
            "tick_count": self._tick_count,
            "intent_count": self._intent_count,
            "checkin_count": self._checkin_count,
            "universe_size": len(self._universe),
        }

    async def _resolve_universe(self, http: httpx.AsyncClient) -> list[tuple[str, str]]:
        """Return [(lane, symbol), ...] from MC's operator-curated universe."""
        out: list[tuple[str, str]] = []
        lanes_on = _enabled_lanes()
        try:
            r = await http.get(
                f"{MC_LOOPBACK_URL}/api/admin/patterns/universe-public",
                timeout=5,
            )
            if r.status_code == 200:
                for x in (r.json() or {}).get("items") or []:
                    if x.get("active") is False:
                        continue
                    lane = (x.get("lane") or "").lower()
                    sym = x.get("symbol") or ""
                    if lane in lanes_on and sym:
                        out.append((lane, sym))
        except Exception as e:  # noqa: BLE001
            logger.warning("universe lookup failed brain=%s: %s", self.brain_id, e)
        if not out:
            for lane in lanes_on:
                for s in FALLBACK_BY_LANE.get(lane, []):
                    out.append((lane, s))
        return out

    async def _fetch_technical(
        self, http: httpx.AsyncClient, symbol: str,
    ) -> Optional[dict]:
        """Hit MC's runtime technical endpoint (loopback, runtime-token auth)."""
        try:
            r = await http.get(
                f"{MC_LOOPBACK_URL}/api/runtime-discussion/technical/{symbol}",
                params={"caller": self.brain_id, "tf": "1h", "bars": 200},
                headers={"X-Runtime-Token": self.token},
            )
            if r.status_code == 200:
                return r.json()
            logger.debug(
                "technical fetch non-200 brain=%s sym=%s status=%s",
                self.brain_id, symbol, r.status_code,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "technical fetch error brain=%s sym=%s: %s",
                self.brain_id, symbol, e,
            )
        return None

    async def _intent_loop(self) -> None:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as http:
            self._universe = await self._resolve_universe(http)
            refresh_every = 20  # re-pull universe every N ticks
            while not self._stop.is_set():
                self._tick_count += 1
                if self._tick_count % refresh_every == 0:
                    self._universe = await self._resolve_universe(http)
                if not self._universe:
                    await asyncio.sleep(TICK_INTERVAL_SEC)
                    continue
                lane, symbol = self._universe[
                    (self._tick_count - 1) % len(self._universe)
                ]
                try:
                    await self._evaluate_and_post(http, lane, symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "intent_loop error brain=%s sym=%s: %s",
                        self.brain_id, symbol, e,
                    )
                await asyncio.sleep(TICK_INTERVAL_SEC + random.uniform(-3, 3))

    async def _evaluate_and_post(
        self, http: httpx.AsyncClient, lane: str, symbol: str,
    ) -> None:
        technical = await self._fetch_technical(http, symbol)
        snapshot, setup_score = _build_snapshot(symbol, lane, technical)
        core = self._cores[lane]
        intent = core.evaluate(symbol, snapshot)
        intent.lane = lane
        intent = _apply_pattern_bias(intent, setup_score)
        payload = _intent_to_mc_payload(intent)
        r = await http.post(
            f"{MC_LOOPBACK_URL}/api/intents",
            json=payload,
            headers={"X-Runtime-Token": self.token},
        )
        if r.status_code // 100 == 2:
            self._intent_count += 1
            if self._intent_count % 10 == 1:
                logger.info(
                    "neutral_brain intent posted brain=%s display=%s "
                    "lane=%s sym=%s action=%s conf=%.2f setup=%.2f total=%d",
                    self.brain_id, self.display_name, lane, symbol,
                    intent.action, intent.confidence, setup_score,
                    self._intent_count,
                )
        else:
            logger.warning(
                "neutral_brain intent rejected brain=%s lane=%s status=%s body=%s",
                self.brain_id, lane, r.status_code, r.text[:300],
            )

    async def _checkin_loop(self) -> None:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as http:
            while not self._stop.is_set():
                try:
                    r = await http.post(
                        f"{MC_LOOPBACK_URL}/api/admin/runtime/"
                        f"sidecar-checkin/{self.brain_id}",
                        json=_checkin_stamp(self.brain_id, self.display_name),
                        headers={"X-Runtime-Token": self.token},
                    )
                    if r.status_code // 100 == 2:
                        self._checkin_count += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("checkin error brain=%s: %s", self.brain_id, e)
                await asyncio.sleep(CHECKIN_INTERVAL_SEC + random.uniform(-2, 2))


# ── module-level manager ─────────────────────────
_RUNNERS: list[BrainRunner] = []
_TASKS: list[asyncio.Task] = []


def is_enabled() -> bool:
    return os.environ.get("NEUTRAL_BRAINS_ENABLED", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )


async def start_neutral_brains() -> None:
    if not is_enabled():
        logger.info("neutral_brains disabled (set NEUTRAL_BRAINS_ENABLED=true)")
        return
    if _RUNNERS:
        logger.info("neutral_brains already started — skipping")
        return
    for brain_id, display_name, token_env in BRAIN_ROSTER:
        token = os.environ.get(token_env, "")
        if not token:
            logger.warning(
                "neutral_brains: %s has no %s set — skipping",
                brain_id, token_env,
            )
            continue
        runner = BrainRunner(
            brain_id=brain_id, display_name=display_name, token=token,
        )
        _RUNNERS.append(runner)
        _TASKS.append(asyncio.create_task(
            runner.run(), name=f"neutral_brain_{brain_id}",
        ))
    logger.info(
        "neutral_brains started: %d runners lanes=%s shadow_only=%s — %s",
        len(_RUNNERS), _enabled_lanes(), _shadow_only_default(),
        ", ".join(f"{r.brain_id}={r.display_name}" for r in _RUNNERS),
    )


async def stop_neutral_brains() -> None:
    for r in _RUNNERS:
        r.stop()
    for t in _TASKS:
        try: await asyncio.wait_for(t, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _RUNNERS.clear()
    _TASKS.clear()


def runtime_stats() -> list[dict]:
    return [r.stats for r in _RUNNERS]
