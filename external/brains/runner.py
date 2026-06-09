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
  5. Asks `NeutralAdversarialBrain.evaluate` for a ranked verdict —
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
import socket
import time
from typing import Optional

import httpx

from .brain_core import BrainIntent, NeutralAdversarialBrain
from .personality import apply_personality_confidence, get_personality


logger = logging.getLogger("risedual.neutral_brains")


# Skills layer — lazy module-level instance. Skills live at
# /app/external/skills/skill_pack/<name>/SKILL.md. Operator can
# hot-edit any SKILL.md and the next selection pass picks it up
# (selector re-reads from disk each call).
_SKILL_SELECTOR = None


def _skill_selector():
    """Lazy accessor for the skill selector — defers import so a
    misconfigured skill_pack can't kill the runner at boot."""
    global _SKILL_SELECTOR
    if _SKILL_SELECTOR is None:
        try:
            import sys as _sys
            if "/app" not in _sys.path:
                _sys.path.insert(0, "/app")
            from external.skills.selector import SkillSelector  # type: ignore
            _SKILL_SELECTOR = SkillSelector()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "skill selector unavailable err=%s — intents will post "
                "without skill enrichment (safe fallback)", exc,
            )
            _SKILL_SELECTOR = False  # sentinel
    return _SKILL_SELECTOR or None


def _select_skills_for(lane: str, symbol: str, action: str, snapshot: dict) -> tuple[list[str], dict]:
    """Run skill selection for the current intent context and return
    (skill_names, skill_evidence). On failure returns empty + safe
    defaults — skill selection is enrichment, never a gate."""
    selector = _skill_selector()
    if not selector:
        return [], {"selector_available": False}
    try:
        task = f"{action} {lane} {symbol}"
        picks = selector.select(task=task, snapshot=snapshot, limit=3)
        names = [s.name for s in picks]
        return names, {
            "selector_available": True,
            "task": task,
            "skills_considered": [s.name for s in picks],
            "tags_per_skill": {s.name: s.tags for s in picks},
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill selection failed err=%s — falling back", exc)
        return [], {"selector_available": False, "error": str(exc)}


# ── Brain identity map. INTERNAL slot ids (alpha/camaro/chevelle/
# redeye) are the DB primary keys used across MC and never change.
# DISPLAY NAMES are the operator-facing brand (Camino / Barracuda /
# Hellcat / GTO) shown on every dashboard, intent card, ladder row.
BRAIN_ROSTER = [
    ("alpha",    "Camino",    "ALPHA_INGEST_TOKEN"),
    ("camaro",   "Barracuda", "CAMARO_INGEST_TOKEN"),
    ("chevelle", "Hellcat",   "CHEVELLE_INGEST_TOKEN"),
    ("redeye",   "GTO",       "REDEYE_INGEST_TOKEN"),
]


# ── Tick + cadence settings ──
MC_LOOPBACK_URL = "http://127.0.0.1:8001"

# Origin discriminator (2026-02-XX). Both preview and prod pods share
# the same .env and therefore both stamp `env_name=prod`. To keep the
# intent stream attributable to its actual source pod, we ALSO stamp
# the hostname (auto-differs per container) and an explicit
# `runtime_origin` label sourced from RISEDUAL_RUNTIME_ORIGIN (which
# CAN be set per-deployment in Emergent's per-environment env panel
# if/when the operator gets access).
#
# Default fallback: socket.gethostname() — guaranteed unique per pod.
# This means preview-pod intents and prod-pod intents are filterable
# in shared_intents even when env_name is identical.
try:
    _POD_HOSTNAME = socket.gethostname() or "unknown"
except Exception:  # noqa: BLE001
    _POD_HOSTNAME = "unknown"
RUNTIME_ORIGIN = (
    os.environ.get("RISEDUAL_RUNTIME_ORIGIN", "").strip()
    or _POD_HOSTNAME
)
TICK_INTERVAL_SEC = float(os.environ.get("NEUTRAL_BRAIN_TICK_SEC", "45"))
CHECKIN_INTERVAL_SEC = float(os.environ.get("NEUTRAL_BRAIN_CHECKIN_SEC", "30"))
HTTP_TIMEOUT_SEC = float(os.environ.get("NEUTRAL_BRAIN_HTTP_TIMEOUT_SEC", "8"))
PATTERN_BIAS = float(os.environ.get("NEUTRAL_BRAIN_PATTERN_BIAS", "0.20"))
# 2026-02-XX (this session) — sovereign contribution cadence.
# Without this loop, MC's `brain_emission_diagnose.sovereign_loop`
# stays in stale/dead and the operator sees a misleading "sovereign
# silent" banner even though the brain is alive and posting intents.
# Default 60s — well inside the 5min stale threshold.
SOVEREIGN_INTERVAL_SEC = float(os.environ.get("NEUTRAL_BRAIN_SOVEREIGN_SEC", "60"))


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


def _env(name: str, default: str = "") -> str:
    """Trimmed env reader. Empty string == "not set"."""
    return os.environ.get(name, default).strip()


def _identity_env_name() -> str:
    """Environment label (prod / preview / staging / unknown).

    Source-of-truth order:
      1. RISEDUAL_ENV   — canonical (same var MC's platform_survival reads).
      2. ENV            — generic fallback for older containers.
      3. BRAIN_ENV_NAME — legacy var from the external-sidecar era.
      4. "unknown"      — fails closed. MC will flag ENV_NOT_PROD and
                          the operator sees an honest "this brain
                          doesn't know what env it's in" verdict
                          instead of a false "prod" stamp.
    """
    return (
        _env("RISEDUAL_ENV")
        or _env("ENV")
        or _env("BRAIN_ENV_NAME")
        or "unknown"
    )


def _identity_mc_url() -> str:
    """The MC URL the brain advertises in its check-in.

    Source-of-truth order:
      1. RISEDUAL_MC_URL          — canonical.
      2. BRAIN_ADVERTISED_MC_URL  — legacy.
      3. ""                       — fails closed (MC flags MC_URL_NOT_PROD).
    """
    return _env("RISEDUAL_MC_URL") or _env("BRAIN_ADVERTISED_MC_URL")


def _identity_git_sha() -> str:
    return (
        _env("RISEDUAL_GIT_SHA")
        or _env("GIT_SHA")
        or _env("VERCEL_GIT_COMMIT_SHA")
        or _env("RAILWAY_GIT_COMMIT_SHA")
        or _env("BRAIN_GIT_SHA")
        or "unknown"
    )


def _identity_broker_mode() -> str:
    """One of `paper | live | dry_run`. MC's validator rejects anything
    else with BAD_BROKER_MODE. Default `paper` — safe for any env."""
    raw = _env("RISEDUAL_BROKER_MODE") or "paper"
    return raw if raw in ("paper", "live", "dry_run") else "paper"


def _checkin_stamp(brain_id: str, display_name: str) -> dict:
    """Build the brain's identity stamp the way MC's
    `validate_for_prod_sidecar` expects it. The `policy_hash` is
    computed by the SAME `policy_hash()` MC validates against, so
    the HASH_MISMATCH badge will only fire when code/doctrine
    actually drifts — never as a self-inflicted off-by-name bug.
    """
    # Late import: `shared.runtime.platform_survival` lives in the
    # backend tree; `external.brains.runner` lives outside. Defer so
    # this module stays import-safe in tooling that doesn't have
    # `/app/backend` on the path yet.
    try:
        import sys as _sys
        if "/app/backend" not in _sys.path:
            _sys.path.insert(0, "/app/backend")
        from shared.runtime.platform_survival import (  # type: ignore
            policy_hash as _canonical_policy_hash,
        )
        policy_hash_val = _canonical_policy_hash()
    except Exception:  # noqa: BLE001
        # If platform_survival can't be reached, fall through with an
        # empty hash. MC's policy_hash_match will be False — visible
        # as HASH MISMATCH — which is the honest signal that the
        # brain's runtime can't see MC's doctrine module.
        policy_hash_val = ""

    return {
        "stamp": {
            "app_name": _env("RISEDUAL_APP_NAME") or "risedual",
            "env_name": _identity_env_name(),
            "git_sha": _identity_git_sha(),
            "platform": _env("RISEDUAL_PLATFORM") or _env("PLATFORM") or "emergent",
            "mc_url": _identity_mc_url(),
            "db_name": _env("RISEDUAL_DB_NAME") or _env("DB_NAME") or "",
            "broker_mode": _identity_broker_mode(),
            "sidecar_room": brain_id,
            "sidecar_version": (
                _env("RISEDUAL_SIDECAR_VERSION")
                or _env("BRAIN_SIDECAR_VERSION")
                or "neutral-v2"
            ),
            # CANONICAL policy hash — same SHA256 MC computes. Matches
            # by construction unless someone forks the doctrine dict
            # in platform_survival.policy_hash().
            "policy_hash": policy_hash_val,
            "local_execution_authority": False,
            "display_name": display_name,
            "timestamp_ms": int(time.time() * 1000),
        }
    }


def _log_identity_once() -> None:
    """Emit a single supervisor-log line per process showing exactly
    what identity the brains will report in their check-ins. Makes
    the "wait, why is prod saying preview?" class of bugs obvious in
    one `tail -f` of the backend logs.

    Doctrine: this log is descriptive evidence only. It does NOT
    mutate or validate anything — the operator reads it once at
    startup to confirm prod is configured as prod.
    """
    env_name = _identity_env_name()
    mc_url = _identity_mc_url() or "<unset>"
    db_name = _env("RISEDUAL_DB_NAME") or _env("DB_NAME") or "<unset>"
    broker = _identity_broker_mode()
    sha = _identity_git_sha()
    logger.info(
        "neutral_brain identity env_name=%s mc_url=%s db_name=%s "
        "broker_mode=%s git_sha=%s — operator: this is what every "
        "brain check-in will stamp. If env_name != 'prod' or "
        "mc_url != 'https://mission.risedual.ai' on prod, set "
        "RISEDUAL_ENV / RISEDUAL_MC_URL in the prod deploy.",
        env_name, mc_url, db_name, broker, sha,
    )


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
            window_high = max(closes)
            window_low = min(closes) or 1.0
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
            "neutral_template_version": "neutral-v2",
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
        self._sovereign_count = 0
        self._universe: list[tuple[str, str]] = []
        # Rolling tape of the brain's last N decisions — fed back into
        # the sovereign contribution as `recent_outcomes` so MC's
        # `sovereign_loop` diagnostic stays fresh AND the audit log
        # carries real (not skeleton) rows.
        self._recent_tape: list[dict] = []
        self._last_action: Optional[str] = None
        self._last_confidence: float = 0.0
        # One brain core per (display_name, lane) so per-lane state
        # (shadow_only flag, min_commitment) can diverge later. Today
        # all lanes share defaults but the layout supports divergence.
        shadow_only = _shadow_only_default()
        self._cores = {
            "crypto": NeutralAdversarialBrain(
                brain_id=brain_id, display_name=display_name,
                lane="crypto", shadow_only=shadow_only,
                min_commitment=0.58, min_gap=0.06,
                max_shadow_size=1.0 if not shadow_only else 0.0,
            ),
            "equity": NeutralAdversarialBrain(
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
        sovereign_task = asyncio.create_task(self._sovereign_loop())
        try:
            await self._stop.wait()
        finally:
            for t in (intent_task, checkin_task, sovereign_task):
                t.cancel()
            for t in (intent_task, checkin_task, sovereign_task):
                try:
                    await t
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
            "sovereign_count": self._sovereign_count,
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

        # 2026-02-XX: skills + personality enrichment. The brain core
        # produces a raw read; the skills layer surfaces lenses; the
        # personality multiplier shapes how confidently THIS brain
        # voices the read. ALL three contributions are recorded as
        # evidence on the intent so the operator audit log shows
        # exactly who touched the conviction and why.
        #
        # None of this is a gate — MC's lane toggles, ladder, sizing
        # gate, exposure caps and receipt are the only restriction
        # layer. Skills + personality only ENRICH the hypothesis.
        skill_names, skill_evidence = _select_skills_for(
            lane=lane, symbol=symbol, action=intent.action, snapshot=snapshot,
        )
        final_confidence, confidence_evidence = apply_personality_confidence(
            brain=self.brain_id,
            raw_confidence=intent.confidence,
        )
        # Substitute the enriched confidence onto the intent so the
        # MC payload mirror at `_intent_to_mc_payload` picks it up.
        # raw_confidence is preserved in confidence_evidence.
        intent.confidence = final_confidence

        payload = _intent_to_mc_payload(intent)
        # Layer skills + personality evidence onto the payload's
        # `evidence` block without clobbering what the core stamped.
        payload["evidence"]["skills_used"] = skill_names
        payload["evidence"]["skill_evidence"] = skill_evidence
        payload["evidence"]["confidence_evidence"] = confidence_evidence
        payload["evidence"]["personality_risk_mode"] = (
            get_personality(self.brain_id).get("risk_mode", "balanced")
        )
        payload["evidence"]["display_name_emit"] = self.display_name
        # Pod-level discriminator so preview vs prod intents are
        # filterable even when env_name is identical. See module-
        # level `RUNTIME_ORIGIN` doctrine comment.
        payload["evidence"]["runtime_origin"] = RUNTIME_ORIGIN
        payload["evidence"]["pod_hostname"] = _POD_HOSTNAME
        payload["evidence"]["env_name_emit"] = os.environ.get(
            "RISEDUAL_ENV", os.environ.get("ENV", "unknown"),
        )

        r = await http.post(
            f"{MC_LOOPBACK_URL}/api/intents",
            json=payload,
            headers={"X-Runtime-Token": self.token},
        )
        if r.status_code // 100 == 2:
            self._intent_count += 1
            # Record onto the rolling tape so the sovereign loop has
            # real `recent_outcomes` to ship. We stamp `outcome=0`
            # (unresolved) — MC's opinion_resolver grades these later;
            # the contribution is a snapshot of the brain's POSTed
            # decisions, not their resolved P&L.
            self._recent_tape.append({
                "symbol": intent.symbol,
                "action": intent.action if intent.action in ("BUY", "SELL") else "HOLD",
                "confidence": float(intent.confidence),
                "outcome": 0,
                "resolved_at": None,
                "notional": float(intent.size),
            })
            if len(self._recent_tape) > 25:
                self._recent_tape = self._recent_tape[-25:]
            self._last_action = intent.action
            self._last_confidence = float(intent.confidence)
            # 2026-02-XX: ALSO post a directional opinion to the
            # discussion layer. The composite-liveness dashboard
            # has an `opinion_loop` that goes DEAD when no opinions
            # are posted in the last hour — without this, the
            # in-process brains look STALE_OPINION even though
            # they're actively producing intents.
            #
            # Opinion is derived from the intent: BUY → long, SELL →
            # short, HOLD → observation. Topic is the symbol.
            # This is descriptive evidence, not execution authority
            # (may_execute=False enforced by MC's schema).
            try:
                await self._post_directional_opinion(http, intent)
            except Exception as exc:  # noqa: BLE001
                # Opinion is enrichment, not a gate. Never let a
                # failed opinion post block intent flow.
                logger.warning(
                    "opinion post failed brain=%s sym=%s err=%s",
                    self.brain_id, intent.symbol, exc,
                )
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

    async def _post_directional_opinion(
        self, http: httpx.AsyncClient, intent,
    ) -> None:
        """POST a directional opinion to the discussion layer for the
        symbol the brain just emitted an intent on.

        The composite-liveness dashboard reads `shared_brain_opinions`
        to fill its `opinion_loop` band — without this post, the
        in-process brains show STALE_OPINION even when their intent +
        sovereign + heartbeat loops are healthy.

        Mapping:
            intent.action  → opinion.stance
                BUY  →  long
                SELL →  short
                HOLD →  observation
            intent.confidence → opinion.confidence
            intent.symbol     → opinion.topic = f"symbol:{symbol}"

        Doctrine: opinions are DESCRIPTIVE EVIDENCE. MC's schema rejects
        any opinion with `may_execute=true`. The opinion never grants
        the brain any new authority — it just makes the brain's voice
        visible in the discussion layer so MC can correlate views
        across brains and the opinion_loop stays fresh.
        """
        stance_map = {"BUY": "long", "SELL": "short", "HOLD": "observation"}
        stance = stance_map.get(intent.action, "observation")
        body = {
            "runtime": self.brain_id,
            "topic": f"symbol:{intent.symbol}",
            "stance": stance,
            "confidence": float(intent.confidence),
            "body": (
                f"{self.display_name} ({self.brain_id}) — {intent.action} "
                f"on {intent.symbol} at conf {intent.confidence:.2f}. "
                f"Derived from in-process intent {intent.intent_id}."
            )[:6000],
            "evidence": {
                "intent_id": intent.intent_id,
                "lane": intent.lane,
                "source": "in_process_brain_runner",
                "personality_risk_mode": (
                    get_personality(self.brain_id).get("risk_mode")
                ),
            },
            "may_execute": False,
        }
        r = await http.post(
            f"{MC_LOOPBACK_URL}/api/ingest/opinion",
            json=body,
            headers={"X-Runtime-Token": self.token},
        )
        if r.status_code // 100 != 2:
            logger.debug(
                "opinion rejected brain=%s sym=%s status=%s body=%s",
                self.brain_id, intent.symbol, r.status_code, r.text[:200],
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

    async def _sovereign_loop(self) -> None:
        """Periodically POSTs a substantive sovereign-state contribution
        to MC so `brain_emission_diagnose.sovereign_loop` stays fresh.

        Doctrine: the payload must be SUBSTANTIVE (≥1 of notes / weights
        / recent_outcomes / delta_reason / confidence_delta non-default)
        or MC's 422 empty-contribution gate rejects it. We always send
        weights + notes + the rolling tape of recent decisions, so the
        gate accepts and the operator audit log carries real signal.
        """
        # Cold-start delay so the first sovereign POST happens AFTER the
        # brain has produced at least one intent (avoid posting with an
        # empty tape on tick 0).
        await asyncio.sleep(8.0 + random.uniform(0, 4.0))
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as http:
            while not self._stop.is_set():
                try:
                    await self._post_sovereign_contribution(http)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "sovereign_loop error brain=%s: %s", self.brain_id, e,
                    )
                await asyncio.sleep(
                    SOVEREIGN_INTERVAL_SEC + random.uniform(-3, 3),
                )

    async def _post_sovereign_contribution(self, http: httpx.AsyncClient) -> None:
        """Build + POST the contribution payload. Substantive by
        construction — never trips the empty-contribution gate."""
        # Weights snapshot from the brain core's published coefficients.
        # We expose the hypothesis weighting so the audit log records
        # WHICH model produced the recent tape. Stays inside the
        # ±WEIGHT_MAX_ABS bounds MC enforces.
        weights = {
            "trend_weight": 0.18,
            "rsi_weight": 0.004,
            "liquidity_weight": 0.08,
            "volatility_penalty": -0.10,
            "spread_penalty": -0.001,
            "pattern_bias": PATTERN_BIAS,
        }
        tape = list(self._recent_tape[-20:])  # MAX_RECENT_OUTCOMES=50
        # `mode=PRD` because the brain is reading live MC technical
        # feeds. `training_signal=False` because the neutral template
        # does NOT mutate weights at runtime — when the real adaptive
        # core lands, flip this to True for actual training ticks.
        body = {
            "mode": "PRD",
            "live_trading_enabled": not _shadow_only_default(),
            "weights": weights,
            "learning_rate": 0.0,
            "confidence_delta": 0.0,
            "delta_reason": "",
            "training_signal": False,
            "recent_outcomes": tape,
            "notes": (
                f"{self.display_name} ({self.brain_id}) — "
                f"ticks={self._tick_count} intents={self._intent_count} "
                f"last_action={self._last_action or 'none'} "
                f"last_confidence={self._last_confidence:.3f} "
                f"shadow_only={_shadow_only_default()} "
                f"lanes={','.join(_enabled_lanes())}"
            )[:2048],
        }
        r = await http.post(
            f"{MC_LOOPBACK_URL}/api/runtime-discussion/sovereign/contribution",
            params={"runtime": self.brain_id},
            json=body,
            headers={
                "X-Runtime-Token": self.token,
                "X-Client-Request-Id": (
                    f"{self.brain_id}-sovereign-{int(time.time())}"
                ),
            },
        )
        if r.status_code // 100 == 2:
            self._sovereign_count += 1
            if self._sovereign_count % 10 == 1:
                logger.info(
                    "neutral_brain sovereign posted brain=%s display=%s "
                    "tape_size=%d total=%d",
                    self.brain_id, self.display_name, len(tape),
                    self._sovereign_count,
                )
        else:
            logger.warning(
                "neutral_brain sovereign rejected brain=%s status=%s body=%s",
                self.brain_id, r.status_code, r.text[:300],
            )


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
    # Emit the identity log ONCE per process start so the operator
    # can confirm prod is configured as prod (or spot the misconfig
    # immediately if env_name="preview" on the prod deploy).
    _log_identity_once()
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
        try:
            await asyncio.wait_for(t, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _RUNNERS.clear()
    _TASKS.clear()


def runtime_stats() -> list[dict]:
    return [r.stats for r in _RUNNERS]


def runner_for(brain_id: str) -> Optional["BrainRunner"]:
    """Return the live in-process runner for a given brain_id, or
    None if neutral brains are disabled / this brain isn't running
    in-process. MC's `/admin/runtime/{brain}/status` proxy uses this
    to synthesize a status payload from local state instead of
    hitting a (now-defunct) external sidecar URL.
    """
    bid = (brain_id or "").lower().strip()
    for r in _RUNNERS:
        if r.brain_id == bid:
            return r
    return None
