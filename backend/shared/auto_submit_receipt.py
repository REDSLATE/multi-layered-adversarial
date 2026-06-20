"""AutoSubmitReceipt — structured exception capture for auto-submit failures.

Doctrine (operator pin 2026-02-20):
    "internal_error is useless. You need internal_error: KeyError:
     broker_id  or  ValueError: None not allowed  or  HTTP 500 Webull."

Replaces the previous `repr(e)` blob with a structured row so the
post-mortem aggregator can group by exception_type. 61 failures
showing the same `internal_error` label collapses to one bug-fix
ticket; 61 failures with breakdown 47×KeyError + 10×ConnectionError
+ 4×ValueError is THREE bug-fix tickets you can chase in parallel.

Usage:
    receipt = build_receipt(intent_id, stage="auto_submit", exc=e)
    await db[SHARED_GATE_RESULTS].insert_one(receipt.to_row(
        kind="auto_submit_failed",
        skip_category="internal_error",
        actor="auto_submit_tier_1",
    ))
"""
from __future__ import annotations

import traceback as _tb
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class AutoSubmitReceipt:
    """One labeled failure receipt. Persisted as a sub-document on
    the `shared_gate_results` row alongside `kind` and `skip_category`
    so the post-mortem can render the row WITHOUT another join."""
    intent_id: str
    stage: str                       # "dry_run" | "auto_submit" | "submit_call" | "post_submit_path"
    exception_type: str              # e.g. "KeyError", "ValueError", "ConnectionError"
    exception_message: str           # str(e), truncated to 400 chars
    traceback: str                   # last 12 frames, joined; truncated to 2000 chars
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_row(self, *, kind: str, skip_category: str, actor: str) -> dict:
        """Compose the full `shared_gate_results` document. Centralized
        so every catch-all writes the same column shape — aggregator
        depends on this contract."""
        return {
            "intent_id": self.intent_id,
            "kind": kind,
            "skip_category": skip_category,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "stage": self.stage,
            "ts": self.ts,
            "by": actor,
            "reason": f"{self.exception_type}: {self.exception_message}"[:500],
            # Traceback intentionally last and bulky — DB index won't
            # touch it; only the per-intent trace endpoint reads it.
            "traceback": self.traceback,
            "receipt": asdict(self),
        }


def _capture_traceback(exc: BaseException, max_chars: int = 2000) -> str:
    """Return the last N chars of the formatted traceback. Limits
    apply so a single deeply-nested coroutine stack doesn't bloat
    `shared_gate_results` rows."""
    try:
        lines = _tb.format_exception(type(exc), exc, exc.__traceback__)
        text = "".join(lines)
    except Exception:  # noqa: BLE001
        text = repr(exc)
    if len(text) <= max_chars:
        return text
    # Keep the TAIL — the most-recent frames are the actionable ones.
    return "...(truncated)...\n" + text[-max_chars:]


def build_receipt(
    intent_id: str,
    *,
    stage: str,
    exc: BaseException,
    message_override: Optional[str] = None,
) -> AutoSubmitReceipt:
    """Build a structured receipt from an exception. `message_override`
    lets callers replace the message when they have a more useful
    label than `str(e)` (e.g. broker HTTP responses where the body is
    the actionable bit, not the Python-level message)."""
    msg = (message_override or str(exc) or repr(exc))[:400]
    return AutoSubmitReceipt(
        intent_id=intent_id,
        stage=stage,
        exception_type=type(exc).__name__,
        exception_message=msg,
        traceback=_capture_traceback(exc),
    )
