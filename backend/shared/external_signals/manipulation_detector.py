"""Witness manipulation/noise cluster detector — RoadGuard layer.

Doctrine pin (2026-02-23, RoadGuard witness layer):
    LOG-ONLY in v1. Detector runs, alerts persist, witness rows
    get tagged with `roadguard_labels`. NOTHING IS BLOCKED.

    The chain:
      Witness page  : displays everything (raw)
      RoadGuard     : labels suspicious witness clusters
      Seat (Step 7) : sees cleaned context (filters tagged rows)
      Verifier      : later decides source promotion

Labels (matching operator-locked vocabulary):

  EXTERNAL_SIGNAL_SPAM
      One source emits > RATE_SPIKE_PER_HOUR signals/hour, OR
      emits > SPAM_PER_SYMBOL on a single symbol in SPAM_WINDOW_MIN.

  EXTERNAL_SIGNAL_DUPLICATE_BURST
      A single article spawns N+ witness rows at the same minute
      (the DTEGY pattern we saw on first deploy — 10 distinct
      articles, all published at the exact same second).

  EXTERNAL_SIGNAL_FLIP_FLOP
      Same (source, symbol) emits BUY then SELL (or reverse) within
      FLIP_WINDOW_MIN. HOLD doesn't count — only directional flips.

  EXTERNAL_SIGNAL_SOFT_NEWS_CLUSTER
      A single article generates BUY/SELL signals on >= 3 unrelated
      tickers AND the article carries no material-news keywords
      (earnings / SEC / analyst / M&A / FDA / etc.). This is the
      "Company Access Network mentions WMT, INTU, NVDA → 3 BUYs"
      pattern from operator review.

  EXTERNAL_SIGNAL_SOURCE_DRIFT
      v1 STUB. Needs historical baseline to compare current
      behavior against. Returns no alerts until enough history
      lands. Wired so the contract is in place.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


# Thresholds — operator-tunable later
RATE_SPIKE_PER_HOUR = 400
SPAM_PER_SYMBOL = 8
SPAM_WINDOW_MIN = 60
DUPLICATE_BURST_MIN_ROWS = 5
FLIP_WINDOW_MIN = 60
SOFT_NEWS_MIN_TICKERS = 3

SEVERITY: dict[str, str] = {
    "EXTERNAL_SIGNAL_SPAM":               "WARNING",
    "EXTERNAL_SIGNAL_DUPLICATE_BURST":    "INFO",
    "EXTERNAL_SIGNAL_FLIP_FLOP":          "WARNING",
    "EXTERNAL_SIGNAL_SOFT_NEWS_CLUSTER":  "INFO",
    "EXTERNAL_SIGNAL_SOURCE_DRIFT":       "WARNING",
}

TriggerLabel = Literal[
    "EXTERNAL_SIGNAL_SPAM",
    "EXTERNAL_SIGNAL_DUPLICATE_BURST",
    "EXTERNAL_SIGNAL_FLIP_FLOP",
    "EXTERNAL_SIGNAL_SOFT_NEWS_CLUSTER",
    "EXTERNAL_SIGNAL_SOURCE_DRIFT",
]


# Material-news keywords. If ANY of these (case-insensitive,
# word-bounded) appear in title/description/keywords, the article
# is NOT soft-news — it's likely material and should NOT trip
# SOFT_NEWS_CLUSTER. Conservative on purpose: better to under-flag
# than over-flag material news.
MATERIAL_KEYWORDS: tuple[str, ...] = (
    # earnings/guidance
    "earnings", "revenue", "profit", "eps", "q1", "q2", "q3", "q4",
    "fiscal year", "guidance", "outlook", "forecast",
    # regulatory
    "sec filing", "10-k", "10-q", "8-k",
    # analyst
    "analyst", "upgrade", "downgrade", "price target", "rating",
    # macro
    "fed ", "fomc", "rate cut", "rate hike", "inflation",
    "cpi", "ppi", "gdp", "yield curve", "treasury",
    # M&A
    "merger", "acquisition", "m&a", "takeover", "buyout", "ipo",
    "spin-off", "divestiture",
    # product/regulatory product
    "fda approval", "drug approval", "product launch", "recall",
    # legal
    "lawsuit", "settled", "sued", "court ruling", "investigation",
    "doj", "ftc",
    # corporate events
    "bankruptcy", "restructure", "layoff", "executive", "ceo",
    "resigns", "appointed", "dividend",
)


class ManipulationAlert(BaseModel):
    """One detected cluster alert. Log-only in v1.

    The existence of this row does NOT modify any witness's
    `influence_allowed` field. Verifier alone owns that. The alert
    is descriptive evidence; downstream consumers (Step 7 Seat
    context filter) decide what to do with the tag.
    """
    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: TriggerLabel
    severity: str
    source: str
    symbol: Optional[str] = None
    article_id: Optional[str] = None
    observed_value: float
    threshold: float
    window_start: str
    window_end: str
    signal_ids: list[str] = Field(default_factory=list)
    detail: str
    enforced: bool = False  # v1 doctrine pin: log-only
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


# ──────────────────────── helpers ────────────────────────


_KW_RES = [
    re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE)
    for k in MATERIAL_KEYWORDS
]


def article_is_material(article: dict[str, Any]) -> bool:
    """Return True if the article carries any material-news keyword
    in title/description/keywords. Used by SOFT_NEWS_CLUSTER to
    AVOID flagging real material news.
    """
    if not isinstance(article, dict):
        return False
    blob_parts = [
        str(article.get("title") or ""),
        str(article.get("description") or ""),
        " ".join([str(k) for k in (article.get("keywords") or [])]),
    ]
    blob = " ".join(blob_parts)
    for pat in _KW_RES:
        if pat.search(blob):
            return True
    return False


# ──────────────────────── detectors ────────────────────────


def detect_spam(
    signals: list[dict[str, Any]],
    *,
    rate_per_hour: int = RATE_SPIKE_PER_HOUR,
    per_symbol: int = SPAM_PER_SYMBOL,
    window_min: int = SPAM_WINDOW_MIN,
    now: Optional[datetime] = None,
) -> list[ManipulationAlert]:
    """EXTERNAL_SIGNAL_SPAM — source-level over-emission or
    symbol-level burst. Each fires one alert."""
    now_dt = now or datetime.now(timezone.utc)
    hour_cutoff = (now_dt - timedelta(hours=1)).isoformat()
    win_cutoff = (now_dt - timedelta(minutes=window_min)).isoformat()
    per_source_ids: dict[str, list[str]] = {}
    per_pair_ids: dict[tuple[str, str], list[str]] = {}
    for s in signals:
        ra = s.get("received_at")
        if not ra:
            continue
        if ra >= hour_cutoff:
            per_source_ids.setdefault(s.get("source", "?"), []).append(s.get("id") or "")
        if ra >= win_cutoff:
            key = (s.get("source", "?"), s.get("symbol", "?"))
            per_pair_ids.setdefault(key, []).append(s.get("id") or "")

    alerts: list[ManipulationAlert] = []
    for src, ids in per_source_ids.items():
        if len(ids) > rate_per_hour:
            alerts.append(ManipulationAlert(
                label="EXTERNAL_SIGNAL_SPAM",
                severity=SEVERITY["EXTERNAL_SIGNAL_SPAM"],
                source=src, symbol=None,
                observed_value=float(len(ids)),
                threshold=float(rate_per_hour),
                window_start=hour_cutoff, window_end=now_dt.isoformat(),
                signal_ids=[i for i in ids if i][:20],
                detail=(f"{src} emitted {len(ids)} signals in last hour "
                        f"(rate threshold {rate_per_hour}). LOG-ONLY."),
            ))
    for (src, sym), ids in per_pair_ids.items():
        if len(ids) > per_symbol:
            alerts.append(ManipulationAlert(
                label="EXTERNAL_SIGNAL_SPAM",
                severity=SEVERITY["EXTERNAL_SIGNAL_SPAM"],
                source=src, symbol=sym,
                observed_value=float(len(ids)),
                threshold=float(per_symbol),
                window_start=win_cutoff, window_end=now_dt.isoformat(),
                signal_ids=[i for i in ids if i][:20],
                detail=(f"{src} emitted {len(ids)} signals on {sym} in last "
                        f"{window_min}min (per-symbol threshold {per_symbol}). LOG-ONLY."),
            ))
    return alerts


def detect_duplicate_burst(
    signals: list[dict[str, Any]],
    *,
    min_rows: int = DUPLICATE_BURST_MIN_ROWS,
) -> list[ManipulationAlert]:
    """EXTERNAL_SIGNAL_DUPLICATE_BURST — one article spawns >= N rows
    in the input window. We use the raw payload's `id` field as
    the article anchor (Polygon's article_id)."""
    per_article: dict[str, list[dict[str, Any]]] = {}
    for s in signals:
        raw = s.get("raw") or {}
        aid = raw.get("id") if isinstance(raw, dict) else None
        if not aid:
            continue
        per_article.setdefault(aid, []).append(s)

    alerts: list[ManipulationAlert] = []
    for aid, items in per_article.items():
        if len(items) < min_rows:
            continue
        src = items[0].get("source", "?")
        alerts.append(ManipulationAlert(
            label="EXTERNAL_SIGNAL_DUPLICATE_BURST",
            severity=SEVERITY["EXTERNAL_SIGNAL_DUPLICATE_BURST"],
            source=src, symbol=None, article_id=aid,
            observed_value=float(len(items)),
            threshold=float(min_rows),
            window_start=min((x.get("received_at") or "") for x in items),
            window_end=max((x.get("received_at") or "") for x in items),
            signal_ids=[x.get("id") or "" for x in items if x.get("id")][:20],
            detail=(f"article {aid[:12]}… spawned {len(items)} witness rows "
                    f"(burst threshold {min_rows}). LOG-ONLY."),
        ))
    return alerts


def detect_flip_flop(
    signals: list[dict[str, Any]],
    *,
    window_min: int = FLIP_WINDOW_MIN,
    now: Optional[datetime] = None,
) -> list[ManipulationAlert]:
    """EXTERNAL_SIGNAL_FLIP_FLOP — same (source, symbol) flips
    BUY↔SELL within window."""
    now_dt = now or datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(minutes=window_min)).isoformat()
    grouped: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for s in signals:
        ra = s.get("received_at")
        if not ra or ra < cutoff:
            continue
        if s.get("side") not in ("BUY", "SELL"):
            continue
        grouped.setdefault(
            (s.get("source", "?"), s.get("symbol", "?")), [],
        ).append((ra, s["side"], s.get("id") or ""))

    alerts: list[ManipulationAlert] = []
    for (src, sym), entries in grouped.items():
        entries.sort()
        for (ra1, side1, id1), (ra2, side2, id2) in zip(entries, entries[1:]):
            if side1 != side2:
                alerts.append(ManipulationAlert(
                    label="EXTERNAL_SIGNAL_FLIP_FLOP",
                    severity=SEVERITY["EXTERNAL_SIGNAL_FLIP_FLOP"],
                    source=src, symbol=sym,
                    observed_value=1.0, threshold=0.0,
                    window_start=ra1, window_end=ra2,
                    signal_ids=[id1, id2],
                    detail=(f"{src} flipped {sym} {side1}→{side2} between "
                            f"{ra1} and {ra2} (within {window_min}min). LOG-ONLY."),
                ))
    return alerts


def detect_soft_news_cluster(
    signals: list[dict[str, Any]],
    *,
    min_tickers: int = SOFT_NEWS_MIN_TICKERS,
) -> list[ManipulationAlert]:
    """EXTERNAL_SIGNAL_SOFT_NEWS_CLUSTER — single article with
    BUY/SELL signals on >= min_tickers AND no material-news
    keywords. Catches the "Company Access Network" pattern.
    """
    per_article_dir: dict[str, list[dict[str, Any]]] = {}
    per_article_raw: dict[str, dict[str, Any]] = {}
    for s in signals:
        if s.get("side") not in ("BUY", "SELL"):
            continue
        raw = s.get("raw") or {}
        aid = raw.get("id") if isinstance(raw, dict) else None
        if not aid:
            continue
        per_article_dir.setdefault(aid, []).append(s)
        per_article_raw[aid] = raw

    alerts: list[ManipulationAlert] = []
    for aid, items in per_article_dir.items():
        if len(items) < min_tickers:
            continue
        article = per_article_raw.get(aid) or {}
        if article_is_material(article):
            # Real material news touching multiple tickers — that's
            # legitimate (e.g. M&A news mentioning acquirer + target).
            # Don't tag.
            continue
        src = items[0].get("source", "?")
        title = (article.get("title") or "")[:80]
        alerts.append(ManipulationAlert(
            label="EXTERNAL_SIGNAL_SOFT_NEWS_CLUSTER",
            severity=SEVERITY["EXTERNAL_SIGNAL_SOFT_NEWS_CLUSTER"],
            source=src, symbol=None, article_id=aid,
            observed_value=float(len(items)),
            threshold=float(min_tickers),
            window_start=min((x.get("received_at") or "") for x in items),
            window_end=max((x.get("received_at") or "") for x in items),
            signal_ids=[x.get("id") or "" for x in items if x.get("id")][:20],
            detail=(f"article '{title}…' generated {len(items)} directional "
                    f"signals across unrelated tickers with no material-news "
                    f"keywords detected. LOG-ONLY."),
        ))
    return alerts


def detect_source_drift(
    signals: list[dict[str, Any]],  # noqa: ARG001 — stub signature
) -> list[ManipulationAlert]:
    """EXTERNAL_SIGNAL_SOURCE_DRIFT — v1 STUB.

    Needs historical baseline (mean & stddev of source emission
    rate, sentiment distribution, BUY/SELL ratio over the trailing
    7-30d) to compare current tick against. Returns no alerts
    until baseline data exists. Wired so the contract is in
    place — Step 9 (Verifier) is the natural place to compute
    rolling baselines and surface them here.
    """
    return []


def run_all_detectors(
    signals: list[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> list[ManipulationAlert]:
    """Run every trigger. Pure, deterministic, no side-effects."""
    out: list[ManipulationAlert] = []
    out.extend(detect_spam(signals, now=now))
    out.extend(detect_duplicate_burst(signals))
    out.extend(detect_flip_flop(signals, now=now))
    out.extend(detect_soft_news_cluster(signals))
    out.extend(detect_source_drift(signals))
    return out


__all__ = (
    "RATE_SPIKE_PER_HOUR",
    "SPAM_PER_SYMBOL",
    "SPAM_WINDOW_MIN",
    "DUPLICATE_BURST_MIN_ROWS",
    "FLIP_WINDOW_MIN",
    "SOFT_NEWS_MIN_TICKERS",
    "MATERIAL_KEYWORDS",
    "ManipulationAlert",
    "article_is_material",
    "detect_spam",
    "detect_duplicate_burst",
    "detect_flip_flop",
    "detect_soft_news_cluster",
    "detect_source_drift",
    "run_all_detectors",
)
