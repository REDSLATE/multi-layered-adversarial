"""Deterministic parity key.

Design freeze update (operator directive, 2026-07, iter-27 pulse
migration): parity is meaningless without a canonical join. The
runner and the pulse must be tied to the *exact same closed bar*
— not the wall-clock moment the brain happened to emit an
intent. A runner and pulse that evaluate 12 seconds apart on the
same 1m close of NVDA are looking at the same market truth and
must be scored as such.

ParityKey is that canonical join:

    ParityKey(
        brain_id="camino",
        symbol="NVDA",
        timeframe="1m",
        source_bar_close_at=<UTC ISO, at bar boundary>,
        snapshot_schema_version="camino-feature-v1",
    )

`timeframe` is part of the identity — two evaluations against the
same nominal close at different tfs are DIFFERENT market events.
`snapshot_schema_version` is a LABEL (not a hash) that flips only
when the canonical feature builder changes shape. Structural
compatibility across manifests is decided by this label; content
identity is decided separately by `feature_digest` on the manifest
itself. Mixing the two into the join key would hide the exact
class of divergence parity is meant to reveal (same inputs →
different opinions, or different inputs → same opinions).

Timestamp semantics are NOT decided by flooring an evaluation
time — the runner and pulse resolve their own bar identity from
the source record (`shared_ohlcv_bars`) via `BarIdentity`. The
`canonical_close_from_evaluation_time` helper exists only for the
narrow case where a caller has ONLY the evaluation time and needs
to identify the most-recently-completed bar.

Validation posture (2026-07 hardening):
    * Timeframes are looked up strictly — an unknown `tf` raises
      instead of silently degrading to 1m. Parity keys with a
      wrong tf label would otherwise pair unrelated market
      events.
    * Intraday sources must supply boundary-aligned timestamps.
      A non-aligned `ts` (e.g. 15:00:12) indicates an upstream
      feeder defect and MUST surface at construction time, not
      be smoothed over.
    * Daily identity accepts explicit open_at + close_at only.
      Equity daily bars are session-calendar based; crypto daily
      bars are UTC based; the two cannot be conflated with a
      single 86400s modulo rule.
    * `parse_parity_key` refuses malformed values so a key read
      from storage compares textually equal to a freshly built
      one for the same (brain, symbol, tf, close).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Label — flip when the canonical Camino feature builder changes
# the shape of the snapshot dict it emits. Not a hash; hashes go
# on the manifest as `feature_digest`.
CAMINO_SNAPSHOT_SCHEMA_VERSION = "camino-feature-v1"

# Bar-timeframe → bucket length in seconds. Intraday only —
# daily bars carry session/calendar semantics that cannot be
# expressed as a fixed modulo. See `daily_bar_identity`.
_INTRADAY_BUCKET_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}

_SUPPORTED_TIMEFRAMES = frozenset({*_INTRADAY_BUCKET_SECONDS, "1d"})


def _intraday_bucket_seconds(timeframe: str) -> int:
    """Strict lookup — raises `ValueError` on unknown tf. Prevents
    a typo like "1min" from silently minting a 1-minute identity
    while storing `timeframe="1min"` on the key."""
    try:
        return _INTRADAY_BUCKET_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(
            f"unsupported intraday timeframe: {timeframe!r} "
            f"(supported: {sorted(_INTRADAY_BUCKET_SECONDS)})"
        ) from exc


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ParityKey:
    """The one true join key for parity comparisons.

    All five fields are part of the identity — dropping any one
    would let two structurally distinct events masquerade as the
    same market moment.

    `source_bar_close_at` is the CANONICAL bar-close timestamp
    (UTC, aligned to the bar's natural boundary). Runner and
    pulse both derive it from their source bar record via
    `BarIdentity`; neither path guesses it from wall-clock.
    """
    brain_id: str
    symbol: str
    timeframe: str
    source_bar_close_at: str
    snapshot_schema_version: str = CAMINO_SNAPSHOT_SCHEMA_VERSION

    def as_string(self) -> str:
        """Sortable string form. Also the Mongo idempotency key
        on the manifest collection (paired with `path`)."""
        return "|".join((
            self.brain_id,
            self.symbol,
            self.timeframe,
            self.source_bar_close_at,
            self.snapshot_schema_version,
        ))

    def hash(self) -> str:
        """Short stable hash for compact logging."""
        return hashlib.sha1(self.as_string().encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class BarIdentity:
    """Authoritative bar identity emitted by the canonical
    feature builder.

    Neither runner nor pulse GUESSES what the source bar's
    timestamp means — the feature builder reads the source
    record (`shared_ohlcv_bars`) and emits open + close
    explicitly. Downstream code composes the ParityKey from
    THIS, not from `datetime.now()`.

    `source` is the origin string ("shared_ohlcv_bars",
    "webull_snapshot", "kraken_ohlc") so a divergence between
    two paths pulling from different sources is visible on the
    manifest instead of being averaged into aggregate metrics.
    """
    timeframe: str
    open_at: datetime
    close_at: datetime
    source: str

    def to_parity_key(self, *, brain_id: str, symbol: str,
                      snapshot_schema_version: str = CAMINO_SNAPSHOT_SCHEMA_VERSION
                      ) -> ParityKey:
        if self.timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"unsupported timeframe: {self.timeframe!r} "
                f"(supported: {sorted(_SUPPORTED_TIMEFRAMES)})"
            )
        return ParityKey(
            brain_id=(brain_id or "").strip().lower(),
            symbol=(symbol or "").strip().upper(),
            timeframe=self.timeframe,
            source_bar_close_at=_ensure_utc(self.close_at).isoformat(),
            snapshot_schema_version=snapshot_schema_version,
        )


def canonical_close_from_evaluation_time(
    evaluation_time: datetime, timeframe: str,
) -> datetime:
    """Return the close timestamp of the most-recently-completed
    bar at `timeframe` for a caller that has only the evaluation
    time (no `BarIdentity` from the source).

    Contract:
        For evaluation_time=15:00:12, tf=1m → returns 15:00:00
        (the 1m bar spanning [14:59:00, 15:00:00) closed at
        exactly 15:00:00; any evaluation between 15:00:00 and
        15:00:59.999 attributes to that same completed close).

    Intraday only — raises `ValueError` for `tf="1d"`, since
    daily boundaries are calendar/session-defined and cannot be
    derived from wall-clock alone. Callers with a daily bar must
    use `daily_bar_identity(open_at=..., close_at=...)` from the
    source record.
    """
    bucket = _intraday_bucket_seconds(timeframe)
    ts = _ensure_utc(evaluation_time)
    epoch = int(ts.timestamp())
    aligned = epoch - (epoch % bucket)
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def intraday_bar_identity(
    *,
    timeframe: str,
    bar_timestamp: datetime,
    timestamp_semantics: str,           # "open" | "close"
    source: str,
) -> BarIdentity:
    """Materialize a `BarIdentity` for a FIXED-LENGTH intraday
    bar (1m / 5m / 15m / 1h).

    Validates that `bar_timestamp` sits on the natural bucket
    boundary — a source that emits 15:00:12 for a nominal 1m bar
    is broken, and the parity key must SURFACE that defect
    rather than smooth it into a spurious identity. `open_at` and
    `close_at` are derived from the aligned timestamp per
    `timestamp_semantics`.

    Daily bars must use `daily_bar_identity`; see that function's
    docstring for why 86_400s modulo is not sufficient for tf=1d.
    """
    bucket = _intraday_bucket_seconds(timeframe)
    ts = _ensure_utc(bar_timestamp)
    epoch = int(ts.timestamp())
    if epoch % bucket != 0:
        raise ValueError(
            f"{source} supplied non-aligned {timestamp_semantics} "
            f"timestamp {ts.isoformat()} for timeframe {timeframe}"
        )
    if timestamp_semantics == "open":
        open_at = ts
        close_at = datetime.fromtimestamp(epoch + bucket, tz=timezone.utc)
    elif timestamp_semantics == "close":
        close_at = ts
        open_at = datetime.fromtimestamp(epoch - bucket, tz=timezone.utc)
    else:
        raise ValueError(
            f"timestamp_semantics must be 'open' or 'close', got "
            f"{timestamp_semantics!r}"
        )
    return BarIdentity(
        timeframe=timeframe,
        open_at=open_at,
        close_at=close_at,
        source=source,
    )


def daily_bar_identity(
    *,
    open_at: datetime,
    close_at: datetime,
    source: str,
) -> BarIdentity:
    """Materialize a `BarIdentity` for a DAILY bar.

    Daily bars cannot be described by a fixed 86_400s modulo:
      * US equity daily bars span the NYSE session
        (14:30–21:00 UTC on regular days, shorter on holidays);
      * Crypto daily bars conventionally span UTC 00:00–00:00;
      * Some providers report the trading-day midnight-UTC as
        both `open_at` and `close_at`.

    Rather than embed calendar logic here, this constructor
    accepts explicit open_at + close_at from the source record
    and validates only that (a) both are UTC and (b) close is
    strictly after open. The source is responsible for supplying
    honest boundaries — the parity key just faithfully carries
    them.
    """
    open_utc = _ensure_utc(open_at)
    close_utc = _ensure_utc(close_at)
    if not close_utc > open_utc:
        raise ValueError(
            f"{source}: daily close_at ({close_utc.isoformat()}) must be "
            f"strictly after open_at ({open_utc.isoformat()})"
        )
    return BarIdentity(
        timeframe="1d",
        open_at=open_utc,
        close_at=close_utc,
        source=source,
    )


def parse_parity_key(raw: Optional[str]) -> Optional[ParityKey]:
    """Reverse `ParityKey.as_string()`. Returns None on any
    malformed input so a key read from storage compares equal to
    a freshly built one for the same (brain, symbol, tf, close).

    Validation:
        * exactly 5 pipe-delimited parts
        * brain_id / symbol / snapshot_schema_version non-empty
        * timeframe ∈ supported set (rejects "1min", "daily", ...)
        * close_at parseable as ISO-8601 with explicit tzinfo
        * casing normalized (brain_id lower, symbol upper) and
          close_at re-emitted in canonical UTC ISO form
    """
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.split("|")
    if len(parts) != 5:
        return None
    brain_id, symbol, timeframe, close_at, version = parts
    if not brain_id or not symbol or not version:
        return None
    if timeframe not in _SUPPORTED_TIMEFRAMES:
        return None
    try:
        parsed_close = datetime.fromisoformat(close_at)
    except ValueError:
        return None
    if parsed_close.tzinfo is None:
        return None
    return ParityKey(
        brain_id=brain_id.strip().lower(),
        symbol=symbol.strip().upper(),
        timeframe=timeframe,
        source_bar_close_at=_ensure_utc(parsed_close).isoformat(),
        snapshot_schema_version=version,
    )
