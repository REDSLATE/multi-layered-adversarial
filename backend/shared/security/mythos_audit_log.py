"""
Mythos Defense — Audit Log

Provides a structured, append-only audit log for every intent that passes
through the Mythos Defense Layer, whether cleared or blocked.

Design principles
-----------------
- Every receipt is written synchronously before any downstream action.
- Log entries are newline-delimited JSON (NDJSON) for easy streaming ingestion.
- The log is append-only; no entry is ever modified or deleted by this module.
- Sensitive field values are redacted before writing to prevent the audit log
  itself from becoming a credential exfiltration vector.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Logger configuration
# ---------------------------------------------------------------------------

_logger = logging.getLogger("risedual.mythos_defense.audit")
_logger.setLevel(logging.INFO)

# Default log file path; override via MYTHOS_AUDIT_LOG_PATH env var.
# NOTE: evaluated at call-time (not import-time) so tests can set the env var
# via monkeypatch before the first write.
_DEFAULT_LOG_PATH = os.path.join(
    os.path.dirname(__file__), "../../../../logs/mythos_audit.ndjson"
)


def _get_log_path() -> str:
    """Return the current log path, respecting runtime env-var overrides."""
    return os.environ.get("MYTHOS_AUDIT_LOG_PATH", _DEFAULT_LOG_PATH)

# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------

_REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset({
    "signed_source", "api_key", "password", "secret", "token",
    "authorization", "private_key", "credential",
})


def _redact(value: Any, key: str = "") -> Any:
    """Redact sensitive values before writing to the audit log."""
    if key.lower() in _SENSITIVE_KEYS:
        return _REDACTED
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "…[truncated]"
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _sanitise_intent(intent: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return a sanitised copy of an intent safe for audit logging."""
    if intent is None:
        return None
    return {k: _redact(v, k) for k, v in intent.items()}


# ---------------------------------------------------------------------------
# Log writer
# ---------------------------------------------------------------------------

def write_receipt(
    receipt: Dict[str, Any],
    intent: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Append a Mythos receipt to the audit log.

    Parameters
    ----------
    receipt : The MythosReceipt dict returned by mythos_defense_check().
    intent  : The original intent dict (will be sanitised before logging).
    """
    log_path = _get_log_path()
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "allowed": receipt.get("allowed"),
        "reason": receipt.get("reason"),
        "security_multiplier": receipt.get("security_multiplier"),
        "restriction_source": receipt.get("restriction_source", "security"),
        "security_layer": receipt.get("security_layer", "mythos_defense"),
        "broker_called": receipt.get("broker_called", False),
        "intent": _sanitise_intent(intent),
    }

    line = json.dumps(entry, default=str)

    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        # Audit log write failure must never silently swallow the error;
        # surface it via the Python logger so it reaches any attached handler.
        _logger.error("MYTHOS_AUDIT_WRITE_FAILURE: %s", exc)

    # Also emit to the Python logging system for real-time visibility.
    level = logging.WARNING if not receipt.get("allowed") else logging.INFO
    _logger.log(level, "MYTHOS receipt | allowed=%s reason=%s",
                receipt.get("allowed"), receipt.get("reason"))
