"""Mission Control HTTP client — the ONLY way a sovereign brain talks
to MC.

Doctrine:
    The brain never writes to MC's database directly. It writes to its
    own local state (see `local_state.py`) and POSTs through these
    helpers. Three endpoints used:

      1. POST /api/runtime-discussion/positions/{id}/stance
         — the brain's vote on an open position.
      2. POST /api/runtime-discussion/sovereign/contribution
         — periodic snapshot of weights, learning rate, recent outcomes.
      3. POST /api/heartbeat-ping/{brain}
         — liveness ping; required so MC's quorum / staleness alerts
         can tell when the brain has gone silent.

    Auth is per-brain via `X-Runtime-Token`. The token lives on the
    brain host's env (`ALPHA_INGEST_TOKEN`, `REDEYE_INGEST_TOKEN`, etc.)
    and matches MC's `<BRAIN>_INGEST_TOKEN`.

    All three calls return non-2xx on schema rejection; the sidecar
    SHOULD NOT retry on 4xx — those mean the brain emitted something
    MC's doctrine refuses. Log + investigate. 5xx is retry-eligible.

Stdlib only — `urllib.request` keeps the brain hosts dependency-free."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class MCClientError(RuntimeError):
    """Wraps non-2xx responses with the status + body for logging."""

    def __init__(self, status: int, body: str, endpoint: str):
        self.status = status
        self.body = body
        self.endpoint = endpoint
        super().__init__(f"MC {endpoint} returned {status}: {body[:240]}")


class MCClient:
    def __init__(self, base_url: str, brain: str, runtime_token: str,
                 timeout: float = 10.0):
        if not base_url or not brain or not runtime_token:
            raise ValueError(
                "MCClient requires base_url, brain, and runtime_token"
            )
        self.base = base_url.rstrip("/")
        self.brain = brain
        self.token = runtime_token
        self.timeout = timeout

    # ──────────────────────── core http ────────────────────────

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Runtime-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8") or "{}"
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise MCClientError(e.code, body, path) from e

    # ──────────────────────── endpoints ────────────────────────

    def post_stance(self, position_id: str, stance: str, confidence: float,
                    notes: str = "", memory_sources: list[str] | None = None,
                    confidence_origin: dict[str, float] | None = None) -> dict:
        """Stamp the brain's stance on a position. stance ∈
        {long, short, abstain}.

        The mapping from the deterministic core's BUY/SELL/HOLD to
        long/short/abstain lives in `wild_adaptive_core_v2.map_action_to_stance`."""
        if stance not in {"long", "short", "abstain"}:
            raise ValueError(f"stance must be long/short/abstain, got {stance!r}")
        body: dict[str, Any] = {
            "stance": stance,
            "confidence": float(confidence),
            "notes": notes,
            "memory_sources": memory_sources or [],
            "confidence_origin": confidence_origin or {},
        }
        path = f"/api/runtime-discussion/positions/{position_id}/stance?runtime={self.brain}"
        return self._post(path, body)

    def post_contribution(self, *, mode: str, weights: dict[str, float],
                          learning_rate: float, recent_outcomes: list[dict],
                          confidence_delta: float = 0.0,
                          delta_reason: str = "",
                          training_signal: bool = False,
                          notes: str = "") -> dict:
        """Periodic snapshot of the brain's deterministic state.

        Sidecar should call this at the same cadence it persists local
        state (default: every 60s) so MC's frontend tile is fresh."""
        if mode not in {"DTD", "PRD"}:
            raise ValueError(f"mode must be DTD or PRD, got {mode!r}")
        body = {
            "mode": mode,
            "live_trading_enabled": False,    # triple-locked
            "weights": dict(weights),
            "learning_rate": float(learning_rate),
            "confidence_delta": float(confidence_delta),
            "delta_reason": delta_reason,
            "training_signal": bool(training_signal),
            "recent_outcomes": list(recent_outcomes),
            "notes": notes,
        }
        path = f"/api/runtime-discussion/sovereign/contribution?runtime={self.brain}"
        return self._post(path, body)

    def heartbeat(self) -> dict:
        """Optional helper — mirrors the existing heartbeat-ping endpoint
        used by the dashboard's staleness alerts."""
        path = f"/api/heartbeat-ping/{self.brain}"
        return self._post(path, {"ts": int(time.time())})


__all__ = ["MCClient", "MCClientError"]
