"""Broker-error taxonomy (2026-02-17).

Doctrine: no intent should retry forever. Broker failures fall into
two disjoint families:

    TERMINAL — the underlying condition will not change on retry.
        market_closed         — Sunday/holiday/pre-open rejections
        insufficient_funds    — account balance issue
        min_order_notional    — order size below broker's per-pair min
        invalid_order_args    — malformed request (400/422)
        auth_or_permission    — API key rejected (401/403)

    TRANSIENT — retry MIGHT succeed. Capped by `MAX_BROKER_RETRIES`.
        rate_limited          — 429 / too many requests
        network_transient     — 5xx / timeout / conn reset

If neither pattern matches, we default to `unknown` in the TRANSIENT
family — the safe choice is to retry a few times before terminal-
stamping, so a novel broker error class doesn't cause silent data loss.

The classifier is INTENTIONALLY string-pattern-based (not exception-
type-based) because the exception types the two brokers raise are
generic (`RuntimeError`, `KrakenError`) — the semantically meaningful
information lives entirely in the message.

Pattern-order matters: `market_closed` must be checked before
`invalid_order_args` because Webull's Sunday response contains BOTH
"INVALID_PARAMETER" and "not supported". Same for `min_order_notional`
before `invalid_order_args` on Kraken's `EGeneral:Invalid arguments:
volume minimum not met`. The tests below lock this precedence in.
"""
from __future__ import annotations

from dataclasses import dataclass


TERMINAL_BUCKETS = frozenset({
    "market_closed",
    "insufficient_funds",
    "min_order_notional",
    "invalid_order_args",
    "auth_or_permission",
})

TRANSIENT_BUCKETS = frozenset({
    "rate_limited",
    "network_transient",
    "unknown",
})


@dataclass(frozen=True)
class BrokerErrorClass:
    bucket: str
    is_terminal: bool
    # Short humanized detail (≤120 chars) drawn from the raw message,
    # safe to store on the intent for operator debugging.
    detail: str


def classify(exc: BaseException) -> BrokerErrorClass:
    """Bucket a broker exception. Never raises — worst case returns
    the `unknown` transient bucket so retry logic still applies."""
    raw = str(exc)
    msg = raw.lower()

    # ─── TERMINAL — order matters ────────────────────────────────
    # market_closed must precede invalid_order_args because Webull's
    # weekend rejection reads:
    #   "HTTP Status: 417, Code: INVALID_PARAMETER, Msg: The time you
    #    sent is not supported."
    # The literal phrase "not supported" is the tell.
    if ("not supported" in msg
            or "market closed" in msg
            or "market is closed" in msg
            or "trading is not open" in msg
            or "outside trading hours" in msg
            or "after hours" in msg):
        return BrokerErrorClass("market_closed", True, _detail(raw))

    if ("insufficient funds" in msg
            or "insufficient_funds" in msg
            or "eorder:insufficient funds" in msg
            or "not enough balance" in msg
            or "not enough cash" in msg
            or "buying power" in msg):
        return BrokerErrorClass("insufficient_funds", True, _detail(raw))

    # min_order_notional must precede invalid_order_args because
    # Kraken's response reads:
    #   "EGeneral:Invalid arguments:volume minimum not met"
    # "volume minimum" is the tell — invalid args framing wraps it.
    if ("volume minimum" in msg
            or "minimum not met" in msg
            or "below minimum" in msg
            or "min_notional" in msg
            or "order size too small" in msg
            or "notional too small" in msg
            or "order too small" in msg):
        return BrokerErrorClass("min_order_notional", True, _detail(raw))

    if ("401" in msg
            or "403" in msg
            or "unauthorized" in msg
            or "forbidden" in msg
            or "invalid api key" in msg
            or "authentication failed" in msg
            or "eapi:invalid key" in msg
            or "invalid_signature" in msg):
        return BrokerErrorClass("auth_or_permission", True, _detail(raw))

    # ─── RATE LIMIT (TRANSIENT) — must peel off BEFORE the 4xx
    # catch-all below. Webull's throttle response reads
    # `HTTP Status: 429, TOO_MANY_REQUESTS, ...` which contains
    # "http status: 4" — without this ordering, every 429 would
    # be misclassified as `invalid_order_args` (terminal) and the
    # retry cap in `auto_router._sweep_submitted_broker_orders`
    # would be defeated on the single most likely RTH rejection
    # (2026-07-06 ordering fix — see test_broker_error_taxonomy.py
    # regression anchor `test_classify_webull_429_transient`).
    if ("429" in msg
            or "rate limit" in msg
            or "too many requests" in msg
            or "eapi:rate limit exceeded" in msg):
        return BrokerErrorClass("rate_limited", False, _detail(raw))

    # Generic 4xx / malformed request. Kept LAST among TERMINAL so
    # market_closed / insufficient_funds / min_order_notional /
    # auth_or_permission / rate_limited can peel off first.
    if ("http status: 4" in msg
            or "invalid_parameter" in msg
            or "invalid arguments" in msg
            or "invalid request" in msg
            or "bad request" in msg
            or " 400 " in msg
            or " 422 " in msg):
        return BrokerErrorClass("invalid_order_args", True, _detail(raw))

    # ─── TRANSIENT ────────────────────────────────────────────────
    if ("timeout" in msg
            or "timed out" in msg
            or "connection" in msg
            or "connection reset" in msg
            or "connection refused" in msg
            or " 5" in msg[:20]  # crude 5xx signal at the head
            or "http status: 5" in msg
            or "eservice:unavailable" in msg
            or "socket" in msg
            or "network" in msg):
        return BrokerErrorClass("network_transient", False, _detail(raw))

    return BrokerErrorClass("unknown", False, _detail(raw))


def _detail(raw: str) -> str:
    """Compact the raw exception message for storage on the intent.
    Preserve the first 120 chars — enough to distinguish rejection
    classes without bloating the doc."""
    return raw[:120]


__all__ = [
    "BrokerErrorClass",
    "TERMINAL_BUCKETS",
    "TRANSIENT_BUCKETS",
    "classify",
]
