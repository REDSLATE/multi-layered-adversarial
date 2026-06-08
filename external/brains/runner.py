"""Neutral-brain sidecar runner — runs INSIDE MC's Python process.

Each `BrainRunner` instance:
  1. Synthesizes a market snapshot for each universe symbol on its lane.
  2. Asks `CaminoAdversarialBrain` to evaluate.
  3. Translates the BrainIntent → MC's `/api/intents` schema.
  4. POSTs to http://127.0.0.1:8001 over loopback (no internet hop).
  5. Pings `/api/admin/runtime/sidecar-checkin/<brain>` so MC's
     composite_liveness flips to LIVE for the slot.

The 4 BrainRunners are spawned by `manager.start_neutral_brains()`
from MC's FastAPI lifespan startup hook. Gated by env
`NEUTRAL_BRAINS_ENABLED=true` so the brains stay off until the
operator flips them on (default: off).

Doctrine pins:
  * Brains hold NO seat. Seat assignment lives in MC's roster and is
    operator-rotatable. The runner does not consult the roster — it
    just emits intents. MC's gate chain decides what authority each
    intent inherits based on whichever seat the brain currently holds.
  * Brain intents are SHADOW-ONLY by default (size=0). The ladder
    stage at MC's side decides whether anything fires. Phase 4
    sizing gate is the authority — the brain just proposes.
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


# ── Brain identity map (operator-locked). The INTERNAL `brain_id`
# matches MC's existing 4 runtime slots (so all 126 production refs
# and 698 test refs keep working untouched). The DISPLAY NAME is the
# car-name the operator wants surfaced in the dashboard. ──
BRAIN_ROSTER = [
    # (internal_id, display_name, ingest_token_env)
    ("alpha",    "Camino",    "ALPHA_INGEST_TOKEN"),
    ("camaro",   "Barracuda", "CAMARO_INGEST_TOKEN"),
    ("chevelle", "Hellcat",   "CHEVELLE_INGEST_TOKEN"),
    ("redeye",   "GTO",       "REDEYE_INGEST_TOKEN"),
]


# ── Tick + cadence settings (operator-tunable via env) ──
MC_LOOPBACK_URL = "http://127.0.0.1:8001"
TICK_INTERVAL_SEC = float(os.environ.get("NEUTRAL_BRAIN_TICK_SEC", "45"))
CHECKIN_INTERVAL_SEC = float(os.environ.get("NEUTRAL_BRAIN_CHECKIN_SEC", "30"))
# Hard limit on the snapshot tick — if MC blows up evaluating an
# intent we don't want the brain task to wedge for minutes.
HTTP_TIMEOUT_SEC = float(os.environ.get("NEUTRAL_BRAIN_HTTP_TIMEOUT_SEC", "8"))


# Default universe symbols if MC doesn't return any. Crypto-only —
# matches the strict-crypto pivot from earlier this session.
FALLBACK_CRYPTO_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD",
]


# ── Snapshot synthesizer ────────────────────────────────────────
def _synthesize_snapshot(symbol: str) -> dict:
    """Generate a realistic-looking market snapshot for the brain.

    This is a STUB — it does not query any real price feed. The
    intent is to exercise the wire, not to make money. When the real
    per-brain wild_adaptive_core arrives, it will replace this with
    actual market-data ingestion.

    Snapshot keys match what `CaminoAdversarialBrain._build_hypotheses`
    expects. `spread_bps` and `price` are also required by MC's
    doctrine_snapshot gates downstream.
    """
    # Symbol-seeded so successive ticks for the same symbol drift
    # smoothly rather than thrashing.
    seed = hash(symbol) ^ (int(time.time()) // 300)  # changes every 5 min
    rng = random.Random(seed)

    base = {
        "BTC/USD": 68000, "ETH/USD": 3400, "SOL/USD": 145,
        "ADA/USD": 0.45,
    }.get(symbol, 100.0)
    drift_pct = rng.uniform(-2.5, 2.5)
    price = base * (1 + drift_pct / 100.0)
    spread_bps = rng.uniform(2.0, 18.0)
    return {
        "symbol": symbol,
        "price": round(price, 4),
        "price_change_pct": round(drift_pct, 3),
        "volume_change_pct": round(rng.uniform(-30.0, 60.0), 2),
        "rsi": round(rng.uniform(28.0, 72.0), 1),
        "spread_bps": round(spread_bps, 2),
        "volatility": round(rng.uniform(0.10, 0.70), 3),
        "trend_score": round(rng.uniform(-0.85, 0.85), 3),
        "liquidity_score": round(rng.uniform(0.50, 0.95), 3),
        # Required by MC's snapshot gates.
        "market_regime": "calm",
        "tick_synth": True,  # stamps "this was a synthetic stub tick"
    }


def _intent_to_mc_payload(intent: BrainIntent) -> dict:
    """Translate the neutral brain's BrainIntent → MC /api/intents."""
    # OBSERVE collapses to HOLD on MC's wire (MC only knows BUY/SELL/HOLD).
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
            # Display-only — MC stores in the snapshot for the
            # operator dashboard to render the car-name.
            "display_name": intent.display_name,
        },
        "evidence": {
            "raw_confidence": intent.confidence,
            # Shadow-only stand-in brains never claim trade authority.
            # MC's Phase 4 ladder gate is the executor of record.
            "size_multiplier": intent.size,
            "would_trade_without_gates": (
                bool(intent.size > 0) and not intent.shadow_only
            ),
            "shadow_only": intent.shadow_only,
            "neutral_template_version": "camino-v1",
            "memory_tags": intent.memory_tags,
            "hypothesis_scores": intent.hypothesis_scores,
            "display_name": intent.display_name,
        },
    }


def _checkin_stamp(brain_id: str, display_name: str) -> dict:
    """Sidecar check-in stamp.

    `local_execution_authority=false` is doctrine-pinned — brains
    never claim execution authority. `env_name` is sourced from
    `BRAIN_ENV_NAME` (defaults to whatever MC itself reports).
    """
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


# ── Per-brain runner ───────────────────────────────────────────
class BrainRunner:
    """One async task per brain. Owns the lifecycle of one slot."""

    def __init__(
        self, brain_id: str, display_name: str, token: str,
        lane: str = "crypto",
    ):
        self.brain_id = brain_id
        self.display_name = display_name
        self.token = token
        self.lane = lane
        self.brain = CaminoAdversarialBrain(
            brain_id=brain_id, display_name=display_name, lane=lane,
            shadow_only=True, min_commitment=0.58, min_gap=0.06,
        )
        self._stop = asyncio.Event()
        self._tick_count = 0
        self._intent_count = 0
        self._checkin_count = 0

    async def run(self) -> None:
        logger.info(
            "neutral_brain start brain_id=%s display=%s lane=%s",
            self.brain_id, self.display_name, self.lane,
        )
        # Stagger initial start so 4 brains don't fire at the same instant.
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
                except (asyncio.CancelledError, Exception):
                    pass

    def stop(self) -> None:
        self._stop.set()

    @property
    def stats(self) -> dict:
        return {
            "brain_id": self.brain_id,
            "display_name": self.display_name,
            "lane": self.lane,
            "tick_count": self._tick_count,
            "intent_count": self._intent_count,
            "checkin_count": self._checkin_count,
        }

    # ── intent loop ──
    async def _intent_loop(self) -> None:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as http:
            symbols = await self._resolve_universe(http)
            while not self._stop.is_set():
                self._tick_count += 1
                # Pick ONE symbol per tick — round-robin keeps the
                # intent rate at ~1/symbol/tick × universe-size /
                # tick_interval. With 4 symbols × 45s, that's
                # ~1 intent per 11s per brain = ~5 per minute.
                # Plenty of signal for Phase 4 paper learning, not
                # spammy.
                idx = (self._tick_count - 1) % max(1, len(symbols))
                symbol = symbols[idx]
                try:
                    await self._evaluate_and_post(http, symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "neutral_brain intent_loop error brain=%s sym=%s: %s",
                        self.brain_id, symbol, e,
                    )
                # Sleep with jitter so the 4 brains drift apart over time.
                await asyncio.sleep(
                    TICK_INTERVAL_SEC + random.uniform(-3, 3),
                )

    async def _evaluate_and_post(self, http: httpx.AsyncClient, symbol: str) -> None:
        snapshot = _synthesize_snapshot(symbol)
        intent = self.brain.evaluate(symbol, snapshot)
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
                    "neutral_brain intent posted brain=%s display=%s sym=%s "
                    "action=%s conf=%.2f total=%d",
                    self.brain_id, self.display_name, symbol,
                    intent.action, intent.confidence, self._intent_count,
                )
        else:
            logger.warning(
                "neutral_brain intent rejected brain=%s status=%s body=%s",
                self.brain_id, r.status_code, r.text[:300],
            )

    async def _resolve_universe(self, http: httpx.AsyncClient) -> list[str]:
        """Pull operator-owned symbol whitelist from MC; fall back to
        the seeded crypto pairs if MC isn't reachable yet (cold-start)."""
        try:
            r = await http.get(
                f"{MC_LOOPBACK_URL}/api/admin/patterns/universe-public",
                timeout=5,
            )
            if r.status_code == 200:
                items = (r.json() or {}).get("items") or []
                syms = [
                    x["symbol"] for x in items
                    if x.get("active") is not False
                ]
                if syms:
                    # Crypto pairs only — match the strict-crypto pivot.
                    crypto = [s for s in syms if "/" in s] or syms
                    return crypto[:8]
        except Exception:  # noqa: BLE001
            pass
        return list(FALLBACK_CRYPTO_SYMBOLS)

    # ── checkin loop ──
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
                    logger.warning(
                        "neutral_brain checkin error brain=%s: %s",
                        self.brain_id, e,
                    )
                await asyncio.sleep(
                    CHECKIN_INTERVAL_SEC + random.uniform(-2, 2),
                )


# ── module-level manager (singleton) ─────────────────────────
_RUNNERS: list[BrainRunner] = []
_TASKS: list[asyncio.Task] = []


def is_enabled() -> bool:
    return os.environ.get("NEUTRAL_BRAINS_ENABLED", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )


async def start_neutral_brains() -> None:
    """Called from MC's lifespan startup. No-op if env disabled."""
    if not is_enabled():
        logger.info("neutral_brains disabled (set NEUTRAL_BRAINS_ENABLED=true to enable)")
        return
    if _RUNNERS:
        logger.info("neutral_brains already started — skipping")
        return
    for brain_id, display_name, token_env in BRAIN_ROSTER:
        token = os.environ.get(token_env, "")
        if not token:
            logger.warning(
                "neutral_brains: %s has no %s set — skipping that slot",
                brain_id, token_env,
            )
            continue
        runner = BrainRunner(
            brain_id=brain_id, display_name=display_name,
            token=token, lane="crypto",
        )
        _RUNNERS.append(runner)
        _TASKS.append(asyncio.create_task(
            runner.run(), name=f"neutral_brain_{brain_id}",
        ))
    logger.info(
        "neutral_brains started: %d runners — %s",
        len(_RUNNERS),
        ", ".join(f"{r.brain_id}={r.display_name}" for r in _RUNNERS),
    )


async def stop_neutral_brains() -> None:
    for r in _RUNNERS:
        r.stop()
    for t in _TASKS:
        try: await asyncio.wait_for(t, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    _RUNNERS.clear()
    _TASKS.clear()


def runtime_stats() -> list[dict]:
    return [r.stats for r in _RUNNERS]
