"""Local brain state — JSON-on-disk persistence.

Doctrine:
    Each sovereign brain persists its weights, decision log, and mode
    on its OWN host. The brain never writes to Mission Control's
    MongoDB. This module is the contract for what gets persisted; the
    on-disk format is JSON so it's git-diffable and inspectable from a
    shell.

    The schema mirrors what `mc_client.MCClient.post_contribution`
    sends to MC, with two extras the local copy keeps but doesn't
    necessarily ship every cycle:
      - `full_decision_log`: every decision the core made, ever (only
        the recent tail goes to MC each cycle).
      - `memory`: free-form dict the brain can use for whatever it
        wants to remember between runs.

    File path defaults to `~/.risedual/<brain>/state.json` but can be
    overridden via the SOVEREIGN_STATE_PATH env var. The file is
    written atomically (write to .tmp, fsync, os.rename).

    Schema version is included so future format changes are detectable
    in the audit trail. STATE_SCHEMA.md documents the wire format."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

# Cap the local decision log so the file doesn't grow unbounded. Tune
# via env var if a brain wants a longer local replay buffer.
LOCAL_DECISION_LOG_MAX = int(os.environ.get("SOVEREIGN_LOG_MAX", "5000"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_path(brain: str) -> Path:
    override = os.environ.get("SOVEREIGN_STATE_PATH")
    if override:
        return Path(override)
    home = Path(os.environ.get("HOME", "/tmp"))
    return home / ".risedual" / brain / "state.json"


class LocalState:
    """Local-host state for one sovereign brain. JSON-backed."""

    def __init__(self, brain: str, path: Path | str | None = None,
                 mode: str = "DTD"):
        if mode not in {"DTD", "PRD"}:
            raise ValueError(f"mode must be DTD or PRD, got {mode!r}")
        self.brain = brain
        self.path = Path(path) if path else _default_path(brain)
        self._data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "brain": brain,
            "mode": mode,
            "live_trading_enabled": False,     # doctrine-pinned
            "weights": {},
            "learning_rate": 0.05,
            "memory": {},
            "full_decision_log": [],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self._load_if_present()

    # ──────────────────────── load / save ────────────────────────

    def _load_if_present(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"sovereign local-state file at {self.path} is corrupt: {e}. "
                "Move it aside and restart to reinitialize."
            ) from e
        # Re-pin the doctrine bit even on load — the file is operator-
        # editable for debugging, but local edits MUST NOT bypass the
        # observation-only doctrine.
        self._data["live_trading_enabled"] = False
        self._data.setdefault("schema_version", SCHEMA_VERSION)

    def save(self) -> None:
        self._data["updated_at"] = _now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp file in same dir, fsync, rename.
        fd, tmp = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    # ──────────────────────── accessors ────────────────────────

    @property
    def mode(self) -> str:
        return self._data["mode"]

    def set_mode(self, mode: str) -> None:
        if mode not in {"DTD", "PRD"}:
            raise ValueError(f"mode must be DTD or PRD, got {mode!r}")
        self._data["mode"] = mode

    @property
    def weights(self) -> dict[str, float]:
        return dict(self._data["weights"])

    def set_weights(self, w: dict[str, float]) -> None:
        # Schema-validated bounds match MC: [-3, +3] per feature, ≤16 feats.
        if len(w) > 16:
            raise ValueError("weights may have at most 16 features")
        out: dict[str, float] = {}
        for k, v in w.items():
            f = float(v)
            if not (-3.0 <= f <= 3.0):
                raise ValueError(f"weight[{k!r}]={f} out of bounds")
            out[str(k)] = f
        self._data["weights"] = out

    @property
    def learning_rate(self) -> float:
        return float(self._data["learning_rate"])

    def set_learning_rate(self, lr: float) -> None:
        lr = float(lr)
        if not (0.0 <= lr <= 0.5):
            raise ValueError(f"learning_rate {lr} out of bounds [0, 0.5]")
        self._data["learning_rate"] = lr

    # ──────────────────────── decision log ────────────────────────

    def append_decision(self, decision: dict) -> None:
        log = self._data.setdefault("full_decision_log", [])
        log.append(decision)
        # Roll the log so the file stays bounded.
        if len(log) > LOCAL_DECISION_LOG_MAX:
            self._data["full_decision_log"] = log[-LOCAL_DECISION_LOG_MAX:]

    def recent_outcomes(self, n: int = 20) -> list[dict]:
        """The tail the sidecar ships to MC each contribution cycle."""
        log = self._data.get("full_decision_log") or []
        # Only ship resolved rows — unresolved decisions aren't outcomes.
        resolved = [d for d in log if d.get("resolved") is True]
        return resolved[-n:]

    # ──────────────────────── memory ────────────────────────

    def remember(self, key: str, value: Any) -> None:
        self._data.setdefault("memory", {})[str(key)] = value

    def recall(self, key: str, default: Any = None) -> Any:
        return self._data.get("memory", {}).get(key, default)

    def asdict(self) -> dict:
        return dict(self._data)


__all__ = ["LocalState", "SCHEMA_VERSION", "LOCAL_DECISION_LOG_MAX"]
