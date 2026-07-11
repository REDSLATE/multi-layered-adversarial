"""Deterministic parity key.

Design freeze update (operator directive, 2026-02): parity is
meaningless without a canonical join. The runner and the pulse
must be tied to the *exact same closed bar* — not the wall-clock
moment the brain happened to emit an intent. A runner and pulse
that evaluate 12 seconds apart on the same 1m close of NVDA are
looking at the same market truth and must be scored as such.

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

# Bar-timeframe → bucket length in seconds. Used only by the
# evaluation-time convenience helper; the authoritative bar
# identity ships as `BarIdentity` from the canonical builder.
_BUCKET_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}


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
        return ParityKey(
            brain_id=(brain_id or "").strip().lower(),
            symbol=(symbol or "").strip().upper(),
            timeframe=self.timeframe,
            source_bar_close_at=_ensure_utc(self.close_at).isoformat(),
            snapshot_schema_version=snapshot_schema_version,
        )


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


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

    This is NOT the same as "aligning the timestamp to the
    bucket boundary and calling it close_at". It answers a
    specific question — "given wall-clock time X, which
    completed bar just closed?" — and is only appropriate when
    the caller has no direct handle on the source bar record.
    Callers that DO have the source record MUST use
    `BarIdentity.to_parity_key` instead so the semantics come
    from the source, not from a guess.
    """
    ts = _ensure_utc(evaluation_time)
    bucket = _BUCKET_SECONDS.get(timeframe, 60)
    epoch = int(ts.timestamp())
    aligned = epoch - (epoch % bucket)
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def bar_identity_from_source(
    *,
    timeframe: str,
    bar_timestamp: datetime,
    timestamp_semantics: str,           # "open" | "close"
    source: str,
) -> BarIdentity:
    """Materialize a `BarIdentity` when the caller has read a
    source bar record and knows which end of the bar the record's
    `ts` labels.

    `shared_ohlcv_bars` labels bars by their OPEN timestamp
    (verified via `_bar_date`'s use of the ts as the session
    grouping key). Feeders that emit close-labelled bars must
    pass `timestamp_semantics="close"`.
    """
    ts = _ensure_utc(bar_timestamp)
    bucket = _BUCKET_SECONDS.get(timeframe, 60)
    if timestamp_semantics == "open":
        open_at = ts
        close_at = datetime.fromtimestamp(
            int(ts.timestamp()) + bucket, tz=timezone.utc,
        )
    elif timestamp_semantics == "close":
        close_at = ts
        open_at = datetime.fromtimestamp(
            int(ts.timestamp()) - bucket, tz=timezone.utc,
        )
    else:
        raise ValueError(
            f"timestamp_semantics must be 'open' or 'close', got {timestamp_semantics!r}",
        )
    return BarIdentity(
        timeframe=timeframe,
        open_at=open_at,
        close_at=close_at,
        source=source,
    )


def parse_parity_key(raw: Optional[str]) -> Optional[ParityKey]:
    """Reverse `ParityKey.as_string()`. Returns None if `raw`
    isn't a well-formed parity key so callers can filter cleanly."""
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.split("|")
    if len(parts) != 5:
        return None
    return ParityKey(
        brain_id=parts[0],
        symbol=parts[1],
        timeframe=parts[2],
        source_bar_close_at=parts[3],
        snapshot_schema_version=parts[4],
    )
